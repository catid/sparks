#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
repo_root="$(dirname -- "${root}")"
rendered_name="mia3-render-test-${$}.env"
rendered_path="${root}/${rendered_name}"
invalid_path="${root}/mia3-invalid-test-${$}.env"
resolver_log="${root}/mia3-resolver-test-${$}.log"
fake_bin="${root}/mia3-fake-bin-${$}"
cleanup_profiles() {
  rm -f -- "${rendered_path}" "${invalid_path}" "${resolver_log}" \
    "${root}/.mia3-sync-root"
  rm -rf -- "${fake_bin}"
}
trap cleanup_profiles EXIT

mkdir -- "${fake_bin}"
for command in getent hostname ip; do
  ln -s -- "${root}/tests/fixtures/fake-management-command" \
    "${fake_bin}/${command}"
done
export PATH="${fake_bin}:${PATH}"

check_profile() {
  local profile="$1" expected="$2"
  MIA3_PARTITION_PROFILE="${profile}" bash -c \
    'source "$1/bin/common.sh"; [[ "${VLLM_PP_LAYER_PARTITION}" == "$2" ]]' \
    -- "${root}" "${expected}"
}

check_profile default ''
check_profile 14-15-14 14,15,14
check_profile 15-15-13 15,15,13
check_profile 16-15-12 16,15,12

bash -c '
  source "$1/bin/common.sh"
  [[ "$(management_ssh_host cerberus2)" == cerberus2.local ]]
  [[ "$(management_ssh_host cerebrus3)" == cerberus3.local ]]
  [[ "$(management_ssh_host 192.168.0.2)" == 192.168.0.2 ]]
' -- "${root}"

MIA3_DFLASH=on bash -c 'source "$1/bin/common.sh"; [[ "${ENABLE_DSPARK}" == 1 ]]' -- "${root}"
MIA3_DFLASH=off bash -c 'source "$1/bin/common.sh"; [[ "${ENABLE_DSPARK}" == 0 ]]' -- "${root}"
bash -c 'source "$1/bin/common.sh"; [[ "${CX7_C3_PORT_MAP}" == c3-p0-to-c2 ]]' -- "${root}"
if MIA3_DFLASH=maybe bash -c 'source "$1/bin/common.sh"' -- "${root}" >/dev/null 2>&1; then
  echo "Unsafe DFlash selector unexpectedly passed." >&2
  exit 1
fi
if MIA3_PARTITION_PROFILE=../../etc/passwd bash -c 'source "$1/bin/common.sh"' -- "${root}" >/dev/null 2>&1; then
  echo "Unsafe profile selector unexpectedly passed." >&2
  exit 1
fi

MIA3_PROFILE_NAME="${rendered_name}" \
  MIA3_REMOTE_REPO_ROOT="${repo_root}/remote+checkout@1" \
  MIA3_CLUSTER_SSH_KEY="${HOME}/.ssh/id_ed25519+mia3@test" \
  MIA3_MODEL_HOST_PATH="${repo_root}/models+cache@1/model" \
  MIA3_HF_CACHE="${repo_root}/portable+hf@cache" \
  MIA3_TMP_HOST="${repo_root}/portable+mia3@tmp" \
  "${repo_root}/scripts/configure-mia3-profile.sh" >/dev/null
MIA3_ENV_FILE="${rendered_name}" bash -c '
  source "$1/bin/common.sh"
  [[ "${REMOTE_INSTALL_DIR}" == "$3/remote+checkout@1/dspark_mia3" ]]
  [[ "${MIA3_ENV_BASENAME}" == "$2" ]]
  [[ "${HEAD_HOST}" == cerberus1 ]]
  [[ "${RANK1_HOST}" == cerberus2 ]]
  [[ "${RANK2_HOST}" == cerberus3 ]]
  [[ -z "${HEAD_MGMT_IP+x}${RANK1_MGMT_IP+x}${RANK2_MGMT_IP+x}${MASTER_ADDR+x}" ]]
  resolve_management_plane
  [[ "$(rank_runtime_ipv4 0)" == 192.0.2.11 ]]
  [[ "$(rank_runtime_ipv4 1)" == 192.0.2.12 ]]
  [[ "$(rank_runtime_ipv4 2)" == 192.0.2.13 ]]
  [[ "${CLUSTER_SSH_KEY}" == "${HOME}/.ssh/id_ed25519+mia3@test" ]]
  [[ "${DSPARK_MODEL_HOST_PATH}" == "$3/models+cache@1/model" ]]
  [[ "${HF_CACHE}" == "$3/portable+hf@cache" ]]
  [[ "${DSPARK_TMP_HOST}" == "$3/portable+mia3@tmp" ]]
' -- "${root}" "${rendered_name}" "${repo_root}"

: >"${resolver_log}"
MIA3_ENV_FILE="${rendered_name}" MIA3_TEST_RESOLVER_LOG="${resolver_log}" \
  bash -c '
    source "$1/bin/common.sh"
    resolve_management_plane
    [[ "${MIA3_RUNTIME_MGMT_NAMES[*]}" == \
       "cerberus1.local cerberus2.local cerberus3.local" ]]
  ' -- "${root}"
[[ "$(tr '\n' ' ' <"${resolver_log}")" == \
   'cerberus1.local cerberus2.local cerberus3.local ' ]]

: >"${resolver_log}"
MIA3_ENV_FILE="${rendered_name}" MIA3_TEST_LOCAL_MISSING=1 \
  MIA3_TEST_RESOLVER_LOG="${resolver_log}" bash -c '
    source "$1/bin/common.sh"
    resolve_management_plane
    [[ "${MIA3_RUNTIME_MGMT_NAMES[*]}" == \
       "cerberus1.lan cerberus2.lan cerberus3.lan" ]]
  ' -- "${root}"
if grep -Eq '^(cerebrus|spark)' "${resolver_log}"; then
  echo 'Canonical Avahi fallback queried a legacy alias.' >&2
  exit 1
fi

: >"${resolver_log}"
MIA3_ENV_FILE="${rendered_name}" MIA3_TEST_CANONICAL_MISSING=1 \
  MIA3_TEST_RESOLVER_LOG="${resolver_log}" bash -c '
    source "$1/bin/common.sh"
    resolve_management_plane
    [[ "${MIA3_RUNTIME_MGMT_NAMES[*]}" == \
       "cerebrus1.lan cerebrus2.lan cerebrus3.lan" ]]
  ' -- "${root}"
grep -Fxq 'cerberus1.local' "${resolver_log}"
grep -Fxq 'cerebrus1.lan' "${resolver_log}"
if grep -Eq '^spark' "${resolver_log}"; then
  echo 'Resolver skipped the first available legacy alias.' >&2
  exit 1
fi

if MIA3_ENV_FILE="${rendered_name}" \
    MIA3_TEST_WRONG_ROUTE_ADDRESS=192.0.2.12 \
    bash -c 'source "$1/bin/common.sh"; resolve_management_plane' \
      -- "${root}" >/dev/null 2>&1; then
  echo 'Management DNS routed outside enP7s7 unexpectedly passed.' >&2
  exit 1
fi
if MIA3_ENV_FILE="${rendered_name}" MIA3_TEST_STALE_LOCAL=1 \
    bash -c 'source "$1/bin/common.sh"; resolve_management_plane' \
      -- "${root}" >/dev/null 2>&1; then
  echo 'Stale local management DNS unexpectedly passed.' >&2
  exit 1
fi
if MIA3_ENV_FILE="${rendered_name}" MIA3_TEST_AMBIGUOUS_RANK=2 \
    bash -c 'source "$1/bin/common.sh"; resolve_management_plane' \
      -- "${root}" >/dev/null 2>&1; then
  echo 'Ambiguous management DNS unexpectedly passed.' >&2
  exit 1
fi

if grep -Eq '10\.10\.84\.|HEAD_MGMT_IP|RANK[12]_MGMT_IP|^MASTER_ADDR=' \
    "${root}/mia3.env" "${rendered_path}"; then
  echo 'Rendered Mia3 profile persisted a numeric management setting.' >&2
  exit 1
fi
grep -Fxq 'RANK1_SYNC_HOST=192.168.0.2' "${rendered_path}"
grep -Fxq 'RANK2_SYNC_HOST=192.168.2.2' "${rendered_path}"
MIA3_ENV_FILE="${rendered_name}" "${root}/bin/validate-static.sh" >/dev/null

assert_remote_path_rejected() {
  local bad_path="$1"
  sed "s|^REMOTE_INSTALL_DIR=.*|REMOTE_INSTALL_DIR=${bad_path}|" \
    "${rendered_path}" >"${invalid_path}"
  if MIA3_ENV_FILE="$(basename -- "${invalid_path}")" \
      bash -c 'source "$1/bin/common.sh"' -- "${root}" >/dev/null 2>&1; then
    echo "Unsafe REMOTE_INSTALL_DIR unexpectedly passed: ${bad_path}" >&2
    exit 1
  fi
}
assert_remote_path_rejected "${HOME}/dspark_mia3"
assert_remote_path_rejected "${HOME}/repo/x/../dspark_mia3"
assert_remote_path_rejected "//${HOME#/}/repo/dspark_mia3"

digest_without_sentinel="$("${root}/bin/tree-digest.sh")"
install -m 0600 /dev/null "${root}/.mia3-sync-root"
digest_with_sentinel="$("${root}/bin/tree-digest.sh")"
[[ "${digest_with_sentinel}" == "${digest_without_sentinel}" ]]
rm -f -- "${root}/.mia3-sync-root"

echo "profile tests passed"
