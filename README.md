# ModelPilot

ModelPilot is a cache-aware model and effort governor for Claude Code, and a benchmark that compares it with fixed settings and with the [Jev router](https://github.com/gargpratyush/jev-router). It is a tested set of components and bounded harnesses, not an operational end-to-end governor. Routing is off by default and the governor runs in dry-run. Only the ModelPilot benchmark arm acts on its decisions.

## What is measured

- **Cache behaviour.** In the tested conditions, switching model or effort wrote a new cache prefix. Returning to an earlier model and effort read its still-warm entry. So ModelPilot tracks cache state by exact model, effort and prefix and by recent use, and does not assume a switch destroys earlier entries. Prewarming is billable even with zero output tokens, and keeping entries warm raised cost for the tested sequences, so shadow warming is off by default.
- **Pass-through proxy.** An eight-hour synthetic soak passed (11,496 requests, no verification failures), as did basic checks with the real client, where the proxy's and the client's totals matched.
- **Benchmark, 29 tuning tasks** (small repository tasks; trials repeated per task):
  - Sonnet 5.5 at low effort with a concise prompt was the cheapest setting at equal quality: $0.0689 a task, $0.0180 below Sonnet 5.5 at medium effort on 23 tuning tasks.
  - Under strict grading Sonnet 5.5 passes three tasks less often than Opus 5.5. Every Sonnet and Opus failure reported success, and a redo costs about $0.25 a task.
  - The ModelPilot arm never left its starting setting (low effort, concise). It cost a third of what Opus 5.5 did, with a strict pass rate of 0.915 against 0.981.
  - A forced Opus review at the finish rescued none of 27 tasks, at $0.069 more a task.
- **Benchmark, final split** (24 held-out tasks, one trial each, October 19; [write-up](docs/4b-writeup.md)):
  - ModelPilot never changed model or effort, so the run compared fixed settings. Strict passes and cost per pass: Sonnet 5.5 low concise 23/24, $0.056; Sonnet 5.5 medium 22/24, $0.067; Opus 5.5 24/24, $0.167.
  - The arm for Sonnet users passed its pre-registered test against Sonnet 5.5 medium. The saving belongs to the low-effort concise setting, not to switching. The arm for Opus users failed its test against Opus 5.5: it ran Opus 5.5 throughout.
- **Predicting misses.** On 38 other tasks, no rule applied at a turn's start found the misses.
- **Where the money goes.** Cache writes are about 45% of cost, mostly from context growth, not rebuilds. 80–90% of tool-result bytes are source code read or searched.

Every result, failures included, is in [docs/evidence.md](docs/evidence.md) with its run directory. [docs/changelog/](docs/changelog/) (since October 14, one file per entry) and [docs/changelog.md](docs/changelog.md) (before) have the dated history, costs and test counts.

## What is not measured

- Whether a one-hour cache entry lasts the hour.
- Request shapes beyond those tested, and broad client compatibility through the proxy.
- The task ledger, workers, verifier and budget controls (M2–M5) as one system: each is a bounded harness with small live checks, and they are not integrated.
- The governor's active mode. In dry-run it is wired into the proxy and Claude Code's hooks. Its live checks so far: a correction delivered through a hook, turn-end delivery, an observed rebuild and a fallback.
- Whether switching model or effort within a task saves anything: on the final split ModelPilot never switched. Sessions longer than a single prompt were not part of the final split.
- Savings or quality in real use. The benchmark is a toy set of tasks, and nothing here is proof of production quality or savings.

## Status

| Part | Verified | Missing |
| --- | --- | --- |
| M0 cache harness | Direct-API cache, effort, model, prewarming and five-minute lifetime measurements | One-hour lifetime; other request shapes |
| M1 proxy | Streaming pass-through, usage and cost records, eight-hour soak, real-client checks | Broad compatibility, including cancellation with the real client; active routing |
| M2–M5 | Ledger, workers, verifier and controls: bounded harnesses, small live checks | Integration |
| M6 benchmark | Fixed, Jev and ModelPilot arms run live on 29 tuning tasks; the final split run once (October 19) | Long sessions; repeated trials on held-out tasks (the final split is spent) |
| Governor | Dry-run, wired into the proxy and hooks; four live checks | Active mode |

The ModelPilot arm starts at Sonnet 5.5, low effort, with a concise prompt. Haiku 5.5 is not one of its session models, it has no advisor, and delegation is off. Its quality floor is no worse than the user's own model (arms `modelpilot-for-sonnet` and `modelpilot-for-opus`; the Opus arm runs standard Opus 5.5 medium, no prompt). Details: [docs/m6-modelpilot-policy.md](docs/m6-modelpilot-policy.md).

## Use it in Claude Code

The benchmark's cheapest setting at equal quality, Sonnet 5.5 at low effort with a concise instruction, installs as a Claude Code plugin:

```
/plugin install modelpilot --marketplace louiskoide/ModelPilot
```

Then run `/effort low` once and press Enter: a plugin can't set effort itself. What it changes, the evidence and its limits are in [plugin/README.md](plugin/README.md). It doesn't include the proxy, the governor or the cache keeper. Version 0.2.0 adds an opt-in helper mode, `/modelpilot:helper opus high`. It hands each request to a helper on that model and effort, so the session's cache isn't rewritten. It hasn't been measured on real work. Version 0.3.0 logs each helper, and `python3 -m modelpilot.helper_report` prices the logged helpers against the rewrite, offline.

## Run it

Python 3.10 or newer, standard library only. On macOS, `pip install certifi` supplies TLS roots if Python lacks them; verification always stays on.

```sh
python3 -m unittest discover -s tests
python3 -m modelpilot                                         # lists the commands; [paid: --live] marks paid ones
python3 -m modelpilot.evaluate                                # writes a plan; sends nothing
python3 -m modelpilot.governor --out runs/governor-demo.json  # offline walk-through
```

Everything is offline by default. Paid harnesses need `--live` and an API key in your environment (some fixed benchmark arms can use a Claude subscription token instead), never in a file you commit; inspect each plan before running it. Their limits are stopping thresholds, not billing caps. Run outputs go to the ignored `runs/` directory, which can hold source data from benchmark tasks.

Tests that drive the real Claude Code client, the benchmark or Jev need the pinned client and checkouts under the ignored `work/` directory, and skip without them. Setup: [docs/jev-baseline-setup.md](docs/jev-baseline-setup.md) and [docs/m6-benchmark-plan.md](docs/m6-benchmark-plan.md).

## Where to look

- [docs/code-map.md](docs/code-map.md): what each module does.
- [docs/4b-writeup.md](docs/4b-writeup.md): the final benchmark, written up.
- [plugin/README.md](plugin/README.md): the Claude Code plugin, its evidence and its limits.
- [docs/evidence.md](docs/evidence.md): measured results and their run directories.
- [docs/changelog/](docs/changelog/) and, before October 14, [docs/changelog.md](docs/changelog.md): what changed, when, at what cost.
- [docs/governor.md](docs/governor.md): the governor and its limits.
- [CLAUDE.md](CLAUDE.md): working context and rules for coding agents on this repository.
