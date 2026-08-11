#!/usr/bin/env bash

set -euo pipefail
umask 077

voice_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
lock_file="${voice_dir}/openclaw/runtime.lock.json"
action="${1:-verify}"
runtime_root="${VOICE_OPENCLAW_RUNTIME_ROOT:-/opt/cerberus/openclaw-runtime}"
legacy_runtime_root="${VOICE_OPENCLAW_LEGACY_RUNTIME_ROOT:-/opt/cerebrus/openclaw-runtime}"

usage() {
  cat <<'EOF'
Usage: install-openclaw-runtime.sh [verify|install|verify-installed]

verify           Validate the checked-in release lock and installer only.
install          Install the pinned arm64 Node and OpenClaw releases atomically.
verify-installed Verify the exact installed versions without changing the host.

Environment:
  VOICE_OPENCLAW_RUNTIME_ROOT  Install root
                              (default: /opt/cerberus/openclaw-runtime)
  VOICE_OPENCLAW_LEGACY_RUNTIME_ROOT
                              Pre-rename root copied atomically when valid
EOF
}

case "${action}" in
  verify|install|verify-installed) ;;
  -h|--help) usage; exit 0 ;;
  *) usage >&2; exit 2 ;;
esac
if (($# > 1)); then
  usage >&2
  exit 2
fi

fail() {
  echo "OpenClaw runtime installer: $*" >&2
  exit 2
}

safe_absolute_path() {
  [[ "$1" =~ ^/[A-Za-z0-9._/@+-]+$ && "$1" != "/" &&
     "$1" != *"/../"* && "$1" != */.. &&
     "$1" != *"/./"* && "$1" != */. ]]
}

safe_absolute_path "${runtime_root}" || fail "unsafe runtime root"
safe_absolute_path "${legacy_runtime_root}" || fail "unsafe legacy runtime root"
[[ "${runtime_root}" != "${legacy_runtime_root}" ]] ||
  fail "canonical and legacy runtime roots must differ"
case "${runtime_root}" in
  /bin|/boot|/dev|/etc|/home|/lib|/lib64|/opt|/proc|/root|/run|/sbin|/srv|/sys|/tmp|/usr|/var)
    fail "runtime root is too broad"
    ;;
esac
[[ -f "${lock_file}" && ! -L "${lock_file}" ]] || fail "missing release lock"

mapfile -t release_values < <(python3 - "${lock_file}" <<'PY'
import json
import pathlib
import sys

data = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
node = data.get("node", {})
openclaw = data.get("openclaw", {})
expected = {
    "node.version": "24.15.0",
    "node.architecture": "arm64",
    "node.archive": "node-v24.15.0-linux-arm64.tar.xz",
    "node.url": "https://nodejs.org/dist/v24.15.0/node-v24.15.0-linux-arm64.tar.xz",
    "node.sha256": "f3d5a797b5d210ce8e2cb265544c8e482eaedcb8aa409a8b46da7e8595d0dda0",
    "openclaw.package": "openclaw",
    "openclaw.version": "2026.7.1-2",
    "openclaw.tarball": "https://registry.npmjs.org/openclaw/-/openclaw-2026.7.1-2.tgz",
    "openclaw.integrity": "sha512-ycF3yPcbjN6bUPeaUx6Mh6vze1hQWoD3CT/wWcmD7a8xaHHHRUaAlaq+lFxMHf1ssEgODVAwjlzYqp2twkYZ7g==",
}
actual = {
    "node.version": node.get("version"),
    "node.architecture": node.get("architecture"),
    "node.archive": node.get("archive"),
    "node.url": node.get("url"),
    "node.sha256": node.get("sha256"),
    "openclaw.package": openclaw.get("package"),
    "openclaw.version": openclaw.get("version"),
    "openclaw.tarball": openclaw.get("tarball"),
    "openclaw.integrity": openclaw.get("integrity"),
}
for key, value in expected.items():
    if actual.get(key) != value:
        raise SystemExit(f"release lock mismatch: {key}")
for key in (
    "node.version", "node.archive", "node.url", "node.sha256",
    "openclaw.version", "openclaw.tarball", "openclaw.integrity",
):
    print(actual[key])
PY
)
[[ "${#release_values[@]}" == 7 ]] || fail "could not parse release lock"
node_version="${release_values[0]}"
node_archive="${release_values[1]}"
node_url="${release_values[2]}"
node_sha256="${release_values[3]}"
openclaw_version="${release_values[4]}"
openclaw_tarball="${release_values[5]}"
openclaw_integrity="${release_values[6]}"

for script in \
  "${voice_dir}/scripts/install-openclaw-runtime.sh" \
  "${voice_dir}/openclaw/gateway-wrapper.sh"; do
  bash -n "${script}"
done

node_release="${runtime_root}/releases/node-v${node_version}-linux-arm64"
openclaw_release="${runtime_root}/releases/openclaw-${openclaw_version}"
legacy_node_release="${legacy_runtime_root}/releases/node-v${node_version}-linux-arm64"
legacy_openclaw_release="${legacy_runtime_root}/releases/openclaw-${openclaw_version}"

validate_node_release() {
  local release="$1"
  [[ -d "${release}" && ! -L "${release}" &&
     -x "${release}/bin/node" &&
     "$("${release}/bin/node" --version 2>/dev/null)" == "v${node_version}" ]]
}

validate_openclaw_release() {
  local node="$1" release="$2" package_json version cli_version
  package_json="${release}/lib/node_modules/openclaw/package.json"
  [[ -d "${release}" && ! -L "${release}" &&
     -x "${release}/bin/openclaw" &&
     -f "${package_json}" && ! -L "${package_json}" ]] || return 1
  version="$("${node}/bin/node" -e '
    const fs = require("node:fs");
    const data = JSON.parse(fs.readFileSync(process.argv[1], "utf8"));
    process.stdout.write(String(data.version || ""));
  ' "${package_json}" 2>/dev/null)" || return 1
  [[ "${version}" == "${openclaw_version}" ]] || return 1
  cli_version="$({ PATH="${node}/bin:/usr/bin:/bin" \
    "${release}/bin/openclaw" --version; } 2>/dev/null)" || return 1
  [[ "${cli_version}" == *"${openclaw_version}"* ]]
}

if [[ "${action}" == "verify" ]]; then
  echo "Verified pinned Node ${node_version} and OpenClaw ${openclaw_version} release lock."
  exit 0
fi

if [[ "${action}" == "verify-installed" ]]; then
  validate_node_release "${node_release}" || fail "pinned Node release is absent or invalid"
  validate_openclaw_release "${node_release}" "${openclaw_release}" ||
    fail "pinned OpenClaw release is absent or invalid"
  echo "Verified installed Node and OpenClaw releases."
  exit 0
fi

[[ "$(uname -m)" == "aarch64" ]] || fail "the pinned runtime is for Linux arm64"
for command_name in base64 curl openssl python3 sha256sum tar xz; do
  command -v "${command_name}" >/dev/null 2>&1 ||
    fail "missing required command ${command_name}"
done

if ((EUID == 0)); then
  elevate=()
else
  command -v sudo >/dev/null 2>&1 || fail "sudo is required for installation"
  elevate=(sudo)
fi

runtime_parent="$(dirname "${runtime_root}")"
if [[ -L "${runtime_parent}" ||
      ( -e "${runtime_parent}" && ! -d "${runtime_parent}" ) ]]; then
  fail "runtime parent must be a regular directory"
fi
if [[ ! -e "${runtime_root}" && -e "${legacy_runtime_root}" ]]; then
  [[ -d "${legacy_runtime_root}" && ! -L "${legacy_runtime_root}" ]] ||
    fail "legacy runtime root must be a regular directory"
  validate_node_release "${legacy_node_release}" ||
    fail "legacy pinned Node release is invalid"
  validate_openclaw_release "${legacy_node_release}" "${legacy_openclaw_release}" ||
    fail "legacy pinned OpenClaw release is invalid"

  "${elevate[@]}" install -d -o root -g root -m 0755 "${runtime_parent}"
  migration_stage="${runtime_root}.migration.$$"
  [[ ! -e "${migration_stage}" && ! -L "${migration_stage}" ]] ||
    fail "legacy runtime migration stage already exists"
  if ! "${elevate[@]}" cp -a --reflink=auto -- \
      "${legacy_runtime_root}" "${migration_stage}"; then
    "${elevate[@]}" rm -rf -- "${migration_stage}"
    fail "could not copy the legacy runtime"
  fi
  "${elevate[@]}" chown -R root:root "${migration_stage}"
  if ! validate_node_release \
      "${migration_stage}/releases/node-v${node_version}-linux-arm64" ||
     ! validate_openclaw_release \
      "${migration_stage}/releases/node-v${node_version}-linux-arm64" \
      "${migration_stage}/releases/openclaw-${openclaw_version}"; then
    "${elevate[@]}" rm -rf -- "${migration_stage}"
    fail "copied legacy runtime failed exact-version verification"
  fi
  if ! "${elevate[@]}" mv -T -- "${migration_stage}" "${runtime_root}"; then
    "${elevate[@]}" rm -rf -- "${migration_stage}"
    fail "could not activate the copied legacy runtime"
  fi
  echo "Copied and verified the pinned pre-rename runtime at the canonical path."
fi

if [[ -L "${runtime_root}" ]]; then
  fail "runtime root must not be a symlink"
fi
"${elevate[@]}" install -d -o root -g root -m 0755 \
  "${runtime_root}" "${runtime_root}/releases"

need_node=1
need_openclaw=1
if [[ -e "${node_release}" ]]; then
  validate_node_release "${node_release}" ||
    fail "existing pinned Node path is invalid; refusing to overwrite it"
  need_node=0
fi
if [[ -e "${openclaw_release}" ]]; then
  validate_openclaw_release "${node_release}" "${openclaw_release}" ||
    fail "existing pinned OpenClaw path is invalid; refusing to overwrite it"
  need_openclaw=0
fi

temporary_dir="$(mktemp -d)"
staged_paths=()
cleanup() {
  rm -rf -- "${temporary_dir}"
  local staged
  for staged in "${staged_paths[@]:-}"; do
    if [[ -n "${staged}" && "${staged}" == "${runtime_root}/releases/"*.staging.* ]]; then
      "${elevate[@]}" rm -rf -- "${staged}"
    fi
  done
}
trap cleanup EXIT

candidate_node="${temporary_dir}/node"
if ((need_node)); then
  archive_path="${temporary_dir}/${node_archive}"
  curl --fail --location --proto '=https' --tlsv1.2 \
    --output "${archive_path}" "${node_url}"
  printf '%s  %s\n' "${node_sha256}" "${archive_path}" | sha256sum --check --status ||
    fail "Node archive checksum mismatch"
  mkdir -m 0700 "${candidate_node}"
  (
    umask 022
    tar --extract --xz --file "${archive_path}" --directory "${candidate_node}" \
      --strip-components=1 --no-same-owner --same-permissions
  )
  validate_node_release "${candidate_node}" || fail "downloaded Node release failed verification"
else
  candidate_node="${node_release}"
fi

candidate_openclaw="${temporary_dir}/openclaw"
if ((need_openclaw)); then
  package_path="${temporary_dir}/openclaw-${openclaw_version}.tgz"
  curl --fail --location --proto '=https' --tlsv1.2 \
    --output "${package_path}" "${openclaw_tarball}"
  expected_sri="${openclaw_integrity#sha512-}"
  actual_sri="$(openssl dgst -sha512 -binary "${package_path}" | base64 | tr -d '\n')"
  [[ "${actual_sri}" == "${expected_sri}" ]] || fail "OpenClaw package integrity mismatch"
  mkdir -m 0700 "${candidate_openclaw}" "${temporary_dir}/npm-home" "${temporary_dir}/npm-cache"
  (
    umask 022
    PATH="${candidate_node}/bin:/usr/bin:/bin" \
      HOME="${temporary_dir}/npm-home" \
      npm_config_cache="${temporary_dir}/npm-cache" \
      npm_config_umask=0022 \
      "${candidate_node}/bin/npm" install --global \
        --prefix "${candidate_openclaw}" \
        --omit=dev --no-audit --no-fund --ignore-scripts=false \
        "${package_path}"
  )
  validate_openclaw_release "${candidate_node}" "${candidate_openclaw}" ||
    fail "installed OpenClaw package failed exact-version verification"
fi

install_release_atomically() {
  local source="$1" destination="$2" stage
  stage="${destination}.staging.$$"
  [[ ! -e "${stage}" && ! -L "${stage}" ]] || fail "staging path already exists"
  staged_paths+=("${stage}")
  "${elevate[@]}" install -d -o root -g root -m 0755 "${stage}"
  "${elevate[@]}" cp -a -- "${source}/." "${stage}/"
  "${elevate[@]}" chown -R root:root "${stage}"
  # `cp -a source/. stage/` preserves the private build root's 0700 mode on
  # the stage directory. Restore traversal for the unprivileged service user.
  "${elevate[@]}" chmod 0755 "${stage}"
  "${elevate[@]}" mv -T -- "${stage}" "${destination}"
  staged_paths=()
}

if ((need_node)); then
  install_release_atomically "${candidate_node}" "${node_release}"
fi
if ((need_openclaw)); then
  install_release_atomically "${candidate_openclaw}" "${openclaw_release}"
fi

atomic_symlink() {
  local destination="$1" link_path="$2" temporary_link
  temporary_link="${runtime_root}/.$(basename "${link_path}").$$"
  "${elevate[@]}" rm -f -- "${temporary_link}"
  "${elevate[@]}" ln -s -- "${destination}" "${temporary_link}"
  "${elevate[@]}" mv -Tf -- "${temporary_link}" "${link_path}"
}

validate_node_release "${node_release}" || fail "installed Node verification failed"
validate_openclaw_release "${node_release}" "${openclaw_release}" ||
  fail "installed OpenClaw verification failed"
atomic_symlink "${node_release}" "${runtime_root}/node-current"
atomic_symlink "${openclaw_release}" "${runtime_root}/openclaw-current"

echo "Installed and verified pinned Node ${node_version} and OpenClaw ${openclaw_version}."
