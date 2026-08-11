(function (global) {
  "use strict";

  const API_URL = "/api/status";
  const POLL_MS = 5000;
  const MAX_HISTORY_POINTS = 60;
  const PERCENT_METRICS = new Set(["cpu", "gpu", "ram"]);
  const METRICS = {
    cpu: { field: "cpu_percent", label: "CPU utilization" },
    gpu: { field: "gpu_percent", label: "GPU utilization" },
    ram: { field: "ram_percent", label: "RAM utilization" },
    tokens: { field: "tokens_per_second", label: "token throughput" },
  };

  let pollTimer = null;
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
    const candidates = [key, host.id, host.name, host.hostname]
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
        slot = [1, 2, 3].find((candidate) => !occupied.has(candidate)) || null;
      }
      if (slot !== null) occupied.add(slot);
      hosts.push({
        key,
        slot,
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
        cpu_percent: finiteNumber(clusterSource.cpu_percent),
        gpu_percent: finiteNumber(clusterSource.gpu_percent),
        ram_percent: finiteNumber(clusterSource.ram_percent),
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

  function historyValue(point, metric) {
    if (metric === "tokens") {
      return finiteNumber(safeObject(point.throughput).tokens_per_second);
    }
    return finiteNumber(safeObject(point.cluster)[METRICS[metric].field]);
  }

  function currentValue(payload, metric) {
    return metric === "tokens"
      ? finiteNumber(payload.throughput.tokens_per_second)
      : finiteNumber(payload.cluster[METRICS[metric].field]);
  }

  function metricSeries(payload, metric) {
    const values = payload.history.slice(-MAX_HISTORY_POINTS).map((point) => historyValue(point, metric));
    const current = currentValue(payload, metric);
    if (!values.length) {
      if (current !== null) values.push(current);
    } else if (current !== null) {
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
    const width = finiteNumber(settings.width) || 240;
    const height = finiteNumber(settings.height) || 84;
    const padding = finiteNumber(settings.padding) ?? 4;
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
    return {
      line: lineParts.join(" "),
      area: areaParts.join(" "),
      latest,
    };
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
    if (PERCENT_METRICS.has(metric)) return clamp(number, 0, 100).toFixed(1);
    return new Intl.NumberFormat("en-US", {
      maximumFractionDigits: number < 100 ? 1 : 0,
      minimumFractionDigits: 0,
    }).format(Math.max(0, number));
  }

  function formatCompact(value, metric) {
    const number = finiteNumber(value);
    if (number === null) return "—";
    if (PERCENT_METRICS.has(metric)) return String(Math.round(clamp(number, 0, 100)));
    const absolute = Math.abs(number);
    if (absolute >= 1e6) return `${(number / 1e6).toFixed(absolute >= 1e7 ? 0 : 1)}M`;
    if (absolute >= 1e3) return `${(number / 1e3).toFixed(absolute >= 1e4 ? 0 : 1)}K`;
    return number < 100 ? number.toFixed(1) : String(Math.round(number));
  }

  function formatNodePercent(value) {
    const number = finiteNumber(value);
    return number === null ? "—" : String(Math.round(clamp(number, 0, 100)));
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

  function renderMetric(payload, metric) {
    const card = document.querySelector(`[data-metric="${metric}"]`);
    const value = currentValue(payload, metric);
    const values = metricSeries(payload, metric);
    const stats = metricStats(values);
    const percent = PERCENT_METRICS.has(metric);
    const graphMax = percent ? 100 : niceCeiling(Math.max(10, stats.max === null ? 10 : stats.max * 1.08));
    const paths = sparklinePaths(values, { width: 240, height: 84, padding: 4, min: 0, max: graphMax });
    const unit = percent ? "%" : " tok/s";

    byId(`${metric}-value`).textContent = formatCurrent(value, metric);
    byId(`${metric}-range`).textContent = stats.min === null
      ? "MIN — · MAX —"
      : `MIN ${formatCompact(stats.min, metric)} · MAX ${formatCompact(stats.max, metric)}`;
    byId(`${metric}-delta`).textContent = stats.delta === null
      ? "Δ —"
      : `Δ ${stats.delta > 0 ? "+" : ""}${formatCompact(stats.delta, metric)}`;
    byId(`${metric}-scale`).textContent = percent ? "100%" : `${formatCompact(graphMax, metric)} MAX`;
    byId(`${metric}-line`).setAttribute("d", paths.line);
    byId(`${metric}-area`).setAttribute("d", paths.area);

    const dot = byId(`${metric}-dot`);
    dot.hidden = paths.latest === null;
    if (paths.latest) {
      dot.setAttribute("cx", paths.latest.x.toFixed(2));
      dot.setAttribute("cy", paths.latest.y.toFixed(2));
    }
    card.dataset.state = value === null ? "unavailable" : "ready";
    if (metric === "tokens") {
      const throughputState = throughputViewState(payload.throughput.state, value);
      const stateLabel = byId("tokens-state");
      card.dataset.throughputState = throughputState.state;
      stateLabel.dataset.state = throughputState.state;
      stateLabel.textContent = throughputState.label;
    }
    byId(`${metric}-chart`).setAttribute(
      "aria-label",
      value === null
        ? `${METRICS[metric].label} is unavailable`
        : `${METRICS[metric].label}, current ${formatCurrent(value, metric)}${unit}`,
    );
  }

  function renderHost(slot, host) {
    const row = byId(`host-c${slot}`);
    const rawState = host ? host.state : null;
    const state = host ? normalizeState(rawState) : "unknown";
    const age = host ? finiteNumber(host.age_seconds) : null;
    const stateText = state === "unknown"
      ? (host ? "UNKNOWN" : "NO DATA")
      : state.toUpperCase();
    row.dataset.state = state;
    row.title = host && host.error ? host.error : "";
    byId(`c${slot}-state`).textContent = age !== null
      ? `${stateText} · ${Math.max(0, Math.round(age))}S`
      : stateText;

    ["cpu", "gpu", "ram"].forEach((metric) => {
      const output = byId(`c${slot}-${metric}`);
      const value = host ? host[`${metric}_percent`] : null;
      output.textContent = formatNodePercent(value);
      output.dataset.available = finiteNumber(value) === null ? "false" : "true";
    });
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

    Object.keys(METRICS).forEach((metric) => renderMetric(payload, metric));
    const bySlot = new Map(payload.hosts.map((host) => [host.slot, host]));
    [1, 2, 3].forEach((slot) => renderHost(slot, bySlot.get(slot) || null));

    let message;
    if (state === "online") {
      message = `${payload.history.length} ROLLING SAMPLES · ALL HOSTS REPORTING`;
    } else if (stale) {
      message = `LATEST SAMPLE IS ${Math.floor(age)}S OLD · CHECK COLLECTOR`;
    } else if (state === "degraded") {
      message = `${availableHosts} OF ${totalHosts} HOSTS AVAILABLE · PARTIAL AVERAGES SHOWN`;
    } else if (state === "offline") {
      message = "NO CLUSTER HOSTS AVAILABLE · LAST VALUES MAY BE RETAINED";
    } else {
      message = "WAITING FOR A COMPLETE CLUSTER SAMPLE";
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
    poll();
  }

  global.C3DashboardUI = {
    POLL_MS,
    MAX_HISTORY_POINTS,
    finiteNumber,
    hostSlot,
    normalizeState,
    throughputViewState,
    normalizePayload,
    metricSeries,
    niceCeiling,
    sparklinePaths,
    metricStats,
    formatCurrent,
    formatCompact,
    sampleAgeSeconds,
    inferredClusterState,
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
