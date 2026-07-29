## Model routing

Use the local `ds4f` model for normal planning, coding, shell work, research,
and conversation. It is the default because it is private, fast, and does not
incur per-token API charges.

Use the `llm-task` tool with provider `openai`, model `gpt-5.6-sol`, and
thinking level `max` only when an independent, quality-first judgment is worth
the extra latency, external data disclosure, and API cost. Good escalation
cases are:

- reviewing a destructive, security-sensitive, or difficult-to-reverse plan;
- checking architecture or concurrency logic where a subtle error could pass
  ordinary tests;
- diagnosing a task whose tests pass but whose actual user objective still
  appears unmet;
- comparing a small number of consequential alternatives against explicit
  success criteria; or
- performing a final semantic verification before an important external
  action.

Give `llm-task` a bounded input and a strict JSON Schema. Ask it for findings,
evidence, severity, and a recommended action. Treat its result as advisory:
inspect the cited local evidence yourself, do not let the reviewer perform
tools or side effects, and retain normal approval boundaries. Do not escalate
routine formatting, file discovery, status checks, or already well-tested
mechanical work.

For long sessions, preserve the current goal, decisions, exact identifiers,
unfinished work, validation evidence, and blockers before context is compacted.
