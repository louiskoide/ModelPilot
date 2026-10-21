#!/usr/bin/env bash
# PostToolUse (Agent) and SubagentStop: ModelPilot helper mode's log (October 21). Appends one line per helper launch
# and finish to helper-log.jsonl in the plugin's data directory, beside helper.sh's one line per note, for
# `python3 -m modelpilot.helper_report`. Ids and transcript paths only, never the brief or the reply. A launch from
# inside a helper names that helper as its caller. Prints nothing; always exits 0.
input=$(cat)
# The first occurrence: the hook's own fields come before tool_response and background_tasks, which repeat some keys.
field() { printf '%s' "$input" | grep -o "\"$1\" *: *\"[^\"]*\"" | head -n 1 | sed 's/^[^:]*: *"\(.*\)"$/\1/'; }
[ -n "${CLAUDE_PLUGIN_DATA:-}" ] || exit 0
session=$(field session_id | tr -cd 'A-Za-z0-9_-')
[ -n "$session" ] || exit 0
case "$(field hook_event_name)" in
  PostToolUse)
    helper=$(field subagent_type)
    case "$helper" in modelpilot:helper-*) ;; *) exit 0 ;; esac
    line="\"event\":\"launch\",\"helper\":\"$helper\",\"model\":\"$(field model)\",\"agent\":\"$(field agentId)\",\"caller\":\"$(field agent_id)\",\"caller_effort\":\"$(field level)\",\"tool_use_id\":\"$(field tool_use_id)\",\"transcript\":\"$(field transcript_path)\"" ;;
  SubagentStop)
    helper=$(field agent_type)
    case "$helper" in modelpilot:helper-*) ;; *) exit 0 ;; esac
    line="\"event\":\"finish\",\"helper\":\"$helper\",\"agent\":\"$(field agent_id)\",\"agent_transcript\":\"$(field agent_transcript_path)\"" ;;
  *) exit 0 ;;
esac
mkdir -p "$CLAUDE_PLUGIN_DATA" 2>/dev/null
printf '{"ts":%s,"session":"%s",%s}\n' "$(date +%s)" "$session" "$line" >> "$CLAUDE_PLUGIN_DATA/helper-log.jsonl" 2>/dev/null
exit 0
