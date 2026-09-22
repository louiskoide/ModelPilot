// Offline Jev/Claude Code compatibility probe. No keys, no network, no billing.
// Real Claude Code -> Jev's real startProxy() -> local fake Messages API, with a fake router in
// place of TypeSafe. Answers one question: does Jev extract a routable prompt from this client's
// request shape? Uses Jev's exported harness hook, so this is a probe, not a stock-launcher result.
// Usage: node jev_compat_probe.mjs <jev-root> <claude-cli> <isolated-dir> <out.json>
import http from 'node:http';
import { spawn } from 'node:child_process';
import { mkdirSync, writeFileSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

const [jevRoot, cli, dir, out] = process.argv.slice(2);
const { startProxy, newTurnPrompt } = await import(pathToFileURL(resolve(jevRoot, 'src/proxy.mjs')));
for (const name of ['home', 'tmp', 'config', 'fixture']) mkdirSync(join(dir, name), { recursive: true, mode: 0o700 });
writeFileSync(join(dir, 'fixture', 'sample.txt'), 'PROBE_TOKEN\n');

const requests = [], routed = [];
const frames = (model) => [
  { type: 'message_start', message: { id: 'msg_probe', type: 'message', role: 'assistant', model, content: [], stop_reason: null,
    usage: { input_tokens: 1, output_tokens: 1, cache_creation_input_tokens: 0, cache_read_input_tokens: 0 } } },
  { type: 'content_block_start', index: 0, content_block: { type: 'text', text: '' } },
  { type: 'content_block_delta', index: 0, delta: { type: 'text_delta', text: 'PROBE_TOKEN' } },
  { type: 'content_block_stop', index: 0 },
  { type: 'message_delta', delta: { stop_reason: 'end_turn' }, usage: { output_tokens: 1 } },
  { type: 'message_stop' },
].map((d) => `event: ${d.type}\ndata: ${JSON.stringify(d)}\n\n`).join('');

const upstream = http.createServer((req, res) => {
  const chunks = [];
  req.on('data', (c) => chunks.push(c));
  req.on('end', () => {
    let body = null;
    try { body = JSON.parse(Buffer.concat(chunks).toString()); } catch {}
    if (body?.messages) {
      const last = body.messages.at(-1);
      // Shape only: roles and block types, never message text beyond Jev's own extraction.
      requests.push({ url: req.url, model: body.model, tools: Array.isArray(body.tools) ? body.tools.length : 0,
        roles: body.messages.map((m) => m.role), last_role: last?.role,
        last_block_types: Array.isArray(last?.content) ? last.content.map((b) => b.type) : typeof last?.content,
        jev_prompt_extracted: newTurnPrompt(body) !== null });
    }
    if (req.method === 'GET') {
      res.writeHead(200, { 'content-type': 'application/json' });
      return res.end(JSON.stringify({ data: [], has_more: false }));
    }
    if (body?.stream) {
      res.writeHead(200, { 'content-type': 'text/event-stream' });
      return res.end(frames(body.model));
    }
    res.writeHead(200, { 'content-type': 'application/json' });
    res.end(JSON.stringify({ id: 'msg_probe', type: 'message', role: 'assistant', model: body?.model,
      content: [{ type: 'text', text: 'PROBE_TOKEN' }], stop_reason: 'end_turn',
      usage: { input_tokens: 1, output_tokens: 1, cache_creation_input_tokens: 0, cache_read_input_tokens: 0 } }));
  });
});
await new Promise((r) => upstream.listen(0, '127.0.0.1', r));

const route = async ({ current, models }) => {
  routed.push({ current, offered: models.map((m) => m.id) });
  const choice = models.find((m) => m.tier === 'sonnet')?.id ?? models[0].id;
  return { choice, confidence: 0.9, request: { probe: true }, response: { probe: true }, metrics: null, ms: 0 };
};
const { port, close } = await startProxy({ upstreamURL: `http://127.0.0.1:${upstream.address().port}`, route });

// Mirrors the stock launcher's autoModelEnv(); the key is a placeholder that never leaves loopback.
const env = { PATH: process.env.PATH, HOME: join(dir, 'home'), TMPDIR: join(dir, 'tmp'), CLAUDE_CONFIG_DIR: join(dir, 'config'),
  ANTHROPIC_API_KEY: 'sk-ant-offline-probe-placeholder', ANTHROPIC_BASE_URL: `http://127.0.0.1:${port}`,
  CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY: '1', ANTHROPIC_CUSTOM_MODEL_OPTION: 'jev-router',
  ANTHROPIC_CUSTOM_MODEL_OPTION_NAME: 'Jev Router',
  ANTHROPIC_CUSTOM_MODEL_OPTION_SUPPORTED_CAPABILITIES: 'thinking,adaptive_thinking,interleaved_thinking,effort,max_effort',
  CLAUDE_CODE_DISABLE_UNKNOWN_MODEL_WINDOW_ENFORCEMENT: '1', ANTHROPIC_MODEL: 'jev-router',
  DISABLE_AUTOUPDATER: '1', CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC: '1' };
const args = ['-p', 'Use the Read tool to read sample.txt in the current directory. Reply with exactly the token in that file.',
  '--output-format', 'stream-json', '--verbose', '--max-turns', '3', '--no-session-persistence', '--setting-sources', '',
  '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}', '--tools', 'Read', '--allowedTools', 'Read'];
const child = spawn(cli, args, { cwd: join(dir, 'fixture'), env, stdio: ['ignore', 'pipe', 'pipe'] });
let stderr = '';
child.stdout.resume();
child.stderr.on('data', (d) => { stderr += d; });
const timer = setTimeout(() => child.kill('SIGKILL'), 90000);
child.on('exit', (code, signal) => {
  clearTimeout(timer);
  close();
  upstream.close();
  const agent = requests.filter((r) => r.tools > 0);
  writeFileSync(out, JSON.stringify({
    compatible: agent.length > 0 && routed.length > 0 && agent[0].jev_prompt_extracted,
    client_exit: code, client_signal: signal, router_calls: routed.length, routed,
    first_agent_request: agent[0] ?? null, requests, stderr: stderr.slice(0, 2000),
  }, null, 2));
});
