// One real router decision, no provider generation or upstream launcher.
import { pathToFileURL } from 'node:url';
import { resolve } from 'node:path';
const { askJev } = await import(pathToFileURL(resolve(process.argv[2], 'src/router.mjs')));
const diagnostic = process.argv.includes('--diagnostic');
if (diagnostic) {
  // In-memory diagnostic override only; upstream files and baseline remain unchanged.
  const { THRESHOLDS } = await import(pathToFileURL(resolve(process.argv[2], 'src/config.mjs')));
  THRESHOLDS.jevTimeoutMs = 15000;
  THRESHOLDS.jevDeadlineMs = 16000;
  THRESHOLDS.jevMaxRetries = 0;
}
const models = [
  {id: 'claude-sonnet-4-6', tier: 'sonnet'},
  {id: 'claude-opus-4-6', tier: 'opus'},
];
const started = Date.now();
const result = await askJev({
  prompt: 'Find the misspelled word recieve in a README and report its corrected spelling. Do not edit files.',
  current: 'claude-opus-4-6', contextTokens: 0, models,
});
console.log(JSON.stringify({diagnostic, result, wall_ms: Date.now() - started, offered_models: models.map(m => m.id)}));
