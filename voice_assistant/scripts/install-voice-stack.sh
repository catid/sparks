#!/usr/bin/env bash

set -euo pipefail
umask 077

voice_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
project_dir="$(cd "${voice_dir}/.." && pwd -P)"
action="${1:-verify}"
if (($#)); then
  shift
fi

service_user="${SPARK_SERVICE_USER:-${SUDO_USER:-${USER:-$(id -un)}}}"
runtime_root="${VOICE_OPENCLAW_RUNTIME_ROOT:-/opt/cerberus/openclaw-runtime}"
legacy_runtime_root="${VOICE_OPENCLAW_LEGACY_RUNTIME_ROOT:-/opt/cerebrus/openclaw-runtime}"
secret_file="${VOICE_STACK_SECRET_FILE:-/etc/cerberus3-voice/gateway.env}"
legacy_secret_file="${VOICE_STACK_LEGACY_SECRET_FILE:-/etc/cerebrus3-voice/gateway.env}"
asr_env_file="${VOICE_STACK_ASR_ENV_FILE:-/etc/default/cerberus3-qwen3-asr}"
legacy_asr_env_file="${VOICE_STACK_LEGACY_ASR_ENV_FILE:-/etc/default/cerebrus3-qwen3-asr}"
bridge_env_file="${VOICE_STACK_BRIDGE_ENV_FILE:-/etc/default/cerberus3-voice-bridge}"
legacy_bridge_env_file="${VOICE_STACK_LEGACY_BRIDGE_ENV_FILE:-/etc/default/cerebrus3-voice-bridge}"
replace_config=0
replace_workspace=0

usage() {
  cat <<'EOF'
Usage: install-voice-stack.sh [verify|prepare|install|enable|start] [options]

verify   Statically check all inputs and rendered units; change nothing.
prepare  Install pinned Node/OpenClaw, download pinned ASR, and build its image.
install  Install config and systemd units without enabling or starting them.
enable   Install and enable the complete target for future boots.
start    Install, enable, and restart the voice stack now.

Options:
  --replace-config     Replace the existing private OpenClaw config.
  --replace-workspace  Replace the existing voice workspace AGENTS.md.

Environment:
  SPARK_SERVICE_USER          Runtime account (default: invoking user)
  VOICE_OPENCLAW_RUNTIME_ROOT Pinned runtime root
  VOICE_OPENCLAW_LEGACY_RUNTIME_ROOT  Pre-rename runtime copied when needed
  VOICE_STACK_SECRET_FILE     Root-owned token dotenv path
  VOICE_STACK_LEGACY_SECRET_FILE  Pre-rename token dotenv copied when needed
  VOICE_STACK_ASR_ENV_FILE    Optional root-owned ASR override path
  VOICE_STACK_LEGACY_ASR_ENV_FILE  Pre-rename ASR override copied when needed
  VOICE_STACK_BRIDGE_ENV_FILE Optional root-owned bridge override path
  VOICE_STACK_LEGACY_BRIDGE_ENV_FILE  Pre-rename bridge override copied when needed

The gateway bearer token is generated once, stored root-owned with mode 0600,
shared with the local voice bridge through systemd, and never printed.
EOF
}

case "${action}" in
  verify|prepare|install|enable|start) ;;
  -h|--help) usage; exit 0 ;;
  *) usage >&2; exit 2 ;;
esac
while (($#)); do
  case "$1" in
    --replace-config) replace_config=1 ;;
    --replace-workspace) replace_workspace=1 ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
  shift
done

fail() {
  echo "Cerberus voice installer: $*" >&2
  exit 2
}

safe_absolute_path() {
  [[ "$1" =~ ^/[A-Za-z0-9._/@+-]+$ && "$1" != "/" &&
     "$1" != *"/../"* && "$1" != */.. &&
     "$1" != *"/./"* && "$1" != */. ]]
}

[[ "${service_user}" =~ ^[a-z_][a-z0-9_-]*[$]?$ ]] || fail "unsafe service user"
service_uid="$(id -u "${service_user}")"
service_gid="$(id -g "${service_user}")"
service_group="$(id -gn "${service_user}")"
service_home="$(getent passwd "${service_user}" | cut -d: -f6)"
[[ "${service_uid}" != "0" ]] || fail "services must not run as root"
[[ "${service_group}" =~ ^[a-z_][a-z0-9_-]*[$]?$ ]] || fail "unsafe service group"
for path_value in \
  "${project_dir}" "${service_home}" "${runtime_root}" "${legacy_runtime_root}" \
  "${secret_file}" "${legacy_secret_file}" \
  "${asr_env_file}" "${legacy_asr_env_file}" \
  "${bridge_env_file}" "${legacy_bridge_env_file}"; do
  safe_absolute_path "${path_value}" || fail "unsafe absolute path"
done

state_dir="${service_home}/.local/state/cerberus-voice/openclaw"
workspace="${service_home}/.local/share/cerberus-voice/workspace"
cache_dir="${service_home}/.cache/cerberus-voice/openclaw"
asr_cache_dir="${service_home}/.cache/cerberus-voice/qwen3-asr"
legacy_state_dir="${service_home}/.local/state/cerebrus-voice/openclaw"
legacy_workspace="${service_home}/.local/share/cerebrus-voice/workspace"
legacy_cache_dir="${service_home}/.cache/cerebrus-voice/openclaw"
legacy_asr_cache_dir="${service_home}/.cache/cerebrus-voice/qwen3-asr"
config_path="${state_dir}/openclaw.json"
node_dir="${runtime_root}/releases/node-v24.15.0-linux-arm64"
openclaw_release="${runtime_root}/releases/openclaw-2026.7.1-2"
mapfile -t pinned_openclaw_plugins < <(python3 - \
  "${voice_dir}/openclaw/runtime.lock.json" <<'PY'
import json
import pathlib
import sys

lock = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
for plugin_id in sorted(lock["plugins"]):
    plugin = lock["plugins"][plugin_id]
    print(f'{plugin["package"]}@{plugin["version"]}')
PY
)
[[ "${#pinned_openclaw_plugins[@]}" == "2" ]] ||
  fail "expected exactly two pinned OpenClaw plugins"

unit_names=(
  cerberus3-openclaw-voice.service
  cerberus3-qwen3-asr.service
  cerberus3-voice-bridge.service
  cerberus3-voice-stack.target
)
required_files=(
  "${voice_dir}/asr_server.py"
  "${voice_dir}/voice_bridge.py"
  "${voice_dir}/run-asr.sh"
  "${voice_dir}/download-model.sh"
  "${voice_dir}/Dockerfile"
  "${voice_dir}/openclaw/runtime.lock.json"
  "${voice_dir}/openclaw/openclaw.json.in"
  "${voice_dir}/openclaw/AGENTS.md"
  "${voice_dir}/openclaw/gateway-wrapper.sh"
  "${voice_dir}/openclaw/plugins/cerberus-health/index.js"
  "${voice_dir}/openclaw/plugins/cerberus-health/openclaw.plugin.json"
  "${voice_dir}/openclaw/plugins/cerberus-health/package.json"
  "${voice_dir}/scripts/install-openclaw-runtime.sh"
  "${voice_dir}/scripts/migrate-legacy-state.py"
  "${voice_dir}/tests/fixtures/fake-node"
  "${voice_dir}/tests/fixtures/fake-openclaw"
  "${voice_dir}/tests/fixtures/fake-openclaw-package.json"
  "${voice_dir}/tests/fixtures/cerberus3-audio8.service"
)
for unit_name in "${unit_names[@]}"; do
  required_files+=("${voice_dir}/systemd/${unit_name}.in")
done
for required_file in "${required_files[@]}"; do
  [[ -f "${required_file}" && ! -L "${required_file}" ]] ||
    fail "missing regular input ${required_file}"
done
for executable_file in \
  "${voice_dir}/run-asr.sh" \
  "${voice_dir}/download-model.sh" \
  "${voice_dir}/openclaw/gateway-wrapper.sh" \
  "${voice_dir}/scripts/install-openclaw-runtime.sh" \
  "${voice_dir}/scripts/install-voice-stack.sh"; do
  [[ -x "${executable_file}" ]] || fail "input is not executable: ${executable_file}"
done

bash -n \
  "${voice_dir}/run-asr.sh" \
  "${voice_dir}/download-model.sh" \
  "${voice_dir}/openclaw/gateway-wrapper.sh" \
  "${voice_dir}/scripts/install-openclaw-runtime.sh" \
  "${voice_dir}/scripts/install-voice-stack.sh"
python3 -m json.tool "${voice_dir}/openclaw/runtime.lock.json" >/dev/null
python3 - \
  "${voice_dir}/asr_server.py" \
  "${voice_dir}/voice_bridge.py" \
  "${voice_dir}/scripts/migrate-legacy-state.py" <<'PY'
import pathlib
import sys

for name in sys.argv[1:]:
    source = pathlib.Path(name).read_bytes()
    compile(source, name, "exec")
PY
"${voice_dir}/scripts/install-openclaw-runtime.sh" verify >/dev/null

temporary_dir="$(mktemp -d)"
trap 'rm -rf -- "${temporary_dir}"' EXIT
rendered_config="${temporary_dir}/openclaw.json"

render_node_dir="${node_dir}"
render_openclaw_release="${openclaw_release}"
if [[ "${action}" == "verify" || "${action}" == "prepare" ]]; then
  render_node_dir="${temporary_dir}/pinned-node"
  render_openclaw_release="${temporary_dir}/pinned-openclaw"
  install -d -m 0755 \
    "${render_node_dir}/bin" "${render_openclaw_release}/bin" \
    "${render_openclaw_release}/lib/node_modules/openclaw"
  install -m 0755 "${voice_dir}/tests/fixtures/fake-node" \
    "${render_node_dir}/bin/node"
  install -m 0755 "${voice_dir}/tests/fixtures/fake-openclaw" \
    "${render_openclaw_release}/bin/openclaw"
  install -m 0644 "${voice_dir}/tests/fixtures/fake-openclaw-package.json" \
    "${render_openclaw_release}/lib/node_modules/openclaw/package.json"
  install -m 0644 "${voice_dir}/tests/fixtures/cerberus3-audio8.service" \
    "${temporary_dir}/cerberus3-audio8.service"
fi

python3 - \
  "${voice_dir}/openclaw/openclaw.json.in" \
  "${rendered_config}" "${workspace}" "${project_dir}" <<'PY'
import json
import pathlib
import sys

source = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")
rendered = source.replace("@WORKSPACE@", sys.argv[3]).replace("@PROJECT_DIR@", sys.argv[4])
if "@WORKSPACE@" in rendered or "@PROJECT_DIR@" in rendered:
    raise SystemExit("unresolved OpenClaw config placeholder")
data = json.loads(rendered)
pathlib.Path(sys.argv[2]).write_text(
    json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
)
PY
chmod 0600 "${rendered_config}"

declare -A replacements=(
  [PROJECT_DIR]="${project_dir}"
  [USER]="${service_user}"
  [GROUP]="${service_group}"
  [HOME]="${service_home}"
  [STATE_DIR]="${state_dir}"
  [CONFIG_PATH]="${config_path}"
  [WORKSPACE]="${workspace}"
  [CACHE_DIR]="${cache_dir}"
  [ASR_CACHE_DIR]="${asr_cache_dir}"
  [NODE_DIR]="${render_node_dir}"
  [OPENCLAW_RELEASE]="${render_openclaw_release}"
  [SECRET_ENV]="${secret_file}"
)
for unit_name in "${unit_names[@]}"; do
  rendered_unit="${temporary_dir}/${unit_name}"
  cp -- "${voice_dir}/systemd/${unit_name}.in" "${rendered_unit}"
  for placeholder in "${!replacements[@]}"; do
    value="${replacements[${placeholder}]}"
    python3 - "${rendered_unit}" "@${placeholder}@" "${value}" <<'PY'
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
text = path.read_text(encoding="utf-8").replace(sys.argv[2], sys.argv[3])
path.write_text(text, encoding="utf-8")
PY
  done
  if rg -q '@[A-Z][A-Z0-9_]+@' "${rendered_unit}"; then
    fail "unresolved placeholder in ${unit_name}"
  fi
done

SYSTEMD_UNIT_PATH="${temporary_dir}:/usr/local/lib/systemd/system:/usr/lib/systemd/system:/lib/systemd/system" \
  systemd-analyze verify \
    "${temporary_dir}/cerberus3-openclaw-voice.service" \
    "${temporary_dir}/cerberus3-qwen3-asr.service" \
    "${temporary_dir}/cerberus3-voice-bridge.service" \
    "${temporary_dir}/cerberus3-voice-stack.target"

if [[ "${action}" == "verify" ]]; then
  echo "Verified the Cerberus voice config, wrappers, Python sources, and systemd units."
  exit 0
fi

case "$(hostname -s)" in
  cerberus3|spark3) ;;
  *) fail "installation is allowed only on cerberus3 (legacy spark3)" ;;
esac

if ((EUID == 0)); then
  elevate=()
else
  command -v sudo >/dev/null 2>&1 || fail "sudo is required for installation"
  elevate=(sudo)
fi

run_as_service_user() {
  if [[ "$(id -u)" == "${service_uid}" ]]; then
    HOME="${service_home}" "$@"
  elif ((EUID == 0)); then
    runuser -u "${service_user}" -- env HOME="${service_home}" "$@"
  else
    fail "cannot run preparation as ${service_user}"
  fi
}

if [[ "${action}" == "prepare" ]]; then
  VOICE_OPENCLAW_RUNTIME_ROOT="${runtime_root}" \
  VOICE_OPENCLAW_LEGACY_RUNTIME_ROOT="${legacy_runtime_root}" \
    "${voice_dir}/scripts/install-openclaw-runtime.sh" install
  run_as_service_user "${voice_dir}/download-model.sh"
  run_as_service_user docker build \
    --pull=false \
    --tag cerberus/qwen3-asr:1.7b-bcd2b5b7 \
    --file "${voice_dir}/Dockerfile" \
    "${voice_dir}"
  echo "Prepared pinned OpenClaw and Qwen3 ASR runtimes."
  exit 0
fi

systemctl_command="$(command -v systemctl || true)"
[[ -x "${systemctl_command}" ]] || fail "systemctl is required for installation"
"${systemctl_command}" cat cerberus3-audio8.service >/dev/null 2>&1 ||
  fail "cerberus3-audio8.service is a prerequisite; run scripts/install-audio8.sh start first"

VOICE_OPENCLAW_RUNTIME_ROOT="${runtime_root}" \
  "${voice_dir}/scripts/install-openclaw-runtime.sh" verify-installed >/dev/null ||
  fail "pinned OpenClaw runtime is missing; run prepare first"
docker image inspect cerberus/qwen3-asr:1.7b-bcd2b5b7 >/dev/null 2>&1 ||
  fail "pinned Qwen3 ASR image is missing; run prepare first"

legacy_stop_order=(
  cerebrus3-voice-bridge.service
  cerebrus3-voice-stack.target
  cerebrus3-openclaw-voice.service
  cerebrus3-qwen3-asr.service
  cerebrus3-audio8.service
)
legacy_unit_names=(
  cerebrus3-openclaw-voice.service
  cerebrus3-qwen3-asr.service
  cerebrus3-voice-bridge.service
  cerebrus3-voice-stack.target
  cerebrus3-audio8.service
)

for legacy_unit in "${legacy_stop_order[@]}"; do
  if "${systemctl_command}" cat "${legacy_unit}" >/dev/null 2>&1; then
    "${elevate[@]}" systemctl stop "${legacy_unit}"
  fi
done
for legacy_unit in "${legacy_unit_names[@]}"; do
  if "${systemctl_command}" cat "${legacy_unit}" >/dev/null 2>&1; then
    "${elevate[@]}" systemctl disable "${legacy_unit}"
  fi
  if "${systemctl_command}" is-active --quiet "${legacy_unit}"; then
    fail "legacy unit remained active after stop: ${legacy_unit}"
  fi
done
run_as_service_user docker rm -f \
  cerebrus3-qwen-asr cerebrus3-audio8 >/dev/null 2>&1 || true
if run_as_service_user docker ps --format '{{.Names}}' | \
    grep -Exq 'cerebrus3-(qwen-asr|audio8)'; then
  fail "a legacy voice container remained active after stop"
fi

"${elevate[@]}" python3 "${voice_dir}/scripts/migrate-legacy-state.py" \
  --legacy-secret "${legacy_secret_file}" \
  --secret "${secret_file}" \
  --legacy-asr-env "${legacy_asr_env_file}" \
  --asr-env "${asr_env_file}" \
  --legacy-bridge-env "${legacy_bridge_env_file}" \
  --bridge-env "${bridge_env_file}" \
  --legacy-state "${legacy_state_dir}" \
  --state "${state_dir}" \
  --legacy-workspace "${legacy_workspace}" \
  --workspace "${workspace}" \
  --legacy-cache "${legacy_cache_dir}" \
  --cache "${cache_dir}" \
  --legacy-asr-cache "${legacy_asr_cache_dir}" \
  --asr-cache "${asr_cache_dir}" \
  --service-uid "${service_uid}" \
  --service-gid "${service_gid}"

"${elevate[@]}" install -d -o "${service_user}" -g "${service_group}" -m 0700 \
  "${state_dir}" "${workspace}" "${cache_dir}" "${asr_cache_dir}"

secret_dir="$(dirname "${secret_file}")"
"${elevate[@]}" python3 - "${secret_dir}" "${secret_file}" <<'PY'
import os
import pathlib
import secrets
import stat
import sys

directory = pathlib.Path(sys.argv[1])
path = pathlib.Path(sys.argv[2])
if directory.is_symlink() or path.is_symlink():
    raise SystemExit("secret directory and file must not be symlinks")
directory.mkdir(mode=0o700, parents=True, exist_ok=True)
os.chown(directory, 0, 0)
os.chmod(directory, 0o700)
if not path.exists():
    token = secrets.token_hex(32)
    slack_signing_secret = secrets.token_hex(32)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(path, flags, 0o600)
    try:
        os.write(fd, (
            f"OPENCLAW_GATEWAY_TOKEN={token}\n"
            f"VOICE_OPENCLAW_TOKEN={token}\n"
            f"SLACK_SIGNING_SECRET={slack_signing_secret}\n"
        ).encode("ascii"))
        os.fsync(fd)
    finally:
        os.close(fd)
info = path.lstat()
if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_gid != 0:
    raise SystemExit("secret dotenv must be a root-owned regular file")
if stat.S_IMODE(info.st_mode) != 0o600:
    raise SystemExit("secret dotenv must have mode 0600")

values = {}
for line in path.read_text(encoding="ascii").splitlines():
    if not line or line.startswith("#"):
        continue
    if "=" not in line:
        raise SystemExit("invalid secret dotenv line")
    key, value = line.split("=", 1)
    if key in values:
        raise SystemExit("duplicate secret dotenv key")
    values[key] = value

if "SLACK_SIGNING_SECRET" not in values:
    signing_secret = secrets.token_hex(32)
    with path.open("a", encoding="ascii") as stream:
        stream.write(f"SLACK_SIGNING_SECRET={signing_secret}\n")
        stream.flush()
        os.fsync(stream.fileno())
    values["SLACK_SIGNING_SECRET"] = signing_secret

gateway = values.get("OPENCLAW_GATEWAY_TOKEN", "")
bridge = values.get("VOICE_OPENCLAW_TOKEN", "")
if gateway != bridge or len(gateway) < 48 or not gateway.isalnum():
    raise SystemExit("gateway and bridge tokens must be the same strong value")
signing_secret = values.get("SLACK_SIGNING_SECRET", "")
if len(signing_secret) < 48 or not signing_secret.isalnum():
    raise SystemExit("Slack signing secret must be a strong alphanumeric value")
slack_bot_token = values.get("SLACK_BOT_TOKEN", "")
if len(slack_bot_token) < 20 or any(char.isspace() for char in slack_bot_token):
    raise SystemExit("SLACK_BOT_TOKEN must be provisioned in the secret dotenv")
exa_api_key = values.get("EXA_API_KEY", "")
if len(exa_api_key) < 16 or any(char.isspace() for char in exa_api_key):
    raise SystemExit("EXA_API_KEY must be provisioned in the secret dotenv")
PY

config_to_validate="${rendered_config}"
if "${elevate[@]}" test -e "${config_path}" && ((replace_config == 0)); then
  "${elevate[@]}" test -f "${config_path}" &&
    ! "${elevate[@]}" test -L "${config_path}" ||
    fail "existing config must be a regular non-symlink file"
  config_to_validate="${config_path}"
  echo "Preserving existing private OpenClaw config."
fi

validation_state="${temporary_dir}/validation-state"
validation_home="${temporary_dir}/validation-home"
mkdir -m 0700 "${validation_state}" "${validation_home}"
validation_token="validation-${RANDOM}-${RANDOM}-${RANDOM}"
OPENCLAW_GATEWAY_TOKEN="${validation_token}" \
OPENCLAW_CONFIG_PATH="${config_to_validate}" \
OPENCLAW_STATE_DIR="${validation_state}" \
OPENCLAW_WORKSPACE_DIR="${workspace}" \
HOME="${validation_home}" \
PATH="${node_dir}/bin:${openclaw_release}/bin:/usr/bin:/bin" \
  "${openclaw_release}/bin/openclaw" config validate >/dev/null

if [[ "${config_to_validate}" == "${rendered_config}" ]]; then
  "${elevate[@]}" install -o "${service_user}" -g "${service_group}" -m 0600 \
    "${rendered_config}" "${config_path}"
fi

for plugin_spec in "${pinned_openclaw_plugins[@]}"; do
  run_as_service_user env \
    OPENCLAW_STATE_DIR="${state_dir}" \
    OPENCLAW_CONFIG_PATH="${config_path}" \
    OPENCLAW_WORKSPACE_DIR="${workspace}" \
    XDG_CACHE_HOME="${cache_dir}" \
    PATH="${node_dir}/bin:${openclaw_release}/bin:/usr/bin:/bin" \
    "${openclaw_release}/bin/openclaw" plugins install --force "${plugin_spec}"
done

agents_target="${workspace}/AGENTS.md"
if "${elevate[@]}" test -e "${agents_target}" && ((replace_workspace == 0)); then
  "${elevate[@]}" test -f "${agents_target}" &&
    ! "${elevate[@]}" test -L "${agents_target}" ||
    fail "existing workspace AGENTS.md must be a regular non-symlink file"
  echo "Preserving existing voice workspace AGENTS.md."
else
  "${elevate[@]}" install -o "${service_user}" -g "${service_group}" -m 0644 \
    "${voice_dir}/openclaw/AGENTS.md" "${agents_target}"
fi

for unit_name in "${unit_names[@]}"; do
  "${elevate[@]}" install -o root -g root -m 0644 \
    "${temporary_dir}/${unit_name}" "/etc/systemd/system/${unit_name}"
done
for legacy_unit in "${legacy_unit_names[@]}"; do
  "${elevate[@]}" rm -f -- "/etc/systemd/system/${legacy_unit}"
done
"${elevate[@]}" systemctl daemon-reload
for legacy_unit in "${legacy_unit_names[@]}"; do
  "${elevate[@]}" systemctl reset-failed "${legacy_unit}" >/dev/null 2>&1 || true
done

if [[ "${action}" == "enable" || "${action}" == "start" ]]; then
  "${elevate[@]}" systemctl enable cerberus3-voice-stack.target
fi
if [[ "${action}" == "start" ]]; then
  "${elevate[@]}" systemctl restart cerberus3-openclaw-voice.service
  "${elevate[@]}" systemctl restart cerberus3-qwen3-asr.service
  "${elevate[@]}" systemctl start cerberus3-audio8.service
  "${elevate[@]}" systemctl restart cerberus3-voice-bridge.service
  "${elevate[@]}" systemctl start cerberus3-voice-stack.target
fi

echo "Installed Cerberus voice services for unprivileged user ${service_user}."
if [[ "${action}" == "enable" || "${action}" == "start" ]]; then
  echo "The complete voice stack is enabled through multi-user.target."
fi
