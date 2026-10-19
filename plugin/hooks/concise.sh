#!/usr/bin/env bash
# SessionStart: add ModelPilot's concise instruction to the session once. concise.json carries the text of
# bench/prompts/concise.md (tests/test_plugin.py checks they match). Skipped when the hook's input shows another
# agent or a model other than Sonnet 5.5: the benchmark measured the instruction on Sonnet 5.5 only.
input=$(cat | tr -d ' \t\r\n')
case "$input" in
  *'"agent_type":"modelpilot:lean"'*) ;;
  *'"agent_type":'*) exit 0 ;;
esac
case "$input" in
  *'"model":"claude-sonnet-5-5'*) ;;
  *'"model":'*) exit 0 ;;
esac
cat "$(dirname "$0")/concise.json"
