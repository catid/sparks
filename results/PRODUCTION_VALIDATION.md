# Production validation

Validated July 29, 2026 after the rolling deployment.

## Live serving configuration

Both Spark 1 and Spark 2:

- target: `poolside/Laguna-S-2.1-NVFP4`
- draft: `poolside/Laguna-S-2.1-DFlash-NVFP4`
- DFlash draft tokens: 7
- tensor parallel: 1
- pipeline parallel: 1
- model context: 262,144 tokens
- FP8 KV cache
- prefix caching enabled
- maximum sequences: 32
- maximum batched prefill tokens: 32,768
- Poolside reasoning and tool parsers
- automatic tool choice enabled

## Long-context and output-limit check

Each backend independently accepted a 40,057-token prompt with
`max_tokens=32768`, retrieved the marker `ORCHID-7391`, and stopped normally
after nine completion tokens.

- Spark 1 cold-prefix request: 16.72 seconds
- Spark 2 cold-prefix request: 15.10 seconds
- Spark 2 repeated prefix-cache-hit request: 0.78 seconds

Raw responses are in `long-context-spark1.json` and
`long-context-spark2.json`.

## Router

The trusted-LAN OpenAI-compatible endpoint was
`http://spark1.lan:8080/v1`. A streaming front end on port 8080 forwards
inference to vLLM Router on loopback port 8081 and independently fails over
`/v1/models`.

Final live verification:

- both workers healthy, circuit breakers closed
- two requests with one stable session ID: 2/2 on the same worker
- eight distinct session IDs: 5/3 distribution across Spark 1/Spark 2
- during each rolling backend restart, tagged chat traffic returned HTTP 200
  through the other Spark
- model discovery remained available during a one-node reload

## Dashboard

All three URLs were checked:

- `http://spark1.lan`
- `https://spark1.lan` (self-signed private-LAN certificate)
- `http://spark1.lan:8090`

The final dashboard status was `routing / serving / serving` for router,
Spark 1, and Spark 2.

## Boot persistence

Enabled and active on Spark 1:

- `dgx-spark-laguna-vllm-agent.service`
- `dgx-laguna-router.service`
- `dgx-laguna-router-front.service`
- `dgx-spark-laguna-dashboard.service`
- `nginx.service`

Enabled and active on Spark 2:

- `dgx-spark-laguna-vllm-agent.service`

Systemd unit verification and the dashboard unit tests passed.

## Cold-reboot validation

Both Sparks were subsequently cold-rebooted, one at a time, after applying the
staged firmware.  Their vLLM backends and Spark 1's router, dashboard, nginx
front end, and TLS listener all returned without manual intervention.

Spark 1 was observed externally from Spark 2 every ten seconds:

- LAN, HTTP/HTTPS dashboard, router readiness, and `/v1/models` were
  unavailable from 06:54:04 through 06:54:34 UTC and were serving again by
  06:54:44 UTC (a 40-second sampled outage).
- The first ConnectX-7 direct link response returned by 06:54:34 UTC.
- The Spark 1 model backend became healthy by 07:03:44 UTC.  Its target weight
  load took 449.64 seconds, followed by drafter loading, KV-cache allocation,
  FlashInfer warm-up, and CUDA-graph capture.
- The backend service reported zero restarts during cold loading.
- The raw external observations are in
  `spark1-reboot-monitor.log`.

After both backends were healthy, the router verification sent two requests
with one session ID and eight requests with distinct session IDs.  All ten
returned HTTP 200 with the exact requested marker.  The repeated session stayed
on one worker, while the distinct sessions split 3/5 across the two workers.

Firmware state after reboot:

- Spark 1: EC `0x03000508`, SoC/GPU `0x02009b0b`, and USB-C PD `0x00000516`
  applied successfully; no capsule update remained pending.
- Spark 2: EC and SoC/GPU updates applied successfully.  USB-C PD `0x00000516`
  was still offered after reboot and remains a follow-up item.

Both machines booted kernel `6.17.0-1029-nvidia` in approximately 27 seconds.
All four ConnectX-7 functions on each machine linked at 200 Gb/s with MTU 9000.
The kernel still records a 27 W PCIe-slot power warning for each function, so
that warning should be rechecked after Spark 2's remaining USB-C PD update.
