#!/usr/bin/env python3
"""Build a Grafana dashboard containing validated, archived benchmark CSV data."""

import argparse
import csv
import hashlib
import io
import json
import math
import re
import statistics
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path


DATASOURCE_TYPE = "grafana-testdata-datasource"
DATASOURCE_UID = "keda-benchmark-csv"
INPUT_NAME = "DS_KEDA_ARCHIVE"
COLORS = ("#5794F2", "#73BF69", "#FF9830", "#B877D9", "#FA6400")


def csv_text(headers, rows):
    output = io.StringIO()
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(headers)
    writer.writerows(rows)
    return output.getvalue()


def version_key(version):
    return tuple(int(value) for value in re.findall(r"\d+", version))


def source_key(row):
    return row["version"], row["scaler"], int(row["objects"]), row["run"]


def load_validated_results(root):
    with (root / "summary-runs.csv").open(newline="") as handle:
        rows = [row for row in csv.DictReader(handle) if row["metric"] == "HPACreated"]
    config = json.loads((root / "matrix-config.json").read_text())
    if config["mode"] != "control-plane":
        raise ValueError("This dashboard validates the archived control-plane timestamp measurements")
    assertions = {}
    for path in root.glob("v*/*/objects-*/run-*/assertions.json"):
        row = json.loads(path.read_text())
        key = row["version"], row["scaler"], int(row["expected"]), row["repeat"]
        if key in assertions:
            raise ValueError(f"Duplicate assertion: {key}")
        assertions[key] = row
    seen = set()
    groups = defaultdict(list)
    time_bounds = []
    for row in rows:
        key = source_key(row)
        if key in seen:
            raise ValueError(f"Duplicate per-run measurement: {key}")
        seen.add(key)
        assertion = assertions[key]
        if not assertion["passed"] or row["mode"] != "control-plane":
            raise ValueError(f"Incomplete or mixed-mode measurement: {key}")
        if int(row["samples"]) != key[2] or assertion["hpaComplete"] != key[2]:
            raise ValueError(f"Incomplete HPA samples: {key}")
        path = root / key[0] / key[1] / f"objects-{key[2]}" / key[3] / "control-plane-hpa-latency.json"
        measurements = json.loads(path.read_text())
        values = sorted(float(item["hpaCreatedLatency"]) for item in measurements)
        for item in measurements:
            start = datetime.fromisoformat(item["timestamp"].replace("Z", "+00:00"))
            time_bounds.extend((start, start + timedelta(milliseconds=item["hpaCreatedLatency"])))
        if len(values) != key[2] or any(not math.isfinite(v) or v < 0 for v in values):
            raise ValueError(f"Invalid per-object values: {key}")
        for field, quantile in (("p50_ms", 0.50), ("p95_ms", 0.95), ("p99_ms", 0.99)):
            measured = values[math.floor((len(values) - 1) * quantile)]
            if not math.isclose(measured, float(row[field]), abs_tol=0.001):
                raise ValueError(f"CSV disagrees with per-object {field}: {key}")
        groups[key[:3]].append(row)
    passing_keys = {key for key, row in assertions.items() if row["passed"]}
    if seen != passing_keys:
        raise ValueError("Per-run CSV does not cover exactly the passing cells")
    with (root / "summary.csv").open(newline="") as handle:
        for row in csv.DictReader(handle):
            if row["metric"] != "HPACreated":
                continue
            key = row["version"], row["scaler"], int(row["objects"])
            samples = groups[key]
            expected = statistics.mean(float(item["p95_ms"]) for item in samples)
            if len(samples) != int(row["runs"]) or not math.isclose(expected, float(row["mean_p95_ms"]), abs_tol=0.02):
                raise ValueError(f"Aggregate CSV disagrees with per-run P95s: {key}")
    server_versions = set()
    for path in root.glob("v*/installation/kubernetes-version.yml"):
        contents = path.read_text()
        if "serverVersion:" not in contents:
            continue
        section = contents.split("serverVersion:", 1)[1]
        match = re.search(r'gitVersion:\s*["\']?([^\s"\']+)', section)
        if match:
            server_versions.add(match.group(1))
    return rows, groups, assertions, config, time_bounds, sorted(server_versions)


def panel(panel_id, title, kind, position, headers=None, rows=None, description=""):
    result = {"id": panel_id, "title": title, "type": kind,
              "gridPos": dict(zip(("x", "y", "w", "h"), position)), "description": description}
    if headers is not None:
        datasource = {"type": DATASOURCE_TYPE, "uid": "${" + INPUT_NAME + "}"}
        result.update({"datasource": datasource, "targets": [{"refId": "A", "datasource": datasource,
            "scenarioId": "csv_content", "csvContent": csv_text(headers, rows), "dropPercent": 0}]})
        result["fieldConfig"] = {"defaults": {"decimals": 1, "color": {"mode": "palette-classic"}}, "overrides": []}
    return result


def statistic(panel_id, title, value, unit, position, description=""):
    result = panel(panel_id, title, "stat", position, [title], [[value]], description)
    result["fieldConfig"]["defaults"].update({"unit": unit, "color": {"mode": "fixed", "fixedColor": "#B877D9"}})
    if title == "Supported cells passed":
        result["fieldConfig"]["defaults"]["decimals"] = 0
    result["options"] = {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                         "colorMode": "value", "graphMode": "none", "textMode": "value", "orientation": "auto"}
    return result


def text_panel(panel_id, title, content, position):
    result = panel(panel_id, title, "text", position)
    result["options"] = {"mode": "markdown", "content": content}
    return result


def table_panel(panel_id, title, headers, rows, position, description=""):
    result = panel(panel_id, title, "table", position, headers, rows, description)
    result["options"] = {"showHeader": True, "cellHeight": "sm", "footer": {"show": False}}
    result["fieldConfig"]["defaults"]["custom"] = {"filterable": True, "align": "auto"}
    return result


def line_panel(panel_id, title, position, headers, rows, description, unit, colors):
    """XY lines use numeric x values, never invented wall-clock timestamps."""
    result = panel(panel_id, title, "xychart", position, headers, rows, description)
    result["options"] = {"mapping": "auto", "series": [{}],
                         "legend": {"showLegend": True, "displayMode": "list", "placement": "bottom", "calcs": []},
                         "tooltip": {"mode": "single"}}
    result["fieldConfig"]["defaults"].update({"unit": unit, "min": 0,
        "custom": {"show": "lines", "lineWidth": 2, "pointSize": {"fixed": 4},
                   "axisLabel": title, "axisPlacement": "auto"}})
    result["fieldConfig"]["overrides"] = [{"matcher": {"id": "byName", "options": headers[0]},
        "properties": [{"id": "unit", "value": "none"}, {"id": "min", "value": min([row[0] for row in rows] + [0])},
                       {"id": "custom.axisLabel", "value": headers[0]}]}]
    for name in headers[1:]:
        properties = [{"id": "color", "value": {"mode": "fixed", "fixedColor": colors[name]}}]
        if "ScaledObjects" in name or "metrics API" in name:
            properties.append({"id": "custom.lineStyle", "value": {"fill": "dash", "dash": [6, 4]}})
        elif "webhooks" in name:
            properties.append({"id": "custom.lineStyle", "value": {"fill": "dash", "dash": [2, 4]}})
        result["fieldConfig"]["overrides"].append({"matcher": {"id": "byName", "options": name}, "properties": properties})
    return result


def creation_progress(path):
    objects = json.loads(path.read_text())
    start = min(datetime.fromisoformat(item["timestamp"].replace("Z", "+00:00")) for item in objects)
    events = defaultdict(lambda: [0, 0])
    for item in objects:
        created = datetime.fromisoformat(item["timestamp"].replace("Z", "+00:00"))
        events[(created - start).total_seconds()][0] += 1
        events[(created - start).total_seconds() + item["hpaCreatedLatency"] / 1000][1] += 1
    counts = [0, 0]
    rows = []
    for elapsed, increments in sorted(events.items()):
        # Duplicate x positions express the discrete jump without smoothing.
        rows.append([elapsed] + counts[:])
        counts = [a + b for a, b in zip(counts, increments)]
        rows.append([elapsed] + counts[:])
    if counts != [len(objects), len(objects)]:
        raise ValueError(f"Incomplete creation progress: {path}")
    return start, rows


def resource_samples(path, start):
    components = {"keda-operator": "operator", "keda-operator-metrics-apiserver": "metrics API",
                  "keda-admission-webhooks": "webhooks"}
    samples = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0]))
    timestamp = None
    for line in path.read_text().splitlines():
        if re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", line):
            timestamp = datetime.fromisoformat(line.replace("Z", "+00:00"))
            continue
        fields = line.split()
        if timestamp is None or len(fields) != 4 or fields[1] not in components:
            continue
        cpu = re.fullmatch(r"(\d+(?:\.\d+)?)(m|n|u)?", fields[2])
        memory = re.fullmatch(r"(\d+(?:\.\d+)?)(Ki|Mi|Gi|Ti|K|M|G)?", fields[3])
        if not cpu or not memory:
            raise ValueError(f"Unrecognized kubectl top units: {line}")
        cpu_m = float(cpu[1]) * {None: 1000, "m": 1, "u": 0.001, "n": 0.000001}[cpu[2]]
        memory_mib = float(memory[1]) * {None: 1 / 2**20, "Ki": 1 / 1024, "Mi": 1,
            "Gi": 1024, "Ti": 1024**2, "K": 1000 / 2**20, "M": 1e6 / 2**20, "G": 1e9 / 2**20}[memory[2]]
        component = components[fields[1]]
        values = samples[(timestamp - start).total_seconds()][component]
        values[0] += cpu_m
        values[1] += memory_mib
    if not samples:
        raise ValueError(f"No resource observations: {path}")
    return samples


def prometheus_snapshot(path, namespace):
    """Parse selected native metrics; do not invent a scrape timestamp or rate."""
    wanted = {"go_goroutines", "process_resident_memory_bytes", "go_memstats_heap_alloc_bytes",
              "controller_runtime_reconcile_total", "workqueue_depth", "keda_internal_scale_loop_latency_seconds",
              "keda_scaler_metrics_latency_seconds", "keda_scaler_detail_errors_total"}
    metrics = defaultdict(list)
    for line in path.read_text().splitlines():
        if not line or line.startswith("#") or line.split("{", 1)[0].split()[0] not in wanted:
            continue
        match = re.fullmatch(r'([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(.*)\})?\s+(\S+)(?:\s+\S+)?', line)
        if not match:
            raise ValueError(f"Invalid Prometheus sample in {path}")
        labels = dict(re.findall(r'(\w+)="((?:\\.|[^"\\])*)"', match[2] or ""))
        value = float(match[3])
        if math.isfinite(value):
            metrics[match[1]].append((labels, value))
    def scalar(name):
        values = metrics[name]
        return values[0][1] if len(values) == 1 else None
    def filtered(name, predicate):
        return [value for labels, value in metrics[name] if predicate(labels)]
    def quantile(values):
        return sorted(values)[math.floor((len(values) - 1) * 0.95)] * 1000 if values else None
    def total(values):
        return sum(values) if values else None
    current = lambda labels: labels.get("namespace") == namespace
    return [scalar("process_resident_memory_bytes") / 2**20 if scalar("process_resident_memory_bytes") is not None else None,
            scalar("go_goroutines"), scalar("go_memstats_heap_alloc_bytes") / 2**20 if scalar("go_memstats_heap_alloc_bytes") is not None else None,
            total(filtered("controller_runtime_reconcile_total", lambda labels: labels.get("controller") == "scaledobject" and labels.get("result") == "success")),
            total(filtered("controller_runtime_reconcile_total", lambda labels: labels.get("controller") == "scaledobject" and labels.get("result") == "error")),
            total(filtered("workqueue_depth", lambda labels: labels.get("name") == "scaledobject")),
            len(metrics["keda_internal_scale_loop_latency_seconds"]),
            quantile(filtered("keda_internal_scale_loop_latency_seconds", current)),
            quantile(filtered("keda_scaler_metrics_latency_seconds", current)),
            total(filtered("keda_scaler_detail_errors_total", current))]


def add_observations(panels, root, versions, scalers, assertions, density, repeat, panel_id, y, baseline, comparison):
    if not any(key[2:] == (density, repeat) for key in assertions):
        raise ValueError(f"No recorded timeline cell for objects-{density}/{repeat}")
    hashes = {}
    snapshots, origins = [], []
    colors = {version: COLORS[i % len(COLORS)] for i, version in enumerate(versions)}
    for index, scaler in enumerate(scalers):
        progress = line_panel(panel_id, f"{scaler} — observed creation progress ({density}, {repeat})",
            (index % 2 * 12, y + index // 2 * 10, 12, 10), ["Elapsed seconds since first ScaledObject"], [],
            "Per-object Kubernetes creation timestamps, aligned to each run's first ScaledObject. Solid: HPAs; dashed: ScaledObjects. These are creation counts, not replica counts.", "none", {})
        progress["targets"] = []
        for version in versions:
            key = version, scaler, density, repeat
            if key not in assertions:
                continue
            cell = root / version / scaler / f"objects-{density}" / repeat
            latency_path = cell / "control-plane-hpa-latency.json"
            start, points = creation_progress(latency_path)
            hashes[str(latency_path.relative_to(root))] = hashlib.sha256(latency_path.read_bytes()).hexdigest()
            origins.append([version, scaler, density, repeat, start.isoformat()])
            headers = ["Elapsed seconds since first ScaledObject", f"{version} ScaledObjects", f"{version} HPAs"]
            frame = line_panel(panel_id, "", (0, 0, 1, 1), headers, points, "", "none", {h: colors[version] for h in headers[1:]})
            frame["targets"][0]["refId"] = chr(65 + len(progress["targets"]))
            progress["targets"].extend(frame["targets"])
            progress["fieldConfig"]["overrides"].extend(frame["fieldConfig"]["overrides"][1:])
        panels.append(progress); panel_id += 1
    y += ((len(scalers) + 1) // 2) * 10
    for scaler in scalers:
        resource_panels = [line_panel(panel_id + i, f"{scaler} — recorded KEDA pod {label} ({density}, {repeat})",
            (i * 12, y, 12, 10), ["Elapsed seconds since first ScaledObject"], [],
            "kubectl top container observations. Nominal 5-second collection plus command time; Metrics Server averaging window applies. Negative elapsed values are pre-creation observations. Solid: operator; dashed: metrics API; dotted: webhooks. Lines connect successive recorded samples; missing measurements are not imputed.", unit, {})
            for i, (label, unit) in enumerate((("CPU", "suffix: mCPU"), ("memory", "suffix: MiB")))]
        for result in resource_panels:
            result["targets"] = []
        for version in dict.fromkeys((baseline, comparison)):
            key = version, scaler, density, repeat
            if key not in assertions:
                continue
            cell = root / version / scaler / f"objects-{density}" / repeat
            start, _ = creation_progress(cell / "control-plane-hpa-latency.json")
            path = cell / "keda-resource-usage.txt"
            samples = resource_samples(path, start)
            hashes[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
            for i, result in enumerate(resource_panels):
                for component in ("operator", "metrics API", "webhooks"):
                    name = f"{version} {component}"
                    points = [[elapsed, observed[component][i]] for elapsed, observed in sorted(samples.items()) if component in observed]
                    frame = line_panel(panel_id, "", (0, 0, 1, 1), ["Elapsed seconds since first ScaledObject", name], points, "", "none", {name: colors[version]})
                    frame["targets"][0]["refId"] = chr(65 + len(result["targets"]))
                    result["targets"].extend(frame["targets"])
                    result["fieldConfig"]["overrides"].extend(frame["fieldConfig"]["overrides"][1:])
                    result["fieldConfig"]["overrides"][0]["properties"][1]["value"] = min(
                        result["fieldConfig"]["overrides"][0]["properties"][1]["value"], min(point[0] for point in points))
            for component, filename in (("operator", "keda-operator-metrics.prom"), ("metrics API", "keda-metrics-apiserver-metrics.prom")):
                path = cell / filename
                snapshots.append([version, scaler, density, repeat, component] + prometheus_snapshot(path, assertions[key]["namespace"]))
                hashes[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
        panels.extend(resource_panels); panel_id += 2; y += 10
    panels.append(table_panel(panel_id, "Native KEDA operator / metrics API metrics — end-of-cell snapshots",
        ["Version", "Scaler", "Objects", "Run", "Component", "Process RSS MiB", "Goroutines", "Go heap MiB",
         "SO reconciles success (process lifetime)", "SO reconciles error (process lifetime)", "SO queue depth",
         "Exposed scale-loop series (all namespaces)", "Current namespace scale-loop P95 ms",
         "Current namespace scaler query P95 ms", "Current namespace scaler errors"], snapshots, (0, y, 24, 10),
        "Raw native Prometheus scrapes at cell end. Counters include earlier cells in the same operator process. Gauge P95s are across objects at scrape time, not temporal percentiles. Missing metrics are blank. Scrape wall-clock timestamps were not saved."))
    panel_id += 1; y += 10
    panels.append(table_panel(panel_id, "Timeline origins — original UTC timestamps", ["Version", "Scaler", "Objects", "Run", "First ScaledObject UTC"], origins, (0, y, 24, 7)))
    return panel_id + 1, y + 7, hashes


def build_dashboard(root, baseline, comparison, timeline_objects=None, timeline_run="run-01"):
    rows, groups, assertions, config, time_bounds, server_versions = load_validated_results(root)
    versions = sorted({row["version"] for row in rows}, key=version_key)
    scalers = [name for name in config["scalers"] if any(row["scaler"] == name for row in rows)]
    densities = sorted({int(row["objects"]) for row in rows})
    mean = {key: statistics.mean(float(row["p95_ms"]) for row in items) / 1000 for key, items in groups.items()}
    contrasts = []
    for scaler in scalers:
        for density in densities:
            a, b = mean.get((baseline, scaler, density)), mean.get((comparison, scaler, density))
            if a is None or b is None:
                continue
            contrasts.append([scaler, density, a, b, b - a, (b / a - 1) * 100])
    if not contrasts:
        raise ValueError("No comparable version/scaler/density pairs found")
    top_density = max(row[1] for row in contrasts)
    median_change = statistics.median(row[-1] for row in contrasts if row[1] == top_density)
    passed = sum(bool(row["passed"]) for row in assertions.values())
    repeat_counts = {len(items) for items in groups.values()}
    repeat_label = str(next(iter(repeat_counts))) if len(repeat_counts) == 1 else "varies"
    title = "KEDA / archived kube-burner HPA timing"
    subtitle = (f"**Dataset:** `{root.name}` · **Mode:** control-plane, inactive triggers · "
                f"**Creation:** {config['qps']} QPS / burst {config['burst']} · "
                f"**Polling:** {config['pollingInterval']} s · **Repeats per cell:** {repeat_label}\n\n"
                "Recorded benchmark data loaded through TestData **CSV Content**. "
                "HPA creation time is measured from each ScaledObject's creation timestamp.")
    interpretation = (f"Version order: {config.get('versionOrder', 'not recorded')}. "
                      f"Recorded Kubernetes server: {', '.join(server_versions) or 'not recorded'}. ")
    if any(version.startswith("v1.36.") for version in server_versions) and any(version.startswith("v2.20.") for version in versions):
        interpretation += "Kubernetes 1.36 exceeds KEDA 2.20's documented tested range (1.33–1.35). "
    interpretation += "Observed timing comparison; attribution to KEDA still requires replication."
    panels = [text_panel(1, "Recorded results and workload", subtitle, (0, 0, 24, 4)),
              statistic(2, "Supported cells passed", passed, f"suffix: / {len(assertions)}", (0, 4, 5, 4)),
              statistic(3, f"Median P95 increase at {top_density} objects", median_change, "percent", (5, 4, 7, 4),
                        f"Median of scaler-specific percentage changes, {baseline} to {comparison}"),
              text_panel(4, "Interpretation", interpretation, (12, 4, 12, 4))]
    panel_id = 5
    chart_rows = (len(scalers) + 1) // 2
    for index, scaler in enumerate(scalers):
        available = [version for version in versions if any((version, scaler, density) in mean for density in densities)]
        values = [[density] + [mean.get((version, scaler, density)) for version in available] for density in densities]
        result = line_panel(panel_id, f"{scaler} — HPA creation P95 versus density", (index % 2 * 12, 8 + index // 2 * 9, 12, 9),
                       ["ScaledObject count"] + available, values, "Aggregate analysis: mean of per-run P95s in seconds versus object count. This is not a time axis.",
                       "suffix: s", {version: COLORS[versions.index(version) % len(COLORS)] for version in available})
        result["fieldConfig"]["defaults"]["custom"]["show"] = "points+lines"
        panels.append(result); panel_id += 1
    y = 8 + chart_rows * 9
    panels.append(table_panel(panel_id, f"Exact comparison — {baseline} to {comparison}",
                  ["Scaler", "ScaledObjects", f"{baseline} P95 (s)", f"{comparison} P95 (s)", "Increase (s)", "Increase (%)"],
                  contrasts, (0, y, 24, 8), "Each latency is a mean of per-run P95s; percentage changes use unrounded values."))
    panel_id += 1; y += 8
    per_run = [[row["version"], row["scaler"], int(row["objects"]), row["run"], int(row["samples"]),
                float(row["p50_ms"]) / 1000, float(row["p95_ms"]) / 1000, float(row["p99_ms"]) / 1000]
               for row in sorted(rows, key=lambda row: (row["scaler"], int(row["objects"]), version_key(row["version"]), row["run"]))]
    panels.append(table_panel(panel_id, "Per-run evidence — use table column filters to inspect repeats",
                  ["Version", "Scaler", "ScaledObjects", "Run", "Samples", "P50 (s)", "P95 (s)", "P99 (s)"], per_run, (0, y, 24, 10)))
    panel_id += 1; y += 10
    panel_id, y, observation_hashes = add_observations(panels, root, versions, scalers, assertions,
        timeline_objects or top_density, timeline_run, panel_id, y, baseline, comparison)
    hashes = {name: hashlib.sha256((root / name).read_bytes()).hexdigest()
              for name in ("summary.csv", "summary-runs.csv", "matrix-config.json")}
    notes = ("**Measurement:** owned HPA creation timestamp minus ScaledObject creation timestamp, matched by owner UID. "
             "One-second timestamp resolution. Per-run P95 uses sorted values at floor((N−1)×0.95); panels show means of those P95s.\n\n"
             f"**Scope:** inactive signals, minReplicaCount=0, maxReplicaCount={config['maxReplicaCount']}. Pod scale-up/down and application throughput were not measured. "
             "The dashboard's time range does not filter these embedded archived CSV results.\n\n"
             f"**Source CSV SHA-256:** `{hashes['summary-runs.csv']}`. The generator checked every displayed per-run P50/P95/P99 "
             "against its saved per-object timestamp measurements and invariant checks. Attach the raw evidence ZIP with screenshots.")
    panels.append(text_panel(panel_id, "Method and provenance", notes, (0, y, 24, 6)))
    dashboard = {"id": None, "uid": "keda-hpa-" + hashlib.sha256(root.name.encode()).hexdigest()[:12], "title": title,
                 "description": f"Archived kube-burner results for {root.name}; validated against per-object measurements.",
                 "tags": ["keda", "kube-burner", "archived-results"], "schemaVersion": 38, "version": 1,
                 "timezone": "utc", "editable": True, "refresh": "", "time": {"from": min(time_bounds).isoformat(), "to": max(time_bounds).isoformat()},
                 "timepicker": {"hidden": True}, "templating": {"list": []}, "annotations": {"list": []}, "panels": panels,
                 "__inputs": [{"name": INPUT_NAME, "label": "Archived benchmark CSV", "type": "datasource",
                               "pluginId": DATASOURCE_TYPE, "pluginName": "TestData"}],
                 "__requires": [{"type": "grafana", "id": "grafana", "name": "Grafana", "version": "11.0.0"},
                                {"type": "datasource", "id": DATASOURCE_TYPE, "name": "TestData", "version": ""}]}
    provenance = {"dataset": root.name, "baseline": baseline, "comparison": comparison, "source_sha256": hashes,
                  "supported_cells": len(assertions), "passing_cells": passed, "validated_per_run_measurements": len(rows),
                  "kubernetes_server_versions": server_versions,
                  "aggregation": "mean of per-run P95s", "source": "recorded benchmark files, embedded CSV Content",
                  "observation_source_sha256": observation_hashes, "timeline_objects": timeline_objects or top_density,
                  "timeline_run": timeline_run, "native_metrics_capture": "end-of-cell snapshots; no scrape timestamps recorded"}
    return dashboard, provenance


def live_dashboard():
    """Separate live Prometheus dashboard; never substitutes for archived data."""
    datasource = {"type": "prometheus", "uid": "${DS_PROMETHEUS}"}
    selector = '{job=~"$keda_job"}'
    so_selector = '{job=~"$keda_job",controller="scaledobject"}'
    queries = [
        ("Reconciliation rate — ScaledObject", f'sum by (job,result) (rate(controller_runtime_reconcile_total{so_selector}[$__rate_interval]))', "ops"),
        ("Reconciliation duration P95 — ScaledObject", f'histogram_quantile(0.95, sum by (job,le) (rate(controller_runtime_reconcile_time_seconds_bucket{so_selector}[$__rate_interval])))', "s"),
        ("ScaledObject queue depth", 'sum by (job) (workqueue_depth{job=~"$keda_job",name="scaledobject"})', "none"),
        ("Queue wait P95 — ScaledObject", 'histogram_quantile(0.95, sum by (job,le) (rate(workqueue_queue_duration_seconds_bucket{job=~"$keda_job",name="scaledobject"}[$__rate_interval])))', "s"),
        ("Process CPU", f'rate(process_cpu_seconds_total{selector}[$__rate_interval])', "suffix: cores"),
        ("Process RSS / Go heap", f'process_resident_memory_bytes{selector}', "bytes"),
        ("Goroutines", f'go_goroutines{selector}', "none"),
        ("Scale-loop deviation — cross-object P95 at each scrape", f'quantile by (job) (0.95, keda_internal_scale_loop_latency_seconds{selector})', "s"),
        ("Scaler query latency — cross-object P95 at each scrape", f'quantile by (job) (0.95, keda_scaler_metrics_latency_seconds{selector})', "s"),
        ("Scaler error rate — all namespaces", f'sum by (job) (rate(keda_scaler_detail_errors_total{selector}[$__rate_interval]))', "ops"),
        ("Internal gRPC server / client duration P99", f'histogram_quantile(0.99, sum by (job,le) (rate(keda_internal_metricsservice_grpc_server_handling_seconds_bucket{selector}[$__rate_interval])))', "s"),
        ("Exposed scale-loop series — all namespaces", f'count by (job) (keda_internal_scale_loop_latency_seconds{selector})', "none"),
    ]
    panels = [text_panel(1, "Live / retained Prometheus observations", "Select the Prometheus datasource that scrapes KEDA and choose its KEDA jobs. Set the time picker to the benchmark interval. This dashboard can show an earlier run only if Prometheus scraped and retained that interval. Cross-object gauge percentiles differ from time-window histogram percentiles. Counters use rates, accounting for process resets. Use the build-info table to identify releases; scraping labels must separate concurrently running KEDA installations.", (0, 0, 24, 4))]
    for index, (title, expression, unit) in enumerate(queries):
        result = {"id": index + 2, "title": title, "type": "timeseries", "datasource": datasource,
                  "gridPos": {"x": index % 2 * 12, "y": 4 + index // 2 * 8, "w": 12, "h": 8},
                  "targets": [{"refId": "A", "datasource": datasource, "expr": expression, "range": True,
                               "legendFormat": "{{job}} {{instance}} {{result}} {{__name__}}"}],
                  "fieldConfig": {"defaults": {"unit": unit, "min": 0, "color": {"mode": "palette-classic"},
                      "custom": {"drawStyle": "line", "lineInterpolation": "linear", "lineWidth": 2,
                                 "fillOpacity": 0, "showPoints": "never", "spanNulls": False}}, "overrides": []},
                  "options": {"legend": {"showLegend": True, "displayMode": "list", "placement": "bottom"},
                              "tooltip": {"mode": "multi", "sort": "none"}}}
        panels.append(result)
        if title == "Process RSS / Go heap":
            result["targets"][0]["legendFormat"] = "RSS {{job}} {{instance}}"
            result["targets"].append({"refId": "B", "datasource": datasource,
                "expr": f'go_memstats_heap_alloc_bytes{selector}', "range": True,
                "legendFormat": "Go heap {{job}} {{instance}}"})
        if title == "Internal gRPC server / client duration P99":
            result["targets"][0]["legendFormat"] = "server {{job}}"
            result["targets"].append({"refId": "B", "datasource": datasource,
                "expr": f'histogram_quantile(0.99, sum by (job,le) (rate(keda_internal_metricsservice_grpc_client_handling_seconds_bucket{selector}[$__rate_interval])))',
                "range": True, "legendFormat": "client {{job}}"})
    panels.append({"id": 14, "title": "Recorded KEDA build labels within selected interval", "type": "table",
        "datasource": datasource, "gridPos": {"x": 0, "y": 52, "w": 24, "h": 8},
        "targets": [{"refId": "A", "datasource": datasource,
                     "expr": f'max_over_time(keda_build_info{selector}[$__range])', "instant": True, "format": "table"}],
        "options": {"showHeader": True}})
    return {"id": None, "uid": "keda-operator-observations", "title": "KEDA / operator and metrics API — Prometheus timelines",
        "schemaVersion": 38, "version": 1, "timezone": "utc", "refresh": "10s", "editable": True,
        "tags": ["keda", "kube-burner", "prometheus"], "time": {"from": "now-1h", "to": "now"},
        "panels": panels, "annotations": {"list": []},
        "__inputs": [{"name": "DS_PROMETHEUS", "label": "Prometheus scraping KEDA", "type": "datasource", "pluginId": "prometheus", "pluginName": "Prometheus"}],
        "templating": {"list": [{"name": "keda_job", "label": "KEDA scrape jobs", "type": "query", "datasource": datasource,
            "query": {"query": "label_values(keda_build_info, job)", "refId": "variable"}, "refresh": 1,
            "multi": True, "includeAll": True,
            "current": {"text": "All", "value": "$__all"}}]}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results_root", type=Path)
    parser.add_argument("--baseline", default="v2.20.1")
    parser.add_argument("--compare", default="v2.20.2")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--timeline-objects", type=int)
    parser.add_argument("--timeline-run", default="run-01")
    args = parser.parse_args()
    root = args.results_root.resolve()
    output = args.output or root / "grafana"
    dashboard, provenance = build_dashboard(root, args.baseline, args.compare, args.timeline_objects, args.timeline_run)
    output.mkdir(parents=True, exist_ok=True)
    (output / "keda-release-comparison.json").write_text(json.dumps(dashboard, indent=2) + "\n")
    (output / "provenance.json").write_text(json.dumps(provenance, indent=2) + "\n")
    (output / "keda-operator-live.json").write_text(json.dumps(live_dashboard(), indent=2) + "\n")
    provisioned = json.loads(json.dumps(dashboard).replace("${" + INPUT_NAME + "}", DATASOURCE_UID))
    provisioned.pop("__inputs", None)
    values = {"fullnameOverride": "keda-results", "persistence": {"enabled": False},
              "resources": {"requests": {"cpu": "100m", "memory": "128Mi"}, "limits": {"cpu": "1", "memory": "512Mi"}},
              "datasources": {"datasources.yaml": {"apiVersion": 1, "datasources": [
                  {"name": "KEDA benchmark CSV", "uid": DATASOURCE_UID, "type": DATASOURCE_TYPE, "access": "proxy", "editable": False}]}},
              "dashboardProviders": {"dashboardproviders.yaml": {"apiVersion": 1, "providers": [
                  {"name": "keda-results", "orgId": 1, "folder": "KEDA benchmark results", "type": "file",
                   "disableDeletion": False, "allowUiUpdates": False, "options": {"path": "/var/lib/grafana/dashboards/keda-results"}}]}},
              "dashboards": {"keda-results": {"release-comparison": {"json": json.dumps(provisioned)}}}}
    (output / "helm-values.json").write_text(json.dumps(values, indent=2) + "\n")
    print(f"Validated {provenance['validated_per_run_measurements']} per-run measurements and {provenance['supported_cells']} invariant files")
    print(f"Dashboard: {output / 'keda-release-comparison.json'}")
    print(f"Standalone Grafana Helm values: {output / 'helm-values.json'}")


if __name__ == "__main__":
    main()
