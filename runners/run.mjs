#!/usr/bin/env node
/**
 * The JavaScript conformance runner: six shipping assertions against one exit contract.
 *
 * Run it from the root of this repository, pointing `--library` at a checkout of the JavaScript
 * monorepo. The flag takes a **path**, never a language word, because the runner file already
 * fixes the language: `run.py` drives Python and `run.mjs` drives JavaScript.
 *
 *     node runners/run.mjs --library /path/to/idfkit-js
 *     node runners/run.mjs --library /path/to/idfkit-js --case naming-blank-vs-absent
 *     node runners/run.mjs --library /path/to/idfkit-js --tag extensible
 *
 * What it checks, per case, in this order:
 *
 * 1. `parse-outcome`  the input parses, or fails to parse, exactly as `case.toml` declares.
 * 2. `epjson`         the library's canonical epJSON equals the committed `expected.epJSON`.
 * 3. `round-trip`     re-parsing the library's own IDF output deep-equals the original document.
 * 4. `diagnostics`    the parse findings the library reports equal `expected.diag.json`, compared as
 *                     an unordered multiset of `(code, line, typeName)` and never on message text.
 * 5. `validation`     the findings the library reports equal `expected.validation.json`.
 * 6. `introspection`  its type descriptions equal `expected.introspection.json`.
 * 7. `docs-url`       its documentation addresses equal `expected.docs-url.json`.
 * 8. `type-lookup`   naming an object type through the library's own collection accessor returns
 *                    what `expected.type-lookup.json` says, for canonical, mis-cased and unknown
 *                    names alike, and leaves the document unchanged.
 *
 * This file is a section-by-section mirror of `run.py`: the same section headers, the same shapes,
 * the same statuses, the same reconciliation, and byte-for-byte the same report strings apart from
 * the library name and the paths. That is deliberate. A reader who diffs the two transcripts sees
 * only the genuine disagreements, which is what makes a one-sided regression obvious.
 *
 * Assertion 3 is the only place IDF field order is compared, which is rule 4 of `compare.md`. That
 * rule is a constraint on what this runner hands to the comparator rather than on the comparator
 * itself: `documentSnapshot` puts each object's field sequence in a JSON **array**, because rule 7
 * makes every array ordered and compared index by index. Flattening the sequence into an object
 * would throw the order away before the comparator ever saw it, and nothing else in the suite
 * catches a round trip that emits the right values in the wrong slots.
 *
 * The snapshot shape, so a reader of a failure path can decode it:
 *
 *     {"<ObjectType>": [{"name": "<object name>",
 *                        "fields": [{"name": "<field>", "value": <value>}, ...]}, ...]}
 *
 * so `/Zone/0/fields/4/name` is the name of the fifth field of the first `Zone`, and
 * `/Zone/0/fields/4/value` is its value. A field that moved shows up as a `value` difference on
 * the `name` path, which is what a positional bug looks like.
 *
 * Four behaviours that are requirements rather than conveniences:
 *
 * - **Outstanding exceptions are printed in the normal output**, never behind a verbose flag. A
 *   silent allowlist is how a temporary exception becomes permanent, so every entry in
 *   `known-divergence.toml` that is still failing is listed with its issue link on every run.
 * - **A stale exception fails the run.** An entry whose case now passes exits 1 and says to remove
 *   it, because a stale exception is removed by the change that fixes the bug.
 * - **An empty corpus is a clean pass**, not a crash. `cases/` is populated by curation from a
 *   bootstrap sweep, and the runner must be usable, and testable, before that lands. No case id
 *   appears anywhere in this file.
 * - **A `--case` or `--tag` filter that selects nothing cannot start.** A green run over zero cases
 *   proves nothing, and it is the one failure a conformance suite must not have: `--tag tier1`
 *   wired into CI against a corpus carrying no such case would report success for ever. That is a
 *   different situation from an empty corpus, and it is reported differently.
 *
 * Exit codes: 0 when everything is green or every failure is allowlisted, 1 for any blocking
 * failure, stale entry, or corpus fault, and 2 when the run could not start at all, for instance
 * because `--library` names no built checkout or because a filter matched no case.
 *
 * LOADING THE LIBRARY. `@idfkit/core` is an ESM TypeScript workspace, and this repository has no
 * `package.json`, no TypeScript, and no dependencies. The runner therefore imports the package's
 * **build output**, `packages/core/dist/index.js` and `packages/core/dist/node.js`, which is what
 * the `exports` map in `packages/core/package.json` already points at and what a consumer
 * installing from npm would get. Importing the sources instead would need a TypeScript loader
 * in this repository and would test a compilation this project performs rather than the one the
 * library ships. A missing `dist/` is an unusable run, not a failure, and says to build the
 * checkout.
 *
 * Node version: 20 or newer, matching `packages/core/package.json`.
 */

import { spawnSync } from 'node:child_process';
import { mkdtempSync, readFileSync, realpathSync, rmSync, statSync, writeFileSync } from 'node:fs';
import { availableParallelism, tmpdir } from 'node:os';
import { gunzipSync } from 'node:zlib';
import { dirname, isAbsolute, join, resolve, sep } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { parseArgs } from 'node:util';

import {
  AssertionReport,
  compareDocuments,
  compareEpjson,
  compareOutcome,
  compareUnordered,
  compareValues,
  Comparison,
} from './compare.mjs';
import {
  Assertion,
  CASES_DIR,
  CORPUS_LEVEL_PATTERN,
  CorpusError,
  DIVERGENCE_FILE,
  EXPECTED_DOCS_URL,
  EXPECTED_EPJSON,
  EXPECTED_DIAGNOSTICS,
  EXPECTED_INTROSPECTION,
  EXPECTED_TYPE_LOOKUP,
  EXPECTED_VALIDATION,
  InputFile,
  Library,
  MANIFEST_FILE,
  ParseOutcome,
  Tag,
  Truth,
  loadCorpus,
  loadManifest,
} from './model.mjs';

const RUNNERS_DIR = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = dirname(RUNNERS_DIR);

/** This file drives the JavaScript library and nothing else. `run.py` is its mirror. */
const LIBRARY = Library.TYPESCRIPT;

/**
 * Rule 6 of compare.md. Every IDF this runner reads or writes is latin-1, which never fails on any
 * byte sequence, so a library that garbles a high byte reports it as a value difference rather than
 * crashing the comparison. Node spells the encoding `latin1`.
 */
const IDF_ENCODING = 'latin1';

/** The assertion order used in every report, whatever order a case.toml happens to list. */
const ASSERTION_ORDER = Object.freeze([
  Assertion.PARSE_OUTCOME,
  Assertion.EPJSON,
  Assertion.ROUND_TRIP,
  Assertion.DIAGNOSTICS,
  Assertion.VALIDATION,
  Assertion.INTROSPECTION,
  Assertion.DOCS_URL,
  Assertion.TYPE_LOOKUP,
]);

const DEFAULT_DIFFERENCE_LIMIT = 20;
const MAX_POOL_WORKERS = 8;
const POOL_THRESHOLD = 8; // Below this many cases a pool costs more than it saves.

const EXIT_OK = 0;
const EXIT_FAILED = 1;
const EXIT_UNUSABLE = 2;

/** The run cannot start: a bad flag, or a `--library` path with nothing built in it. */
export class RunnerError extends Error {
  /** @param {string} message */
  constructor(message) {
    super(message);
    this.name = 'RunnerError';
  }
}

/**
 * What one assertion did.
 *
 * `FAILED` is the only status the exception register can accept. `ERRORED` means the runner could
 * not evaluate the assertion at all, which is a corpus or environment problem rather than a library
 * disagreement, so it always blocks and is never allowlistable.
 *
 * @readonly
 * @enum {string}
 */
export const Status = Object.freeze({
  PASSED: 'pass',
  FAILED: 'fail',
  SKIPPED: 'skip',
  ERRORED: 'error',
});

// ---------------------------------------------------------------------------
// The shapes carried through the run
// ---------------------------------------------------------------------------
//
// Classes rather than bare objects, matching `run.py`'s dataclasses one for one, so nothing in this
// file indexes into an untyped record or compares a string against a spelling it hopes is correct.

/** The checkout `--library` named, once its build output has been imported out of that path. */
export class LibraryUnderTest {
  /**
   * @param {{ root: string, packageRoot: string, moduleFile: string, version: string,
   *           core: Record<string, *>, node: Record<string, *>,
   *           prose: readonly string[] | undefined }} fields
   */
  constructor({ root, packageRoot, moduleFile, version, core, node, prose }) {
    /** @type {string} */
    this.root = root;
    /** @type {string} */
    this.packageRoot = packageRoot;
    /** @type {string} */
    this.moduleFile = moduleFile;
    /** @type {string} */
    this.version = version;
    /** The portable surface: parsing, writing, and the document types. */
    this.core = core;
    /** The Node edge: schema resolution against the bundle this checkout ships. */
    this.node = node;
    /**
     * The schema's explanatory prose, or undefined when this checkout ships none.
     *
     * TypeScript keeps `describeObjectType` synchronous and takes the pool as an
     * argument, so the prose is opt-in for the caller rather than always loaded.
     * This runner is a caller that wants the whole description, so it opts in.
     * Python needs no equivalent step: its schema object carries the prose already.
     *
     * Undefined here is not a skip. It means the checkout predates the pool, and
     * the description is then compared without prose, which is what the corpus
     * did before feature 002 and what the allowlist entry recorded.
     */
    this.prose = prose;
    Object.freeze(this);
  }

  /** One line for the header: which implementation, which build, and where it came from. */
  describe() {
    return `${LIBRARY}, @idfkit/core ${this.version} from ${this.moduleFile}`;
  }
}

/** The corpus level this run is reporting against, and where the value came from. */
export class Level {
  /**
   * @param {string | null} value
   * @param {string} source
   */
  constructor(value, source) {
    /** @type {string | null} */
    this.value = value;
    /** @type {string} */
    this.source = source;
    Object.freeze(this);
  }

  /** One line for the header. An unpinned corpus says so rather than inventing a level. */
  describe() {
    if (this.value === null) {
      return `unpinned (${this.source})`;
    }
    return `${this.value} (${this.source})`;
  }
}

/** Everything one case needs, flattened so a worker needs no corpus of its own. */
export class CaseJob {
  /**
   * @param {{ caseId: string, caseDir: string, inputFile: InputFile,
   *           expectedParseOutcome: ParseOutcome, truth: Truth,
   *           assertions: readonly Assertion[], writerOptions?: * }} fields
   */
  constructor({
    caseId,
    caseDir,
    inputFile,
    expectedParseOutcome,
    truth,
    assertions,
    writerOptions = null,
  }) {
    /** @type {string} */
    this.caseId = caseId;
    /** @type {string} */
    this.caseDir = caseDir;
    /** @type {InputFile} */
    this.inputFile = inputFile;
    /** @type {ParseOutcome} */
    this.expectedParseOutcome = expectedParseOutcome;
    /** @type {Truth} */
    this.truth = truth;
    /** @type {readonly Assertion[]} */
    this.assertions = Object.freeze([...assertions]);
    /**
     * Writer controls this case applies before re-reading, or null for every writer default.
     *
     * @type {* | null}
     */
    this.writerOptions = writerOptions ?? null;
    Object.freeze(this);
  }

  /** The case input, whose extension decides which reader the runner dispatches to. */
  get inputPath() {
    return join(this.caseDir, this.inputFile);
  }

  /** The committed expectation. Present only when `truth = oracle`. */
  get expectedEpjsonPath() {
    return join(this.caseDir, EXPECTED_EPJSON);
  }
}

/**
 * One assertion's verdict for one case, with everything the exit contract has to print.
 *
 * `lines` is the finished printable block. For a failed comparison it comes straight from
 * `compare.AssertionReport.render`, which is the exit contract's own layout, so this runner never
 * writes a second rendering of it. `reason` is the one-line summary the digest views use, and
 * `firstDifference` is the single most informative line, quoted beside an accepted exception so a
 * reader sees what the library does today without scrolling.
 */
export class AssertionOutcome {
  /**
   * @param {{ caseId: string, assertion: Assertion, status: Status, reason?: string,
   *           lines?: readonly string[], firstDifference?: string }} fields
   */
  constructor({ caseId, assertion, status, reason = '', lines = [], firstDifference = '' }) {
    /** @type {string} */
    this.caseId = caseId;
    /** @type {Assertion} */
    this.assertion = assertion;
    /** @type {Status} */
    this.status = status;
    /** @type {string} */
    this.reason = reason;
    /** @type {readonly string[]} */
    this.lines = Object.freeze([...lines]);
    /** @type {string} */
    this.firstDifference = firstDifference;
    Object.freeze(this);
  }

  /** Whether this outcome fails the run unless the register accepts it. */
  get blocks() {
    return this.status === Status.FAILED || this.status === Status.ERRORED;
  }

  /**
   * The report block for this outcome.
   *
   * @returns {readonly string[]}
   */
  render() {
    if (this.lines.length > 0) {
      return this.lines;
    }
    return Object.freeze([head(this.caseId, this.assertion, this.reason)]);
  }
}

/** Every assertion a case declared, plus what the case cost. */
export class CaseOutcome {
  /**
   * @param {{ caseId: string, seconds: number, outcomes?: readonly AssertionOutcome[] }} fields
   */
  constructor({ caseId, seconds, outcomes = [] }) {
    /** @type {string} */
    this.caseId = caseId;
    /** @type {number} */
    this.seconds = seconds;
    /** @type {readonly AssertionOutcome[]} */
    this.outcomes = Object.freeze([...outcomes]);
    Object.freeze(this);
  }

  /**
   * This case's verdict on one assertion, or `null` when the case never declared it.
   *
   * @param {Assertion} assertion
   * @returns {AssertionOutcome | null}
   */
  outcomeFor(assertion) {
    for (const outcome of this.outcomes) {
      if (outcome.assertion === assertion) {
        return outcome;
      }
    }
    return null;
  }
}

/**
 * What the run found, sorted into the buckets the exit contract distinguishes.
 *
 * Mutable on purpose: it is filled in as results arrive, then read once to print and once to decide
 * the exit code.
 */
export class RunReport {
  /**
   * @param {{ library: LibraryUnderTest, level: Level, corpusRoot: string,
   *           registerNote: string, selected: number, total: number }} fields
   */
  constructor({ library, level, corpusRoot, registerNote, selected, total }) {
    /** @type {LibraryUnderTest} */
    this.library = library;
    /** @type {Level} */
    this.level = level;
    /** @type {string} */
    this.corpusRoot = corpusRoot;
    /** @type {string} */
    this.registerNote = registerNote;
    /** @type {number} */
    this.selected = selected;
    /** @type {number} */
    this.total = total;
    /** @type {CaseOutcome[]} */
    this.cases = [];
    /** @type {AssertionOutcome[]} */
    this.blocking = [];
    /** @type {Array<[import('./model.mjs').Divergence, AssertionOutcome]>} */
    this.outstanding = [];
    /** @type {import('./model.mjs').Divergence[]} */
    this.stale = [];
    /** @type {Array<[import('./model.mjs').Divergence, string]>} */
    this.unexercisable = [];
    /** @type {import('./model.mjs').Divergence[]} */
    this.deferredExceptions = [];
    /** @type {number} */
    this.unselectedExceptions = 0;
    /** @type {number} */
    this.seconds = 0;
  }

  /**
   * How many assertions landed in each status, across every case that ran.
   *
   * @returns {Record<Status, number>}
   */
  counts() {
    /** @type {Record<string, number>} */
    const tally = {};
    for (const status of Object.values(Status)) {
      tally[status] = 0;
    }
    for (const record of this.cases) {
      for (const outcome of record.outcomes) {
        tally[outcome.status] += 1;
      }
    }
    return tally;
  }

  /** How many assertions ran, which is not the case count: a case declares several. */
  get assertionCount() {
    let total = 0;
    for (const record of this.cases) {
      total += record.outcomes.length;
    }
    return total;
  }

  /** Whether the run passes: nothing blocking, nothing stale, no corpus fault. */
  get green() {
    return this.blocking.length === 0 && this.stale.length === 0 && this.unexercisable.length === 0;
  }
}

// ---------------------------------------------------------------------------
// Importing the library out of the path `--library` named
// ---------------------------------------------------------------------------

/**
 * Load the schema's explanatory prose from the checkout under test.
 *
 * Returns undefined rather than throwing when the file is not there. A checkout
 * from before feature 002 ships no pool, and the right behaviour then is to
 * compare the description without prose and let the allowlist entry explain the
 * difference, not to fail the whole run with an import error.
 *
 * @param {string} resolved  the checkout root
 * @param {string} packageRoot  the `@idfkit/core` package root inside it
 * @returns {Promise<readonly string[] | undefined>}
 */
async function importProse(resolved, packageRoot) {
  // `@idfkit/schemas` sits beside `@idfkit/core` in the workspace, and its data
  // directory is what ships in the published package too.
  const candidates = [
    join(resolved, 'packages', 'schemas', 'data', 'docs.json.gz'),
    join(packageRoot, 'node_modules', '@idfkit', 'schemas', 'data', 'docs.json.gz'),
    join(resolved, 'node_modules', '@idfkit', 'schemas', 'data', 'docs.json.gz'),
  ];
  const file = candidates.find((candidate) => isFile(candidate));
  if (file === undefined) return undefined;

  try {
    const parsed = JSON.parse(gunzipSync(readFileSync(file)).toString('utf8'));
    return Array.isArray(parsed) ? parsed : undefined;
  } catch {
    // A pool that will not parse is worse than none: it would produce prose for
    // the wrong fields. Fall back to no prose and let the comparison say so.
    return undefined;
  }
}

/**
 * Import `@idfkit/core` out of the checkout at `root`, never from an installed copy elsewhere.
 *
 * The suite tests a checkout, so a package resolved from some ambient `node_modules` would silently
 * test the wrong build. Both entry points are addressed by absolute path and then verified to sit
 * under `root`, and a mismatch is an error rather than a warning.
 *
 * `dist/` is the build output of `tsc --build`. It is what the package's `exports` map points at
 * and what an npm consumer receives, so testing it tests what the library ships.
 *
 * @param {string} root
 * @returns {Promise<LibraryUnderTest>}
 */
export async function importLibrary(root) {
  const resolved = resolve(expandUser(root));
  if (!isDir(resolved)) {
    throw new RunnerError(`--library ${resolved} is not a directory`);
  }

  const candidates = [join(resolved, 'packages', 'core'), resolved];
  const packageRoot = candidates.find((candidate) => isFile(join(candidate, 'dist', 'index.js')));
  if (packageRoot === undefined) {
    const unbuilt = candidates.find((candidate) => isFile(join(candidate, 'package.json')));
    const looked = candidates.map((candidate) => join(candidate, 'dist', 'index.js')).join(', ');
    throw new RunnerError(
      `no built '@idfkit/core' under ${resolved}. Looked for ${looked}. ` +
        (unbuilt === undefined
          ? ''
          : `Build the checkout first: 'npm install && npm run build' in ${resolved}. `) +
        `run.mjs drives the JavaScript library; use ` +
        `'python runners/run.py --library <path>' for idfkit`
    );
  }

  const indexFile = join(packageRoot, 'dist', 'index.js');
  const nodeFile = join(packageRoot, 'dist', 'node.js');
  if (!isFile(nodeFile)) {
    throw new RunnerError(
      `${indexFile} is built but ${nodeFile} is missing, so the schema bundle cannot be reached. ` +
        `Rebuild the checkout with 'npm run build' in ${resolved}`
    );
  }

  const moduleFile = realpathSync(indexFile);
  if (!isUnder(moduleFile, realpathSync(packageRoot))) {
    throw new RunnerError(
      `imported '@idfkit/core' from ${moduleFile}, which is not under ${packageRoot}. ` +
        `Another copy is shadowing the checkout, so the run would test the wrong build`
    );
  }

  /** @type {Record<string, *>} */
  let core;
  /** @type {Record<string, *>} */
  let node;
  try {
    core = await import(pathToFileURL(indexFile).href);
    node = await import(pathToFileURL(nodeFile).href);
  } catch (error) {
    throw new RunnerError(
      `could not import '@idfkit/core' from ${packageRoot}: ${describeError(error)}. ` +
        `A workspace dependency may be uninstalled: run 'npm install' in ${resolved}`
    );
  }

  for (const name of ['parseIdf', 'parseEpJson', 'writeIdf', 'writeEpJson', 'getIdfVersion']) {
    if (typeof core[name] !== 'function') {
      throw new RunnerError(
        `'@idfkit/core' at ${moduleFile} exports no ${name}(), so this runner cannot drive it. ` +
          `The build output may be stale: rebuild with 'npm run build' in ${resolved}`
      );
    }
  }

  return new LibraryUnderTest({
    root: resolved,
    packageRoot,
    moduleFile,
    version: packageVersion(join(packageRoot, 'package.json')),
    core,
    node,
    prose: await importProse(resolved, packageRoot),
  });
}

/**
 * The `version` field of a `package.json`, or `unknown`, mirroring `run.py`'s `__version__` read.
 *
 * @param {string} path
 * @returns {string}
 */
function packageVersion(path) {
  try {
    const raw = JSON.parse(readFileSync(path, 'utf8'));
    return typeof raw.version === 'string' ? raw.version : 'unknown';
  } catch {
    return 'unknown';
  }
}

// ---------------------------------------------------------------------------
// The level: an explicit flag, else the repository's git tag, else the manifest
// ---------------------------------------------------------------------------

/**
 * Resolve the corpus level. A level is an immutable git tag of the form `conformance-YYYY.N`.
 *
 * @param {string} root
 * @param {import('./model.mjs').Manifest} manifest
 * @param {string | undefined} explicit
 * @returns {Level}
 */
export function detectLevel(root, manifest, explicit) {
  if (explicit !== undefined) {
    if (!CORPUS_LEVEL_PATTERN.test(explicit)) {
      throw new RunnerError(`--level must match conformance-YYYY.N, got ${quote(explicit)}`);
    }
    return new Level(explicit, '--level');
  }

  const exact = gitTag(root, true);
  if (exact !== null) {
    return new Level(exact, 'git tag');
  }
  const nearest = gitTag(root, false);
  if (nearest !== null) {
    return new Level(nearest, 'nearest git tag, HEAD is ahead of it');
  }
  if (manifest.corpusLevel !== null) {
    return new Level(manifest.corpusLevel, 'manifest.json');
  }
  return new Level(null, 'no conformance-* git tag, and manifest.json sets corpus_level to null');
}

/**
 * The level tag on HEAD, or the most recent one, or `null`. Never throws.
 *
 * @param {string} root
 * @param {boolean} exact
 * @returns {string | null}
 */
function gitTag(root, exact) {
  const command = ['-C', root, 'describe', '--tags', '--match', 'conformance-*'];
  command.push(exact ? '--exact-match' : '--abbrev=0');
  const completed = spawnSync('git', command, { encoding: 'utf8', timeout: 10_000 });
  if (completed.error !== undefined || completed.status !== 0) {
    return null;
  }
  const tag = (completed.stdout ?? '').trim();
  return CORPUS_LEVEL_PATTERN.test(tag) ? tag : null;
}

// ---------------------------------------------------------------------------
// Selecting cases
// ---------------------------------------------------------------------------

/**
 * The cases to run, in manifest order, narrowed by `--case` and `--tag`.
 *
 * Both filters may be repeated. Given both, a case must match an id **and** carry a tag, so
 * `--case x --tag numeric` asks whether that one case pins that hazard.
 *
 * A filter that selects nothing is a `RunnerError`, and so exit 2, never an empty pass. A run that
 * checks no case proves nothing, and reporting it green is how `--tag tier1` ends up in CI as a
 * gate over a corpus that carries no such case. This is the same treatment an unknown `--case` id
 * and an unknown `--tag` value already get, for the same reason. An empty **corpus** is a different
 * situation and keeps its own message and its clean pass: `cases/` is curated after the runner
 * ships, so nothing is wrong with a runner that has nothing to run yet.
 *
 * @param {import('./model.mjs').Corpus} corpus
 * @param {readonly string[]} caseIds
 * @param {readonly Tag[]} tags
 * @returns {readonly CaseJob[]}
 */
export function buildJobs(corpus, caseIds, tags) {
  const entries = [...corpus.manifest.entries()];
  const known = new Set(entries.map((entry) => entry.id));
  const unknown = caseIds.filter((caseId) => !known.has(caseId));
  if (unknown.length > 0) {
    throw new RunnerError(
      `--case named ${unknown.map(quote).join(', ')}, which is not in the manifest. ` +
        `The corpus holds ${known.size} case(s)`
    );
  }

  const wantedIds = new Set(caseIds);
  const wantedTags = new Set(tags);
  /** @type {CaseJob[]} */
  const jobs = [];
  for (const entry of entries) {
    if (wantedIds.size > 0 && !wantedIds.has(entry.id)) {
      continue;
    }
    if (wantedTags.size > 0 && !entry.tags.some((tag) => wantedTags.has(tag))) {
      continue;
    }
    jobs.push(
      new CaseJob({
        caseId: entry.id,
        caseDir: corpus.caseDir(entry.id),
        inputFile: entry.input,
        expectedParseOutcome: entry.parseOutcome,
        truth: entry.truth,
        assertions: ASSERTION_ORDER.filter((assertion) => entry.assertions.includes(assertion)),
        writerOptions: entry.writerOptions,
      })
    );
  }

  // Every id and every tag was individually valid, and together they still match nothing. The
  // filters are named back rather than summarised, because the caller has to see which pair of
  // them cannot both hold.
  if (jobs.length === 0 && (caseIds.length > 0 || tags.length > 0)) {
    const filters = [
      ...caseIds.map((caseId) => `--case ${quote(caseId)}`),
      ...tags.map((tag) => `--tag ${quote(tag)}`),
    ].join(' ');
    throw new RunnerError(
      `${filters} selected no case, so the run would check nothing. The corpus holds ` +
        `${known.size} case(s)` +
        (caseIds.length > 0 && tags.length > 0
          ? `, and given both filters a case must match an id and carry a tag`
          : '')
    );
  }
  return Object.freeze(jobs);
}

// ---------------------------------------------------------------------------
// The six shipping assertions
// ---------------------------------------------------------------------------

/** What reading the input did: the outcome, the document if there is one, and the error if not. */
class Parse {
  /**
   * @param {ParseOutcome} outcome
   * @param {* | null} [document]
   * @param {string} [error]
   * @param {readonly *[]} [diagnostics]
   */
  constructor(outcome, document = null, error = '', diagnostics = []) {
    /** @type {ParseOutcome} */
    this.outcome = outcome;
    /** @type {* | null} */
    this.document = document;
    /** @type {string} */
    this.error = error;
    /**
     * The findings that STOPPED the read, from the error that reported them.
     *
     * Empty on success, because a read that succeeded had none by definition. The recoverable
     * findings are a separate question and are read from the returning path by assertion 4.
     *
     * @type {readonly *[]}
     */
    this.diagnostics = Object.freeze([...diagnostics]);
    Object.freeze(this);
  }
}

/**
 * Read the case input with the reader its extension dispatches to. A crash here is a finding.
 *
 * Rule 6 is applied here rather than left to the library: an IDF is decoded latin-1 and an epJSON
 * utf-8, by this runner, so neither the platform default nor a future change of the library's own
 * default can decide it.
 *
 * @param {LibraryUnderTest} library
 * @param {CaseJob} job
 * @returns {Promise<Parse>}
 */
async function parseInput(library, job) {
  try {
    if (job.inputFile === InputFile.IDF) {
      const text = readFileSync(job.inputPath).toString(IDF_ENCODING);
      const schema = await library.node.schemaFor(library.core.getIdfVersion(text));
      return new Parse(ParseOutcome.SUCCESS, library.core.parseIdf(text, schema).document);
    }
    const text = readFileSync(job.inputPath, 'utf8');
    const schema = await library.node.schemaFor(library.core.getEpJsonVersion(text));
    return new Parse(ParseOutcome.SUCCESS, library.core.parseEpJson(text, schema).document);
  } catch (error) {
    // Any failure to read is the observed outcome, not a crash.
    //
    // `IdfParseError` carries every finding that stopped the parse. Any other error carries none,
    // and an empty list is the honest answer rather than one synthesised from the message text.
    const carried = Array.isArray(/** @type {*} */ (error)?.diagnostics)
      ? /** @type {*} */ (error).diagnostics
      : [];
    return new Parse(ParseOutcome.FAILURE, null, describeError(error), carried);
  }
}

/**
 * A JSON-safe view of a parsed document in which the field sequence is an array.
 *
 * Rule 4 of `compare.md` compares IDF field order in assertion 3 and nowhere else, and rule 7 makes
 * arrays ordered. Putting each object's fields in an array is therefore what makes the order
 * observable to the comparator, including trailing unset fields and the boundaries of extensible
 * groups: an extensible wrapper is a field whose value is a list, and that list stays ordered too.
 *
 * `IdfObject.toJSON()` yields the object's set fields in IDF positional order, which is the
 * JavaScript counterpart of the Python object's field dictionary. It is turned into an array of
 * `{name, value}` records here, because handing the comparator an object would put the sequence
 * under rule 3, which does not compare key order.
 *
 * An empty collection contributes nothing to the model and is skipped: `IdfDocument.all()`
 * creates one lazily on any access, so an incidental lookup on one side would otherwise read as a
 * document difference.
 *
 * @param {*} document
 * @returns {Record<string, unknown>}
 */
export function documentSnapshot(document) {
  /** @type {Record<string, unknown>} */
  const snapshot = {};
  for (const objType of document.types()) {
    const collection = document.all(objType);
    if (collection.size === 0) {
      continue;
    }
    snapshot[objType] = [...collection].map((obj) => ({
      name: obj.name,
      fields: Object.entries(obj.toJSON()).map(([name, value]) => ({
        name,
        value: jsonable(value),
      })),
    }));
  }
  return snapshot;
}

/**
 * Pass a parsed field value through unchanged, or fail loudly on a type JSON cannot carry.
 *
 * Never coerce. A silent `String(value)` here would turn a library returning the wrong type into a
 * passing case, which is the one outcome the suite exists to prevent. `undefined` is rejected too:
 * JSON has no such value, and `JSON.stringify` would drop the key rather than report it.
 *
 * @param {*} value
 * @returns {*}
 */
function jsonable(value) {
  if (value === null) {
    return value;
  }
  const type = typeof value;
  if (type === 'boolean' || type === 'number' || type === 'string') {
    return value;
  }
  if (Array.isArray(value)) {
    return value.map(jsonable);
  }
  if (isPlainObject(value)) {
    /** @type {Record<string, unknown>} */
    const out = {};
    for (const [key, item] of Object.entries(value)) {
      out[key] = jsonable(item);
    }
    return out;
  }
  throw new RunnerError(
    `field value of type ${quote(typeNameOf(value))} is not a JSON value: ${String(value)}`
  );
}

/**
 * Write the document as IDF, read it back, and snapshot the result.
 *
 * Rule 6: the IDF is written and read as latin-1, never as UTF-8 and never at a platform default.
 * `writeIdf` has no lossless mode to disable, unlike the Python writer: it always re-emits the
 * document from its values rather than echoing the source text, which is what makes this assertion
 * a real round trip rather than a trivially true one.
 *
 * @param {LibraryUnderTest} library
 * @param {*} document
 * @returns {Promise<Record<string, unknown>>}
 */
function writerOptionsFor(options) {
  // Mapped control by control rather than passed through, because the two writers spell the same
  // thing differently and a pass-through would silently accept a name only one of them has. A
  // control the case does not set is not passed at all, so the writer's own default applies.
  if (options === null || options.isDefault) return {};

  /** @type {Record<string, unknown>} */
  const mapped = {};
  if (options.compressed !== null) mapped['compressed'] = options.compressed;
  if (options.comments !== null) mapped['comments'] = options.comments;
  // The corpus counts indent in spaces; this writer takes the string itself.
  if (options.indent !== null) mapped['indent'] = ' '.repeat(options.indent);
  if (options.commentColumn !== null) mapped['commentColumn'] = options.commentColumn;
  if (options.ordering !== null) mapped['ordering'] = options.ordering;
  return mapped;
}

async function roundTripSnapshot(library, document, writerOptions = null) {
  const text = library.core.writeIdf(document, writerOptionsFor(writerOptions));
  const scratch = mkdtempSync(join(tmpdir(), 'idfkit-conformance-'));
  try {
    const path = join(scratch, 'round-trip.idf');
    writeFileSync(path, Buffer.from(text, IDF_ENCODING));
    const roundTripped = readFileSync(path).toString(IDF_ENCODING);
    const schema = await library.node.schemaFor(library.core.getIdfVersion(roundTripped));
    return documentSnapshot(library.core.parseIdf(roundTripped, schema).document);
  } finally {
    rmSync(scratch, { recursive: true, force: true });
  }
}

/**
 * Run every assertion the case declares, in `ASSERTION_ORDER`, and never throw.
 *
 * @param {LibraryUnderTest} library
 * @param {CaseJob} job
 * @param {number} limit
 * @returns {Promise<CaseOutcome>}
 */
export async function runCase(library, job, limit) {
  const started = performance.now();
  const parse = await parseInput(library, job);
  /** @type {AssertionOutcome[]} */
  const outcomes = [];
  for (const assertion of job.assertions) {
    try {
      outcomes.push(await runAssertion(library, job, assertion, parse, limit));
    } catch (error) {
      // A runner crash is reported against the case, not propagated.
      outcomes.push(
        errored(
          job.caseId,
          assertion,
          `the runner could not evaluate this assertion: ${describeError(error)}`
        )
      );
    }
  }
  return new CaseOutcome({
    caseId: job.caseId,
    seconds: (performance.now() - started) / 1000,
    outcomes,
  });
}

/**
 * Dispatch one assertion. Assertion 4 is accepted and skipped.
 *
 * @param {LibraryUnderTest} library
 * @param {CaseJob} job
 * @param {Assertion} assertion
 * @param {Parse} parse
 * @param {number} limit
 * @returns {Promise<AssertionOutcome>}
 */
async function runAssertion(library, job, assertion, parse, limit) {
  if (assertion === Assertion.DIAGNOSTICS) {
    return await assertDiagnostics(library, job, parse, limit);
  }
  if (assertion === Assertion.PARSE_OUTCOME) {
    return assertParseOutcome(job, parse, limit);
  }
  if (parse.document === null) {
    return noDocument(job, assertion, parse);
  }
  if (assertion === Assertion.EPJSON) {
    return assertEpjson(library, job, parse.document, limit);
  }
  if (assertion === Assertion.ROUND_TRIP) {
    return assertRoundTrip(library, job, parse.document, limit);
  }
  if (assertion === Assertion.VALIDATION) {
    return assertValidation(library, job, parse.document, limit);
  }
  if (assertion === Assertion.INTROSPECTION) {
    return assertIntrospection(library, job, parse.document, limit);
  }
  if (assertion === Assertion.DOCS_URL) {
    return assertDocsUrl(library, job, parse.document, limit);
  }
  if (assertion === Assertion.TYPE_LOOKUP) {
    return assertTypeLookup(library, job, parse.document, limit);
  }
  // A ninth assertion added to model.mjs without a runner change lands here. Saying so beats
  // falling through to whichever branch happened to be last.
  return errored(
    job.caseId,
    assertion,
    `this runner has no implementation for ${quote(assertion)}`
  );
}

/**
 * An assertion the runner could not evaluate. Always blocking, never allowlistable.
 *
 * @param {string} caseId
 * @param {Assertion} assertion
 * @param {string} reason
 * @returns {AssertionOutcome}
 */
function errored(caseId, assertion, reason) {
  return new AssertionOutcome({
    caseId,
    assertion,
    status: Status.ERRORED,
    reason,
    firstDifference: reason,
  });
}

/**
 * The first line of every report block, matching `compare.AssertionReport`'s own head.
 *
 * @param {string} caseId
 * @param {Assertion} assertion
 * @param {string} reason
 * @returns {string}
 */
function head(caseId, assertion, reason) {
  return `${caseId}: ${LIBRARY}: ${assertion}: ${reason}`;
}

/**
 * A report block for an outcome no comparison produced: a head, then indented detail.
 *
 * @param {string} caseId
 * @param {Assertion} assertion
 * @param {string} reason
 * @param {readonly string[]} [detail]
 * @returns {readonly string[]}
 */
function block(caseId, assertion, reason, detail = []) {
  return Object.freeze([head(caseId, assertion, reason), ...detail.map((line) => `  ${line}`)]);
}

/**
 * What to say about assertion 2 or 3 when the input never parsed.
 *
 * A case that declares failure and fails has nothing left to compare, and saying so is honest. A
 * case that declares success and failed has genuinely not satisfied the assertion, so it fails here
 * as well as on assertion 1: the register keys on case, library and assertion, so each is accepted
 * or blocked on its own terms.
 *
 * @param {CaseJob} job
 * @param {Assertion} assertion
 * @param {Parse} parse
 * @returns {AssertionOutcome}
 */
function noDocument(job, assertion, parse) {
  if (job.expectedParseOutcome === ParseOutcome.FAILURE) {
    return new AssertionOutcome({
      caseId: job.caseId,
      assertion,
      status: Status.SKIPPED,
      reason:
        `the input is declared to fail parsing, so there is no document to compare. ` +
        `Drop this assertion from ${job.caseId}/case.toml if it was not meant to apply`,
    });
  }
  const reason = 'the input did not parse, so the library produced nothing to compare';
  return new AssertionOutcome({
    caseId: job.caseId,
    assertion,
    status: Status.FAILED,
    reason,
    lines: block(job.caseId, assertion, reason, [parse.error]),
    firstDifference: parse.error,
  });
}

/**
 * Assertion 1: the observed outcome against the declared one.
 *
 * @param {CaseJob} job
 * @param {Parse} parse
 * @param {number} limit
 * @returns {AssertionOutcome}
 */
function assertParseOutcome(job, parse, limit) {
  const comparison = compareOutcome(parse.outcome, job.expectedParseOutcome);
  const extra = parse.error ? [`parse error: ${parse.error}`] : [];
  return fromComparison(job.caseId, Assertion.PARSE_OUTCOME, comparison, limit, extra);
}

/**
 * Assertion 2: canonical epJSON against the committed expectation.
 *
 * `truth = convention` forbids an expectation file, so a convention case declaring this assertion
 * has nothing to be compared against. That is a corpus fault, reported as an error rather than
 * passed over, because an assertion nobody can evaluate reads as a green tick.
 *
 * @param {LibraryUnderTest} library
 * @param {CaseJob} job
 * @param {*} document
 * @param {number} limit
 * @returns {AssertionOutcome}
 */
function assertEpjson(library, job, document, limit) {
  if (job.truth !== Truth.ORACLE) {
    return errored(
      job.caseId,
      Assertion.EPJSON,
      `the case sits in the convention section, which forbids ${EXPECTED_EPJSON}, so there is no ` +
        `expectation to compare. Move the case to the oracle section or drop the 'epjson' assertion`
    );
  }
  if (!isFile(job.expectedEpjsonPath)) {
    return errored(
      job.caseId,
      Assertion.EPJSON,
      `${EXPECTED_EPJSON} is missing while truth = oracle. Generate it with ` +
        `tools/regenerate.sh and commit it`
    );
  }

  const produced = JSON.parse(library.core.writeEpJson(document));
  const expected = JSON.parse(readFileSync(job.expectedEpjsonPath, 'utf8'));
  return fromComparison(job.caseId, Assertion.EPJSON, compareEpjson(produced, expected), limit);
}

/**
 * Assertion 3: re-parsing the library's own IDF output against the original document.
 *
 * @param {LibraryUnderTest} library
 * @param {CaseJob} job
 * @param {*} document
 * @param {number} limit
 * @returns {Promise<AssertionOutcome>}
 */
async function assertRoundTrip(library, job, document, limit) {
  const original = documentSnapshot(document);
  const reparsed = await roundTripSnapshot(library, document, job.writerOptions);
  return fromComparison(
    job.caseId,
    Assertion.ROUND_TRIP,
    compareDocuments(reparsed, original),
    limit
  );
}

/**
 * Read the expectation a Tier 1 assertion compares against, or `undefined` when the case has none.
 *
 * `JSON.parse` is left to throw on a file that is not JSON, exactly as assertion 2 leaves it: a
 * corrupt expectation is reported against the case by `runCase` rather than swallowed here, and a
 * runner that guessed at a half-parsed expectation would compare against something nobody wrote.
 *
 * @param {CaseJob} job
 * @param {string} file
 * @returns {*}
 */
function expectation(job, file) {
  const path = join(job.caseDir, file);
  return isFile(path) ? JSON.parse(readFileSync(path, 'utf8')) : undefined;
}

/**
 * The error for an expectation file a case declares an assertion for and does not carry.
 *
 * `model.mjs` already refuses to load such a case, so reaching this means the corpus was edited
 * underneath a loaded manifest. It is an error rather than a skip, because an assertion nobody can
 * evaluate reads as a green tick.
 *
 * @param {string} caseId
 * @param {Assertion} assertion
 * @param {string} file
 * @returns {AssertionOutcome}
 */
function missingExpectation(caseId, assertion, file) {
  return errored(
    caseId,
    assertion,
    `${file} is missing while the case declares the ${quote(assertion)} assertion. Seed it with ` +
      `tools/seed_tier1.py, read the draft against the rule that governs it, and commit it`
  );
}

/**
 * An absent value written as JSON `null`, which is the corpus's spelling for one.
 *
 * Every key of a Tier 1 record is always present, which is what keeps rule 3's "absent is not
 * `null`" meaningful here: a key missing from a description is a library that lost a member of its
 * own type, and that is a difference rather than an absent value.
 *
 * @param {*} value
 * @returns {*}
 */
function orNull(value) {
  return value === undefined ? null : value;
}

/**
 * The object types the parsed document holds, sorted.
 *
 * Assertions 6 and 7 describe what the document contains rather than what the schema defines, so
 * the case input decides the coverage and a maintainer reading the expectation sees only the types
 * that case put there. The sort makes the file diffable and means nothing else: rule 3 does not
 * compare object key order.
 *
 * @param {*} document
 * @returns {readonly string[]}
 */
function objectTypesOf(document) {
  return [...document.types()].sort();
}

/**
 * One finding in the corpus vocabulary: what kind, where, and in which type.
 *
 * `message` is never included, and that is the whole design of this assertion. Wording is a
 * presentation choice each library should stay free to improve; pinning it would turn every
 * improvement into a conformance failure, and the suite would be edited to match rather than
 * believed. `code` carries the meaning instead, from a table both libraries derive mechanically
 * from Python's exception hierarchy.
 *
 * @param {*} diagnostic
 * @returns {Record<string, unknown>}
 */
function diagnosticEntry(diagnostic) {
  return {
    code: diagnostic?.code ?? null,
    line: diagnostic?.line ?? null,
    typeName: diagnostic?.typeName ?? null,
  };
}

/**
 * Assertion 4: parse findings against `expected.diag.json`, as an unordered multiset.
 *
 * Two paths, one expectation. A read that FAILED reports the findings its error carried; a read
 * that SUCCEEDED reports the recoverable findings the returning path hands back. Which path a case
 * exercises is a property of its input, not something it declares, so the assertion reads whichever
 * one applies and compares the same shape either way.
 *
 * Unordered, because the order findings are noticed in is an implementation detail of the scan and
 * not something either library promises. A multiset rather than a set, because two skips of the
 * same type at different lines are two findings, and collapsing them would hide exactly the bug
 * this assertion was built to catch.
 *
 * @param {LibraryUnderTest} library
 * @param {CaseJob} job
 * @param {Parse} parse
 * @param {number} limit
 * @returns {Promise<AssertionOutcome>}
 */
async function assertDiagnostics(library, job, parse, limit) {
  const expected = expectation(job, EXPECTED_DIAGNOSTICS);
  if (expected === undefined) {
    return missingExpectation(job.caseId, Assertion.DIAGNOSTICS, EXPECTED_DIAGNOSTICS);
  }

  /** @type {*[]} */
  const differences = [];

  // `diagnostics`: the findings from whichever path this input actually takes. A read that FAILED
  // reports what its error carried; a read that SUCCEEDED reports what the returning path gave.
  if (expected.diagnostics !== undefined) {
    const found =
      parse.outcome === ParseOutcome.FAILURE
        ? parse.diagnostics.map(diagnosticEntry)
        : await recoverableFindings(library, job);
    differences.push(
      ...compareUnordered(found, expected.diagnostics, { path: '/diagnostics' }).differences
    );
  }

  // `recoverable`: the returning path, asked for explicitly.
  //
  // Needed because an input whose strict parse fails never reaches the returning path above, and
  // the returning path is half of what this feature closed. A case that wants to compare it says
  // so, and gets it whatever strict mode did.
  if (expected.recoverable !== undefined) {
    differences.push(
      ...compareUnordered(await recoverableFindings(library, job), expected.recoverable, {
        path: '/recoverable',
      }).differences
    );
  }

  return fromComparison(job.caseId, Assertion.DIAGNOSTICS, new Comparison(differences), limit);
}

/**
 * The findings the returning path hands back for this input, in the corpus vocabulary.
 *
 * @param {LibraryUnderTest} library
 * @param {CaseJob} job
 * @returns {Promise<Record<string, unknown>[]>}
 */
async function recoverableFindings(library, job) {
  if (job.inputFile === InputFile.IDF) {
    const text = readFileSync(job.inputPath).toString(IDF_ENCODING);
    const schema = await library.node.schemaFor(library.core.getIdfVersion(text));
    return library.core.parseIdf(text, schema, { strict: false }).diagnostics.map(diagnosticEntry);
  }
  const text = readFileSync(job.inputPath, 'utf8');
  const schema = await library.node.schemaFor(library.core.getEpJsonVersion(text));
  return library.core.parseEpJson(text, schema, { strict: false }).diagnostics.map(diagnosticEntry);
}

/**
 * Assertion 5: the findings the library reports against `expected.validation.json`.
 *
 * The three severity arrays are concatenated because severity is one of the members a finding is
 * compared on, so keeping the split would compare the split instead of the findings. The result is
 * an unordered multiset under rule 7: neither library promises an order within one array, and a
 * library that visited object types in a different order would otherwise fail every validation
 * case for a property nobody claims.
 *
 * `message` is never serialized, for the reason assertion 4 gives: wording is a presentation choice
 * each library is free to improve. The two already differ where both are right, Python rendering
 * one finding as `Value -5.0 is below minimum 0.0` and JavaScript as `Value -5 is below minimum 0`,
 * because one runtime distinguishes int from float and the other has no way to.
 *
 * The keys are the corpus's snake_case rather than this library's camelCase. The vocabulary belongs
 * to neither library, and a file written in either spelling would read as a transcript of that one.
 *
 * @param {LibraryUnderTest} library
 * @param {CaseJob} job
 * @param {*} document
 * @param {number} limit
 * @returns {AssertionOutcome}
 */
function assertValidation(library, job, document, limit) {
  const expected = expectation(job, EXPECTED_VALIDATION);
  if (expected === undefined) {
    return missingExpectation(job.caseId, Assertion.VALIDATION, EXPECTED_VALIDATION);
  }

  const result = library.core.validateDocument(document);
  const findings = [...result.errors, ...result.warnings, ...result.info].map((finding) => ({
    object_type: finding.objType,
    object_name: finding.objName,
    field: orNull(finding.field),
    code: finding.code,
    severity: finding.severity,
  }));
  return fromComparison(
    job.caseId,
    Assertion.VALIDATION,
    compareUnordered(findings, expected.findings, { path: '/findings' }),
    limit
  );
}

/**
 * Assertion 6: the library's description of every type in the document against
 * `expected.introspection.json`.
 *
 * Field entries stay in schema order, in an array, because that order is the positional order of
 * the type and rule 7 is what makes an array compared index by index. `field_type` for an `anyOf`
 * field is the pipe-delimited union in declaration order, and `exclusive_minimum` and
 * `exclusive_maximum` carry whatever the schema declares, a boolean on 8.9.0 through 9.5.0 and a
 * number from 9.6.0. Both are the library's business rather than this runner's; the runner renames
 * and never reshapes.
 *
 * @param {LibraryUnderTest} library
 * @param {CaseJob} job
 * @param {*} document
 * @param {number} limit
 * @returns {AssertionOutcome}
 */
function assertIntrospection(library, job, document, limit) {
  const expected = expectation(job, EXPECTED_INTROSPECTION);
  if (expected === undefined) {
    return missingExpectation(job.caseId, Assertion.INTROSPECTION, EXPECTED_INTROSPECTION);
  }

  /** @type {Record<string, unknown>} */
  const objectTypes = {};
  for (const objType of objectTypesOf(document)) {
    const described = library.core.describeObjectType(document.schema, objType, library.prose);
    objectTypes[objType] = {
      obj_type: described.objType,
      memo: orNull(described.memo),
      has_name: described.hasName,
      is_extensible: described.isExtensible,
      extensible_size: orNull(described.extensibleSize),
      required_fields: [...described.requiredFields],
      fields: described.fields.map(fieldEntry),
    };
  }
  return fromComparison(
    job.caseId,
    Assertion.INTROSPECTION,
    compareValues({ object_types: objectTypes }, expected),
    limit
  );
}

/**
 * One field of a type description, in the corpus vocabulary.
 *
 * Written out key by key rather than derived from a name list, so that a member this library
 * renames or drops is a compile-time-visible edit here instead of a silently absent key.
 *
 * @param {*} field
 * @returns {Record<string, unknown>}
 */
function fieldEntry(field) {
  return {
    name: field.name,
    field_type: orNull(field.fieldType),
    required: field.required,
    default: orNull(field.default),
    units: orNull(field.units),
    enum_values: orNull(field.enumValues),
    minimum: orNull(field.minimum),
    maximum: orNull(field.maximum),
    exclusive_minimum: orNull(field.exclusiveMinimum),
    exclusive_maximum: orNull(field.exclusiveMaximum),
    note: orNull(field.note),
    is_reference: field.isReference,
    object_list: orNull(field.objectList),
  };
}

/**
 * Assertion 7: the documentation address the library builds for every type in the document against
 * `expected.docs-url.json`.
 *
 * The version segment comes from the document, never from a constant, so a library that hardcoded a
 * release fails this the next time one ships. The schema is passed because without it a type
 * outside the bundled mapping resolves to nothing here, and the schema-group fallback that covers
 * those types is half of what this assertion exists to compare.
 *
 * `null` where the library builds no address, which is a value the expectation records and not an
 * absence: rule 3 keeps the two apart, and a type losing its address is exactly the 404 in front of
 * a reader that this assertion is here to catch.
 *
 * @param {LibraryUnderTest} library
 * @param {CaseJob} job
 * @param {*} document
 * @param {number} limit
 * @returns {AssertionOutcome}
 */
function assertDocsUrl(library, job, document, limit) {
  const expected = expectation(job, EXPECTED_DOCS_URL);
  if (expected === undefined) {
    return missingExpectation(job.caseId, Assertion.DOCS_URL, EXPECTED_DOCS_URL);
  }

  /** @type {Record<string, unknown>} */
  const objectTypes = {};
  for (const objType of objectTypesOf(document)) {
    const address = library.core.docsUrlForObject(objType, document.version, document.schema);
    objectTypes[objType] =
      address === undefined
        ? null
        : {
            url: address.url,
            doc_set: address.docSet,
            version: address.version,
            label: address.label,
          };
  }
  return fromComparison(
    job.caseId,
    Assertion.DOCS_URL,
    compareValues({ object_types: objectTypes }, expected),
    limit
  );
}

/**
 * Assertion 8: what the library returns when a caller names an object type, and what that read
 * left behind.
 *
 * The queried names are the keys of the expectation's `lookups` object, so the case file alone
 * decides which spellings are exercised and the runner never invents one. Each query records the
 * object names the collection accessor returned, in order, and what the membership test said, so
 * that a library whose `has` disagrees with its `all` fails here rather than agreeing by halves.
 *
 * `object_types_after` is taken after every query has run, and it is what makes a read that
 * mutates the document a finding. Both libraries used to file an empty collection under whatever
 * name was asked for, so probing three misspellings left three keys behind, visible to every later
 * iteration over the document and to `toJSON`. It is sorted because the order of a document's type
 * list is already compared by assertions 2 and 3; what this assertion adds is the *set*.
 *
 * @param {LibraryUnderTest} library
 * @param {CaseJob} job
 * @param {*} document
 * @param {number} limit
 * @returns {AssertionOutcome}
 */
function assertTypeLookup(library, job, document, limit) {
  const expected = expectation(job, EXPECTED_TYPE_LOOKUP);
  if (expected === undefined) {
    return missingExpectation(job.caseId, Assertion.TYPE_LOOKUP, EXPECTED_TYPE_LOOKUP);
  }

  /** @type {Record<string, unknown>} */
  const lookups = {};
  for (const written of Object.keys(expected.lookups ?? {})) {
    const collection = document.all(written);
    lookups[written] = {
      names: [...collection].map((obj) => obj.name),
      present: document.has(written),
    };
  }
  return fromComparison(
    job.caseId,
    Assertion.TYPE_LOOKUP,
    compareValues({ lookups, object_types_after: objectTypesOf(document) }, expected),
    limit
  );
}

/**
 * Turn a comparison into an outcome, rendered eagerly through `compare.mjs`'s own layout.
 *
 * `compare.AssertionReport` already prints the exit contract: the case id, the library, the
 * assertion, the differing value, and the path to both sides. The runner takes that block verbatim
 * rather than composing a second one, so the two can never drift apart.
 *
 * @param {string} caseId
 * @param {Assertion} assertion
 * @param {import('./compare.mjs').Comparison} comparison
 * @param {number} limit
 * @param {readonly string[]} [extra]
 * @returns {AssertionOutcome}
 */
function fromComparison(caseId, assertion, comparison, limit, extra = []) {
  const report = new AssertionReport({ caseId, library: LIBRARY, assertion, comparison });
  if (report.passed) {
    return new AssertionOutcome({ caseId, assertion, status: Status.PASSED });
  }
  const first = comparison.first;
  return new AssertionOutcome({
    caseId,
    assertion,
    status: Status.FAILED,
    reason: `${comparison.count} difference(s), left is ${LIBRARY}`,
    lines: [...report.render({ limit: limit || null }), ...extra.map((line) => `  ${line}`)],
    firstDifference: first === null ? '' : first.render(),
  });
}

// ---------------------------------------------------------------------------
// Execution, serial or pooled
// ---------------------------------------------------------------------------

/**
 * How many cases to have in flight at once. Zero means decide from the corpus size.
 *
 * `run.py` spends this budget on worker processes. JavaScript runs the corpus on one thread, so the
 * budget buys overlap on the file reads and the schema loads rather than parallel parsing. The flag
 * exists on both runners because the command-line surface is shared; the ceiling is the same so
 * neither runner is tuned against a number the other cannot honour.
 *
 * @param {number} requested
 * @param {number} caseCount
 * @returns {number}
 */
function workerCount(requested, caseCount) {
  if (requested > 0) {
    return Math.min(requested, Math.max(caseCount, 1));
  }
  if (caseCount < POOL_THRESHOLD) {
    return 1;
  }
  return Math.max(1, Math.min(availableParallelism(), MAX_POOL_WORKERS, caseCount));
}

/**
 * Run every job, reported in manifest order whatever order they finish in.
 *
 * @param {readonly CaseJob[]} jobs
 * @param {LibraryUnderTest} library
 * @param {number} limit
 * @param {number} jobsRequested
 * @returns {Promise<CaseOutcome[]>}
 */
export async function execute(jobs, library, limit, jobsRequested) {
  const workers = workerCount(jobsRequested, jobs.length);
  if (workers <= 1 || jobs.length <= 1) {
    /** @type {CaseOutcome[]} */
    const serial = [];
    for (const job of jobs) {
      serial.push(await runCase(library, job, limit));
    }
    return serial;
  }

  /** @type {CaseOutcome[]} */
  const results = new Array(jobs.length);
  let next = 0;
  const worker = async () => {
    while (next < jobs.length) {
      const index = next;
      next += 1;
      results[index] = await runCase(library, jobs[index], limit);
    }
  };
  await Promise.all(Array.from({ length: workers }, () => worker()));
  return results;
}

// ---------------------------------------------------------------------------
// The exception register: outstanding, stale, and unexercisable entries
// ---------------------------------------------------------------------------

/**
 * Sort every failure and every register entry into the buckets the exit contract distinguishes.
 *
 * An outstanding exception is reported and does not block. A stale one blocks. An entry naming an
 * assertion its case never declares can never be exercised, so it blocks too: it looks like
 * coverage and is not.
 *
 * @param {RunReport} report
 * @param {readonly import('./model.mjs').Divergence[]} divergences
 * @param {Iterable<string>} selected
 */
export function reconcile(report, divergences, selected) {
  const selectedIds = new Set(selected);
  /** @type {Map<string, CaseOutcome>} */
  const byCase = new Map(report.cases.map((record) => [record.caseId, record]));
  /** @type {Set<string>} */
  const accepted = new Set();

  for (const divergence of divergences) {
    if (!selectedIds.has(divergence.case)) {
      report.unselectedExceptions += 1;
      continue;
    }
    const record = byCase.get(divergence.case);
    const outcome = record === undefined ? null : record.outcomeFor(divergence.assertion);
    if (outcome === null) {
      report.unexercisable.push([
        divergence,
        `case ${quote(divergence.case)} does not declare the ${quote(divergence.assertion)} ` +
          `assertion, so this entry can never be exercised`,
      ]);
      continue;
    }
    if (outcome.status === Status.SKIPPED) {
      report.deferredExceptions.push(divergence);
      continue;
    }
    if (outcome.status === Status.PASSED) {
      report.stale.push(divergence);
      continue;
    }
    if (outcome.status === Status.FAILED) {
      accepted.add(acceptanceKey(divergence.case, divergence.assertion));
      report.outstanding.push([divergence, outcome]);
    }
  }

  for (const record of report.cases) {
    for (const outcome of record.outcomes) {
      if (outcome.blocks && !accepted.has(acceptanceKey(outcome.caseId, outcome.assertion))) {
        report.blocking.push(outcome);
      }
    }
  }
}

/**
 * The register key: one accepted exception per case and assertion, for this runner's one library.
 *
 * @param {string} caseId
 * @param {Assertion} assertion
 * @returns {string}
 */
function acceptanceKey(caseId, assertion) {
  return `${caseId} ${assertion}`;
}

// ---------------------------------------------------------------------------
// Printing
// ---------------------------------------------------------------------------

/**
 * @param {string} title
 * @param {Iterable<string>} body
 */
function printBlock(title, body) {
  const lines = [...body];
  if (lines.length === 0) {
    return;
  }
  console.log();
  console.log(title);
  for (const line of lines) {
    console.log(`  ${line}`);
  }
}

/**
 * Print the whole run, in the order a reader needs it: what ran, what broke, what is accepted.
 *
 * @param {RunReport} report
 */
export function printReport(report) {
  console.log('idfkit conformance runner');
  console.log(`  corpus     ${report.corpusRoot}`);
  console.log(`  level      ${report.level.describe()}`);
  console.log(`  library    ${report.library.describe()}`);
  console.log(`  register   ${report.registerNote}`);
  console.log(`  selection  ${report.selected} of ${report.total} case(s)`);

  if (report.total === 0) {
    console.log();
    console.log(
      `no cases in the corpus: ${join(report.corpusRoot, CASES_DIR)} holds none yet, so there is ` +
        `nothing to check`
    );
  } else if (report.selected === 0) {
    console.log();
    console.log('no case matched the selection, so nothing ran');
  }

  printBlock(
    'failures not in known-divergence.toml (exit 1)',
    report.blocking
      .filter((outcome) => outcome.status === Status.FAILED)
      .flatMap((outcome) => [...outcome.render()])
  );
  // An errored assertion is a corpus or environment problem, not a library disagreement, so it is
  // never allowlistable and is kept out of the failure block where a reader would look for one.
  printBlock(
    'assertions the runner could not evaluate (exit 1)',
    report.blocking
      .filter((outcome) => outcome.status === Status.ERRORED)
      .flatMap((outcome) => [...outcome.render()])
  );
  printBlock(
    'stale exceptions: these cases now pass, so remove their entries (exit 1)',
    report.stale.flatMap((divergence) => [
      `${divergence.case}: ${divergence.library}: ${divergence.assertion} now passes`,
      `    remove this entry from ${DIVERGENCE_FILE}. A stale exception is removed by the change ` +
        `that fixes the bug`,
      `    issue: ${divergence.issue}`,
    ])
  );
  printBlock(
    'exceptions that can never be exercised, so remove them (exit 1)',
    report.unexercisable.flatMap(([divergence, why]) => [
      `${divergence.case}: ${divergence.assertion}: ${why}`,
      `    issue: ${divergence.issue}`,
    ])
  );
  // T038: outstanding exceptions are normal output, never behind a flag. A silent allowlist is how
  // a temporary exception becomes permanent.
  printBlock(
    'outstanding exceptions: accepted, tracked, still failing',
    report.outstanding.flatMap(([divergence, outcome]) => [
      `${divergence.case}: ${divergence.library}: ${divergence.assertion}`,
      `    issue:    ${divergence.issue}`,
      `    observed: ${divergence.observed}`,
      `    expected: ${divergence.expected}`,
      ...(outcome.firstDifference ? [`    now:      ${outcome.firstDifference}`] : []),
    ])
  );
  printBlock(
    'exceptions not evaluated: their assertion is skipped',
    report.deferredExceptions.map(
      (divergence) => `${divergence.case}: ${divergence.assertion}: ${divergence.issue}`
    )
  );
  printBlock(
    'skipped assertions',
    report.cases.flatMap((record) =>
      record.outcomes
        .filter((outcome) => outcome.status === Status.SKIPPED)
        .map((outcome) => `${outcome.caseId}: ${outcome.assertion}: ${outcome.reason}`)
    )
  );

  if (report.unselectedExceptions) {
    console.log();
    console.log(
      `${report.unselectedExceptions} exception(s) were not evaluated because --case or --tag ` +
        `excluded their case. Run without a filter before trusting a green result`
    );
  }

  const counts = report.counts();
  console.log();
  console.log(
    `${report.selected} case(s), ${report.assertionCount} assertion(s): ` +
      `${counts[Status.PASSED]} passed, ${counts[Status.FAILED]} failed, ` +
      `${counts[Status.ERRORED]} errored, ${counts[Status.SKIPPED]} skipped; ` +
      `${report.outstanding.length} outstanding exception(s); ` +
      `level ${report.level.value ?? 'unpinned'}; ${report.seconds.toFixed(2)}s`
  );
  console.log(report.green ? 'PASS' : 'FAIL');
}

// ---------------------------------------------------------------------------
// Entry point
// ---------------------------------------------------------------------------

/** The hazard taxonomy, wrapped to the help text's option column. */
const TAG_LIST = wrap(Object.values(Tag).join(', '), 70).join('\n' + ' '.repeat(29));

const USAGE = `usage: run.mjs --library PATH [--case ID] [--tag TAG] [--corpus PATH]
                  [--level conformance-YYYY.N] [--jobs N] [--max-differences N]

Run the idfkit conformance corpus against a checkout of the JavaScript library.

options:
  --library PATH             path to the idfkit-js checkout to test, for example ../idfkit-js
  --case ID                  run only this case. Repeatable
  --tag TAG                  run only cases carrying this hazard tag. Repeatable
                             (${TAG_LIST})
  --corpus PATH              root of the conformance corpus (default: the repository this runner
                             lives in)
  --level conformance-YYYY.N report against this level instead of the repository's git tag
  --jobs N                   cases in flight at once. 0 decides from the corpus size (default)
  --max-differences N        differences printed per failing assertion, 0 for all (default: \
${DEFAULT_DIFFERENCE_LIMIT}).
                             The total is always printed
  -h, --help                 show this message and exit

Exit 0 when every case passes or every failure is listed in known-divergence.toml, 1 for a blocking
failure, a stale exception, or a corpus fault, and 2 when the run could not start.`;

/** The CLI. `--library` takes a path, never a language word: the runner file fixes the language. */
const OPTIONS = Object.freeze({
  library: { type: 'string' },
  case: { type: 'string', multiple: true, default: [] },
  tag: { type: 'string', multiple: true, default: [] },
  corpus: { type: 'string' },
  level: { type: 'string' },
  jobs: { type: 'string' },
  'max-differences': { type: 'string' },
  help: { type: 'boolean', short: 'h', default: false },
});

/**
 * Parse the command line, rejecting anything `run.py`'s argparse would reject.
 *
 * @param {readonly string[]} argv
 * @returns {{ library: string, case: string[], tag: string[], corpus: string,
 *             level: string | undefined, jobs: number, maxDifferences: number }}
 */
function parseCommandLine(argv) {
  /** @type {{ values: Record<string, *>, positionals: string[] }} */
  let parsed;
  try {
    parsed = parseArgs({ args: [...argv], options: OPTIONS, allowPositionals: true });
  } catch (error) {
    throw new RunnerError(`${describeMessage(error)}\n${USAGE}`);
  }
  if (parsed.positionals.length > 0) {
    throw new RunnerError(
      `unrecognized argument(s): ${parsed.positionals.map(quote).join(', ')}\n${USAGE}`
    );
  }
  if (parsed.values.help) {
    console.log(USAGE);
    return { help: true };
  }
  if (parsed.values.library === undefined) {
    throw new RunnerError(`the following arguments are required: --library\n${USAGE}`);
  }
  return {
    library: parsed.values.library,
    case: parsed.values.case ?? [],
    tag: parsed.values.tag ?? [],
    corpus: parsed.values.corpus ?? REPO_ROOT,
    level: parsed.values.level,
    jobs: integerFlag(parsed.values.jobs, '--jobs', 0),
    maxDifferences: integerFlag(
      parsed.values['max-differences'],
      '--max-differences',
      DEFAULT_DIFFERENCE_LIMIT
    ),
  };
}

/**
 * @param {string | undefined} raw
 * @param {string} flag
 * @param {number} fallback
 * @returns {number}
 */
function integerFlag(raw, flag, fallback) {
  if (raw === undefined) {
    return fallback;
  }
  const value = Number(raw);
  if (!Number.isInteger(value)) {
    throw new RunnerError(`${flag} must be an integer, got ${quote(raw)}`);
  }
  return value;
}

/**
 * @param {readonly string[]} values
 * @returns {readonly Tag[]}
 */
function parseTags(values) {
  const known = new Set(Object.values(Tag));
  /** @type {Tag[]} */
  const tags = [];
  for (const value of values) {
    if (!known.has(value)) {
      throw new RunnerError(
        `--tag ${quote(value)} is not in the taxonomy: ${Object.values(Tag).join(', ')}`
      );
    }
    tags.push(value);
  }
  return Object.freeze(tags);
}

/**
 * Run the corpus and return the exit code. Never throws out of a corpus or library problem.
 *
 * @param {readonly string[]} [argv]
 * @returns {Promise<number>}
 */
export async function main(argv = process.argv.slice(2)) {
  const started = performance.now();

  /** @type {*} */
  let args;
  /** @type {readonly Tag[]} */
  let tags;
  /** @type {LibraryUnderTest} */
  let library;
  try {
    args = parseCommandLine(argv);
    if (args.help) {
      return EXIT_OK;
    }
    tags = parseTags(args.tag);
    library = await importLibrary(args.library);
  } catch (error) {
    if (error instanceof RunnerError) {
      console.error(`run.mjs: ${error.message}`);
      return EXIT_UNUSABLE;
    }
    throw error;
  }

  const corpusRoot = resolve(expandUser(args.corpus));

  // Preflight, so the exit contract's dedicated row for a missing expectation prints the case id
  // rather than a loader stack trace.
  /** @type {import('./model.mjs').Manifest} */
  let manifest;
  try {
    manifest = loadManifest(join(corpusRoot, MANIFEST_FILE));
  } catch (error) {
    if (error instanceof CorpusError) {
      console.error(`run.mjs: ${error.message}`);
      return EXIT_FAILED;
    }
    throw error;
  }

  const missing = [...manifest.entries()]
    .filter(
      (entry) =>
        entry.truth === Truth.ORACLE &&
        !isFile(join(corpusRoot, CASES_DIR, entry.id, EXPECTED_EPJSON))
    )
    .map((entry) => entry.id);
  if (missing.length > 0) {
    for (const caseId of missing) {
      console.error(
        `run.mjs: case ${caseId}: ${EXPECTED_EPJSON} is missing while truth = oracle. ` +
          `Generate it with tools/regenerate.sh and commit it`
      );
    }
    return EXIT_FAILED;
  }

  const registerPath = join(corpusRoot, DIVERGENCE_FILE);
  /** @type {import('./model.mjs').Corpus} */
  let corpus;
  try {
    corpus = loadCorpus(corpusRoot, { requireDivergences: false });
  } catch (error) {
    if (error instanceof CorpusError) {
      console.error(`run.mjs: ${error.message}`);
      return EXIT_FAILED;
    }
    throw error;
  }

  /** @type {Level} */
  let level;
  /** @type {readonly CaseJob[]} */
  let jobs;
  try {
    level = detectLevel(corpusRoot, manifest, args.level);
    jobs = buildJobs(corpus, args.case, tags);
  } catch (error) {
    if (error instanceof RunnerError) {
      console.error(`run.mjs: ${error.message}`);
      return EXIT_UNUSABLE;
    }
    throw error;
  }

  const registerNote = isFile(registerPath)
    ? `${corpus.divergences.entries.length} entry(ies) in ${DIVERGENCE_FILE}`
    : `${DIVERGENCE_FILE} is absent, so no failure is accepted`;
  const report = new RunReport({
    library,
    level,
    corpusRoot,
    registerNote,
    selected: jobs.length,
    total: corpus.manifest.oracle.length + corpus.manifest.convention.length,
  });
  const limit = Math.max(args.maxDifferences, 0);
  report.cases = await execute(jobs, library, limit, args.jobs);
  reconcile(
    report,
    corpus.divergences.forLibrary(LIBRARY),
    jobs.map((job) => job.caseId)
  );
  report.seconds = (performance.now() - started) / 1000;

  printReport(report);
  return report.green ? EXIT_OK : EXIT_FAILED;
}

// ---------------------------------------------------------------------------
// Small helpers
// ---------------------------------------------------------------------------

/**
 * Greedy line wrapping, so the help text stays inside a terminal whatever the taxonomy grows to.
 *
 * @param {string} text
 * @param {number} width
 * @returns {string[]}
 */
function wrap(text, width) {
  /** @type {string[]} */
  const lines = [];
  let line = '';
  for (const word of text.split(' ')) {
    if (line === '') {
      line = word;
    } else if (line.length + 1 + word.length <= width) {
      line += ` ${word}`;
    } else {
      lines.push(line);
      line = word;
    }
  }
  if (line !== '') {
    lines.push(line);
  }
  return lines;
}

/**
 * Render a value the way Python's `!r` does for a string, so the two runners word a bad flag the
 * same way. `model.mjs` keeps its own copy for the same reason; this one is not exported.
 *
 * @param {unknown} value
 * @returns {string}
 */
function quote(value) {
  return `'${String(value).replace(/\\/g, '\\\\').replace(/'/g, "\\'")}'`;
}

/**
 * `TypeName: message` for a thrown value, matching `run.py`'s `f"{type(error).__name__}: {error}"`.
 *
 * @param {unknown} error
 * @returns {string}
 */
function describeError(error) {
  if (error instanceof Error) {
    return `${error.name}: ${error.message}`;
  }
  return `${typeNameOf(error)}: ${String(error)}`;
}

/**
 * The message of a thrown value, without its type.
 *
 * @param {unknown} error
 * @returns {string}
 */
function describeMessage(error) {
  return error instanceof Error ? error.message : String(error);
}

/**
 * @param {unknown} value
 * @returns {string}
 */
function typeNameOf(value) {
  if (value === null) {
    return 'null';
  }
  if (typeof value !== 'object') {
    return typeof value;
  }
  return value.constructor?.name ?? 'object';
}

/**
 * Whether `value` is a genuinely plain object, the JSON sense of the word.
 *
 * `compare.mjs` makes the same distinction: a `Map` or a `Date` has no enumerable own keys, so
 * accepting it would silently snapshot an empty object rather than report a value JSON cannot
 * carry.
 *
 * @param {unknown} value
 * @returns {value is Record<string, unknown>}
 */
function isPlainObject(value) {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    return false;
  }
  const proto = Object.getPrototypeOf(value);
  return proto === Object.prototype || proto === null;
}

/**
 * Expand a leading `~`, which a shell does not when the path arrives quoted.
 *
 * @param {string} path
 * @returns {string}
 */
function expandUser(path) {
  if (path === '~' || path.startsWith(`~${sep}`) || path.startsWith('~/')) {
    const home = process.env.HOME ?? process.env.USERPROFILE;
    if (home !== undefined && home !== '') {
      return join(home, path.slice(1));
    }
  }
  return path;
}

/**
 * @param {string} child
 * @param {string} parent
 * @returns {boolean}
 */
function isUnder(child, parent) {
  const base = parent.endsWith(sep) ? parent : `${parent}${sep}`;
  return isAbsolute(child) && child.startsWith(base);
}

/**
 * @param {string} path
 * @returns {boolean}
 */
function isFile(path) {
  try {
    return statSync(path).isFile();
  } catch {
    return false;
  }
}

/**
 * @param {string} path
 * @returns {boolean}
 */
function isDir(path) {
  try {
    return statSync(path).isDirectory();
  } catch {
    return false;
  }
}

// Run only when invoked as a script. Importing this module from a test must not start a run, and
// `process.argv[1]` may be a symlink into this file, so both sides are resolved before comparing.
if (isMainModule()) {
  process.exitCode = await main();
}

/**
 * Whether this file is the entry point Node was given, rather than an imported module.
 *
 * @returns {boolean}
 */
function isMainModule() {
  const entry = process.argv[1];
  if (entry === undefined) {
    return false;
  }
  try {
    return realpathSync(entry) === realpathSync(fileURLToPath(import.meta.url));
  } catch {
    return false;
  }
}
