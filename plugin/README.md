# ModelPilot lean mode (Claude Code plugin)

This plugin runs your Claude Code sessions in the setting that came out cheapest at equal quality on [ModelPilot's benchmark](../docs/4b-writeup.md): Sonnet 5.5 at low effort, with a short instruction to work economically. Version 0.1.0.

## Install

In Claude Code (2.1.281 or later):

```
/plugin install modelpilot --marketplace louiskoide/ModelPilot
```

Or from your shell: `claude plugin install modelpilot --marketplace louiskoide/ModelPilot`.

Then, **once**: start a new session, run `/effort low` and press Enter. That saves low effort for Sonnet 5.5 only. The plugin can't set effort itself, because Claude Code doesn't apply a plugin agent's effort to the main session. Without this step you get Sonnet 5.5 at medium effort.

The startup header shows `@modelpilot:lean` when the plugin is active.

## What it changes

- **Model.** The plugin's default agent, `modelpilot:lean`, runs the main session on Sonnet 5.5 (`claude-sonnet-5-5`). The agent has no prompt of its own, so Claude Code's system prompt, tools, CLAUDE.md files and permissions stay as they were.
- **Instruction.** A SessionStart hook adds [this text](../bench/prompts/concise.md) once at the start of each session, about 290 tokens. It asks for the smallest change that solves the task, only the tests that change needs, small tool output, no repeated reads or runs, and short replies. The hook skips it when the session runs a different agent or model.
- **Effort.** Only through your saved `/effort low`; see Install.

Nothing else changes. No requests go anywhere new, and the plugin runs nothing in the background.

## The evidence, and its limits

On 24 held-out repository tasks, one trial each (October 19, 2026), strict passes and cost per pass at API rates:

| Setting | Strict passes | $ a pass |
| --- | --- | --- |
| Sonnet 5.5, low effort, this instruction | 23/24 | $0.056 |
| Sonnet 5.5, medium effort (Claude Code's default for Sonnet) | 22/24 | $0.067 |
| Opus 5.5, medium effort | 24/24 | $0.167 |

On 23 earlier tuning tasks the same setting cost $0.0689 a task, $0.0180 less than Sonnet 5.5 at medium. Full results, failures included: [docs/evidence.md](../docs/evidence.md).

Read these numbers with their limits:

- **Small tasks, one trial each.** This is a toy benchmark. It is not proof of savings or quality in real use.
- **Opus users give up quality.** Opus 5.5 passed every task, and Sonnet 5.5 missed some that Opus passed. Every miss on the benchmark reported success, and no rule found the misses in advance. If you normally use Opus, installing this plugin is a choice to trade some quality for cost.
- **Not quite the measured setting.** The benchmark appended the instruction to the system prompt. A plugin can't, so the instruction arrives as context at the start of the session instead.
- **On a subscription** the saving is usage-limit headroom, not dollars.

## Turning it off or around it

- **For one session:** `claude --model opus` overrides the model. In `claude -p` runs the hook can't see the model, so the instruction is still added there.
- **Your own default agent:** an `agent` in your own settings takes precedence over the plugin's.
- **Off:** `claude plugin disable modelpilot@modelpilot`, or use `/plugin`. To clear the saved effort, run `/effort auto` in a Sonnet 5.5 session.

## Known gaps

- Claude sees `modelpilot:lean` in its list of subagents. Its description tells Claude not to delegate to it, but nothing prevents it.
- ModelPilot's cache keeper and proxy aren't part of the plugin. A plugin can't route Claude Code's requests through a proxy.

## Checks

`python3 -m modelpilot.plugin_check` runs a real Claude Code client with this plugin against a local fake API, at no cost. It records what reaches the model: the model, the effort, whether the system prompt changed, and how many times the instruction arrived. It also runs `claude plugin validate --strict`. Results for 2.1.284 and 2.1.303 are in [docs/evidence.md](../docs/evidence.md).
