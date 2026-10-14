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

The dry-run implementation passed its overnight synthetic checks. M1 is not yet certified against actual Claude Code traffic. Next validate API-key-based Claude Code streaming, tool calls, cancellation, headers and usage through the proxy. Subscription OAuth, cloud adapters, chunked client uploads, server-side fallback accounting, tool surcharges and long-running streams beyond the 120-second inactivity timeout remain limitations. Cost scenarios assume a warm source, cold target, unchanged context/output and five-minute TTL; quality and actual remaining task length are unknown. Keep routing disabled.

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

Bugs found, each an `expectedFailure` test with the fix it needs:

1. **Server-side web search isn't priced and isn't unknown.** `cache_probe.priced_usage` reads tokens only, so a reply whose usage has `server_tool_use.web_search_requests` (Claude Code's WebSearch runs it; the pricing page lists $10 per 1,000 searches) is logged as a known cost without the search fee, and `logged_usage` drops the counter. Fix: keep numeric `server_tool_use` counters in `logged_usage`; return None from `priced_usage` when a fee-bearing counter is positive unless the model's rates carry that fee.
2. **An upstream that closes without replying gets no 502.** `http.client.RemoteDisconnected` subclasses `ConnectionResetError`, so `do_POST`'s client-disconnect branch takes it: the client gets a closed connection instead of the 502 the next branch would send, and the row reads `connection_closed` instead of `transport_error`. Accounting is unaffected (both count as lost requests). An upstream reset mid-stream is labelled the same way. Fix: treat `RemoteDisconnected` and errors from `conn.request`/`getresponse`/`read1` as upstream failures before the client branch, or confine that branch to `self.wfile` writes.
3. **A header sent twice is forwarded once.** `clean_headers` builds a dict from `HTTPMessage.items()`, so the last value wins: two `anthropic-beta` headers reach the API as one, two `Set-Cookie` headers reach the client as one. Low impact for Claude Code (fetch merges repeats into one header). Fix: keep (name, value) pairs and send them one by one.
