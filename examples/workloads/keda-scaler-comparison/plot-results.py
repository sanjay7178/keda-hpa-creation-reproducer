#!/usr/bin/env python3
"""Create issue-ready PNG charts from one KEDA scaler matrix result directory."""

import argparse
import csv
import json
import math
import re
import statistics
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError as exc:
    raise SystemExit("Pillow is required for charts: python3 -m pip install Pillow") from exc


DEFAULT_SCALERS = ("kubernetes-resource", "opensearch", "temporal")
COLORS = ("#2563eb", "#059669", "#d97706", "#7c3aed", "#dc2626", "#0891b2")
INK = "#17243a"
MUTED = "#54647a"
GRID = "#dbe2eb"
PAPER = "#ffffff"


def font(size, bold=False):
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    path = Path("/usr/share/fonts/truetype/dejavu") / name
    try:
        return ImageFont.truetype(str(path), size)
    except OSError:
        return ImageFont.load_default()


FONTS = {size: font(size) for size in (17, 19, 21, 23, 26, 33)}
BOLD = {size: font(size, True) for size in (17, 19, 21, 23, 26, 33)}


def version_key(version):
    return tuple(int(part) for part in re.findall(r"\d+", version))


def supported(version, scaler):
    minimum = {"kubernetes-resource": "v2.19.0", "opensearch": "v2.20.0",
               "temporal": "v2.17.0"}.get(scaler)
    return minimum is None or version_key(version) >= version_key(minimum)


def read_csv(path):
    if not path.is_file():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def key(row):
    return (
        row["version"], row["scaler"], row["mode"],
        int(row["objects"]), row["run"],
    )


def load_runs(root, scalers):
    runs = {}
    for path in root.rglob("assertions.json"):
        row = json.loads(path.read_text(encoding="utf-8"))
        if row["scaler"] not in scalers:
            continue
        run_key = (
            row["version"], row["scaler"], row["mode"],
            int(row["expected"]), row["repeat"],
        )
        if run_key in runs:
            raise ValueError(f"duplicate run key: {run_key}")
        row["directory"] = path.parent
        row["hpa_ratio"] = min(1.0, int(row["hpaComplete"]) / int(row["expected"]))
        runs[run_key] = row

    for row in read_csv(root / "summary-runs.csv"):
        if row["metric"] != "HPACreated":
            continue
        run = runs.get(key(row))
        if run is not None:
            run["hpa_p95_seconds"] = float(row["p95_ms"]) / 1000.0
            run["hpa_samples"] = int(row["samples"])

    for row in read_csv(root / "summary-keda-metrics.csv"):
        run = runs.get(key(row))
        if run is not None:
            run["loop_p99_ms"] = float(row["p99_ms"])
            run["scaler_errors"] = int(float(row["scaler_errors"]))

    for run in runs.values():
        usage = run["directory"] / "keda-resource-usage.txt"
        if usage.is_file():
            values = []
            for line in usage.read_text(encoding="utf-8", errors="replace").splitlines():
                parts = line.split()
                if len(parts) >= 4 and parts[1] == "keda-operator":
                    value = parse_memory_mib(parts[3])
                    if value is not None:
                        values.append(value)
            if values:
                run["operator_peak_mib"] = max(values)
        run["valid"] = (
            run["passed"] is True
            and run.get("hpa_samples") == int(run["expected"])
            and run.get("hpa_p95_seconds") is not None
        )
    return runs


def parse_memory_mib(value):
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)(Ki|Mi|Gi|Ti|K|M|G)?", value)
    if not match:
        return None
    amount = float(match.group(1))
    multiplier = {
        None: 1 / 1048576, "Ki": 1 / 1024, "Mi": 1, "Gi": 1024,
        "Ti": 1024 * 1024, "K": 1000 / 1048576,
        "M": 1000000 / 1048576, "G": 1000000000 / 1048576,
    }
    return amount * multiplier[match.group(2)]


def chart_base(width, height, title, subtitle):
    image = Image.new("RGB", (width, height), PAPER)
    draw = ImageDraw.Draw(image)
    draw.text((54, 28), title, font=BOLD[33], fill=INK)
    draw.text((56, 77), subtitle, font=FONTS[19], fill=MUTED)
    draw.line((54, 112, width - 54, 112), fill=GRID, width=2)
    return image, draw


def centered_text(draw, x, y, value, chosen_font, fill):
    bounds = draw.textbbox((0, 0), value, font=chosen_font)
    draw.text((x - (bounds[2] - bounds[0]) / 2, y), value, font=chosen_font, fill=fill)


def series_for(runs, scaler, version, density, field, expected_repeats):
    members = [run for run in runs.values() if run["scaler"] == scaler
               and run["version"] == version and int(run["expected"]) == density]
    if len(members) != expected_repeats or not all(run["valid"] for run in members):
        return None
    values = [run[field] for run in members if field in run]
    if len(values) != len(members):
        return None
    return statistics.mean(values), min(values), max(values), len(values)


def draw_line_chart(root, runs, scalers, versions, densities, expected_repeats,
                    field, name, title, unit, description):
    panel_height = 340
    width = 1560
    height = 152 + panel_height * len(scalers) + 76
    image, draw = chart_base(width, height, title, description)
    color_by_version = {version: COLORS[index % len(COLORS)] for index, version in enumerate(versions)}
    for panel_index, scaler in enumerate(scalers):
        top = 145 + panel_index * panel_height
        left, right = 145, 1160
        plot_top, plot_bottom = top + 53, top + 252
        draw.text((54, top), scaler, font=BOLD[23], fill=INK)
        candidates = [series_for(runs, scaler, version, density, field, expected_repeats)
                      for version in versions for density in densities]
        visible = [item for item in candidates if item is not None]
        maximum = max((item[2] for item in visible), default=0)
        upper = nice_ceiling(maximum * 1.13)
        for tick in range(5):
            value = upper * tick / 4
            y = round(plot_bottom - (plot_bottom - plot_top) * tick / 4)
            draw.line((left, y, right, y), fill=GRID, width=1)
            draw.text((55, y - 12), f"{value:g}", font=FONTS[17], fill=MUTED)
        draw.text((55, plot_top - 33), unit, font=FONTS[17], fill=MUTED)
        x_positions = [left + 35 + (right - left - 70) * i / max(1, len(densities) - 1)
                       for i in range(len(densities))]
        for x, density in zip(x_positions, densities):
            draw.text((int(x - 24), plot_bottom + 12), str(density), font=FONTS[17], fill=MUTED)
        draw.text((right - 125, plot_bottom + 42), "ScaledObjects", font=FONTS[17], fill=MUTED)
        for version_index, version in enumerate(versions):
            color = color_by_version[version]
            points = []
            for x, density in zip(x_positions, densities):
                item = series_for(runs, scaler, version, density, field, expected_repeats)
                if item is None:
                    if len(points) > 1:
                        draw.line(points, fill=color, width=3, joint="curve")
                    points = []
                    continue
                mean, low, high, count = item
                x += (version_index - (len(versions) - 1) / 2) * 12
                y = plot_bottom - mean / upper * (plot_bottom - plot_top)
                low_y = plot_bottom - low / upper * (plot_bottom - plot_top)
                high_y = plot_bottom - high / upper * (plot_bottom - plot_top)
                if count > 1:
                    draw.line((x, high_y, x, low_y), fill=color, width=2)
                    draw.line((x - 7, high_y, x + 7, high_y), fill=color, width=2)
                    draw.line((x - 7, low_y, x + 7, low_y), fill=color, width=2)
                points.append((x, y))
                draw.ellipse((x - 6, y - 6, x + 6, y + 6), fill=color, outline=PAPER, width=2)
            if len(points) > 1:
                draw.line(points, fill=color, width=3, joint="curve")
            legend_y = plot_top + 9 + version_index * 34
            draw.line((1210, legend_y + 9, 1245, legend_y + 9), fill=color, width=4)
            draw.text((1256, legend_y), version, font=FONTS[19], fill=INK)
        if not visible:
            draw.text((left + 260, plot_top + 82), "No complete cells with this metric", font=FONTS[21], fill=MUTED)
        draw.line((54, top + panel_height - 12, width - 54, top + panel_height - 12), fill=GRID, width=1)
    draw.text((56, height - 46),
              "Points = mean across complete runs; whiskers = run range. Incomplete cells are omitted; see completion chart.",
              font=FONTS[17], fill=MUTED)
    output = root / "figures" / name
    image.save(output)
    return output


def nice_ceiling(value):
    if value <= 0:
        return 1.0
    power = 10 ** math.floor(math.log10(value))
    for step in (1, 1.5, 2, 2.5, 3, 4, 5, 7.5, 10):
        if step * power >= value:
            return float(step * power)
    return float(10 * power)


def draw_version_contrast(root, runs, scaler, versions, densities, expected_repeats):
    """Make one directly labeled release comparison per scaler."""
    slot = 91
    group_width = max(330, len(versions) * slot + 34)
    width = max(1180, 205 + len(densities) * group_width)
    height = 770
    image, draw = chart_base(
        width, height, f"{scaler}: KEDA version comparison",
        "HPA creation P95 · seconds · only complete runs contribute a bar",
    )
    plot_top, plot_bottom = 199, 562
    visible = [series_for(runs, scaler, version, density, "hpa_p95_seconds", expected_repeats)
               for density in densities for version in versions]
    upper = nice_ceiling(max((item[2] for item in visible if item is not None), default=0) * 1.25)
    left, right = 118, width - 48
    for tick in range(5):
        value = upper * tick / 4
        y = round(plot_bottom - (plot_bottom - plot_top) * tick / 4)
        draw.line((left, y, right, y), fill=GRID, width=1)
        draw.text((50, y - 11), f"{value:g}", font=FONTS[17], fill=MUTED)
    draw.text((50, plot_top - 30), "sec", font=FONTS[17], fill=MUTED)

    for density_index, density in enumerate(densities):
        group_left = 118 + density_index * group_width
        group_right = group_left + group_width - 12
        if density_index % 2 == 0:
            draw.rectangle((group_left, plot_top, group_right, plot_bottom), fill="#f8fafc")
            for tick in range(5):
                y = round(plot_bottom - (plot_bottom - plot_top) * tick / 4)
                draw.line((group_left, y, group_right, y), fill=GRID, width=1)
        baseline = next((item[0] for version in versions
                         if (item := series_for(runs, scaler, version, density,
                                                "hpa_p95_seconds", expected_repeats)) is not None), None)
        start = group_left + (group_width - len(versions) * slot) / 2
        for version_index, version in enumerate(versions):
            x = start + version_index * slot + 11
            bar_width = 65
            center = x + bar_width / 2
            color = COLORS[version_index % len(COLORS)]
            item = series_for(runs, scaler, version, density, "hpa_p95_seconds", expected_repeats)
            if item is None:
                draw.rectangle((x, plot_bottom - 50, x + bar_width, plot_bottom),
                               fill="#edf0f4", outline="#96a3b5", width=2)
                draw.line((x + 4, plot_bottom - 46, x + bar_width - 4, plot_bottom - 4),
                          fill="#96a3b5", width=2)
                draw.line((x + bar_width - 4, plot_bottom - 46, x + 4, plot_bottom - 4),
                          fill="#96a3b5", width=2)
                status = "N/A" if not supported(version, scaler) else "partial"
                centered_text(draw, center, plot_bottom - 76, status, FONTS[17], MUTED)
            else:
                mean, _low, _high, _count = item
                top = plot_bottom - mean / upper * (plot_bottom - plot_top)
                draw.rectangle((x, top, x + bar_width, plot_bottom), fill=color)
                centered_text(draw, center, top - 55, f"{mean:.1f}s", BOLD[17], INK)
                if baseline is not None:
                    change = (mean / baseline - 1) * 100 if baseline else 0
                    detail = "baseline" if mean == baseline else f"{change:+.1f}%"
                    centered_text(draw, center, top - 32, detail, FONTS[17], MUTED)
            centered_text(draw, center, plot_bottom + 15, version.removeprefix("v"), FONTS[17], INK)
        centered_text(draw, group_left + group_width / 2, plot_bottom + 70,
                      f"{density} ScaledObjects", BOLD[19], INK)
    draw.text((56, 705),
              "Bar = mean of each run's P95. Percentage compares with the earliest complete release at that density.",
              font=FONTS[17], fill=MUTED)
    draw.text((56, 733),
              "Crossed gray blocks are unsupported or incomplete; use the completion chart to distinguish them.",
              font=FONTS[17], fill=MUTED)
    output = root / "figures" / f"version-contrast-{scaler}.png"
    image.save(output)
    return output


def draw_completion(root, runs, scalers, versions, densities, expected_repeats):
    width = 1560
    row_height = 54
    height = 184 + len(scalers) * (61 + row_height * len(versions)) + 82
    image, draw = chart_base(width, height, "HPA completion and cell validity",
                             "Each cell shows HPA completion and passing runs; gray means the scaler is unsupported on that release.")
    column_left = 545
    column_width = min(260, (width - column_left - 58) // max(1, len(densities)))
    for index, density in enumerate(densities):
        draw.text((column_left + index * column_width + 15, 145),
                  f"{density} objects", font=BOLD[19], fill=INK)
    y = 190
    for scaler in scalers:
        draw.text((54, y), scaler, font=BOLD[23], fill=INK)
        y += 48
        for version in versions:
            draw.text((76, y + 12), version, font=FONTS[19], fill=INK)
            for index, density in enumerate(densities):
                members = [run for run in runs.values() if run["scaler"] == scaler
                           and run["version"] == version and int(run["expected"]) == density]
                x = column_left + index * column_width
                if not supported(version, scaler):
                    fill = "#f0f2f5"
                    label = "unsupported"
                else:
                    ratio = sum(run["hpa_ratio"] for run in members) / expected_repeats
                    passed = sum(run["valid"] for run in members)
                    if passed == expected_repeats and ratio == 1:
                        fill = "#d6f5e6"
                    elif ratio >= 0.8:
                        fill = "#fff0c2"
                    else:
                        fill = "#ffd8d8"
                    label = f"{ratio:.0%}  ·  {passed}/{expected_repeats} pass"
                draw.rounded_rectangle((x, y + 2, x + column_width - 12, y + 46),
                                       radius=8, fill=fill)
                draw.text((x + 13, y + 13), label, font=FONTS[17], fill=INK)
            y += row_height
        y += 13
    draw.text((56, height - 52),
              "A run passes only when all expected ScaledObjects and HPAs exist and kube-burner succeeds.",
              font=FONTS[17], fill=MUTED)
    output = root / "figures" / "hpa-completion.png"
    image.save(output)
    return output


def write_report(root, runs, figures, contrasts, config, scalers, versions, densities, expected_repeats):
    failures = [run for run in runs.values() if not run["valid"]]
    modes = sorted({run["mode"] for run in runs.values()})
    all_runs = len(runs)
    lines = [
        "# Recently added KEDA scalers: chart guide", "",
        f"Matrix mode: `{', '.join(modes)}`. Complete runs: **{all_runs - len(failures)}/{all_runs}**.", "",
        "These figures are descriptive evidence, not a regression claim by themselves. "
        "Compare the same density, mode, QPS, polling interval, observation window, and profiling setting across releases.", "",
    ]
    if config:
        fields = ("versions", "scalers", "densities", "qps", "burst", "pollingInterval",
                  "settleDuration", "settleDurations", "kubeBurnerTimeout",
                  "namespaceDeleteTimeout", "profileKeda", "versionOrder")
        lines.extend(["## Run settings", "",
                      "| Setting | Value |", "|---|---|"])
        for field in fields:
            if field in config:
                lines.append(f"| {field} | `{config[field]}` |")
        lines.append("")
    lines.extend([
        "## Figures", "",
        "The version-contrast charts directly label each release at each density. "
        "The percentage on a bar compares its P95 with the earliest complete release at that density; "
        "crossed gray blocks have no complete result.", "",
    ])
    for scaler, filename in contrasts.items():
        lines.extend([f"### {scaler}", "", f"![{scaler} version comparison](figures/{filename})", ""])
    lines.extend([
        "HPA creation P95 is the mean of each complete run's P95, with whiskers showing the run range. "
        "It includes the configured creation throttle, so compare only runs with the same QPS and burst.", "",
        f"![HPA creation P95](figures/{figures['hpa']})", "",
        "The completion chart identifies cells that should not be used as latency evidence.", "",
        f"![HPA completion](figures/{figures['completion']})", "",
        "Operator memory is the mean of the peak sampled container memory in each complete run; "
        "these samples are taken throughout the cell, including dependency setup.", "",
        f"![Operator peak memory](figures/{figures['memory']})", "",
        "The internal scale-loop P99 is an **end-of-run snapshot across ScaledObjects**, "
        "not a time-series P99 over the whole workload.", "",
        f"![Internal scale-loop P99](figures/{figures['loop']})", "",
        "## Excluded or incomplete runs", "",
    ])
    if failures:
        lines.extend(["| Version | Scaler | Objects | Run | HPAs | Reason |",
                      "|---|---|---:|---|---:|---|"])
        for run in sorted(failures, key=lambda item: (item["scaler"], version_key(item["version"]),
                                                       item["expected"], item["repeat"])):
            reason = "invariant failed" if not run["passed"] else "missing HPA latency samples"
            lines.append(f"| {run['version']} | {run['scaler']} | {run['expected']} | {run['repeat']} | "
                         f"{run['hpaComplete']} | {reason} |")
    else:
        lines.append("None.\n")
    missing = []
    for scaler in scalers:
        for version in versions:
            if not supported(version, scaler):
                continue
            for density in densities:
                count = sum(1 for run in runs.values() if run["scaler"] == scaler
                            and run["version"] == version and int(run["expected"]) == density)
                if count < expected_repeats:
                    missing.append((version, scaler, density, count))
    if missing:
        lines.extend(["", "## Missing runs", "",
                      "These cells were expected by the matrix configuration but have fewer result files than requested.", "",
                      "| Version | Scaler | Objects | Runs found / requested |",
                      "|---|---|---:|---:|"])
        for version, scaler, density, count in missing:
            lines.append(f"| {version} | {scaler} | {density} | {count}/{expected_repeats} |")
    lines.extend(["", "Attach the relevant PNG figures and the raw `summary-runs.csv`, "
                  "`summary-keda-metrics.csv`, and `assertions.json` files to the issue. "
                  "A focused last-good/first-bad comparison with repeated runs is stronger than a broad discovery matrix.", ""])
    output = root / "visualization.md"
    output.write_text("\n".join(lines), encoding="utf-8")
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results_root", type=Path)
    parser.add_argument("--scalers", nargs="+", default=list(DEFAULT_SCALERS),
                        help="scalers to plot (default: the three recently added examples)")
    args = parser.parse_args()
    root = args.results_root.resolve()
    if not root.is_dir():
        parser.error(f"results directory does not exist: {root}")
    config_path = root / "matrix-config.json"
    config = json.loads(config_path.read_text(encoding="utf-8")) if config_path.is_file() else {}
    runs = load_runs(root, set(args.scalers))
    if not runs:
        parser.error("no assertions.json files found for the selected scalers")
    modes = {run["mode"] for run in runs.values()}
    if len(modes) != 1:
        parser.error(f"mixed workload modes cannot share charts: {sorted(modes)}")
    scalers = [scaler for scaler in args.scalers if scaler in config.get("scalers", [])
               or any(run["scaler"] == scaler for run in runs.values())]
    versions = sorted(config.get("versions") or {run["version"] for run in runs.values()}, key=version_key)
    densities = sorted(int(value) for value in (config.get("densities") or
                                               {run["expected"] for run in runs.values()}))
    expected_repeats = int(config.get("repeats", 1))
    (root / "figures").mkdir(exist_ok=True)
    figures = {
        "hpa": draw_line_chart(root, runs, scalers, versions, densities, expected_repeats, "hpa_p95_seconds",
                               "hpa-creation-p95.png", "HPA creation latency, P95", "seconds",
                               "Recent scalers · complete cells only · same creation QPS required for comparison").name,
        "completion": draw_completion(root, runs, scalers, versions, densities, expected_repeats).name,
        "memory": draw_line_chart(root, runs, scalers, versions, densities, expected_repeats, "operator_peak_mib",
                                  "operator-peak-memory.png", "KEDA operator peak memory", "MiB",
                                  "Recent scalers · mean of sampled per-run peaks · complete cells only").name,
        "loop": draw_line_chart(root, runs, scalers, versions, densities, expected_repeats, "loop_p99_ms",
                                "internal-scale-loop-p99.png", "KEDA internal scale-loop P99", "ms",
                                "End-of-run snapshot across ScaledObjects · complete cells only").name,
    }
    contrasts = {scaler: draw_version_contrast(root, runs, scaler, versions,
                                               densities, expected_repeats).name for scaler in scalers}
    report = write_report(root, runs, figures, contrasts, config, scalers,
                          versions, densities, expected_repeats)
    print(f"Wrote {report} and {len(figures) + len(contrasts)} PNG figures in {root / 'figures'}")


if __name__ == "__main__":
    main()
