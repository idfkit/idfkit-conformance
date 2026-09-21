/**
 * Drive `runners/geometry-check.mjs`'s ring comparison over the shared fixture table.
 *
 * `ring_fixtures.json` is the contract between the two comparators: this file asserts that the
 * JavaScript one returns exactly the errors the table records, and `test_ring.py` asserts the same
 * of `geometry_check.py`. Both harnesses decode the table the same way, so a case added there
 * constrains both implementations.
 *
 * The test bodies below are the Python file's, test for test and in its order, so the two can be
 * read side by side.
 *
 * Run it from the root of the repository:
 *
 *     node --test runners/tests/test-ring.mjs
 */

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

import { TOLERANCE_M, indexError, ringError } from '../geometry-check.mjs';

const HERE = dirname(fileURLToPath(import.meta.url));
const TABLE = JSON.parse(readFileSync(join(HERE, 'ring_fixtures.json'), 'utf8'));

function vertices(raw) {
  return raw.map(([x, y, z]) => [Number(x), Number(y), Number(z)]);
}

for (const kase of TABLE.cases) {
  test(`the error is what the table records: ${kase.name}`, () => {
    const found = ringError(vertices(kase.resolved), vertices(kase.reported));
    assert.ok(Math.abs(found - kase.error) < 1e-9, kase.note ?? `${found} against ${kase.error}`);
  });
}

for (const kase of TABLE.cases) {
  test(`the verdict follows the tolerance: ${kase.name}`, () => {
    const found = ringError(vertices(kase.resolved), vertices(kase.reported));
    assert.equal(found <= TOLERANCE_M, kase.within_tolerance, kase.note ?? '');
  });
}

for (const kase of TABLE.rejected) {
  test(`the table's rejections throw: ${kase.name}`, () => {
    assert.throws(() => ringError(vertices(kase.resolved), vertices(kase.reported)), RangeError);
  });
}

test('the tolerance matches the table', () => {
  // The table states the tolerance it was written against, so a change to one fails the other.
  assert.equal(TOLERANCE_M, TABLE.tolerance_m);
});

test('a rotation passes and its reversal does not', () => {
  /*
   * The asymmetry, asserted directly rather than only through the table.
   *
   * This is the property a later reader will try to simplify away, by minimising over the reversal
   * as well as over the rotations. It reads like a generalisation and it is a hole: it would pass a
   * model resolved inside out, which is exactly what the vertex entry direction clause exists to
   * prevent. The table covers it case by case; this test says it in one sentence so that whoever
   * breaks it reads why.
   */
  const ring = vertices(TABLE.cases[0].reported);
  const rotated = [...ring.slice(1), ring[0]];
  const reversed = [...ring].reverse();

  assert.ok(ringError(rotated, ring) < 1e-9);
  assert.ok(ringError(reversed, ring) > TOLERANCE_M);
});

test('the index comparison sees a rotation the ring comparison does not', () => {
  /*
   * What `--without starting-vertex` rests on, asserted here rather than only on a fixture.
   *
   * The ring comparison exists because the engine renormalises every surface it reports to begin at
   * its upper-left corner while a faithful extractor keeps the author's order, so the two agree on
   * the polygon and differ on where it starts. Comparing by index is the same thing as demanding
   * the extractor reproduce the engine's starting vertex, and this is what that demand costs on one
   * four-metre wall. On the committed model that declares a lower-left start it is 17.59 m.
   */
  const ring = vertices(TABLE.cases[0].reported);
  const rotated = [...ring.slice(1), ring[0]];

  assert.ok(ringError(rotated, ring) < 1e-9);
  assert.ok(indexError(rotated, ring) > TOLERANCE_M);
  assert.ok(indexError(ring, ring) < 1e-9);
});
