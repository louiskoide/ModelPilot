#!/usr/bin/env bash
# UserPromptSubmit: ModelPilot helper mode (October 20; off until the user sets it). `/modelpilot:helper MODEL [EFFORT]`
# is handled here and blocked, so it sends no request: it saves the setting for this session in the plugin's data
# directory. While a setting is saved, each prompt gets a note asking the main session to hand the request to a helper
# on that model and effort (agents/helper-EFFORT.md, with the Agent tool's model). The main session's own model, effort
# and cache stay as they are. Prints nothing when there is nothing to do; always exits 0.
input=$(cat)
field() { printf '%s' "$input" | sed -n "s/.*\"$1\" *: *\"\([^\"]*\)\".*/\1/p" | head -n 1; }
session=$(field session_id | tr -cd 'A-Za-z0-9_-')
[ -n "$session" ] && [ -n "${CLAUDE_PLUGIN_DATA:-}" ] || exit 0
state="$CLAUDE_PLUGIN_DATA/helper-$session"
usage='Usage: /modelpilot:helper sonnet|opus|haiku [low|medium|high|xhigh|max], /modelpilot:helper off, or /modelpilot:helper status.'
reply() { printf '{"decision":"block","reason":"%s"}\n' "$1"; exit 0; }
prompt=$(field prompt)
case "$prompt" in
  /modelpilot:helper|'/modelpilot:helper '*) ;;
  '<task-notification>'*) exit 0 ;;  # a helper finishing (the client runs this hook for it too): never hand that off
  *)
    [ -f "$state" ] || exit 0
    read -r model effort < "$state"
    note="ModelPilot helper mode (the user's setting): hand this request to a helper on $model at $effort effort. Call the Agent tool once with subagent_type modelpilot:helper-$effort and model $model. The helper has none of this conversation, so make its prompt self-contained: the request in full, the file paths and facts it needs, and what has been found or decided so far. Then relay its reply in a sentence or two. Answer without a helper only when the request is about this conversation itself or the helper's last reply."
    printf '{"hookSpecificOutput":{"hookEventName":"UserPromptSubmit","additionalContext":"%s"}}\n' "$note"
    # The helper-mode log (hooks/helper-log.sh): one line per prompt that got the note, to compare with launches.
    printf '{"ts":%s,"session":"%s","event":"note","model":"%s","effort":"%s"}\n' "$(date +%s)" "$session" "$model" \
      "$effort" >> "$CLAUDE_PLUGIN_DATA/helper-log.jsonl" 2>/dev/null
    exit 0 ;;
esac
set -f
set -- ${prompt#/modelpilot:helper}
case "$1" in
  ''|status)
    if [ -f "$state" ]; then
      read -r model effort < "$state"
      reply "ModelPilot helper mode is on: each request goes to a helper on $model at $effort effort. /modelpilot:helper off stops it."
    fi
    reply "ModelPilot helper mode is off. $usage" ;;
  off)
    rm -f "$state"
    reply 'ModelPilot helper mode is off: this session handles requests itself again.' ;;
  sonnet|opus|haiku) ;;
  *) reply "Nothing was changed. $usage" ;;
esac
model=$1 effort=${2:-medium}
case "$effort" in low|medium|high|xhigh|max) ;; *) reply "Nothing was changed. $usage" ;; esac
[ $# -le 2 ] || reply "Nothing was changed. $usage"
mkdir -p "$CLAUDE_PLUGIN_DATA" && printf '%s %s\n' "$model" "$effort" > "$state" || reply 'Nothing was changed: the setting could not be saved.'
reply "ModelPilot helper mode is on: each request goes to a helper on $model at $effort effort, and this session's model, effort and cache stay as they are. The helper starts without this conversation. For an effort change alone, /effort is cheaper (see the plugin README). /modelpilot:helper off stops it."
