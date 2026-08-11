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
    cerebrus1: {
      state: "online",
      error: null,
      cpu_percent: 21.2,
      gpu_percent: 71.8,
      ram_percent: 63.4,
      ram_used_bytes: 81234567890,
      ram_total_bytes: 128000000000,
      age_seconds: 1.2,
    },
    cerebrus2: {
      state: "online",
      error: null,
      cpu_percent: 31.7,
      gpu_percent: 75.1,
      ram_percent: 64.8,
      age_seconds: 0.8,
    },
    cerebrus3: {
      state: "online",
      error: null,
      cpu_percent: 18.5,
      gpu_percent: 68.4,
      ram_percent: 61.1,
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
        cerebrus1: { state: "online", cpu_percent: 19, gpu_percent: 63, ram_percent: 62 },
        cerebrus2: { state: "online", cpu_percent: 29, gpu_percent: 68, ram_percent: 64 },
        cerebrus3: { state: "online", cpu_percent: 12, gpu_percent: 61, ram_percent: 59 },
      },
    },
    {
      timestamp: "2026-08-10T18:30:05Z",
      cluster: { cpu_percent: 22, gpu_percent: 70, ram_percent: 63 },
      throughput: { state: "online", tokens_per_second: 140 },
      hosts: {
        cerebrus1: { state: "online", cpu_percent: 20, gpu_percent: 70, ram_percent: 63 },
        cerebrus2: { state: "online", cpu_percent: 32, gpu_percent: 74, ram_percent: 65 },
        cerebrus3: { state: "online", cpu_percent: 18, gpu_percent: 68, ram_percent: 61 },
      },
    },
  ],
};

test("normalizes the backend status contract without treating missing data as zero", () => {
  const payload = ui.normalizePayload(statusPayload);

  assert.equal(payload.interval_seconds, 5);
  assert.equal(payload.cluster.cpu_percent, undefined);
  assert.equal(payload.throughput.tokens_per_second, 144.7);
  assert.deepEqual(Array.from(payload.hosts, (host) => host.slot), [1, 2, 3]);
  assert.equal(payload.hosts[0].ram_total_bytes, 128000000000);

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

test("accepts the common C1, Spark 2, and cerebrus3 host labels", () => {
  assert.equal(ui.hostSlot("C1", {}, 0), 1);
  assert.equal(ui.hostSlot("Spark 2", {}, 0), 2);
  assert.equal(ui.hostSlot("telemetry", { hostname: "cerebrus3" }, 0), 3);
});

test("builds independent host graphs and API-wide token history", () => {
  const payload = ui.normalizePayload(statusPayload);

  assert.deepEqual(Array.from(ui.hostMetricSeries(payload, "cpu", 1)), [19, 21.2]);
  assert.deepEqual(Array.from(ui.hostMetricSeries(payload, "cpu", 2)), [29, 31.7]);
  assert.deepEqual(Array.from(ui.hostMetricSeries(payload, "gpu", 3)), [61, 68.4]);
  assert.deepEqual(Array.from(ui.tokenSeries(payload)), [132, 144.7]);
});

test("missing host samples remain gaps rather than cluster-average fallbacks", () => {
  const payload = ui.normalizePayload({
    hosts: { cerebrus1: { state: "online", cpu_percent: 25 } },
    history: [
      { hosts: { cerebrus1: { cpu_percent: 10 } } },
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

test("renderer exposes per-node values and honest API-wide token scope", () => {
  const ids = [
    "dashboard", "cluster-indicator", "cluster-state", "host-count", "sample-time",
    "sample-age", "connection-message",
  ];
  for (const metric of ["cpu", "gpu", "ram"]) {
    for (const slot of [1, 2, 3]) {
      ids.push(
        `${metric}-c${slot}-row`, `${metric}-c${slot}-value`,
        `${metric}-c${slot}-line`, `${metric}-c${slot}-dot`, `${metric}-c${slot}-chart`,
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
  assert.equal(elements["tokens-value"].textContent, "145");
  assert.equal(elements["tokens-delta"].textContent, "API AGG · NOT PER NODE");
  assert.equal(elements["tokens-state"].textContent, "LIVE");
  assert.equal(elements["tokens-state"].dataset.state, "active");
  assert.equal(elements["ram-c3-value"].dataset.available, "true");
  assert.match(elements["cpu-c1-line"].attributes.d, /^M/);
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
  assert.equal((html.match(/class="metric-card /g) || []).length, 4);
  assert.equal((html.match(/class="trace-row /g) || []).length, 9);
  assert.equal((html.match(/class="host-state"/g) || []).length, 3);
  assert.match(html, /id="tokens-state"/);
  assert.match(html, /id="ambient-canvas"[^>]+width="178"[^>]+height="35"/);
  assert.equal((html.match(/PER NODE · %/g) || []).length, 3);
  assert.doesNotMatch(html, /CLUSTER AVG/);
  assert.match(html, /C1\+C2 MODEL AGGREGATE/);
  assert.match(html, /NOT PER NODE/);
  assert.match(css, /grid-template-columns:\s*repeat\(4, minmax\(0, 1fr\)\)/);
  assert.match(css, /height:\s*100dvh/);
  assert.match(css, /grid-template-rows:\s*34px minmax\(0, 1fr\) 17px/);
  assert.match(css, /image-rendering:\s*pixelated/);
  assert.match(css, /overflow:\s*hidden/);
  assert.match(html, /aria-live="polite"/);
  assert.match(html, /aria-live="assertive"/);
});
