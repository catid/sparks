(function (global) {
  "use strict";

  const API_URL = "/api/status";
  const POLL_MS = 5000;
  const MAX_HISTORY_POINTS = 60;
  const NODE_SLOTS = [1, 2, 3];
  const AMBIENT_SCENE_MS = 30000;
  const AMBIENT_FRAME_MS = 125;
  const AMBIENT_SCENES = 4;
  const METRICS = {
    cpu: { field: "cpu_percent", label: "CPU utilization" },
    gpu: { field: "gpu_percent", label: "GPU utilization" },
    ram: { field: "ram_percent", label: "RAM utilization" },
  };

  let pollTimer = null;
  let ambientTimer = null;
  let lastPayload = null;
  let lastSuccessMs = null;

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

  function hostMetricSeries(payload, metric, slot) {
    const field = METRICS[metric].field;
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

  function renderNodeMetric(payload, metric, slot) {
    const host = hostAtSlot(payload, slot);
    const value = host ? finiteNumber(host[METRICS[metric].field]) : null;
    const values = hostMetricSeries(payload, metric, slot);
    const paths = sparklinePaths(values, { width: 220, height: 42, padding: 3, min: 0, max: 100 });
    const state = host ? normalizeState(host.state) : "unknown";
    const row = byId(`${metric}-c${slot}-row`);
    const output = byId(`${metric}-c${slot}-value`);
    const chart = byId(`${metric}-c${slot}-chart`);
    const dot = byId(`${metric}-c${slot}-dot`);

    row.dataset.state = state;
    row.title = host && host.error ? host.error : "";
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
    document.title = state === "offline" ? "OFFLINE · Cerebrus Cluster Pulse" : "Cerebrus Cluster Pulse";
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
    document.title = "LINK LOST · Cerebrus Cluster Pulse";
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

  async function fetchStatus() {
    const controller = typeof AbortController === "function" ? new AbortController() : null;
    const timeout = controller ? setTimeout(() => controller.abort(), 4200) : null;
    try {
      const response = await fetch(API_URL, {
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

  function start() {
    if (pollTimer !== null) clearTimeout(pollTimer);
    startAmbient();
    poll();
  }

  global.C3DashboardUI = {
    POLL_MS,
    MAX_HISTORY_POINTS,
    AMBIENT_SCENE_MS,
    finiteNumber,
    hostSlot,
    normalizeState,
    throughputViewState,
    normalizePayload,
    hostMetricSeries,
    tokenSeries,
    niceCeiling,
    sparklinePaths,
    metricStats,
    formatCurrent,
    formatCompact,
    sampleAgeSeconds,
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
