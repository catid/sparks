#!/usr/bin/env bash

set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
action="${1:-verify}"
openclaw_bin="${OPENCLAW_BIN:-$(command -v openclaw || true)}"
python_bin="${PYTHON_BIN:-$(command -v python3 || true)}"
launchd_label="${OPENCLAW_LAUNCHD_LABEL:-ai.openclaw.gateway.headless}"
ops_dir="${OPENCLAW_OPS_DIR:-${HOME}/.openclaw/ops/slack-heartbeat}"

if [[ -z "${openclaw_bin}" && -x /opt/homebrew/bin/openclaw ]]; then
  openclaw_bin="/opt/homebrew/bin/openclaw"
fi
if [[ -z "${python_bin}" && -x /usr/bin/python3 ]]; then
  python_bin="/usr/bin/python3"
fi

usage() {
  cat <<'EOF'
Usage: install-slack-heartbeat.sh [verify|install]

Verify or install the guarded five-second Slack thinking heartbeat for the
qualified @openclaw/slack 2026.7.1 adapter.

`install` patches the active generated plugin copy and, when OpenClaw still
retains it, the npm base project. It then selects Slack progress-preview mode.
It does not restart the gateway. Restart the headless LaunchDaemon after
installation:

  sudo launchctl kickstart -k system/ai.openclaw.gateway.headless

Optional environment overrides:
  OPENCLAW_BIN             absolute OpenClaw executable path
  PYTHON_BIN               Python 3 executable
  OPENCLAW_LAUNCHD_LABEL   default: ai.openclaw.gateway.headless
  OPENCLAW_OPS_DIR         durable verifier copy under private OpenClaw state
EOF
}

case "${action}" in
  verify|install) ;;
  -h|--help)
    usage
    exit 0
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac

[[ "$(uname -s)" == "Darwin" ]] || {
  echo "This deployment helper is qualified only on macOS." >&2
  exit 2
}
[[ -n "${openclaw_bin}" && -x "${openclaw_bin}" ]] || {
  echo "OpenClaw executable is missing." >&2
  exit 2
}
[[ -n "${python_bin}" && -x "${python_bin}" ]] || {
  echo "Python 3 executable is missing." >&2
  exit 2
}
[[ "${launchd_label}" =~ ^[A-Za-z0-9][A-Za-z0-9._-]+$ ]] || {
  echo "Unsafe launchd label: ${launchd_label}" >&2
  exit 2
}

plugin_dirs="$(
  "${openclaw_bin}" plugins inspect slack --runtime --json |
    "${python_bin}" -c '
import json
import pathlib
import sys

data = json.load(sys.stdin)
active = pathlib.Path(data["install"]["installPath"]).resolve()
parts = list(active.parts)
project_index = next(
    index
    for index, part in enumerate(parts)
    if part.startswith("openclaw-slack-")
)
base_project = parts[project_index].split("__openclaw-generation__", 1)[0]
base = pathlib.Path(*parts[:project_index], base_project, *parts[project_index + 1 :])
if base != active and base.is_dir():
    print(base)
print(active)
'
)"

patch_args=()
while IFS= read -r plugin_dir; do
  [[ -n "${plugin_dir}" ]] || continue
  patch_args+=(--plugin-dir "${plugin_dir}")
done <<<"${plugin_dirs}"

if [[ "${#patch_args[@]}" -lt 1 ]]; then
  echo "Could not resolve the installed Slack plugin." >&2
  exit 2
fi

if [[ "${action}" == "verify" ]]; then
  "${python_bin}" "${script_dir}/patch-slack-heartbeat.py" "${patch_args[@]}"
  exit 0
fi

streaming_config='{"mode":"progress","nativeTransport":false,"progress":{"label":false,"toolProgress":false,"commentary":false,"commandText":"status"}}'
"${openclaw_bin}" config set channels.slack.streaming \
  "${streaming_config}" --strict-json --dry-run

"${python_bin}" "${script_dir}/patch-slack-heartbeat.py" \
  --apply "${patch_args[@]}"

"${openclaw_bin}" config set channels.slack.streaming \
  "${streaming_config}" --strict-json
"${openclaw_bin}" config validate

if [[ -L "${ops_dir}" ]]; then
  echo "Refusing a symlinked OpenClaw ops directory: ${ops_dir}" >&2
  exit 2
fi
if [[ "${script_dir}" != "${ops_dir}" ]]; then
  /usr/bin/install -d -m 0700 "${ops_dir}"
  /usr/bin/install -m 0755 \
    "${script_dir}/install-slack-heartbeat.sh" \
    "${script_dir}/patch-slack-heartbeat.py" \
    "${ops_dir}/"
  /usr/bin/install -m 0644 \
    "${script_dir}/test_patch_slack_heartbeat.py" \
    "${ops_dir}/"
fi

echo "Installed Slack heartbeat files and configuration."
echo "Restart with: sudo launchctl kickstart -k system/${launchd_label}"
