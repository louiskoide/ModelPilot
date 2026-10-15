# M1 overnight synthetic validation results

Completed September 22, 2026 at 06:23 AM America/New_York. The run started September 21 at 10:23 PM and lasted 28,800.35 seconds (eight hours plus shutdown). No duplicate run or paid API call was started by monitoring.

## Verified outcome

- 11,496 requests across 2,874 concurrent batches; zero verification failures.
- Exactly 11,496 parseable log records and unique request hashes. Final audit reconstructed every synthetic request and matched its hash, model, response classification, cost and no-op decision.
- 2,874 JSON successes, 2,874 streaming successes, 2,874 deliberate HTTP 429 errors, and 2,874 deliberate streaming errors. Expected upstream errors are test cases, not failed checks.
- Successful SSE output usage remained cumulative at four tokens. All 5,748 successful responses had the expected configured cost estimates. All 5,748 error cases retained unknown costs, not zero-cost success.
- No routing action was applied. Fixture credentials and prompt strings were absent from the log. During execution the harness also checked response bytes/status and upstream request hashes.
- Actual provider charges: **$0**. Successful synthetic observations carry illustrative per-request estimates of $0.007525 for Opus and $0.004515 for Sonnet; these are not API billing or measured savings.

## Stability observations

Peak RSS reported on macOS stayed at 33,832,960 bytes (about 32.3 MiB) across hourly snapshots. Active threads stayed around seven/eight during execution and returned to one after shutdown. This is bounded evidence from this workload, not a general guarantee against memory leaks.

Median proxy request wall time was about 6.18 ms; maximum was 27.55 ms. The harness's maximum end-to-end request measurement was 52.39 ms. These timings are for a loopback fake upstream, not Anthropic latency or proxy overhead relative to a direct-client baseline.

## Evidence

Run directory: `runs/m1-overnight-20260921-222324/`, containing `status.json` and `observations.jsonl`; `runs/m1-active.json` identifies this run. Original logs remain unchanged and ignored by Git.

Observation-log SHA-256: `907a4f148e1c5560327688092746c7bd069a02280f571be63417d40143c3dfc7`.

## Remaining M1 work

The dry-run implementation passed its overnight synthetic checks. M1 is not yet certified against actual Claude Code traffic. Next validate API-key-based Claude Code streaming, tool calls, cancellation, headers and usage through the proxy. Subscription OAuth, cloud adapters, chunked client uploads, server-side fallback accounting (since October 14 a reply served by another model is tested to stay unknown), tool surcharges (since October 14 a reply whose usage counts a web search is left unknown, not priced on its tokens) and long-running streams beyond the 120-second inactivity timeout remain limitations. Cost scenarios assume a warm source, cold target, unchanged context/output and five-minute TTL; quality and actual remaining task length are unknown. Keep routing disabled.

The soak stopped automatically. The overnight follow-up is disabled after this completion report; no further overnight runs are scheduled by this task.

## Real Claude Code follow-up — September 22, 09:12 AM

Run: `runs/m1-claude-20260922-091219/`. Claude Code 2.1.278, API-key authentication, Sonnet 4.6, low effort, identity response encoding requested.

Both the text-only task and the Read-tool task passed. Three forwarded API requests returned HTTP 200 with complete usage, and no routing changes were applied. Text task cost: $0.02522025; Read-tool task cost: $0.03456450. The proxy total of $0.05978475 matched the client's combined total, with zero unpriced requests. These are API list-price estimates, not invoice reconciliation.

This validates basic real-client streaming, a Read-tool round trip, cache-write/read accounting and dry-run forwarding for this configuration. The earlier strict text-check failure and missing usage issue are resolved in this run. Cancellation and broader client/provider compatibility remain untested. M1 basic integration passes; the remaining limitations above still apply. M2 can proceed as offline/dry-run feature development without enabling model routing.

## Request shapes against local upstreams — October 14, 2026 (tests only, $0)

Overnight queue item 5, branch `codex/proxy-shapes`: `tests/test_proxy_shapes.py`, 17 tests. Each scripts a local upstream (framing, pauses, truncation, dropped connections) and checks byte-for-byte passthrough and that the logged cost is either right or explicitly unknown (`cost_usd` null, so `bench.accounting` leaves the trial's total unknown). `proxy.py` was not changed: another session owns it.

Passes:

- A non-streamed reply with signed thinking, redacted thinking and a tool call, and a request resending signed thinking, both byte for byte, priced from usage.
- A streamed reply with `thinking_delta`, `signature_delta` and an `input_json_delta` tool call split across events, cut at arbitrary byte boundaries, with an SSE comment between events, with LF and with CRLF separators: forwarded unchanged, priced from the final cumulative usage.
- Image (3 MB base64 and URL) and document blocks (PDF with citations and a 1h breakpoint, text): the request reaches the upstream unchanged, `cache_ttl` is `['1h']` and the 1h write is priced at the 1h rate. Neither the image data nor document text reaches the log. A body over 32 MiB, declared by `Content-Length` or as one chunk, is refused before it is read.
- `count_tokens` with images, documents, tools and thinking: forwarded unchanged and logged at $0, also when refused with 429.
- 429 and 529, streamed or not: status, reason, `retry-after`, `x-should-retry` and rate-limit headers pass through, the proxy sends once and never retries, and the row is an unpriced rejection.
- An `error` event mid-stream (HTTP 200): forwarded, cost unknown (`error_event`), counted as billed work, not a rejection.
- A stream the upstream cuts short, chunked without its last chunk (`IncompleteRead`) or close-delimited (`stream_ended_early`): the client gets what arrived, cost unknown.
- **Cancellation:** a client that disconnects after the first event, or before the first byte, makes the proxy close the upstream before the stream ends (the upstream's writes fail); the row is `connection_closed`, cost unknown. Real-client cancellation stays untested.
- A reply the upstream gzips despite the proxy's `identity` request: forwarded as sent, cost unknown (`compressed_response`). A gzipped request body: refused with 400 and never forwarded (fails closed).
- A keep-alive client: one reply per connection (`Connection: close`), each request forwarded and logged once.
- An event or reply over 1 MiB: forwarded, cost unknown (`oversized_response`). The bound is checked between reads, so an event a little over it can still be parsed and priced.

Bugs found, each an `expectedFailure` test with the fix it needs (all three fixed the same day; see the next section):

1. **Server-side web search isn't priced and isn't unknown.** `cache_probe.priced_usage` reads tokens only, so a reply whose usage has `server_tool_use.web_search_requests` (Claude Code's WebSearch runs it; the pricing page lists $10 per 1,000 searches) is logged as a known cost without the search fee, and `logged_usage` drops the counter. Fix: keep numeric `server_tool_use` counters in `logged_usage`; return None from `priced_usage` when a fee-bearing counter is positive unless the model's rates carry that fee.
2. **An upstream that closes without replying gets no 502.** `http.client.RemoteDisconnected` subclasses `ConnectionResetError`, so `do_POST`'s client-disconnect branch takes it: the client gets a closed connection instead of the 502 the next branch would send, and the row reads `connection_closed` instead of `transport_error`. Accounting is unaffected (both count as lost requests). An upstream reset mid-stream is labelled the same way. Fix: treat `RemoteDisconnected` and errors from `conn.request`/`getresponse`/`read1` as upstream failures before the client branch, or confine that branch to `self.wfile` writes.
3. **A header sent twice is forwarded once.** `clean_headers` builds a dict from `HTTPMessage.items()`, so the last value wins: two `anthropic-beta` headers reach the API as one, two `Set-Cookie` headers reach the client as one. Low impact for Claude Code (fetch merges repeats into one header). Fix: keep (name, value) pairs and send them one by one.

## The three shape bugs fixed — October 14, 2026 (cloud batch, tests only, $0)

Cloud batch item 2, branch `claude/proxy-shape-fixes`. The three `expectedFailure` decorators are gone and each test passes for the reason it names, strengthened where it allowed a weaker fix. `tests/test_proxy.py` and the soak's fixtures are unchanged and pass.

1. **Server-side tool fees.**
   - `logged_usage` keeps `usage.server_tool_use`'s numeric counters (names `[a-z_]`, at most 64 characters).
   - `cache_probe.priced_usage` returns None when one counts a request with a fee: web search, $10 per 1,000 searches. It also returns None for any counter it doesn't know, and for anything in `server_tool_use` that isn't a counter.
   - Web fetch isn't metered, so `web_fetch_requests` keeps the token price. Source: the claude-api skill's pricing notes (bundled with client 2.1.300, read October 14, 2026: "web searches at $10 per 1,000"; `web_fetch_requests` "currently always 0 since web fetch is not metered").
   - No rates table carries the search fee, so such a reply's cost is unknown (`usage_problem: unpriced_usage`) and so is its trial's total.
   - Checked on the main machine on October 14: no recorded run's usage has `web_search_requests` above 0, so no recorded cost changes.
   - Not covered: code execution's container hours (billed past a free allowance) don't appear in usage at all. Claude Code doesn't declare that tool.
2. **Upstream failures are the upstream's.**
   - Only writes to the client raise `ClientGone`, which logs a row `connection_closed`. Every other exception is the upstream's: `transport_error` with its type, and a 502 if the client has had nothing yet.
   - An upstream that closes before its status line now gets the client a 502 and logs `RemoteDisconnected`. Before the fix the client got no reply and the row read `connection_closed`.
   - An upstream reset mid-stream (new test, an RST after the first event) logs `ConnectionResetError` as a `transport_error`; the client keeps what had arrived.
   - The same split now holds for `GET /v1/models` (new test).
   - Accounting is unchanged: either status was already a lost request of unknown cost.
3. **Repeated headers.** `clean_headers` returns (name, value) pairs in order, and requests go out through `putrequest`/`putheader` (`proxy.send_request`). Two `anthropic-beta` headers now reach the upstream as two headers, and two `Set-Cookie` headers reach the client as two. Host is still sent once, the upstream's. Where the active arm adds a beta, the client's repeats are merged into one header with it, matched in any case.

Tests: 20 in `tests/test_proxy_shapes.py` (17 before; 3 new: the reset, the catalog close, the unmetered counters). Each new or fixed test fails on the old code.

## Real-client cancellation, chunked uploads and idle streams — October 14, 2026 (cloud batch, tests only, $0)

Cloud batch item 3, branch `claude/proxy-client-gaps` (stacked on the shape fixes): `tests/test_proxy_client_gaps.py`, 8 tests. The real-client tests run the pinned 2.1.284 in `-p` mode through the proxy to a loopback upstream, with a fake key, an empty HOME and config, no tools, `CLAUDE_CODE_MAX_RETRIES=0`. They skip without node or that client first on PATH. No proxy bug was found, so `proxy.py` is unchanged here.

- **Real-client cancellation** (SIGINT, SIGTERM, SIGKILL, each sent about 0.3 s into a stream of pings 50 ms apart): for every signal the proxy closed the upstream before its stream ended. The row is `connection_closed` with HTTP 200 and cost unknown, after well under the stream's 10 s. There is one row per request that reached the upstream, `bench.accounting` counts it once as unpriced (total unknown), and `transport_check` counts it lost. None of the three signals made the client send a second request. What the client did:
  - **SIGINT:** exit 0 with a `result` event of subtype `error_during_execution`. A harness must not read a 0 exit as success; `bench` reads the result's subtype.
  - **SIGTERM:** exit 143 with no `result` event.
  - **SIGKILL:** exit −9 with no `result` event.

  These are pinned in the tests, so a client change shows.
- **Chunked request bodies** (raw sockets: 7-byte, 4 KiB and single chunks, chunk extensions, a trailer). The upstream gets the exact bytes with a `Content-Length`, and the row is priced. Refused with 400 before anything reaches the upstream, and nothing logged:
  - 33 chunks of 1 MiB: the proxy stops at the chunk that would pass 32 MiB, without reading the rest;
  - chunked framing with a `Content-Length` too;
  - `gzip, chunked`, or the `Transfer-Encoding` header sent twice;
  - a size that isn't hex, a short chunk, or no last chunk.
- **A stream idle past the inactivity timeout.** The upstream sends `message_start`, then nothing for 3 s; the proxy's upstream timeout (`ProxyServer.connection`'s 120 s) is lowered to 1 s in the test only.
  - **The proxy** gives up after about a second. The client already has the HTTP 200 and `message_start`, then the reply ends with no `message_stop`. The row is `transport_error`/`TimeoutError` with cost unknown.
  - **The pinned client** sent the request again *without streaming*, even with `CLAUDE_CODE_MAX_RETRIES=0`, and the session succeeded (exit 0, `success`). This reproduces offline the hidden retry `bench_report.transport_check` found in `bench-20261011-234003`.
  - **In the log** the retry is its own row, priced, and the cut request stays unknown. `transport_check` reports 1 lost and 1 sent after, so `bench_report` would exclude such a trial (`connection_retried`).

Still untested: a real client cancelled during a tool call or between requests, subscription OAuth, and a live provider's own idle behaviour.

## A second review of the cloud batch's proxy changes — October 15, 2026 (overnight cloud run, tests only, $0)

Overnight item 1, branch `claude/stack-review`: #78's `ClientGone`/`transport_error` split, repeated headers, the 502 rule and `server_tool_use` pricing, read again after the merge.

- **Bug, fixed:** a write to a client that stopped reading fails with `TimeoutError` (the handler's 120 s socket timeout), not a broken pipe or a reset, so `to_client` let it through and the row read `transport_error`/`TimeoutError`: the upstream's failure. Any failed write to the client is now the client's (`ClientGone`, any `OSError`; the 502 and the active arm's refusal swallow the same). Accounting was the same either way: a lost request of unknown cost, counted once. New test `test_a_client_that_stops_reading_is_the_clients_failure` (handler timeout 0.5 s, 4 KiB socket buffers, a client that never reads), which fails on the old code with `transport_error`.
- **Checked, no bug:** only client writes are wrapped, so an upstream reset or close (`RemoteDisconnected`) is never the client's; a 502 is sent only before the upstream's status line reached the client (`sent_headers` is set before the buffered headers are flushed, and nothing between them can fail); `Host`, `Content-Length` and the names `Connection` lists stay dropped, repeats go out as pairs both ways; repricing (`bench_report.repriced`, `api_key_equivalent`) skips a row whose cost is unknown, so a web-search reply can't be repriced into a known cost.
- **Left for the fuzzing item:** `cache_probe.cost` doesn't check its counters, so a negative or fractional token count in a reply would be priced (overnight item 7).

## The client's connectivity check — October 15, 2026 (overnight cloud run, tests only, $0)

Found by the offline end-to-end check (`tests/test_e2e_dry_run.py`, `docs/governor.md`, "Offline end-to-end check"): at the start of every invocation the pinned 2.1.284 client sends `HEAD /api/hello` to its base URL, with `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` set. The proxy has no `HEAD` handler, so `BaseHTTPRequestHandler` answers 501 without forwarding or logging it, and the client carries on as normal. Not billable; no accounting changes. A proxy log doesn't show these requests, and whether the real API's answer changes anything in the client is untested.
