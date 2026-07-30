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
const ui = context.DashboardHealthUI;

test("down endpoint uses the cluster health contract", () => {
  const model = ui.healthViewModel({
    cluster: {
      state: "down",
      healthy: false,
      endpoint_healthy: false,
      affected_nodes: ["spark1", "spark2"],
      reason: "rank 1 worker is unreachable",
      outage_started_at: "2026-07-29T11:58:00Z",
      outage_elapsed_seconds: 125,
      recovery_started_at: null,
      endpoint: {
        healthy: false,
        state: "down",
        url: "http://spark1:8889/v1",
        reason: "endpoint refused connection",
      },
    },
  }, Date.parse("2026-07-29T12:00:05Z"));

  assert.equal(model.state, "down");
  assert.equal(model.label, "ENDPOINT DOWN");
  assert.equal(model.title, "Inference requests are unavailable");
  assert.equal(model.reason, "rank 1 worker is unreachable");
  assert.deepEqual(Array.from(model.affected), ["spark1", "spark2"]);
  assert.equal(model.elapsedLabel, "Down for");
  assert.equal(model.outageElapsed, 125);
});

test("degraded endpoint remains distinct from a full outage", () => {
  const model = ui.healthViewModel({
    cluster: {
      state: "degraded",
      healthy: false,
      endpoint_healthy: true,
      affected_nodes: ["spark2"],
      reason: "one replica is offline",
      outage_elapsed_seconds: 72,
    },
  });

  assert.equal(model.state, "degraded");
  assert.equal(model.label, "DEGRADED");
  assert.equal(model.title, "Inference capacity is degraded");
  assert.equal(model.elapsedLabel, "Degraded for");
});

test("recovering state reports both outage and recovery timers", () => {
  const now = Date.parse("2026-07-29T12:10:15Z");
  const model = ui.healthViewModel({
    cluster: {
      state: "recovering",
      healthy: false,
      endpoint_healthy: true,
      affected_nodes: ["spark2"],
      reason: "",
      outage_elapsed_seconds: 605,
      recovery_started_at: "2026-07-29T12:10:00Z",
    },
  }, now);

  assert.equal(model.state, "recovering");
  assert.equal(model.label, "RECOVERING");
  assert.equal(model.elapsedLabel, "Outage duration");
  assert.equal(model.outageElapsed, 605);
  assert.equal(model.recoveryElapsed, 15);
});

test("legacy starting state is derived when cluster metadata is absent", () => {
  const model = ui.healthViewModel({
    router: { healthy: false, state: "starting" },
  });

  assert.equal(model.state, "starting");
  assert.equal(model.label, "STARTING");
  assert.match(model.title, /starting/i);
});

test("duration formatter is stable and concise", () => {
  assert.equal(ui.formatDuration(0), "0s");
  assert.equal(ui.formatDuration(65), "1m 5s");
  assert.equal(ui.formatDuration(3661), "1h 1m");
  assert.equal(ui.formatDuration(90061), "1d 1h");
  assert.equal(ui.formatDuration(null), "—");
});

test("dashboard transport failures do not masquerade as a healthy endpoint", () => {
  const model = ui.unavailableHealthViewModel(new Error("HTTP 503"), 14);

  assert.equal(model.state, "down");
  assert.equal(model.label, "HEALTH API DOWN");
  assert.equal(model.reason, "HTTP 503");
  assert.deepEqual(Array.from(model.affected), ["dashboard health API"]);
  assert.equal(model.outageElapsed, 14);
});

test("renderer makes outages prominent and collapses a healthy state", () => {
  const ids = [
    "cluster-health",
    "health-state",
    "health-title",
    "health-reason",
    "health-affected-item",
    "health-affected-label",
    "health-affected",
    "health-elapsed-item",
    "health-elapsed-label",
    "health-elapsed",
    "health-recovery-item",
    "health-recovery",
    "health-announcer",
  ];
  const elements = Object.fromEntries(ids.map((id) => [id, {
    className: "",
    hidden: false,
    textContent: "",
  }]));
  context.document = {
    title: "",
    getElementById(id) {
      return elements[id];
    },
  };

  ui.renderClusterHealth(ui.healthViewModel({
    cluster: {
      state: "down",
      affected_nodes: ["spark2"],
      reason: "worker unreachable",
      outage_elapsed_seconds: 91,
    },
  }));
  assert.equal(elements["cluster-health"].className, "cluster-health cluster-health--down");
  assert.equal(elements["health-state"].textContent, "ENDPOINT DOWN");
  assert.equal(elements["health-affected-item"].hidden, false);
  assert.equal(elements["health-affected"].textContent, "spark2");
  assert.equal(elements["health-elapsed"].textContent, "1m 31s");
  assert.equal(context.document.title, "DOWN · Spark Array");

  ui.renderClusterHealth(ui.healthViewModel({
    cluster: { state: "serving", affected_nodes: [], reason: "" },
  }));
  assert.equal(elements["cluster-health"].className, "cluster-health cluster-health--serving");
  assert.equal(elements["health-affected-item"].hidden, true);
  assert.equal(elements["health-elapsed-item"].hidden, true);
  assert.equal(context.document.title, "Spark Array");
});

test("health banner has accessible fixed fields and state styling", () => {
  const html = fs.readFileSync(path.join(staticDirectory, "index.html"), "utf8");
  const css = fs.readFileSync(path.join(staticDirectory, "style.css"), "utf8");

  for (const id of [
    "cluster-health",
    "health-state",
    "health-title",
    "health-reason",
    "health-affected",
    "health-elapsed",
    "health-recovery",
    "health-announcer",
  ]) {
    assert.match(html, new RegExp(`id="${id}"`));
  }
  assert.match(html, /aria-live="assertive"/);
  for (const state of ["serving", "degraded", "down", "recovering", "starting", "stale"]) {
    assert.match(css, new RegExp(`\\.cluster-health--${state}\\b`));
  }
});
