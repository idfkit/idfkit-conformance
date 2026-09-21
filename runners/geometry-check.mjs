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
 * `--without <clause>` removes one clause and requires that the check then fail on the fixture that
 * clause exists for. A check that has only ever passed is half a check, and this is the half that
 * asks. The clause is removed on the output rather than inside the library, because the corpus
 * cannot reach into either library's source and must ask the same question of both.
 *
 * Four clauses, and the fourth is not like the other three. `coordinate-system`, `north-axis` and
 * `entry-direction` are clauses of the RESOLUTION rule, and removing one changes what the library
 * is taken to have returned. `starting-vertex` is a clause of the COMPARISON: it drops the
 * rotation-insensitivity of the ring comparison, which is the same thing as requiring the
 * extractor's first vertex to be the engine's. It is the one guard that must go on failing, and
 * `checks/geometry-vertices/check.md` says at length why.
 *
 * WHICH FIXTURES MAY FAIL UNDER A GUARD
 *
 * Not "only the named one". A clause fires wherever the model declares the condition it reads, and
 * two fixtures declare a non-zero building north axis. So each guard says which models it APPLIES
 * to, reading the library's own declaration, and the verdict is that the named fixture must fail by
 * at least the recorded magnitude and no fixture the clause never touched may fail at all. A
 * fixture the clause did touch is allowed to fail and is reported as expected company, because that
 * is the clause doing its job in a second model rather than a second bug.
 *
 * A guarded run reverses the verdict. It exits 0 when that holds, and 1 when the clause turned out
 * not to matter, which is the finding worth reporting.
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

/**
 * The same comparison with the rotation search removed: vertex one against vertex one.
 *
 * NOT THE CHECK'S COMPARISON, and never reached except under `--without starting-vertex`. It is
 * here to be the wrong answer, because requiring the extractor's first vertex to be the engine's is
 * exactly what comparing by index requires, and the fixture set carries a model that proves what
 * that costs.
 */
export function indexError(resolved, reported) {
  if (resolved.length !== reported.length) {
    throw new RangeError(`${resolved.length} vertices against ${reported.length}`);
  }
  if (resolved.length === 0) throw new RangeError('an empty ring has no error to report');
  let worst = 0;
  for (let at = 0; at < resolved.length; at += 1) {
    worst = Math.max(worst, vertexError(resolved[at], reported[at]));
  }
  return worst;
}

// ---------------------------------------------------------------------------
// The guards
// ---------------------------------------------------------------------------

/**
 * One extraction with every ring replaced, and everything else carried through.
 *
 * An extraction is `{ surfaces, entryDirection, coordinateSystem, startingVertexPosition,
 * northAxis, zoneOrigins }`: the surfaces the library resolved as `{ name, vertices, parentSurface,
 * zone }`, and what it read the model to declare. The declarations come from the library rather
 * than from a second parse by the runner, so a library that misreads one leaves its own guard a
 * no-op and the guard says so instead of passing.
 */
function mapped(extraction, move) {
  return {
    ...extraction,
    surfaces: extraction.surfaces.map((surface) => ({ ...surface, vertices: move(surface) })),
  };
}

/** What a guard that removes a clause of the comparison rather than of the rule does to it. */
export function unchanged(extraction) {
  return extraction;
}

/**
 * What a library that never wrote the vertex entry direction clause would have returned.
 *
 * The clause reverses a ring while holding its first vertex, so applying it twice is applying it
 * never, which is what makes undoing it on the output exact rather than approximate.
 *
 * THE FIRST VERTEX STAYS WHERE IT IS. Reversing the whole list would renormalise the starting
 * vertex as a side effect, and the check's ring comparison is insensitive to where a ring starts,
 * so nothing downstream would ever say so.
 */
export function withoutEntryDirection(extraction) {
  return mapped(extraction, (surface) => [surface.vertices[0], ...surface.vertices.slice(1).reverse()]);
}

/**
 * What a library that never read `GlobalGeometryRules`'s coordinate system would have returned.
 *
 * Clause one applies the zone's origin only under the relative system. A library that never wrote
 * the condition applies it always, so on a model declaring `World` every surface comes out
 * displaced by its zone's origin, and on one declaring `Relative` it comes out exactly where it
 * already is. That is why this is applied only to the models that declare `World`: on the others
 * the clause has already fired and applying it again would measure a double shift, which is a third
 * answer neither library would ever give.
 *
 * THE ORIGIN IS ROTATED BEFORE IT IS ADDED, and that is not a flourish. Clause one runs before
 * clause two, so a library missing clause one returns `R(v + o)` where `R` is the building
 * rotation, while this function is handed `R(v)` and can only add. `R(v + o)` is `R(v) + R(o)`, so
 * the origin is turned by the same angle first. Adding it unturned is exact only when the model's
 * north axis is zero, which all three of the fixtures declaring `World` happen to be; at an axis of
 * 45 degrees on a zone origin of (1.98, 4.58) it is 2.74 m wrong, and it would have been a silently
 * approximate guard rather than an exact one.
 *
 * ONE THING IT DOES NOT DO, because the fixture set does not exercise it. It does not apply the
 * zone's `direction_of_relative_north`, which clause one also governs. No model in the set declares
 * `World` and carries a non-zero zone rotation, so a branch for it would be an untested path
 * standing in for a proof.
 *
 * AND ONE IT CANNOT DO. It does not move a surface the library placed in no zone. Twenty-one of the
 * ninety-nine surfaces in `world-nonzero-zone-origin` are `Shading:Zone:Detailed`, which resolve
 * against the zone of the surface they are attached to and which a scene reports with no zone of
 * their own. The guard therefore moves seventy-eight of them, which is enough to make the point at
 * 201.98 m and is less than a library without the clause would move. Under-reaching is safe here in
 * a way that over-reaching would not be: it can only make the guard harder to satisfy.
 */
export function withoutCoordinateSystem(extraction) {
  const origins = extraction.zoneOrigins ?? new Map();
  const radians = (-extraction.northAxis * Math.PI) / 180;
  const cosine = Math.cos(radians);
  const sine = Math.sin(radians);
  return mapped(extraction, (surface) => {
    const origin = surface.zone ? origins.get(surface.zone.toUpperCase()) : undefined;
    if (origin === undefined) return surface.vertices;
    const turned = [origin[0] * cosine - origin[1] * sine, origin[0] * sine + origin[1] * cosine, origin[2]];
    return surface.vertices.map((v) => [v[0] + turned[0], v[1] + turned[1], v[2] + turned[2]]);
  });
}

/**
 * What a library that never rotated the building by its north axis would have returned.
 *
 * Clause two turns the whole resolved building about the world origin by the negation of
 * `Building.north_axis`, the negation being there because EnergyPlus measures the axis clockwise
 * from true north while a rotation turns counter-clockwise. Undoing it is turning it back, and a
 * rotation is exactly invertible, so this undoing is as exact as the entry direction's.
 *
 * It is the one clause of the three that is unconditional in the rule: it fires on every model
 * carrying a non-zero axis, and two fixtures do. Both are therefore allowed to fail under this
 * guard, and the verdict says which one it was written for.
 */
export function withoutNorthAxis(extraction) {
  const radians = (extraction.northAxis * Math.PI) / 180;
  const cosine = Math.cos(radians);
  const sine = Math.sin(radians);
  return mapped(extraction, (surface) =>
    surface.vertices.map((v) => [v[0] * cosine - v[1] * sine, v[0] * sine + v[1] * cosine, v[2]])
  );
}

/**
 * The guards this runner implements. `geometry_check.py` names the same ones, and
 * `checks/geometry-vertices/check.md` records what each is worth and on which fixture.
 *
 * `atLeastM` is a measurement and not a threshold to clear: it is how far the fixture moved when
 * the clause was first removed, recorded so that a clause quietly becoming a rounding difference is
 * a failure rather than a pass.
 *
 * `applies` answers, from the library's own reading of a model, whether the clause had anything to
 * do in it. It is what separates a second fixture failing because the clause fired there too from a
 * second fixture failing because something else is wrong.
 *
 * `byIndex` is set by the one guard that removes a clause of the COMPARISON instead. Its `remove`
 * is the identity, because there is nothing wrong with what the library returned.
 */
export const GUARDS = {
  'coordinate-system': {
    name: 'coordinate-system',
    failsOn: 'world-nonzero-zone-origin',
    atLeastM: 201.98,
    applies: (extraction) => extraction.coordinateSystem.toLowerCase() !== 'relative',
    remove: withoutCoordinateSystem,
    byIndex: false,
  },
  'north-axis': {
    name: 'north-axis',
    failsOn: 'north-axis-multizone',
    atLeastM: 22.5571,
    applies: (extraction) => extraction.northAxis !== 0,
    remove: withoutNorthAxis,
    byIndex: false,
  },
  'entry-direction': {
    name: 'entry-direction',
    failsOn: 'clockwise-entry',
    atLeastM: 4.0,
    applies: (extraction) => extraction.entryDirection.toLowerCase().startsWith('clockwise'),
    remove: withoutEntryDirection,
    byIndex: false,
  },
  'starting-vertex': {
    name: 'starting-vertex',
    failsOn: 'lower-left-start',
    atLeastM: 17.59,
    applies: (extraction) => extraction.startingVertexPosition.toLowerCase() !== 'upperleftcorner',
    remove: unchanged,
    byIndex: true,
  },
};

/**
 * Whether the clause is load-bearing: report it, and return the run's exit code.
 *
 * Three things have to hold, and the last two are the ones a weaker guard would skip. The named
 * fixture must fail; it must fail by at least what was measured when the clause was written, so
 * that a clause reduced to noise cannot pass as one that matters; and no fixture the clause never
 * touched may fail, because a clause firing on a model that declares no such thing is a different
 * bug wearing this one's clothes.
 *
 * `appliedTo` names the fixtures whose declarations put the clause in scope. A fixture in that list
 * failing is the clause doing its job twice and is reported as such; a fixture outside it failing
 * is the finding.
 */
export function guardVerdict(guard, report, appliedTo) {
  const failures = report.failed.get(guard.failsOn) ?? 0;
  const worst = report.worst.get(guard.failsOn) ?? 0;
  const inScope = new Set(appliedTo);
  const alongside = [...report.failed.keys()].filter((n) => n !== guard.failsOn && inScope.has(n)).sort();
  const untouched = [...report.failed.keys()].filter((n) => !inScope.has(n)).sort();

  console.log(`  guard       ${guard.name}: the clause removed, ${guard.failsOn} expected to fail`);
  console.log(`     in scope: ${[...inScope].sort().join(', ') || 'no fixture declares it'}`);
  console.log(`     ${guard.failsOn}: ${failures} comparisons disagree, worst ${worst.toFixed(4)} m`);
  for (const name of alongside) {
    const seen = (report.worst.get(name) ?? 0).toFixed(4);
    console.log(`     ${name}: ${report.failed.get(name)} disagree, worst ${seen} m, and it declares it too`);
  }
  if (untouched.length > 0) {
    console.log(`     also failing, untouched by the clause: ${untouched.join(', ')}`);
  }
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
  if (untouched.length > 0) {
    console.error(
      `GUARD DID NOT HOLD: removing the ${guard.name} clause also fails ` +
        `${untouched.join(', ')}, which declares no such thing.`
    );
    return 1;
  }
  const company = alongside.length > 0 ? `, along with ${alongside.join(', ')}, which declares it too` : '';
  console.log(
    `GUARD HOLDS: without the ${guard.name} clause, ${guard.failsOn} is ${worst.toFixed(4)} m from ` +
      `the engine over ${failures} comparisons${company}, and no fixture the clause never touched ` +
      'changes its verdict.'
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

/**
 * Import the build output of the packages this check drives, out of the checkout `--library` named.
 *
 * Three modules rather than one. `@idfkit/geometry` is what is under test; `@idfkit/core` parses
 * the committed fixture text, since extraction takes a document and not a string; and
 * `@idfkit/core/node` resolves the schema for the version the fixture declares. The Python runner
 * needs no equivalent because one import there carries the reader, the parser and the schemas.
 *
 * The build output rather than the sources, because that is what a consumer installing from npm
 * gets. A missing `dist/` is an unusable run rather than a failure.
 */
async function importLibrary(root) {
  const resolved = resolve(root);
  // The import specifier each entry stands for, so the message names what a reader would install
  // rather than the key this object happens to use. `node` is a subpath of `@idfkit/core`, not a
  // package of its own, and saying `@idfkit/core` for it sends a reader looking for the wrong file.
  const SPECIFIER = {
    geometry: '@idfkit/geometry',
    core: '@idfkit/core',
    node: '@idfkit/core/node',
  };
  const wanted = {
    geometry: join(resolved, 'packages', 'geometry', 'dist', 'index.js'),
    core: join(resolved, 'packages', 'core', 'dist', 'index.js'),
    node: join(resolved, 'packages', 'core', 'dist', 'node.js'),
  };
  for (const [name, path] of Object.entries(wanted)) {
    if (existsSync(path)) continue;
    throw new Unusable(
      `no built ${SPECIFIER[name]} under ${resolved}. ` +
        `Looked for ${path}.\n` +
        '  Build the checkout with `npm run build`, or `npx tsc --build`.\n' +
        '  geometry-check.mjs drives the JavaScript library; use ' +
        "'python runners/geometry_check.py --library <path>' for idfkit."
    );
  }

  const library = {
    geometry: await import(pathToFileURL(wanted.geometry).href),
    core: await import(pathToFileURL(wanted.core).href),
    node: await import(pathToFileURL(wanted.node).href),
  };
  if (typeof library.geometry.getScene !== 'function') {
    throw new Unusable(
      `@idfkit/geometry at ${wanted.geometry} exports no getScene(), so this runner cannot drive ` +
        'it. The build output may be stale: rebuild the checkout.'
    );
  }
  return library;
}

/**
 * Resolve one model's geometry with the library under test.
 *
 * The adapter, and only the adapter. Everything the comparison needs is read out of the library's
 * own scene type here, so that the two runners compare a shape the corpus owns rather than one
 * library's spelling.
 *
 * Asynchronous where the Python runner is synchronous, and for one reason: the schema bundle is
 * loaded from disk here and is already in memory there. The document it produces is the same
 * document, read with the same defaults as `load_idf` uses, so the two runners hand their libraries
 * the same thing.
 *
 * It returns the shape the guards above take: `surfaces` of `{ name, vertices, parentSurface, zone }`
 * and the four declarations the library read from the model, plus each zone's origin.
 */
async function extract(library, model) {
  const schema = await library.node.schemaFor(library.core.getIdfVersion(model));
  const { document } = library.core.parseIdf(model, schema);
  const scene = library.geometry.getScene(document);

  return {
    surfaces: scene.surfaces.map((surface) => ({
      name: surface.name,
      vertices: surface.polygon.vertices.map((vertex) => [vertex.x, vertex.y, vertex.z]),
      parentSurface: surface.parentSurface ?? '',
      zone: surface.zone,
    })),
    entryDirection: scene.applied.vertexEntryDirection,
    coordinateSystem: scene.applied.coordinateSystem,
    startingVertexPosition: scene.applied.startingVertexPosition,
    northAxis: scene.applied.northAxis,
    zoneOrigins: zoneOrigins(document),
    // What the library could not place, and what it says it did not attempt. Both are here so that
    // a surface going missing is a failure rather than one fewer comparison: without them the check
    // only ever asks about the surfaces a library chose to return, which is the one question a
    // library that dropped a wall would answer correctly.
    unresolved: scene.unresolved.map((item) => `${item.objectType} ${item.name}: ${item.reason}`),
    unattempted: scene.unattempted.map((item) => [item.objectType, item.count]),
  };
}

/**
 * Each zone's declared origin, keyed by upper-cased name.
 *
 * Read from the document rather than from the scene because a scene names each surface's zone and
 * not that zone's origin, and the coordinate system guard needs the origin. It is still the
 * library's own parse of the file: the runner asks the document it was handed for three numeric
 * fields and does not read the text itself.
 */
function zoneOrigins(document) {
  const found = new Map();
  for (const zone of document.all('Zone').toArray()) {
    found.set(String(zone.name).toUpperCase(), [
      Number(zone.get('x_origin') ?? 0) || 0,
      Number(zone.get('y_origin') ?? 0) || 0,
      Number(zone.get('z_origin') ?? 0) || 0,
    ]);
  }
  return found;
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

/**
 * Compare one model's resolved surfaces against the engine's report of the same model.
 *
 * `byIndex` is the starting-vertex guard and nothing else. An unguarded run always compares as a
 * ring, and `check.md` records why the option to do otherwise exists only to be shown failing.
 */
function compareFixture(name, extraction, report, byIndex = false) {
  const compare = byIndex ? indexError : ringError;
  const reported = new Map(expectation(name).map((surface) => [surface.name.toUpperCase(), surface]));
  const matched = new Set();
  for (const surface of extraction.surfaces) {
    const against = reported.get(surface.name.toUpperCase());
    if (against === undefined) {
      fail(report, name, `${surface.name} was resolved and the engine reports no such surface`);
      continue;
    }
    matched.add(surface.name.toUpperCase());
    let error;
    try {
      error = compare(surface.vertices, against.vertices);
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

  for (const line of extraction.unresolved ?? []) report.unresolved.push(`${name}: ${line}`);
  accountForTheRest(name, extraction, reported, matched, report);
}

/**
 * Fail when the engine reports a surface the library neither resolved nor accounted for.
 *
 * WITHOUT THIS THE CHECK CANNOT FAIL ON AN OMISSION, which is the cheapest regression there is. The
 * comparison walks the surfaces the library returned and looks each one up in the expectation, so a
 * library that dropped a wall compares one fewer surface and passes: delete one from
 * `north-axis-multizone` and the run reports 233 green comparisons instead of 234 and exits 0. The
 * engine's report is the authority on what is in the model, so it is the side that has to be
 * exhausted.
 *
 * ONE EXEMPTION, AND IT IS DERIVED RATHER THAN NAMED. A library that reports unattempted types is
 * saying the model holds geometry this slice does not read, and the engine reported those surfaces
 * anyway. `simplified-only-unread` is such a model: 45 reported surfaces against 43 unattempted
 * objects, which is not an accounting error but the engine's own expansion, since a `Shading:Fin`
 * becomes two surfaces and the model holds two of them. Counting objects against surfaces there
 * would mean teaching this check the engine's expansion rules, which is exactly the knowledge
 * `regenerate.py` refuses to hold. So the rule asks its question only of a model the library
 * attempted in full, which is six of the seven fixtures and every one of the 234 surfaces the check
 * actually compares.
 */
function accountForTheRest(name, extraction, reported, matched, report) {
  if ((extraction.unattempted ?? []).length > 0) return;
  const missing = [...reported.entries()]
    .filter(([key]) => !matched.has(key))
    .map(([, surface]) => surface.name)
    .sort();
  for (const absent of missing) {
    fail(report, name, `the engine reports ${absent} and the library resolved no such surface`);
  }
}

// ---------------------------------------------------------------------------

/**
 * The value a flag takes, or an unusable run.
 *
 * A flag written with no value after it must not read as the flag being absent: `--without` with
 * nothing after it would otherwise run the UNGUARDED check and exit 0, which reads as a guard that
 * held. `geometry_check.py` gets this from argparse; this is the same refusal.
 */
function value(argv, at, flag) {
  const given = argv[at];
  if (given === undefined || given.startsWith('--')) {
    throw new Unusable(`${flag} expects one argument`);
  }
  return given;
}

function parseArgs(argv) {
  const args = { library: undefined, verbose: false, without: undefined };
  for (let i = 0; i < argv.length; i += 1) {
    if (argv[i] === '--library') args.library = value(argv, ++i, '--library');
    else if (argv[i] === '--verbose') args.verbose = true;
    else if (argv[i] === '--without') args.without = value(argv, ++i, '--without');
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

  const appliedTo = [];
  for (const fixture of committed) {
    const name = fixture.replace(/\.idf\.gz$/, '');
    report.notes.push(`${name}: ${provenance(name).engine ?? 'engine unrecorded'}`);
    let extraction = await extract(library, modelText(fixture));
    // Only where the model declares the condition the clause reads. Removing a clause from a model
    // that never triggered it would measure a second wrong answer, not this one, and a fixture out
    // of scope must be judged exactly as an unguarded run judges it: that is what makes a failure
    // there a foreign bug rather than this guard's own doing.
    const inScope = guard !== undefined && guard.applies(extraction);
    if (inScope) {
      appliedTo.push(name);
      extraction = guard.remove(extraction);
    }
    compareFixture(name, extraction, report, inScope && guard.byIndex);
  }

  const shape = guard !== undefined && guard.byIndex ? 'vertex by vertex' : 'ring comparison';
  console.log(`  vertices    ${committed.length} fixtures, ${shape} within ${TOLERANCE_M} m`);
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

  if (guard !== undefined) return guardVerdict(guard, report, appliedTo);

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
