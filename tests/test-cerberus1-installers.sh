#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
fixture="${repo_root}/tests/fixtures/hostname-installers/fake-command"
test_root="$(mktemp -d)"
trap 'rm -rf -- "${test_root}"' EXIT

fake_bin="${test_root}/bin"
sudo_log="${test_root}/sudo.log"
mkdir -p "${fake_bin}"
for command in hostname sudo docker nvidia-smi nvcc pipx systemd-analyze nginx; do
  ln -s "${fixture}" "${fake_bin}/${command}"
done

run_host_package_install() {
  local hostname="$1"
  local output_file="$2"
  : >"${sudo_log}"
  TEST_HOSTNAME="${hostname}" \
  TEST_SUDO_LOG="${sudo_log}" \
  PATH="${fake_bin}:/usr/bin:/bin" \
    "${repo_root}/scripts/install-host-packages.sh" --install >"${output_file}"
}

assert_nginx_package() {
  local expected="$1"
  local install_line
  install_line="$(grep -F 'apt-get install ' "${sudo_log}")"
  if [[ "${expected}" == "yes" ]]; then
    grep -Eq '(^|[[:space:]])nginx([[:space:]]|$)' <<<"${install_line}"
  elif grep -Eq '(^|[[:space:]])nginx([[:space:]]|$)' <<<"${install_line}"; then
    echo "Unexpected nginx package for non-dashboard host." >&2
    exit 1
  fi
}

run_host_package_install cerberus1 "${test_root}/cerberus1-packages.out"
assert_nginx_package yes
grep -Fq 'Cerberus node 1 dashboard dependency installed: nginx' \
  "${test_root}/cerberus1-packages.out"

run_host_package_install spark1 "${test_root}/spark1-packages.out"
assert_nginx_package yes

run_host_package_install cerberus2 "${test_root}/cerberus2-packages.out"
assert_nginx_package no
grep -Fq 'belongs on cerberus1' "${test_root}/cerberus2-packages.out"

run_dashboard_install() {
  local hostname="$1"
  shift
  TEST_HOSTNAME="${hostname}" \
  TEST_SUDO_LOG="${sudo_log}" \
  SPARK_SERVICE_USER="$(id -un)" \
  PATH="${fake_bin}:/usr/bin:/bin" \
    "${repo_root}/scripts/install-dashboard.sh" install "$@"
}

: >"${sudo_log}"
cerberus_output="$(
  run_dashboard_install cerberus1 --web --allow-unauthenticated-web
)"
grep -Fq 'Web endpoint: https://cerberus1.lan' <<<"${cerberus_output}"
grep -Fq 'https://cerberus1.lan' <<<"${cerberus_output}"

run_dashboard_install spark1 >/dev/null

set +e
rejected_output="$(run_dashboard_install cerberus2 2>&1)"
rejected_status=$?
set -e
[[ "${rejected_status}" == "2" ]]
grep -Fq 'Install the cluster dashboard on cerberus1 (legacy spark1).' \
  <<<"${rejected_output}"

echo "Cerberus node 1 installer compatibility tests passed."
