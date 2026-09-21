/**
 * Drive `runners/geometry-check.mjs`'s guards over the shared fixture table.
 *
 * `guard_fixtures.json` is the contract between the two guards: this file asserts that the
 * JavaScript removal of the vertex entry direction clause produces exactly the rings the table
 * records, and `test_guard.py` asserts the same of `geometry_check.py`. Both harnesses decode the
 * table the same way, so a case added there constrains both implementations.
 *
 * The test bodies below are the Python file's, test for test and in its order, so the two can be
 * read side by side. They carry more weight here: this runner's library call is not wired up yet,
 * so until it is, these are the only assertions the JavaScript guard has.
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

import { GUARDS, guardVerdict, withoutEntryDirection } from '../geometry-check.mjs';

const HERE = dirname(fileURLToPath(import.meta.url));
const TABLE = JSON.parse(readFileSync(join(HERE, 'guard_fixtures.json'), 'utf8'));

/** One side of a case as the runner's own shape. */
function extraction(kase, key = 'surfaces') {
  return {
    surfaces: kase[key].map((surface) => ({
      name: surface.name,
      vertices: surface.vertices.map(([x, y, z]) => [Number(x), Number(y), Number(z)]),
      parentSurface: surface.parent_surface,
    })),
    entryDirection: kase.entry_direction,
  };
}

for (const kase of TABLE.cases) {
  test(`the clause is removed as the table records: ${kase.name}`, () => {
    const found = withoutEntryDirection(extraction(kase));
    assert.deepEqual(found, extraction(kase, 'expected'), kase.note ?? kase.name);
  });
}

for (const kase of TABLE.cases) {
  /**
   * The clause is its own inverse, which is what makes undoing it on the output exact.
   *
   * If it were not, the guard would be measuring the distance between two wrong answers rather
   * than the distance between the right one and the one a library without the clause would give.
   */
  test(`removing the clause twice removes it never: ${kase.name}`, () => {
    const once = withoutEntryDirection(extraction(kase));
    assert.deepEqual(withoutEntryDirection(once), extraction(kase), kase.note ?? kase.name);
  });
}

for (const kase of TABLE.cases) {
  /**
   * FR-008's line, held on the guard side too.
   *
   * The check's ring comparison is insensitive to where a ring starts, so a guard that reversed
   * the whole list would produce a different starting vertex and nothing downstream would ever say
   * so.
   */
  test(`the first vertex never moves: ${kase.name}`, () => {
    const before = extraction(kase).surfaces;
    const after = withoutEntryDirection(extraction(kase)).surfaces;
    assert.equal(before.length, after.length);
    for (let at = 0; at < before.length; at += 1) {
      assert.deepEqual(after[at].vertices[0], before[at].vertices[0]);
    }
  });
}

test('the guard names the fixture and the magnitude the table records', () => {
  const guard = GUARDS[TABLE.clause];
  assert.equal(guard.failsOn, TABLE.guard.fails_on);
  assert.equal(guard.atLeastM, TABLE.guard.at_least_m);
});

/**
 * The verdict, asserted directly: a clause whose removal changes nothing has not been proven.
 *
 * This is the case a guard that only counted failures would get wrong, and it is not hypothetical:
 * it is what a guard reports the day someone deletes the fixture the clause was written for.
 */
test('a guarded run needs the named fixture to fail', () => {
  const guard = GUARDS[TABLE.clause];
  const report = (failed, worst) => ({
    compared: 8,
    failures: [],
    unresolved: [],
    notes: [],
    failed: new Map(Object.entries(failed)),
    worst: new Map(Object.entries(worst)),
  });

  assert.equal(guardVerdict(guard, report({}, {})), 1);
  assert.equal(guardVerdict(guard, report({ [guard.failsOn]: 8 }, { [guard.failsOn]: 0.004 })), 1);
  assert.equal(
    guardVerdict(
      guard,
      report(
        { [guard.failsOn]: 8, 'relative-zone-origin': 1 },
        { [guard.failsOn]: 4.0, 'relative-zone-origin': 4.0 }
      )
    ),
    1
  );
  assert.equal(guardVerdict(guard, report({ [guard.failsOn]: 8 }, { [guard.failsOn]: 4.0 })), 0);
});
