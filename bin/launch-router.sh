#!/usr/bin/env bash
set -euo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
router_bin="${VLLM_ROUTER_BIN:-/home/catid/venvs/vllm-router0115/bin/vllm-router}"

router_host="${ROUTER_HOST:-0.0.0.0}"
router_port="${ROUTER_PORT:-8080}"
metrics_host="${ROUTER_METRICS_HOST:-0.0.0.0}"
metrics_port="${ROUTER_METRICS_PORT:-29000}"
worker_1="${ROUTER_WORKER_1:-http://192.168.100.10:8000}"
worker_2="${ROUTER_WORKER_2:-http://192.168.100.11:8000}"
router_policy="${ROUTER_POLICY:-consistent_hash}"

if [[ ! -x "${router_bin}" ]]; then
  echo "vLLM router is not executable: ${router_bin}" >&2
  exit 1
fi

mkdir -p "${root_dir}/logs/vllm-router"

# The router waits for both replicas during startup. Twenty minutes covers the
# approximately ten-minute cold model load, while systemd restarts it if the
# backends stay unavailable beyond that window.
#
# consistent_hash is the safe default for agentic Chat Completions traffic:
# vllm-router 0.1.15 does not derive cache-aware keys from `messages`, and the
# Laguna replicas enable prefix caching. Give each agent conversation a stable
# X-Session-ID so every turn returns to the replica holding that prefix.
# ROUTER_POLICY=cache_aware remains available for request schemas from which
# the router can extract routing text or session_params.
exec "${router_bin}" \
  --host "${router_host}" \
  --port "${router_port}" \
  --worker-urls "${worker_1}" "${worker_2}" \
  --policy "${router_policy}" \
  --cache-threshold "${ROUTER_CACHE_THRESHOLD:-0.30}" \
  --balance-abs-threshold "${ROUTER_BALANCE_ABS_THRESHOLD:-16}" \
  --balance-rel-threshold "${ROUTER_BALANCE_REL_THRESHOLD:-1.5}" \
  --eviction-interval-secs "${ROUTER_EVICTION_INTERVAL_SECS:-120}" \
  --worker-startup-timeout-secs "${ROUTER_STARTUP_TIMEOUT_SECS:-1200}" \
  --worker-startup-check-interval "${ROUTER_STARTUP_CHECK_INTERVAL_SECS:-10}" \
  --health-failure-threshold "${ROUTER_HEALTH_FAILURE_THRESHOLD:-2}" \
  --health-success-threshold "${ROUTER_HEALTH_SUCCESS_THRESHOLD:-1}" \
  --health-check-timeout-secs "${ROUTER_HEALTH_TIMEOUT_SECS:-5}" \
  --health-check-interval-secs "${ROUTER_HEALTH_INTERVAL_SECS:-10}" \
  --health-check-endpoint /health \
  --retry-max-retries "${ROUTER_RETRY_MAX_RETRIES:-3}" \
  --retry-initial-backoff-ms "${ROUTER_RETRY_INITIAL_BACKOFF_MS:-100}" \
  --retry-max-backoff-ms "${ROUTER_RETRY_MAX_BACKOFF_MS:-5000}" \
  --retry-backoff-multiplier "${ROUTER_RETRY_BACKOFF_MULTIPLIER:-2.0}" \
  --retry-jitter-factor "${ROUTER_RETRY_JITTER_FACTOR:-0.2}" \
  --cb-failure-threshold "${ROUTER_CB_FAILURE_THRESHOLD:-3}" \
  --cb-success-threshold "${ROUTER_CB_SUCCESS_THRESHOLD:-2}" \
  --cb-timeout-duration-secs "${ROUTER_CB_TIMEOUT_SECS:-30}" \
  --cb-window-duration-secs "${ROUTER_CB_WINDOW_SECS:-60}" \
  --request-timeout-secs "${ROUTER_REQUEST_TIMEOUT_SECS:-3600}" \
  --max-concurrent-requests "${ROUTER_MAX_CONCURRENT_REQUESTS:-64}" \
  --queue-size "${ROUTER_QUEUE_SIZE:-128}" \
  --queue-timeout-secs "${ROUTER_QUEUE_TIMEOUT_SECS:-900}" \
  --request-id-headers \
    x-session-affinity x-session-id x-client-request-id \
    x-request-id x-correlation-id x-trace-id \
  --prometheus-host "${metrics_host}" \
  --prometheus-port "${metrics_port}" \
  --log-dir "${root_dir}/logs/vllm-router" \
  --log-level "${ROUTER_LOG_LEVEL:-info}"
