/**
 * JavaScript side of the bootstrap sweep: parse an IDF with `@idfkit/core` and
 * write its canonical epJSON.
 *
 * Driven by `bootstrap_sweep.py`, which keeps one of these alive per worker.
 * Loading a schema bundle costs far more than parsing a file, so the process is
 * long-lived and speaks NDJSON rather than being spawned once per file.
 *
 *   node sweep_js.mjs <path to idfkit-js/packages/core>
 *
 * Request  (one JSON object per line on stdin):  { "input": "...", "output": "..." }
 * Response (one JSON object per line on stdout): { "input": "...", "ok": true }
 *                                             or { "input": "...", "ok": false, "error": "..." }
 *
 * The epJSON goes to `output` rather than down the pipe: a large model is tens
 * of megabytes of JSON and does not belong on a line-oriented channel.
 */

import { writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { createInterface } from 'node:readline';
import { pathToFileURL } from 'node:url';

const coreRoot = process.argv[2];
if (coreRoot === undefined) {
  process.stderr.write('usage: node sweep_js.mjs <path to idfkit-js/packages/core>\n');
  process.exit(2);
}

function describe(error) {
  if (error instanceof Error) {
    return error.stack ?? `${error.name}: ${error.message}`;
  }
  return String(error);
}

function reply(payload) {
  process.stdout.write(`${JSON.stringify(payload)}\n`);
}

// Imported by absolute path, not by the `@idfkit/core` specifier: this file
// lives in a different repository with no node_modules of its own.
let nodeApi;
let coreApi;
try {
  nodeApi = await import(pathToFileURL(join(coreRoot, 'dist', 'node.js')).href);
  coreApi = await import(pathToFileURL(join(coreRoot, 'dist', 'index.js')).href);
} catch (error) {
  process.stderr.write(
    `cannot load @idfkit/core from ${coreRoot}: ${describe(error)}\n` +
      'run `npm install && npm run build:schemas && npx tsc --build` in idfkit-js first\n'
  );
  process.exit(2);
}

for await (const line of createInterface({ input: process.stdin })) {
  const text = line.trim();
  if (text === '') continue;

  let request;
  try {
    request = JSON.parse(text);
  } catch (error) {
    reply({ input: null, ok: false, error: `unparsable request: ${describe(error)}` });
    continue;
  }

  try {
    const document = await nodeApi.loadIdf(request.input);
    await writeFile(request.output, JSON.stringify(coreApi.toEpJson(document)), 'utf8');
    reply({ input: request.input, ok: true });
  } catch (error) {
    reply({ input: request.input, ok: false, error: describe(error) });
  }
}
