```bash
git clone https://github.com/sanjay7178/keda-hpa-creation-reproducer.git
cd keda-hpa-creation-reproducer || exit 1

# Use the binary from https://github.com/kube-burner/kube-burner/releases
KUBE_BURNER_VERSION=2.8.6
KUBE_BURNER_ARCH=$(uname -m)
KUBE_BURNER_ARCH=${KUBE_BURNER_ARCH/aarch64/arm64}
ARCHIVE="kube-burner-V${KUBE_BURNER_VERSION}-linux-${KUBE_BURNER_ARCH}.tar.gz"
RELEASE_URL="https://github.com/kube-burner/kube-burner/releases/download/v${KUBE_BURNER_VERSION}"

mkdir -p .tools/downloads .tools/bin
curl -fL "${RELEASE_URL}/${ARCHIVE}" -o ".tools/downloads/${ARCHIVE}"
curl -fL "${RELEASE_URL}/kube-burner-checksums.txt" -o .tools/downloads/kube-burner-checksums.txt
(cd .tools/downloads && sha256sum --check --ignore-missing kube-burner-checksums.txt)
tar -xzf ".tools/downloads/${ARCHIVE}" -C .tools/bin kube-burner

export KUBE_BURNER_BIN="$PWD/.tools/bin/kube-burner"
"$KUBE_BURNER_BIN" version
```

```bash
# Disposable cluster only: the runner reinstalls KEDA and deletes its CRDs.
export KUBECONFIG=/path/to/dedicated-cluster-kubeconfig
export ALLOW_KEDA_RESET=true
./run.sh smoke
```

```bash
# Metrics API: v2.20.1/v2.20.2, 500/1,000 objects, three repeats.
./run.sh compare
```

```bash
# All four scalers.
SCALERS="metrics-api kubernetes-resource opensearch temporal" ./run.sh compare
```

```bash
# Reverse release order.
VERSION_ORDER=reverse ./run.sh compare
```

```bash
# Without profiling.
PROFILE_KEDA=false ./run.sh compare
```

```bash
python3 examples/workloads/keda-scaler-comparison/build-grafana-dashboard.py \
  results/compare-YOUR_UTC_TIMESTAMP
```
