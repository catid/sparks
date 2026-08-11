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

if [[ "${1:-}" == image && "${2:-}" == inspect ]]; then
  exit 0
fi
if [[ "${1:-}" == run && -n "${TEST_DOWNLOAD_MODEL_DIR:-}" ]]; then
  for required in config.json model.safetensors codec.pth processing_arktts.py; do
    printf 'fixture\n' >"${TEST_DOWNLOAD_MODEL_DIR}/${required}"
  done
  exit 0
fi
if [[ "${1:-}" == run && -n "${TEST_DOCKER_LOG:-}" ]]; then
  printf '%s\n' "$@" >"${TEST_DOCKER_LOG}"
  exit 0
fi
echo "unexpected fake docker invocation: $*" >&2
exit 99
SH
chmod 0755 "${fake_bin}/docker"

cat >"${fake_bin}/curl" <<'SH'
#!/usr/bin/env bash
set -euo pipefail

output=""
while (($#)); do
  if [[ "$1" == -o ]]; then
    output="$2"
    shift 2
  else
    shift
  fi
done
[[ -n "${output}" ]]
printf 'not a wave file\n' >"${output}"
SH
chmod 0755 "${fake_bin}/curl"

mapfile -t model_values < <(python3 - "${repo_root}/audio8/MODEL.lock.json" <<'PY'
import json
import pathlib
import sys

data = json.loads(pathlib.Path(sys.argv[1]).read_text())
print(data["revision"])
print(data["local_directory"])
PY
)
revision="${model_values[0]}"
directory="${model_values[1]}"
model_root="${test_root}/models"
model_dir="${model_root}/${directory}"
mkdir -p -- "${model_dir}"
for required in config.json model.safetensors codec.pth processing_arktts.py; do
  printf 'fixture\n' >"${model_dir}/${required}"
done

set +e
missing_pin_output="$({
  PATH="${fake_bin}:/usr/bin:/bin" \
    AUDIO8_MODEL_DIR="${model_dir}" \
    "${repo_root}/audio8/run-server.sh"
} 2>&1)"
missing_pin_status=$?
set -e
[[ "${missing_pin_status}" == 2 ]]
grep -Fq 'does not match pinned revision' <<<"${missing_pin_output}"

printf 'wrong-revision\n' >"${model_dir}/.pinned-revision"
set +e
wrong_pin_output="$({
  PATH="${fake_bin}:/usr/bin:/bin" \
    AUDIO8_MODEL_DIR="${model_dir}" \
    "${repo_root}/audio8/run-server.sh"
} 2>&1)"
wrong_pin_status=$?
set -e
[[ "${wrong_pin_status}" == 2 ]]
grep -Fq 'does not match pinned revision' <<<"${wrong_pin_output}"

printf '%s\n' "${revision}" >"${model_dir}/.pinned-revision"
docker_log="${test_root}/docker.log"
PATH="${fake_bin}:/usr/bin:/bin" \
  TEST_DOCKER_LOG="${docker_log}" \
  AUDIO8_MODEL_DIR="${model_dir}" \
  AUDIO8_MAX_ACTIVE_REQUESTS=3 \
  "${repo_root}/audio8/run-server.sh"
grep -Fxq 'AUDIO8_MAX_ACTIVE_REQUESTS=3' "${docker_log}"

set +e
invalid_limit_output="$({
  PATH="${fake_bin}:/usr/bin:/bin" \
    AUDIO8_MODEL_DIR="${model_dir}" \
    AUDIO8_MAX_ACTIVE_REQUESTS=0 \
    "${repo_root}/audio8/run-server.sh"
} 2>&1)"
invalid_limit_status=$?
set -e
[[ "${invalid_limit_status}" == 2 ]]
grep -Fq 'Invalid AUDIO8_MAX_ACTIVE_REQUESTS' <<<"${invalid_limit_output}"

victim="${test_root}/pin-victim"
printf 'do not overwrite\n' >"${victim}"
rm -f -- "${model_dir}/.pinned-revision"
ln -s -- "${victim}" "${model_dir}/.pinned-revision"
PATH="${fake_bin}:/usr/bin:/bin" \
  TEST_DOWNLOAD_MODEL_DIR="${model_dir}" \
  AUDIO8_MODEL_ROOT="${model_root}" \
  "${repo_root}/audio8/download-model.sh" >/dev/null
grep -Fxq 'do not overwrite' "${victim}"
[[ -f "${model_dir}/.pinned-revision" && ! -L "${model_dir}/.pinned-revision" ]]
grep -Fxq "${revision}" "${model_dir}/.pinned-revision"

test_wav="${test_root}/invalid.wav"
set +e
invalid_wav_output="$({
  PATH="${fake_bin}:/usr/bin:/bin" \
    AUDIO8_TEST_WAV="${test_wav}" \
    "${repo_root}/audio8/synthesize-test.sh"
} 2>&1)"
invalid_wav_status=$?
set -e
[[ "${invalid_wav_status}" != 0 ]]
grep -Fq 'not a valid WAV container' <<<"${invalid_wav_output}"
[[ ! -e "${test_wav}" ]]

grep -Fxq 'COPY server.py ./server.py' "${repo_root}/audio8/Dockerfile"
grep -Fq '"${root}/audio8"' "${repo_root}/scripts/install-audio8.sh"
grep -Fxq 'Restart=always' \
  "${repo_root}/systemd/cerebrus3-audio8.service.in"
rendered_unit="${test_root}/cerebrus3-audio8.service"
sed \
  -e "s|@PROJECT_DIR@|${repo_root}|g" \
  -e "s|@HOME@|${HOME}|g" \
  -e "s|@MODEL_DIR@|${model_dir}|g" \
  -e "s|@USER@|$(id -un)|g" \
  -e "s|@GROUP@|$(id -gn)|g" \
  "${repo_root}/systemd/cerebrus3-audio8.service.in" >"${rendered_unit}"
verify_log="${test_root}/systemd-verify.log"
if ! systemd-analyze verify "${rendered_unit}" >"${verify_log}" 2>&1; then
  cat "${verify_log}" >&2
  exit 1
fi

echo "Audio8 checkpoint, admission, and service static tests passed."
