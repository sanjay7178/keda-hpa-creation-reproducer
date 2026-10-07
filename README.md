# KEDA HPA creation timing reproducer with kube-burner

Compare ScaledObject-to-HPA creation timing between **KEDA v2.20.1 and
v2.20.2** using repeatable kube-burner workloads. This repository contains
only workload code and instructions. Collected cluster evidence and benchmark
results are not published here. The scripts generate fresh results locally.

The examples cover **Metrics API, Kubernetes Resource, OpenSearch, and
Temporal**. Each ScaledObject targets a separate Deployment, with inactive
signals and zero target replicas. This measures HPA creation; it is a different
workload from the active external-metric authorization bottleneck described
in [KEDA #8258](https://github.com/kedacore/keda/issues/8258).

## Install kube-burner

Prerequisites: Linux, Bash/GNU coreutils, Git, Go **1.25+**, kubectl, Helm,
jq, and Python 3. Metrics Server is optional for CPU/memory samples. Use
Kubernetes **1.33–1.35** for replication within KEDA 2.20's
[documented tested range](https://keda.sh/docs/2.20/operate/cluster/#kubernetes-compatibility).

```bash
git clone https://github.com/sanjay7178/keda-hpa-creation-reproducer.git
cd keda-hpa-creation-reproducer
./scripts/install-kube-burner.sh
.tools/bin/kube-burner version
```

The installer builds public kube-burner revision
`69cca726f3487cd134f7ae26d5375dd4f9428807`, which includes the KEDA measurement
from [kube-burner PR #1234](https://github.com/kube-burner/kube-burner/pull/1234),
and applies `patches/benchmark-core.patch`. The patch preserves two
authorization-error handling changes used by the experiment's build; it
does not change measurement, profiling, or workload configuration. The binary
stays in `.tools/bin`, outside Git tracking. An existing compatible installation
can be selected with `KUBE_BURNER_BIN=/absolute/path/to/kube-burner`.

## Run the comparison

**Use a disposable benchmark cluster. The runner reinstalls KEDA and deletes
its CRDs between releases.** It deletes each workload namespace and leaves
the last installed KEDA release in place. Set the reset acknowledgment only
on a cluster with no unrelated KEDA resources.

```bash
export KUBECONFIG=/path/to/dedicated-cluster-kubeconfig
export ALLOW_KEDA_RESET=true

# Wiring check: five objects, Metrics API, one release, no profiling.
./run.sh smoke

# Focused pair: Metrics API, both releases, 500/1,000 objects, three repeats.
./run.sh compare

# All four scaler examples.
SCALERS="metrics-api kubernetes-resource opensearch temporal" ./run.sh compare

# Separate checks for release-order and profiling effects.
VERSION_ORDER=reverse ./run.sh compare
PROFILE_KEDA=false ./run.sh compare
```

`compare` defaults to injector QPS/burst 50/100, five-second polling,
min/max replicas 0/1, and observation pauses of 450s for 500 objects and
900s for 1,000. CPU profiles cover 20-second windows every 120s, plus
boundary CPU/heap captures, for operator, metrics API, and webhooks. They
are samples, not continuous CPU recordings. Use `./run.sh --help` for overrides.

| Scaler | Dependency | Inactive signal |
| --- | --- | --- |
| Metrics API | Three-replica HTTP mock returning valid JSON | 0 |
| Kubernetes Resource | ConfigMap per ScaledObject | Numeric value 0 |
| OpenSearch | HTTP mock returning OpenSearch-style JSON | 0; no real OpenSearch backend |
| Temporal | Real development server with distinct workflow task queues | Empty queues |

## Read the results

Each invocation prints a fresh `results/<mode>-<UTC timestamp>/` directory.
It includes:

- `runner.log`, `reproducer-config.txt`, and `kube-burner-version.txt`.
- `summary-runs.csv`, `summary.csv`, and `summary.md`.
- Per-cell assertions, ScaledObject/HPA snapshots, ownership-matched
  `control-plane-hpa-latency.json`, logs, events, and resource samples.
- End-of-cell native metrics scrapes and CPU/heap profiles when enabled.

Latency is owned-HPA creation timestamp minus ScaledObject creation timestamp.
Kubernetes timestamps have one-second resolution. Per-run P95 uses sorted
index `floor((N-1)*0.95)`; comparison values are **means of per-run P95s**,
not pooled percentiles or backend query latency. Inspect each cell's
`assertions.json` before comparing results. A completed run is not by itself
proof that a timing difference is caused by KEDA.

Generated results, kubeconfigs, logs, profiles, and tool binaries are ignored
by Git. Review any selected diagnostics separately before attaching them to
an upstream issue. A short issue-form reproduction section is available in
[docs/GITHUB_ISSUE_REPRODUCTION.md](docs/GITHUB_ISSUE_REPRODUCTION.md).

## Grafana

Generate an importable dashboard for a completed fresh comparison:

```bash
python3 examples/workloads/keda-scaler-comparison/build-grafana-dashboard.py \
  results/compare-YOUR_UTC_TIMESTAMP
```

Import `grafana/keda-release-comparison.json` into Grafana and select its
built-in TestData datasource. It shows version-colored P95-versus-density
lines, timestamp-derived creation progress, sampled pod CPU/memory, and
end-of-cell operator/metrics API snapshots. Its default timeline example is
the largest density, run-01. Use `--timeline-objects 500 --timeline-run run-02`
to inspect another recorded cell.

The separate `grafana/keda-operator-live.json` uses Prometheus that scrapes
KEDA. Continuous historical charts require Prometheus to have scraped and
retained the run interval; end-of-cell snapshots cannot reconstruct that
history.

## Validation and license

The pinned installer and shell scripts were checked locally. All four scaler
configurations were rendered with kube-burner's parser, with profiling enabled
and disabled, including validation of the HTTP mock Python scripts. These
checks do not constitute a new cluster benchmark.

The copied workload utilities retain their original multi-scaler support;
`run.sh` limits the main reproduction to the four documented examples.
See [NOTICE](NOTICE) and [LICENSE](LICENSE).
