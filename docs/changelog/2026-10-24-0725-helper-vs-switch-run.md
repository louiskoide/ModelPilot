## October 23, 2026 (overnight run): helper mode against `/model`, the paired run

The user's run of `runs/helper-vs-switch-20261023.sh a` and `b`, side by side on two subscription tokens, Oct 23 21:53 to 23:35, branch `claude/helper-vs-switch` at `eb37a27`, pinned client 2.1.284, seed 23. $0 API key; $26.69 subscription as sent ($14.80 `a`, $11.89 `b`). Report: `report` ($0), `runs/helper-vs-switch-report-20261023.json`. Run directories: `bench-20261023-215335` (`a`), `bench-20261023-215315` (`b`). Dollars below are cold-equivalent, API-key equivalent (1h writes priced at the 5m rate). Tuning split, at most 3 trials per sequence: no savings or quality claim.

- **Not all trials counted:** 58 of 72 planned. `b` finished its 36. `a` stopped `rate_limited` after 33: `cachetools-a` stay/0, `networkx-a` stay/1 and switch/2 never ran, and `cachetools-a` stay/1 was cut off. Ten finished trials are ineligible, each with one lost request (`transport_check`): 6 `transport_error`, 4 `connection_retried` (helper 4, switch 4, stay 2). Not re-run (no retries in measurement runs).
- **Last task (where the arms differ):**

  | Arm | Passes | Mean $ |
  | --- | --- | --- |
  | `low-concise-stay` | 15/19 | $0.074 |
  | `low-concise-switch-opus` | 18/19 | $0.272 |
  | `low-concise-helper-opus` | 19/20 | $0.267 |

  Paired, helper minus switch: −$0.025 [−0.063, +0.018], passes +0.06 [0, 0.19]. Helper minus stay: +$0.195 [0.126, 0.279]; switch minus stay: +$0.219 [0.176, 0.269]. All flagged too few tasks (8). Over whole sequences, per subtask (28 subtasks): helper minus switch −$0.006 [−0.020, +0.006], no difference shown. The earlier subtasks run the same setting in all three arms.
- **Helper mode wasn't cheaper than `/model` here.** The switch arm's rewrite writes were about $0.105 a sequence ($0.944 over 9 trials in `a`, $1.055 over 10 in `b`), the helper arm's switch-classed writes about $0.011. The helper arm's growth writes were higher ($0.141 against $0.091 a sequence in `a`, $0.085 against $0.060 in `b`), and with Sonnet's 3 requests around each handoff that cancelled the saving. Likely the helper re-reading code the conversation already held; its requests weren't inspected. These conversations are about 25–33k tokens; the forecast's longer sessions remain untested.
- **Sonnet low follows the note:** every eligible helper trial's last turn used Opus 5.5 (20 of 20, after 3 Sonnet requests in each).
- **Per sequence, last task (helper / switch / stay):** `parse-decimal-grouping` 3/3, 2/2, 0/2, most of Opus's gain; `nx-ismags-monomorphism` 1/2, 0/1, 1/2, unreliable on every arm; `nx-classes-weak-views` 3/3, 2/2, 2/3; the other five passed on every arm.
- **Wall time:** whole trials in `a` averaged 194 s on the helper arm against 146 s for switch; in `b`, 121 s against 118 s.
