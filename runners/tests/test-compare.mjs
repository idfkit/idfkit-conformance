/**
 * Drive `runners/compare.mjs` over the shared fixture table.
 *
 * `compare_fixtures.json` is the contract between the two comparators: this file asserts that the
 * JavaScript one returns exactly the verdicts the table records, and `test_compare.py` asserts the
 * same of `compare.py`. Both harnesses decode the table the same way, so a fixture added here
 * constrains both implementations.
 *
 * The test bodies below are the Python file's, test for test and in its order, so the two can be
 * read side by side. Where a test cannot be, the reason is on the test.
 *
 * Run it from the root of the repository:
 *
 *     node --test runners/tests/test-compare.mjs
 */

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

import * as compare from '../compare.mjs';
import { ABSENT, Absent, Comparison, Difference, DifferenceKind } from '../compare.mjs';
import { Assertion, Library, ParseOutcome } from '../model.mjs';

const FIXTURE_FILE = new URL('./compare_fixtures.json', import.meta.url);

// Every rule in compare.md that a fixture can exercise. Rule 6, the encoding, belongs to the
// runner: values reach the comparator already decoded, so the fixtures pin only that a decoded high
// byte compares as itself.
const RULES = [1, 2, 3, 4, 5, 6, 7];

/**
 * Which comparator a fixture drives.
 *
 * @readonly
 * @enum {string}
 */
const Mode = Object.freeze({
  VALUES: 'values',
  UNORDERED: 'unordered',
});

/**
 * @typedef {object} FixtureFields
 * @property {string} id
 * @property {readonly number[]} rules
 * @property {Mode} mode
 * @property {string} path
 * @property {string} why
 * @property {*} left
 * @property {*} right
 * @property {readonly Difference[]} expect
 */

/** One row of the shared table, decoded. */
class Fixture {
  /** @param {FixtureFields} fields */
  constructor({ id, rules, mode, path, why, left, right, expect }) {
    this.id = id;
    this.rules = Object.freeze([...rules]);
    this.mode = mode;
    this.path = path;
    this.why = why;
    this.left = left;
    this.right = right;
    this.expect = Object.freeze([...expect]);
    Object.freeze(this);
  }
}

/**
 * Whether a value is an object `JSON.parse` could have produced.
 *
 * The harness needs its own copy: `compare.mjs` keeps this private, and a test that borrowed the
 * module's own idea of a plain object could not catch the module getting it wrong.
 *
 * @param {*} value
 * @returns {boolean}
 */
function isPlainObject(value) {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/** Read the table, decode the special values, and build a `Fixture` per row. */
function loadFixtures() {
  const document = JSON.parse(readFileSync(FIXTURE_FILE, 'utf8'));
  const specialKey = document.special_key;
  /** @type {Map<string, *>} */
  const specials = new Map([
    ['nan', Number.NaN],
    ['infinity', Number.POSITIVE_INFINITY],
    ['-infinity', Number.NEGATIVE_INFINITY],
    ['absent', ABSENT],
  ]);
  assert.deepStrictEqual(
    [...document.specials].sort(),
    [...specials.keys()].sort(),
    'the table declares a special this harness cannot decode'
  );

  /**
   * @param {*} value
   * @returns {*}
   */
  function decode(value) {
    if (isPlainObject(value)) {
      const keys = Object.keys(value);
      if (keys.length === 1 && keys[0] === specialKey) {
        const name = value[specialKey];
        assert.ok(specials.has(name), `unknown special ${JSON.stringify(name)}`);
        return specials.get(name);
      }
      return Object.fromEntries(keys.map((key) => [key, decode(value[key])]));
    }
    if (Array.isArray(value)) {
      return value.map((member) => decode(member));
    }
    return value;
  }

  const fixtures = document.fixtures.map(
    (row) =>
      new Fixture({
        id: row.id,
        rules: row.rules,
        mode: row.mode,
        path: row.path ?? '',
        why: row.why,
        left: decode(row.left),
        right: decode(row.right),
        expect: row.expect.map(
          (entry) => new Difference(entry.kind, entry.path, decode(entry.left), decode(entry.right))
        ),
      })
  );
  return { fixtures, specialKey, document };
}

const { fixtures: FIXTURES, specialKey: SPECIAL_KEY, document: TABLE } = loadFixtures();

/**
 * Exact structural equality, for checking a verdict against the table.
 *
 * This is not the comparator's equality and must not become it: no tolerance, and NaN matches NaN,
 * because the table records the NaN a comparator reported verbatim.
 *
 * @param {*} left
 * @param {*} right
 * @returns {boolean}
 */
function same(left, right) {
  if (left instanceof Absent || right instanceof Absent) {
    return left === right;
  }
  if (left === null || right === null) {
    return left === null && right === null;
  }
  if (typeof left === 'boolean' || typeof right === 'boolean') {
    return typeof left === 'boolean' && typeof right === 'boolean' && left === right;
  }
  if (typeof left === 'number' && typeof right === 'number') {
    if (Number.isNaN(left) || Number.isNaN(right)) {
      return Number.isNaN(left) && Number.isNaN(right);
    }
    return left === right;
  }
  if (typeof left === 'string' && typeof right === 'string') {
    return left === right;
  }
  if (Array.isArray(left) || Array.isArray(right)) {
    if (!Array.isArray(left) || !Array.isArray(right) || left.length !== right.length) {
      return false;
    }
    return left.every((member, index) => same(member, right[index]));
  }
  if (isPlainObject(left) && isPlainObject(right)) {
    const leftKeys = Object.keys(left);
    const rightKeys = Object.keys(right);
    if (leftKeys.length !== rightKeys.length) {
      return false;
    }
    return leftKeys.every((key) => Object.hasOwn(right, key) && same(left[key], right[key]));
  }
  return false;
}

/**
 * Drive the comparator the fixture names.
 *
 * @param {Fixture} fixture
 * @returns {Comparison}
 */
function run(fixture) {
  if (fixture.mode === Mode.VALUES) {
    return compare.compareValues(fixture.left, fixture.right, { path: fixture.path });
  }
  return compare.compareUnordered(fixture.left, fixture.right, { path: fixture.path });
}

/**
 * Kinds and paths only, so a failure reads before the values are inspected.
 *
 * @param {readonly Difference[]} differences
 * @returns {[string, string][]}
 */
function shape(differences) {
  return differences.map((difference) => [difference.kind, difference.path]);
}

// ---------------------------------------------------------------------------
// The shared table
// ---------------------------------------------------------------------------

for (const fixture of FIXTURES) {
  test(fixture.id, () => {
    // The comparator returns exactly the differences the table records, in the table's order.
    const result = run(fixture);
    assert.deepStrictEqual(shape(result.differences), shape(fixture.expect), fixture.why);
    for (const [index, observed] of result.differences.entries()) {
      const expected = fixture.expect[index];
      assert.ok(
        same(observed.left, expected.left),
        `${fixture.id}: left value at ${expected.path}`
      );
      assert.ok(
        same(observed.right, expected.right),
        `${fixture.id}: right value at ${expected.path}`
      );
    }
    assert.equal(result.equal, fixture.expect.length === 0);
    assert.equal(result.count, fixture.expect.length);
  });
}

test('fixture ids are unique', () => {
  // A duplicated id would let one fixture silently replace another in a report.
  const ids = FIXTURES.map((fixture) => fixture.id);
  assert.equal(ids.length, new Set(ids).size);
});

test('every difference kind is pinned', () => {
  // A kind no fixture produces is a kind the two comparators can disagree about.
  const produced = new Set(
    FIXTURES.flatMap((fixture) => fixture.expect.map((difference) => difference.kind))
  );
  assert.deepStrictEqual([...produced].sort(), Object.values(DifferenceKind).slice().sort());
});

test('every rule is pinned', () => {
  // Every section of compare.md is exercised by at least one fixture.
  const covered = new Set(FIXTURES.flatMap((fixture) => [...fixture.rules]));
  assert.deepStrictEqual(
    [...covered].sort((a, b) => a - b),
    RULES
  );
});

test('both modes are pinned', () => {
  // Both entry points are covered, so neither comparator can ship half the contract.
  const modes = new Set(FIXTURES.map((fixture) => fixture.mode));
  assert.deepStrictEqual([...modes].sort(), Object.values(Mode).slice().sort());
});

test('the float repr case is pinned', () => {
  // The case compare.md cites by name stays in the table, whatever else is edited around it.
  const fixture = FIXTURES.find((item) => item.id === 'float-repr-noise-is-not-a-difference');
  assert.ok(fixture);
  assert.deepStrictEqual([...fixture.expect], []);
  const ascending = (/** @type {number} */ a, /** @type {number} */ b) => a - b;
  assert.deepStrictEqual(Object.values(fixture.left).sort(ascending), [0, 1e-5, 3]);
  assert.deepStrictEqual(Object.values(fixture.right).sort(ascending), [0, 1e-5, 3]);
});

test('no fixture uses the special key literally', () => {
  // The special encoding is only readable while no fixture writes that key for real.
  const names = new Set(TABLE.specials);

  /** @param {*} value */
  function walk(value) {
    if (isPlainObject(value)) {
      if (Object.hasOwn(value, SPECIAL_KEY)) {
        assert.ok(
          Object.keys(value).length === 1 && names.has(value[SPECIAL_KEY]),
          `literal ${SPECIAL_KEY} key in the table`
        );
      }
      for (const member of Object.values(value)) {
        walk(member);
      }
    } else if (Array.isArray(value)) {
      for (const member of value) {
        walk(member);
      }
    }
  }

  for (const row of TABLE.fixtures) {
    walk(row.left);
    walk(row.right);
    walk(row.expect);
  }
});

// ---------------------------------------------------------------------------
// The pieces the table cannot reach
// ---------------------------------------------------------------------------

test('escapeToken', () => {
  // RFC 6901 escaping, tilde before slash, so `~1` never turns into `~01`.
  const cases = [
    ['a', 'a'],
    ['a/b', 'a~1b'],
    ['m~n', 'm~0n'],
    ['~/', '~0~1'],
    ['', ''],
    ['Zone 1', 'Zone 1'],
  ];
  for (const [token, escaped] of cases) {
    assert.equal(compare.escapeToken(token), escaped);
  }
});

test('jsonPointer', () => {
  // A pointer is the tokens, each escaped and each preceded by a slash. No tokens is the root.
  assert.equal(compare.jsonPointer(), compare.ROOT_POINTER);
  assert.equal(compare.ROOT_POINTER, '');
  const pointer = compare.jsonPointer('Zone', 'Zone 1', 'ceiling_height');
  assert.equal(pointer, '/Zone/Zone 1/ceiling_height');
  assert.equal(compare.jsonPointer('a/b'), '/a~1b');
});

test('the tolerance is the documented one', () => {
  // The number in compare.md, not a number near it.
  assert.equal(compare.TOLERANCE, 1e-12);
});

test('a difference renders the path and both values', () => {
  // The exit contract prints the differing value and the path to both sides. The numbers read as
  // JavaScript writes them, `3` where Python writes `3.0`, which is rule 1's own example and
  // decides nothing.
  const difference = new Difference(DifferenceKind.VALUE, '/Zone/Zone 1/ceiling_height', 3.0, 2.4);
  const line = difference.render();
  assert.ok(line.includes('/Zone/Zone 1/ceiling_height'));
  assert.ok(line.includes('3'));
  assert.ok(line.includes('2.4'));
  assert.ok(line.startsWith('value at '));
});

test('a difference renders the root and the absent side by name', () => {
  // An empty pointer and an absent value both have to read as something.
  const difference = new Difference(DifferenceKind.MISSING, '', ABSENT, 1);
  const line = difference.render();
  assert.ok(line.includes('(document root)'));
  assert.ok(line.includes('<absent>'));
  assert.ok(difference.leftIsAbsent && !difference.rightIsAbsent);
});

test('the absent marker names itself when it is interpolated', () => {
  // A runner that drops ABSENT into a template literal must not get `[object Object]`. The Python
  // counterpart gets this from the enum's __str__.
  assert.equal(`${ABSENT}`, '<absent>');
  assert.equal(String(ABSENT), '<absent>');
  assert.ok(ABSENT instanceof Absent);
});

test('a difference truncates long values only in the line', () => {
  // Truncation is a display choice. The instance keeps the value verbatim.
  const value = Array.from({ length: 20 }, () => 'x'.repeat(40));
  const difference = new Difference(DifferenceKind.LENGTH, '/v', value, []);
  const line = difference.render({ maxValueLength: 40 });
  assert.ok(line.includes('characters)'));
  assert.equal(difference.left, value);
});

test('a non-finite number renders by name and never as null', () => {
  // JSON.stringify writes NaN and the infinities as `null`, which is a real JSON value and a
  // different finding. Rule 2 makes those three reportable, so they have to read as themselves.
  const line = new Difference(
    DifferenceKind.VALUE,
    '/v',
    Number.NaN,
    Number.POSITIVE_INFINITY
  ).render();
  assert.ok(line.includes('NaN'));
  assert.ok(line.includes('Infinity'));
  assert.ok(!line.includes('null'));
  const nested = new Difference(DifferenceKind.VALUE, '/v', { a: Number.NaN }, { a: 1 }).render();
  assert.ok(nested.includes('NaN'));
});

test('a comparison reports the total when it truncates', () => {
  // The runner may truncate what it prints, and must print the total count when it does.
  const result = compare.compareValues({ a: 1, b: 2, c: 3 }, { a: 9, b: 9, c: 9 });
  assert.equal(result.count, 3);
  const lines = result.render({ limit: 2 });
  assert.equal(lines.length, 3);
  assert.equal(lines.at(-1), '... 3 differences in total, 2 shown');
  assert.deepStrictEqual(
    [...result.render()],
    result.differences.map((difference) => difference.render())
  );
});

test('the first difference is the first in document order', () => {
  // `first` is a convenience over the same ordering, never a second ordering.
  assert.equal(new Comparison().first, null);
  const result = compare.compareValues({ b: 1, a: 1 }, { b: 2, a: 2 });
  assert.ok(result.first !== null);
  assert.equal(result.first.path, '/b');
});

test('an assertion report prints the whole exit contract', () => {
  // Case id, library, assertion, the differing value, and the path to both sides.
  const result = compare.compareValues(
    { Zone: { 'Zone 1': { ceiling_height: 3.0 } } },
    { Zone: { 'Zone 1': { ceiling_height: 2.4 } } }
  );
  const report = new compare.AssertionReport({
    caseId: 'numeric-zero-against-absent',
    library: Library.TYPESCRIPT,
    assertion: Assertion.EPJSON,
    comparison: result,
  });
  assert.ok(!report.passed);
  const text = report.render().join('\n');
  assert.ok(text.includes('numeric-zero-against-absent'));
  assert.ok(text.includes('typescript'));
  assert.ok(text.includes('epjson'));
  assert.ok(text.includes('/Zone/Zone 1/ceiling_height'));
  assert.ok(text.includes('3') && text.includes('2.4'));
});

test('a passing assertion report is one line', () => {
  // A passing assertion says so and says nothing else.
  const report = new compare.AssertionReport({
    caseId: 'naming-blank-vs-absent',
    library: Library.TYPESCRIPT,
    assertion: Assertion.ROUND_TRIP,
    comparison: new Comparison(),
  });
  assert.ok(report.passed);
  assert.deepStrictEqual(
    [...report.render()],
    ['naming-blank-vs-absent: typescript: round-trip: pass']
  );
});

test('compareOutcome', () => {
  // Assertion 1 compares an outcome, not a value, and reports through the same shape.
  assert.ok(compare.compareOutcome(ParseOutcome.SUCCESS, ParseOutcome.SUCCESS).equal);
  const result = compare.compareOutcome(ParseOutcome.SUCCESS, ParseOutcome.FAILURE);
  assert.deepStrictEqual(shape(result.differences), [['value', '']]);
  assert.equal(result.differences[0].left, 'success');
  assert.equal(result.differences[0].right, 'failure');
});

test('epjson and documents are the same comparison', () => {
  // Rule 4 lives in what the runner hands over, not in a second set of rules.
  const left = { Zone: [{ name: 'Zone 1' }, { name: null }] };
  const right = { Zone: [{ name: 'Zone 1' }] };
  assert.deepStrictEqual(
    shape(compare.compareEpjson(left, right).differences),
    shape(compare.compareDocuments(left, right).differences)
  );
});

test('a value that is not parsed JSON throws', () => {
  // A runner bug is thrown, never reported as a finding about a library. The Python counterpart
  // passes bytes; the JavaScript shapes JSON.parse never produces are a typed array, a Date, a Map
  // and undefined.
  assert.throws(
    () => compare.compareValues({ a: new Uint8Array([1]) }, { a: 'bytes' }),
    (error) => error instanceof compare.ComparisonError && String(error.message).includes('/a')
  );
  for (const value of [new Date(0), new Map(), undefined, 1n]) {
    assert.throws(() => compare.compareValues({ a: value }, { a: 'x' }), compare.ComparisonError);
  }
  // Python rejects a non-string key. JavaScript coerces an integer key to a string, so the shape
  // that stays invisible to the walk is a symbol key, and that is what is rejected here.
  const symbolKeyed = { [Symbol('code')]: 1 };
  assert.throws(() => compare.compareValues(symbolKeyed, {}), compare.ComparisonError);
});

test('deeply nested values are compared all the way down', () => {
  // Recursion is not depth limited, and a difference at the bottom is still reported.
  const depth = 40;
  /** @type {*} */
  let left = 1;
  /** @type {*} */
  let right = 2;
  for (let index = 0; index < depth; index += 1) {
    left = { child: left };
    right = { child: right };
  }
  const result = compare.compareValues(left, right);
  assert.deepStrictEqual(shape(result.differences), [['value', '/child'.repeat(depth)]]);
});

// ---------------------------------------------------------------------------
// The hazards that exist only on this side
//
// Each of these is a way JavaScript would diverge from compare.py by default. The table cannot
// reach them, because a fixture that pinned one would be asserting a language quirk rather than a
// rule, so they are pinned here instead.
// ---------------------------------------------------------------------------

test('a key named after an Object.prototype member is compared like any other', () => {
  // `'toString' in {}` is true, so a comparator using `in` for membership would report no missing
  // and no extra here. Python's `in` on a dict has no such prototype chain to walk.
  const missing = compare.compareValues({}, { toString: 1, constructor: 2 });
  assert.deepStrictEqual(shape(missing.differences), [
    ['missing', '/constructor'],
    ['missing', '/toString'],
  ]);
  const extra = compare.compareValues({ toString: 1 }, {});
  assert.deepStrictEqual(shape(extra.differences), [['extra', '/toString']]);
  const equal = compare.compareValues({ hasOwnProperty: 1 }, { hasOwnProperty: 1 });
  assert.ok(equal.equal);
});

test('missing keys sort by code point, not by UTF-16 code unit', () => {
  // Python's `sorted` orders by code point. JavaScript's default sort orders by code unit, which
  // puts every character above U+FFFF below U+E000 because its first code unit is a high
  // surrogate. The missing-key order is part of the contract the two comparators are diffed on.
  const result = compare.compareValues({}, { '\u{1F600}': 1, '': 2 });
  assert.deepStrictEqual(shape(result.differences), [
    ['missing', '/'],
    ['missing', '/\u{1F600}'],
  ]);
});

test('an object key set is compared, whatever JSON.parse made of the values', () => {
  // JSON.parse defines `__proto__` as an own data property rather than setting the prototype, so a
  // key by that name reaches the walk like any other. An object literal would not, which is why
  // this test parses its input.
  const left = JSON.parse('{"__proto__": 1}');
  const right = JSON.parse('{"__proto__": 2}');
  assert.deepStrictEqual(shape(compare.compareValues(left, right).differences), [
    ['value', '/__proto__'],
  ]);
});

test('a boolean does not pair with the string it serializes to', () => {
  // `JSON.stringify(true)` and the string `'true'` are the same text, so a pairing keyed on an
  // element's JSON text would merge these two. Python has the same hazard through `str`, and both
  // sides avoid it by keying on a structural form that carries the JSON type.
  const result = compare.compareUnordered([true], ['true']);
  assert.deepStrictEqual(shape(result.differences), [
    ['unmatched', ''],
    ['unmatched', ''],
  ]);
  assert.equal(result.differences[0].left, true);
  assert.equal(result.differences[1].right, 'true');
});

test('unmatched elements are reported left side first, each in its own document order', () => {
  // The table has no collection with two unmatched elements on the same side, so the ordering
  // half of rule 7's reporting contract is pinned here. Sorting either side, or reporting the
  // right side first, changes this list.
  const result = compare.compareUnordered(['bravo', 'alpha'], ['delta', 'charlie']);
  assert.deepStrictEqual(
    result.differences.map((difference) => [
      difference.leftIsAbsent ? 'right' : 'left',
      difference.leftIsAbsent ? difference.right : difference.left,
    ]),
    [
      ['left', 'bravo'],
      ['left', 'alpha'],
      ['right', 'delta'],
      ['right', 'charlie'],
    ]
  );
});
