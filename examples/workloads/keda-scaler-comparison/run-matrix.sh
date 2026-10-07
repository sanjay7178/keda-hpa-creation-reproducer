#!/usr/bin/env bash
set -euo pipefail

workload_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
repo_root=$(cd "${workload_dir}/../../.." && pwd)
machine_arch=$(uname -m)
machine_arch=${machine_arch/x86_64/amd64}
machine_arch=${machine_arch/aarch64/arm64}
default_kube_burner_bin="${repo_root}/bin/${machine_arch}/kube-burner"
if [[ ! -x ${default_kube_burner_bin} && -x ${repo_root}/bin/kube-burner ]]; then
  default_kube_burner_bin="${repo_root}/bin/kube-burner"
fi

matrix_profile=${MATRIX_PROFILE:-core}
profile_workload_mode=lifecycle
case "${matrix_profile}" in
  core)
    profile_versions="v2.18.3 v2.19.0 v2.20.2"
    profile_scalers="cron prometheus metrics-api kafka"
    ;;
  new)
    profile_versions="v2.19.0 v2.20.0 v2.20.1 v2.20.2"
    profile_scalers="kubernetes-resource opensearch"
    ;;
  full)
    profile_versions="v2.18.3 v2.19.0 v2.20.0 v2.20.1 v2.20.2"
    profile_scalers="cron prometheus metrics-api kafka kubernetes-resource opensearch"
    ;;
  community)
    profile_versions="v2.18.3 v2.19.0 v2.20.0 v2.20.1 v2.20.2"
    profile_scalers="temporal"
    profile_workload_mode=control-plane
    ;;
  *)
    printf 'error: MATRIX_PROFILE must be core, new, full, or community\n' >&2
    exit 1
    ;;
esac

versions=${VERSIONS:-${profile_versions}}
scalers=${SCALERS:-${profile_scalers}}
densities=${DENSITIES:-${OBJECTS:-25}}
repeats=${REPEATS:-1}
qps=${QPS:-20}
burst=${BURST:-30}
settle_duration=${SETTLE_DURATION:-90s}
settle_durations=${SETTLE_DURATIONS:-}
kube_burner_timeout=${KUBE_BURNER_TIMEOUT:-4h}
namespace_delete_timeout=${NAMESPACE_DELETE_TIMEOUT:-15m}
polling_interval=${POLLING_INTERVAL:-5}
cooldown_period=${COOLDOWN_PERIOD:-10}
max_replica_count=${MAX_REPLICA_COUNT:-1}
threshold=${THRESHOLD:-100}
workload_mode=${WORKLOAD_MODE:-${profile_workload_mode}}
mock_replicas=${MOCK_REPLICAS:-3}
version_order=${VERSION_ORDER:-forward}
resource_sample_interval=${RESOURCE_SAMPLE_INTERVAL:-5}
profile_keda=${PROFILE_KEDA:-false}
pprof_interval=${PPROF_INTERVAL:-60s}
pprof_cpu_seconds=${PPROF_CPU_SECONDS:-20}
pre_load_images=${PRE_LOAD_IMAGES:-true}
create_kind_cluster=${CREATE_KIND_CLUSTER:-false}
kind_cluster_name=${KIND_CLUSTER_NAME:-keda-kube-burner}
kind_node_image=${KIND_NODE_IMAGE:-kindest/node:v1.35.0}
keep_cluster=${KEEP_CLUSTER:-false}
kube_context=${KUBE_CONTEXT:-}
allow_keda_reset=${ALLOW_KEDA_RESET:-false}
kube_burner_bin=${KUBE_BURNER_BIN:-${default_kube_burner_bin}}
results_root=${RESULTS_ROOT:-${repo_root}/keda-scaler-comparison-results/$(date -u +%Y%m%dT%H%M%SZ)}
created_cluster=false
failures=0
skips=0
active_namespace=""
active_sampler_pid=""
active_dependency_sampler_pid=""
declare -A settle_by_density=()

usage() {
  sed -n '1,260p' "${workload_dir}/README.md"
}

die() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"
}

version_at_least() {
  local actual=${1#v}
  local required=${2#v}
  [[ $(printf '%s\n%s\n' "${required}" "${actual}" | sort -V | head -n1) == "${required}" ]]
}

scaler_supported() {
  local version=$1
  local scaler=$2
  case "${scaler}" in
    kubernetes-resource) version_at_least "${version}" v2.19.0 ;;
    opensearch) version_at_least "${version}" v2.20.0 ;;
    temporal) version_at_least "${version}" v2.17.0 ;;
    *) return 0 ;;
  esac
}

cleanup() {
  if [[ -n ${active_sampler_pid} ]]; then
    kill "${active_sampler_pid}" >/dev/null 2>&1 || true
    wait "${active_sampler_pid}" 2>/dev/null || true
  fi
  if [[ -n ${active_dependency_sampler_pid} ]]; then
    kill "${active_dependency_sampler_pid}" >/dev/null 2>&1 || true
    wait "${active_dependency_sampler_pid}" 2>/dev/null || true
  fi
  if [[ -n ${active_namespace} && -n ${kube_context} ]] && command -v kubectl >/dev/null 2>&1; then
    kubectl --context "${kube_context}" delete namespace "${active_namespace}" \
      --ignore-not-found --wait=false >/dev/null 2>&1 || true
  fi
  if [[ ${created_cluster} == true && ${keep_cluster} != true ]]; then
    kind delete cluster --name "${kind_cluster_name}" >/dev/null
  fi
}
trap cleanup EXIT

if [[ ${1:-} == "--help" || ${1:-} == "-h" ]]; then
  usage
  exit 0
fi

for command_name in kubectl helm jq date sort; do
  require_command "${command_name}"
done
if [[ ${create_kind_cluster} == true ]]; then
  require_command kind
fi
[[ -x ${kube_burner_bin} ]] || die "kube-burner binary is not executable: ${kube_burner_bin}"
[[ ${repeats} =~ ^[1-9][0-9]*$ ]] || die "REPEATS must be a positive integer"
[[ ${mock_replicas} =~ ^[1-9][0-9]*$ ]] || die "MOCK_REPLICAS must be a positive integer"
[[ ${pre_load_images} == true || ${pre_load_images} == false ]] ||
  die "PRE_LOAD_IMAGES must be true or false"
[[ ${profile_keda} == true || ${profile_keda} == false ]] ||
  die "PROFILE_KEDA must be true or false"
[[ ${pprof_cpu_seconds} =~ ^[1-9][0-9]*$ ]] ||
  die "PPROF_CPU_SECONDS must be a positive integer"
[[ ${pprof_interval} =~ ^[1-9][0-9]*s$ ]] ||
  die "PPROF_INTERVAL must be a positive number of seconds (for example 60s)"
[[ ${settle_duration} =~ ^([1-9][0-9]*(h|m|s))+$ ]] ||
  die "SETTLE_DURATION must be a positive duration (for example 900s or 15m)"
[[ ${kube_burner_timeout} =~ ^([1-9][0-9]*(h|m|s))+$ ]] ||
  die "KUBE_BURNER_TIMEOUT must be a positive duration (for example 30m)"
[[ ${namespace_delete_timeout} =~ ^([1-9][0-9]*(h|m|s))+$ ]] ||
  die "NAMESPACE_DELETE_TIMEOUT must be a positive duration (for example 15m)"
[[ ${workload_mode} == lifecycle || ${workload_mode} == control-plane ]] ||
  die "WORKLOAD_MODE must be lifecycle or control-plane"
if [[ ${workload_mode} == lifecycle && " ${scalers} " == *" temporal "* ]]; then
  die "Temporal currently supports control-plane mode only (no workflow backlog is generated)"
fi
for density in ${densities}; do
  [[ ${density} =~ ^[1-9][0-9]*$ ]] || die "DENSITIES must contain positive integers"
done
for setting in ${settle_durations}; do
  if [[ ${setting} =~ ^([1-9][0-9]*):(([1-9][0-9]*(h|m|s))+)$ ]]; then
    density=${BASH_REMATCH[1]}
    [[ " ${densities} " == *" ${density} "* ]] ||
      die "SETTLE_DURATIONS includes density ${density}, which is not in DENSITIES"
    settle_by_density[${density}]=${BASH_REMATCH[2]}
  else
    die "SETTLE_DURATIONS must contain density:duration pairs (for example 500:450s 1000:900s)"
  fi
done

case "${version_order}" in
  forward) ;;
  reverse) versions=$(tr ' ' '\n' <<<"${versions}" | sort -Vr | xargs) ;;
  *) die "VERSION_ORDER must be forward or reverse" ;;
esac

if [[ ${create_kind_cluster} == true ]]; then
  if ! kind get clusters 2>/dev/null | grep -Fxq "${kind_cluster_name}"; then
    kind create cluster --name "${kind_cluster_name}" --image "${kind_node_image}"
    created_cluster=true
  fi
  kube_context="kind-${kind_cluster_name}"
fi

if [[ -z ${kube_context} ]]; then
  kube_context=$(kubectl config current-context)
fi
kubectl --context "${kube_context}" get nodes >/dev/null

if [[ ${kube_context} != "kind-${kind_cluster_name}" && ${allow_keda_reset} != true ]]; then
  die "the matrix reinstalls KEDA and its CRDs; set ALLOW_KEDA_RESET=true only for a disposable benchmark cluster (${kube_context})"
fi

mkdir -p "${results_root}"
results_root=$(cd "${results_root}" && pwd)
exec > >(tee -a "${results_root}/runner.log") 2>&1
jq -n \
  --arg profile "${matrix_profile}" \
  --arg versions "${versions}" \
  --arg scalers "${scalers}" \
  --arg densities "${densities}" \
  --arg mode "${workload_mode}" \
  --arg versionOrder "${version_order}" \
  --arg context "${kube_context}" \
  --arg settleDuration "${settle_duration}" \
  --arg settleDurations "${settle_durations}" \
  --arg kubeBurnerTimeout "${kube_burner_timeout}" \
  --arg namespaceDeleteTimeout "${namespace_delete_timeout}" \
  --argjson repeats "${repeats}" \
  --argjson qps "${qps}" \
  --argjson burst "${burst}" \
  --argjson pollingInterval "${polling_interval}" \
  --argjson cooldownPeriod "${cooldown_period}" \
  --argjson maxReplicaCount "${max_replica_count}" \
  --argjson threshold "${threshold}" \
  --argjson mockReplicas "${mock_replicas}" \
  --argjson resourceSampleInterval "${resource_sample_interval}" \
  --argjson profileKeda "${profile_keda}" \
  --arg pprofInterval "${pprof_interval}" \
  --argjson pprofCpuSeconds "${pprof_cpu_seconds}" \
  --argjson preLoadImages "${pre_load_images}" \
  '{profile:$profile, versions:($versions / " "), scalers:($scalers / " "), densities:($densities / " "), mode:$mode, repeats:$repeats, qps:$qps, burst:$burst, pollingInterval:$pollingInterval, cooldownPeriod:$cooldownPeriod, maxReplicaCount:$maxReplicaCount, threshold:$threshold, mockReplicas:$mockReplicas, settleDuration:$settleDuration, settleDurations:$settleDurations, kubeBurnerTimeout:$kubeBurnerTimeout, namespaceDeleteTimeout:$namespaceDeleteTimeout, resourceSampleInterval:$resourceSampleInterval, profileKeda:$profileKeda, pprofInterval:$pprofInterval, pprofCpuSeconds:$pprofCpuSeconds, preLoadImages:$preLoadImages, versionOrder:$versionOrder, kubeContext:$context}' \
  >"${results_root}/matrix-config.json"
kubectl --context "${kube_context}" get nodes -o wide >"${results_root}/nodes-before.txt"
kubectl --context "${kube_context}" get pods -A -o wide >"${results_root}/pods-before.txt"
kubectl --context "${kube_context}" top nodes >"${results_root}/node-usage-before.txt" 2>&1 || true
helm repo add kedacore https://kedacore.github.io/charts --force-update >/dev/null
helm repo update kedacore >/dev/null

reset_keda() {
  helm --kube-context "${kube_context}" uninstall keda --namespace keda --wait >/dev/null 2>&1 || true
  kubectl --context "${kube_context}" delete namespace keda --wait=true \
    --timeout="${namespace_delete_timeout}" --ignore-not-found >/dev/null
  kubectl --context "${kube_context}" delete crd \
    scaledobjects.keda.sh \
    scaledjobs.keda.sh \
    triggerauthentications.keda.sh \
    clustertriggerauthentications.keda.sh \
    cloudeventsources.eventing.keda.sh \
    --ignore-not-found --wait=true --timeout="${namespace_delete_timeout}" >/dev/null
}

install_keda() {
  local version=$1
  local chart_version=${version#v}
  local install_dir="${results_root}/${version}/installation"
  mkdir -p "${install_dir}"

  reset_keda
  helm --kube-context "${kube_context}" upgrade --install keda kedacore/keda \
    --namespace keda \
    --create-namespace \
    --version "${chart_version}" \
    --set prometheus.operator.enabled=true \
    --set prometheus.metricServer.enabled=true \
    --set "profiling.operator.enabled=${profile_keda}" \
    --set "profiling.metricsServer.enabled=${profile_keda}" \
    --set "profiling.webhooks.enabled=${profile_keda}" \
    --wait \
    --timeout 5m 2>&1 | tee "${install_dir}/helm-install.log"
  kubectl --context "${kube_context}" -n keda rollout status deployment/keda-operator --timeout=180s
  if [[ ${profile_keda} == true ]]; then
    kubectl --context "${kube_context}" -n keda apply \
      -f "${workload_dir}/templates/pprof-client.yml" >"${install_dir}/pprof-client-apply.log"
    kubectl --context "${kube_context}" -n keda rollout status \
      deployment/kube-burner-pprof-client --timeout=180s
  fi
  kubectl --context "${kube_context}" -n keda get all -o wide >"${install_dir}/resources.txt"
  helm --kube-context "${kube_context}" get manifest keda --namespace keda >"${install_dir}/manifest.yml"
  helm --kube-context "${kube_context}" get values keda --namespace keda --all >"${install_dir}/values.yml"
  kubectl --context "${kube_context}" version -o yaml >"${install_dir}/kubernetes-version.yml"
}

sample_keda_resources() {
  local output=$1
  while true; do
    date -u +%Y-%m-%dT%H:%M:%SZ
    kubectl --context "${kube_context}" -n keda top pods --containers 2>&1 || true
    sleep "${resource_sample_interval}"
  done >>"${output}" 2>&1
}

sample_dependency_resources() {
  local namespace=$1
  local output=$2
  while true; do
    date -u +%Y-%m-%dT%H:%M:%SZ
    kubectl --context "${kube_context}" -n "${namespace}" top pods --containers 2>&1 || true
    sleep "${resource_sample_interval}"
  done >>"${output}" 2>&1
}

capture_keda_metrics() {
  local service=$1
  local output=$2
  local port
  port=$(kubectl --context "${kube_context}" -n keda get service "${service}" -o json 2>/dev/null |
    jq -r '.spec.ports[] | select(.name == "metrics") | .port' | head -n1)
  if [[ -n ${port} ]]; then
    kubectl --context "${kube_context}" get --raw \
      "/api/v1/namespaces/keda/services/http:${service}:${port}/proxy/metrics" >"${output}" 2>&1 || true
  fi
}

run_scaler() {
  local version=$1
  local scaler=$2
  local objects=$3
  local repeat=$4
  local repeat_label
  local safe_version=${version//./-}
  repeat_label=$(printf 'run-%02d' "${repeat}")
  local namespace="keda-compare-${safe_version#v}-${scaler}-n${objects}-r${repeat}"
  namespace=${namespace:0:63}
  local run_dir="${results_root}/${version}/${scaler}/objects-${objects}/${repeat_label}"
  local metrics_dir="${run_dir}/metrics"
  local user_data="${run_dir}/user-data.yml"
  local metadata="${run_dir}/metadata.yml"
  local uuid
  local cron_start
  local cron_end
  local signal_value
  local started_at
  local rc
  local metric_file
  local actual=0
  local hpa_complete=0
  local lifecycle_complete=0
  local live_scaledobjects=0
  local live_hpas=0
  local passed=false
  local sampler_pid
  local dependency_sampler_pid=""
  local cell_settle_duration=${settle_by_density[${objects}]:-${settle_duration}}

  if [[ ${workload_mode} == lifecycle ]]; then
    signal_value=${SIGNAL_VALUE:-${threshold}}
    cron_start=$(date -u -d '1 minute ago' '+%M %H * * *')
    cron_end=$(date -u -d '30 minutes' '+%M %H * * *')
  else
    signal_value=${SIGNAL_VALUE:-0}
    cron_start=$(date -u -d '6 hours' '+%M %H * * *')
    cron_end=$(date -u -d '7 hours' '+%M %H * * *')
  fi
  uuid=$(cat /proc/sys/kernel/random/uuid)
  started_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  mkdir -p "${metrics_dir}"

  printf '%s\n' \
    "scaler: ${scaler}" \
    "objects: ${objects}" \
    "qps: ${qps}" \
    "burst: ${burst}" \
    "settleDuration: ${cell_settle_duration}" \
    "pollingInterval: ${polling_interval}" \
    "cooldownPeriod: ${cooldown_period}" \
    "maxReplicaCount: ${max_replica_count}" \
    "threshold: ${threshold}" \
    "signalValue: ${signal_value}" \
    "mockReplicas: ${mock_replicas}" \
    "preLoadImages: ${pre_load_images}" \
    "profileKeda: ${profile_keda}" \
    "pprofInterval: ${pprof_interval}" \
    "pprofCpuSeconds: ${pprof_cpu_seconds}" \
    "pprofDirectory: ${run_dir}/pprof" \
    "workloadMode: ${workload_mode}" \
    "namespace: ${namespace}" \
    "resultsDir: ${metrics_dir}" \
    "cronStart: '${cron_start}'" \
    "cronEnd: '${cron_end}'" >"${user_data}"

  printf '%s\n' \
    "kedaVersion: ${version}" \
    "scaler: ${scaler}" \
    "repeat: ${repeat}" \
    "objectCount: ${objects}" \
    "workloadMode: ${workload_mode}" \
    "matrixProfile: ${matrix_profile}" \
    "creationQPS: ${qps}" \
    "creationBurst: ${burst}" \
    "settleDuration: ${cell_settle_duration}" \
    "kubeBurnerTimeout: ${kube_burner_timeout}" \
    "pollingInterval: ${polling_interval}" \
    "preLoadImages: ${pre_load_images}" \
    "profileKeda: ${profile_keda}" \
    "pprofInterval: ${pprof_interval}" \
    "pprofCpuSeconds: ${pprof_cpu_seconds}" \
    "kubeContext: ${kube_context}" \
    "startedAt: ${started_at}" >"${metadata}"

  printf '\nRunning KEDA %s / %s / %s / repeat %s / %s objects\n' \
    "${version}" "${scaler}" "${workload_mode}" "${repeat}" "${objects}"
  active_namespace=${namespace}
  sample_keda_resources "${run_dir}/keda-resource-usage.txt" &
  sampler_pid=$!
  active_sampler_pid=${sampler_pid}
  if [[ ${scaler} == temporal ]]; then
    sample_dependency_resources "${namespace}" "${run_dir}/dependency-resource-usage.txt" &
    dependency_sampler_pid=$!
    active_dependency_sampler_pid=${dependency_sampler_pid}
  fi
  set +e
  (
    cd "${repo_root}" || exit 1
    "${kube_burner_bin}" init \
      --config examples/workloads/keda-scaler-comparison/kube-burner.yml \
      --user-data "${user_data}" \
      --user-metadata "${metadata}" \
      --uuid "${uuid}" \
      --kube-context "${kube_context}" \
      --timeout "${kube_burner_timeout}" \
      --log-level info \
      --skip-log-file
  ) 2>&1 | tee "${run_dir}/kube-burner.log"
  rc=${PIPESTATUS[0]}
  set -e
  if [[ ${profile_keda} == true ]]; then
    local target phase profile_found
    local profile_file
    for target in keda-operator-cpu keda-operator-heap \
      keda-metrics-apiserver-cpu keda-metrics-apiserver-heap \
      keda-webhooks-cpu keda-webhooks-heap; do
      for phase in start end; do
        profile_found=false
        for profile_file in "${run_dir}"/pprof/"${target}"-*"-${phase}.pprof"; do
          if [[ -s ${profile_file} ]]; then
            profile_found=true
            break
          fi
        done
        if [[ ${profile_found} == false ]]; then
          printf 'error: missing %s %s pprof for %s/%s/%s\n' \
            "${target}" "${phase}" "${version}" "${scaler}" "${repeat_label}" >&2
          rc=1
        fi
      done
    done
  fi
  kill "${sampler_pid}" >/dev/null 2>&1 || true
  wait "${sampler_pid}" 2>/dev/null || true
  active_sampler_pid=""
  if [[ -n ${dependency_sampler_pid} ]]; then
    kill "${dependency_sampler_pid}" >/dev/null 2>&1 || true
    wait "${dependency_sampler_pid}" 2>/dev/null || true
    active_dependency_sampler_pid=""
  fi

  kubectl --context "${kube_context}" -n "${namespace}" get \
    scaledobjects.keda.sh,horizontalpodautoscalers.autoscaling,deployments.apps,pods,jobs.batch \
    -o yaml >"${run_dir}/workload-state.yml" 2>"${run_dir}/workload-state.err" || true
  kubectl --context "${kube_context}" -n "${namespace}" get events \
    --sort-by=.lastTimestamp >"${run_dir}/events.txt" 2>&1 || true
  if [[ ${scaler} == temporal ]]; then
    kubectl --context "${kube_context}" -n "${namespace}" logs deployment/temporal \
      --since-time="${started_at}" >"${run_dir}/temporal.log" 2>&1 || true
  fi
  kubectl --context "${kube_context}" -n "${namespace}" get scaledobjects.keda.sh \
    -o json >"${run_dir}/scaledobjects.json" 2>/dev/null || printf '{"items":[]}\n' >"${run_dir}/scaledobjects.json"
  kubectl --context "${kube_context}" -n "${namespace}" get horizontalpodautoscalers.autoscaling \
    -o json >"${run_dir}/hpas.json" 2>/dev/null || printf '{"items":[]}\n' >"${run_dir}/hpas.json"
  live_scaledobjects=$(jq '.items | length' "${run_dir}/scaledobjects.json")
  live_hpas=$(jq '.items | length' "${run_dir}/hpas.json")
  if [[ ${workload_mode} == control-plane ]]; then
    jq -n \
      --slurpfile scaledobjects "${run_dir}/scaledobjects.json" \
      --slurpfile hpas "${run_dir}/hpas.json" \
      '[ $hpas[0].items[] as $hpa |
         ($hpa.metadata.ownerReferences[]? | select(.kind == "ScaledObject") | .uid) as $ownerUID |
         $scaledobjects[0].items[] |
         select(.metadata.uid == $ownerUID) |
         {
           name: .metadata.name,
           hpaName: $hpa.metadata.name,
           timestamp: .metadata.creationTimestamp,
           hpaCreatedLatency: ((($hpa.metadata.creationTimestamp | fromdateiso8601) - (.metadata.creationTimestamp | fromdateiso8601)) * 1000)
         }
       ]' >"${run_dir}/control-plane-hpa-latency.json"
  fi
  kubectl --context "${kube_context}" -n keda logs deployment/keda-operator \
    --since-time="${started_at}" >"${run_dir}/keda-operator.log" 2>&1 || true
  kubectl --context "${kube_context}" -n keda logs deployment/keda-operator-metrics-apiserver \
    --since-time="${started_at}" >"${run_dir}/keda-metrics-apiserver.log" 2>&1 || true
  capture_keda_metrics keda-operator "${run_dir}/keda-operator-metrics.prom"
  capture_keda_metrics keda-operator-metrics-apiserver "${run_dir}/keda-metrics-apiserver-metrics.prom"

  metric_file="${metrics_dir}/scaledObjectLatencyMeasurement-${scaler}-targets.json"
  if [[ -f ${metric_file} ]]; then
    actual=$(jq 'length' "${metric_file}")
    hpa_complete=$(jq '[.[] | select(.hpaName != "")] | length' "${metric_file}")
    lifecycle_complete=$(jq '[.[] | select(.hpaName != "" and .scaledObjectActiveLatency > 0 and .deploymentReadyLatency > 0)] | length' "${metric_file}")
  fi
  if [[ ${workload_mode} == lifecycle ]]; then
    [[ ${rc} -eq 0 && ${actual} -eq ${objects} && ${lifecycle_complete} -eq ${objects} ]] && passed=true
  else
    actual=${live_scaledobjects}
    hpa_complete=${live_hpas}
    [[ ${rc} -eq 0 && ${actual} -eq ${objects} && ${hpa_complete} -eq ${objects} ]] && passed=true
  fi

  jq -n \
    --arg version "${version}" \
    --arg scaler "${scaler}" \
    --arg mode "${workload_mode}" \
    --arg repeat "${repeat_label}" \
    --arg namespace "${namespace}" \
    --argjson expected "${objects}" \
    --argjson measured "${actual}" \
    --argjson hpaComplete "${hpa_complete}" \
    --argjson lifecycleComplete "${lifecycle_complete}" \
    --argjson kubeBurnerRC "${rc}" \
    --argjson passed "${passed}" \
    '{version:$version, scaler:$scaler, mode:$mode, repeat:$repeat, namespace:$namespace, expected:$expected, measured:$measured, hpaComplete:$hpaComplete, lifecycleComplete:$lifecycleComplete, kubeBurnerRC:$kubeBurnerRC, passed:$passed}' \
    >"${run_dir}/assertions.json"

  if [[ ${passed} != true ]]; then
    printf 'FAILED %s/%s/%s: rc=%s, measured=%s/%s, HPA=%s/%s, lifecycle=%s/%s\n' \
      "${version}" "${scaler}" "${workload_mode}" "${rc}" "${actual}" "${objects}" \
      "${hpa_complete}" "${objects}" "${lifecycle_complete}" "${objects}" >&2
    failures=$((failures + 1))
  fi

  if ! kubectl --context "${kube_context}" delete namespace "${namespace}" \
    --ignore-not-found --wait=true --timeout="${namespace_delete_timeout}" \
    >"${run_dir}/namespace-delete.log" 2>&1; then
    printf 'error: namespace %s did not delete within %s; stopping to avoid contaminating the next cell\n' \
      "${namespace}" "${namespace_delete_timeout}" >&2
    cat "${run_dir}/namespace-delete.log" >&2
    return 1
  fi
  active_namespace=""
}

for version in ${versions}; do
  install_keda "${version}"
  for scaler in ${scalers}; do
    case "${scaler}" in
      cron|prometheus|metrics-api|kafka|kubernetes-resource|opensearch|temporal|redis|rabbitmq) ;;
      *) die "unsupported scaler: ${scaler}" ;;
    esac
    if ! scaler_supported "${version}" "${scaler}"; then
      printf 'Skipping unsupported matrix cell: KEDA %s / %s\n' "${version}" "${scaler}"
      skips=$((skips + 1))
      continue
    fi
    for objects in ${densities}; do
      for repeat in $(seq 1 "${repeats}"); do
        run_scaler "${version}" "${scaler}" "${objects}" "${repeat}"
      done
    done
  done
done

kubectl --context "${kube_context}" get pods -A -o wide >"${results_root}/pods-after.txt"
kubectl --context "${kube_context}" top nodes >"${results_root}/node-usage-after.txt" 2>&1 || true
"${workload_dir}/summarize.sh" "${results_root}"
if [[ " ${scalers} " == *" kubernetes-resource "* ||
      " ${scalers} " == *" opensearch "* ||
      " ${scalers} " == *" temporal "* ]]; then
  if command -v python3 >/dev/null 2>&1 && python3 -c 'import PIL' >/dev/null 2>&1; then
    python3 "${workload_dir}/plot-results.py" "${results_root}" ||
      printf 'warning: chart generation failed; rerun plot-results.py after inspecting the results\n' >&2
  else
    printf 'warning: PNG charts require Python 3 and Pillow; run plot-results.py after installing them\n' >&2
  fi
fi
printf '\nResults: %s\n' "${results_root}"
printf 'Skipped unsupported cells: %s\n' "${skips}"
if [[ ${failures} -gt 0 ]]; then
  printf '%s matrix cells failed invariant checks. See assertions.json and diagnostics.\n' "${failures}" >&2
  exit 1
fi
