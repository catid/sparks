import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const testDirectory = path.dirname(fileURLToPath(import.meta.url));
const staticDirectory = path.join(testDirectory, "..", "static");
const source = fs.readFileSync(path.join(staticDirectory, "app.js"), "utf8");
const context = vm.createContext({});
vm.runInContext(source, context, { filename: "app.js" });
const ui = context.C3DashboardUI;

const statusPayload = {
  generated_at: "2026-08-10T18:30:05Z",
  interval_seconds: 5,
  hosts: {
    cerberus1: {
      state: "online",
      error: null,
      cpu_percent: 21.2,
      gpu_percent: 71.8,
      ram_percent: 63.4,
      cpu_temperature_c: 48.2,
      gpu_temperature_c: 51,
      soc_temperature_c: 46.7,
      ram_temperature_c: null,
      ram_used_bytes: 81234567890,
      ram_total_bytes: 128000000000,
      age_seconds: 1.2,
    },
    cerberus2: {
      state: "online",
      error: null,
      cpu_percent: 31.7,
      gpu_percent: 75.1,
      ram_percent: 64.8,
      cpu_temperature_c: 49.4,
      gpu_temperature_c: 52,
      soc_temperature_c: 47.1,
      ram_temperature_c: null,
      age_seconds: 0.8,
    },
    cerberus3: {
      state: "online",
      error: null,
      cpu_percent: 18.5,
      gpu_percent: 68.4,
      ram_percent: 61.1,
      cpu_temperature_c: 45.8,
      gpu_temperature_c: 49,
      soc_temperature_c: 44.9,
      ram_temperature_c: null,
      age_seconds: 1.5,
    },
  },
  cluster: {
    state: "online",
    available_hosts: 3,
    total_hosts: 3,
    cpu_percent: 23.8,
    gpu_percent: 71.8,
    ram_percent: 63.1,
  },
  throughput: {
    state: "online",
    tokens_per_second: 144.7,
    age_seconds: 0.5,
    source: "vllm",
  },
  history: [
    {
      timestamp: "2026-08-10T18:30:00Z",
      cluster: { cpu_percent: 20, gpu_percent: 65, ram_percent: 62 },
      throughput: { state: "online", tokens_per_second: 132 },
      hosts: {
        cerberus1: { state: "online", cpu_percent: 19, gpu_percent: 63, ram_percent: 62, cpu_temperature_c: 47, gpu_temperature_c: 50, soc_temperature_c: 46, ram_temperature_c: null },
        cerberus2: { state: "online", cpu_percent: 29, gpu_percent: 68, ram_percent: 64, cpu_temperature_c: 48, gpu_temperature_c: 51, soc_temperature_c: 47, ram_temperature_c: null },
        cerberus3: { state: "online", cpu_percent: 12, gpu_percent: 61, ram_percent: 59, cpu_temperature_c: 44, gpu_temperature_c: 48, soc_temperature_c: 44, ram_temperature_c: null },
      },
    },
    {
      timestamp: "2026-08-10T18:30:05Z",
      cluster: { cpu_percent: 22, gpu_percent: 70, ram_percent: 63 },
      throughput: { state: "online", tokens_per_second: 140 },
      hosts: {
        cerberus1: { state: "online", cpu_percent: 20, gpu_percent: 70, ram_percent: 63, cpu_temperature_c: 48, gpu_temperature_c: 51, soc_temperature_c: 46, ram_temperature_c: null },
        cerberus2: { state: "online", cpu_percent: 32, gpu_percent: 74, ram_percent: 65, cpu_temperature_c: 49, gpu_temperature_c: 52, soc_temperature_c: 47, ram_temperature_c: null },
        cerberus3: { state: "online", cpu_percent: 18, gpu_percent: 68, ram_percent: 61, cpu_temperature_c: 45, gpu_temperature_c: 49, soc_temperature_c: 45, ram_temperature_c: null },
      },
    },
  ],
};

function voiceStatus({
  state = "ready",
  stage = "listening",
  watchword = "listening",
  asr = "idle",
  openclaw = "idle",
  tts = "idle",
  statusError = null,
  lastError = null,
  chunkIndex = 0,
  chunkTotal = 0,
} = {}) {
  return {
    schema: 1,
    service: "cerberus-voice",
    device: "Cerberus",
    state,
    healthy: ["ready", "busy", "armed"].includes(state) && !statusError,
    stage,
    stage_started_at: "2026-08-10T18:30:00Z",
    stage_elapsed_seconds: 5,
    updated_at: "2026-08-10T18:30:04.5Z",
    age_seconds: 0.5,
    stale_after_seconds: 15,
    status_error: statusError,
    watchword: {
      state: watchword,
      last_triggered_at: watchword === "listening" ? null : "2026-08-10T18:29:59Z",
      armed_until: watchword === "armed" ? "2026-08-10T18:30:12Z" : null,
      armed_remaining_seconds: watchword === "armed" ? 7 : null,
    },
    asr: {
      state: asr,
      duration_seconds: asr === "ok" ? 1.21 : null,
      elapsed_seconds: asr === "processing" ? 5 : null,
    },
    openclaw: {
      state: openclaw,
      duration_seconds: openclaw === "ok" ? 24.6 : null,
      elapsed_seconds: openclaw === "thinking" ? 5 : null,
    },
    tts: {
      state: tts,
      duration_seconds: tts === "ok" ? 3.4 : null,
      elapsed_seconds: ["synthesizing", "playing", "cooldown"].includes(tts) ? 5 : null,
      chunk_index: chunkIndex,
      chunk_total: chunkTotal,
    },
    last_error: lastError,
  };
}

test("normalizes the backend status contract without treating missing data as zero", () => {
  const payload = ui.normalizePayload(statusPayload);

  assert.equal(payload.interval_seconds, 5);
  assert.equal(payload.cluster.cpu_percent, undefined);
  assert.equal(payload.throughput.tokens_per_second, 144.7);
  assert.deepEqual(Array.from(payload.hosts, (host) => host.slot), [1, 2, 3]);
  assert.equal(payload.hosts[0].ram_total_bytes, 128000000000);
  assert.equal(payload.hosts[0].cpu_temperature_c, 48.2);
  assert.equal(payload.hosts[0].ram_temperature_c, null);

  const sparse = ui.normalizePayload({
    hosts: { c1: { state: "online", cpu_percent: null, gpu_percent: "" } },
    cluster: {},
    throughput: {},
  });
  assert.equal(sparse.hosts[0].cpu_percent, null);
  assert.equal(sparse.hosts[0].gpu_percent, null);
  assert.equal(sparse.cluster.ram_percent, undefined);
  assert.equal(sparse.throughput.tokens_per_second, null);
});

test("accepts the common C1, Spark 2, and cerberus3 host labels", () => {
  assert.equal(ui.hostSlot("C1", {}, 0), 1);
  assert.equal(ui.hostSlot("Spark 2", {}, 0), 2);
  assert.equal(ui.hostSlot("telemetry", { hostname: "cerberus3" }, 0), 3);
});

test("builds independent host graphs and API-wide token history", () => {
  const payload = ui.normalizePayload(statusPayload);

  assert.deepEqual(Array.from(ui.hostMetricSeries(payload, "cpu", 1)), [19, 21.2]);
  assert.deepEqual(Array.from(ui.hostMetricSeries(payload, "cpu", 2)), [29, 31.7]);
  assert.deepEqual(Array.from(ui.hostMetricSeries(payload, "gpu", 3)), [61, 68.4]);
  assert.deepEqual(Array.from(ui.hostTemperatureSeries(payload, "cpu", 1)), [47, 48.2]);
  assert.deepEqual(Array.from(ui.hostTemperatureSeries(payload, "gpu", 3)), [48, 49]);
  assert.deepEqual(Array.from(ui.hostTemperatureSeries(payload, "ram", 2)), [47, 47.1]);
  assert.deepEqual(Array.from(ui.tokenSeries(payload)), [132, 144.7]);
});

test("missing host samples remain gaps rather than cluster-average fallbacks", () => {
  const payload = ui.normalizePayload({
    hosts: { cerberus1: { state: "online", cpu_percent: 25 } },
    history: [
      { hosts: { cerberus1: { cpu_percent: 10 } } },
      { cluster: { cpu_percent: 99 }, hosts: {} },
    ],
  });

  assert.deepEqual(Array.from(ui.hostMetricSeries(payload, "cpu", 1)), [10, 25]);
  assert.deepEqual(Array.from(ui.hostMetricSeries(payload, "cpu", 2)), [null, null]);
});

test("sparkline paths preserve telemetry gaps and never emit invalid coordinates", () => {
  const graph = ui.sparklinePaths([10, null, 30, 40], {
    width: 240,
    height: 84,
    padding: 4,
    min: 0,
    max: 100,
  });

  assert.equal((graph.line.match(/M/g) || []).length, 2);
  assert.equal((graph.area.match(/Z/g) || []).length, 2);
  assert.doesNotMatch(graph.line, /NaN|Infinity/);
  assert.ok(graph.latest.x > 230);
  assert.ok(graph.latest.y > 4 && graph.latest.y < 80);
});

test("zero is valid telemetry and token chart ceilings are stable", () => {
  assert.equal(ui.formatCurrent(0, "cpu"), "0");
  assert.equal(ui.formatCurrent(0, "tokens"), "0");
  assert.equal(ui.formatCurrent(48.6, "temperature"), "49");
  assert.equal(ui.formatCurrent(135, "temperature"), "135");
  assert.equal(ui.niceCeiling(144.7 * 1.08), 200);
  assert.deepEqual(
    { ...ui.metricStats([null, 0, 10, 4]) },
    { min: 0, max: 10, delta: 4 },
  );
});

test("ambient renderer changes scene every 30 seconds and stays bounded", () => {
  assert.equal(ui.AMBIENT_SCENE_MS, 30000);
  assert.equal(ui.ambientSceneAt(0), 0);
  assert.equal(ui.ambientSceneAt(29999), 0);
  assert.equal(ui.ambientSceneAt(30000), 1);
  assert.equal(ui.ambientSceneAt(90000), 3);
  assert.equal(ui.ambientSceneAt(120000), 0);

  const offsets = [0, 1, 2, 3].map((scene) => ({ ...ui.burnInOffset(scene) }));
  assert.equal(new Set(offsets.map(({ x, y }) => `${x},${y}`)).size, 4);
  for (let scene = 0; scene < 4; scene += 1) {
    const pixel = Array.from(ui.ambientPixel(scene, 20, 12, 31.5, 178, 35));
    assert.equal(pixel.length, 3);
    pixel.forEach((channel) => assert.ok(Number.isInteger(channel) && channel >= 0 && channel <= 255));
  }
});

test("throughput state distinguishes idle zero from warming, stale, and down", () => {
  assert.deepEqual({ ...ui.throughputViewState("active", 42) }, { state: "active", label: "LIVE" });
  assert.deepEqual({ ...ui.throughputViewState("idle", 0) }, { state: "idle", label: "IDLE" });
  assert.deepEqual({ ...ui.throughputViewState("warming", null) }, { state: "warming", label: "WARMING" });
  assert.deepEqual({ ...ui.throughputViewState("stale", null) }, { state: "stale", label: "STALE" });
  assert.deepEqual({ ...ui.throughputViewState("down", null) }, { state: "down", label: "DOWN" });
  assert.deepEqual({ ...ui.throughputViewState(null, 0) }, { state: "active", label: "LIVE" });
});

test("voice status normalization drops content and preserves diagnostic metadata", () => {
  assert.equal(ui.VOICE_POLL_MS, 750);
  const normalized = ui.normalizeVoiceStatus({
    ...voiceStatus({ state: "busy", stage: "openclaw", openclaw: "thinking" }),
    transcript: "private words",
    response: "private answer",
    openclaw_token: "private token",
  });

  assert.equal(normalized.device, "Cerberus");
  assert.equal(normalized.stage, "openclaw");
  assert.equal(normalized.openclaw.state, "thinking");
  assert.doesNotMatch(JSON.stringify(normalized), /private/);
});

test("voice view model covers every live pipeline stage and failure mode", () => {
  const now = Date.parse("2026-08-10T18:30:05Z");
  const cases = [
    ["listening", voiceStatus(), "ready", "idle", "LISTENING FOR CERBERUS"],
    ["armed", voiceStatus({ state: "armed", watchword: "armed" }), "armed", "armed", "LISTENING FOR CERBERUS"],
    ["triggered", voiceStatus({ state: "busy", stage: "watchword", watchword: "triggered" }), "busy", "active", "CHECKING WATCHWORD"],
    ["asr", voiceStatus({ state: "busy", stage: "asr", watchword: "triggered", asr: "processing" }), "busy", "active", "ASR TRANSCRIBING"],
    ["thinking", voiceStatus({ state: "busy", stage: "openclaw", watchword: "triggered", asr: "ok", openclaw: "thinking" }), "busy", "active", "OPENCLAW THINKING"],
    ["tts synth", voiceStatus({ state: "busy", stage: "tts_synthesis", watchword: "triggered", asr: "ok", openclaw: "ok", tts: "synthesizing", chunkIndex: 1, chunkTotal: 3 }), "busy", "active", "TTS SYNTHESIZING"],
    ["tts playback", voiceStatus({ state: "busy", stage: "tts_playback", watchword: "triggered", asr: "ok", openclaw: "ok", tts: "playing", chunkIndex: 2, chunkTotal: 3 }), "busy", "active", "PLAYING RESPONSE"],
    ["cooldown", voiceStatus({ state: "busy", stage: "cooldown", tts: "cooldown", chunkIndex: 3, chunkTotal: 3 }), "busy", "active", "MIC COOLDOWN"],
  ];

  for (const [label, payload, expectedState, expectedStep, expectedStage] of cases) {
    const view = ui.voiceViewModel(payload, now);
    assert.equal(view.state, expectedState, label);
    const step = label === "asr" ? view.steps.asr
      : label === "thinking" ? view.steps.openclaw
        : label === "tts synth" ? view.steps.tts
          : label === "tts playback" || label === "cooldown" ? view.steps.playback
          : view.steps.watchword;
    assert.equal(step.state, expectedStep, label);
    assert.equal(view.stageLabel, expectedStage, label);
  }

  const stale = ui.voiceViewModel(voiceStatus({ state: "stale", statusError: "stale" }), now);
  assert.equal(stale.state, "stale");
  assert.match(stale.error, /HEARTBEAT STALE/);
  assert.match(stale.detail, /FROZEN AT LISTENING FOR CERBERUS/);

  const down = ui.voiceViewModel(voiceStatus({ state: "down", statusError: "missing" }), now);
  assert.equal(down.state, "down");
  assert.match(down.error, /STATUS FILE MISSING/);
  assert.equal(down.detail, "VOICE PIPELINE UNAVAILABLE");

  const stopped = ui.voiceViewModel(voiceStatus({ state: "stopped", stage: "stopped" }), now);
  assert.equal(stopped.state, "down");
  assert.equal(stopped.error, "VOICE BRIDGE STOPPED");
  assert.equal(stopped.detail, "VOICE PIPELINE UNAVAILABLE");

  const asrPriority = ui.voiceViewModel(voiceStatus({
    state: "busy", stage: "asr", watchword: "checking", asr: "processing",
  }), now);
  assert.equal(asrPriority.steps.asr.state, "active");
  assert.equal(asrPriority.steps.watchword.state, "idle");
  assert.equal(asrPriority.steps.watchword.label, "WAIT");

  const failed = ui.voiceViewModel(voiceStatus({
    state: "degraded",
    stage: "retry_wait",
    lastError: { stage: "openclaw", error_type: "timeout", at: "2026-08-10T18:29:00Z" },
  }), now);
  assert.equal(failed.state, "error");
  assert.match(failed.error, /OPENCLAW · TIMEOUT/);

  const synthFailed = ui.voiceViewModel(voiceStatus({
    state: "degraded", stage: "retry_wait", tts: "error",
    lastError: { stage: "tts_synthesis", error_type: "timeout", at: "2026-08-10T18:29:00Z" },
  }), now);
  assert.equal(synthFailed.steps.tts.state, "error");
  assert.equal(synthFailed.steps.playback.state, "idle");

  const playbackFailed = ui.voiceViewModel(voiceStatus({
    state: "degraded", stage: "retry_wait", tts: "error",
    lastError: { stage: "tts_playback", error_type: "devicebusy", at: "2026-08-10T18:29:00Z" },
  }), now);
  assert.equal(playbackFailed.steps.tts.state, "complete");
  assert.equal(playbackFailed.steps.playback.state, "error");
});

test("voice duration formatting is compact and deterministic", () => {
  assert.equal(ui.formatVoiceDuration(null), "—");
  assert.equal(ui.formatVoiceDuration(1.21), "1.2S");
  assert.equal(ui.formatVoiceDuration(12.8), "13S");
  assert.equal(ui.formatVoiceDuration(125), "2M05S");
});

test("cluster state falls back to availability counts", () => {
  const full = ui.normalizePayload({ cluster: { available_hosts: 3, total_hosts: 3 } });
  const partial = ui.normalizePayload({ cluster: { available_hosts: 2, total_hosts: 3 } });
  const down = ui.normalizePayload({ cluster: { available_hosts: 0, total_hosts: 3 } });

  assert.equal(ui.inferredClusterState(full), "online");
  assert.equal(ui.inferredClusterState(partial), "degraded");
  assert.equal(ui.inferredClusterState(down), "offline");
});

function fakeElement() {
  return {
    className: "",
    dataset: {},
    hidden: false,
    textContent: "",
    title: "",
    attributes: {},
    setAttribute(name, value) { this.attributes[name] = String(value); },
  };
}

function installVoiceDom() {
  const ids = [
    "voice-card", "voice-state", "voice-stage", "voice-elapsed", "voice-detail",
    "voice-error", "voice-heartbeat", "voice-last-event",
  ];
  for (const stage of ["asr", "watchword", "openclaw", "tts", "playback"]) {
    ids.push(`voice-${stage}-step`, `voice-${stage}-state`);
  }
  const elements = Object.fromEntries(ids.map((id) => [id, fakeElement()]));
  context.document = {
    title: "",
    getElementById(id) { return elements[id]; },
    querySelector() { return null; },
  };
  return elements;
}

test("voice renderer exposes stage, chunk progress, and last failed stage", () => {
  const elements = installVoiceDom();
  ui.renderVoice(voiceStatus({
    state: "busy",
    stage: "tts_playback",
    watchword: "triggered",
    asr: "ok",
    openclaw: "ok",
    tts: "playing",
    chunkIndex: 2,
    chunkTotal: 4,
    lastError: { stage: "asr", error_type: "timeout", at: "2026-08-10T18:29:00Z" },
  }), Date.parse("2026-08-10T18:30:05Z"));

  assert.equal(elements["voice-card"].dataset.state, "busy");
  assert.equal(elements["voice-stage"].textContent, "PLAYING RESPONSE");
  assert.match(elements["voice-detail"].textContent, /CHUNK 2\/4/);
  assert.equal(elements["voice-tts-step"].dataset.state, "complete");
  assert.equal(elements["voice-tts-state"].textContent, "OK");
  assert.equal(elements["voice-playback-step"].dataset.state, "active");
  assert.equal(elements["voice-playback-state"].textContent, "PLAY");
  assert.equal(elements["voice-error"].hidden, false);
  assert.match(elements["voice-error"].textContent, /ASR · TIMEOUT/);
});

test("voice transport failure does not touch cluster status elements", () => {
  const elements = installVoiceDom();
  const untouchedCluster = fakeElement();
  untouchedCluster.textContent = "CLUSTER ONLINE";
  elements["cluster-state"] = untouchedCluster;
  ui.renderVoiceTransportError(new Error("connection refused"));
  assert.equal(elements["voice-card"].dataset.state, "down");
  assert.equal(elements["voice-state"].textContent, "LINK DOWN");
  assert.equal(elements["voice-playback-step"].dataset.state, "unknown");
  assert.equal(elements["cluster-state"].textContent, "CLUSTER ONLINE");
});

test("renderer exposes per-node values and honest API-wide token scope", () => {
  const ids = [
    "dashboard", "cluster-indicator", "cluster-state", "host-count", "sample-time",
    "sample-age", "connection-message",
  ];
  for (const metric of ["cpu", "gpu", "ram"]) {
    for (const slot of [1, 2, 3]) {
      const temperatureId = metric === "ram" ? "ram-soc" : `${metric}-temp`;
      ids.push(
        `${metric}-c${slot}-row`, `${metric}-c${slot}-value`,
        `${metric}-c${slot}-line`, `${metric}-c${slot}-dot`, `${metric}-c${slot}-chart`,
        `${temperatureId}-c${slot}-value`, `${temperatureId}-c${slot}-line`,
        `${temperatureId}-c${slot}-dot`, `${temperatureId}-c${slot}-chart`,
      );
    }
  }
  ids.push(
    "tokens-value", "tokens-range", "tokens-delta", "tokens-scale",
    "tokens-line", "tokens-area", "tokens-dot", "tokens-chart",
  );
  ids.push("tokens-state");
  for (const slot of [1, 2, 3]) {
    ids.push(
      `host-c${slot}`, `c${slot}-state`,
    );
  }
  const elements = Object.fromEntries(ids.map((id) => [id, fakeElement()]));
  const cards = Object.fromEntries(["cpu", "gpu", "ram", "tokens"].map((metric) => [metric, fakeElement()]));
  context.document = {
    title: "",
    getElementById(id) { return elements[id]; },
    querySelector(selector) {
      const match = selector.match(/^\[data-metric="(.+)"\]$/);
      return match ? cards[match[1]] : null;
    },
  };

  ui.render(statusPayload, Date.parse("2026-08-10T18:30:06Z"));

  assert.equal(elements.dashboard.dataset.connection, "online");
  assert.equal(elements["cluster-state"].textContent, "CLUSTER ONLINE");
  assert.equal(elements["host-count"].textContent, "3 / 3 NODES");
  assert.equal(elements["cpu-c1-value"].textContent, "21");
  assert.equal(elements["cpu-c2-value"].textContent, "32");
  assert.equal(elements["gpu-c3-value"].textContent, "68");
  assert.equal(elements["ram-c3-value"].textContent, "61");
  assert.equal(elements["cpu-temp-c1-value"].textContent, "48");
  assert.equal(elements["gpu-temp-c3-value"].textContent, "49");
  assert.equal(elements["ram-soc-c3-value"].textContent, "45");
  assert.equal(elements["ram-soc-c3-value"].dataset.available, "true");
  assert.equal(elements["tokens-value"].textContent, "145");
  assert.equal(elements["tokens-delta"].textContent, "API AGG · NOT PER NODE");
  assert.equal(elements["tokens-state"].textContent, "LIVE");
  assert.equal(elements["tokens-state"].dataset.state, "active");
  assert.equal(elements["ram-c3-value"].dataset.available, "true");
  assert.match(elements["cpu-c1-line"].attributes.d, /^M/);
  assert.match(elements["cpu-temp-c1-line"].attributes.d, /^M/);
  assert.match(elements["ram-soc-c1-line"].attributes.d, /^M/);
  assert.match(elements["tokens-line"].attributes.d, /^M/);
  assert.equal(cards.cpu.dataset.availableNodes, "3");
  assert.equal(cards.tokens.dataset.state, "ready");
  assert.equal(cards.tokens.dataset.throughputState, "active");
  assert.match(cards.tokens.title, /no per-node attribution/i);
});

test("static shell is self-contained and contains the exact rack-display regions", () => {
  const html = fs.readFileSync(path.join(staticDirectory, "index.html"), "utf8");
  const css = fs.readFileSync(path.join(staticDirectory, "style.css"), "utf8");

  assert.match(html, /href="\/style\.css"/);
  assert.match(html, /src="\/app\.js"/);
  assert.doesNotMatch(`${html}\n${css}`, /https?:\/\//);
  assert.equal((html.match(/class="metric-card /g) || []).length, 5);
  assert.equal((html.match(/class="trace-row /g) || []).length, 9);
  assert.equal((html.match(/class="host-state"/g) || []).length, 3);
  assert.match(html, /id="tokens-state"/);
  assert.match(html, /id="voice-card"/);
  assert.equal((html.match(/class="voice-step"/g) || []).length, 5);
  assert.ok(html.indexOf('id="voice-asr-step"') < html.indexOf('id="voice-watchword-step"'));
  assert.ok(html.indexOf('id="voice-watchword-step"') < html.indexOf('id="voice-openclaw-step"'));
  assert.ok(html.indexOf('id="voice-openclaw-step"') < html.indexOf('id="voice-tts-step"'));
  assert.ok(html.indexOf('id="voice-tts-step"') < html.indexOf('id="voice-playback-step"'));
  assert.match(html, /id="ambient-canvas"[^>]+width="178"[^>]+height="35"/);
  assert.equal((html.match(/TEMP °C/g) || []).length, 3);
  assert.equal((html.match(/class="mini-chart mini-chart--temp"/g) || []).length, 9);
  assert.match(html, /SOC TEMP · NO LPDDR SENSOR/);
  assert.doesNotMatch(html, /RAM TEMP/);
  assert.equal((html.match(/SoC temperature/g) || []).length, 6);
  assert.doesNotMatch(html, /aria-label="[^"]*RAM temperature/);
  assert.doesNotMatch(html, /CLUSTER AVG/);
  assert.match(html, /C1\+C2 MODEL AGGREGATE/);
  assert.match(html, /NOT PER NODE/);
  assert.match(css, /grid-template-columns:\s*repeat\(3, minmax\(0, 1fr\)\) minmax\(0, \.92fr\) minmax\(0, 1\.16fr\)/);
  assert.match(css, /grid-template-columns:\s*repeat\(5, minmax\(0, 1fr\)\)/);
  assert.match(css, /grid-template-columns:\s*29px 24px 7px minmax\(28px, \.85fr\) 27px 11px minmax\(28px, \.85fr\)/);
  assert.match(css, /height:\s*100dvh/);
  assert.match(css, /grid-template-rows:\s*34px minmax\(0, 1fr\) 17px/);
  assert.match(css, /image-rendering:\s*pixelated/);
  assert.match(css, /overflow:\s*hidden/);
  assert.match(html, /aria-live="polite"/);
  assert.match(html, /aria-live="assertive"/);
  assert.match(html, />CERBERUS</);
  assert.doesNotMatch(html, />CEREBRUS</);
});
