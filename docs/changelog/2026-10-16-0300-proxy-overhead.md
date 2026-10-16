## October 15, 2026 (overnight cloud run): the proxy's overhead on loopback

$0, branch `claude/proxy-overhead` (overnight item 11, stacked on `claude/idle-replay`). New `modelpilot/proxy_overhead.py` and `tests/test_proxy_overhead.py`, a `commands.py` row and a code-map line. Details and the full table: `docs/m1-results.md`, "Proxy overhead".

- **What it measures:** what the proxy adds to each request against an upstream that answers at once, with the same request sent direct in the same round. That covers connect, first byte, end, per event and upload throughput, plus the proxy process's CPU seconds and peak memory. Bodies run from 1 KB to 30 MB, streams of 10 to 10,000 events, at 1, 4 and 16 clients, with and without `--governor-*`. It reports medians and p95, and counts failures without retrying them.
- **Results (cloud container, 4 CPUs, 5 rounds):**
  - At one client the proxy adds about 1 ms to the first byte of a small request or a stream. A large body adds most of its upload time: 138 ms for 30 MB, because the proxy reads and parses the whole body before forwarding.
  - Each streamed event costs about 9 µs at one client.
  - `--governor-*` adds 3–4 ms before the first byte at one client. At sixteen clients its p95 is 110–930 ms.
  - Every reply body matched the direct one.
- **Finding:** the proxy listens with socketserver's default backlog of 5. At sixteen concurrent clients, about a third of connects waited for a 1 s SYN retry (129 and 172 of 480).
  - The first full run stopped on a connection reset in a 16-client case. Its stderr is kept as `runs/cloud-overnight-proxy-overhead-first-reset.err`, and the harness now counts such failures instead of crashing.
  - A fix (`request_queue_size = 64`) would change shipped behaviour, so it is proposed in the overnight report, not made.
- **Smoke test:** a quick run through a real proxy process, both modes, under 5 s.
- **Tests:** all 936 offline tests pass on Python 3.12 with the pinned 2.1.284 client, no skips (`runs/cloud-overnight-proxy-overhead-regression-py312.log`). `tests/test_proxy_overhead.py` also passes on Python 3.10.20.
