"use strict";

const history = {
  cluster: [],
  spark1Gpu: [],
  spark1Cpu: [],
  spark2Gpu: [],
  spark2Cpu: [],
};
const maxHistory = 90;

const q = (id) => document.getElementById(id);
const safe = (v) => v === null || v === undefined || Number.isNaN(v) ? null : v;
const num = (v, digits = 1) => safe(v) === null ? "—" : Number(v).toFixed(digits);
const pct = (v) => safe(v) === null ? "—" : `${num(v, 1)}%`;
const rate = (v) => safe(v) === null ? "—" : Number(v) >= 100 ? num(v, 0) : num(v, 1);
const temperature = (v) => safe(v) === null ? "—" : `${num(v, 1)} °C`;
const bytes = (v) => {
  if (safe(v) === null) return "—";
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let n = Number(v), i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(i > 2 ? 2 : 1)} ${units[i]}`;
};
const bitsRate = (v) => {
  if (safe(v) === null) return "—";
  const bits = Number(v) * 8;
  if (bits >= 1e9) return `${(bits / 1e9).toFixed(2)} Gb/s`;
  if (bits >= 1e6) return `${(bits / 1e6).toFixed(1)} Mb/s`;
  if (bits >= 1e3) return `${(bits / 1e3).toFixed(1)} kb/s`;
  return `${bits.toFixed(0)} b/s`;
};
const width = (v) => `${Math.max(0, Math.min(100, safe(v) ?? 0))}%`;
const sumPresent = (values) => {
  const present = values.filter((value) => safe(value) !== null);
  return present.length ? present.reduce((total, value) => total + Number(value), 0) : null;
};

function nodeTemplate(name, node, index) {
  const s = node.system || {}, gpu = s.gpu || {}, mem = s.memory || {}, therm = s.thermals || {};
  const v = node.vllm || {}, rates = v.rates || {};
  const role = node.role || v.role || "replica";
  const worker = role === "worker";
  const memPercent = mem.used_bytes != null && mem.total_bytes ? mem.used_bytes / mem.total_bytes * 100 : null;
  const networkValues = Object.values(s.network || {});
  const fabricRx = sumPresent(networkValues.map((network) => network.rx_bytes_per_second));
  const fabricTx = sumPresent(networkValues.map((network) => network.tx_bytes_per_second));
  const networks = Object.entries(s.network || {}).map(([nic, n]) => `
    <div class="link-row">
      <span>${n.rdma_device ? `${nic} · ${n.rdma_device}` : nic}</span>
      <span>↓ ${bitsRate(n.rx_bytes_per_second)}</span>
      <span>↑ ${bitsRate(n.tx_bytes_per_second)}</span>
      <span class="link-state ${n.operstate === "up" ? "" : "down"}">${(n.counter_source || "netdev").toUpperCase()} · ${n.operstate || "—"} · MTU ${n.mtu || "—"}</span>
    </div>`).join("");
  const dflash = v.dflash_window_acceptance_percent ?? v.dflash_acceptance_percent;
  const error = s.error || (worker ? null : v.error);
  const endpoint = worker
    ? "headless TP worker · telemetry over SSH"
    : `${role === "aggregate" ? "cluster-wide metrics" : "node metrics"} · ${node.endpoint || "no endpoint"}`;
  let stateLabel;
  if (worker) {
    stateLabel = v.healthy
      ? `RANK ${node.rank ?? index - 1} · WORKER`
      : v.state === "worker_stopped"
        ? `RANK ${node.rank ?? index - 1} · STOPPED`
        : `RANK ${node.rank ?? index - 1} · UNREACHABLE`;
  } else {
    stateLabel = `RANK ${node.rank ?? index - 1} · ${v.healthy ? "SERVING" : "OFFLINE"}`;
  }
  const primaryMetrics = worker ? `
        <div class="metric"><strong>${bitsRate(fabricRx)}</strong><label>fabric receive</label></div>
        <div class="metric"><strong>${bitsRate(fabricTx)}</strong><label>fabric transmit</label></div>` : `
        <div class="metric"><strong>${rate(rates.generation_tokens_per_second)}</strong><label>cluster generation tok/s</label></div>
        <div class="metric"><strong>${rate(rates.prompt_tokens_per_second)}</strong><label>cluster prompt tok/s</label></div>`;
  const thirdBar = worker ? `
        <div>
          <div class="bar-head"><span>Headless TP rank</span><span>${v.healthy ? "active" : "not running"}</span></div>
          <div class="track"><div class="fill" style="width:${v.healthy ? "100%" : "0%"}"></div></div>
        </div>` : `
        <div>
          <div class="bar-head"><span>KV cache</span><span>${pct(v.kv_cache_usage_percent)}</span></div>
          <div class="track"><div class="fill" style="width:${width(v.kv_cache_usage_percent)}"></div></div>
        </div>`;
  const miniMetrics = worker ? `
        <div class="mini"><strong>${num(gpu.sm_clock_mhz, 0)} MHz</strong><label>SM clock</label></div>
        <div class="mini"><strong>${bytes(s.vllm_rss_bytes)}</strong><label>worker RSS</label></div>
        <div class="mini"><strong>${pct(gpu.gpu_util_percent)}</strong><label>GPU utilization</label></div>
        <div class="mini"><strong>${num(gpu.memory_util_percent, 0)}%</strong><label>memory controller</label></div>
        <div class="mini"><strong>${bytes(mem.swap_used_bytes)}</strong><label>swap used</label></div>
        <div class="mini"><strong>SSH</strong><label>telemetry source</label></div>
        <div class="mini"><strong>rank 0</strong><label>API counters reported by</label></div>
        <div class="mini"><strong>none</strong><label>local HTTP endpoint</label></div>` : `
        <div class="mini"><strong>${num(gpu.sm_clock_mhz, 0)} MHz</strong><label>SM clock</label></div>
        <div class="mini"><strong>${bytes(s.vllm_rss_bytes)}</strong><label>vLLM RSS</label></div>
        <div class="mini"><strong>${num(v.running_requests, 0)} / ${num(v.waiting_requests, 0)}</strong><label>running / waiting</label></div>
        <div class="mini"><strong>${pct(dflash)}</strong><label>Draft acceptance</label></div>
        <div class="mini"><strong>${pct(v.prefix_hit_percent)}</strong><label>prefix hit rate</label></div>
        <div class="mini"><strong>${num(gpu.memory_util_percent, 0)}%</strong><label>memory controller</label></div>
        <div class="mini"><strong>${bytes(mem.swap_used_bytes)}</strong><label>swap used</label></div>
        <div class="mini"><strong>${num(v.latency_ms, 0)} ms</strong><label>metrics latency</label></div>`;
  const thermalMetrics = `
        <div class="mini"><strong>${temperature(therm.soc_c)}</strong><label>SoC temperature</label></div>
        <div class="mini"><strong>${temperature(therm.nvme_composite_c)}</strong><label>NVMe composite</label></div>
        <div class="mini"><strong>${temperature(therm.connectx_asic_max_c)}</strong><label>hottest ConnectX ASIC</label></div>
        <div class="mini"><strong>${temperature(therm.memory_c)}</strong><label>LPDDR5X temperature</label></div>`;
  return `
    <article class="node-card">
      <div class="node-head">
        <div class="node-title">
          <span class="node-index">0${index}</span>
          <div><h2>${s.hostname || name}</h2><p class="endpoint mono">${endpoint}</p></div>
        </div>
        <span class="state ${v.healthy ? "online" : "offline"}">${stateLabel}</span>
      </div>
      ${error ? `<p class="error-line">${String(error).slice(0, 150)}</p>` : ""}
      <div class="metric-major">
        ${primaryMetrics}
        <div class="metric"><strong>${num(gpu.temperature_c, 0)}<span class="metric-unit"> °C</span></strong><label>GPU temperature</label></div>
        <div class="metric"><strong>${temperature(therm.cpu_cluster_max_c)}</strong><label>CPU cluster hotspot</label></div>
        <div class="metric"><strong>${num(gpu.power_w, 1)}<span class="metric-unit"> W</span></strong><label>GPU power</label></div>
      </div>
      <div class="bars">
        <div>
          <div class="bar-head"><span>GPU utilization</span><span>${pct(gpu.gpu_util_percent)}</span></div>
          <div class="track"><div class="fill" style="width:${width(gpu.gpu_util_percent)}"></div></div>
        </div>
        <div>
          <div class="bar-head"><span>Unified memory</span><span>${bytes(mem.used_bytes)} / ${bytes(mem.total_bytes)}</span></div>
          <div class="track"><div class="fill" style="width:${width(memPercent)}"></div></div>
        </div>
        ${thirdBar}
      </div>
      <div class="mini-grid">
        ${miniMetrics}
        ${thermalMetrics}
      </div>
      <div class="links"><h3>CONNECTX-7 RDMA DATA LINKS</h3>${networks || "<p class='muted'>No link counters</p>"}</div>
    </article>`;
}

function drawChart() {
  const canvas = q("throughput-chart"), rect = canvas.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  canvas.width = rect.width * dpr; canvas.height = rect.height * dpr;
  const ctx = canvas.getContext("2d"); ctx.scale(dpr, dpr);
  const w = rect.width, h = rect.height, pad = { l: 36, r: 10, t: 10, b: 23 };
  const all = history.cluster.filter(v => v != null);
  const max = Math.max(10, ...all) * 1.12;
  ctx.strokeStyle = "#253040"; ctx.fillStyle = "#8290a3"; ctx.font = "10px ui-monospace";
  for (let i = 0; i <= 4; i++) {
    const y = pad.t + (h - pad.t - pad.b) * i / 4;
    ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(w - pad.r, y); ctx.stroke();
    ctx.fillText(Math.round(max * (1 - i / 4)), 2, y + 3);
  }
  const plot = (values, color) => {
    if (values.length < 2) return;
    ctx.strokeStyle = color; ctx.lineWidth = 2; ctx.beginPath();
    values.forEach((value, i) => {
      const x = pad.l + (w - pad.l - pad.r) * i / Math.max(maxHistory - 1, values.length - 1);
      const y = pad.t + (h - pad.t - pad.b) * (1 - (value || 0) / max);
      i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
    });
    ctx.stroke();
  };
  plot(history.cluster, "#5ce1e6");
  ctx.fillText("TOK/S · 3 MINUTE WINDOW", pad.l, h - 4);
}

function drawTemperatureChart() {
  const canvas = q("temperature-chart"), rect = canvas.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  canvas.width = rect.width * dpr; canvas.height = rect.height * dpr;
  const ctx = canvas.getContext("2d"); ctx.scale(dpr, dpr);
  const w = rect.width, h = rect.height, pad = { l: 36, r: 10, t: 10, b: 23 };
  const series = [
    [history.spark1Gpu, "#5ce1e6", []],
    [history.spark1Cpu, "#73e2a7", [5, 4]],
    [history.spark2Gpu, "#a983ff", []],
    [history.spark2Cpu, "#f7bd5b", [5, 4]],
  ];
  const all = series.flatMap(([values]) => values).filter(value => safe(value) !== null).map(Number);
  let floor = all.length ? Math.floor((Math.min(...all) - 5) / 5) * 5 : 20;
  let ceiling = all.length ? Math.ceil((Math.max(...all) + 5) / 5) * 5 : 100;
  floor = Math.max(0, floor);
  if (ceiling - floor < 20) ceiling = floor + 20;
  const span = ceiling - floor;
  ctx.strokeStyle = "#253040"; ctx.fillStyle = "#8290a3"; ctx.font = "10px ui-monospace";
  for (let i = 0; i <= 4; i++) {
    const y = pad.t + (h - pad.t - pad.b) * i / 4;
    ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(w - pad.r, y); ctx.stroke();
    ctx.fillText(`${Math.round(ceiling - span * i / 4)}°`, 2, y + 3);
  }
  const plot = (values, color, dash) => {
    if (values.length < 2) return;
    ctx.strokeStyle = color; ctx.lineWidth = 2; ctx.setLineDash(dash); ctx.beginPath();
    let active = false;
    values.forEach((value, i) => {
      if (safe(value) === null) { active = false; return; }
      const x = pad.l + (w - pad.l - pad.r) * i / Math.max(maxHistory - 1, values.length - 1);
      const y = pad.t + (h - pad.t - pad.b) * (1 - (Number(value) - floor) / span);
      active ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
      active = true;
    });
    ctx.stroke(); ctx.setLineDash([]);
  };
  series.forEach(([values, color, dash]) => plot(values, color, dash));
  ctx.fillText("°C · 3 MINUTE WINDOW", pad.l, h - 4);
}

function appendHistory(key, value) {
  history[key].push(value);
  if (history[key].length > maxHistory) history[key].shift();
}

function loadHistory(data, nodes, router) {
  const points = Array.isArray(data.history) ? data.history.slice(-maxHistory) : [];
  if (points.length) {
    const nodeValue = (point, name, key) => (((point.nodes || {})[name] || {})[key]);
    history.cluster = points.map(point => point.generation_tokens_per_second);
    history.spark1Gpu = points.map(point => nodeValue(point, "spark1", "gpu_c"));
    history.spark1Cpu = points.map(point => nodeValue(point, "spark1", "cpu_cluster_max_c"));
    history.spark2Gpu = points.map(point => nodeValue(point, "spark2", "gpu_c"));
    history.spark2Cpu = points.map(point => nodeValue(point, "spark2", "cpu_cluster_max_c"));
    return;
  }
  appendHistory("cluster", router.backend_generation_tokens_per_second);
  appendHistory("spark1Gpu", (((nodes.spark1 || {}).system || {}).gpu || {}).temperature_c);
  appendHistory("spark1Cpu", (((nodes.spark1 || {}).system || {}).thermals || {}).cpu_cluster_max_c);
  appendHistory("spark2Gpu", (((nodes.spark2 || {}).system || {}).gpu || {}).temperature_c);
  appendHistory("spark2Cpu", (((nodes.spark2 || {}).system || {}).thermals || {}).cpu_cluster_max_c);
}

function drawCharts() {
  drawChart();
  drawTemperatureChart();
}

async function refresh() {
  try {
    const response = await fetch("/api/status", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json(), nodes = data.nodes || {}, router = data.router || {};
    q("nodes").innerHTML = ["spark1", "spark2"].map((name, i) => nodeTemplate(name, nodes[name] || {}, i + 1)).join("");
    q("endpoint-eyebrow").textContent = router.mode === "direct" ? "TENSOR-PARALLEL API" : "UNIFIED ENDPOINT";
    q("endpoint-title").textContent = router.label || (router.mode === "direct" ? "TP2 aggregate endpoint" : "Router");
    q("router-url").textContent = router.url || "—";
    q("cluster-out").textContent = rate(router.backend_generation_tokens_per_second);
    q("cluster-in").textContent = rate(router.backend_prompt_tokens_per_second);
    q("router-rps").textContent = rate((router.rates || {}).requests_per_second);
    const state = q("router-state");
    state.textContent = router.mode === "direct"
      ? `${router.healthy ? "SERVING" : "OFFLINE"} · ${num(router.active_ranks, 0)}/${num(router.expected_ranks, 0)} RANKS`
      : router.healthy
        ? `ROUTING · ${num(router.active_workers, 0)} WORKERS`
        : router.state === "starting"
          ? `STARTING · ${num(router.active_workers, 0)} WORKERS`
          : "OFFLINE";
    state.className = `state ${router.healthy ? "online" : router.state === "starting" ? "" : "offline"}`;
    loadHistory(data, nodes, router);
    drawCharts();
    const stamp = data.generated_at ? new Date(data.generated_at) : null;
    const age = stamp ? (Date.now() - stamp.getTime()) / 1000 : Infinity;
    const fresh = q("freshness");
    fresh.className = `freshness ${age < 8 ? "live" : "stale"}`;
    fresh.querySelector("span").textContent = age < 8 ? `Live · ${stamp.toLocaleTimeString()}` : `Stale · ${num(age, 0)}s`;
  } catch (error) {
    const fresh = q("freshness");
    fresh.className = "freshness stale";
    fresh.querySelector("span").textContent = `Dashboard unavailable · ${error.message}`;
  }
}
window.addEventListener("resize", drawCharts);
refresh();
setInterval(refresh, 2000);
