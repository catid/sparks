#!/usr/bin/env bash

set -euo pipefail
umask 077

voice_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
project_dir="$(cd "${voice_dir}/.." && pwd -P)"
action="${1:-verify}"
config_path="${OPENCLAW_CONFIG_PATH:-${HOME}/.openclaw/openclaw.json}"
plugin_path="${project_dir}/voice_assistant/openclaw/plugins/cerberus-alarms"
tools=(alarm_cancel alarm_dismiss alarm_set alarms_list timer_set)

case "${action}" in
  verify|install) ;;
  *) echo "Usage: configure-main-openclaw-alarms.sh [verify|install]" >&2; exit 2 ;;
esac

[[ -f "${config_path}" && ! -L "${config_path}" ]] || {
  echo "OpenClaw config must be a regular non-symlink file: ${config_path}" >&2
  exit 2
}
original_hash="$(sha256sum "${config_path}" | cut -d' ' -f1)"
for required in index.js openclaw.plugin.json package.json; do
  [[ -f "${plugin_path}/${required}" && ! -L "${plugin_path}/${required}" ]] || {
    echo "Missing alarm plugin file: ${plugin_path}/${required}" >&2
    exit 2
  }
done

temporary="$(mktemp "$(dirname "${config_path}")/.alarms-config.XXXXXX")"
cleanup() { rm -f -- "${temporary}"; }
trap cleanup EXIT

python3 - "${config_path}" "${temporary}" "${plugin_path}" "${tools[@]}" <<'PY'
import json
import os
import pathlib
import sys

source, destination, plugin_path, *tools = sys.argv[1:]
data = json.loads(pathlib.Path(source).read_text(encoding="utf-8"))
plugins = data.setdefault("plugins", {})
load = plugins.setdefault("load", {})
paths = load.setdefault("paths", [])
if plugin_path not in paths:
    paths.append(plugin_path)
allowed_plugins = plugins.setdefault("allow", [])
if "cerberus-alarms" not in allowed_plugins:
    allowed_plugins.append("cerberus-alarms")
plugins.setdefault("entries", {})["cerberus-alarms"] = {"enabled": True}
also_allow = data.setdefault("tools", {}).setdefault("alsoAllow", [])
for tool in tools:
    if tool not in also_allow:
        also_allow.append(tool)

target = pathlib.Path(destination)
target.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
os.chmod(target, 0o600)
PY

OPENCLAW_CONFIG_PATH="${temporary}" openclaw config validate >/dev/null

if [[ "${action}" == "verify" ]]; then
  echo "Verified the main OpenClaw alarm integration."
  exit 0
fi

case "$(hostname -s)" in
  cerberus3|spark3) ;;
  *) echo "Alarm integration install is allowed only on cerberus3 (legacy spark3)." >&2; exit 2 ;;
esac

backup_dir="$(dirname "${config_path}")/backups"
install -d -m 0700 "${backup_dir}"
backup_path="${backup_dir}/openclaw-before-alarms-$(date -u +%Y%m%dT%H%M%SZ).json"
install -m 0600 "${config_path}" "${backup_path}"
[[ "$(sha256sum "${config_path}" | cut -d' ' -f1)" == "${original_hash}" ]] || {
  echo "OpenClaw config changed while alarm integration was being prepared." >&2
  exit 2
}
install -m 0600 "${temporary}" "${config_path}"
if ! openclaw config validate >/dev/null ||
   ! systemctl --user restart openclaw-gateway.service ||
   ! systemctl --user is-active --quiet openclaw-gateway.service; then
  install -m 0600 "${backup_path}" "${config_path}"
  systemctl --user restart openclaw-gateway.service || true
  echo "Alarm integration failed; restored the previous OpenClaw config." >&2
  exit 1
fi
echo "Installed the alarm tools in the main OpenClaw gateway."
