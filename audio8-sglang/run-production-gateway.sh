#!/usr/bin/env bash
set -euo pipefail

[[ "${AUDIO8_SGLANG_PRODUCTION:-0}" == 1 ]] || {
  echo "Set AUDIO8_SGLANG_PRODUCTION=1 to run the production gateway." >&2
  exit 2
}
[[ "${AUDIO8_SGLANG_EXPERIMENTAL:-0}" != 1 ]] || {
  echo "Production and experimental modes are mutually exclusive." >&2
  exit 2
}

root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
"${root}/wait-for-docker.sh"
lock="${root}/RUNTIME.lock.json"
runtime_uid="$(id -u)"
runtime_gid="$(id -g)"
backend_network=cerberus3-audio8-sglang-backend
frontend_network=cerberus3-audio8-sglang-frontend

identity_values="$(python3 "${root}/runtime_identity.py" values "${lock}" "${root}")"
mapfile -t values <<<"${identity_values}"
[[ "${#values[@]}" == 8 ]] || exit 2
image="${values[3]}"
runtime_fingerprint="${values[7]}"
image_fingerprint="$(docker image inspect --format \
  '{{index .Config.Labels "io.cerberus.audio8-sglang.source-contract-patchset-sha256"}}' \
  "${image}")" || {
  echo "Missing ${image}; run audio8-sglang/build-image.sh first." >&2
  exit 1
}

backend_contract="$(docker network inspect --format \
  '{{.Driver}}|{{.Scope}}|{{.Internal}}|{{(index .IPAM.Config 0).Subnet}}|{{index .Labels "io.cerberus.audio8-sglang.role"}}' \
  "${backend_network}")" || exit 1
frontend_contract="$(docker network inspect --format \
  '{{.Driver}}|{{.Scope}}|{{.Internal}}|{{(index .IPAM.Config 0).Subnet}}|{{index .Labels "io.cerberus.audio8-sglang.role"}}' \
  "${frontend_network}")" || exit 1
[[ "${backend_contract}" == 'bridge|local|true|172.30.82.0/29|backend' ]] || {
  echo "Production backend network contract is invalid." >&2
  exit 1
}
[[ "${frontend_contract}" == 'bridge|local|false|172.30.81.0/29|frontend' ]] || {
  echo "Production frontend network contract is invalid." >&2
  exit 1
}

image_labels="$(docker image inspect --format '{{json .Config.Labels}}' "${image}")" || {
  echo "Missing ${image}; run audio8-sglang/build-image.sh first." >&2
  exit 1
}
if [[ "${image_fingerprint}" == "${runtime_fingerprint}" ]]; then
  printf '%s\n' "${image_labels}" | \
    python3 "${root}/runtime_identity.py" verify-labels "${lock}" "${root}"
else
  [[ "${AUDIO8_SGLANG_ALLOW_GATEWAY_ONLY_UPDATE:-0}" == 1 &&
     "${image_fingerprint}" =~ ^[0-9a-f]{64}$ ]] || {
    echo "Image identity differs from this runtime." >&2
    exit 1
  }
fi

if ss -ltnH 'sport = :8010' | grep -q .; then
  echo "Production port 8010 is already in use." >&2
  exit 1
fi

exec docker run --rm --pull never \
  --init \
  --name cerberus3-audio8-sglang-gateway \
  --hostname cerberus3-audio8-sglang-gateway \
  --label io.cerberus.audio8-sglang.role=gateway \
  --label "io.cerberus.audio8-sglang.runtime-fingerprint=${image_fingerprint}" \
  --user "${runtime_uid}:${runtime_gid}" \
  --network "name=${frontend_network},ip=172.30.81.2" \
  --network "name=${backend_network},ip=172.30.82.3" \
  --publish 127.0.0.1:8010:8010 \
  --read-only \
  --cap-drop ALL \
  --security-opt no-new-privileges \
  --pids-limit 128 \
  --memory 512m \
  --memory-swap 512m \
  --log-opt max-size=10m \
  --log-opt max-file=3 \
  --tmpfs "/tmp:rw,nosuid,nodev,noexec,size=32m,uid=${runtime_uid},gid=${runtime_gid},mode=0700" \
  --health-cmd 'python3 /opt/cerberus/check_health.py gateway' \
  --health-interval 10s \
  --health-timeout 5s \
  --health-start-period 30s \
  --health-retries 3 \
  --env HOME=/tmp \
  --env USER=audio8-gateway \
  --env LOGNAME=audio8-gateway \
  --env AUDIO8_SGLANG_PRODUCTION=1 \
  --dns 127.0.0.1 \
  --env AUDIO8_SGLANG_BACKEND_URL=http://172.30.82.2:8010/v1/audio/speech \
  --env AUDIO8_SGLANG_GATEWAY_HOST=0.0.0.0 \
  --env AUDIO8_SGLANG_GATEWAY_PORT=8010 \
  --env AUDIO8_SGLANG_MAX_ACTIVE_REQUESTS=2 \
  --env AUDIO8_SGLANG_MAX_CONNECTIONS=16 \
  --env HTTP_PROXY= \
  --env HTTPS_PROXY= \
  --env ALL_PROXY= \
  --env http_proxy= \
  --env https_proxy= \
  --env all_proxy= \
  --volume "${root}/gateway.py:/opt/cerberus/gateway.py:ro" \
  --volume "${root}/check_health.py:/opt/cerberus/check_health.py:ro" \
  --entrypoint python3 \
  "${image}" \
  /opt/cerberus/gateway.py
