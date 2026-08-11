#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
fixture_path="${repo_root}/tests/fixtures/cx7-installer/path"
test_root="$(mktemp -d)"
cleanup() {
  chmod -R u+rwx -- "${test_root}" 2>/dev/null || true
  rm -rf -- "${test_root}"
}
trap cleanup EXIT

mkdir -p "${test_root}/tmp" "${test_root}/sys-class-net"
for interface in \
  enp1s0f0np0 enP2p1s0f0np0 enp1s0f1np1 enP2p1s0f1np1; do
  mkdir -p "${test_root}/sys-class-net/${interface}"
done
test_log="${test_root}/sudo.log"
: >"${test_log}"

PATH="${fixture_path}:${PATH}" \
TMPDIR="${test_root}/tmp" \
CX7_SYS_CLASS_NET_ROOT="${test_root}/sys-class-net" \
CX7_INSTALLER_TEST_LOG="${test_log}" \
  "${repo_root}/scripts/install-cx7-netplan.sh" cerebrus1 >/dev/null

current_c3_output="$(
  PATH="${fixture_path}:${PATH}" \
  TMPDIR="${test_root}/tmp" \
  CX7_SYS_CLASS_NET_ROOT="${test_root}/sys-class-net" \
  CX7_INSTALLER_TEST_LOG="${test_log}" \
    "${repo_root}/scripts/install-cx7-netplan.sh" cerebrus3 \
      --c3-port-map c3-p0-to-c1
)"
/usr/bin/grep -Fq 'netplan/cerebrus3-40-cx7.yaml' <<<"${current_c3_output}"
/usr/bin/grep -Fq '(c3-p0-to-c1)' <<<"${current_c3_output}"

crossed_c3_output="$(
  PATH="${fixture_path}:${PATH}" \
  TMPDIR="${test_root}/tmp" \
  CX7_SYS_CLASS_NET_ROOT="${test_root}/sys-class-net" \
  CX7_INSTALLER_TEST_LOG="${test_log}" \
    "${repo_root}/scripts/install-cx7-netplan.sh" cerebrus3 \
      --c3-port-map=c3-p0-to-c2
)"
/usr/bin/grep -Fq 'netplan/cerebrus3-40-cx7-p0-to-c2.yaml' <<<"${crossed_c3_output}"
/usr/bin/grep -Fq '(c3-p0-to-c2)' <<<"${crossed_c3_output}"

set +e
implicit_apply_output="$(
  "${repo_root}/scripts/install-cx7-netplan.sh" cerebrus3 --apply 2>&1
)"
implicit_apply_status=$?
wrong_role_output="$(
  "${repo_root}/scripts/install-cx7-netplan.sh" cerebrus1 \
    --c3-port-map c3-p0-to-c2 2>&1
)"
wrong_role_status=$?
set -e
[[ "${implicit_apply_status}" == 2 ]]
/usr/bin/grep -Fq 'without an explicit --c3-port-map' <<<"${implicit_apply_output}"
[[ "${wrong_role_status}" == 2 ]]
/usr/bin/grep -Fq 'valid only for cerebrus3' <<<"${wrong_role_output}"

validation_root="$(
  /usr/bin/awk '
    $1 == "netplan" && $2 == "generate" && $3 == "--root-dir" {
      print $4
      exit
    }
  ' "${test_log}"
)"
[[ -n "${validation_root}" && "${validation_root}" == "${test_root}/tmp/"* ]]
[[ ! -e "${validation_root}" ]]
/usr/bin/grep -Fq "/usr/bin/rm -rf -- ${validation_root}" "${test_log}"

apply_target="${test_root}/40-cx7.yaml"
printf 'original-netplan\n' >"${apply_target}"
set +e
apply_output="$(
  PATH="${fixture_path}:${PATH}" \
  TMPDIR="${test_root}/tmp" \
  CX7_SYS_CLASS_NET_ROOT="${test_root}/sys-class-net" \
  CX7_INSTALLER_TEST_LOG="${test_log}" \
  CX7_INSTALLER_TEST_TARGET="${apply_target}" \
  CX7_INSTALLER_FAIL_APPLY_ONCE=1 \
    "${repo_root}/scripts/install-cx7-netplan.sh" cerebrus1 --apply 2>&1
)"
apply_status=$?
set -e
[[ "${apply_status}" == 73 ]]
/usr/bin/grep -Fq 'restoring the prior Netplan state' <<<"${apply_output}"
[[ "$(<"${apply_target}")" == original-netplan ]]
[[ -e "${apply_target}.apply-failed" ]]

rm -f -- "${apply_target}" "${apply_target}.apply-failed"
set +e
new_target_output="$(
  PATH="${fixture_path}:${PATH}" \
  TMPDIR="${test_root}/tmp" \
  CX7_SYS_CLASS_NET_ROOT="${test_root}/sys-class-net" \
  CX7_INSTALLER_TEST_LOG="${test_log}" \
  CX7_INSTALLER_TEST_TARGET="${apply_target}" \
  CX7_INSTALLER_FAIL_APPLY_ONCE=1 \
    "${repo_root}/scripts/install-cx7-netplan.sh" cerebrus1 --apply 2>&1
)"
new_target_status=$?
set -e
[[ "${new_target_status}" == 73 ]]
/usr/bin/grep -Fq 'restoring the prior Netplan state' <<<"${new_target_output}"
[[ ! -e "${apply_target}" ]]

echo "CX-7 installer test passed: map selection, cleanup, and failed-apply rollback are safe."
