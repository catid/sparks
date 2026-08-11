(function (global) {
  "use strict";

  const API_URL = "/api/status";
  const VOICE_API_URL = "/api/voice-status";
  const POLL_MS = 5000;
  const VOICE_POLL_MS = 750;
  const MAX_HISTORY_POINTS = 60;
  const NODE_SLOTS = [1, 2, 3];
  const AMBIENT_SCENE_MS = 30000;
  // One intentionally chunky frame per second is enough for a pixel-art panel.
  // WebKit otherwise repaints the entire software-scaled 1424x280 canvas.
  const AMBIENT_FRAME_MS = 1000;
  const AMBIENT_TRANSITION_MS = 4000;
  const AMBIENT_SCENES = 6;
  const AMBIENT_NODE_CENTERS = [0.18, 0.5, 0.82];
  const AMBIENT_NODE_PALETTES = [[40, 132, 170], [146, 48, 112], [77, 150, 44]];
  const SAVER_IDLE_MS = 5 * 60 * 1000;
  // The DP-0101 TFT is specified at a relatively slow 50 ms response time.
  // A 3.2 s, 48 px sweep gives every 280 px panel row roughly 0.47 s at
  // exact black (more than nine response-time constants), then stays out of
  // the way for 30 minutes. Response time only bounds visible transition; the
  // cadence is a low-disruption heuristic, not an OLED pixel-refresh cycle.
  const SAVER_BAND_PX = 48;
  const SAVER_REPEAT_MS = 30 * 60 * 1000;
  const SAVER_SWEEP_MS = 3200;
  const VOICE_PROGRESS_ORDER = ["heard_name", "asr", "openclaw", "tts", "play"];
  const VOICE_PROGRESS_LABELS = {
    heard_name: "HEARD NAME", asr: "ASR", openclaw: "CLAW", tts: "TTS", play: "PLAY",
  };
  const METRICS = {
    cpu: { field: "cpu_percent", label: "CPU utilization", temperatureField: "cpu_temperature_c", temperatureId: "cpu-temp", temperatureLabel: "CPU temperature" },
    gpu: { field: "gpu_percent", label: "GPU utilization", temperatureField: "gpu_temperature_c", temperatureId: "gpu-temp", temperatureLabel: "GPU temperature" },
    ram: { field: "ram_percent", label: "RAM utilization", temperatureField: "soc_temperature_c", temperatureId: "ram-soc", temperatureLabel: "SoC temperature" },
  };

  let pollTimer = null;
  let voicePollTimer = null;
  let ambientTimer = null;
  let lastPayload = null;
  let ambientSurface = null;
  let ambientMotionQuery = null;
  let ambientLifecycleBound = false;
  let saverTimer = null;
  let saverFinishTimer = null;
  let saverLastAttentionMs = null;
  let saverLastSweepMs = null;
  let saverActive = false;
  let saverLifecycleBound = false;
  const saverAttentionSignatures = { cluster: null, voice: null };

  const byId = (id) => document.getElementById(id);

  function finiteNumber(value) {
    if (value === null || value === undefined || value === "" || typeof value === "boolean") return null;
    const number = Number(value);
    return Number.isFinite(number) ? number : null;
  }

  function safeObject(value) {
    return value && typeof value === "object" && !Array.isArray(value) ? value : {};
  }

  function clamp(value, min, max) {
    return Math.min(max, Math.max(min, value));
  }

  function hostSlot(key, host, fallbackIndex) {
    const candidates = [key, host.id, host.name, host.hostname, host.reported_hostname]
      .filter((value) => value !== null && value !== undefined)
      .map(String);
    for (const candidate of candidates) {
      const match = candidate.match(/(?:^|[^a-z0-9])(?:c|cerberus|cerebrus|spark)[-_ ]?([123])(?:[^0-9]|$)/i)
        || candidate.match(/^(?:c|spark|cerberus|cerebrus)?[-_ ]?([123])$/i)
        || candidate.match(/([123])$/);
      if (match) return Number(match[1]);
    }
    return fallbackIndex >= 0 && fallbackIndex < 3 ? fallbackIndex + 1 : null;
  }

  function normalizeState(value) {
    const state = String(value || "").trim().toLowerCase();
    if (["online", "up", "healthy", "ok", "ready", "serving", "active"].includes(state)) return "online";
    if (["degraded", "partial", "warning", "warn", "stale", "starting", "recovering"].includes(state)) return "degraded";
    if (["offline", "down", "unavailable", "unreachable", "failed", "error", "dead"].includes(state)) return "offline";
    return "unknown";
  }

  function throughputViewState(value, tokensPerSecond) {
    const state = String(value || "").trim().toLowerCase();
    const models = {
      active: { state: "active", label: "LIVE" },
      idle: { state: "idle", label: "IDLE" },
      warming: { state: "warming", label: "WARMING" },
      stale: { state: "stale", label: "STALE" },
      down: { state: "down", label: "DOWN" },
    };
    if (models[state]) return models[state];
    return finiteNumber(tokensPerSecond) === null
      ? { state: "unknown", label: "UNKNOWN" }
      : { state: "active", label: "LIVE" };
  }

  function normalizePayload(raw) {
    const source = safeObject(raw);
    const clusterSource = safeObject(source.cluster);
    const throughputSource = safeObject(source.throughput);
    const hostsSource = safeObject(source.hosts);
    const occupied = new Set();
    const hosts = [];

    Object.entries(hostsSource).forEach(([key, rawHost], index) => {
      const host = safeObject(rawHost);
      let slot = hostSlot(key, host, index);
      if (slot === null || occupied.has(slot)) {
        slot = NODE_SLOTS.find((candidate) => !occupied.has(candidate)) || null;
      }
      if (slot !== null) occupied.add(slot);
      hosts.push({
        key,
        slot,
        name: host.name || key,
        state: host.state,
        error: host.error === null || host.error === undefined ? "" : String(host.error),
        cpu_percent: finiteNumber(host.cpu_percent),
        gpu_percent: finiteNumber(host.gpu_percent),
        ram_percent: finiteNumber(host.ram_percent),
        cpu_temperature_c: finiteNumber(host.cpu_temperature_c),
        gpu_temperature_c: finiteNumber(host.gpu_temperature_c),
        ram_temperature_c: finiteNumber(host.ram_temperature_c),
        soc_temperature_c: finiteNumber(host.soc_temperature_c),
        ram_used_bytes: finiteNumber(host.ram_used_bytes),
        ram_total_bytes: finiteNumber(host.ram_total_bytes),
        age_seconds: finiteNumber(host.age_seconds),
      });
    });
    hosts.sort((a, b) => (a.slot || 99) - (b.slot || 99));

    const history = Array.isArray(source.history)
      ? source.history.filter((point) => point && typeof point === "object").slice(-MAX_HISTORY_POINTS)
      : [];

    return {
      generated_at: source.generated_at || null,
      interval_seconds: finiteNumber(source.interval_seconds) || POLL_MS / 1000,
      cluster: {
        state: clusterSource.state,
        available_hosts: finiteNumber(clusterSource.available_hosts),
        total_hosts: finiteNumber(clusterSource.total_hosts),
      },
      throughput: {
        state: throughputSource.state,
        tokens_per_second: finiteNumber(throughputSource.tokens_per_second),
        age_seconds: finiteNumber(throughputSource.age_seconds),
        source: throughputSource.source || null,
      },
      hosts,
      history,
    };
  }

  function hostAtSlot(payload, slot) {
    return payload.hosts.find((host) => host.slot === slot) || null;
  }

  function historyHostAtSlot(point, slot) {
    const hosts = safeObject(point.hosts);
    const entries = Object.entries(hosts);
    for (let index = 0; index < entries.length; index += 1) {
      const [key, rawHost] = entries[index];
      const host = safeObject(rawHost);
      if (hostSlot(key, host, index) === slot) return host;
    }
    return null;
  }

  function hostFieldSeries(payload, field, slot) {
    const values = payload.history.slice(-MAX_HISTORY_POINTS).map((point) => {
      const host = historyHostAtSlot(point, slot);
      return host ? finiteNumber(host[field]) : null;
    });
    const currentHost = hostAtSlot(payload, slot);
    const current = currentHost ? finiteNumber(currentHost[field]) : null;
    if (!values.length) {
      if (current !== null) values.push(current);
    } else {
      values[values.length - 1] = current;
    }
    return values;
  }

  function hostMetricSeries(payload, metric, slot) {
    return hostFieldSeries(payload, METRICS[metric].field, slot);
  }

  function hostTemperatureSeries(payload, metric, slot) {
    return hostFieldSeries(payload, METRICS[metric].temperatureField, slot);
  }

  function tokenSeries(payload) {
    const values = payload.history.slice(-MAX_HISTORY_POINTS).map((point) => (
      finiteNumber(safeObject(point.throughput).tokens_per_second)
    ));
    const current = finiteNumber(payload.throughput.tokens_per_second);
    if (!values.length) {
      if (current !== null) values.push(current);
    } else {
      values[values.length - 1] = current;
    }
    return values;
  }

  function niceCeiling(value) {
    const number = finiteNumber(value);
    if (number === null || number <= 0) return 10;
    const exponent = 10 ** Math.floor(Math.log10(number));
    const fraction = number / exponent;
    const steps = [1, 1.25, 1.5, 2, 2.5, 3, 4, 5, 7.5, 10];
    const step = steps.find((candidate) => fraction <= candidate) || 10;
    return step * exponent;
  }

  function sparklinePaths(values, options) {
    const settings = options || {};
    const width = finiteNumber(settings.width) || 220;
    const height = finiteNumber(settings.height) || 42;
    const padding = finiteNumber(settings.padding) ?? 3;
    const min = finiteNumber(settings.min) ?? 0;
    const maxCandidate = finiteNumber(settings.max);
    const max = maxCandidate !== null && maxCandidate > min ? maxCandidate : min + 1;
    const clean = values.map(finiteNumber);
    const denominator = Math.max(1, clean.length - 1);
    const bottom = height - padding;
    const xAt = (index) => clean.length === 1
      ? width - padding
      : padding + ((width - padding * 2) * index / denominator);
    const yAt = (value) => padding + (height - padding * 2)
      * (1 - (clamp(value, min, max) - min) / (max - min));
    const point = (x, y) => `${x.toFixed(2)},${y.toFixed(2)}`;
    const lineParts = [];
    const areaParts = [];
    let segment = [];

    function flushSegment() {
      if (!segment.length) return;
      lineParts.push(segment.map((item, index) => `${index ? "L" : "M"}${point(item.x, item.y)}`).join(" "));
      areaParts.push(`M${point(segment[0].x, bottom)} L${segment.map((item) => point(item.x, item.y)).join(" L")} L${point(segment[segment.length - 1].x, bottom)} Z`);
      segment = [];
    }

    clean.forEach((value, index) => {
      if (value === null) {
        flushSegment();
      } else {
        segment.push({ x: xAt(index), y: yAt(value) });
      }
    });
    flushSegment();

    let latest = null;
    for (let index = clean.length - 1; index >= 0; index -= 1) {
      if (clean[index] !== null) {
        latest = { x: xAt(index), y: yAt(clean[index]) };
        break;
      }
    }
    return { line: lineParts.join(" "), area: areaParts.join(" "), latest };
  }

  function metricStats(values) {
    const present = values.map(finiteNumber).filter((value) => value !== null);
    if (!present.length) return { min: null, max: null, delta: null };
    return {
      min: Math.min(...present),
      max: Math.max(...present),
      delta: present.length > 1 ? present[present.length - 1] - present[0] : null,
    };
  }

  function formatCurrent(value, metric) {
    const number = finiteNumber(value);
    if (number === null) return "—";
    if (metric === "temperature") return clamp(number, -50, 200).toFixed(0);
    if (metric !== "tokens") return clamp(number, 0, 100).toFixed(0);
    return new Intl.NumberFormat("en-US", {
      maximumFractionDigits: number < 100 ? 1 : 0,
      minimumFractionDigits: 0,
    }).format(Math.max(0, number));
  }

  function formatCompact(value, metric) {
    const number = finiteNumber(value);
    if (number === null) return "—";
    if (metric !== "tokens") return String(Math.round(clamp(number, 0, 100)));
    const absolute = Math.abs(number);
    if (absolute >= 1e6) return `${(number / 1e6).toFixed(absolute >= 1e7 ? 0 : 1)}M`;
    if (absolute >= 1e3) return `${(number / 1e3).toFixed(absolute >= 1e4 ? 0 : 1)}K`;
    return number < 100 ? number.toFixed(1) : String(Math.round(number));
  }

  function sampleAgeSeconds(payload, nowMs) {
    if (!payload.generated_at) return null;
    const generatedMs = Date.parse(payload.generated_at);
    return Number.isFinite(generatedMs) ? Math.max(0, (nowMs - generatedMs) / 1000) : null;
  }

  function formatSampleTime(timestamp) {
    const date = new Date(timestamp);
    if (!timestamp || !Number.isFinite(date.getTime())) return "--:--:--";
    return date.toLocaleTimeString([], {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    });
  }

  function timestampMs(value) {
    if (value === null || value === undefined || value === "" || typeof value === "boolean") return null;
    if (typeof value === "number") {
      if (!Number.isFinite(value) || value < 0) return null;
      return value < 1e12 ? value * 1000 : value;
    }
    const parsed = Date.parse(String(value));
    return Number.isFinite(parsed) ? parsed : null;
  }

  function safeStatusToken(value, fallback) {
    const token = typeof value === "string" ? value.trim().toLowerCase() : "";
    return /^[a-z][a-z0-9_.-]{0,63}$/.test(token)
      ? token
      : (arguments.length > 1 ? fallback : "unknown");
  }

  function normalizeVoiceComponent(value) {
    const source = safeObject(value);
    const error = safeObject(source.last_error);
    return {
      state: safeStatusToken(source.state),
      started_at: source.started_at || source.started || null,
      completed_at: source.completed_at || source.completed || null,
      last_success_at: source.last_success_at || null,
      duration_seconds: finiteNumber(source.duration_seconds ?? source.duration),
      elapsed_seconds: finiteNumber(source.elapsed_seconds),
      chunk_index: finiteNumber(source.chunk_index ?? safeObject(source.progress).current),
      chunk_total: finiteNumber(source.chunk_total ?? safeObject(source.progress).total),
      consecutive_failures: finiteNumber(source.consecutive_failures),
      last_error: error.stage || error.type || error.code ? {
        stage: safeStatusToken(error.stage),
        error_type: safeStatusToken(error.error_type ?? error.type ?? error.code),
        at: error.at || null,
      } : null,
    };
  }

  function normalizeVoiceProgressState(value) {
    const source = safeObject(value);
    const state = safeStatusToken(typeof value === "string" ? value : source.state);
    if (["idle", "active", "complete", "error", "unknown"].includes(state)) return state;
    if (["running", "processing", "thinking", "synthesizing", "playing", "checking"].includes(state)) return "active";
    if (["ok", "done", "success", "triggered"].includes(state)) return "complete";
    if (["failed", "down", "stopped"].includes(state)) return "error";
    return "unknown";
  }

  function normalizeVoiceStatus(raw) {
    const source = safeObject(raw);
    const overall = safeObject(source.overall);
    const stages = safeObject(source.stages);
    const wake = safeObject(source.watchword || source.wake_word);
    const activeRequest = safeObject(source.active_request);
    const lastRequest = safeObject(source.last_request);
    const lastErrorSource = safeObject(source.last_error);
    const heartbeat = safeObject(source.heartbeat);
    const pipelineSource = safeObject(source.pipeline);
    const pipelineSteps = safeObject(pipelineSource.steps);
    const hasPipeline = VOICE_PROGRESS_ORDER.some((name) => (
      Object.prototype.hasOwnProperty.call(pipelineSteps, name)
    ));
    const tts = normalizeVoiceComponent(source.tts || stages.tts);
    const playback = normalizeVoiceComponent(stages.playback || source.playback);
    const directError = lastErrorSource.stage || lastErrorSource.error_type
      || lastErrorSource.type || lastErrorSource.code;
    return {
      device: "Cerberus",
      state: safeStatusToken(source.state || overall.state),
      healthy: source.healthy === true,
      stage: safeStatusToken(
        source.stage || overall.stage || overall.phase || activeRequest.stage,
      ),
      stage_started_at: source.stage_started_at || overall.stage_started_at
        || overall.phase_started_at || overall.phase_started || null,
      stage_elapsed_seconds: finiteNumber(source.stage_elapsed_seconds),
      updated_at: source.updated_at || heartbeat.at || heartbeat.updated_at || null,
      age_seconds: finiteNumber(source.age_seconds),
      stale_after_seconds: finiteNumber(source.stale_after_seconds),
      status_error: safeStatusToken(source.status_error, null),
      watchword: {
        state: safeStatusToken(wake.state),
        last_triggered_at: wake.last_triggered_at || wake.last_trigger_at || null,
        armed_until: wake.armed_until || null,
        armed_remaining_seconds: finiteNumber(wake.armed_remaining_seconds),
      },
      asr: normalizeVoiceComponent(source.asr || stages.asr),
      openclaw: normalizeVoiceComponent(source.openclaw || stages.openclaw),
      tts,
      playback,
      pipeline: hasPipeline ? {
        source: ["producer", "derived", "unavailable"].includes(safeStatusToken(pipelineSource.source))
          ? safeStatusToken(pipelineSource.source)
          : "unknown",
        active: pipelineSource.active === true,
        mode: safeStatusToken(pipelineSource.mode),
        steps: Object.fromEntries(VOICE_PROGRESS_ORDER.map((name) => [
          name, normalizeVoiceProgressState(pipelineSteps[name]),
        ])),
      } : null,
      last_error: directError ? {
        stage: safeStatusToken(lastErrorSource.stage),
        error_type: safeStatusToken(
          lastErrorSource.error_type ?? lastErrorSource.type ?? lastErrorSource.code,
        ),
        at: lastErrorSource.at || null,
      } : null,
      last_request: {
        result: safeStatusToken(lastRequest.result),
        failed_stage: safeStatusToken(lastRequest.failed_stage, null),
        tts_chunks: finiteNumber(lastRequest.tts_chunks),
      },
    };
  }

  function formatVoiceDuration(value) {
    const seconds = finiteNumber(value);
    if (seconds === null) return "—";
    const clamped = Math.max(0, seconds);
    if (clamped < 10) return `${clamped.toFixed(1)}S`;
    if (clamped < 60) return `${Math.round(clamped)}S`;
    if (clamped >= 86400) {
      const days = Math.floor(clamped / 86400);
      const hours = Math.floor((clamped % 86400) / 3600);
      return `${days}D${String(hours).padStart(2, "0")}H`;
    }
    if (clamped >= 3600) {
      const hours = Math.floor(clamped / 3600);
      const minutes = Math.floor((clamped % 3600) / 60);
      return `${hours}H${String(minutes).padStart(2, "0")}M`;
    }
    const minutes = Math.floor(clamped / 60);
    const remainder = Math.floor(clamped % 60);
    return `${minutes}M${String(remainder).padStart(2, "0")}S`;
  }

  function compactAge(timestamp, nowMs) {
    const parsed = timestampMs(timestamp);
    if (parsed === null) return null;
    const seconds = Math.max(0, (nowMs - parsed) / 1000);
    if (seconds < 2) return "NOW";
    if (seconds < 60) return `${Math.floor(seconds)}S AGO`;
    if (seconds < 3600) return `${Math.floor(seconds / 60)}M AGO`;
    return `${Math.floor(seconds / 3600)}H AGO`;
  }

  function voiceCardState(voice) {
    if (voice.status_error === "stale" || voice.state === "stale") return "stale";
    if (["down", "stopped"].includes(voice.state) || voice.status_error) return "down";
    if (voice.state === "degraded") return "error";
    if (["starting", "stopping", "unknown"].includes(voice.state)) return "starting";
    if (voice.state === "armed" || voice.watchword.state === "armed") return "armed";
    if (voice.state === "busy") return "busy";
    return "ready";
  }

  function voiceStep(kind, component, currentStage) {
    const state = safeStatusToken(component.state);
    const activeStages = {
      watchword: ["speech_detected", "watchword"],
      asr: ["asr"],
      openclaw: ["openclaw"],
      tts: ["tts_synthesis"],
      playback: ["tts_playback", "cooldown"],
    };
    const labelMaps = {
      watchword: {
        listening: "LISTEN", checking: "CHECK", armed: "ARMED", triggered: "HIT",
        not_detected: "MISS", stopped: "STOP", unknown: "—",
      },
      asr: { idle: "IDLE", processing: "RUN", ok: "OK", error: "FAIL", unknown: "—" },
      openclaw: { idle: "IDLE", thinking: "THINK", ok: "OK", error: "FAIL", unknown: "—" },
      tts: {
        idle: "IDLE", synthesizing: "SYNTH", playing: "DONE", cooldown: "DONE",
        ok: "OK", error: "FAIL", unknown: "—",
      },
      playback: {
        idle: "IDLE", synthesizing: "WAIT", playing: "PLAY", cooldown: "COOL",
        ok: "OK", error: "FAIL", unknown: "—",
      },
    };
    let viewState = "idle";
    if (["error", "failed"].includes(state)) viewState = "error";
    else if (["stopped", "down"].includes(state)) viewState = "down";
    else if (kind === "watchword" && state === "armed") viewState = "armed";
    else if (
      activeStages[kind].includes(currentStage)
      && !["ok", "error", "stopped", "down"].includes(state)
    ) viewState = "active";
    else if (["checking", "processing", "thinking", "synthesizing", "playing", "cooldown"].includes(state)) viewState = "active";
    else if (["ok", "triggered"].includes(state)) viewState = "complete";
    else if (state === "unknown") viewState = "unknown";
    const waitingForAsr = kind === "watchword" && state === "checking" && currentStage === "asr";
    if (waitingForAsr) viewState = "idle";
    const label = waitingForAsr
      ? "WAIT"
      : (labelMaps[kind] && labelMaps[kind][state]) || state.slice(0, 6).toUpperCase() || "—";
    return { state: viewState, label };
  }

  function voiceViewModel(raw, nowMs) {
    const voice = normalizeVoiceStatus(raw);
    const now = finiteNumber(nowMs) ?? Date.now();
    const state = voiceCardState(voice);
    const stageLabels = {
      starting: "STARTING VOICE STACK", listening: "LISTENING FOR CERBERUS",
      speech_detected: "SPEECH DETECTED", asr: "ASR TRANSCRIBING",
      watchword: "CHECKING WATCHWORD", openclaw: "OPENCLAW THINKING",
      tts_synthesis: "TTS SYNTHESIZING", tts_playback: "PLAYING RESPONSE",
      cooldown: "MIC COOLDOWN", retry_wait: "RETRY WAIT",
      stopping: "VOICE STACK STOPPING", stopped: "VOICE STACK STOPPED",
      unknown: "VOICE STATUS UNKNOWN",
    };
    const elapsed = voice.stage_elapsed_seconds !== null
      ? voice.stage_elapsed_seconds
      : (() => {
        const started = timestampMs(voice.stage_started_at);
        const stoppedClock = state === "stale" ? timestampMs(voice.updated_at) : null;
        const elapsedClock = stoppedClock === null ? now : stoppedClock;
        return started === null ? null : Math.max(0, (elapsedClock - started) / 1000);
      })();
    const synthComponent = { ...voice.tts };
    const playbackComponent = voice.playback.state !== "unknown"
      ? { ...voice.playback }
      : { ...voice.tts, state: "idle" };
    const ttsFailureStage = voice.last_error ? voice.last_error.stage : null;
    if (voice.tts.state === "playing") {
      synthComponent.state = "ok";
      playbackComponent.state = "playing";
    } else if (voice.tts.state === "cooldown") {
      synthComponent.state = "ok";
      playbackComponent.state = "cooldown";
    } else if (voice.tts.state === "ok" && voice.tts.chunk_total > 0) {
      synthComponent.state = "ok";
      playbackComponent.state = "ok";
    } else if (voice.tts.state === "error") {
      if (ttsFailureStage === "tts_playback") {
        synthComponent.state = "ok";
        playbackComponent.state = "error";
      } else {
        synthComponent.state = "error";
        playbackComponent.state = "idle";
      }
    }
    if (playbackComponent.chunk_index === null) playbackComponent.chunk_index = voice.tts.chunk_index;
    if (playbackComponent.chunk_total === null) playbackComponent.chunk_total = voice.tts.chunk_total;
    const chunks = voice.tts.chunk_index !== null && voice.tts.chunk_total !== null
      ? `${Math.round(voice.tts.chunk_index)}/${Math.round(voice.tts.chunk_total)}`
      : null;
    const durations = [];
    if (voice.asr.duration_seconds !== null) durations.push(`ASR ${formatVoiceDuration(voice.asr.duration_seconds)}`);
    if (voice.openclaw.duration_seconds !== null) durations.push(`CLAW ${formatVoiceDuration(voice.openclaw.duration_seconds)}`);
    if (voice.tts.duration_seconds !== null) durations.push(`TTS ${formatVoiceDuration(voice.tts.duration_seconds)}`);
    if (chunks) durations.push(`CHUNK ${chunks}`);
    let detail = durations.join(" · ");
    if (!detail && voice.watchword.state === "armed") {
      detail = `ARMED · ${formatVoiceDuration(voice.watchword.armed_remaining_seconds)} LEFT`;
    }
    if (state === "down") detail = "VOICE PIPELINE UNAVAILABLE";
    else if (state === "stale") {
      const frozenStage = stageLabels[voice.stage] || voice.stage.toUpperCase();
      detail = `FROZEN AT ${frozenStage}`;
    } else if (!detail) {
      detail = state === "ready" ? 'SAY "CERBERUS" TO START' : "PIPELINE TELEMETRY ACTIVE";
    }

    let error = null;
    if (voice.status_error) {
      const statusErrors = {
        missing: "VOICE BRIDGE DOWN · STATUS FILE MISSING",
        unreadable: "VOICE STATUS UNREADABLE",
        malformed: "VOICE STATUS MALFORMED",
        invalid: "VOICE STATUS INVALID",
        schema_mismatch: "VOICE STATUS SCHEMA MISMATCH",
        stale: `HEARTBEAT STALE · ${formatVoiceDuration(voice.age_seconds)}`,
      };
      error = statusErrors[voice.status_error] || `VOICE STATUS ${voice.status_error.toUpperCase()}`;
    } else if (["down", "stopped"].includes(voice.state)) {
      error = voice.state === "stopped" ? "VOICE BRIDGE STOPPED" : "VOICE BRIDGE DOWN";
    } else if (voice.last_error) {
      const errorAge = compactAge(voice.last_error.at, now);
      error = `LAST FAIL · ${voice.last_error.stage.toUpperCase()} · ${voice.last_error.error_type.toUpperCase()}`;
      if (errorAge) error += ` · ${errorAge}`;
    } else if (voice.last_request.failed_stage) {
      error = `LAST REQUEST FAILED · ${voice.last_request.failed_stage.toUpperCase()}`;
    }
    const heartbeatAge = voice.age_seconds !== null
      ? voice.age_seconds
      : (() => {
        const updated = timestampMs(voice.updated_at);
        return updated === null ? null : Math.max(0, (now - updated) / 1000);
      })();
    const lastTrigger = compactAge(voice.watchword.last_triggered_at, now);
    const stateLabels = {
      ready: "READY", busy: "BUSY", armed: "ARMED", starting: "STARTING",
      stale: "STALE", down: "DOWN", error: "ERROR",
    };
    return {
      voice,
      state,
      stateLabel: stateLabels[state] || "UNKNOWN",
      stageLabel: stageLabels[voice.stage] || voice.stage.toUpperCase(),
      // Listening is the steady state, not an in-flight operation. Its tenure
      // can be weeks on an always-on kiosk and obscures useful diagnostics.
      elapsedLabel: voice.stage === "listening" ? "—" : formatVoiceDuration(elapsed),
      detail,
      error,
      heartbeatLabel: heartbeatAge === null ? "NO HEARTBEAT" : `HEARTBEAT ${formatVoiceDuration(heartbeatAge)}`,
      lastEventLabel: lastTrigger ? `TRIGGER ${lastTrigger}` : "NO TRIGGER",
      steps: {
        watchword: voiceStep("watchword", voice.watchword, voice.stage),
        asr: voiceStep("asr", voice.asr, voice.stage),
        openclaw: voiceStep("openclaw", voice.openclaw, voice.stage),
        tts: voiceStep("tts", synthComponent, voice.stage),
        playback: voiceStep("playback", playbackComponent, voice.stage),
      },
    };
  }

  function voiceProgressModel(raw, nowMs) {
    const view = voiceViewModel(raw, nowMs);
    const voice = view.voice;
    if (voice.pipeline) {
      const explicit = voice.pipeline;
      return {
        source: explicit.source,
        active: explicit.active,
        mode: explicit.mode,
        state: view.state,
        stage: voice.stage,
        steps: { ...explicit.steps },
        error: view.error,
      };
    }

    const steps = Object.fromEntries(VOICE_PROGRESS_ORDER.map((name) => [name, "idle"]));
    const stageIndex = {
      speech_detected: 0, watchword: 0, asr: 1, openclaw: 2,
      tts_synthesis: 3, tts_playback: 4, cooldown: 4,
    }[voice.stage];
    if (stageIndex !== undefined) {
      for (let index = 0; index < stageIndex; index += 1) {
        steps[VOICE_PROGRESS_ORDER[index]] = "complete";
      }
      steps[VOICE_PROGRESS_ORDER[stageIndex]] = "active";
    }
    if (voice.stage === "asr" && voice.watchword.state !== "armed") {
      steps.heard_name = "idle";
    }

    // Legacy payloads describe component completion even if the overall stage
    // has already advanced. Preserve that information without carrying any
    // transcript, response, audio, request ID, or raw error content.
    const legacy = {
      heard_name: view.steps.watchword,
      asr: view.steps.asr,
      openclaw: view.steps.openclaw,
      tts: view.steps.tts,
      play: view.steps.playback,
    };
    for (const name of VOICE_PROGRESS_ORDER) {
      const state = legacy[name] ? legacy[name].state : "unknown";
      if (state === "error" || state === "down") steps[name] = "error";
      else if (state === "complete" && steps[name] !== "active") steps[name] = "complete";
      else if (state === "active") steps[name] = "active";
    }
    if (view.steps.watchword.state === "armed") steps.heard_name = "complete";

    const failureStep = {
      watchword: "heard_name", wake_word: "heard_name", asr: "asr",
      openclaw: "openclaw", tts: "tts", tts_synthesis: "tts",
      playback: "play", tts_playback: "play",
    }[voice.last_error && voice.last_error.stage];
    if (failureStep) steps[failureStep] = "error";

    return {
      source: "derived",
      active: ["busy", "armed"].includes(view.state) || Object.values(steps).includes("active"),
      mode: view.state === "busy" ? "request" : view.state === "armed" ? "armed" : "idle",
      state: view.state,
      stage: voice.stage,
      steps,
      error: view.error,
    };
  }

  function renderVoice(raw, nowMs) {
    const model = voiceProgressModel(raw, nowMs);
    const progress = byId("voice-progress");
    const dashboard = byId("dashboard");
    if (!progress || !dashboard) return model;
    const setData = (element, name, value) => {
      if (element.dataset[name] !== value) element.dataset[name] = value;
    };
    setData(progress, "state", model.state);
    setData(progress, "active", model.active ? "true" : "false");
    setData(progress, "source", model.source);
    setData(dashboard, "voiceState", model.state);
    setData(dashboard, "voiceStage", model.stage);
    for (const name of VOICE_PROGRESS_ORDER) {
      const element = byId(`voice-${name.replace("_", "-")}-step`);
      if (element) setData(element, "state", model.steps[name]);
    }
    const summary = VOICE_PROGRESS_ORDER
      .map((name) => `${VOICE_PROGRESS_LABELS[name]} ${model.steps[name]}`)
      .join("; ");
    const announcedState = safeStatusToken(model.state, "unknown");
    const announcedStage = safeStatusToken(model.stage, "unknown");
    const failureReported = Boolean(model.error)
      || Object.values(model.steps).includes("error")
      || ["down", "error", "stale"].includes(announcedState);
    const ariaLabel = `Voice pipeline ${announcedState} at ${announcedStage}`
      + `${failureReported ? "; failure reported" : ""}: ${summary}`;
    if (progress.getAttribute("aria-label") !== ariaLabel) progress.setAttribute("aria-label", ariaLabel);
    const title = model.error || `Voice pipeline ${model.mode}`;
    if (progress.title !== title) progress.title = title;
    observeVoiceSaverAttention(model, finiteNumber(nowMs) ?? Date.now());
    return model;
  }

  function renderVoiceTransportError(_error) {
    const progress = byId("voice-progress");
    const dashboard = byId("dashboard");
    if (progress) {
      if (progress.dataset.state !== "down") progress.dataset.state = "down";
      if (progress.dataset.active !== "false") progress.dataset.active = "false";
      if (progress.dataset.source !== "unavailable") progress.dataset.source = "unavailable";
      if (progress.getAttribute("aria-label") !== "Voice pipeline status unavailable") {
        progress.setAttribute("aria-label", "Voice pipeline status unavailable");
      }
      if (progress.title !== "Voice status link unavailable") progress.title = "Voice status link unavailable";
    }
    if (dashboard) {
      if (dashboard.dataset.voiceState !== "down") dashboard.dataset.voiceState = "down";
      if (dashboard.dataset.voiceStage !== "unavailable") dashboard.dataset.voiceStage = "unavailable";
    }
    for (const name of VOICE_PROGRESS_ORDER) {
      const element = byId(`voice-${name.replace("_", "-")}-step`);
      if (element && element.dataset.state !== "unknown") element.dataset.state = "unknown";
    }
    observeSaverHealth("voice", "down:transport", Date.now());
  }

  function renderNodeMetric(payload, metric, slot) {
    const host = hostAtSlot(payload, slot);
    const value = host ? finiteNumber(host[METRICS[metric].field]) : null;
    const values = hostMetricSeries(payload, metric, slot);
    const paths = sparklinePaths(values, { width: 220, height: 42, padding: 3, min: 0, max: 100 });
    const temperatureField = METRICS[metric].temperatureField;
    const temperature = host ? finiteNumber(host[temperatureField]) : null;
    const temperatureValues = hostTemperatureSeries(payload, metric, slot);
    const temperaturePaths = sparklinePaths(
      temperatureValues,
      { width: 220, height: 42, padding: 3, min: 20, max: 110 },
    );
    const state = host ? normalizeState(host.state) : "unknown";
    const row = byId(`${metric}-c${slot}-row`);
    const output = byId(`${metric}-c${slot}-value`);
    const chart = byId(`${metric}-c${slot}-chart`);
    const dot = byId(`${metric}-c${slot}-dot`);

    row.dataset.state = state;
    const diagnostics = [];
    if (host && host.error) diagnostics.push(host.error);
    if (host && temperature === null) diagnostics.push(`C${slot} ${METRICS[metric].temperatureLabel.toLowerCase()} is unavailable.`);
    row.title = diagnostics.join(" ");
    output.textContent = formatCurrent(value, metric);
    output.dataset.available = value === null ? "false" : "true";
    byId(`${metric}-c${slot}-line`).setAttribute("d", paths.line);
    dot.hidden = value === null || paths.latest === null;
    if (paths.latest) {
      dot.setAttribute("cx", paths.latest.x.toFixed(2));
      dot.setAttribute("cy", paths.latest.y.toFixed(2));
    }
    chart.setAttribute(
      "aria-label",
      value === null
        ? `C${slot} ${METRICS[metric].label} is unavailable`
        : `C${slot} ${METRICS[metric].label}, current ${formatCurrent(value, metric)} percent`,
    );
    const temperatureId = METRICS[metric].temperatureId;
    const temperatureOutput = byId(`${temperatureId}-c${slot}-value`);
    const temperatureChart = byId(`${temperatureId}-c${slot}-chart`);
    const temperatureDot = byId(`${temperatureId}-c${slot}-dot`);
    temperatureOutput.textContent = formatCurrent(temperature, "temperature");
    temperatureOutput.dataset.available = temperature === null ? "false" : "true";
    byId(`${temperatureId}-c${slot}-line`).setAttribute("d", temperaturePaths.line);
    temperatureDot.hidden = temperature === null || temperaturePaths.latest === null;
    if (temperaturePaths.latest) {
      temperatureDot.setAttribute("cx", temperaturePaths.latest.x.toFixed(2));
      temperatureDot.setAttribute("cy", temperaturePaths.latest.y.toFixed(2));
    }
    temperatureChart.setAttribute(
      "aria-label",
      temperature === null
        ? `C${slot} ${METRICS[metric].temperatureLabel} is unavailable`
        : `C${slot} ${METRICS[metric].temperatureLabel}, current ${formatCurrent(temperature, "temperature")} degrees Celsius`,
    );
    row.dataset.temperatureAvailable = temperature === null ? "false" : "true";
    return value !== null;
  }

  function renderPerNodeMetric(payload, metric) {
    const available = NODE_SLOTS.map((slot) => renderNodeMetric(payload, metric, slot))
      .filter(Boolean).length;
    const card = document.querySelector(`[data-metric="${metric}"]`);
    delete card.dataset.freshnessState;
    card.dataset.state = available ? "ready" : "unavailable";
    card.dataset.availableNodes = String(available);
  }

  function renderTokens(payload) {
    const card = document.querySelector('[data-metric="tokens"]');
    const value = finiteNumber(payload.throughput.tokens_per_second);
    const values = tokenSeries(payload);
    const stats = metricStats(values);
    const graphMax = niceCeiling(Math.max(10, stats.max === null ? 10 : stats.max * 1.08));
    const paths = sparklinePaths(values, { width: 300, height: 78, padding: 4, min: 0, max: graphMax });
    const state = throughputViewState(payload.throughput.state, value);
    delete card.dataset.freshnessState;

    byId("tokens-value").textContent = formatCurrent(value, "tokens");
    byId("tokens-range").textContent = stats.min === null
      ? "MIN — · MAX —"
      : `MIN ${formatCompact(stats.min, "tokens")} · MAX ${formatCompact(stats.max, "tokens")}`;
    byId("tokens-delta").textContent = "API AGG · NOT PER NODE";
    byId("tokens-scale").textContent = `${formatCompact(graphMax, "tokens")} MAX`;
    byId("tokens-line").setAttribute("d", paths.line);
    byId("tokens-area").setAttribute("d", paths.area);
    const dot = byId("tokens-dot");
    dot.hidden = value === null || paths.latest === null;
    if (paths.latest) {
      dot.setAttribute("cx", paths.latest.x.toFixed(2));
      dot.setAttribute("cy", paths.latest.y.toFixed(2));
    }
    const stateLabel = byId("tokens-state");
    stateLabel.dataset.state = state.state;
    stateLabel.textContent = state.label;
    card.dataset.state = value === null ? "unavailable" : "ready";
    card.dataset.throughputState = state.state;
    card.title = payload.throughput.source
      ? `Cluster-wide output rate from ${payload.throughput.source}; no per-node attribution is available.`
      : "Cluster-wide API output rate; no per-node attribution is available.";
    byId("tokens-chart").setAttribute(
      "aria-label",
      value === null
        ? "API-wide output token throughput is unavailable"
        : `API-wide output token throughput, current ${formatCurrent(value, "tokens")} tokens per second; not attributable per node`,
    );
  }

  function renderHost(slot, host) {
    const summary = byId(`host-c${slot}`);
    const state = host ? normalizeState(host.state) : "unknown";
    const age = host ? finiteNumber(host.age_seconds) : null;
    const stateText = state === "unknown"
      ? (host ? "UNKNOWN" : "NO DATA")
      : state.toUpperCase();
    summary.dataset.state = state;
    summary.title = host && host.error ? host.error : "";
    byId(`c${slot}-state`).textContent = age !== null
      ? `${stateText} · ${Math.max(0, Math.round(age))}S`
      : stateText;
  }

  function inferredClusterState(payload) {
    const explicit = normalizeState(payload.cluster.state);
    if (explicit !== "unknown") return explicit;
    const total = finiteNumber(payload.cluster.total_hosts) || 3;
    const available = finiteNumber(payload.cluster.available_hosts);
    if (available !== null) {
      if (available <= 0) return "offline";
      return available < total ? "degraded" : "online";
    }
    const states = payload.hosts.map((host) => normalizeState(host.state));
    const online = states.filter((state) => state === "online").length;
    if (online === total) return "online";
    if (online > 0) return "degraded";
    return states.length ? "offline" : "unknown";
  }

  function markClusterDataStale(ageSeconds, title) {
    const age = finiteNumber(ageSeconds);
    for (const metric of Object.keys(METRICS)) {
      const card = document.querySelector(`[data-metric="${metric}"]`);
      if (card) {
        card.dataset.freshnessState = "stale";
        card.title = title;
      }
      for (const slot of NODE_SLOTS) {
        const row = byId(`${metric}-c${slot}-row`);
        if (row) row.dataset.state = "stale";
      }
    }
    const tokensCard = document.querySelector('[data-metric="tokens"]');
    if (tokensCard) {
      tokensCard.dataset.freshnessState = "stale";
      tokensCard.dataset.throughputState = "stale";
      tokensCard.title = title;
    }
    const tokenState = byId("tokens-state");
    if (tokenState) {
      tokenState.dataset.state = "stale";
      tokenState.textContent = "STALE";
    }
    for (const slot of NODE_SLOTS) {
      const summary = byId(`host-c${slot}`);
      const state = byId(`c${slot}-state`);
      if (summary) summary.dataset.state = "stale";
      if (state) state.textContent = age === null ? "STALE" : `STALE · ${Math.max(0, Math.floor(age))}S`;
    }
  }

  function render(raw, nowMs) {
    const payload = normalizePayload(raw);
    const now = finiteNumber(nowMs) ?? Date.now();
    const dashboard = byId("dashboard");
    const age = sampleAgeSeconds(payload, now);
    const staleAfter = Math.max(15, payload.interval_seconds * 3);
    let state = inferredClusterState(payload);
    const stale = age === null || age > staleAfter;
    if (stale) state = "degraded";

    const totalHosts = finiteNumber(payload.cluster.total_hosts) || 3;
    const derivedAvailable = payload.hosts.filter((host) => normalizeState(host.state) === "online").length;
    const availableHosts = finiteNumber(payload.cluster.available_hosts) ?? derivedAvailable;
    observeClusterSaverAttention(state, availableHosts, payload, stale, now);
    const labels = {
      online: "CLUSTER ONLINE",
      degraded: stale ? "TELEMETRY STALE" : "CLUSTER DEGRADED",
      offline: "CLUSTER OFFLINE",
      unknown: "STATUS UNKNOWN",
    };

    dashboard.dataset.connection = state;
    byId("cluster-indicator").className = "status-dot";
    byId("cluster-state").textContent = labels[state];
    byId("host-count").textContent = stale
      ? `LAST ${availableHosts} / ${totalHosts} NODES`
      : `${availableHosts} / ${totalHosts} NODES`;
    byId("sample-time").textContent = formatSampleTime(payload.generated_at);
    byId("sample-age").textContent = age === null ? "NO SAMPLE" : age < 2 ? "LIVE" : `${Math.floor(age)}S AGO`;

    Object.keys(METRICS).forEach((metric) => renderPerNodeMetric(payload, metric));
    renderTokens(payload);
    const bySlot = new Map(payload.hosts.map((host) => [host.slot, host]));
    NODE_SLOTS.forEach((slot) => renderHost(slot, bySlot.get(slot) || null));
    if (stale) {
      const staleTitle = age === null
        ? "Retained values; cluster sample time is missing or invalid."
        : `Retained values; latest cluster sample is ${Math.floor(age)} seconds old.`;
      markClusterDataStale(age, staleTitle);
    }

    let message;
    if (state === "online") {
      message = `${payload.history.length} ROLLING SAMPLES · C1/C2/C3 TRACES LIVE · TOKEN RATE IS API-WIDE`;
    } else if (stale) {
      message = age === null
        ? "LATEST SAMPLE TIME IS MISSING OR INVALID · CHECK COLLECTOR"
        : `LATEST SAMPLE IS ${Math.floor(age)}S OLD · CHECK COLLECTOR`;
    } else if (state === "degraded") {
      message = `${availableHosts} OF ${totalHosts} HOSTS AVAILABLE · MISSING NODE TRACES ARE SHOWN AS GAPS`;
    } else if (state === "offline") {
      message = "NO CLUSTER HOSTS AVAILABLE · RETAINED HISTORY IS NOT CURRENT DATA";
    } else {
      message = "WAITING FOR PER-NODE TELEMETRY";
    }
    byId("connection-message").textContent = message;
    document.title = state === "offline" ? "OFFLINE · Cerberus Cluster Pulse" : "Cerberus Cluster Pulse";
    lastPayload = payload;
    return payload;
  }

  function renderTransportError(error) {
    const dashboard = byId("dashboard");
    dashboard.dataset.connection = "error";
    byId("cluster-indicator").className = "status-dot";
    byId("cluster-state").textContent = "DATA LINK LOST";
    const retainedAge = lastPayload ? sampleAgeSeconds(lastPayload, Date.now()) : null;
    const elapsed = retainedAge === null ? null : Math.floor(retainedAge);
    const retainedTotal = lastPayload ? (finiteNumber(lastPayload.cluster.total_hosts) || 3) : 3;
    const retainedAvailable = lastPayload
      ? (finiteNumber(lastPayload.cluster.available_hosts)
        ?? lastPayload.hosts.filter((host) => normalizeState(host.state) === "online").length)
      : null;
    byId("host-count").textContent = retainedAvailable === null
      ? `? / ${retainedTotal} NODES`
      : `LAST ${retainedAvailable} / ${retainedTotal} NODES`;
    byId("sample-age").textContent = elapsed === null ? "NO SAMPLE" : `${elapsed}S AGO`;
    byId("connection-message").textContent = `STATUS API UNAVAILABLE · ${String(error && error.message ? error.message : error).slice(0, 90)}`;
    markClusterDataStale(elapsed, "Retained values; cluster telemetry transport is unavailable.");
    document.title = "LINK LOST · Cerberus Cluster Pulse";
    observeSaverHealth("cluster", "error:transport", Date.now());
  }

  function saverStateAt(nowMs, lastAttentionMs, attentionActive, lastSweepMs = null) {
    if (attentionActive) return "awake";
    const now = finiteNumber(nowMs);
    const last = finiteNumber(lastAttentionMs);
    if (now === null || last === null || now < last) return "awake";
    const previousSweep = finiteNumber(lastSweepMs);
    const firstDue = last + SAVER_IDLE_MS;
    const repeatDue = previousSweep === null ? firstDue : previousSweep + SAVER_REPEAT_MS;
    return now >= Math.max(firstDue, repeatDue) ? "sweep" : "awake";
  }

  function setSaverActive(active) {
    const next = active === true;
    const changed = saverActive !== next;
    saverActive = next;
    const overlay = byId("lcd-refresh-sweep");
    const dashboard = byId("dashboard");
    if (overlay) {
      overlay.hidden = !next;
      overlay.dataset.active = next ? "true" : "false";
      overlay.setAttribute("aria-hidden", "true");
    }
    if (dashboard) dashboard.dataset.saverState = next ? "sweeping" : "awake";
    if (next && ambientTimer !== null) {
      clearTimeout(ambientTimer);
      ambientTimer = null;
    }
    if (changed && !next && typeof document !== "undefined" && !document.hidden) startAmbient();
    return next;
  }

  function evaluateSaver(nowMs) {
    if (typeof document !== "undefined" && document.hidden) {
      setSaverActive(false);
      return "awake";
    }
    const state = saverStateAt(
      nowMs,
      saverLastAttentionMs,
      false,
      saverLastSweepMs,
    );
    if (state === "sweep" && !saverActive) startSaverSweep(nowMs);
    return saverActive ? "sweep" : "awake";
  }

  function finishSaverSweep(nowMs) {
    if (saverFinishTimer !== null) clearTimeout(saverFinishTimer);
    saverFinishTimer = null;
    if (typeof document !== "undefined" && document.hidden) {
      setSaverActive(false);
      return;
    }
    // Only a sweep that stayed visible through completion earns the 30-minute
    // interval. A hidden/cancelled pass must remain due when the panel returns.
    saverLastSweepMs = finiteNumber(nowMs) ?? Date.now();
    setSaverActive(false);
    scheduleSaver(nowMs);
  }

  function startSaverSweep(nowMs) {
    if (saverActive || (typeof document !== "undefined" && document.hidden)) return false;
    const now = finiteNumber(nowMs) ?? Date.now();
    setSaverActive(true);
    saverFinishTimer = setTimeout(
      () => finishSaverSweep(Date.now()),
      SAVER_SWEEP_MS + 100,
    );
    return true;
  }

  function scheduleSaver(nowMs) {
    if (saverTimer !== null) clearTimeout(saverTimer);
    saverTimer = null;
    if (saverActive || (typeof document !== "undefined" && document.hidden)) return;
    const now = finiteNumber(nowMs) ?? Date.now();
    if (saverLastAttentionMs === null) saverLastAttentionMs = now;
    const firstDue = saverLastAttentionMs + SAVER_IDLE_MS;
    const repeatDue = saverLastSweepMs === null
      ? firstDue
      : saverLastSweepMs + SAVER_REPEAT_MS;
    const remaining = Math.max(0, Math.max(firstDue, repeatDue) - now);
    if (remaining === 0) {
      startSaverSweep(now);
      return;
    }
    saverTimer = setTimeout(() => {
      saverTimer = null;
      if (evaluateSaver(Date.now()) !== "sweep") scheduleSaver(Date.now());
    }, remaining + 16);
  }

  function noteSaverAttention(reason, nowMs) {
    const overlay = byId("lcd-refresh-sweep");
    if (!overlay) return false;
    const now = finiteNumber(nowMs) ?? Date.now();
    saverLastAttentionMs = now;
    const dashboard = byId("dashboard");
    if (dashboard) dashboard.dataset.saverWake = safeStatusToken(reason, "activity");
    if (saverFinishTimer !== null) clearTimeout(saverFinishTimer);
    saverFinishTimer = null;
    setSaverActive(false);
    scheduleSaver(now);
    return true;
  }

  function observeSaverHealth(channel, troubleSignature, nowMs) {
    if (!Object.prototype.hasOwnProperty.call(saverAttentionSignatures, channel)) return false;
    const next = troubleSignature === null || troubleSignature === undefined
      ? null
      : String(troubleSignature).slice(0, 96);
    const previous = saverAttentionSignatures[channel];
    saverAttentionSignatures[channel] = next;
    // A new problem, a materially changed problem, or recovery gets one full
    // attention window. An unchanged outage may then receive the next sweep.
    if (saverTroubleTransition(previous, next)) {
      return noteSaverAttention(`${channel}-${next === null ? "recovered" : "attention"}`, nowMs);
    }
    return false;
  }

  function saverTroubleTransition(previous, next) {
    return previous !== next && (previous !== null || next !== null);
  }

  function observeVoiceSaverAttention(model, nowMs) {
    const trouble = voiceTroubleSignature(model);
    observeSaverHealth("voice", trouble, nowMs);
    if (model.active) noteSaverAttention("voice-active", nowMs);
  }

  function voiceTroubleSignature(model) {
    const source = safeObject(model);
    const steps = safeObject(source.steps);
    const errorSteps = VOICE_PROGRESS_ORDER.filter((name) => steps[name] === "error");
    if (errorSteps.length) return `error:${errorSteps.join(",")}`;
    return ["down", "error", "stale", "starting"].includes(source.state)
      ? `${source.state}:${source.stage || "unknown"}`
      : null;
  }

  function observeClusterSaverAttention(state, availableHosts, payload, stale, nowMs) {
    const trouble = clusterTroubleSignature(state, availableHosts, payload, stale);
    observeSaverHealth("cluster", trouble, nowMs);
    const throughputState = String(payload.throughput.state || "").trim().toLowerCase();
    const rate = finiteNumber(payload.throughput.tokens_per_second);
    if (!stale && throughputState === "active" && rate !== null && rate > 0) {
      noteSaverAttention("model-active", nowMs);
    }
  }

  function clusterTroubleSignature(state, availableHosts, payload, stale) {
    const normalized = safeObject(payload);
    const hosts = Array.isArray(normalized.hosts) ? normalized.hosts : [];
    const hostStates = NODE_SLOTS.map((slot) => {
      const host = hosts.find((candidate) => safeObject(candidate).slot === slot);
      return host ? normalizeState(host.state) : "unknown";
    });
    if (state === "online" && !stale && hostStates.every((hostState) => hostState === "online")) return null;
    const hostFingerprint = hostStates.map((hostState, index) => `c${index + 1}=${hostState}`).join(",");
    return `${state}:${Math.max(0, finiteNumber(availableHosts) || 0)}:${stale ? "stale" : "fresh"}:${hostFingerprint}`;
  }

  function startSaver() {
    const overlay = byId("lcd-refresh-sweep");
    if (!overlay) return;
    const now = Date.now();
    saverLastAttentionMs = now;
    saverLastSweepMs = null;
    setSaverActive(false);
    if (!saverLifecycleBound && typeof document.addEventListener === "function") {
      saverLifecycleBound = true;
      for (const eventName of ["pointerdown", "touchstart", "keydown"]) {
        document.addEventListener(
          eventName,
          () => noteSaverAttention(`local-${eventName}`, Date.now()),
          { passive: true, capture: true },
        );
      }
      document.addEventListener("visibilitychange", () => {
        if (document.hidden) {
          if (saverTimer !== null) clearTimeout(saverTimer);
          saverTimer = null;
          if (saverFinishTimer !== null) clearTimeout(saverFinishTimer);
          saverFinishTimer = null;
          // Do not mark an off-screen or interrupted pass as completed.
          setSaverActive(false);
          return;
        }
        if (evaluateSaver(Date.now()) !== "sweep") scheduleSaver(Date.now());
      });
      overlay.addEventListener("animationend", () => {
        if (saverActive) finishSaverSweep(Date.now());
      });
    }
    scheduleSaver(now);
  }

  function ambientSceneAt(elapsedMs) {
    const elapsed = finiteNumber(elapsedMs);
    if (elapsed === null) return 0;
    return Math.floor(Math.max(0, elapsed) / AMBIENT_SCENE_MS) % AMBIENT_SCENES;
  }

  function ambientFrameAt(elapsedMs, reducedMotion) {
    const elapsed = Math.max(0, finiteNumber(elapsedMs) || 0);
    const phase = Math.floor(elapsed / AMBIENT_SCENE_MS);
    const within = elapsed % AMBIENT_SCENE_MS;
    const transitionStart = AMBIENT_SCENE_MS - AMBIENT_TRANSITION_MS;
    const transitionProgress = reducedMotion || within <= transitionStart
      ? 0
      : clamp((within - transitionStart) / AMBIENT_TRANSITION_MS, 0, 1);
    const mix = transitionProgress * transitionProgress * (3 - 2 * transitionProgress);
    return {
      phase,
      scene: phase % AMBIENT_SCENES,
      nextScene: (phase + 1) % AMBIENT_SCENES,
      mix,
    };
  }

  function burnInOffset(phase) {
    return [
      { x: 0, y: 0 }, { x: -1, y: 0 }, { x: 1, y: 0 },
      { x: 0, y: -1 }, { x: 0, y: 1 }, { x: -1, y: -1 },
      { x: 1, y: 1 }, { x: -1, y: 1 }, { x: 1, y: -1 },
    ][Math.abs(Math.trunc(phase)) % 9];
  }

  function ambientDisplayMode(connection, voiceState) {
    const cluster = String(connection || "").toLowerCase();
    const voice = String(voiceState || "").toLowerCase();
    if (["offline", "error"].includes(cluster)) return "critical";
    if (["degraded", "connecting"].includes(cluster)
      || ["down", "error", "stale", "starting"].includes(voice)) return "degraded";
    if (["busy", "armed"].includes(voice)) return "voice";
    return "normal";
  }

  function ambientHash(x, y, seed) {
    let value = Math.imul(x + seed * 17, 374761393)
      ^ Math.imul(y + seed * 31, 668265263);
    value = Math.imul(value ^ (value >>> 13), 1274126177);
    return ((value ^ (value >>> 16)) >>> 0) / 4294967295;
  }

  function ambientPixel(scene, x, y, seconds, width, height, mode, target) {
    const color = target || [0, 0, 0];
    const nx = x / Math.max(1, width - 1);
    const ny = y / Math.max(1, height - 1);
    let wave = 0;
    let pulse = 0;
    let red = 0;
    let green = 0;
    let blue = 0;
    switch (scene % AMBIENT_SCENES) {
      case 1:
        // A slow packet tunnel with a vanishing point behind the card row.
        wave = Math.abs(((nx * 12 - seconds * 0.32) % 1 + 1) % 1 - 0.5);
        pulse = Math.max(0, 1 - wave * 11);
        {
          const horizon = Math.abs(ny - 0.5);
          const rails = Math.max(0, 1 - Math.abs((horizon * 9 + seconds * 0.16) % 1 - 0.5) * 8);
          const spokes = Math.max(0, 1 - Math.abs(Math.sin((nx - 0.5) * 21 + ny * 2)) * 8);
          red = 10 + 48 * pulse + 20 * rails;
          green = 25 + 84 * rails + 40 * spokes;
          blue = 38 + 106 * pulse + 42 * rails;
        }
        break;
      case 2:
        // Three interference sources form animated topographic contours.
        wave = Math.sin(Math.hypot(nx - 0.18, ny - 0.44) * 42 - seconds * 0.65)
          + Math.sin(Math.hypot(nx - 0.51, ny - 0.62) * 36 + seconds * 0.48)
          + Math.sin(Math.hypot(nx - 0.82, ny - 0.38) * 40 - seconds * 0.55);
        pulse = Math.max(0, 1 - Math.abs(Math.sin(wave * 2.2)) * 6);
        red = 20 + 105 * pulse + 14 * (wave + 3) / 6;
        green = 22 + 62 * pulse + 34 * (wave + 3) / 6;
        blue = 48 + 102 * pulse;
        break;
      case 3:
        // Deterministic packet rain; no random state or per-frame allocation.
        {
          const column = Math.floor(x / 2);
          const speed = 0.8 + ambientHash(column, 0, 3) * 1.7;
          const head = ((seconds * speed + ambientHash(column, 4, 7) * height) % (height + 9)) - 5;
          const trail = y - head;
          pulse = trail >= 0 && trail < 7 ? (1 - trail / 7) : 0;
          const node = column % 3;
          red = 8 + pulse * (node === 1 ? 150 : 42);
          green = 18 + pulse * (node === 2 ? 150 : 74);
          blue = 24 + pulse * (node === 0 ? 170 : 88);
        }
        break;
      case 4:
        // Circuit-board cells with packets turning at deterministic junctions.
        {
          const gridX = x % 11;
          const gridY = y % 7;
          const wire = gridX === 0 || gridY === 0 ? 1 : 0;
          const packetX = Math.floor(seconds * 2.2 + y * 0.37) % 11;
          const packetY = Math.floor(seconds * 1.4 + x * 0.23) % 7;
          pulse = (gridY === 0 && gridX === packetX) || (gridX === 0 && gridY === packetY) ? 1 : 0;
          const junction = gridX < 2 && gridY < 2 ? 1 : 0;
          red = 9 + 24 * wire + 95 * pulse;
          green = 20 + 68 * wire + 122 * pulse + 25 * junction;
          blue = 28 + 58 * wire + 95 * pulse;
        }
        break;
      case 5:
        // Wide aurora ribbons move slowly enough to remain calm overnight.
        wave = Math.sin(nx * 13 + seconds * 0.22)
          + 0.55 * Math.sin(nx * 27 - seconds * 0.16);
        pulse = Math.max(0, 1 - Math.abs(ny - (0.5 + wave * 0.15)) * 8);
        {
          const upper = Math.max(0, 1 - Math.abs(ny - (0.25 - wave * 0.06)) * 12);
          red = 13 + 52 * pulse + 76 * upper;
          green = 26 + 124 * pulse + 34 * upper;
          blue = 42 + 82 * pulse + 106 * upper;
        }
        break;
      default:
        // Cerberus triad: three colored nodes share one breathing data link.
        {
          red = 7; green = 16; blue = 24;
          const linkY = 0.5 + Math.sin(nx * 14 - seconds * 0.28) * 0.045;
          const link = Math.max(0, 1 - Math.abs(ny - linkY) * 70);
          red += 18 * link; green += 62 * link; blue += 78 * link;
          for (let index = 0; index < AMBIENT_NODE_CENTERS.length; index += 1) {
            const center = AMBIENT_NODE_CENTERS[index];
            const cy = 0.5 + Math.sin(seconds * 0.22 + index * 2.1) * 0.08;
            const distance = Math.hypot(nx - center, ny - cy);
            const glow = 1 / (1 + distance * distance * 310);
            const ring = Math.max(0, 1 - Math.abs(distance * 32 - (2.2 + Math.sin(seconds * 0.35 + index))) * 1.8);
            const palette = AMBIENT_NODE_PALETTES[index];
            red += palette[0] * glow + palette[0] * ring * 0.3;
            green += palette[1] * glow + palette[1] * ring * 0.3;
            blue += palette[2] * glow + palette[2] * ring * 0.3;
          }
        }
        break;
    }

    const displayMode = mode || "normal";
    if (displayMode === "critical") {
      const luminance = red * 0.21 + green * 0.72 + blue * 0.07;
      const bar = Math.max(0, 1 - Math.abs(((nx + ny * 0.5 - seconds * 0.025) * 8) % 1 - 0.5) * 9);
      red = 22 + luminance * 0.72 + bar * 70;
      green = 5 + luminance * 0.1;
      blue = 16 + luminance * 0.22 + bar * 16;
    } else if (displayMode === "degraded") {
      const luminance = red * 0.21 + green * 0.72 + blue * 0.07;
      red = 18 + luminance * 0.68;
      green = 15 + luminance * 0.48;
      blue = 22 + luminance * 0.3;
    } else if (displayMode === "voice") {
      const sweepPosition = ((seconds * 0.055) % 1 + 1) % 1;
      const sweep = Math.max(0, 1 - Math.abs(nx - sweepPosition) * 13);
      red += sweep * 18;
      green += sweep * 70;
      blue += sweep * 48;
    }
    color[0] = Math.round(clamp(red, 0, 255));
    color[1] = Math.round(clamp(green, 0, 255));
    color[2] = Math.round(clamp(blue, 0, 255));
    return color;
  }

  function paintAmbient(canvas, nowMs, options) {
    if (!canvas || typeof canvas.getContext !== "function") return null;
    const width = canvas.width || 178;
    const height = canvas.height || 35;
    if (!ambientSurface || ambientSurface.canvas !== canvas
      || ambientSurface.width !== width || ambientSurface.height !== height) {
      const context = canvas.getContext("2d", { alpha: false });
      if (!context || typeof context.createImageData !== "function") return null;
      ambientSurface = {
        canvas,
        context,
        image: context.createImageData(width, height),
        width,
        height,
      };
    }
    const settings = options || {};
    const reducedMotion = settings.reducedMotion === true;
    const frame = ambientFrameAt(nowMs, reducedMotion);
    const dashboard = byId("dashboard");
    const mode = settings.mode || ambientDisplayMode(
      dashboard && dashboard.dataset.connection,
      dashboard && dashboard.dataset.voiceState,
    );
    // Reduced-motion snapshots are frozen within each scene.
    const seconds = reducedMotion
      ? (frame.phase * AMBIENT_SCENE_MS + AMBIENT_SCENE_MS * 0.42) / 1000
      : Math.max(0, finiteNumber(nowMs) || 0) / 1000;
    const image = ambientSurface.image;
    const primary = [0, 0, 0];
    const secondary = [0, 0, 0];
    for (let y = 0; y < height; y += 1) {
      for (let x = 0; x < width; x += 1) {
        const offset = (y * width + x) * 4;
        ambientPixel(frame.scene, x, y, seconds, width, height, mode, primary);
        if (frame.mix > 0) {
          ambientPixel(frame.nextScene, x, y, seconds, width, height, mode, secondary);
        }
        image.data[offset] = primary[0] + (secondary[0] - primary[0]) * frame.mix;
        image.data[offset + 1] = primary[1] + (secondary[1] - primary[1]) * frame.mix;
        image.data[offset + 2] = primary[2] + (secondary[2] - primary[2]) * frame.mix;
        image.data[offset + 3] = 255;
      }
    }
    ambientSurface.context.putImageData(image, 0, 0);
    if (dashboard) {
      dashboard.dataset.ambientMode = mode;
      if (dashboard.dataset.ambientPhase !== String(frame.phase)) {
        const offset = burnInOffset(frame.phase);
        dashboard.dataset.ambientPhase = String(frame.phase);
        dashboard.dataset.ambientScene = String(frame.scene);
        dashboard.style.setProperty("--burnin-x", `${offset.x}px`);
        dashboard.style.setProperty("--burnin-y", `${offset.y}px`);
      }
    }
    return frame.scene;
  }

  function startAmbient() {
    if (ambientTimer !== null) clearTimeout(ambientTimer);
    ambientTimer = null;
    if (saverActive) return;
    const canvas = byId("ambient-canvas");
    if (!canvas || typeof canvas.getContext !== "function") return;
    if (!ambientMotionQuery && typeof global.matchMedia === "function") {
      ambientMotionQuery = global.matchMedia("(prefers-reduced-motion: reduce)");
    }
    if (!ambientLifecycleBound) {
      ambientLifecycleBound = true;
      if (ambientMotionQuery && typeof ambientMotionQuery.addEventListener === "function") {
        ambientMotionQuery.addEventListener("change", startAmbient);
      }
      if (typeof document.addEventListener === "function") {
        document.addEventListener("visibilitychange", startAmbient);
      }
    }
    if (document.hidden) return;
    const reducedMotion = Boolean(ambientMotionQuery && ambientMotionQuery.matches);
    const tick = () => {
      if (document.hidden || saverActive) return;
      const now = Date.now();
      paintAmbient(canvas, now, { reducedMotion });
      const cadence = reducedMotion ? AMBIENT_SCENE_MS : AMBIENT_FRAME_MS;
      // Align updates to wall-clock boundaries so reduced motion does not
      // drift into the middle of the following 30-second scene.
      const delay = Math.max(50, cadence - (now % cadence) + 16);
      ambientTimer = setTimeout(tick, delay);
    };
    tick();
  }

  async function fetchJson(url, timeoutMs) {
    const controller = typeof AbortController === "function" ? new AbortController() : null;
    const timeout = controller ? setTimeout(() => controller.abort(), timeoutMs) : null;
    try {
      const response = await fetch(url, {
        cache: "no-store",
        headers: { Accept: "application/json" },
        signal: controller ? controller.signal : undefined,
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      return await response.json();
    } finally {
      if (timeout !== null) clearTimeout(timeout);
    }
  }

  function fetchStatus() {
    return fetchJson(API_URL, 4200);
  }

  function fetchVoiceStatus() {
    return fetchJson(VOICE_API_URL, 600);
  }

  async function poll() {
    const started = Date.now();
    try {
      render(await fetchStatus(), Date.now());
    } catch (error) {
      renderTransportError(error);
    } finally {
      const elapsed = Date.now() - started;
      pollTimer = setTimeout(poll, Math.max(250, POLL_MS - elapsed));
    }
  }

  async function pollVoice() {
    const started = Date.now();
    try {
      renderVoice(await fetchVoiceStatus(), Date.now());
    } catch (error) {
      renderVoiceTransportError(error);
    } finally {
      const elapsed = Date.now() - started;
      voicePollTimer = setTimeout(pollVoice, Math.max(100, VOICE_POLL_MS - elapsed));
    }
  }

  function start() {
    if (pollTimer !== null) clearTimeout(pollTimer);
    if (voicePollTimer !== null) clearTimeout(voicePollTimer);
    startSaver();
    startAmbient();
    poll();
    pollVoice();
  }

  global.C3DashboardUI = {
    POLL_MS,
    VOICE_POLL_MS,
    MAX_HISTORY_POINTS,
    AMBIENT_SCENE_MS,
    AMBIENT_FRAME_MS,
    SAVER_IDLE_MS,
    SAVER_BAND_PX,
    SAVER_REPEAT_MS,
    SAVER_SWEEP_MS,
    VOICE_PROGRESS_ORDER,
    finiteNumber,
    hostSlot,
    normalizeState,
    throughputViewState,
    normalizePayload,
    hostMetricSeries,
    hostTemperatureSeries,
    tokenSeries,
    niceCeiling,
    sparklinePaths,
    metricStats,
    formatCurrent,
    formatCompact,
    sampleAgeSeconds,
    normalizeVoiceStatus,
    normalizeVoiceProgressState,
    formatVoiceDuration,
    voiceStep,
    voiceViewModel,
    voiceProgressModel,
    renderVoice,
    renderVoiceTransportError,
    inferredClusterState,
    ambientSceneAt,
    ambientFrameAt,
    burnInOffset,
    ambientDisplayMode,
    ambientPixel,
    paintAmbient,
    saverStateAt,
    startSaverSweep,
    setSaverActive,
    evaluateSaver,
    observeSaverHealth,
    saverTroubleTransition,
    voiceTroubleSignature,
    clusterTroubleSignature,
    startSaver,
    render,
    renderTransportError,
    start,
  };

  if (typeof document !== "undefined" && typeof fetch === "function") {
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", start, { once: true });
    } else {
      start();
    }
  }
})(typeof globalThis !== "undefined" ? globalThis : this);
