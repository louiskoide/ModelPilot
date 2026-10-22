# ModelPilot lean mode (Claude Code plugin)

This plugin runs your Claude Code sessions in the setting that came out cheapest at equal quality on [ModelPilot's benchmark](../docs/4b-writeup.md): Sonnet 5.5 at low effort, with a short instruction to work economically. Version 0.3.0.

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

Nothing else changes until you turn on helper mode. No requests go anywhere new, and the plugin runs nothing in the background.

## Helper mode: another model without the cache rewrite (opt-in, October 20)

Switching the session's model with `/model` makes the next request write the whole conversation again into the new model's cache. Helper mode leaves the session where it is and hands each request to a helper on the model and effort you pick:

```
/modelpilot:helper opus high     # sonnet, opus or haiku; low, medium (the default), high, xhigh or max
/modelpilot:helper status
/modelpilot:helper off
```

- **The command costs nothing.** A hook handles it and blocks the prompt, so no request is sent. The setting lasts for this session only.
- **While it's on,** each prompt gets a note of about 140 tokens. It asks the session to call the Agent tool once with the agent `modelpilot:helper-<effort>` and the chosen model, give it a self-contained brief, and relay its reply. The session's model, effort and cache stay as they were. On 2.1.284 and 2.1.305 the helper runs on the chosen model and effort (checked offline, see Checks).
- **The helper starts with none of the conversation.** At the chosen model's rates, it writes its own system prompt and tools (about 7,500 tokens), the brief, and the files it re-reads. The session still writes the brief and reads the reply. For a long conversation and a self-contained task, that's much less than the rewrite. For work that leans on the conversation, it may not be. `python3 -m modelpilot.switch_quote --from claude-sonnet-5-5/low --to claude-opus-5-5/high --prefix-tokens 120000` estimates both. Its sizes are guesses, apart from the helper's base.
- **Every helper is logged (v0.3.0).** Hooks write one line to `helper-log.jsonl` in the plugin's data directory (`~/.claude/plugins/data/modelpilot-modelpilot/`) for each prompt that got the note, each helper launch and each helper finish. A line holds ids, the model and effort and the transcript's path, never the brief or the reply. Comparing notes with launches shows whether the session followed the note.
- **What it saved, from the log:** `python3 -m modelpilot.helper_report` ($0, offline, from the repository). It reads the log and Claude Code's own transcripts. For each helper it measures, at API rates: the helper's requests (nested helpers included), its startup, the session's side (writing the brief and relaying the reply), and the conversation's size at the launch. It also estimates the rewrite `/model` would have written at that size, and `est_saving_usd` = rewrite − startup − session side. That estimate doesn't count files the helper re-read that the session already had, which makes it too high. It also doesn't count the new model reading the whole conversation, or the rewrite on switching back, which makes it too low. It doesn't measure quality. Claude Code deletes transcripts after 30 days by default (`cleanupPeriodDays`), so report before then. On a subscription the dollars are what the same tokens would cost at API rates, not a bill.
- **Not measured on real work.** No live session has used it. The benchmark arms never delegated, so there's no evidence yet that it is cheaper at equal quality. The session's model decides whether to follow the note, and it relays the helper's account without checking it.

**Effort alone.** For an effort change on the same model, use `/effort` instead. On 2.1.284 and 2.1.305, resuming a session at a new effort left the earlier messages unchanged. The client sent the new effort as a message at the new turn (checked offline). In ModelPilot's October 1 live probe, that shape kept the cache and changed thinking from the next turn. The interactive `/effort` path wasn't checked separately.

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

- Claude sees `modelpilot:lean` and the five `modelpilot:helper-*` agents in its list of subagents, in every request. Their descriptions tell Claude when to use them, but nothing enforces it.
- A helper has the Agent tool, so it can start helpers of its own, each with its own startup. In the offline check the fixture's scripted reply made it do so. The log and the report count them.
- ModelPilot's cache keeper and proxy aren't part of the plugin. A plugin can't route Claude Code's requests through a proxy.

## Checks

`python3 -m modelpilot.plugin_check` runs a real Claude Code client with this plugin against a local fake API, at no cost. It records what reaches the model: the model, the effort, whether the system prompt changed, and how many times the instruction and the helper-mode note arrived. Two more cases cover helper mode (the helper's model and effort when the session makes the call the note asks for, and the log's lines) and a session resumed at a new effort. It also runs `claude plugin validate --strict`. Results for 2.1.284 and 2.1.303 (v0.1.0), for 2.1.284 and 2.1.305 (v0.2.0, helper mode included), and for 2.1.284 and 2.1.305 (v0.3.0, the log included), are in [docs/evidence.md](../docs/evidence.md).
