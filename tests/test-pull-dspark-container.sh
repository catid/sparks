#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
test_root="$(mktemp -d)"
cleanup() {
  chmod -R u+rwx -- "${test_root}" 2>/dev/null || true
  rm -rf -- "${test_root}"
}
trap cleanup EXIT

fixture_root="${test_root}/repo"
fake_bin="${test_root}/bin"
mkdir -p \
  "${fixture_root}/scripts" \
  "${fixture_root}/dspark_mia/bin" \
  "${fake_bin}"
cp -- "${repo_root}/scripts/pull-dspark-container.sh" \
  "${fixture_root}/scripts/pull-dspark-container.sh"

expected_digest="ghcr.io/anemll/dspark-vllm-gx10@sha256:$(printf 'a%.0s' {1..64})"
expected_id="sha256:$(printf '3%.0s' {1..64})"
mismatched_id="sha256:$(printf '4%.0s' {1..64})"
wrong_digest="ghcr.io/anemll/dspark-vllm-gx10@sha256:$(printf 'b%.0s' {1..64})"
printf 'image=%s\n' "${expected_digest}" > "${fixture_root}/dspark_mia/UPSTREAM.lock"
touch "${fixture_root}/dspark_mia/mia-throughput.env"
touch "${test_root}/cluster-key"
chmod 0600 "${test_root}/cluster-key"
printf '11111111111111111111111111111111\n' >"${test_root}/machine-id"

cat > "${fixture_root}/dspark_mia/bin/common.sh" <<EOF
#!/usr/bin/env bash
WORKER_HOST=\${TEST_WORKER_HOST:-cerebrus2}
CLUSTER_SSH_KEY=${test_root}/cluster-key
MIA_SSH_OPTIONS=(-i "\${CLUSTER_SSH_KEY}" -o BatchMode=yes)
require_ssh_identity() {
  [[ -f "\${CLUSTER_SSH_KEY}" ]] || return 2
}
need_command() {
  command -v "\$1" >/dev/null
}
EOF

cat > "${fake_bin}/hostname" <<'EOF'
#!/usr/bin/env bash
[[ "${1:-}" == "-s" ]]
printf '%s\n' "${MOCK_HOSTNAME:-cerebrus1}"
EOF

cat > "${fake_bin}/sudo" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "${MOCK_SUDO_LOG}"
if [[ "$*" == *'docker image inspect'* ]]; then
  printf '%s|%s\n' \
    "${MOCK_LOCAL_ID:-${EXPECTED_ID}}" \
    "${MOCK_LOCAL_DIGESTS:-${EXPECTED_DIGEST}}"
else
  printf 'mock pull complete\n'
fi
EOF

cat > "${fake_bin}/ssh" <<'EOF'
#!/usr/bin/env bash
args=("$@")
(( ${#args[@]} >= 2 ))
host="${args[${#args[@]} - 2]}"
remote_command="${args[${#args[@]} - 1]}"
if [[ "${remote_command}" == *machine-id* ]]; then
  printf 'identity %s\n' "${host}" >> "${MOCK_SSH_LOG}"
  case "${host}" in
    cerebrus1|cerebrus1.lan|10.10.84.28)
      printf '11111111111111111111111111111111\n'
      ;;
    cerebrus2|cerebrus2.lan|10.10.84.12)
      printf '22222222222222222222222222222222\n'
      ;;
    *) printf '33333333333333333333333333333333\n' ;;
  esac
  exit 0
fi
printf 'pull %s\n' "${host}" >> "${MOCK_SSH_LOG}"
image_id="${EXPECTED_ID}"
repo_digests="${EXPECTED_DIGEST}"
if [[ "${host}" == "${MOCK_MISMATCH_HOST:-}" ]]; then
  image_id="${MISMATCHED_ID}"
fi
if [[ "${host}" == "${MOCK_BAD_DIGEST_HOST:-}" ]]; then
  repo_digests="${WRONG_DIGEST}"
fi
printf '%s|%s\n' "${image_id}" "${repo_digests}"
EOF
chmod 0755 \
  "${fixture_root}/scripts/pull-dspark-container.sh" \
  "${fixture_root}/dspark_mia/bin/common.sh" \
  "${fake_bin}/hostname" "${fake_bin}/sudo" "${fake_bin}/ssh"

export EXPECTED_DIGEST="${expected_digest}"
export EXPECTED_ID="${expected_id}"
export MISMATCHED_ID="${mismatched_id}"
export WRONG_DIGEST="${wrong_digest}"
export MOCK_SUDO_LOG="${test_root}/sudo.log"
export MOCK_SSH_LOG="${test_root}/ssh.log"
export DSPARK_MACHINE_ID_FILE="${test_root}/machine-id"
export PATH="${fake_bin}:${PATH}"
script="${fixture_root}/scripts/pull-dspark-container.sh"

: > "${MOCK_SUDO_LOG}"
: > "${MOCK_SSH_LOG}"
describe_output="$("${script}" describe)"
grep -Fq -- '--pull-all' <<<"${describe_output}"
[[ ! -s "${MOCK_SUDO_LOG}" && ! -s "${MOCK_SSH_LOG}" ]]

pull_output="$("${script}" --pull)"
grep -Fq "image_id=${expected_id}" <<<"${pull_output}"
[[ "$(wc -l < "${MOCK_SUDO_LOG}")" == 2 ]]
[[ ! -s "${MOCK_SSH_LOG}" ]]

: > "${MOCK_SUDO_LOG}"
: > "${MOCK_SSH_LOG}"
both_output="$("${script}" --pull-both)"
grep -Fq "cerebrus1 and cerebrus2: ${expected_id}" <<<"${both_output}"
mapfile -t both_calls <"${MOCK_SSH_LOG}"
[[ "${both_calls[*]}" == "identity cerebrus2 pull cerebrus2" ]]

: > "${MOCK_SUDO_LOG}"
: > "${MOCK_SSH_LOG}"
all_output="$("${script}" --pull-all)"
grep -Fq "cerebrus1, cerebrus2, and cerebrus3: ${expected_id}" <<<"${all_output}"
mapfile -t pulled_hosts < "${MOCK_SSH_LOG}"
[[ "${pulled_hosts[*]}" == \
  "identity cerebrus2 identity cerebrus3 pull cerebrus2 pull cerebrus3" ]]
[[ "$(wc -l < "${MOCK_SUDO_LOG}")" == 2 ]]

: > "${MOCK_SSH_LOG}"
set +e
mismatch_output="$(MOCK_MISMATCH_HOST=cerebrus3 "${script}" --pull-all 2>&1)"
mismatch_status=$?
bad_digest_output="$(MOCK_BAD_DIGEST_HOST=cerebrus3 "${script}" --pull-all 2>&1)"
bad_digest_status=$?
unsafe_host_output="$(DSPARK_PULL_THIRD_HOST='-oProxyCommand=bad' "${script}" --pull-all 2>&1)"
unsafe_host_status=$?
duplicate_alias_output="$(DSPARK_PULL_THIRD_HOST=cerebrus2.lan "${script}" --pull-all 2>&1)"
duplicate_alias_status=$?
wrong_head_output="$(MOCK_HOSTNAME=cerebrus2 "${script}" --pull-all 2>&1)"
wrong_head_status=$?
set -e
[[ "${mismatch_status}" == 1 ]]
grep -Fq 'Image IDs differ:' <<<"${mismatch_output}"
[[ "${bad_digest_status}" == 1 ]]
grep -Fq 'Pinned repo digest is absent on cerebrus3' <<<"${bad_digest_output}"
[[ "${unsafe_host_status}" == 2 ]]
grep -Fq 'Unsafe DSPARK_PULL_THIRD_HOST' <<<"${unsafe_host_output}"
[[ "${duplicate_alias_status}" == 2 ]]
grep -Fq 'fewer than three distinct machines' <<<"${duplicate_alias_output}"
[[ "${wrong_head_status}" == 2 ]]
grep -Fq 'Coordinate the cluster image pull from cerebrus1' <<<"${wrong_head_output}"

printf 'image=not-a-digest\n' > "${fixture_root}/dspark_mia/UPSTREAM.lock"
set +e
invalid_lock_output="$("${script}" describe 2>&1)"
invalid_lock_status=$?
set -e
[[ "${invalid_lock_status}" == 1 ]]
grep -Fq 'does not contain exactly one valid digest-pinned image' \
  <<<"${invalid_lock_output}"

echo "Pinned-container pull test passed: local, two-node, and three-node paths verify exact digest and image identity offline."
