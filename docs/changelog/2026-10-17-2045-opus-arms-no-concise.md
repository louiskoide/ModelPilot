## October 17, 2026: The concise prompt is off both Opus ModelPilot arms

User request, after the decision to continue with standard Opus. $0, no run. Branch `claude/opus-levers`.

- **What changed** (`modelpilot/bench.py`):
  - `modelpilot-for-opus` no longer appends `bench/prompts/concise.md`. It now starts at Opus 5.5 medium with no appended prompt. That is `opus-5.5`'s exact setting, and the one the floor's Opus outcomes were measured on.
  - `modelpilot-for-opus-risk` no longer has a prompt of its own. Each trial already ran its route's own arm, so the flagged route (`modelpilot-for-opus`) now sends none. The unflagged route (`modelpilot-for-sonnet`) keeps the concise prompt.
  - The run manifest's `appended_prompts` now also lists a risk arm's routes. Before, the risk arm's own entry held the shared prompt's text; without it, a run of that arm alone would have lost the unflagged route's prompt text from the manifest. Trial records were already right, because each keeps its routed arm's path and sha256.
- **Why it is safe to change:** neither arm has run live, so no recorded evidence was made under the old definition.
- **What it means for 4b:** `modelpilot-for-opus` against `opus-5.5` no longer measures the concise prompt on Opus. On today's evidence it measures only the proxy's overhead. The October 11 stand-in for that pair (the concise prompt's effect on Sonnet, scaled to Opus) no longer applies.
- **Docs:** `docs/m6-modelpilot-policy.md` (arm table, "What it means now"), `docs/m6-benchmark-plan.md` (arm table, "Opus levers: lean-io read"), `configs/modelpilot-policy.json` (calibration note), `README.md`, `CLAUDE.md`.
- **Tests:** `tests/test_bench.py` checks that both Opus arms carry no prompt and that `modelpilot-for-sonnet` carries the concise one. `tests/test_risk_route.py` checks each route's prompt and the manifest's `appended_prompts`.
- **Test run:** all 1021 offline tests pass on Python 3.12 with the pinned 2.1.284 client, no skips (`runs/opus-no-concise-regression-py312.log`). A first run on the machine's default `python3` (3.9.6, below the project's 3.10 minimum) failed 16 tests in unrelated modules (exposure, thinking_probe, transport, miss_detector, proxy_client_gaps). That log was deleted, and the run was repeated on 3.12.
