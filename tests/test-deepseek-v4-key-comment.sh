#!/usr/bin/env bash
set -euo pipefail

# Regression for OpenSSH builds whose `ssh-keygen -y` output retains the
# private key's comment. Exercise the real rank0-key installer path while
# redirecting its fixed key and ssh-keygen paths into an isolated directory.

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly installer="${root_dir}/bin/install-deepseek-v4-rank1-control.sh"
tmp_dir="$(/usr/bin/mktemp -d)"
trap '/usr/bin/rm -rf -- "${tmp_dir}"' EXIT
readonly test_key="${tmp_dir}/control-key"
readonly test_installer="${tmp_dir}/install-control"
readonly keygen_wrapper="${tmp_dir}/ssh-keygen"
readonly keygen_log="${tmp_dir}/derived-public.log"

/usr/bin/ssh-keygen -q -t ed25519 -N '' \
  -C "public-file-comment" -f "${test_key}"

{
  printf '%s\n' '#!/usr/bin/env bash'
  printf '%s\n' 'set -euo pipefail'
  # Emit literal variable references into the recorder.
  # shellcheck disable=SC2016
  printf '%s\n' 'if [[ "${1:-}" == "-y" ]]; then'
  # shellcheck disable=SC2016
  printf '%s\n' '  derived="$(/usr/bin/ssh-keygen "$@")"'
  # shellcheck disable=SC2016
  printf '%s\n' '  read -r key_type key_blob _ <<<"${derived}"'
  # shellcheck disable=SC2016
  printf '%s\n' '  printf "%s %s %s\n" "${key_type}" "${key_blob}" "derived-private-comment" |'
  # shellcheck disable=SC2016
  printf '%s\n' '    /usr/bin/tee "${KEYGEN_COMMENT_LOG}"'
  printf '%s\n' '  exit 0'
  printf '%s\n' 'fi'
  # shellcheck disable=SC2016
  printf '%s\n' 'exec /usr/bin/ssh-keygen "$@"'
} >"${keygen_wrapper}"
/usr/bin/chmod 0755 "${keygen_wrapper}"

current_hostname="$(/usr/bin/hostname -s)"
/usr/bin/sed \
  -e "s|^readonly rank0_hostname=.*|readonly rank0_hostname=\"${current_hostname}\"|" \
  -e "s|^readonly dedicated_key=.*|readonly dedicated_key=\"${test_key}\"|" \
  -e "s|/usr/bin/ssh-keygen|${keygen_wrapper}|g" \
  "${installer}" >"${test_installer}"
/usr/bin/chmod 0755 "${test_installer}"

# Fail closed before executing the transformed copy: it must not retain the
# live dedicated-key path or invoke the real ssh-keygen directly.
/usr/bin/grep -Fqx \
  "readonly dedicated_key=\"${test_key}\"" "${test_installer}"
if /usr/bin/grep -Fq \
  '/home/catid/.ssh/id_ed25519_deepseek_v4_rank1_control' \
  "${test_installer}"; then
  echo "Temporary installer still contains the live dedicated-key path." >&2
  exit 1
fi
if /usr/bin/grep -Fq '/usr/bin/ssh-keygen' "${test_installer}"; then
  echo "Temporary installer still contains the real ssh-keygen path." >&2
  exit 1
fi

output="$(
  KEYGEN_COMMENT_LOG="${keygen_log}" "${test_installer}" rank0-key
)"
/usr/bin/grep -Fq 'Preserving the existing dedicated keypair.' <<<"${output}"
/usr/bin/grep -Fq "Public key for Spark 2: ${test_key}.pub" <<<"${output}"

read -r derived_type derived_blob derived_comment <"${keygen_log}"
[[ "${derived_type}" == "ssh-ed25519" ]]
[[ -n "${derived_blob}" ]]
[[ "${derived_comment}" == "derived-private-comment" ]]

public_type=""
public_blob=""
read -r public_type public_blob _ <"${test_key}.pub"
[[ "${derived_type}" == "${public_type}" ]]
[[ "${derived_blob}" == "${public_blob}" ]]

echo "Commented ssh-keygen -y regression test passed."
