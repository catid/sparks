#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"
test_root="$(mktemp -d)"
cleanup() { rm -rf -- "${test_root}"; }
trap cleanup EXIT

fake_bin="${test_root}/bin"
mkdir -p -- "${fake_bin}"
cat >"${fake_bin}/docker" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
[[ "${1:-}" == run ]]
printf '%s\n' "$@" >"${TEST_DOCKER_LOG}"
SH
chmod 0755 "${fake_bin}/docker"

mapfile -t values < <(python3 - "${repo_root}/voice_assistant/MODEL.lock.json" <<'PY'
import json
import pathlib
import sys

data = json.loads(pathlib.Path(sys.argv[1]).read_text())
assert data["revision"] == "bcd2b5b7f32b480ab5790554cfa8347f246a14f3"
assert data["weights"]["sha256"] == "2db53c7d81bd9b8cbc6a074e89be2c968a0d373fb4ee68bb1b1e14f7042dfee1"
assert data["weights"]["size_bytes"] == 4076193080
print(data["revision"])
print(data["local_directory"])
print(data["weights"]["path"])
print(data["weights"]["size_bytes"])
PY
)
revision="${values[0]}"
directory="${values[1]}"
weight_path="${values[2]}"
weight_size="${values[3]}"
model_dir="${test_root}/${directory}"
mkdir -p -- "${model_dir}"
for required in config.json processor_config.json tokenizer.json chat_template.jinja; do
  printf 'fixture\n' >"${model_dir}/${required}"
done
truncate -s "${weight_size}" -- "${model_dir}/${weight_path}"
printf '%s\n' "${revision}" >"${model_dir}/.pinned-revision"

docker_log="${test_root}/docker.log"
PATH="${fake_bin}:/usr/bin:/bin" \
  TEST_DOCKER_LOG="${docker_log}" \
  QWEN_ASR_MODEL_DIR="${model_dir}" \
  "${repo_root}/voice_assistant/run-asr.sh"

grep -Fxq 'QWEN_ASR_HOST=127.0.0.1' "${docker_log}"
grep -Fxq 'QWEN_ASR_PORT=8020' "${docker_log}"
grep -Fxq \
  'QWEN_ASR_VOCABULARY_PROMPT=Vocabulary: Cerberus, Cerberus One, Cerberus Two, Cerberus Three, cerberus1, cerberus2, cerberus3.' \
  "${docker_log}"
grep -Fxq 'TRANSFORMERS_OFFLINE=1' "${docker_log}"
grep -Fxq 'HF_HUB_OFFLINE=1' "${docker_log}"
grep -Fxq 'ALL' "${docker_log}"
grep -Fxq 'host' "${docker_log}"
grep -Fq 'transformers==5.13.0' "${repo_root}/voice_assistant/Dockerfile"
grep -Fq 'sha256:a83948492cf13df455170fb42885f5ef4db54fefe0feff0f841ecbff464ac9d8' \
  "${repo_root}/voice_assistant/Dockerfile"
if grep -Fq 'shell=True' "${repo_root}/voice_assistant/voice_bridge.py"; then
  echo "Voice bridge must not invoke a shell." >&2
  exit 1
fi
if grep -Fq 'urllib.request.urlopen' \
  "${repo_root}/voice_assistant/voice_bridge.py"; then
  echo "Voice bridge must use its proxy-free, no-redirect opener." >&2
  exit 1
fi
grep -Fq 'urllib.request.ProxyHandler({})' \
  "${repo_root}/voice_assistant/voice_bridge.py"
if grep -Fq 'Mm.' "${repo_root}/voice_assistant/voice_bridge.py"; then
  echo "Voice bridge must not synthesize the thinking cue through Audio8." >&2
  exit 1
fi
grep -Fq 'load_private_thinking_cue(self.settings.thinking_cue_wav)' \
  "${repo_root}/voice_assistant/voice_bridge.py"
if grep -Eq 'tempfile|mkstemp|NamedTemporaryFile' \
  "${repo_root}/voice_assistant/voice_bridge.py"; then
  echo "Voice bridge must keep audio in memory." >&2
  exit 1
fi
grep -Fq 'http.client.HTTPConnection("127.0.0.1"' \
  "${repo_root}/voice_assistant/alarm_service.py"
if grep -Eq 'tempfile|mkstemp|NamedTemporaryFile' \
  "${repo_root}/voice_assistant/alarm_service.py"; then
  echo "Alarm service must keep generated audio in memory." >&2
  exit 1
fi
grep -Fq 'ThreadingUnixStreamServer' \
  "${repo_root}/voice_assistant/alarm_service.py"
grep -Fq 'threading.BoundedSemaphore(max_connections)' \
  "${repo_root}/voice_assistant/alarm_service.py"
grep -Fq 'READY=1' \
  "${repo_root}/voice_assistant/alarm_service.py"
grep -Fq 'MAX_RINGING_SECONDS = 10 * 60' \
  "${repo_root}/voice_assistant/alarm_service.py"

echo "ASR pinning, container isolation, and RAM-only bridge static tests passed."
