#!/usr/bin/env bash

set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${script_dir}/common.sh"

need_command sudo
need_command docker

actual="$({
  sudo -n docker run --rm --pull never --network none \
    --entrypoint bash \
    -e "NCCL_RUNTIME_PATH=${NCCL_RUNTIME_PATH}" \
    "${DSPARK_VLLM_IMAGE}" -lc '
      export LD_LIBRARY_PATH="${NCCL_RUNTIME_PATH}:${LD_LIBRARY_PATH:-}"
      python3 - <<"PY"
import ctypes

library = ctypes.CDLL("libnccl.so.2")
version = ctypes.c_int()
status = library.ncclGetVersion(ctypes.byref(version))
if status != 0:
    raise SystemExit(f"ncclGetVersion failed: {status}")
print(version.value)
PY
    '
} 2>/dev/null)"

if [[ "${actual}" != "${NCCL_EXPECTED_VERSION}" ]]; then
  echo "Expected NCCL ${NCCL_EXPECTED_VERSION}, loaded ${actual:-nothing}." >&2
  exit 1
fi
echo "Container ring NCCL runtime verified: ${actual}."
