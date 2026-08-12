const STATUS_URL = "http://127.0.0.1:9763/api/status";
const REQUEST_TIMEOUT_MS = 3000;
const MAX_RESPONSE_BYTES = 512 * 1024;
const HOST_NAMES = ["cerberus1", "cerberus2", "cerberus3"];

function objectOrEmpty(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

function finiteNumberOrNull(value) {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function shortStringOrNull(value, maxLength = 160) {
  if (typeof value !== "string") return null;
  const normalized = value.replace(/\s+/g, " ").trim();
  return normalized ? normalized.slice(0, maxLength) : null;
}

function sanitizeHost(value) {
  const host = objectOrEmpty(value);
  return {
    state: shortStringOrNull(host.state, 32) ?? "unknown",
    error: shortStringOrNull(host.error),
    cpu_percent: finiteNumberOrNull(host.cpu_percent),
    gpu_percent: finiteNumberOrNull(host.gpu_percent),
    ram_percent: finiteNumberOrNull(host.ram_percent),
    cpu_temperature_c: finiteNumberOrNull(host.cpu_temperature_c),
    gpu_temperature_c: finiteNumberOrNull(host.gpu_temperature_c),
    soc_temperature_c: finiteNumberOrNull(host.soc_temperature_c),
    age_seconds: finiteNumberOrNull(host.age_seconds),
  };
}

function sanitizeCluster(value) {
  const cluster = objectOrEmpty(value);
  return {
    state: shortStringOrNull(cluster.state, 32) ?? "unknown",
    available_hosts: finiteNumberOrNull(cluster.available_hosts),
    total_hosts: finiteNumberOrNull(cluster.total_hosts),
    cpu_percent: finiteNumberOrNull(cluster.cpu_percent),
    gpu_percent: finiteNumberOrNull(cluster.gpu_percent),
    ram_percent: finiteNumberOrNull(cluster.ram_percent),
    cpu_temperature_c: finiteNumberOrNull(cluster.cpu_temperature_c),
    gpu_temperature_c: finiteNumberOrNull(cluster.gpu_temperature_c),
    soc_temperature_c: finiteNumberOrNull(cluster.soc_temperature_c),
  };
}

function sanitizeThroughput(value) {
  const throughput = objectOrEmpty(value);
  return {
    state: shortStringOrNull(throughput.state, 32) ?? "unknown",
    tokens_per_second: finiteNumberOrNull(throughput.tokens_per_second),
    age_seconds: finiteNumberOrNull(throughput.age_seconds),
  };
}

function sanitizeLastError(value) {
  if (value === null || value === undefined) return null;
  const error = objectOrEmpty(value);
  const stage = shortStringOrNull(error.stage, 32);
  const errorType = shortStringOrNull(error.error_type, 80);
  const at = shortStringOrNull(error.at, 64);
  return stage || errorType || at
    ? { stage, error_type: errorType, at }
    : null;
}

function sanitizeVoiceAgent(value) {
  const voice = objectOrEmpty(value);
  const watchword = objectOrEmpty(voice.watchword);
  const asr = objectOrEmpty(voice.asr);
  const openclaw = objectOrEmpty(voice.openclaw);
  const tts = objectOrEmpty(voice.tts);
  return {
    state: shortStringOrNull(voice.state, 32) ?? "unknown",
    stage: shortStringOrNull(voice.stage, 32),
    age_seconds: finiteNumberOrNull(voice.age_seconds),
    watchword_state: shortStringOrNull(watchword.state, 32),
    asr_state: shortStringOrNull(asr.state, 32),
    openclaw_state: shortStringOrNull(openclaw.state, 32),
    tts_state: shortStringOrNull(tts.state, 32),
    status_error: shortStringOrNull(voice.status_error),
    last_error: sanitizeLastError(voice.last_error),
  };
}

export function sanitizeStatus(payload) {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    throw new Error("telemetry response was not a JSON object");
  }
  const sourceHosts = objectOrEmpty(payload.hosts);
  const hosts = {};
  for (const name of HOST_NAMES) hosts[name] = sanitizeHost(sourceHosts[name]);
  return {
    generated_at: shortStringOrNull(payload.generated_at, 64),
    cluster: sanitizeCluster(payload.cluster),
    hosts,
    throughput: sanitizeThroughput(payload.throughput),
    voice_agent: sanitizeVoiceAgent(payload.voice_agent),
  };
}

export async function fetchHealthSnapshot(fetchImpl = globalThis.fetch) {
  if (typeof fetchImpl !== "function") throw new Error("HTTP fetch is unavailable");
  const response = await fetchImpl(STATUS_URL, {
    method: "GET",
    headers: { accept: "application/json" },
    signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
  });
  if (!response || response.ok !== true) {
    const status = Number.isInteger(response?.status) ? `HTTP ${response.status}` : "request failed";
    throw new Error(`telemetry endpoint returned ${status}`);
  }
  const declaredLength = Number(response.headers?.get?.("content-length"));
  if (Number.isFinite(declaredLength) && declaredLength > MAX_RESPONSE_BYTES) {
    throw new Error("telemetry response was too large");
  }
  const text = await response.text();
  if (Buffer.byteLength(text, "utf8") > MAX_RESPONSE_BYTES) {
    throw new Error("telemetry response was too large");
  }
  let payload;
  try {
    payload = JSON.parse(text);
  } catch {
    throw new Error("telemetry response was not valid JSON");
  }
  return sanitizeStatus(payload);
}

export function createCerberusHealthTool(fetchImpl = globalThis.fetch) {
  return {
    name: "cerberus_health",
    label: "Cerberus Health",
    description: "Read the current Cerberus cluster health, utilization, temperatures, model throughput, and voice pipeline status. This tool is read-only.",
    parameters: {
      type: "object",
      additionalProperties: false,
      properties: {},
    },
    async execute() {
      const snapshot = await fetchHealthSnapshot(fetchImpl);
      return {
        content: [{ type: "text", text: JSON.stringify(snapshot) }],
        details: snapshot,
      };
    },
  };
}

export default {
  id: "cerberus-health",
  name: "Cerberus Health",
  description: "Read-only access to the local Cerberus cluster telemetry snapshot.",
  register(api) {
    api.registerTool(createCerberusHealthTool(), { optional: true });
  },
};
