(function (global) {
  "use strict";

  const API_URL = "/api/status";
  const VOICE_API_URL = "/api/voice-status";
  const POLL_MS = 5000;
  const VOICE_POLL_MS = 750;
  const MAX_HISTORY_POINTS = 60;
  const NODE_SLOTS = [1, 2, 3];
  const AMBIENT_SCENE_MS = 30000;
  const AMBIENT_FRAME_MS = 125;
  const AMBIENT_SCENES = 4;
  const METRICS = {
    cpu: { field: "cpu_percent", label: "CPU utilization", temperatureField: "cpu_temperature_c", temperatureId: "cpu-temp", temperatureLabel: "CPU temperature" },
    gpu: { field: "gpu_percent", label: "GPU utilization", temperatureField: "gpu_temperature_c", temperatureId: "gpu-temp", temperatureLabel: "GPU temperature" },
    ram: { field: "ram_percent", label: "RAM utilization", temperatureField: "soc_temperature_c", temperatureId: "ram-soc", temperatureLabel: "SoC temperature" },
  };

  let pollTimer = null;
  let voicePollTimer = null;
  let ambientTimer = null;
  let lastPayload = null;
  let lastSuccessMs = null;
  let voiceLastSuccessMs = null;

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
      const match = candidate.match(/(?:^|[^a-z0-9])(?:c(?:erebrus)?|spark)[-_ ]?([123])(?:[^0-9]|$)/i)
        || candidate.match(/^(?:c|spark|cerebrus)?[-_ ]?([123])$/i)
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

  function normalizeVoiceStatus(raw) {
    const source = safeObject(raw);
    const overall = safeObject(source.overall);
    const stages = safeObject(source.stages);
    const wake = safeObject(source.watchword || source.wake_word);
    const activeRequest = safeObject(source.active_request);
    const lastRequest = safeObject(source.last_request);
    const lastErrorSource = safeObject(source.last_error);
    const heartbeat = safeObject(source.heartbeat);
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
      elapsedLabel: formatVoiceDuration(elapsed),
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

  function renderVoice(raw, nowMs) {
    const model = voiceViewModel(raw, nowMs);
    const card = byId("voice-card");
    card.dataset.state = model.state;
    byId("voice-state").dataset.state = model.state;
    byId("voice-state").textContent = model.stateLabel;
    byId("voice-stage").textContent = model.stageLabel;
    byId("voice-elapsed").textContent = model.elapsedLabel;
    byId("voice-detail").textContent = model.detail;
    byId("voice-heartbeat").textContent = model.heartbeatLabel;
    byId("voice-last-event").textContent = model.lastEventLabel;
    const error = byId("voice-error");
    error.hidden = !model.error;
    error.textContent = model.error || "";
    for (const [name, step] of Object.entries(model.steps)) {
      byId(`voice-${name}-step`).dataset.state = step.state;
      byId(`voice-${name}-state`).textContent = step.label;
    }
    card.title = model.error || `${model.stageLabel}; ${model.detail}`;
    voiceLastSuccessMs = finiteNumber(nowMs) ?? Date.now();
    return model;
  }

  function renderVoiceTransportError(error) {
    const card = byId("voice-card");
    card.dataset.state = "down";
    byId("voice-state").dataset.state = "down";
    byId("voice-state").textContent = "LINK DOWN";
    byId("voice-stage").textContent = "VOICE STATUS UNREACHABLE";
    const elapsed = voiceLastSuccessMs === null
      ? null
      : Math.max(0, (Date.now() - voiceLastSuccessMs) / 1000);
    byId("voice-elapsed").textContent = formatVoiceDuration(elapsed);
    byId("voice-detail").textContent = "CLUSTER TELEMETRY CONTINUES INDEPENDENTLY";
    const errorOutput = byId("voice-error");
    errorOutput.hidden = false;
    errorOutput.textContent = `VOICE API FAILED · ${String(error && error.message ? error.message : error).slice(0, 48)}`;
    byId("voice-heartbeat").textContent = elapsed === null ? "NO HEARTBEAT" : `LAST GOOD ${formatVoiceDuration(elapsed)}`;
    for (const name of ["asr", "watchword", "openclaw", "tts", "playback"]) {
      byId(`voice-${name}-step`).dataset.state = "unknown";
      byId(`voice-${name}-state`).textContent = "—";
    }
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

  function render(raw, nowMs) {
    const payload = normalizePayload(raw);
    const now = finiteNumber(nowMs) ?? Date.now();
    const dashboard = byId("dashboard");
    const age = sampleAgeSeconds(payload, now);
    const staleAfter = Math.max(15, payload.interval_seconds * 3);
    let state = inferredClusterState(payload);
    const stale = age !== null && age > staleAfter;
    if (stale && state !== "offline") state = "degraded";

    const totalHosts = finiteNumber(payload.cluster.total_hosts) || 3;
    const derivedAvailable = payload.hosts.filter((host) => normalizeState(host.state) === "online").length;
    const availableHosts = finiteNumber(payload.cluster.available_hosts) ?? derivedAvailable;
    const labels = {
      online: "CLUSTER ONLINE",
      degraded: stale ? "TELEMETRY STALE" : "CLUSTER DEGRADED",
      offline: "CLUSTER OFFLINE",
      unknown: "STATUS UNKNOWN",
    };

    dashboard.dataset.connection = state;
    byId("cluster-indicator").className = "status-dot";
    byId("cluster-state").textContent = labels[state];
    byId("host-count").textContent = `${availableHosts} / ${totalHosts} NODES`;
    byId("sample-time").textContent = formatSampleTime(payload.generated_at);
    byId("sample-age").textContent = age === null ? "NO SAMPLE" : age < 2 ? "LIVE" : `${Math.floor(age)}S AGO`;

    Object.keys(METRICS).forEach((metric) => renderPerNodeMetric(payload, metric));
    renderTokens(payload);
    const bySlot = new Map(payload.hosts.map((host) => [host.slot, host]));
    NODE_SLOTS.forEach((slot) => renderHost(slot, bySlot.get(slot) || null));

    let message;
    if (state === "online") {
      message = `${payload.history.length} ROLLING SAMPLES · C1/C2/C3 TRACES LIVE · TOKEN RATE IS API-WIDE`;
    } else if (stale) {
      message = `LATEST SAMPLE IS ${Math.floor(age)}S OLD · CHECK COLLECTOR`;
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
    lastSuccessMs = now;
    return payload;
  }

  function renderTransportError(error) {
    const dashboard = byId("dashboard");
    dashboard.dataset.connection = "error";
    byId("cluster-indicator").className = "status-dot";
    byId("cluster-state").textContent = "DATA LINK LOST";
    const elapsed = lastSuccessMs === null ? null : Math.max(0, Math.floor((Date.now() - lastSuccessMs) / 1000));
    byId("sample-age").textContent = elapsed === null ? "NO SAMPLE" : `${elapsed}S AGO`;
    byId("connection-message").textContent = `STATUS API UNAVAILABLE · ${String(error && error.message ? error.message : error).slice(0, 90)}`;
    document.title = "LINK LOST · Cerberus Cluster Pulse";
  }

  function ambientSceneAt(elapsedMs) {
    const elapsed = finiteNumber(elapsedMs);
    if (elapsed === null) return 0;
    return Math.floor(Math.max(0, elapsed) / AMBIENT_SCENE_MS) % AMBIENT_SCENES;
  }

  function burnInOffset(scene) {
    return [
      { x: -1, y: 0 },
      { x: 1, y: -1 },
      { x: 0, y: 1 },
      { x: 1, y: 0 },
    ][Math.abs(Math.trunc(scene)) % AMBIENT_SCENES];
  }

  function ambientPixel(scene, x, y, seconds, width, height) {
    const nx = x / Math.max(1, width - 1);
    const ny = y / Math.max(1, height - 1);
    let wave;
    let pulse;
    let color;
    switch (scene % AMBIENT_SCENES) {
      case 1:
        wave = Math.sin((x + y * 2) * 0.34 - seconds * 1.35);
        pulse = Math.max(0, Math.sin(x * 0.11 - seconds * 2.1)) * (0.25 + 0.75 * ny);
        color = [40 + 35 * pulse, 32 + 45 * (wave + 1), 82 + 95 * pulse];
        break;
      case 2:
        wave = Math.sin(Math.hypot(nx - 0.5, ny - 0.5) * 42 - seconds * 2.2);
        pulse = Math.max(0, Math.cos((nx * 3 - ny * 2 + seconds * 0.13) * Math.PI));
        color = [18 + 48 * pulse, 58 + 74 * (wave + 1) / 2, 65 + 90 * pulse];
        break;
      case 3:
        wave = Math.sin((x * 0.18) + Math.sin(y * 0.55 + seconds) * 2.1 + seconds * 0.7);
        pulse = ((x * 17 + y * 31 + Math.floor(seconds * 3)) % 97) < 2 ? 1 : 0;
        color = [32 + 105 * pulse, 52 + 52 * (wave + 1), 42 + 55 * (1 - ny)];
        break;
      default:
        wave = Math.sin(x * 0.16 + seconds * 0.8) + Math.cos(y * 0.48 - seconds * 0.55);
        pulse = (Math.sin((nx + ny) * 18 - seconds * 1.1) + 1) / 2;
        color = [15 + 30 * pulse, 58 + 48 * (wave + 2) / 4, 72 + 82 * pulse];
        break;
    }
    return color.map((channel) => Math.round(clamp(channel, 0, 255)));
  }

  function paintAmbient(canvas, nowMs) {
    if (!canvas || typeof canvas.getContext !== "function") return null;
    const context = canvas.getContext("2d", { alpha: false });
    if (!context || typeof context.createImageData !== "function") return null;
    const width = canvas.width || 178;
    const height = canvas.height || 35;
    const scene = ambientSceneAt(nowMs);
    const seconds = Math.max(0, finiteNumber(nowMs) || 0) / 1000;
    const image = context.createImageData(width, height);
    for (let y = 0; y < height; y += 1) {
      for (let x = 0; x < width; x += 1) {
        const offset = (y * width + x) * 4;
        const color = ambientPixel(scene, x, y, seconds, width, height);
        image.data[offset] = color[0];
        image.data[offset + 1] = color[1];
        image.data[offset + 2] = color[2];
        image.data[offset + 3] = 255;
      }
    }
    context.putImageData(image, 0, 0);
    const dashboard = byId("dashboard");
    if (dashboard.dataset.ambientScene !== String(scene)) {
      const offset = burnInOffset(scene);
      dashboard.dataset.ambientScene = String(scene);
      dashboard.style.setProperty("--burnin-x", `${offset.x}px`);
      dashboard.style.setProperty("--burnin-y", `${offset.y}px`);
    }
    return scene;
  }

  function startAmbient() {
    if (ambientTimer !== null) clearTimeout(ambientTimer);
    const canvas = byId("ambient-canvas");
    if (!canvas || typeof canvas.getContext !== "function") return;
    const reducedMotion = typeof global.matchMedia === "function"
      && global.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const frameDelay = reducedMotion ? AMBIENT_SCENE_MS : AMBIENT_FRAME_MS;
    const tick = () => {
      paintAmbient(canvas, Date.now());
      ambientTimer = setTimeout(tick, frameDelay);
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
    startAmbient();
    poll();
    pollVoice();
  }

  global.C3DashboardUI = {
    POLL_MS,
    VOICE_POLL_MS,
    MAX_HISTORY_POINTS,
    AMBIENT_SCENE_MS,
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
    formatVoiceDuration,
    voiceStep,
    voiceViewModel,
    renderVoice,
    renderVoiceTransportError,
    inferredClusterState,
    ambientSceneAt,
    burnInOffset,
    ambientPixel,
    paintAmbient,
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
