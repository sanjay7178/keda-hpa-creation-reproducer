#!/usr/bin/env bash
set -euo pipefail
reproducer_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
revision=69cca726f3487cd134f7ae26d5375dd4f9428807
source_dir="${reproducer_root}/.tools/kube-burner-source"
patch_file="${reproducer_root}/patches/benchmark-core.patch"
for required in git go; do
  command -v "${required}" >/dev/null || { printf 'error: missing %s\n' "${required}" >&2; exit 1; }
done
mkdir -p "${reproducer_root}/.tools/bin"
if [[ ! -d ${source_dir}/.git ]]; then
  git clone --filter=blob:none --no-checkout https://github.com/kube-burner/kube-burner.git "${source_dir}"
  git -C "${source_dir}" checkout --detach "${revision}"
fi
[[ $(git -C "${source_dir}" rev-parse HEAD) == "${revision}" ]] || {
  printf 'error: existing source checkout is not at pinned revision %s\n' "${revision}" >&2
  exit 1
}
if git -C "${source_dir}" apply --check "${patch_file}"; then
  git -C "${source_dir}" apply "${patch_file}"
elif ! git -C "${source_dir}" apply --reverse --check "${patch_file}"; then
  printf 'error: historical core patch does not apply to the existing checkout\n' >&2
  exit 1
fi
(
  cd "${source_dir}"
  CGO_ENABLED=0 go build -trimpath \
    -ldflags "-X github.com/cloud-bulldozer/go-commons/v2/version.Version=hpa-reproducer -X github.com/cloud-bulldozer/go-commons/v2/version.GitCommit=${revision}+benchmark-core.patch" \
    -o "${reproducer_root}/.tools/bin/kube-burner" ./cmd/kube-burner
)
"${reproducer_root}/.tools/bin/kube-burner" version
printf 'Installed: %s\n' "${reproducer_root}/.tools/bin/kube-burner"
