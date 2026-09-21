/**
 * Drive `runners/geometry-check.mjs`'s guards over the shared fixture table.
 *
 * `guard_fixtures.json` is the contract between the two runners' guards: this file asserts that the
 * JavaScript removal of each clause produces exactly the rings the table records, and
 * `test_guard.py` asserts the same of `geometry_check.py`. Both harnesses decode the table the same
 * way, so a case added there constrains both implementations.
 *
 * The test bodies below are the Python file's, test for test and in its order, so the two can be
 * read side by side.
 *
 * Run it from the root of the repository:
 *
 *     node --test runners/tests/test-guard.mjs
 */

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

import { GUARDS, guardVerdict } from '../geometry-check.mjs';

const HERE = dirname(fileURLToPath(import.meta.url));
const TABLE = JSON.parse(readFileSync(join(HERE, 'guard_fixtures.json'), 'utf8'));
const CLAUSES = TABLE.clauses;
const TOLERANCE_M = Number(TABLE.tolerance_m);

/**
 * Every case in the table, each carrying the clause it belongs to, so that one loop covers all four
 * guards and a clause added to the table is picked up without a code change here.
 */
const CASES = Object.keys(CLAUSES)
  .sort()
  .flatMap((clause) => CLAUSES[clause].cases.map((kase) => [clause, kase]));

/**
 * The cases whose clause is in scope. `remove` is defined only where `applies` holds, because that
 * is the only place the runner calls it: removing a clause from a model that never triggered it
 * would apply the clause a second time rather than undo it. The out-of-scope cases are asserted by
 * the table-consistency test below instead.
 */
const APPLYING = CASES.filter(([, kase]) => kase.applies);

/**
 * One side of a case as the runner's own shape.
 *
 * A case states only the declarations its clause reads; the rest take the values a model that
 * declares nothing is read as, which is what the runner's own defaults are for.
 */
function extraction(kase, key = 'surfaces') {
  const declares = kase.declares ?? {};
  const origins = new Map(
    Object.entries(kase.zone_origins ?? {}).map(([name, [x, y, z]]) => [name, [Number(x), Number(y), Number(z)]])
  );
  return {
    surfaces: kase[key].map((surface) => ({
      name: surface.name,
      vertices: surface.vertices.map(([x, y, z]) => [Number(x), Number(y), Number(z)]),
      parentSurface: surface.parent_surface,
      zone: surface.zone ?? '',
    })),
    entryDirection: declares.entry_direction ?? 'Counterclockwise',
    coordinateSystem: declares.coordinate_system ?? 'Relative',
    startingVertexPosition: declares.starting_vertex_position ?? 'UpperLeftCorner',
    northAxis: Number(declares.north_axis ?? 0),
    zoneOrigins: origins,
  };
}

/**
 * The same extraction within a nanometre.
 *
 * Compared rather than equated because two of the four clauses are undone by a rotation, and a
 * rotation through a right angle is not exact in binary floating point. Everything about a surface
 * that is not a coordinate is still equated exactly.
 */
function assertClose(found, wanted, note) {
  assert.deepEqual(
    found.surfaces.map((s) => s.name),
    wanted.surfaces.map((s) => s.name),
    note
  );
  for (let at = 0; at < wanted.surfaces.length; at += 1) {
    const one = found.surfaces[at];
    const other = wanted.surfaces[at];
    assert.equal(one.parentSurface, other.parentSurface, note);
    assert.equal(one.zone, other.zone, note);
    assert.equal(one.vertices.length, other.vertices.length, note);
    for (let vertex = 0; vertex < other.vertices.length; vertex += 1) {
      for (let axis = 0; axis < 3; axis += 1) {
        const gap = Math.abs(one.vertices[vertex][axis] - other.vertices[vertex][axis]);
        assert.ok(gap <= TOLERANCE_M, `${note}: ${one.vertices[vertex]} against ${other.vertices[vertex]}`);
      }
    }
  }
}

for (const [clause, kase] of APPLYING) {
  test(`the clause is removed as the table records: ${clause}: ${kase.name}`, () => {
    assertClose(GUARDS[clause].remove(extraction(kase)), extraction(kase, 'expected'), kase.note ?? kase.name);
  });
}

for (const [clause, kase] of CASES) {
  /**
   * The half of a guard that decides which models it touches at all.
   *
   * A guard that removed its clause everywhere would measure the distance between two wrong answers
   * on every model that never declared the condition, and the fixtures that came out wrong would
   * read as the clause firing where it should not.
   */
  test(`the clause is in scope only where the model declares it: ${clause}: ${kase.name}`, () => {
    assert.equal(GUARDS[clause].applies(extraction(kase)), kase.applies, kase.note ?? kase.name);
  });
}

for (const [clause, kase] of APPLYING) {
  /**
   * A clause removed changes where surfaces are, never which ones or how many vertices each has.
   *
   * A guard that dropped or added a surface would fail the named fixture for a reason that has
   * nothing to do with the clause, and the run would report the clause as load-bearing anyway.
   */
  test(`a guard never changes which surfaces there are: ${clause}: ${kase.name}`, () => {
    const before = extraction(kase);
    const after = GUARDS[clause].remove(before);
    assert.deepEqual(
      after.surfaces.map((s) => [s.name, s.vertices.length]),
      before.surfaces.map((s) => [s.name, s.vertices.length])
    );
  });
}

for (const [clause, kase] of APPLYING.filter(([name]) => CLAUSES[name].involution)) {
  /**
   * The clause is its own inverse, which is what makes undoing it on the output exact.
   *
   * If it were not, the guard would be measuring the distance between two wrong answers rather than
   * the distance between the right one and the one a library without the clause would give. Only
   * the entry direction claims this: undoing a rotation twice turns the building through twice the
   * axis.
   */
  test(`removing the clause twice removes it never: ${clause}: ${kase.name}`, () => {
    const once = GUARDS[clause].remove(extraction(kase));
    assertClose(GUARDS[clause].remove(once), extraction(kase), kase.note ?? kase.name);
  });
}

for (const [clause, kase] of APPLYING.filter(([name]) => CLAUSES[name].holds_the_head)) {
  /**
   * FR-008's line, held on the guard side too.
   *
   * The check's ring comparison is insensitive to where a ring starts, so a guard that reversed the
   * whole list would produce a different starting vertex and nothing downstream would ever say so.
   */
  test(`the first vertex never moves: ${clause}: ${kase.name}`, () => {
    const after = GUARDS[clause].remove(extraction(kase)).surfaces;
    const before = extraction(kase).surfaces;
    for (let at = 0; at < before.length; at += 1) {
      assert.deepEqual(after[at].vertices[0], before[at].vertices[0]);
    }
  });
}

for (const [clause, kase] of CASES.filter(([, k]) => !k.applies)) {
  /**
   * A model the clause never touched reaches the comparison unchanged, and the table says so.
   *
   * Asserted on the table rather than on the guard, because the guard is never called for these:
   * the runner asks `applies` first. What could go wrong is the table recording a mutation for a
   * case the runner will never mutate, which would read as a guarantee nothing enforces.
   */
  test(`a model out of scope is compared as the library returned it: ${clause}: ${kase.name}`, () => {
    assert.deepEqual(kase.expected, kase.surfaces, kase.note ?? kase.name);
  });
}

for (const clause of Object.keys(CLAUSES).sort()) {
  /** The recorded measurement is part of the contract, not a threshold either side may relax. */
  test(`the guard names the fixture and the magnitude the table records: ${clause}`, () => {
    const guard = GUARDS[clause];
    assert.equal(guard.name, clause);
    assert.equal(guard.failsOn, CLAUSES[clause].guard.fails_on);
    assert.equal(guard.atLeastM, CLAUSES[clause].guard.at_least_m);
  });
}

/** A guard added to one and not the other is the failure this catches. */
test('the table and the runner name the same guards', () => {
  assert.deepEqual(Object.keys(GUARDS).sort(), Object.keys(CLAUSES).sort());
});

/**
 * The three resolution clauses are removed from the answer; the fourth from the question.
 *
 * Asserted rather than left to a reader, because a guard that quietly compared by index would make
 * every other guard's magnitude a measurement of something else.
 */
test('only the starting vertex guard changes the comparison', () => {
  const byIndex = Object.keys(GUARDS)
    .filter((name) => GUARDS[name].byIndex)
    .sort();
  assert.deepEqual(byIndex, ['starting-vertex']);
});

/** The runner's own report shape, built here so the verdict can be asserted directly. */
function report(failed, worst) {
  return {
    compared: 8,
    failures: [],
    unresolved: [],
    notes: [],
    failed: new Map(Object.entries(failed)),
    worst: new Map(Object.entries(worst)),
  };
}

/**
 * The verdict, asserted directly: a clause whose removal changes nothing has not been proven.
 *
 * This is the case a guard that only counted failures would get wrong, and it is not hypothetical:
 * it is what a guard reports the day someone deletes the fixture the clause was written for.
 */
test('a guarded run needs the named fixture to fail', () => {
  const guard = GUARDS['entry-direction'];
  const scope = [guard.failsOn];

  assert.equal(guardVerdict(guard, report({}, {}), scope), 1);
  assert.equal(guardVerdict(guard, report({ [guard.failsOn]: 8 }, { [guard.failsOn]: 0.004 }), scope), 1);
  assert.equal(
    guardVerdict(
      guard,
      report({ [guard.failsOn]: 8, 'relative-zone-origin': 1 }, { [guard.failsOn]: 4.0, 'relative-zone-origin': 4.0 }),
      scope
    ),
    1
  );
  assert.equal(guardVerdict(guard, report({ [guard.failsOn]: 8 }, { [guard.failsOn]: 4.0 }), scope), 0);
});

/**
 * Two fixtures declare a non-zero north axis, and both fail when the clause is removed.
 *
 * The verdict must call that the clause doing its job twice rather than a second bug, and must go
 * on refusing a fixture that declares no such thing. Both halves are asserted here, because a rule
 * loosened to admit the first would otherwise admit the second by accident.
 */
test('a second fixture declaring the clause may fail alongside the named one', () => {
  const guard = GUARDS['north-axis'];
  const both = report(
    { 'north-axis-multizone': 34, 'clockwise-entry': 8 },
    { 'north-axis-multizone': 22.5571, 'clockwise-entry': 5.71 }
  );
  assert.equal(guardVerdict(guard, both, ['north-axis-multizone', 'clockwise-entry']), 0);
  assert.equal(guardVerdict(guard, both, ['north-axis-multizone']), 1);
});
