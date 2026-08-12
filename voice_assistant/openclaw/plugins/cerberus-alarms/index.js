import http from "node:http";

const SOCKET_PATH = "/run/cerberus3-alarms/api.sock";
const REQUEST_TIMEOUT_MS = 3000;
const MAX_RESPONSE_BYTES = 256 * 1024;

function shortString(value, maxLength) {
  if (typeof value !== "string") return null;
  const normalized = value.replace(/\s+/g, " ").trim();
  return normalized ? normalized.slice(0, maxLength) : null;
}

function sanitizeAlarm(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("alarm service returned an invalid event");
  }
  const id = typeof value.id === "string" && /^[0-9a-f]{12}$/.test(value.id)
    ? value.id
    : null;
  const kind = value.kind === "timer" || value.kind === "alarm" ? value.kind : null;
  const status = ["pending", "ringing", "dismissed", "cancelled", "expired"].includes(value.status)
    ? value.status
    : null;
  const boundedString = (candidate, maxLength) => {
    if (typeof candidate !== "string" || candidate.length > maxLength) return null;
    const normalized = candidate.replace(/\s+/g, " ").trim();
    return normalized || null;
  };
  const dueAt = boundedString(value.due_at, 64);
  const localDueAt = boundedString(value.local_due_at, 64);
  const timezone = boundedString(value.timezone, 64);
  let label = null;
  if (value.label !== null && value.label !== undefined) {
    label = boundedString(value.label, 80);
    if (!label) throw new Error("alarm service returned an incomplete event");
  }
  if (!id || !kind || !status || !dueAt || !localDueAt || !timezone) {
    throw new Error("alarm service returned an incomplete event");
  }
  return {
    id,
    kind,
    label,
    status,
    due_at: dueAt,
    local_due_at: localDueAt,
    timezone,
  };
}

function requireAlarmId(value) {
  if (typeof value !== "string" || !/^[0-9a-f]{12}$/.test(value)) {
    throw new Error("alarm id must contain exactly 12 lowercase hexadecimal characters");
  }
  return value;
}

export function requestAlarmService(
  path,
  {
    method = "GET",
    body,
    requestImpl = http.request,
    timeoutMs = REQUEST_TIMEOUT_MS,
  } = {},
) {
  return new Promise((resolve, reject) => {
    const encoded = body === undefined ? null : Buffer.from(JSON.stringify(body), "utf8");
    let request;
    let settled = false;

    const finish = (callback, value) => {
      if (settled) return false;
      settled = true;
      clearTimeout(totalTimer);
      callback(value);
      return true;
    };
    const fail = (error, destroy = false) => {
      if (!finish(reject, error)) return;
      if (destroy && typeof request?.destroy === "function") {
        try {
          request.destroy();
        } catch {
          // The promise is already rejected with the bounded transport error.
        }
      }
    };
    const totalTimer = setTimeout(
      () => fail(new Error("alarm service timed out"), true),
      timeoutMs,
    );

    try {
      request = requestImpl(
        {
          socketPath: SOCKET_PATH,
          path,
          method,
          headers: encoded
            ? { "content-type": "application/json", "content-length": String(encoded.length) }
            : { accept: "application/json" },
        },
        (response) => {
          const chunks = [];
          let length = 0;
          let ended = false;
          response.on("data", (chunk) => {
            if (settled) return;
            const bytes = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
            length += bytes.length;
            if (length > MAX_RESPONSE_BYTES) {
              fail(new Error("alarm service response was too large"), true);
              return;
            }
            chunks.push(bytes);
          });
          response.on("aborted", () => {
            fail(new Error("alarm service response ended prematurely"), true);
          });
          response.on("error", () => {
            fail(new Error("alarm service response failed"), true);
          });
          response.on("close", () => {
            if (!ended) {
              fail(new Error("alarm service response ended prematurely"), true);
            }
          });
          response.on("end", () => {
            ended = true;
            if (settled) return;
            let payload;
            try {
              payload = JSON.parse(Buffer.concat(chunks).toString("utf8"));
            } catch {
              fail(new Error("alarm service returned invalid JSON"));
              return;
            }
            if (!Number.isInteger(response.statusCode) || response.statusCode < 200 || response.statusCode >= 300) {
              const message = shortString(payload?.error, 160) ?? `HTTP ${response.statusCode ?? "error"}`;
              fail(new Error(`alarm service rejected the request: ${message}`));
              return;
            }
            finish(resolve, payload);
          });
        },
      );
      request.setTimeout(timeoutMs, () => {
        fail(new Error("alarm service timed out"), true);
      });
      request.on("error", () => {
        fail(new Error("alarm service request failed"));
      });
      if (encoded) request.write(encoded);
      request.end();
    } catch {
      fail(new Error("alarm service request failed"), true);
    }
  });
}

function toolResult(details) {
  return {
    content: [{ type: "text", text: JSON.stringify(details) }],
    details,
  };
}

export function createAlarmTools(request = requestAlarmService) {
  return [
    {
      name: "timer_set",
      label: "Set Timer",
      description: "Set a persistent countdown timer on the Cerberus speaker. Use only after determining the exact whole-number duration in seconds.",
      parameters: {
        type: "object",
        additionalProperties: false,
        required: ["duration_seconds"],
        properties: {
          duration_seconds: { type: "integer", minimum: 1, maximum: 604800 },
          label: { type: "string", minLength: 1, maxLength: 80 },
        },
      },
      async execute(_toolCallId, args) {
        const payload = await request("/v1/alarms", {
          method: "POST",
          body: { kind: "timer", duration_seconds: args.duration_seconds, ...(args.label ? { label: args.label } : {}) },
        });
        return toolResult({ alarm: sanitizeAlarm(payload.alarm) });
      },
    },
    {
      name: "alarm_set",
      label: "Set Alarm",
      description: "Set a persistent alarm on the Cerberus speaker. due_at must be a future ISO 8601 timestamp with an explicit UTC offset; clarify AM or PM before calling when needed.",
      parameters: {
        type: "object",
        additionalProperties: false,
        required: ["due_at"],
        properties: {
          due_at: { type: "string", minLength: 20, maxLength: 64 },
          label: { type: "string", minLength: 1, maxLength: 80 },
        },
      },
      async execute(_toolCallId, args) {
        const payload = await request("/v1/alarms", {
          method: "POST",
          body: { kind: "alarm", due_at: args.due_at, ...(args.label ? { label: args.label } : {}) },
        });
        return toolResult({ alarm: sanitizeAlarm(payload.alarm) });
      },
    },
    {
      name: "alarms_list",
      label: "List Timers and Alarms",
      description: "List all pending or ringing Cerberus timers and alarms, including their IDs and local due times.",
      parameters: { type: "object", additionalProperties: false, properties: {} },
      async execute() {
        const payload = await request("/v1/alarms");
        if (!Array.isArray(payload.alarms)) throw new Error("alarm service returned an invalid list");
        return toolResult({ alarms: payload.alarms.slice(0, 100).map(sanitizeAlarm) });
      },
    },
    {
      name: "alarm_cancel",
      label: "Cancel Timer or Alarm",
      description: "Cancel one pending or ringing Cerberus timer or alarm by the exact ID returned when it was set or listed.",
      parameters: {
        type: "object",
        additionalProperties: false,
        required: ["id"],
        properties: { id: { type: "string", pattern: "^[0-9a-f]{12}$" } },
      },
      async execute(_toolCallId, args) {
        const alarmId = requireAlarmId(args.id);
        const payload = await request(`/v1/alarms/${alarmId}/cancel`, { method: "POST", body: {} });
        return toolResult({ alarm: sanitizeAlarm(payload.alarm) });
      },
    },
    {
      name: "alarm_dismiss",
      label: "Stop Ringing Alarm",
      description: "Stop currently ringing Cerberus timers and alarms. Omit id to stop every alarm that is ringing now.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: { id: { type: "string", pattern: "^[0-9a-f]{12}$" } },
      },
      async execute(_toolCallId, args = {}) {
        const alarmId = args.id === undefined ? undefined : requireAlarmId(args.id);
        const payload = await request("/v1/alarms/dismiss", {
          method: "POST",
          body: alarmId ? { id: alarmId } : {},
        });
        if (!Array.isArray(payload.dismissed)) throw new Error("alarm service returned an invalid dismissal");
        return toolResult({ dismissed: payload.dismissed.slice(0, 100).map(sanitizeAlarm) });
      },
    },
  ];
}

export default {
  id: "cerberus-alarms",
  name: "Cerberus Timers and Alarms",
  description: "Constrained access to persistent local speaker timers and alarms.",
  register(api) {
    for (const tool of createAlarmTools()) api.registerTool(tool, { optional: true });
  },
};
