#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
test_root="$(mktemp -d)"
trap 'rm -rf -- "${test_root}"' EXIT

git -C "${test_root}" init -q
mkdir -p -- "${test_root}/scripts"
install -m 0755 "${repo_root}/scripts/check-public.sh" \
  "${test_root}/scripts/check-public.sh"
printf 'hostname=cerberus1.local\n' >"${test_root}/safe.txt"
git -C "${test_root}" add scripts/check-public.sh safe.txt

(
  cd "${test_root}"
  scripts/check-public.sh --tracked >/dev/null
)

legacy_address='10.10.84.'
legacy_address+='28'
printf 'obsolete_management_address=%s\n' "${legacy_address}" \
  >"${test_root}/legacy-management.txt"
git -C "${test_root}" add legacy-management.txt
set +e
failure_output="$(
  cd "${test_root}"
  scripts/check-public.sh --staged 2>&1
)"
failure_status=$?
set -e
[[ "${failure_status}" == 1 ]]
grep -Fq \
  "unapproved literal 10/8 address ${legacy_address} in legacy-management.txt" \
  <<<"${failure_output}"

echo 'Public-safety test passed: former management addresses fail closed.'
