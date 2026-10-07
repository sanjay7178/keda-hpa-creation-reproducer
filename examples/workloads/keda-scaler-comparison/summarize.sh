#!/usr/bin/env bash
set -euo pipefail

results_root=${1:?usage: summarize.sh RESULTS_DIRECTORY}
run_csv="${results_root}/summary-runs.csv"
aggregate_csv="${results_root}/summary-aggregate.csv"
summary_csv="${results_root}/summary.csv"
operator_csv="${results_root}/summary-keda-metrics.csv"
summary_md="${results_root}/summary.md"

printf 'version,scaler,mode,objects,run,metric,samples,p50_ms,p95_ms,p99_ms,min_ms,max_ms,avg_ms\n' >"${run_csv}"

while IFS= read -r quantiles_file; do
  assertions_file="$(dirname "$(dirname "${quantiles_file}")")/assertions.json"
  if [[ ! -f ${assertions_file} || $(jq -r .passed "${assertions_file}") != true ]]; then
    continue
  fi
  metrics_file=${quantiles_file/LatencyQuantilesMeasurement/LatencyMeasurement}
  samples=0
  if [[ -f ${metrics_file} ]]; then
    samples=$(jq 'length' "${metrics_file}")
  fi
  jq -r --argjson samples "${samples}" '.[] | [
    .metadata.kedaVersion,
    .metadata.scaler,
    .metadata.workloadMode,
    .metadata.objectCount,
    .metadata.repeat,
    .quantileName,
    $samples,
    .P50,
    .P95,
    .P99,
    .min,
    .max,
    .avg
  ] | join(",")' "${quantiles_file}" >>"${run_csv}"
done < <(find "${results_root}" -type f -name 'scaledObjectLatencyQuantilesMeasurement-*-targets.json' | sort -V)

while IFS= read -r latency_file; do
  run_dir=$(dirname "${latency_file}")
  assertions_file="${run_dir}/assertions.json"
  if [[ ! -f ${assertions_file} || $(jq -r .passed "${assertions_file}") != true ]]; then
    continue
  fi
  version=$(jq -r .version "${assertions_file}")
  scaler=$(jq -r .scaler "${assertions_file}")
  objects=$(jq -r .expected "${assertions_file}")
  run=$(jq -r .repeat "${assertions_file}")
  jq -r \
    --arg version "${version}" \
    --arg scaler "${scaler}" \
    --arg objects "${objects}" \
    --arg run "${run}" '
      def percentile($p): sort | .[((length - 1) * $p | floor)];
      [.[].hpaCreatedLatency] as $values |
      if ($values | length) == 0 then empty else
        [
          $version,
          $scaler,
          "control-plane",
          $objects,
          $run,
          "HPACreated",
          ($values | length),
          ($values | percentile(0.50)),
          ($values | percentile(0.95)),
          ($values | percentile(0.99)),
          ($values | min),
          ($values | max),
          (($values | add) / ($values | length))
        ] | join(",")
      end' "${latency_file}" >>"${run_csv}"
done < <(find "${results_root}" -type f -name control-plane-hpa-latency.json | sort -V)

awk -F, 'BEGIN { OFS="," }
  NR == 1 { next }
  {
    key=$1 SUBSEP $2 SUBSEP $3 SUBSEP $4 SUBSEP $6
    if (!(key in seen)) { seen[key]=1; order[++keys]=key }
    runs[key]++
    samples[key]+=$7
    p50[key]+=$8
    p95[key]+=$9
    p99[key]+=$10
    min[key]+=$11
    max[key]+=$12
    avg[key]+=$13
  }
  END {
    print "version,scaler,mode,objects,metric,runs,samples,mean_p50_ms,mean_p95_ms,mean_p99_ms,mean_min_ms,mean_max_ms,mean_avg_ms"
    for (i=1; i<=keys; i++) {
      key=order[i]
      split(key, fields, SUBSEP)
      printf "%s,%s,%s,%s,%s,%d,%d,%.2f,%.2f,%.2f,%.2f,%.2f,%.2f\n", fields[1],fields[2],fields[3],fields[4],fields[5],runs[key],samples[key],p50[key]/runs[key],p95[key]/runs[key],p99[key]/runs[key],min[key]/runs[key],max[key]/runs[key],avg[key]/runs[key]
    }
  }' "${run_csv}" >"${aggregate_csv}"

awk -F, 'BEGIN { OFS="," }
  NR == 1 { print $0,"p99_delta_from_baseline_pct"; next }
  {
    key=$2 SUBSEP $3 SUBSEP $4 SUBSEP $5
    if (!(key in baseline)) baseline[key]=$10
    if (baseline[key] == 0) delta=0; else delta=(($10 / baseline[key]) - 1) * 100
    printf "%s,%.2f\n", $0, delta
  }' "${aggregate_csv}" >"${summary_csv}"

printf 'version,scaler,mode,objects,run,samples,p50_ms,p95_ms,p99_ms,max_ms,scaler_errors\n' >"${operator_csv}"
while IFS= read -r metrics_file; do
  run_dir=$(dirname "${metrics_file}")
  assertions_file="${run_dir}/assertions.json"
  version=$(jq -r .version "${assertions_file}")
  scaler=$(jq -r .scaler "${assertions_file}")
  mode=$(jq -r .mode "${assertions_file}")
  objects=$(jq -r .expected "${assertions_file}")
  run=$(jq -r .repeat "${assertions_file}")
  run_namespace=$(jq -r .namespace "${assertions_file}")
  errors=$(awk -v namespace_label="namespace=\"${run_namespace}\"" '/^keda_scaler_detail_errors_total{/ && index($0, namespace_label) { total += $NF } END { print total + 0 }' "${metrics_file}")
  stats=$(awk -v namespace_label="namespace=\"${run_namespace}\"" '/^keda_internal_scale_loop_latency_seconds{/ && index($0, namespace_label) { print $NF * 1000 }' "${metrics_file}" |
    jq -Rsc 'def percentile($p): sort | .[((length - 1) * $p | floor)]; [splits("\n") | select(length > 0) | tonumber] as $v | if ($v | length) == 0 then empty else {samples:($v|length),p50:($v|percentile(0.50)),p95:($v|percentile(0.95)),p99:($v|percentile(0.99)),max:($v|max)} end')
  if [[ -n ${stats} ]]; then
    jq -r --arg version "${version}" --arg scaler "${scaler}" --arg mode "${mode}" --arg objects "${objects}" --arg run "${run}" --arg errors "${errors}" '[$version,$scaler,$mode,$objects,$run,.samples,.p50,.p95,.p99,.max,$errors] | join(",")' <<<"${stats}" >>"${operator_csv}"
  fi
done < <(find "${results_root}" -type f -name keda-operator-metrics.prom | sort -V)

{
  printf '# KEDA scaler comparison\n\n'
  printf 'Latency rows include only runs that passed all invariant checks. Incomplete runs are listed below.\n\n'
  printf '| KEDA | Scaler | Mode | Objects | Condition | Runs | Samples | Mean P50 (ms) | Mean P95 (ms) | Mean P99 (ms) | P99 vs oldest release |\n'
  printf '|---|---|---|---:|---|---:|---:|---:|---:|---:|---:|\n'
  tail -n +2 "${summary_csv}" | while IFS=, read -r version scaler mode objects metric runs samples p50 p95 p99 _min _max _avg delta; do
    printf '| %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s%% |\n' \
      "${version}" "${scaler}" "${mode}" "${objects}" "${metric}" "${runs}" "${samples}" \
      "${p50}" "${p95}" "${p99}" "${delta}"
  done

  printf '\n## Invariant checks\n\n'
  printf '| KEDA | Scaler | Mode | Expected | Measured | HPA complete | Lifecycle complete | kube-burner RC | Passed |\n'
  printf '|---|---|---|---:|---:|---:|---:|---:|---|\n'
  while IFS= read -r assertions_file; do
    jq -r '[.version,.scaler,.mode,.expected,.measured,.hpaComplete,.lifecycleComplete,.kubeBurnerRC,.passed] | "| \(.[0]) | \(.[1]) | \(.[2]) | \(.[3]) | \(.[4]) | \(.[5]) | \(.[6]) | \(.[7]) | \(.[8]) |"' "${assertions_file}"
  done < <(find "${results_root}" -type f -name assertions.json | sort -V)

  printf '\n## KEDA internal scale-loop snapshot\n\n'
  printf '| KEDA | Scaler | Mode | Objects | Run | Samples | P50 (ms) | P95 (ms) | P99 (ms) | Max (ms) | Scaler errors |\n'
  printf '|---|---|---|---:|---|---:|---:|---:|---:|---:|---:|\n'
  tail -n +2 "${operator_csv}" | while IFS=, read -r version scaler mode objects run samples p50 p95 p99 max errors; do
    printf '| %s | %s | %s | %s | %s | %s | %.3f | %.3f | %.3f | %.3f | %s |\n' \
      "${version}" "${scaler}" "${mode}" "${objects}" "${run}" "${samples}" \
      "${p50}" "${p95}" "${p99}" "${max}" "${errors}"
  done
} >"${summary_md}"

rm -f "${aggregate_csv}"
printf 'Wrote %s, %s, %s, and %s\n' "${run_csv}" "${summary_csv}" "${operator_csv}" "${summary_md}"
