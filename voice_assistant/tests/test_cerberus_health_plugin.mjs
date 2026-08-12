import assert from "node:assert/strict";
import test from "node:test";

import plugin, {
  createCerberusHealthTool,
  fetchHealthSnapshot,
  sanitizeStatus,
} from "../openclaw/plugins/cerberus-health/index.js";

const sample = {
  generated_at: "2026-08-11T12:00:00Z",
  hosts: {
    cerberus1: {
      state: "online",
      cpu_percent: 10,
      gpu_percent: 20,
      ram_percent: 30,
      cpu_temperature_c: 40,
      gpu_temperature_c: 50,
      soc_temperature_c: 45,
      age_seconds: 1,
      private_detail: "must not escape",
    },
    cerberus2: { state: "degraded", error: " probe   delayed " },
    cerberus3: { state: "online" },
    unexpected: { state: "online", secret: "must not escape" },
  },
  cluster: {
    state: "degraded",
    available_hosts: 2,
    total_hosts: 3,
    cpu_percent: 11,
    gpu_percent: 22,
    ram_percent: 33,
  },
  throughput: { state: "online", tokens_per_second: 144.7, age_seconds: 0.5 },
  voice_agent: {
    state: "busy",
    stage: "openclaw",
    age_seconds: 0.6,
    watchword: { state: "triggered" },
    asr: { state: "ok" },
    openclaw: { state: "thinking" },
    tts: { state: "idle" },
    status_error: null,
    last_error: {
      stage: "tts_synthesis",
      error_type: "TimeoutError",
      at: "2026-08-11T12:00:01Z",
      message: "must not escape",
    },
    transcript: "must not escape",
  },
  history: [{ secret: "must not escape" }],
};

test("sanitizes the telemetry snapshot to fixed current-state fields", () => {
  const result = sanitizeStatus(sample);
  assert.equal(result.cluster.state, "degraded");
  assert.equal(result.hosts.cerberus2.error, "probe delayed");
  assert.equal(result.throughput.tokens_per_second, 144.7);
  assert.equal(result.voice_agent.openclaw_state, "thinking");
  assert.deepEqual(result.voice_agent.last_error, {
    stage: "tts_synthesis",
    error_type: "TimeoutError",
    at: "2026-08-11T12:00:01Z",
  });
  assert.deepEqual(Object.keys(result.hosts), ["cerberus1", "cerberus2", "cerberus3"]);
  assert.equal(JSON.stringify(result).includes("must not escape"), false);
  assert.equal(Object.hasOwn(result, "history"), false);
});

test("fetches only the fixed loopback health endpoint", async () => {
  let requestedUrl;
  let requestedOptions;
  const result = await fetchHealthSnapshot(async (url, options) => {
    requestedUrl = url;
    requestedOptions = options;
    return {
      ok: true,
      status: 200,
      headers: { get: () => null },
      text: async () => JSON.stringify(sample),
    };
  });
  assert.equal(requestedUrl, "http://127.0.0.1:9763/api/status");
  assert.equal(requestedOptions.method, "GET");
  assert.deepEqual(requestedOptions.headers, { accept: "application/json" });
  assert.equal(result.cluster.available_hosts, 2);
});

test("rejects invalid or oversized responses", async () => {
  await assert.rejects(
    fetchHealthSnapshot(async () => ({
      ok: false,
      status: 503,
      headers: { get: () => null },
      text: async () => "",
    })),
    /HTTP 503/,
  );
  await assert.rejects(
    fetchHealthSnapshot(async () => ({
      ok: true,
      status: 200,
      headers: { get: () => String(513 * 1024) },
      text: async () => "",
    })),
    /too large/,
  );
});

test("registers one optional read-only tool", async () => {
  let registeredTool;
  let registeredOptions;
  plugin.register({
    registerTool(tool, options) {
      registeredTool = tool;
      registeredOptions = options;
    },
  });
  assert.equal(registeredTool.name, "cerberus_health");
  assert.deepEqual(registeredOptions, { optional: true });

  const tool = createCerberusHealthTool(async () => ({
    ok: true,
    status: 200,
    headers: { get: () => null },
    text: async () => JSON.stringify(sample),
  }));
  const output = await tool.execute("test", {});
  assert.equal(output.details.cluster.state, "degraded");
  assert.equal(output.content[0].type, "text");
});
