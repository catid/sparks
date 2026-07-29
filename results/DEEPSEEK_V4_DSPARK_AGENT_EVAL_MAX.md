# DeepSeek V4 Flash DSpark max-context agent evaluation

Run:
[`20260729T153346Z-af97febb`](./deepseek-v4-dspark-agent-eval-max/20260729T153346Z-af97febb/)

## Result

The run is an official **fail**. It reached the 24-turn ceiling with every
assistant response ending in another tool call and no final response. The
post-run visible and hidden graders both happened to pass, but an independent
semantic audit found that the final implementation did not satisfy the task.

The task was a realistic OpenClaw-style Python cancellation bug. The model
needed to make concurrent and repeated `WorkerPool.stop()` calls idempotent
while ensuring cancellation of one caller could not interrupt each worker's
asynchronous cleanup barrier.

The final patch caught and discarded `CancelledError` inside the worker's
cleanup:

```python
try:
    await self.cleanup_release.wait()
except asyncio.CancelledError:
    pass
self.cleanup_finished += 1
```

That makes the supplied counters and tests pass without preserving the
required cleanup semantics. A read-only follow-up probe observed
`cleanup_release.is_set() == False` while `cleanup_finished == 4` and all
workers were already done. In other words, cancellation skipped the cleanup
barrier and merely made the completion counter look correct.

## Behavior

The trajectory was safe and technically capable, but too meandering:

- It inspected the repository, reproduced the failure, and used focused
  asyncio experiments rather than guessing.
- It correctly discovered that cancellation of a caller waiting on
  `asyncio.gather()` propagates to the gathered worker tasks.
- It validated `asyncio.shield()` as the key mechanism for preventing that
  cascade and recognized the need for one shared shutdown operation.
- It then abandoned that robust direction on turn 23 for the
  catch-and-discard workaround above.
- Turn 24 ran the visible unittest file successfully, but the model had no turn
  left to inspect the result, remove `worker_pool.py.bak`, or provide a final
  answer.

The grader reported `HIDDEN_OK`, but that hidden test shared the same
counter-based blind spot as the visible test. Passing tests therefore did not
override the semantic audit.

### Why the tests passed

Both visible and hidden tests followed this sequence:

1. start workers and call `stop()`;
2. wait until worker cleanup begins;
3. cancel one caller awaiting `stop()`;
4. set `cleanup_release`;
5. assert only the final counter and absence of live workers.

They never inspected the crucial intermediate state between steps 3 and 4.
The missing assertion was that `cleanup_finished` must still be zero and every
worker must remain alive while `cleanup_release` is false. Against the saved
candidate, a one-worker probe produced:

```text
before caller cancellation: release=False finished=0 live=1
after caller cancellation:  release=False finished=1 live=0 worker_done=[True]
```

In a real service, that corresponds to an async flush, close, or commit being
aborted while bookkeeping falsely reports success.

The bug originates in `stop()`: cancelling a task that directly awaits
`asyncio.gather()` cancels the gather future, which propagates a second
cancellation into the workers while they are inside their `finally` cleanup.
The model correctly proved this and, on turn 21, verified that
`asyncio.shield()` kept workers alive until the release barrier. A small robust
shape would change cancellation ownership in `stop()`, not weaken cleanup:

```python
async def stop(self):
    workers = tuple(self._workers)
    if not self._closing:
        self._closing = True
        for task in workers:
            task.cancel()
    await asyncio.shield(asyncio.gather(*workers, return_exceptions=True))
    self._workers.difference_update(workers)
```

A production implementation would preferably create one shared internal
shutdown task and have all callers await it through `shield()`.

### Turn-by-turn failure mode

- Turns 1--4 inspected the code and reproduced the focused unittest failure.
- Turns 5--12 spent heavily on asyncio/CPython cancellation experiments.
- Turns 13--15 installed an `_closing`-only patch, which still failed.
- Turns 16--20 isolated cancellation propagation through `gather()`.
- Turns 21--22 experimentally validated and discussed `shield()` plus
  one-time cancellation—the right abstraction.
- Turn 23 nevertheless replaced the worker cleanup with the
  catch-and-discard workaround.
- Turn 24 ran the full visible file successfully. The turn budget then ended,
  leaving no final answer, no separate focused-then-full evidence report, and
  an unnecessary `worker_pool.py.bak`.

## Measured trajectory

| Metric | Value |
|---|---:|
| Turns | 24 / 24 |
| Completion tokens | 34,114 |
| Cumulative prompt tokens | 529,951 |
| API generation time | 648.231 s |
| Wall time | 656.047 s |
| Output throughput, API-only | 52.626 tok/s |
| Output throughput, wall-clock | 51.998 tok/s |
| Tool calls | 25 |
| Invalid tool calls | 0 |
| Policy violations | 0 |
| Tool-output truncations | 0 |
| Tool errors | 0 |

Every request used maximum reasoning, thinking enabled, temperature/top-p
1.0, and the exact remaining part of the 1,048,576-token context window as its
output ceiling. The final rendered prompt was 41,045 tokens and the API was
offered up to 1,007,531 output tokens. Natural tool-call stops, not an
artificial token cap, ended each turn.

“Max-context” therefore describes the configured allowance and exact
remaining-context budgeting, not a one-million-token input test. The actual
largest prompt was 41,045 tokens, only 3.91% of the window. Likewise, 52.626
tok/s is `34,114 completion tokens / 648.231 API seconds`; it includes
reasoning and tool-call generation. It is decoder throughput across a
sequential trajectory, not useful final-answer throughput or proof that the
agent completed 52.6 tokens of correct work each second.

The sandbox had no network, a read-only root, all Linux capabilities dropped,
`no-new-privileges`, no Docker socket, and only the disposable workspace and
temporary directory writable. Protected tests were unchanged, cleanup
succeeded, and no host credential was exposed.

## Deployment verdict

This one difficult trajectory is not a broad capability benchmark, but it is
directly relevant to an autonomous coding agent. The native DSpark checkpoint
is materially better and more than twice as fast as the earlier ModelOpt +
DFlash run on the same cancellation task: it reached a production-code edit
and passing tests at about 52.6 output tok/s, whereas the earlier run reached
24 turns without editing production code at about 21.6 tok/s on this task.

It still should not be given an unsupervised OpenClaw or Hermes role with broad
host permissions. Recommended use is supervised, disposable-sandbox execution
with semantic invariants, hidden tests, strict turn/time budgets, and a
requirement for a final completion report. A verifier must assess behavior,
not just test exit status.

Primary artifacts:

- [`SUMMARY.json`](./deepseek-v4-dspark-agent-eval-max/20260729T153346Z-af97febb/SUMMARY.json)
- [`task summary`](./deepseek-v4-dspark-agent-eval-max/20260729T153346Z-af97febb/worker_pool_cancel/summary.json)
- [`trajectory`](./deepseek-v4-dspark-agent-eval-max/20260729T153346Z-af97febb/worker_pool_cancel/trajectory.json)
- [`raw SSE events`](./deepseek-v4-dspark-agent-eval-max/20260729T153346Z-af97febb/worker_pool_cancel/events.jsonl)
- [`final workspace`](./deepseek-v4-dspark-agent-eval-max/20260729T153346Z-af97febb/worker_pool_cancel/workspace/)
