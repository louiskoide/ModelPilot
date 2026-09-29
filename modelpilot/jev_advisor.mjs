// Jev as an advisor for ModelPilot's execution policy: it predicts the model and effort a turn needs and never
// rewrites a request. ModelPilot's proxy decides whether moving there pays (modelpilot/switch_policy.py).
//
// Reads one JSON object on stdin:
//   {"jev_root", "body", "current", "catalog", "efforts", "strip_prefix"?, "evidence"?}
// and writes one JSON object on stdout. The pinned Jev checkout is not changed: its own newTurnPrompt,
// claudeModels, complexity questions and model question are used as they are, so the model answer comes from
// the same question the Jev arm asks. One effort question is added to the same TypeSafe call. The prompt
// never appears in the output (only its length and SHA-256).
//
//   node jev_advisor.mjs                 live: JEV_API_KEY or TYPESAFE_API_KEY in the environment
//   node jev_advisor.mjs --dry           the request's structure; no key, nothing sent
//   node jev_advisor.mjs --stub <json>   offline tests only: answers taken from <json>, nothing sent
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

const input = JSON.parse(readFileSync(0, 'utf8'));
const jevRoot = resolve(input.jev_root);
const load = (path) => import(pathToFileURL(resolve(jevRoot, path)).href);
const { newTurnPrompt, claudeModels } = await load('src/proxy.mjs');
const { QUESTIONS, questionForModels, availableTiers, COMPLEXITY_MAX_SCORE, CONTEXT_WINDOW_TOKENS, THRESHOLDS } =
  await load('src/config.mjs');
const { TypeSafeClient, choice } = await load('node_modules/@typesafe-ai/sdk/dist/index.mjs');

const EFFORT_GUIDANCE = {
  low: { what: 'Mechanical or fully specified work.', signals: ['Rename, reformat, or apply an exact, given change'],
    not_for: 'Anything that needs a plan or debugging.' },
  medium: { what: 'Ordinary engineering with a clear shape.',
    signals: ['Implement a specified function or fix an understood, local bug'],
    not_for: 'Unknown-cause bugs or multi-step designs.' },
  high: { what: 'Work that needs a plan or careful checking.',
    signals: ['Multi-file changes, tricky edge cases, or debugging with a few candidate causes'],
    not_for: 'Routine edits.' },
  xhigh: { what: 'Hard reasoning.',
    signals: ['Unknown-cause debugging, subtle state or concurrency, algorithmic or cross-module design'],
    not_for: 'Work with a clear implementation.' },
  max: { what: 'The hardest problems, where correctness matters far more than cost.',
    signals: ['Deep algorithmic or security-critical reasoning that lower efforts get wrong'],
    not_for: 'Anything a lower effort can finish in one pass.' },
};

const effortQuestion = (efforts) =>
  choice(
    [
      'Pick the lowest reasoning effort at which the chosen model can fully complete this coding request in one pass, without retrying at a higher effort.',
      'Higher effort means more thinking before each step, which costs more tokens and time.',
    ],
    Object.fromEntries(efforts.map((e) => [e, EFFORT_GUIDANCE[e] ?? { what: e }])),
  );

// The turn's prompt as Jev's proxy extracts it. When the latest request continues a turn (evidence arrived
// after tool results), the prompt is that of the latest user turn, extracted the same way.
function turnPrompt(body) {
  let prompt = newTurnPrompt(body);
  const messages = body?.messages ?? [];
  for (let i = messages.length - 1; prompt === null && i >= 0; i--) {
    if (messages[i]?.role === 'user') prompt = newTurnPrompt({ ...body, messages: messages.slice(0, i + 1) });
  }
  if (prompt && input.strip_prefix && prompt.startsWith(input.strip_prefix.trim())) {
    prompt = prompt.slice(input.strip_prefix.trim().length).trim();
  }
  return prompt;
}

const body = input.body ?? {};
const prompt = turnPrompt(body);
// Jev falls back to its static tiers (Sonnet 5, Opus 5) when no catalog entry matches; advice about models the
// policy can't run would be wrong, so that is an error here, not advice.
const listed = new Set((input.catalog ?? []).map((m) => m?.id));
const models = claudeModels(input.catalog ?? []).filter((m) => availableTiers().includes(m.tier) && listed.has(m.id));
const contextTokens = Math.round(JSON.stringify(body.messages ?? []).length / 4);
const session = { current_model: input.current, context_tokens: contextTokens };
if (input.evidence) session.progress = input.evidence;
const request = {
  state: { request: prompt, session, environment: { available_models: models.map((m) => m.id) } },
  questions: { ...QUESTIONS, model: questionForModels(models), effort: effortQuestion(input.efforts ?? []) },
};
const facts = {
  prompt_found: Boolean(prompt),
  prompt_chars: prompt?.length ?? 0,
  prompt_sha256: prompt ? createHash('sha256').update(prompt).digest('hex') : null,
  context_tokens: contextTokens,
  models: models.map((m) => m.id),
};
const answer = (a) => a && { choice: a.choice, confidence: a.confidence, probabilities: a.probabilities };
const write = (out) => process.stdout.write(JSON.stringify(out) + '\n');

const mode = process.argv[2];
if (!prompt || !models.length) {
  write({ ...facts, error: prompt ? 'no_models' : 'no_prompt' });
} else if (mode === '--dry') {
  write({ ...facts, questions: Object.keys(request.questions), state_keys: Object.keys(request.state),
    session_keys: Object.keys(session), model_labels: Object.keys(request.questions.model.criteria),
    effort_labels: Object.keys(request.questions.effort.criteria),
    model_question: request.questions.model, effort_question: request.questions.effort });
} else if (mode === '--stub') {
  const stub = JSON.parse(process.argv[3]);
  write({ ...facts, stub: true, model: answer(stub.model), effort: answer(stub.effort), usage: null, ms: 0 });
} else {
  // Jev's own client settings (router.mjs): its timeouts, retries and deadline, never debug logging.
  const started = Date.now();
  const abort = new AbortController();
  const deadline = setTimeout(() => abort.abort(), THRESHOLDS.jevDeadlineMs);
  try {
    const client = new TypeSafeClient({
      apiKey: process.env.JEV_API_KEY ?? process.env.TYPESAFE_API_KEY,
      timeout: THRESHOLDS.jevTimeoutMs,
      retry: { maxRetries: THRESHOLDS.jevMaxRetries, backoffInitialMs: 150, backoffMaxMs: 400 },
      logLevel: 'warn',
    });
    const result = await client.systemOne(request, { signal: abort.signal });
    const a = result.answers;
    write({
      ...facts, model: answer(a.model), effort: answer(a.effort), router_model: result.model, usage: result.usage,
      metrics: {
        taskComplexity: a.task_complexity.score / COMPLEXITY_MAX_SCORE,
        reasoningRequired: a.reasoning_required.score / COMPLEXITY_MAX_SCORE,
        toolComplexity: a.tool_complexity.score / COMPLEXITY_MAX_SCORE,
        contextSize: Math.min(contextTokens / CONTEXT_WINDOW_TOKENS, 1),
      },
      ms: Date.now() - started,
    });
  } catch (err) {
    write({ ...facts, error: `advice_failed: ${err?.name ?? 'Error'}`, ms: Date.now() - started });
  } finally {
    clearTimeout(deadline);
  }
}
