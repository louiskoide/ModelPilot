// Accounted Jev launcher (harness): Claude Code -> Jev's real proxy -> ModelPilot proxy -> Anthropic.
//
// Mirrors the pinned bin/jev-claude.mjs so Jev's routing is unchanged, with these differences only:
//   1. startProxy({ upstreamURL }) points Jev at ModelPilot's proxy instead of api.anthropic.com.
//   2. No .env / ~/.jev-router.env / ~/.jev-claude.env loading: the caller passes an explicit environment.
//   3. No status line (--settings) and no saved-model restore: benchmark runs use an isolated HOME.
//   4. Refuses to start without JEV_API_KEY/TYPESAFE_API_KEY instead of launching Claude Code unrouted.
// The routing policy, prompt extraction, tier rewrite and catalog discovery are Jev's own code.
//
// Usage: node jev_accounted_launch.mjs <jev-root> <modelpilot-proxy-url> -- <claude args...>
//        node jev_accounted_launch.mjs --self-test <jev-root> <upstream-url>   (stub router, no Claude Code)
import { spawn } from 'node:child_process';
import { accessSync, constants } from 'node:fs';
import http from 'node:http';
import { join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

const argv = process.argv.slice(2);
const selfTest = argv[0] === '--self-test';
const [jevRoot, upstreamURL] = selfTest ? argv.slice(1, 3) : argv.slice(0, 2);
if (!jevRoot || !/^http:\/\/127\.0\.0\.1:\d+$/.test(upstreamURL ?? '')) {
  process.stderr.write('[accounted] usage: <jev-root> <http://127.0.0.1:PORT> -- <claude args>\n');
  process.exit(2);
}
const { startProxy } = await import(pathToFileURL(resolve(jevRoot, 'src/proxy.mjs')));
const { AUTO_MODEL } = await import(pathToFileURL(resolve(jevRoot, 'src/config.mjs')));

// Same values as the pinned launcher's autoModelEnv().
function autoModelEnv() {
  const env = {
    ANTHROPIC_CUSTOM_MODEL_OPTION: AUTO_MODEL,
    ANTHROPIC_CUSTOM_MODEL_OPTION_NAME: 'Jev Router',
    ANTHROPIC_CUSTOM_MODEL_OPTION_DESCRIPTION: 'Route each turn to the cheapest model that can do it',
    ANTHROPIC_CUSTOM_MODEL_OPTION_SUPPORTED_CAPABILITIES: 'thinking,adaptive_thinking,interleaved_thinking,effort,max_effort',
    CLAUDE_CODE_DISABLE_UNKNOWN_MODEL_WINDOW_ENFORCEMENT: '1',
  };
  if (!process.env.ANTHROPIC_MODEL) env.ANTHROPIC_MODEL = AUTO_MODEL;
  return env;
}

function request(port, method, path, body) {
  return new Promise((done, fail) => {
    const data = body ? JSON.stringify(body) : undefined;
    const req = http.request({ host: '127.0.0.1', port, method, path,
      headers: { 'content-type': 'application/json', 'x-api-key': 'sk-ant-self-test-placeholder', 'anthropic-version': '2023-06-01' } },
      (res) => { const chunks = []; res.on('data', (c) => chunks.push(c)); res.on('end', () => done({ status: res.statusCode, body: Buffer.concat(chunks).toString() })); });
    req.on('error', fail);
    if (data) req.write(data);
    req.end();
  });
}

if (selfTest) {
  // Plumbing check only: a stub replaces TypeSafe and no Claude Code runs.
  let offered = null;
  const route = async ({ models }) => {
    offered = models.map((m) => m.id);
    const choice = models.find((m) => m.tier === 'sonnet')?.id ?? models[0].id;
    return { choice, confidence: 0.9, request: { selfTest: true }, response: { selfTest: true }, metrics: null, ms: 0 };
  };
  const { port, close } = await startProxy({ upstreamURL, route });
  try {
    await request(port, 'GET', '/v1/models?limit=100');
    const reply = await request(port, 'POST', '/v1/messages', {
      model: AUTO_MODEL, max_tokens: 32, tools: [{ name: 'Read', input_schema: { type: 'object' } }],
      messages: [{ role: 'user', content: 'Read sample.txt' }, { role: 'system', content: '# Environment' }] });
    process.stdout.write(JSON.stringify({ status: reply.status, offered }) + '\n');
  } finally {
    close();
  }
  process.exit(0);
}

const sep = argv.indexOf('--');
const args = sep >= 0 ? argv.slice(sep + 1) : [];
if (!(process.env.JEV_API_KEY || process.env.TYPESAFE_API_KEY)) {
  process.stderr.write('[accounted] no JEV_API_KEY: refusing to run Claude Code unrouted\n');
  process.exit(2);
}
function resolveClaude() {
  for (const dir of (process.env.PATH ?? '').split(':')) {
    if (!dir) continue;
    const file = join(dir, 'claude');
    try { accessSync(file, constants.X_OK); return file; } catch {}
  }
  return null;
}
const binaryIndex = argv.indexOf('--claude-bin');
const explicitClaude = binaryIndex >= 0 ? argv[binaryIndex + 1] : null;
if (binaryIndex >= 0 && (!explicitClaude || binaryIndex >= sep || !explicitClaude.startsWith('/'))) {
  process.stderr.write('[accounted] --claude-bin requires an absolute executable path before --\n');
  process.exit(2);
}
if (explicitClaude) {
  try { accessSync(explicitClaude, constants.X_OK); }
  catch { process.stderr.write('[accounted] explicit Claude binary is unavailable\n'); process.exit(2); }
}
const claude = explicitClaude || resolveClaude();
if (!claude) {
  process.stderr.write('[accounted] claude is not on PATH\n');
  process.exit(1);
}
const { port, close } = await startProxy({ upstreamURL });
const env = { ...process.env, ANTHROPIC_BASE_URL: `http://127.0.0.1:${port}`,
  CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY: '1', ...autoModelEnv() };
const child = spawn(claude, [...args, '--add-dir', resolve(jevRoot)], { stdio: 'inherit', env });
child.on('error', (err) => { process.stderr.write(`[accounted] could not start Claude Code: ${err.message}\n`); close(); process.exit(1); });
child.on('exit', (code, signal) => { close(); process.exit(signal ? 1 : (code ?? 0)); });
