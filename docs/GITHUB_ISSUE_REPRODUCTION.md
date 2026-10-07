## Steps to Reproduce the Problem

Install kube-burner first, then run the standalone workload repository:

```bash
git clone https://github.com/sanjay7178/keda-hpa-creation-reproducer.git
cd keda-hpa-creation-reproducer
./scripts/install-kube-burner.sh

export KUBECONFIG=/path/to/dedicated-cluster-kubeconfig
export ALLOW_KEDA_RESET=true
./run.sh smoke
./run.sh compare
```

The installer builds a pinned public kube-burner revision plus the supplied
historical core patch. Go 1.25+, Git, Bash/GNU tools, kubectl, Helm, jq, and
Python 3 are required. Use a disposable Kubernetes 1.33–1.35 cluster: the
runner reinstalls KEDA and deletes its CRDs between releases.

`compare` runs Metrics API on KEDA v2.20.1/v2.20.2 at 500 and 1,000 objects,
three repeats, with profiling. To use all four scaler examples:

```bash
SCALERS="metrics-api kubernetes-resource opensearch temporal" ./run.sh compare
```

Compare `summary-runs.csv` and `summary.csv` in the printed results directory,
and check each cell's object/HPA invariants and per-object timestamps. Repeat
with `VERSION_ORDER=reverse`, and separately with `PROFILE_KEDA=false`, to
investigate release-order and profiling effects. The reported statistic is
mean-of-per-run HPA creation P95; this workload does not reproduce #8258's
active external-metric authorization bottleneck.
