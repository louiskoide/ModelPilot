# ModelPilot

Measure cache behavior before building a model-routing governor for Claude Code.

## Status

M0 is implemented as a direct Anthropic API experiment harness. The quick and five-minute TTL suites completed 213 requests with no errors; effort, model, prewarming and five-minute cache behavior are measured for the tested configuration. See [results](docs/m0-results.md). M1 dry-run proxy is implemented and the eight-hour synthetic soak passed with 11,496 verified requests and zero failures. Real Claude Code text and Read-tool checks passed with matching cost totals. Cancellation, broader client compatibility and one-hour TTL remain unverified. Routing is disabled. See [M1 results](docs/m1-results.md) and [run instructions](docs/m1.md).

## Run locally

Python 3.10 or newer; no third-party runtime dependencies required.

```sh
python3 -m unittest discover -s tests -v
python3 -m modelpilot.cache_probe --config configs/m0.json --out runs/plan
```

The default is offline: it creates a plan without contacting the API. Output directories should be new for each run. Optional installation with `pip install -e .` exposes `modelpilot-cache-probe`.

On macOS, if Python lacks certificate roots, install the optional certificate bundle with `python3 -m pip install certifi`. The probe supplements system trust with this bundle when available and always verifies TLS.

For measured runs, set `ANTHROPIC_API_KEY` in your environment, verify the models and rates in the configuration, then add `--live`. Do not commit credentials. See [the M0 protocol](docs/m0.md) for commands, cost accounting, interpretation and limitations.

## Build order

| Milestone | Deliverable | Status |
| --- | --- | --- |
| M0 | Effort/model cache tests, prewarming costs, TTL/refresh | Direct-API quick + 5m TTL measured |
| M1 | Proxy, logging and cost model; dry-run decisions only | Synthetic + basic real-client checks passed; cancellation pending |
| M2 | Stuck detector, reference outputs, shared task ledger | Local tests + basic live MCP checks passed |
| M3 | Fork execution with persistent role-based workers | Bounded workers + six-task live check passed |
| M4 | Cascade and economically justified shadow caches | Dry-run planner + bounded live draft checks passed; execution pending |
| M5 | Rebase coalescing, budgets, offline threshold learning | Offline control prototype tested; integration and real calibration pending |
| M6 | Fixed model/effort and jev-router comparisons: cost, pass rate, wall time | Fixed-baseline live smoke passed; governor and jev-router comparisons pending |

Persistent workers need versioned task corrections and stale-result rejection. Rebase triggers include explicit user/goal-boundary compaction. The selected jev-router baseline is gargpratyush/jev-router, pinned in `configs/jev-baseline.json`; see [setup requirements](docs/jev-baseline-setup.md).

M2 commands, behavior and limits: [M2 guide](docs/m2.md). Offline tests cover the proxy, ledger, workers and M4 decisions.

M2 live validation: [results](docs/m2-results.md). Automatic tool-output interception and mid-flight correction delivery remain unvalidated.

M3 behavior and live check: [worker guide](docs/m3.md).

M3 live evidence: [worker results](docs/m3-results.md).

M4 dry-run decisions and two-request live check: [M4 guide](docs/m4.md).

M5 controls, evidence and integration limits: [M5 guide](docs/m5.md).

M6 paired baseline harness and remaining evaluation work: [M6 guide](docs/m6.md).

M6 measured smoke comparison: [results](docs/m6-results.md).

Claude Code handoff: [CLAUDE.md](CLAUDE.md).
