#!/usr/bin/env bash
set -euo pipefail

reproducer_root=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
mode=${1:-compare}
if [[ ${mode} == --help || ${mode} == -h ]]; then
  printf '%s\n' 'Usage: ./run.sh [smoke|compare]' \
    'Requires a disposable cluster and ALLOW_KEDA_RESET=true.' \
    'Overrides: KUBECONFIG, KUBE_CONTEXT, KUBE_BURNER_BIN, RESULTS_ROOT,' \
    'VERSIONS, SCALERS, DENSITIES, REPEATS, PROFILE_KEDA, VERSION_ORDER,' \
    'QPS, BURST, POLLING_INTERVAL, SETTLE_DURATION, SETTLE_DURATIONS.'
  exit 0
fi
case "${mode}" in
  smoke)
    export VERSIONS=${VERSIONS:-v2.20.2}
    export DENSITIES=${DENSITIES:-5}
    export REPEATS=${REPEATS:-1}
    export PROFILE_KEDA=${PROFILE_KEDA:-false}
    export SETTLE_DURATION=${SETTLE_DURATION:-60s}
    ;;
  compare)
    export VERSIONS=${VERSIONS:-"v2.20.1 v2.20.2"}
    export DENSITIES=${DENSITIES:-"500 1000"}
    export REPEATS=${REPEATS:-3}
    export PROFILE_KEDA=${PROFILE_KEDA:-true}
    # Density-specific pauses are defaults only when using default densities.
    if [[ ${DENSITIES} == "500 1000" ]]; then
      export SETTLE_DURATIONS=${SETTLE_DURATIONS:-"500:450s 1000:900s"}
    fi
    ;;
  *) printf 'error: mode must be smoke or compare\n' >&2; exit 1 ;;
esac
export SCALERS=${SCALERS:-metrics-api}
for scaler in ${SCALERS}; do
  case "${scaler}" in
    metrics-api|kubernetes-resource|opensearch|temporal) ;;
    *) printf 'error: unsupported reproducer scaler: %s\n' "${scaler}" >&2; exit 1 ;;
  esac
done
export MATRIX_PROFILE=new WORKLOAD_MODE=control-plane
export QPS=${QPS:-50} BURST=${BURST:-100} POLLING_INTERVAL=${POLLING_INTERVAL:-5}
export PRE_LOAD_IMAGES=${PRE_LOAD_IMAGES:-false}
export PPROF_INTERVAL=${PPROF_INTERVAL:-120s} PPROF_CPU_SECONDS=${PPROF_CPU_SECONDS:-20}
export KUBE_BURNER_TIMEOUT=${KUBE_BURNER_TIMEOUT:-30m}
export RESULTS_ROOT=${RESULTS_ROOT:-"${reproducer_root}/results/${mode}-$(date -u +%Y%m%dT%H%M%SZ)"}
if [[ -z ${KUBE_BURNER_BIN:-} ]]; then
  if [[ -x ${reproducer_root}/.tools/bin/kube-burner ]]; then
    export KUBE_BURNER_BIN="${reproducer_root}/.tools/bin/kube-burner"
  else
    export KUBE_BURNER_BIN
    KUBE_BURNER_BIN=$(command -v kube-burner || true)
  fi
fi
[[ -x ${KUBE_BURNER_BIN:-} ]] || {
  printf 'error: install kube-burner with ./scripts/install-kube-burner.sh\n' >&2
  exit 1
}
if [[ ${ALLOW_KEDA_RESET:-false} != true ]]; then
  printf 'error: use a disposable cluster and explicitly export ALLOW_KEDA_RESET=true\n' >&2
  exit 1
fi
mkdir -p "${RESULTS_ROOT}"
RESULTS_ROOT=$(cd "${RESULTS_ROOT}" && pwd)
export RESULTS_ROOT
"${KUBE_BURNER_BIN}" version >"${RESULTS_ROOT}/kube-burner-version.txt"
printf '%s\n' "mode=${mode}" "versions=${VERSIONS}" "scalers=${SCALERS}" \
  "densities=${DENSITIES}" "repeats=${REPEATS}" "profiling=${PROFILE_KEDA}" \
  >"${RESULTS_ROOT}/reproducer-config.txt"
printf 'Results: %s\n' "${RESULTS_ROOT}"
exec "${reproducer_root}/examples/workloads/keda-scaler-comparison/run-matrix.sh"
