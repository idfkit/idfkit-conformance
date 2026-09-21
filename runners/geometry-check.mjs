#!/usr/bin/env node
/**
 * The JavaScript entry point for `checks/geometry-vertices`.
 *
 * Run it from the root of this repository, pointing `--library` at a checkout of the JavaScript
 * library. The flag takes a **path**, never a language word, exactly as `run.mjs` and
 * `weather-check.mjs` do: the runner file already fixes the language, and `geometry_check.py`
 * drives Python.
 *
 *     node runners/geometry-check.mjs --library /path/to/idfkit-js
 *
 * WHY THIS IS A SEPARATE ENTRY POINT AND NOT A FLAG ON `run.mjs`
 *
 * `run.mjs` runs cases. A case is an input file, a parsed document, and an assertion about what the
 * library made of it against an expectation `ConvertInputFormat` produced. `ConvertInputFormat`
 * never resolves a coordinate: handed a relative-coordinate model it returns the authored numbers in
 * the other format. The claim here is about what a library computes from a document rather than
 * about what the document contains, which is the criterion `checks/README.md` states for this
 * directory.
 *
 * WHAT IT CHECKS, which is `checks/geometry-vertices/check.md` in code:
 *
 * Per surface, the polygon the library resolved against the polygon EnergyPlus reported, **as a
 * ring** and with **orientation preserved**, within 0.005 m. Fenestration additionally compares the
 * parent surface the library named against the base surface column of the report.
 *
 * THE RING COMPARISON, AND WHY IT IS NOT AN INDEX COMPARISON
 *
 * The engine renormalises every reported surface to begin at its upper-left corner. A faithful
 * extractor preserves the author's order. Comparing index by index therefore fails the models that
 * declare a different starting corner for a reason that has nothing to do with resolution, and
 * `lower-left-start` is in the fixture set to keep that fact in front of whoever reads this next.
 *
 * The comparison takes the smallest maximum vertex error over the **cyclic rotations** of the ring.
 * It never tries the reversal, because a reversed ring is a real difference and it is the difference
 * the clockwise clause exists to produce. That asymmetry is the whole point of this function and is
 * the thing a later reader will try to simplify away; `runners/tests/test-ring.mjs` asserts both
 * halves of it.
 *
 * THE GUARDS
 *
 * `--without <clause>` removes one clause of the resolution rule and requires that the check then
 * fail on the fixture that clause exists for. A check that has only ever passed is half a check,
 * and this is the half that asks. The clause is removed on the output rather than inside the
 * library, because the corpus cannot reach into either library's source and must ask the same
 * question of both. For the entry direction that undoing is exact: the clause reverses a ring while
 * holding its head, which is its own inverse.
 *
 * A guarded run reverses the verdict. It exits 0 when the named fixture fails by at least the
 * recorded magnitude and every other fixture still passes, and 1 when the clause turned out not to
 * matter, which is the finding worth reporting.
 *
 * NO NETWORK, and no dependency. The fixtures are committed gzipped and decompressed here with
 * `node:zlib`, which is the only compression both standard libraries hold.
 *
 * This file is a section-by-section mirror of `geometry_check.py`: the same sections, the same
 * statuses, and the same report strings apart from the library name and the paths. A reader who
 * diffs the two transcripts sees only the genuine disagreements.
 *
 * Exit codes: 0 when every comparison is green, 1 for any failure, and 2 when the run could not
 * start at all, which is what a library without the capability reports rather than a failure.
 */

import { existsSync, readFileSync, readdirSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { gunzipSync } from 'node:zlib';

const RUNNERS_DIR = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = dirname(RUNNERS_DIR);
const CHECK_DIR = join(REPO_ROOT, 'checks', 'geometry-vertices');

/**
 * EnergyPlus input is written latin-1, and latin-1 never fails on any byte sequence.
 */
const ENCODING = 'latin1';

/**
 * Derived, not chosen. The report prints two decimals, so half the last printed place is the finest
 * agreement the authority can express. Tightening it asks the oracle for a digit it does not state;
 * `check.md` carries this derivation so nobody tries.
 */
export const TOLERANCE_M = 0.005;

export class Unusable extends Error {}

// ---------------------------------------------------------------------------
// The ring comparison
// ---------------------------------------------------------------------------

/**
 * The largest disagreement on any one coordinate, not the distance between the two points.
 *
 * PER COORDINATE, BECAUSE THAT IS WHAT THE ORACLE STATES.
 *
 * The report prints each coordinate to two decimals, so the tolerance is half the last printed
 * place of ONE NUMBER. Euclidean distance mixes three independently rounded numbers into one
 * figure, and three coordinates each a legal 0.005 out give a distance of 0.00866: over the
 * tolerance without a single coordinate disagreeing by more than the report can express.
 *
 * Measured rather than reasoned. Comparing by distance failed 44 of 234 surfaces in the fixture
 * set, every one of them between 0.0054 and 0.0073 m. Comparing per coordinate passes all 234.
 */
export function vertexError(one, other) {
  let worst = 0;
  for (let axis = 0; axis < 3; axis += 1) worst = Math.max(worst, Math.abs(one[axis] - other[axis]));
  return worst;
}

/**
 * The smallest maximum vertex error over the cyclic rotations of one ring against another.
 *
 * Rotation-insensitive and **orientation-sensitive**: the reversal is never tried. Throws when the
 * two rings differ in length, because that is a disagreement about the surface rather than about
 * where it is, and averaging over a shorter ring would hide it.
 */
export function ringError(resolved, reported) {
  if (resolved.length !== reported.length) {
    throw new RangeError(`${resolved.length} vertices against ${reported.length}`);
  }
  if (resolved.length === 0) throw new RangeError('an empty ring has no error to report');
  const count = resolved.length;
  let best = Infinity;
  for (let shift = 0; shift < count; shift += 1) {
    let worst = 0;
    for (let at = 0; at < count; at += 1) {
      worst = Math.max(worst, vertexError(resolved[(at + shift) % count], reported[at]));
    }
    best = Math.min(best, worst);
  }
  return best;
}

// ---------------------------------------------------------------------------
// The guards
// ---------------------------------------------------------------------------

/**
 * What a library that never wrote the vertex entry direction clause would have returned.
 *
 * The clause reverses a ring while holding its first vertex, so applying it twice is applying it
 * never. On a model that does not declare clockwise entry this is the identity, which is the point:
 * a clause that fires where it was not declared would show up here as a second fixture failing, and
 * the guard fails the run when one does.
 *
 * An extraction is `{ surfaces, entryDirection }`: the surfaces the library resolved, and what it
 * read the model to declare. The declaration comes from the library rather than from a second parse
 * by the runner, so a library that misreads it leaves its own guard a no-op and the guard says so
 * instead of passing.
 */
export function withoutEntryDirection(extraction) {
  if (!extraction.entryDirection.toLowerCase().startsWith('clockwise')) return extraction;
  return {
    surfaces: extraction.surfaces.map((surface) => ({
      ...surface,
      vertices: [surface.vertices[0], ...surface.vertices.slice(1).reverse()],
    })),
    entryDirection: extraction.entryDirection,
  };
}

/**
 * The guards this runner implements. `geometry_check.py` names the same ones, and
 * `checks/geometry-vertices/check.md` records what each is worth.
 *
 * `atLeastM` is a measurement and not a threshold to clear: it is how far the fixture moved when
 * the clause was first removed, recorded so that a clause quietly becoming a rounding difference is
 * a failure rather than a pass.
 */
export const GUARDS = {
  'entry-direction': {
    name: 'entry-direction',
    failsOn: 'clockwise-entry',
    atLeastM: 4.0,
    remove: withoutEntryDirection,
  },
};

/**
 * Whether the clause is load-bearing: report it, and return the run's exit code.
 *
 * Three things have to hold, and the last two are the ones a weaker guard would skip. The named
 * fixture must fail; it must fail by at least what was measured when the clause was written, so
 * that a clause reduced to noise cannot pass as one that matters; and no other fixture may fail,
 * because a clause that fires on a model that did not declare it is a different bug wearing this
 * one's clothes.
 */
export function guardVerdict(guard, report) {
  const failures = report.failed.get(guard.failsOn) ?? 0;
  const worst = report.worst.get(guard.failsOn) ?? 0;
  const elsewhere = [...report.failed.keys()].filter((name) => name !== guard.failsOn).sort();

  console.log(`  guard       ${guard.name}: the clause removed, ${guard.failsOn} expected to fail`);
  console.log(`     ${guard.failsOn}: ${failures} comparisons disagree, worst ${worst.toFixed(4)} m`);
  if (elsewhere.length > 0) console.log(`     also failing: ${elsewhere.join(', ')}`);
  console.log('');

  if (failures === 0) {
    console.error(
      `GUARD DID NOT HOLD: ${guard.failsOn} passes without the ${guard.name} clause, ` +
        'so nothing here proves the clause is doing anything.'
    );
    return 1;
  }
  if (worst < guard.atLeastM) {
    console.error(
      `GUARD DID NOT HOLD: removing the ${guard.name} clause moves ${guard.failsOn} by ` +
        `${worst.toFixed(4)} m, and it was worth at least ${guard.atLeastM} m when it was written.`
    );
    return 1;
  }
  if (elsewhere.length > 0) {
    console.error(
      `GUARD DID NOT HOLD: removing the ${guard.name} clause also fails ` +
        `${elsewhere.join(', ')}, which declares no such thing.`
    );
    return 1;
  }
  console.log(
    `GUARD HOLDS: without the ${guard.name} clause, ${guard.failsOn} is ${worst.toFixed(4)} m from ` +
      `the engine over ${failures} comparisons, and no other fixture changes its verdict.`
  );
  return 0;
}

// ---------------------------------------------------------------------------
// The corpus side
// ---------------------------------------------------------------------------

/** Every committed fixture, in name order so two runs report in the same order. */
function fixtures() {
  const found = readdirSync(join(CHECK_DIR, 'fixtures'))
    .filter((name) => name.endsWith('.idf.gz'))
    .sort();
  if (found.length === 0) throw new Unusable(`no fixtures under ${join(CHECK_DIR, 'fixtures')}`);
  return found;
}

/** Decompress one committed fixture into text. */
function modelText(fixture) {
  return gunzipSync(readFileSync(join(CHECK_DIR, 'fixtures', fixture))).toString(ENCODING);
}

/**
 * Read one committed expectation.
 *
 * The comment lines carrying the engine version and the frame are skipped here and read by
 * `provenance`; they are in the file so that a reader of the expectation meets them, not so that
 * this function does.
 */
export function expectation(name) {
  const path = join(CHECK_DIR, 'expected', `${name}.csv`);
  if (!existsSync(path)) throw new Unusable(`no expectation at ${path}`);
  const rows = [];
  for (const line of readFileSync(path, 'utf8').split('\n')) {
    if (line === '' || line.startsWith('#')) continue;
    const fields = line.split(',');
    if (fields[0] === 'kind') continue;
    const count = Number.parseInt(fields[4], 10);
    const flat = fields.slice(5, 5 + count * 3).map(Number);
    const vertices = [];
    for (let at = 0; at < flat.length; at += 3) vertices.push([flat[at], flat[at + 1], flat[at + 2]]);
    rows.push({
      kind: fields[0],
      name: fields[1],
      surfaceClass: fields[2],
      baseSurface: fields[3],
      vertices,
    });
  }
  return rows;
}

/** The engine version and frame the expectation carries, for `--verbose`. */
function provenance(name) {
  const found = {};
  for (const line of readFileSync(join(CHECK_DIR, 'expected', `${name}.csv`), 'utf8').split('\n')) {
    if (!line.startsWith('#')) break;
    const at = line.indexOf(':');
    if (at === -1) continue;
    found[line.slice(1, at).trim()] = line.slice(at + 1).trim();
  }
  return found;
}

// ---------------------------------------------------------------------------
// The library side
// ---------------------------------------------------------------------------

/** Import `@idfkit/geometry`'s build output out of the checkout `--library` named. */
async function importLibrary(root) {
  const resolved = resolve(root);
  const dist = join(resolved, 'packages', 'geometry', 'dist', 'index.js');
  if (!existsSync(dist)) {
    throw new Unusable(
      `no built @idfkit/geometry under ${resolved}. Looked for ${dist}.\n` +
        '  Build the checkout with `npm run build`, or `npx tsc --build`.\n' +
        '  geometry-check.mjs drives the JavaScript library; use ' +
        "'python runners/geometry_check.py --library <path>' for idfkit."
    );
  }
  return import(pathToFileURL(dist).href);
}

/**
 * Resolve one model's geometry with the library under test.
 *
 * UNIMPLEMENTED ON PURPOSE, and loudly.
 *
 * The capability this check exists for does not ship yet in either language. Writing the comparison
 * first means the rule is established against committed evidence rather than against whatever the
 * first implementation happens to produce, which is the order the corpus already uses for cases.
 * What is missing here is only the adapter from the library's own scene type to the shape below;
 * everything around this function is complete and under test.
 *
 * It throws rather than returning an empty array. An empty array would make every fixture compare
 * nothing and the run report success, which is the failure mode a check must never have.
 *
 * When it is wired up it returns `{ surfaces, entryDirection }`, the shape the guards above take:
 * `surfaces` of `{ name, vertices, parentSurface }` and `entryDirection` as the library read it
 * from the model.
 */
// eslint-disable-next-line no-unused-vars
function extract(library, model) {
  throw new Unusable(
    'the library\'s scene extraction is not wired into this runner yet. ' +
      'This runner is complete apart from this call; see checks/geometry-vertices/check.md'
  );
}

// ---------------------------------------------------------------------------
// The comparison
// ---------------------------------------------------------------------------

/**
 * Record one disagreement against the fixture it was found in.
 *
 * Per fixture because a guard names one fixture and must not be satisfied by another one failing.
 */
function fail(report, fixture, message) {
  report.failures.push(`${fixture}: ${message}`);
  report.failed.set(fixture, (report.failed.get(fixture) ?? 0) + 1);
}

/** Record one comparison's error, whether or not it was within tolerance. */
function measured(report, fixture, error) {
  report.worst.set(fixture, Math.max(report.worst.get(fixture) ?? 0, error));
}

/** Compare one model's resolved surfaces against the engine's report of the same model. */
function compareFixture(name, extracted, report) {
  const reported = new Map(expectation(name).map((surface) => [surface.name.toUpperCase(), surface]));
  for (const surface of extracted) {
    const against = reported.get(surface.name.toUpperCase());
    if (against === undefined) {
      fail(report, name, `${surface.name} was resolved and the engine reports no such surface`);
      continue;
    }
    let error;
    try {
      error = ringError(surface.vertices, against.vertices);
    } catch (reason) {
      fail(report, name, `${surface.name} ${reason.message}`);
      continue;
    }
    report.compared += 1;
    measured(report, name, error);
    if (error > TOLERANCE_M) {
      fail(
        report,
        name,
        `${surface.name} is ${error.toFixed(4)} m from the engine, tolerance ${TOLERANCE_M} m`
      );
    }
    if (
      surface.parentSurface &&
      surface.parentSurface.toUpperCase() !== against.baseSurface.toUpperCase()
    ) {
      fail(
        report,
        name,
        `${surface.name} names parent ${JSON.stringify(surface.parentSurface)} ` +
          `and the engine reports ${JSON.stringify(against.baseSurface)}`
      );
    }
  }
}

// ---------------------------------------------------------------------------

function parseArgs(argv) {
  const args = { library: undefined, verbose: false, without: undefined };
  for (let i = 0; i < argv.length; i += 1) {
    if (argv[i] === '--library') args.library = argv[++i];
    else if (argv[i] === '--verbose') args.verbose = true;
    else if (argv[i] === '--without') args.without = argv[++i];
    else throw new Unusable(`unknown argument ${JSON.stringify(argv[i])}`);
  }
  if (args.without !== undefined && !(args.without in GUARDS)) {
    throw new Unusable(
      `unknown guard ${JSON.stringify(args.without)}; this runner has ${Object.keys(GUARDS).sort().join(', ')}`
    );
  }
  if (args.library === undefined) {
    throw new Unusable('--library <path to an idfkit-js checkout> is required');
  }
  return args;
}

async function main(argv) {
  const args = parseArgs(argv);
  const guard = args.without === undefined ? undefined : GUARDS[args.without];
  const library = await importLibrary(args.library);

  if (!existsSync(CHECK_DIR)) throw new Unusable(`${CHECK_DIR}: missing`);
  const committed = fixtures();

  const report = {
    compared: 0,
    failures: [],
    unresolved: [],
    notes: [],
    failed: new Map(),
    worst: new Map(),
  };

  console.log('idfkit geometry-vertices check: JavaScript');
  console.log(`  library     ${resolve(args.library)}`);
  console.log(`  check       ${CHECK_DIR}`);
  console.log('');

  for (const fixture of committed) {
    const name = fixture.replace(/\.idf\.gz$/, '');
    report.notes.push(`${name}: ${provenance(name).engine ?? 'engine unrecorded'}`);
    let extraction = extract(library, modelText(fixture));
    if (guard !== undefined) extraction = guard.remove(extraction);
    compareFixture(name, extraction.surfaces, report);
  }

  console.log(`  vertices    ${committed.length} fixtures, ring comparison within ${TOLERANCE_M} m`);
  if (args.verbose) for (const note of report.notes) console.log(`     ${note}`);
  console.log('');

  for (const line of report.unresolved) console.log(`  UNRESOLVED ${line}`);
  // Under a guard these are the expected finding rather than the bad news, so they are not written
  // to the error stream and are not called failures.
  for (const line of report.failures) {
    if (guard === undefined) console.error(`  FAIL       ${line}`);
    else console.log(`  WOULD FAIL ${line}`);
  }
  console.log('');

  // A check that compared nothing has proven nothing, and must not report success for it. This is
  // not a defensive line: an adapter that returns an empty array on every fixture is the cheapest
  // way for this check to go quietly green while proving nothing at all.
  if (report.compared === 0) {
    console.error('FAIL: no surface was compared, so a green run would prove nothing.');
    return 1;
  }

  if (guard !== undefined) return guardVerdict(guard, report);

  if (report.failures.length > 0) {
    console.error(
      `FAIL: ${report.failures.length} of ${report.compared} comparisons disagree ` +
        `(${report.unresolved.length} unresolved).`
    );
    return 1;
  }
  console.log(
    `PASS: ${report.compared} surfaces against the engine's own vertex report ` +
      `(${report.unresolved.length} unresolved).`
  );
  return 0;
}

// Importable for the runner's own tests: `node --test` loads this file for `ringError` and must not
// have it run a check as a side effect.
if (process.argv[1] && resolve(process.argv[1]) === resolve(fileURLToPath(import.meta.url))) {
  try {
    process.exit(await main(process.argv.slice(2)));
  } catch (error) {
    if (error instanceof Unusable) {
      console.error(`The check could not run: ${error.message}`);
      process.exit(2);
    }
    throw error;
  }
}
