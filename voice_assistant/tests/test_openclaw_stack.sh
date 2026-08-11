#!/usr/bin/env bash

set -euo pipefail

voice_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
temporary_dir="$(mktemp -d)"
trap 'rm -rf -- "${temporary_dir}"' EXIT

rendered_config="${temporary_dir}/openclaw.json"
sed 's|@WORKSPACE@|/home/test/.local/share/cerberus-voice/workspace|g' \
  "${voice_dir}/openclaw/openclaw.json.in" >"${rendered_config}"
python3 - "${rendered_config}" "${voice_dir}/openclaw/runtime.lock.json" <<'PY'
import json
import pathlib
import sys

config = json.loads(pathlib.Path(sys.argv[1]).read_text())
lock = json.loads(pathlib.Path(sys.argv[2]).read_text())

assert lock["node"]["version"] == "24.15.0"
assert lock["node"]["architecture"] == "arm64"
assert lock["node"]["sha256"] == "f3d5a797b5d210ce8e2cb265544c8e482eaedcb8aa409a8b46da7e8595d0dda0"
assert lock["openclaw"]["version"] == "2026.7.1-2"
assert lock["openclaw"]["integrity"].startswith("sha512-")

gateway = config["gateway"]
assert gateway["mode"] == "local"
assert gateway["bind"] == "loopback"
assert gateway["port"] == 18789
assert gateway["auth"]["mode"] == "token"
assert gateway["auth"]["token"] == {
    "source": "env", "provider": "default", "id": "OPENCLAW_GATEWAY_TOKEN"
}
assert gateway["http"]["endpoints"]["chatCompletions"]["enabled"] is True
assert gateway["http"]["endpoints"]["responses"]["enabled"] is False

agents = config["agents"]
assert agents["defaults"]["thinkingDefault"] == "xhigh"
assert agents["defaults"]["maxConcurrent"] == 1
assert len(agents["list"]) == 1
voice = agents["list"][0]
assert voice["id"] == "voice" and voice["default"] is True
assert voice["thinkingDefault"] == "xhigh"
assert voice["tools"]["profile"] == "minimal"
assert voice["skills"] == []
assert config["tools"]["profile"] == "minimal"

provider = config["models"]["providers"]["vllm"]
assert provider["baseUrl"] == "http://cerberus1.local:8889/v1"
assert provider["api"] == "openai-completions"
model = provider["models"][0]
assert model["id"] == "deepseek-v4-flash"
assert model["contextWindow"] == 1048576
assert model["compat"]["reasoningEffortMap"]["xhigh"] == "max"
PY

node_dir="${temporary_dir}/node"
release_dir="${temporary_dir}/openclaw"
mkdir -p \
  "${node_dir}/bin" "${release_dir}/bin" \
  "${release_dir}/lib/node_modules/openclaw"
install -m 0755 "${voice_dir}/tests/fixtures/fake-node" "${node_dir}/bin/node"
install -m 0755 "${voice_dir}/tests/fixtures/fake-openclaw" \
  "${release_dir}/bin/openclaw"
install -m 0644 "${voice_dir}/tests/fixtures/fake-openclaw-package.json" \
  "${release_dir}/lib/node_modules/openclaw/package.json"

marker="${temporary_dir}/gateway.args"
test_token="private-test-${RANDOM}-${RANDOM}-${RANDOM}"
output="$({
  OPENCLAW_NODE_DIR="${node_dir}" \
  OPENCLAW_RELEASE_DIR="${release_dir}" \
  OPENCLAW_GATEWAY_TOKEN="${test_token}" \
  OPENCLAW_TEST_MARKER="${marker}" \
    "${voice_dir}/openclaw/gateway-wrapper.sh"
} 2>&1)"
[[ "${output}" != *"${test_token}"* ]]
[[ "$(<"${marker}")" == "gateway run" ]]

if OPENCLAW_NODE_DIR="${node_dir}" \
   OPENCLAW_RELEASE_DIR="${release_dir}" \
   OPENCLAW_NODE_VERSION=24.15.1 \
   OPENCLAW_GATEWAY_TOKEN=test \
   OPENCLAW_TEST_MARKER="${marker}" \
   "${voice_dir}/openclaw/gateway-wrapper.sh" >/dev/null 2>&1; then
  echo "wrapper accepted a mismatched Node version" >&2
  exit 1
fi

bridge_unit="${voice_dir}/systemd/cerberus3-voice-bridge.service.in"
rg -q '^Environment=VOICE_OPENCLAW_MODEL=openclaw/voice$' "${bridge_unit}"
rg -q '^Environment=VOICE_ASR_URL=http://127\.0\.0\.1:8020/transcribe$' "${bridge_unit}"
rg -q '^Environment=VOICE_TTS_URL=http://127\.0\.0\.1:8010/v1/audio/speech$' "${bridge_unit}"
rg -q '^SupplementaryGroups=audio$' "${bridge_unit}"
rg -q '^DeviceAllow=char-alsa rw$' "${bridge_unit}"
rg -q '^Environment=VOICE_STATE_DIR=%t/cerberus3-voice-bridge$' "${bridge_unit}"
rg -q '^RuntimeDirectory=cerberus3-voice-bridge$' "${bridge_unit}"
rg -q '^RuntimeDirectoryMode=0700$' "${bridge_unit}"
rg -q '^Conflicts=cerebrus3-voice-bridge\.service$' "${bridge_unit}"
rg -q '^Conflicts=cerebrus3-openclaw-voice\.service$' \
  "${voice_dir}/systemd/cerberus3-openclaw-voice.service.in"
rg -q '^Conflicts=cerebrus3-qwen3-asr\.service$' \
  "${voice_dir}/systemd/cerberus3-qwen3-asr.service.in"
rg -q '^Conflicts=cerebrus3-voice-stack\.target$' \
  "${voice_dir}/systemd/cerberus3-voice-stack.target.in"
rg -Fq 'if [[ "${action}" == "verify" || "${action}" == "prepare" ]]; then' \
  "${voice_dir}/scripts/install-voice-stack.sh"
rg -Fq -- '--tag cerberus/qwen3-asr:1.7b-bcd2b5b7' \
  "${voice_dir}/scripts/install-voice-stack.sh"
if rg -q 'cerebrus/qwen3-asr' "${voice_dir}/scripts/install-voice-stack.sh"; then
  echo 'Voice installer still launches the misspelled image namespace.' >&2
  exit 1
fi
for override in \
  '/etc/default/cerebrus3-qwen3-asr' \
  '/etc/default/cerberus3-qwen3-asr' \
  '/etc/default/cerebrus3-voice-bridge' \
  '/etc/default/cerberus3-voice-bridge'; do
  rg -Fq "${override}" "${voice_dir}/scripts/install-voice-stack.sh"
done
rg -Fq 'systemctl reset-failed "${legacy_unit}"' \
  "${voice_dir}/scripts/install-voice-stack.sh"
if rg -q '^StateDirectory=' "${bridge_unit}"; then
  echo "ephemeral voice status must not use a persistent StateDirectory" >&2
  exit 1
fi
rg -q '^InaccessiblePaths=.*docker\.sock.*@HOME@/\.ssh' "${bridge_unit}"
rg -q '^InaccessiblePaths=.*docker\.sock.*@HOME@/\.ssh' \
  "${voice_dir}/systemd/cerberus3-openclaw-voice.service.in"
if rg -n '(OPENCLAW_GATEWAY_TOKEN|VOICE_OPENCLAW_TOKEN)=[A-Za-z0-9_-]{16,}' \
  "${voice_dir}"; then
  echo "repository contains a materialized gateway token" >&2
  exit 1
fi

fake_bin="${temporary_dir}/fake-bin"
legacy_runtime="${temporary_dir}/legacy-runtime"
canonical_runtime="${temporary_dir}/canonical-runtime"
legacy_node="${legacy_runtime}/releases/node-v24.15.0-linux-arm64"
legacy_openclaw="${legacy_runtime}/releases/openclaw-2026.7.1-2"
mkdir -p \
  "${fake_bin}" "${legacy_node}/bin" "${legacy_openclaw}/bin" \
  "${legacy_openclaw}/lib/node_modules/openclaw"
cat >"${fake_bin}/sudo" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == install ]]; then
  shift
  filtered=()
  while (($#)); do
    case "$1" in
      -o|-g) shift 2 ;;
      *) filtered+=("$1"); shift ;;
    esac
  done
  exec install "${filtered[@]}"
fi
if [[ "${1:-}" == chown ]]; then
  exit 0
fi
exec "$@"
SH
chmod 0755 "${fake_bin}/sudo"
install -m 0755 "${voice_dir}/tests/fixtures/fake-node" \
  "${legacy_node}/bin/node"
install -m 0755 "${voice_dir}/tests/fixtures/fake-openclaw" \
  "${legacy_openclaw}/bin/openclaw"
install -m 0644 "${voice_dir}/tests/fixtures/fake-openclaw-package.json" \
  "${legacy_openclaw}/lib/node_modules/openclaw/package.json"

runtime_output="$({
  PATH="${fake_bin}:/usr/bin:/bin" \
  VOICE_OPENCLAW_RUNTIME_ROOT="${canonical_runtime}" \
  VOICE_OPENCLAW_LEGACY_RUNTIME_ROOT="${legacy_runtime}" \
    "${voice_dir}/scripts/install-openclaw-runtime.sh" install
} 2>&1)"
grep -Fq 'Copied and verified the pinned pre-rename runtime' <<<"${runtime_output}"
VOICE_OPENCLAW_RUNTIME_ROOT="${canonical_runtime}" \
  "${voice_dir}/scripts/install-openclaw-runtime.sh" verify-installed >/dev/null
[[ "$(readlink "${canonical_runtime}/node-current")" == \
   "${canonical_runtime}/releases/node-v24.15.0-linux-arm64" ]]
[[ "$(readlink "${canonical_runtime}/openclaw-current")" == \
   "${canonical_runtime}/releases/openclaw-2026.7.1-2" ]]

# A repeat install validates and reuses the canonical copy without touching the
# still-present legacy rollback source.
PATH="${fake_bin}:/usr/bin:/bin" \
VOICE_OPENCLAW_RUNTIME_ROOT="${canonical_runtime}" \
VOICE_OPENCLAW_LEGACY_RUNTIME_ROOT="${legacy_runtime}" \
  "${voice_dir}/scripts/install-openclaw-runtime.sh" install >/dev/null
[[ -x "${legacy_node}/bin/node" ]]

echo "OpenClaw voice config, pins, wrapper, routing, and secret boundary passed."
