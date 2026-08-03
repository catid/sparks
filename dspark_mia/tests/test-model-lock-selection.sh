#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
repo_root="$(dirname "${root}")"
default_lock="${root}/MODEL.lock.json"

alternate_lock="$(mktemp "${root}/test-model-lock.XXXXXX.json")"
relative_profile="$(mktemp "${root}/test-model-lock.XXXXXX.env")"
absolute_profile="$(mktemp "${root}/test-model-lock.XXXXXX.env")"
symlink_lock="${root}/test-model-lock-symlink.$$.json"
symlink_profile="$(mktemp "${root}/test-model-lock.XXXXXX.env")"
outside_lock="$(mktemp /tmp/dspark-model-lock.XXXXXX.json)"
outside_profile="$(mktemp "${root}/test-model-lock.XXXXXX.env")"
wrong_suffix_lock="$(mktemp "${root}/test-model-lock.XXXXXX.lock")"
wrong_suffix_profile="$(mktemp "${root}/test-model-lock.XXXXXX.env")"
cleanup() {
  rm -f -- \
    "${alternate_lock}" "${relative_profile}" "${absolute_profile}" \
    "${symlink_lock}" "${symlink_profile}" "${outside_lock}" \
    "${outside_profile}" "${wrong_suffix_lock}" "${wrong_suffix_profile}"
}
trap cleanup EXIT

jq \
  '.repo_id = "example/model-lock-selection" |
   .revision = "test-revision"' \
  "${default_lock}" >"${alternate_lock}"
cp -- "${default_lock}" "${outside_lock}"
cp -- "${default_lock}" "${wrong_suffix_lock}"
ln -s -- "$(basename "${default_lock}")" "${symlink_lock}"

make_profile() {
  local destination="$1"
  local selected_lock="$2"
  cp -- "${root}/mia-throughput.env" "${destination}"
  printf '\nMIA_MODEL_LOCK=%s\n' "${selected_lock}" >>"${destination}"
}

resolved="$({
  MIA_ENV_FILE=mia-throughput.env \
    bash -c 'source "$1"; printf "%s\n" "${MIA_MODEL_LOCK}"' \
      _ "${root}/bin/common.sh"
})"
[[ "${resolved}" == "${default_lock}" ]]

make_profile "${relative_profile}" "$(basename "${alternate_lock}")"
resolved="$({
  MIA_ENV_FILE="${relative_profile}" \
    bash -c 'source "$1"; printf "%s\n" "${MIA_MODEL_LOCK}"' \
      _ "${root}/bin/common.sh"
})"
[[ "${resolved}" == "${alternate_lock}" ]]

make_profile "${absolute_profile}" "${alternate_lock}"
resolved="$({
  MIA_ENV_FILE="${absolute_profile}" \
    bash -c 'source "$1"; printf "%s\n" "${MIA_MODEL_LOCK}"' \
      _ "${root}/bin/common.sh"
})"
[[ "${resolved}" == "${alternate_lock}" ]]

assert_rejected() {
  local profile="$1"
  local expected="$2"
  local output status
  set +e
  output="$(MIA_ENV_FILE="${profile}" bash -c 'source "$1"' \
    _ "${root}/bin/common.sh" 2>&1)"
  status=$?
  set -e
  [[ "${status}" == "2" ]] || {
    echo "Expected model-lock rejection status 2, got ${status}." >&2
    exit 1
  }
  grep -Fq "${expected}" <<<"${output}"
}

make_profile "${symlink_profile}" "$(basename "${symlink_lock}")"
assert_rejected "${symlink_profile}" "Model lock must be a regular, non-symlink file"

make_profile "${outside_profile}" "${outside_lock}"
assert_rejected "${outside_profile}" "Model lock must be directly inside ${root}"

make_profile "${wrong_suffix_profile}" "$(basename "${wrong_suffix_lock}")"
assert_rejected "${wrong_suffix_profile}" "Model lock basename must end in .json"

describe="$({
  MIA_ENV_FILE="${relative_profile}" \
  MODEL_DIR=/tmp/model-lock-selection-destination \
    "${repo_root}/scripts/download-pinned-model.sh" describe
})"
grep -Fq 'repo=example/model-lock-selection' <<<"${describe}"
grep -Fq 'revision=test-revision' <<<"${describe}"
grep -Fq 'destination=/tmp/model-lock-selection-destination' <<<"${describe}"

echo "Model-lock selection test passed: default/alternate locks, containment, and downloader profile propagation are explicit."
