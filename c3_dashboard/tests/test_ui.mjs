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
  pipeline = null,
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
    ...(pipeline ? { pipeline } : {}),
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

test("ambient renderer rotates six scenes, crossfades, and stays bounded", () => {
  assert.equal(ui.AMBIENT_SCENE_MS, 30000);
  assert.equal(ui.AMBIENT_FRAME_MS, 1000);
  assert.equal(ui.ambientSceneAt(0), 0);
  assert.equal(ui.ambientSceneAt(29999), 0);
  assert.equal(ui.ambientSceneAt(30000), 1);
  assert.equal(ui.ambientSceneAt(150000), 5);
  assert.equal(ui.ambientSceneAt(180000), 0);

  const transition = ui.ambientFrameAt(29000, false);
  assert.equal(transition.scene, 0);
  assert.equal(transition.nextScene, 1);
  assert.ok(transition.mix > 0 && transition.mix < 1);
  assert.equal(ui.ambientFrameAt(29000, true).mix, 0);

  const offsets = Array.from({ length: 9 }, (_, phase) => ({ ...ui.burnInOffset(phase) }));
  assert.equal(new Set(offsets.map(({ x, y }) => `${x},${y}`)).size, 9);
  for (const mode of ["normal", "voice", "degraded", "critical"]) {
    for (let scene = 0; scene < 6; scene += 1) {
      const pixel = Array.from(ui.ambientPixel(scene, 20, 12, 31.5, 178, 35, mode));
      assert.equal(pixel.length, 3);
      pixel.forEach((channel) => assert.ok(Number.isInteger(channel) && channel >= 0 && channel <= 255));
    }
  }

  assert.equal(ui.ambientDisplayMode("online", "ready"), "normal");
  assert.equal(ui.ambientDisplayMode("online", "busy"), "voice");
  assert.equal(ui.ambientDisplayMode("online", "down"), "degraded");
  assert.equal(ui.ambientDisplayMode("offline", "busy"), "critical");
});

test("ambient painter reuses its image buffer and follows live health state", () => {
  let allocations = 0;
  const writes = [];
  const context2d = {
    createImageData(width, height) {
      allocations += 1;
      return { data: new Uint8ClampedArray(width * height * 4) };
    },
    putImageData(image) { writes.push(image); },
  };
  const canvas = { width: 12, height: 4, getContext() { return context2d; } };
  const dashboard = {
    dataset: { connection: "online", voiceState: "busy" },
    style: { values: {}, setProperty(key, value) { this.values[key] = value; } },
  };
  context.document = {
    hidden: false,
    getElementById(id) {
      return { dashboard }[id] || null;
    },
  };

  ui.paintAmbient(canvas, 0, { reducedMotion: false });
  ui.paintAmbient(canvas, 1000, { reducedMotion: false });
  assert.equal(allocations, 1);
  assert.equal(writes.length, 2);
  assert.equal(writes[0], writes[1]);
  assert.equal(dashboard.dataset.ambientMode, "voice");

  dashboard.dataset.connection = "offline";
  ui.paintAmbient(canvas, 2000, { reducedMotion: true });
  assert.equal(allocations, 1);
  assert.equal(dashboard.dataset.ambientMode, "critical");
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
    ...voiceStatus({
      state: "busy",
      stage: "openclaw",
      openclaw: "thinking",
      pipeline: {
        source: "producer",
        active: true,
        mode: "request",
        request_id: "must-not-survive",
        steps: {
          heard_name: "complete", asr: "complete", openclaw: "active",
          tts: "idle", play: "idle", private_step: "secret",
        },
      },
    }),
    transcript: "private words",
    response: "private answer",
    openclaw_token: "private token",
  });

  assert.equal(normalized.device, "Cerberus");
  assert.equal(normalized.stage, "openclaw");
  assert.equal(normalized.openclaw.state, "thinking");
  assert.equal(normalized.pipeline.source, "producer");
  assert.deepEqual(
    { ...normalized.pipeline.steps },
    { heard_name: "complete", asr: "complete", openclaw: "active", tts: "idle", play: "idle" },
  );
  assert.doesNotMatch(JSON.stringify(normalized), /private/);
  assert.doesNotMatch(JSON.stringify(normalized), /request_id|must-not-survive|secret/);
  const unknownSource = ui.normalizeVoiceStatus(voiceStatus({
    pipeline: {
      source: "attacker-controlled-label",
      active: false,
      mode: "idle",
      steps: { heard_name: "idle", asr: "idle", openclaw: "idle", tts: "idle", play: "idle" },
    },
  }));
  assert.equal(unknownSource.pipeline.source, "unknown");
  assert.doesNotMatch(JSON.stringify(unknownSource), /attacker/);
});

test("voice background progress is ordered and prefers the producer contract", () => {
  assert.deepEqual(Array.from(ui.VOICE_PROGRESS_ORDER), ["heard_name", "asr", "openclaw", "tts", "play"]);
  const explicit = ui.voiceProgressModel(voiceStatus({
    state: "busy",
    stage: "openclaw",
    pipeline: {
      source: "producer",
      active: true,
      mode: "request",
      steps: {
        heard_name: "complete", asr: "complete", openclaw: "active",
        tts: "idle", play: "idle",
      },
    },
  }), Date.parse("2026-08-10T18:30:05Z"));
  assert.equal(explicit.source, "producer");
  assert.equal(explicit.active, true);
  assert.deepEqual(
    Array.from(ui.VOICE_PROGRESS_ORDER, (name) => explicit.steps[name]),
    ["complete", "complete", "active", "idle", "idle"],
  );

  const legacyPlayback = ui.voiceProgressModel(voiceStatus({
    state: "busy", stage: "tts_playback", watchword: "triggered",
    asr: "ok", openclaw: "ok", tts: "playing",
  }), Date.parse("2026-08-10T18:30:05Z"));
  assert.equal(legacyPlayback.source, "derived");
  assert.deepEqual(
    Array.from(ui.VOICE_PROGRESS_ORDER, (name) => legacyPlayback.steps[name]),
    ["complete", "complete", "complete", "complete", "active"],
  );

  const persistentError = {
    state: "ready", stage: "listening",
    steps: { heard_name: "complete", asr: "error", openclaw: "idle", tts: "idle", play: "idle" },
  };
  assert.equal(ui.voiceTroubleSignature(persistentError), "error:asr");
  assert.equal(ui.voiceTroubleSignature({ ...persistentError, heartbeat: 999 }), "error:asr");
  assert.equal(ui.voiceTroubleSignature({
    ...persistentError,
    steps: { ...persistentError.steps, asr: "complete", tts: "error" },
  }), "error:tts");
  assert.equal(ui.voiceTroubleSignature({ ...persistentError, steps: {} }), null);
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
  assert.equal(ui.formatVoiceDuration(31500), "8H45M");
  assert.equal(ui.formatVoiceDuration(187200), "2D04H");
  const listening = ui.voiceViewModel({
    ...voiceStatus(),
    stage_elapsed_seconds: 31500,
  }, Date.parse("2026-08-10T18:30:05Z"));
  assert.equal(listening.elapsedLabel, "—");
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
    getAttribute(name) { return this.attributes[name] ?? null; },
  };
}

function installVoiceDom() {
  const ids = ["dashboard", "voice-progress"];
  for (const stage of ["heard-name", "asr", "openclaw", "tts", "play"]) ids.push(`voice-${stage}-step`);
  const elements = Object.fromEntries(ids.map((id) => [id, fakeElement()]));
  context.document = {
    title: "",
    getElementById(id) { return elements[id]; },
    querySelector() { return null; },
  };
  return elements;
}

test("voice renderer paints the five background bands without a subdashboard", () => {
  const elements = installVoiceDom();
  let ariaWrites = 0;
  const originalSetAttribute = elements["voice-progress"].setAttribute;
  elements["voice-progress"].setAttribute = function setAttribute(name, value) {
    if (name === "aria-label") ariaWrites += 1;
    originalSetAttribute.call(this, name, value);
  };
  const payload = voiceStatus({
    state: "busy",
    stage: "tts_playback",
    pipeline: {
      source: "producer",
      active: true,
      mode: "responding",
      steps: {
        heard_name: "complete", asr: "complete", openclaw: "complete",
        tts: "complete", play: "active",
      },
    },
  });
  ui.renderVoice(payload, Date.parse("2026-08-10T18:30:05Z"));
  ui.renderVoice(payload, Date.parse("2026-08-10T18:30:06Z"));

  assert.equal(elements["voice-progress"].dataset.state, "busy");
  assert.equal(elements["voice-progress"].dataset.active, "true");
  assert.equal(elements["voice-progress"].dataset.source, "producer");
  assert.equal(elements.dashboard.dataset.voiceState, "busy");
  assert.equal(elements.dashboard.dataset.voiceStage, "tts_playback");
  assert.equal(elements["voice-heard-name-step"].dataset.state, "complete");
  assert.equal(elements["voice-tts-step"].dataset.state, "complete");
  assert.equal(elements["voice-play-step"].dataset.state, "active");
  assert.match(elements["voice-progress"].attributes["aria-label"], /^Voice pipeline busy at tts_playback: HEARD NAME complete.*ASR complete.*CLAW complete.*TTS complete.*PLAY active/);
  assert.equal(ariaWrites, 1, "unchanged 750 ms polls must not repeat live-region announcements");
});

test("voice transport failure clears progress without touching cluster status", () => {
  const elements = installVoiceDom();
  const untouchedCluster = fakeElement();
  untouchedCluster.textContent = "CLUSTER ONLINE";
  elements["cluster-state"] = untouchedCluster;
  ui.renderVoiceTransportError(new Error("connection refused"));
  assert.equal(elements["voice-progress"].dataset.state, "down");
  assert.equal(elements["voice-progress"].dataset.active, "false");
  assert.equal(elements["voice-progress"].dataset.source, "unavailable");
  assert.equal(elements["voice-play-step"].dataset.state, "unknown");
  assert.equal(elements.dashboard.dataset.voiceState, "down");
  assert.equal(elements["cluster-state"].textContent, "CLUSTER ONLINE");
});

test("voice live region announces safe stale and failure state without error content", () => {
  const ids = ["dashboard", "voice-progress"];
  for (const name of ui.VOICE_PROGRESS_ORDER) ids.push(`voice-${name.replace("_", "-")}-step`);
  const elements = Object.fromEntries(ids.map((id) => [id, fakeElement()]));
  context.document = { getElementById(id) { return elements[id] || null; } };
  const privateError = "PRIVATE transcript and upstream response";

  ui.renderVoice({
    overall: { state: "ready", stage: "listening" },
    status_error: "stale",
    last_error: { stage: "asr", type: "TimeoutError", message: privateError },
    pipeline: {
      source: "producer", active: false, mode: "error",
      steps: { heard_name: "complete", asr: "error", openclaw: "complete", tts: "complete", play: "complete" },
    },
  }, Date.now());

  const announcement = elements["voice-progress"].attributes["aria-label"];
  assert.match(announcement, /^Voice pipeline stale at listening; failure reported:/);
  assert.match(announcement, /ASR error/);
  assert.doesNotMatch(announcement, /PRIVATE|transcript|upstream|response|TimeoutError/);
});

test("infrequent TFT black sweep timing and overlay state are deterministic", () => {
  assert.equal(ui.SAVER_IDLE_MS, 300000);
  assert.equal(ui.SAVER_BAND_PX, 48);
  assert.equal(ui.SAVER_REPEAT_MS, 1800000);
  assert.equal(ui.SAVER_SWEEP_MS, 3200);
  const rowBlackSeconds = ui.SAVER_BAND_PX
    / ((280 + ui.SAVER_BAND_PX) / (ui.SAVER_SWEEP_MS / 1000));
  assert.ok(rowBlackSeconds > 0.45 && rowBlackSeconds < 0.5);
  assert.ok(rowBlackSeconds / 0.05 > 9, "each row gets >9 specified panel response times");
  assert.equal(ui.saverStateAt(1000, 1000, false), "awake");
  assert.equal(ui.saverStateAt(300999, 1000, false), "awake");
  assert.equal(ui.saverStateAt(301000, 1000, false), "sweep");
  assert.equal(ui.saverStateAt(2100999, 1000, false, 301000), "awake");
  assert.equal(ui.saverStateAt(2101000, 1000, false, 301000), "sweep");
  assert.equal(ui.saverStateAt(999999, 1000, true), "awake");
  assert.equal(ui.saverStateAt(900, 1000, false), "awake");
  assert.equal(ui.saverTroubleTransition(null, null), false);
  assert.equal(ui.saverTroubleTransition(null, "offline:0"), true, "failure onset wakes");
  assert.equal(ui.saverTroubleTransition("offline:0", "offline:0"), false, "unchanged failure does not reset idle time");
  assert.equal(ui.saverTroubleTransition("offline:0", "degraded:2"), true, "meaningful change wakes");
  assert.equal(ui.saverTroubleTransition("degraded:2", null), true, "recovery wakes");
  assert.equal(ui.voiceActivityNeedsAttention({
    active: true,
    steps: { heard_name: "idle", asr: "active", openclaw: "idle", tts: "idle", play: "idle" },
  }), false, "unrecognized room speech must not starve panel care");
  assert.equal(ui.voiceActivityNeedsAttention({
    active: true,
    steps: { heard_name: "complete", asr: "active", openclaw: "idle", tts: "idle", play: "idle" },
  }), true, "an armed follow-up is meaningful activity");
  assert.equal(ui.voiceActivityNeedsAttention({
    active: true,
    steps: { heard_name: "complete", asr: "complete", openclaw: "active", tts: "idle", play: "idle" },
  }), true, "Claw work keeps the dashboard awake");
  assert.equal(ui.voiceActivityNeedsAttention({
    active: false,
    steps: { heard_name: "complete", asr: "complete", openclaw: "complete", tts: "complete", play: "complete" },
  }), false, "a completed historical turn is not ongoing activity");

  const firstEndpointFailure = ui.normalizePayload({
    hosts: {
      cerberus1: { state: "online" },
      cerberus2: { state: "offline", error: "private failure one" },
      cerberus3: { state: "online" },
    },
  });
  const replacementFailure = ui.normalizePayload({
    hosts: {
      cerberus1: { state: "offline", error: "private failure two" },
      cerberus2: { state: "online" },
      cerberus3: { state: "online" },
    },
  });
  const firstSignature = ui.clusterTroubleSignature("degraded", 2, firstEndpointFailure, false);
  const replacementSignature = ui.clusterTroubleSignature("degraded", 2, replacementFailure, false);
  assert.notEqual(firstSignature, replacementSignature, "same-count endpoint replacement must wake");
  assert.equal(ui.saverTroubleTransition(firstSignature, replacementSignature), true);
  assert.doesNotMatch(`${firstSignature}${replacementSignature}`, /private|failure/);
  const allOnline = ui.normalizePayload({
    hosts: {
      cerberus1: { state: "online" }, cerberus2: { state: "online" }, cerberus3: { state: "online" },
    },
  });
  assert.equal(ui.clusterTroubleSignature("online", 3, allOnline, false), null);

  const dashboard = fakeElement();
  const overlay = fakeElement();
  context.document = {
    hidden: false,
    getElementById(id) { return { dashboard, "lcd-refresh-sweep": overlay }[id] || null; },
    addEventListener() {},
  };
  ui.setSaverActive(true);
  assert.equal(overlay.hidden, false);
  assert.equal(overlay.dataset.active, "true");
  assert.equal(dashboard.dataset.saverState, "sweeping");
  ui.setSaverActive(false);
  assert.equal(overlay.hidden, true);
  assert.equal(overlay.dataset.active, "false");
  assert.equal(dashboard.dataset.saverState, "awake");
  context.document.hidden = true;
  assert.equal(ui.startSaverSweep(301000), false, "hidden pages never count an off-screen sweep");
  assert.equal(overlay.hidden, true);
  context.document.hidden = false;
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

  ui.renderTransportError(new Error("connection refused"));
  assert.equal(elements.dashboard.dataset.connection, "error");
  assert.equal(elements["cluster-state"].textContent, "DATA LINK LOST");
  assert.equal(elements["host-count"].textContent, "LAST 3 / 3 NODES");
  assert.equal(elements["cpu-c1-row"].dataset.state, "stale");
  assert.equal(elements["host-c1"].dataset.state, "stale");
  assert.match(elements["c1-state"].textContent, /^STALE/);
  assert.equal(elements["tokens-state"].dataset.state, "stale");
  assert.equal(elements["tokens-state"].textContent, "STALE");
  assert.equal(cards.cpu.dataset.freshnessState, "stale");
  assert.equal(cards.tokens.dataset.freshnessState, "stale");
  assert.match(cards.cpu.title, /retained values/i);

  ui.render(statusPayload, Date.parse("2026-08-10T18:30:07Z"));
  assert.equal(cards.cpu.dataset.freshnessState, undefined);
  assert.equal(cards.tokens.dataset.freshnessState, undefined);
  assert.equal(elements["host-c1"].dataset.state, "online");
  assert.equal(elements["tokens-state"].textContent, "LIVE");

  ui.render(statusPayload, Date.parse("2026-08-10T18:31:05Z"));
  assert.equal(elements.dashboard.dataset.connection, "degraded");
  assert.equal(elements["cluster-state"].textContent, "TELEMETRY STALE");
  assert.equal(elements["host-count"].textContent, "LAST 3 / 3 NODES");
  assert.equal(elements["host-c2"].dataset.state, "stale");
  assert.equal(cards.ram.dataset.freshnessState, "stale");
  assert.equal(elements["tokens-state"].textContent, "STALE");
  assert.match(elements["connection-message"].textContent, /LATEST SAMPLE IS 60S OLD/);
});

test("static shell is self-contained and contains the exact rack-display regions", () => {
  const html = fs.readFileSync(path.join(staticDirectory, "index.html"), "utf8");
  const css = fs.readFileSync(path.join(staticDirectory, "style.css"), "utf8");

  assert.match(html, /href="\/style\.css"/);
  assert.match(html, /src="\/app\.js"/);
  assert.doesNotMatch(`${html}\n${css}`, /https?:\/\//);
  assert.equal((html.match(/class="metric-card /g) || []).length, 4);
  assert.equal((html.match(/class="trace-row /g) || []).length, 9);
  assert.equal((html.match(/class="host-state"/g) || []).length, 3);
  assert.match(html, /id="tokens-state"/);
  assert.doesNotMatch(html, /id="voice-card"|metric-card--voice|VOICE AGENT/);
  assert.equal((html.match(/class="voice-progress-step"/g) || []).length, 5);
  assert.ok(html.indexOf('id="voice-heard-name-step"') < html.indexOf('id="voice-asr-step"'));
  assert.ok(html.indexOf('id="voice-asr-step"') < html.indexOf('id="voice-openclaw-step"'));
  assert.ok(html.indexOf('id="voice-openclaw-step"') < html.indexOf('id="voice-tts-step"'));
  assert.ok(html.indexOf('id="voice-tts-step"') < html.indexOf('id="voice-play-step"'));
  assert.match(html, />HEARD NAME</);
  assert.match(html, /id="ambient-canvas"[^>]+width="178"[^>]+height="35"/);
  assert.match(html, /data-ambient-mode="degraded"/);
  assert.equal((html.match(/TEMP °C/g) || []).length, 3);
  assert.equal((html.match(/class="mini-chart mini-chart--temp"/g) || []).length, 9);
  assert.match(html, /SOC TEMP · NO LPDDR SENSOR/);
  assert.doesNotMatch(html, /RAM TEMP/);
  assert.equal((html.match(/SoC temperature/g) || []).length, 6);
  assert.doesNotMatch(html, /aria-label="[^"]*RAM temperature/);
  assert.doesNotMatch(html, /CLUSTER AVG/);
  assert.match(html, /C1\+C2 MODEL AGGREGATE/);
  assert.match(html, /NOT PER NODE/);
  assert.match(css, /\.main-grid[\s\S]*?grid-template-columns:\s*repeat\(4, minmax\(0, 1fr\)\)/);
  assert.match(css, /grid-template-columns:\s*repeat\(5, minmax\(0, 1fr\)\)/);
  assert.match(css, /\.voice-progress\[data-state="down"\]::after\s*\{\s*content:\s*"VOICE LINK DOWN"/);
  assert.match(css, /\.voice-progress\[data-state="stale"\]::after\s*\{\s*content:\s*"VOICE STATUS STALE"/);
  assert.match(css, /grid-template-columns:\s*29px 24px 7px minmax\(28px, \.85fr\) 27px 11px minmax\(28px, \.85fr\)/);
  assert.match(css, /height:\s*100dvh/);
  assert.match(css, /grid-template-rows:\s*34px minmax\(0, 1fr\) 17px/);
  assert.match(css, /image-rendering:\s*pixelated/);
  assert.match(css, /contain:\s*strict/);
  assert.doesNotMatch(css, /filter:\s*saturate/);
  assert.match(css, /overflow:\s*hidden/);
  assert.match(html, /<div id="lcd-refresh-sweep" class="lcd-refresh-sweep" hidden aria-hidden="true"><\/div>/);
  assert.match(css, /@keyframes lcd-refresh-sweep[\s\S]*?translate3d\(0, -100%, 0\)[\s\S]*?translate3d\(0, 100vh, 0\)/);
  assert.match(css, /\.lcd-refresh-sweep:not\(\[hidden\]\)[\s\S]*?z-index:\s*2147483647/);
  assert.match(css, /\.lcd-refresh-sweep:not\(\[hidden\]\)[\s\S]*?height:\s*48px/);
  assert.match(css, /\.lcd-refresh-sweep:not\(\[hidden\]\)[\s\S]*?background-color:\s*#000 !important/);
  assert.match(css, /\.lcd-refresh-sweep:not\(\[hidden\]\)[\s\S]*?animation:\s*lcd-refresh-sweep 3\.2s linear both/);
  assert.match(css, /\.lcd-refresh-sweep:not\(\[hidden\]\)[\s\S]*?pointer-events:\s*none/);
  assert.doesNotMatch(css, /data-saver-state="black"/);
  assert.match(html, /aria-live="polite"/);
  assert.match(html, /aria-live="assertive"/);
  assert.match(html, />CERBERUS</);
  assert.doesNotMatch(html, />CEREBRUS</);
});
