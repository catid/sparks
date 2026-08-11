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
      hosts: {},
    },
    {
      timestamp: "2026-08-10T18:30:05Z",
      cluster: { cpu_percent: 22, gpu_percent: 70, ram_percent: 63 },
      throughput: { state: "online", tokens_per_second: 140 },
      hosts: {},
    },
  ],
};

test("normalizes the backend status contract without treating missing data as zero", () => {
  const payload = ui.normalizePayload(statusPayload);

  assert.equal(payload.interval_seconds, 5);
  assert.equal(payload.cluster.cpu_percent, 23.8);
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
  assert.equal(sparse.cluster.ram_percent, null);
  assert.equal(sparse.throughput.tokens_per_second, null);
});

test("accepts the common C1, Spark 2, and cerebrus3 host labels", () => {
  assert.equal(ui.hostSlot("C1", {}, 0), 1);
  assert.equal(ui.hostSlot("Spark 2", {}, 0), 2);
  assert.equal(ui.hostSlot("telemetry", { hostname: "cerebrus3" }, 0), 3);
});

test("builds each graph from the nested history fields and ends at current value", () => {
  const payload = ui.normalizePayload(statusPayload);

  assert.deepEqual(Array.from(ui.metricSeries(payload, "cpu")), [20, 23.8]);
  assert.deepEqual(Array.from(ui.metricSeries(payload, "tokens")), [132, 144.7]);
  assert.deepEqual(Array.from(ui.metricSeries(payload, "gpu")), [65, 71.8]);
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
  assert.equal(ui.formatCurrent(0, "cpu"), "0.0");
  assert.equal(ui.formatCurrent(0, "tokens"), "0");
  assert.equal(ui.niceCeiling(144.7 * 1.08), 200);
  assert.deepEqual(
    { ...ui.metricStats([null, 0, 10, 4]) },
    { min: 0, max: 10, delta: 4 },
  );
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

test("renderer exposes current cluster values and all three host values", () => {
  const ids = [
    "dashboard", "cluster-indicator", "cluster-state", "host-count", "sample-time",
    "sample-age", "connection-message",
  ];
  for (const metric of ["cpu", "gpu", "ram", "tokens"]) {
    ids.push(
      `${metric}-value`, `${metric}-range`, `${metric}-delta`, `${metric}-scale`,
      `${metric}-line`, `${metric}-area`, `${metric}-dot`, `${metric}-chart`,
    );
  }
  ids.push("tokens-state");
  for (const slot of [1, 2, 3]) {
    ids.push(`host-c${slot}`, `c${slot}-state`, `c${slot}-cpu`, `c${slot}-gpu`, `c${slot}-ram`);
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
  assert.equal(elements["cpu-value"].textContent, "23.8");
  assert.equal(elements["tokens-value"].textContent, "145");
  assert.equal(elements["tokens-state"].textContent, "LIVE");
  assert.equal(elements["tokens-state"].dataset.state, "active");
  assert.equal(elements["c1-cpu"].textContent, "21");
  assert.equal(elements["c2-gpu"].textContent, "75");
  assert.equal(elements["c3-ram"].textContent, "61");
  assert.equal(elements["c3-ram"].dataset.available, "true");
  assert.match(elements["tokens-line"].attributes.d, /^M/);
  assert.equal(cards.tokens.dataset.state, "ready");
  assert.equal(cards.tokens.dataset.throughputState, "active");
});

test("static shell is self-contained and contains the exact rack-display regions", () => {
  const html = fs.readFileSync(path.join(staticDirectory, "index.html"), "utf8");
  const css = fs.readFileSync(path.join(staticDirectory, "style.css"), "utf8");

  assert.match(html, /href="\/style\.css"/);
  assert.match(html, /src="\/app\.js"/);
  assert.doesNotMatch(`${html}\n${css}`, /https?:\/\//);
  assert.equal((html.match(/class="metric-card /g) || []).length, 4);
  assert.equal((html.match(/class="node-row"/g) || []).length, 3);
  assert.match(html, /id="tokens-state"/);
  assert.match(css, /grid-template-columns:\s*minmax\(0, 1fr\) 398px/);
  assert.match(css, /overflow:\s*hidden/);
  assert.match(html, /aria-live="polite"/);
  assert.match(html, /aria-live="assertive"/);
});
