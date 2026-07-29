#!/usr/bin/env bash
set -euo pipefail

# Prepare the dedicated, forced-command Spark 1 -> Spark 2 control key without
# replacing any existing SSH key or sudoers rule. This script does not inspect
# or change model-service state.

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly root_dir
readonly action="${1:-}"
readonly control_user="catid"
readonly rank0_hostname="spark1"
readonly rank1_hostname="spark2"
readonly rank0_source="192.168.100.10"
readonly dedicated_key="/home/catid/.ssh/id_ed25519_deepseek_v4_rank1_control"
readonly installed_wrapper="/usr/local/libexec/dgx-spark-deepseek-v4-rank1-control"
readonly installed_sudoers="/etc/sudoers.d/dgx-spark-deepseek-v4-rank1-control"
readonly wrapper_source="${root_dir}/libexec/dgx-spark-deepseek-v4-rank1-control"
readonly sudoers_source="${root_dir}/security/dgx-spark-deepseek-v4-rank1-control.sudoers"
readonly authorized_keys="/home/catid/.ssh/authorized_keys"

usage() {
  cat <<'EOF'
Usage:
  install-deepseek-v4-rank1-control.sh rank0-key
  install-deepseek-v4-rank1-control.sh rank1-policy PUBLIC_KEY_FILE

rank0-key:
  Run locally as catid on Spark 1. Creates the dedicated Ed25519 key only when
  it is absent; never replaces an existing key.

rank1-policy:
  Run locally on Spark 2. Installs the root-owned forced-command wrapper and
  exact sudoers policy, then appends a source-restricted authorized_keys entry.
  Existing authorized_keys and sudoers entries are preserved.

Neither action starts, stops, restarts, enables, or disables a service.
EOF
}

run_root() {
  if ((EUID == 0)); then
    "$@"
  else
    /usr/bin/sudo "$@"
  fi
}

require_host() {
  local expected="$1"
  local actual
  actual="$(/usr/bin/hostname -s)"
  if [[ "${actual}" != "${expected}" ]]; then
    printf 'This action must run on %s (current host: %s).\n' \
      "${expected}" "${actual}" >&2
    exit 2
  fi
}

validate_public_key() {
  local public_key_file="$1"
  local line key_type key_blob nonblank_count

  if [[ ! -f "${public_key_file}" || ! -r "${public_key_file}" ]]; then
    printf 'Public-key file is not readable: %s\n' "${public_key_file}" >&2
    exit 2
  fi

  nonblank_count="$(
    /usr/bin/awk 'NF && $1 !~ /^#/ { count++ } END { print count + 0 }' \
      "${public_key_file}"
  )"
  if [[ "${nonblank_count}" != "1" ]]; then
    echo "Public-key file must contain exactly one non-comment key." >&2
    exit 2
  fi
  line="$(
    /usr/bin/awk 'NF && $1 !~ /^#/ { print; exit }' "${public_key_file}"
  )"
  read -r key_type key_blob _ <<<"${line}"
  if [[ "${key_type}" != "ssh-ed25519" ||
    ! "${key_blob}" =~ ^[A-Za-z0-9+/]+={0,3}$ ]]; then
    echo "The dedicated control key must be a plain Ed25519 public key." >&2
    exit 2
  fi
  if ! /usr/bin/ssh-keygen -lf "${public_key_file}" >/dev/null 2>&1; then
    echo "Public-key file is not a valid OpenSSH public key." >&2
    exit 2
  fi

  validated_key_type="${key_type}"
  validated_key_blob="${key_blob}"
}

case "${action}" in
  rank0-key)
    if (($# != 1)); then
      usage >&2
      exit 2
    fi
    require_host "${rank0_hostname}"
    if [[ "$(/usr/bin/id -un)" != "${control_user}" ]]; then
      echo "Run rank0-key locally as catid, without sudo." >&2
      exit 2
    fi
    if [[ -L "/home/catid/.ssh" ||
      (-e "/home/catid/.ssh" && ! -d "/home/catid/.ssh") ]]; then
      echo "/home/catid/.ssh must be a real directory, not a symlink." >&2
      exit 2
    fi
    if [[ ! -d "/home/catid/.ssh" ]]; then
      /usr/bin/install -d -m 0700 "/home/catid/.ssh"
    fi
    if [[ "$(/usr/bin/stat -c '%U:%G:%a' /home/catid/.ssh)" != \
      "catid:catid:700" ]]; then
      echo "/home/catid/.ssh must be owned by catid:catid with mode 0700." >&2
      exit 2
    fi

    if [[ -e "${dedicated_key}" || -e "${dedicated_key}.pub" ]]; then
      if [[ ! -f "${dedicated_key}" || ! -f "${dedicated_key}.pub" ]]; then
        echo "Dedicated keypair is incomplete; refusing to overwrite it." >&2
        exit 2
      fi
      echo "Preserving the existing dedicated keypair."
    else
      umask 077
      /usr/bin/ssh-keygen -q -t ed25519 -N '' \
        -C "deepseek-v4-rank1-control@spark1" \
        -f "${dedicated_key}"
      echo "Created the dedicated rank-1 control keypair."
    fi

    if [[ "$(/usr/bin/stat -c '%U:%G:%a' "${dedicated_key}")" != \
      "catid:catid:600" ]]; then
      echo "Dedicated private key must be owned by catid:catid, mode 0600." >&2
      exit 2
    fi
    derived_public="$(
      /usr/bin/ssh-keygen -y -f "${dedicated_key}"
    )"
    # This OpenSSH build may retain the private key's trailing comment in
    # `ssh-keygen -y` output. Type plus key blob are the cryptographic identity;
    # comments on the private and .pub files need not be equal.
    read -r derived_key_type derived_key_blob _ <<<"${derived_public}"
    validate_public_key "${dedicated_key}.pub"
    if [[ "${derived_key_type}" != "${validated_key_type}" ||
      "${derived_key_blob}" != "${validated_key_blob}" ]]; then
      echo "Dedicated public key does not match its private key." >&2
      exit 2
    fi

    /usr/bin/ssh-keygen -lf "${dedicated_key}.pub"
    printf 'Public key for Spark 2: %s.pub\n' "${dedicated_key}"
    echo "No existing SSH key, authorized_keys entry, or service was changed."
    ;;

  rank1-policy)
    if (($# != 2)); then
      usage >&2
      exit 2
    fi
    require_host "${rank1_hostname}"
    validate_public_key "$2"

    for path in "${wrapper_source}" "${sudoers_source}"; do
      if [[ ! -f "${path}" ]]; then
        printf 'Required policy source is missing: %s\n' "${path}" >&2
        exit 1
      fi
    done
    if [[ ! -x "${wrapper_source}" ]]; then
      echo "Forced-command wrapper source is not executable." >&2
      exit 1
    fi
    /usr/sbin/visudo -cf "${sudoers_source}" >/dev/null

    for path in "${installed_wrapper}" "${installed_sudoers}"; do
      if [[ -L "${path}" ]]; then
        printf 'Refusing to replace a symlinked policy target: %s\n' \
          "${path}" >&2
        exit 1
      fi
    done
    if [[ -L "/home/catid/.ssh" ||
      (-e "/home/catid/.ssh" && ! -d "/home/catid/.ssh") ]]; then
      echo "/home/catid/.ssh must be a real directory, not a symlink." >&2
      exit 1
    fi
    if [[ -L "${authorized_keys}" ||
      (-e "${authorized_keys}" && ! -f "${authorized_keys}") ]]; then
      echo "authorized_keys must be a regular file, not a symlink." >&2
      exit 1
    fi

    authorized_line="$(
      printf 'from="%s",restrict,command="%s" %s %s %s' \
        "${rank0_source}" "${installed_wrapper}" \
        "${validated_key_type}" "${validated_key_blob}" \
        "deepseek-v4-rank1-control@spark1"
    )"

    run_root /usr/bin/install -d -o root -g root -m 0755 \
      /usr/local/libexec
    run_root /usr/bin/install -o root -g root -m 0755 \
      "${wrapper_source}" "${installed_wrapper}"
    run_root /usr/bin/install -o root -g root -m 0440 \
      "${sudoers_source}" "${installed_sudoers}"
    run_root /usr/sbin/visudo -cf "${installed_sudoers}" >/dev/null
    if [[ "$(run_root /usr/bin/stat -c '%U:%G:%a' "${installed_wrapper}")" != \
      "root:root:755" ||
      "$(run_root /usr/bin/stat -c '%U:%G:%a' "${installed_sudoers}")" != \
      "root:root:440" ]]; then
      echo "Installed control files have unsafe ownership or permissions." >&2
      exit 1
    fi
    if [[ "$(/usr/bin/sha256sum "${wrapper_source}" | /usr/bin/awk '{print $1}')" != \
      "$(run_root /usr/bin/sha256sum "${installed_wrapper}" |
        /usr/bin/awk '{print $1}')" ||
      "$(/usr/bin/sha256sum "${sudoers_source}" | /usr/bin/awk '{print $1}')" != \
      "$(run_root /usr/bin/sha256sum "${installed_sudoers}" |
        /usr/bin/awk '{print $1}')" ]]; then
      echo "Installed control files do not match repository sources." >&2
      exit 1
    fi

    run_root /usr/bin/install -d -o catid -g catid -m 0700 \
      /home/catid/.ssh
    if [[ ! -e "${authorized_keys}" ]]; then
      run_root /usr/bin/install -o catid -g catid -m 0600 \
        /dev/null "${authorized_keys}"
    else
      run_root /usr/bin/chown catid:catid "${authorized_keys}"
      run_root /usr/bin/chmod 0600 "${authorized_keys}"
    fi

    if /usr/bin/grep -Fqx -- "${authorized_line}" "${authorized_keys}"; then
      echo "Dedicated forced-command authorized_keys entry already exists."
    elif /usr/bin/grep -Fq -- "${validated_key_blob}" "${authorized_keys}"; then
      cat >&2 <<'EOF'
This public key already appears in authorized_keys with different options.
Refusing to add a second entry: OpenSSH could select the less-restricted one.
Generate a fresh dedicated key instead; no existing entry was changed.
EOF
      exit 1
    else
      # A leading newline preserves a final existing line even if the file did
      # not already end with one. Blank authorized_keys lines are harmless.
      if ((EUID == 0)); then
        printf '\n%s\n' "${authorized_line}" >>"${authorized_keys}"
      else
        printf '\n%s\n' "${authorized_line}" |
          /usr/bin/sudo /usr/bin/tee -a "${authorized_keys}" >/dev/null
      fi
      echo "Appended the dedicated forced-command authorized_keys entry."
    fi

    run_root /usr/bin/chown catid:catid "${authorized_keys}"
    run_root /usr/bin/chmod 0600 "${authorized_keys}"
    /usr/bin/ssh-keygen -lf "$2"
    cat <<EOF
Installed:
  ${installed_wrapper}
  ${installed_sudoers}
Preserved all pre-existing SSH keys and sudoers files.
No service state was inspected or changed.
EOF
    ;;

  -h | --help)
    usage
    ;;

  *)
    usage >&2
    exit 2
    ;;
esac
