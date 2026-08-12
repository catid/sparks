#!/usr/bin/env bash
set -euo pipefail

[[ "${AUDIO8_SGLANG_PRODUCTION:-0}" == 1 ]] || {
  echo "Set AUDIO8_SGLANG_PRODUCTION=1 to supervise production." >&2
  exit 2
}

root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
container=cerberus3-audio8-sglang-gateway
backend=cerberus3-audio8-sglang-backend
docker_pid=
stopping=0

notify() {
  if [[ -n "${NOTIFY_SOCKET:-}" ]]; then
    systemd-notify "$@" || true
  fi
}

# Signal handlers are invoked indirectly by Bash.
# shellcheck disable=SC2317
shutdown() {
  stopping=1
  notify --stopping --status="Stopping Audio8 SGLang gateway"
  docker stop --timeout 15 "${container}" >/dev/null 2>&1 || true
}
trap shutdown TERM INT

"${root}/run-production-gateway.sh" &
docker_pid=$!
ready=0
unhealthy_polls=0

while kill -0 "${docker_pid}" 2>/dev/null; do
  gateway_health="$(docker inspect --format \
    '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' \
    "${container}" 2>/dev/null || true)"
  backend_health="$(docker inspect --format \
    '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' \
    "${backend}" 2>/dev/null || true)"
  case "${gateway_health}" in
    healthy)
      unhealthy_polls=0
      if ((ready == 0)); then
        ready=1
        notify --ready --status="Audio8 SGLang gateway healthy"
      else
        notify WATCHDOG=1 --status="Audio8 SGLang gateway healthy"
      fi
      ;;
    unhealthy)
      if [[ "${backend_health}" == healthy ]]; then
        ((unhealthy_polls += 1))
        notify WATCHDOG=1 \
          --status="Audio8 gateway unhealthy with healthy backend (${unhealthy_polls}/3)"
        if ((unhealthy_polls >= 3)); then
          echo "Gateway remained unhealthy while backend was healthy; restarting." >&2
          docker stop --timeout 15 "${container}" >/dev/null 2>&1 || true
          break
        fi
      else
        unhealthy_polls=0
        notify WATCHDOG=1 \
          --status="Audio8 gateway waiting for backend (${backend_health:-absent})"
      fi
      ;;
    starting|none|"")
      unhealthy_polls=0
      notify WATCHDOG=1 \
        --status="Audio8 SGLang gateway ${gateway_health:-launching}"
      ;;
    *)
      echo "Unexpected gateway health state: ${gateway_health}" >&2
      docker stop --timeout 15 "${container}" >/dev/null 2>&1 || true
      break
      ;;
  esac
  sleep 5
done

set +e
wait "${docker_pid}"
status=$?
set -e
if ((stopping == 1)); then
  exit 0
fi
if ((status == 0)); then
  echo "Production gateway exited unexpectedly." >&2
  exit 1
fi
exit "${status}"
