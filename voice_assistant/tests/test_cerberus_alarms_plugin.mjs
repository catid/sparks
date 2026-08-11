import assert from "node:assert/strict";
import test from "node:test";

import plugin, { createAlarmTools } from "../openclaw/plugins/cerberus-alarms/index.js";

const sampleAlarm = {
  id: "0123456789ab",
  kind: "timer",
  label: "pasta",
  status: "pending",
  due_at: "2026-08-11T22:00:00Z",
  local_due_at: "2026-08-11T17:00:00-05:00",
  timezone: "America/Chicago",
  private_value: "must not escape",
};

test("registers five optional constrained tools", () => {
  const registrations = [];
  plugin.register({
    registerTool(tool, options) {
      registrations.push([tool.name, options]);
    },
  });
  assert.deepEqual(
    registrations.map(([name]) => name),
    ["timer_set", "alarm_set", "alarms_list", "alarm_cancel", "alarm_dismiss"],
  );
  for (const [, options] of registrations) assert.deepEqual(options, { optional: true });
});

test("timer and alarm tools send only structured schedule fields", async () => {
  const calls = [];
  const request = async (path, options = {}) => {
    calls.push([path, options]);
    return { alarm: sampleAlarm };
  };
  const tools = Object.fromEntries(createAlarmTools(request).map((tool) => [tool.name, tool]));
  const timer = await tools.timer_set.execute("call-1", {
    duration_seconds: 300,
    label: "pasta",
  });
  await tools.alarm_set.execute("call-2", {
    due_at: "2026-08-12T07:00:00-05:00",
  });
  assert.deepEqual(calls, [
    ["/v1/alarms", { method: "POST", body: { kind: "timer", duration_seconds: 300, label: "pasta" } }],
    ["/v1/alarms", { method: "POST", body: { kind: "alarm", due_at: "2026-08-12T07:00:00-05:00" } }],
  ]);
  assert.equal(timer.details.alarm.local_due_at, sampleAlarm.local_due_at);
  assert.equal(JSON.stringify(timer.details).includes("must not escape"), false);
});

test("list cancel and dismiss use fixed API routes", async () => {
  const calls = [];
  const request = async (path, options = {}) => {
    calls.push([path, options]);
    if (path.endsWith("/cancel")) return { alarm: { ...sampleAlarm, status: "cancelled" } };
    if (path.endsWith("/dismiss")) return { dismissed: [{ ...sampleAlarm, status: "dismissed" }] };
    return { alarms: [sampleAlarm] };
  };
  const tools = Object.fromEntries(createAlarmTools(request).map((tool) => [tool.name, tool]));
  const listed = await tools.alarms_list.execute("call-1", {});
  const cancelled = await tools.alarm_cancel.execute("call-2", { id: sampleAlarm.id });
  const dismissed = await tools.alarm_dismiss.execute("call-3", {});
  assert.equal(listed.details.alarms.length, 1);
  assert.equal(cancelled.details.alarm.status, "cancelled");
  assert.equal(dismissed.details.dismissed[0].status, "dismissed");
  assert.deepEqual(calls, [
    ["/v1/alarms", {}],
    [`/v1/alarms/${sampleAlarm.id}/cancel`, { method: "POST", body: {} }],
    ["/v1/alarms/dismiss", { method: "POST", body: {} }],
  ]);
});

test("rejects malformed service data", async () => {
  const tools = Object.fromEntries(
    createAlarmTools(async () => ({ alarm: { id: "not-complete" } })).map((tool) => [tool.name, tool]),
  );
  await assert.rejects(tools.timer_set.execute("call", { duration_seconds: 1 }), /incomplete/);
});

test("rejects an invalid id before constructing an API path", async () => {
  let called = false;
  const tools = Object.fromEntries(
    createAlarmTools(async () => {
      called = true;
      return {};
    }).map((tool) => [tool.name, tool]),
  );
  await assert.rejects(
    tools.alarm_cancel.execute("call", { id: "../../health" }),
    /12 lowercase hexadecimal/,
  );
  assert.equal(called, false);
});
