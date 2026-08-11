#!/usr/bin/env bash
set -euo pipefail

[[ "${AUDIO8_SGLANG_PRODUCTION:-0}" == 1 ]] || {
  echo "Set AUDIO8_SGLANG_PRODUCTION=1 to supervise production." >&2
  exit 2
}

root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
container=cerberus3-audio8-sglang-backend
startup_deadline=$((SECONDS + 420))
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
  notify --stopping --status="Stopping Audio8 SGLang backend"
  docker stop --time 30 "${container}" >/dev/null 2>&1 || true
}
trap shutdown TERM INT

"${root}/run-production-backend.sh" &
docker_pid=$!
ready=0
unhealthy_polls=0

while kill -0 "${docker_pid}" 2>/dev/null; do
  health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' \
    "${container}" 2>/dev/null || true)"
  case "${health}" in
    healthy)
      unhealthy_polls=0
      if ((ready == 0)); then
        ready=1
        notify --ready --status="Audio8 SGLang backend healthy and optimized"
      else
        notify WATCHDOG=1 --status="Audio8 SGLang backend healthy"
      fi
      ;;
    unhealthy)
      if ((ready == 1)); then
        ((unhealthy_polls += 1))
        notify WATCHDOG=1 --status="Audio8 SGLang backend unhealthy (${unhealthy_polls}/3)"
        if ((unhealthy_polls >= 3)); then
          echo "Backend remained unhealthy; forcing a bounded restart." >&2
          docker stop --time 30 "${container}" >/dev/null 2>&1 || true
          break
        fi
      elif ((SECONDS >= startup_deadline)); then
        echo "Backend failed its startup health deadline." >&2
        docker stop --time 30 "${container}" >/dev/null 2>&1 || true
        break
      fi
      ;;
    starting|none|"")
      if ((ready == 0 && SECONDS >= startup_deadline)); then
        echo "Backend failed its startup health deadline." >&2
        docker stop --time 30 "${container}" >/dev/null 2>&1 || true
        break
      fi
      notify WATCHDOG=1 --status="Audio8 SGLang backend ${health:-launching}"
      ;;
    *)
      echo "Unexpected backend health state: ${health}" >&2
      docker stop --time 30 "${container}" >/dev/null 2>&1 || true
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
  echo "Production backend exited unexpectedly." >&2
  exit 1
fi
exit "${status}"
