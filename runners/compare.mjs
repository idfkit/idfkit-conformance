/**
 * The value comparator, a direct transcription of `runners/compare.md`.
 *
 * `compare.md` is normative and this module implements it, section by section, under the same
 * names. `compare.py` is the mirror of this file in Python, and
 * `runners/tests/compare_fixtures.json` is the shared table both are tested against. Nothing here
 * decides anything `compare.md` leaves unstated: a case that raises a question the rules do not
 * answer is an amendment to that file first, and a change to both comparators in the same change.
 *
 * NAMING: the Python module is snake_case and this one is camelCase, the same one to one mapping
 * `model.mjs` uses. `compare_values` is `compareValues`, `escape_token` is `escapeToken`, and so
 * on. The enum members, the difference kinds and the report strings are byte for byte identical,
 * because those are the output a maintainer diffs across the two languages.
 *
 * Where each rule lives:
 *
 *   Rule 1, values not text: `compareValues` walks parsed values. This module never serializes to
 *     decide a verdict. The only formatting is in `Difference.render`, which produces a report line
 *     and decides nothing, and `pairingKey` builds a structural key rather than a string for the
 *     same reason.
 *   Rule 2, numbers: `numbersEqual`.
 *   Rule 3, object key order: `compareObject`.
 *   Rule 4, IDF field order: `compareDocuments`, which differs from `compareEpjson` only in what
 *     the runner hands it.
 *   Rule 5, strings: `compareScalar`, one `!==`, no normalisation of any kind.
 *   Rule 6, encoding: the runner's, not this module's. Values arrive already decoded.
 *   Rule 7, unordered collections: `compareUnordered`.
 *
 * Orientation, fixed and never swapped: `left` is the library under test, `right` is the
 * expectation. A `missing` therefore always means the library omitted something.
 *
 * Difference ordering, fixed so the two comparators can be diffed against each other: document
 * order of the left side, and keys absent from the left visited sorted by code point, after the
 * left walk of the object that holds them.
 *
 * Two readings `compare.py` had to settle, carried here unchanged so that overturning one is an
 * edit to `compare.md` and then to both comparators:
 *
 *   Rule 7 says unmatched elements are reported "under rule 3's missing and extra kinds", while
 *     the reporting table defines `unmatched` for exactly this situation. Rule 3's kinds govern
 *     object key sets, the first unordered collection, and `unmatched` governs every other
 *     unordered collection, the second. The alternative leaves `unmatched` unreachable in both
 *     comparators, which cannot be what a normative table means.
 *   Rule 2 makes NaN and the infinities always a difference. Rule 7 pairs unordered elements by
 *     exact equality, and exact equality would pair infinity with infinity. Rule 2 wins: an
 *     element carrying NaN or an infinity anywhere inside it pairs with nothing, so it is reported
 *     on both sides.
 *
 * Four places where JavaScript and Python do not agree by themselves. The first two are handled
 * here, the last two cannot be and are stated so that handling them is a visible decision:
 *
 *   HANDLED, key sorting. Python's `sorted` orders strings by code point and JavaScript's default
 *     sort orders them by UTF-16 code unit. The two disagree above U+FFFF, where a surrogate pair
 *     sorts below U+E000 in UTF-16 order and above it in code point order. `codePointOrder` below
 *     sorts by code point, so the two comparators emit missing keys in the same order whatever the
 *     corpus later contains. Rule 6 confines today's corpus to latin-1, where the two orders
 *     already agree, so this is insurance rather than a fix.
 *   HANDLED, prototype chains. Python's `key in mapping` asks about the mapping and JavaScript's
 *     `key in object` walks the prototype chain, so `'toString' in {}` is true. Every membership
 *     test below is `Object.hasOwn`. A comparator using `in` would report no `missing` for a key
 *     named after an `Object.prototype` member.
 *   UNHANDLED, integer-like keys. JavaScript visits integer-like object keys first, in ascending
 *     numeric order, ahead of the insertion-ordered rest, and `JSON.parse` cannot preserve the
 *     document order it read. An epJSON object named `3` would therefore be visited in a different
 *     order by the two comparators, and their difference lists would not diff. The corpus has no
 *     such key and `compare_fixtures.json` says it may not acquire one.
 *   UNHANDLED, integer width. Python parses a JSON integer too large for a double as an exact
 *     `int`, JavaScript rounds it to the nearest double at parse time. The two comparators would
 *     then be judging different values, and nothing inside a comparator can recover what the
 *     parser discarded. No case in the corpus carries an integer beyond 2**53.
 *
 * `Difference.render` is display only and its output is deliberately not pinned across the two
 * languages: Python prints `3.0` where JavaScript prints `3`, which is rule 1's own example of two
 * JSON texts for one JSON value. Verdicts are pinned, formatting is not.
 */

/** @typedef {import('./model.mjs').Assertion} Assertion */
/** @typedef {import('./model.mjs').Library} Library */
/** @typedef {import('./model.mjs').ParseOutcome} ParseOutcome */

// The relative tolerance of rule 2. There is no absolute floor, deliberately: exact zero is equal
// only to exact zero, because an unset field read as 0 instead of omitted is a real bug.
export const TOLERANCE = 1e-12;

// RFC 6901 names the whole document with the empty string.
export const ROOT_POINTER = '';

// ---------------------------------------------------------------------------
// Enumerations
// ---------------------------------------------------------------------------

/**
 * The six kinds in `compare.md`'s reporting table, in the order that table lists them.
 *
 * @readonly
 * @enum {string}
 */
export const DifferenceKind = Object.freeze({
  VALUE: 'value',
  TYPE: 'type',
  MISSING: 'missing',
  EXTRA: 'extra',
  LENGTH: 'length',
  UNMATCHED: 'unmatched',
});

/**
 * The six JSON types. A `type` difference is a disagreement between two of these.
 *
 * `BOOLEAN` is separate from `NUMBER` and `STRING` because rule 2 says so: `true` against `1` is a
 * difference, and `"3"` against `3` is a difference.
 *
 * @readonly
 * @enum {string}
 */
export const JsonType = Object.freeze({
  NULL: 'null',
  BOOLEAN: 'boolean',
  NUMBER: 'number',
  STRING: 'string',
  ARRAY: 'array',
  OBJECT: 'object',
});

/**
 * The marker for "there is no value at this path on this side".
 *
 * Rule 3 is explicit that absent is not `null`, so the two cannot share a representation. A
 * difference carrying `ABSENT` on one side is a `missing`, an `extra`, or an `unmatched`.
 *
 * The Python counterpart is a single-member enum. Here it is a class with one instance, so that
 * `instanceof` is the membership test on both sides and no ordinary value can impersonate it.
 */
export class Absent {
  constructor() {
    /** @type {string} */
    this.token = 'absent';
    Object.freeze(this);
  }

  /** @returns {string} */
  toString() {
    return '<absent>';
  }
}

/** The one instance. Compare against it with `===` or with `instanceof Absent`. */
export const ABSENT = new Absent();

/**
 * A value reached the comparator that is not a parsed JSON value.
 *
 * Thrown rather than reported, because it is a runner bug and not a finding about a library.
 */
export class ComparisonError extends Error {
  /** @param {string} message */
  constructor(message) {
    super(message);
    this.name = 'ComparisonError';
  }
}

// ---------------------------------------------------------------------------
// RFC 6901 JSON Pointers
// ---------------------------------------------------------------------------

/**
 * Escape one pointer token: `~` becomes `~0` and `/` becomes `~1`, in that order.
 *
 * @param {string} token
 * @returns {string}
 */
export function escapeToken(token) {
  return token.replaceAll('~', '~0').replaceAll('/', '~1');
}

/**
 * Build a pointer from tokens. No tokens is the document root, the empty string.
 *
 * @param {...string} tokens
 * @returns {string}
 */
export function jsonPointer(...tokens) {
  return tokens.map((token) => `/${escapeToken(token)}`).join('');
}

/**
 * The pointer to `token` inside the value at `path`.
 *
 * @param {string} path
 * @param {string} token
 * @returns {string}
 */
function child(path, token) {
  return `${path}/${escapeToken(token)}`;
}

// ---------------------------------------------------------------------------
// The reported shapes
// ---------------------------------------------------------------------------

/**
 * @typedef {object} DifferenceFields
 * @property {DifferenceKind} kind
 * @property {string} path
 * @property {*} left
 * @property {*} right
 */

/**
 * One reported disagreement: a kind, a path, and both sides' values.
 *
 * `left` and `right` hold the values verbatim as parsed, never reformatted, so the exit contract's
 * "the differing value" is the value the library actually produced. Either may be `ABSENT`.
 * Formatting happens in `render` and nowhere else.
 */
export class Difference {
  /**
   * @param {DifferenceKind} kind
   * @param {string} path
   * @param {*} left
   * @param {*} right
   */
  constructor(kind, path, left, right) {
    /** @type {DifferenceKind} */
    this.kind = kind;
    /** @type {string} */
    this.path = path;
    /** @type {*} */
    this.left = left;
    /** @type {*} */
    this.right = right;
    Object.freeze(this);
  }

  /** Whether the left side has no value at this path. */
  get leftIsAbsent() {
    return this.left instanceof Absent;
  }

  /** Whether the right side has no value at this path. */
  get rightIsAbsent() {
    return this.right instanceof Absent;
  }

  /**
   * One report line: the kind, the path to both sides, and both values.
   *
   * Truncating a long value here is a display choice and affects no verdict. The verbatim values
   * stay on the instance.
   *
   * @param {{ maxValueLength?: number }} [options]
   * @returns {string}
   */
  render({ maxValueLength = 160 } = {}) {
    const location = this.path ? this.path : '(document root)';
    const left = renderValue(this.left, maxValueLength);
    const right = renderValue(this.right, maxValueLength);
    return `${this.kind} at ${location}: left ${left}, right ${right}`;
  }
}

/**
 * Every difference between one pair of values, in the deterministic order of `compare.md`.
 *
 * The comparator returns them all, never just the first. Truncation is the runner's choice, and
 * `render` prints the total count whenever it truncates.
 */
export class Comparison {
  /** @param {readonly Difference[]} [differences] */
  constructor(differences = []) {
    /** @type {readonly Difference[]} */
    this.differences = Object.freeze([...differences]);
    Object.freeze(this);
  }

  /** Whether the two sides agree under every rule. */
  get equal() {
    return this.differences.length === 0;
  }

  /** How many differences were found. */
  get count() {
    return this.differences.length;
  }

  /** The first difference in document order, or `null` when the sides agree. */
  get first() {
    return this.differences.length > 0 ? this.differences[0] : null;
  }

  /**
   * Report lines, at most `limit` of them, with a total count appended when truncated.
   *
   * @param {{ limit?: number | null, maxValueLength?: number }} [options]
   * @returns {readonly string[]}
   */
  render({ limit = null, maxValueLength = 160 } = {}) {
    const shown = limit === null ? this.differences : this.differences.slice(0, limit);
    const lines = shown.map((difference) => difference.render({ maxValueLength }));
    if (shown.length < this.count) {
      lines.push(`... ${this.count} differences in total, ${shown.length} shown`);
    }
    return Object.freeze(lines);
  }
}

/**
 * @typedef {object} AssertionReportFields
 * @property {string} caseId
 * @property {Library} library
 * @property {Assertion} assertion
 * @property {Comparison} comparison
 */

/**
 * One assertion's result for one case and one library: everything the exit contract prints.
 *
 * The contract asks for the case id, the library, the assertion, the differing value, and the path
 * to both sides. The first three are fields here, and the last two come from every `Difference` in
 * the comparison.
 */
export class AssertionReport {
  /** @param {AssertionReportFields} fields */
  constructor({ caseId, library, assertion, comparison }) {
    /** @type {string} */
    this.caseId = caseId;
    /** @type {Library} */
    this.library = library;
    /** @type {Assertion} */
    this.assertion = assertion;
    /** @type {Comparison} */
    this.comparison = comparison;
    Object.freeze(this);
  }

  /** Whether the assertion held. */
  get passed() {
    return this.comparison.equal;
  }

  /**
   * The failure block for this assertion, or a single pass line.
   *
   * @param {{ limit?: number | null, maxValueLength?: number }} [options]
   * @returns {readonly string[]}
   */
  render({ limit = null, maxValueLength = 160 } = {}) {
    const head = `${this.caseId}: ${this.library}: ${this.assertion}`;
    if (this.passed) {
      return Object.freeze([`${head}: pass`]);
    }
    const lines = this.comparison.render({ limit, maxValueLength });
    return Object.freeze([
      `${head}: ${this.comparison.count} difference(s), left is ${this.library}`,
      ...lines.map((line) => `  ${line}`),
    ]);
  }
}

// ---------------------------------------------------------------------------
// Rule 1: compare parsed JSON values, never JSON text
// ---------------------------------------------------------------------------

/**
 * Compare two parsed values under every rule in `compare.md`.
 *
 * `left` is the library under test and `right` is the expectation. `path` is the pointer the two
 * values already sit at, for a caller comparing a subtree.
 *
 * @param {*} left
 * @param {*} right
 * @param {{ path?: string }} [options]
 * @returns {Comparison}
 */
export function compareValues(left, right, { path = ROOT_POINTER } = {}) {
  /** @type {Difference[]} */
  const differences = [];
  compare(left, right, path, differences);
  return new Comparison(differences);
}

/**
 * Assertion 2: the library's canonical epJSON against `expected.epJSON`.
 *
 * Rule 4 does not apply here. An epJSON field is a named key and its order is not observable, so
 * rule 3 governs in full and comparing key order would test the oracle's key order, which is not a
 * claim either library makes.
 *
 * @param {*} left
 * @param {*} right
 * @param {{ path?: string }} [options]
 * @returns {Comparison}
 */
export function compareEpjson(left, right, { path = ROOT_POINTER } = {}) {
  return compareValues(left, right, { path });
}

/**
 * Assertion 3: a document re-parsed from the library's own IDF output against the original.
 *
 * Rule 4 applies here and only here: a parsed document retains, per object, the sequence of its
 * fields as written, and that sequence is compared position by position, including trailing unset
 * fields and the boundaries of extensible groups. This function enforces nothing extra to achieve
 * that. It is the runner's job to hand over a value in which the field sequence is an array,
 * because rule 7 makes every array ordered and compared index by index. A runner that flattened the
 * field sequence into an object would throw the order away before the comparator ever saw it.
 *
 * @param {*} left
 * @param {*} right
 * @param {{ path?: string }} [options]
 * @returns {Comparison}
 */
export function compareDocuments(left, right, { path = ROOT_POINTER } = {}) {
  return compareValues(left, right, { path });
}

/**
 * Assertion 1: the declared parse outcome against the observed one.
 *
 * Assertion 1 compares an outcome, not a value, and uses no rule but rule 6, which the runner
 * applies when it reads the input. It is here so both runners report through one shape.
 *
 * @param {ParseOutcome} left
 * @param {ParseOutcome} right
 * @returns {Comparison}
 */
export function compareOutcome(left, right) {
  if (left === right) {
    return new Comparison();
  }
  return new Comparison([new Difference(DifferenceKind.VALUE, ROOT_POINTER, left, right)]);
}

/**
 * Dispatch on JSON type, then apply the rule for that type.
 *
 * @param {*} left
 * @param {*} right
 * @param {string} path
 * @param {Difference[]} out
 */
function compare(left, right, path, out) {
  const leftType = jsonTypeOf(left, path, 'left');
  const rightType = jsonTypeOf(right, path, 'right');
  if (leftType !== rightType) {
    out.push(new Difference(DifferenceKind.TYPE, path, left, right));
    return;
  }
  if (leftType === JsonType.OBJECT) {
    compareObject(left, right, path, out);
  } else if (leftType === JsonType.ARRAY) {
    compareArray(left, right, path, out);
  } else {
    compareScalar(left, right, leftType, path, out);
  }
}

/**
 * Whether a value is an object `JSON.parse` could have produced.
 *
 * A `Map`, a `Date` or a class instance is not: `Object.keys` on one of those returns a key set
 * that has nothing to do with the value, so a comparator that accepted them would silently report
 * two unrelated values as equal. Python rejects the same shapes by asking for a `Mapping`.
 *
 * @param {*} value
 * @returns {boolean}
 */
function isPlainObject(value) {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    return false;
  }
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

/**
 * The JSON type of a parsed value.
 *
 * `typeof null` is `'object'`, so null is tested first. Arrays are tested after plain objects
 * because `isPlainObject` already excludes them, which keeps the order the same as `compare.py`'s
 * Mapping-before-Sequence test. Anything else, `undefined` and a `BigInt` included, is not
 * something `JSON.parse` produces, so reaching here with one is a runner bug.
 *
 * @param {*} value
 * @param {string} path
 * @param {string} side
 * @returns {JsonType}
 */
function jsonTypeOf(value, path, side) {
  if (value === null) {
    return JsonType.NULL;
  }
  if (typeof value === 'boolean') {
    return JsonType.BOOLEAN;
  }
  if (typeof value === 'number') {
    return JsonType.NUMBER;
  }
  if (typeof value === 'string') {
    return JsonType.STRING;
  }
  if (isPlainObject(value)) {
    return JsonType.OBJECT;
  }
  if (Array.isArray(value)) {
    return JsonType.ARRAY;
  }
  const location = path ? path : '(document root)';
  throw new ComparisonError(
    `${side} value at ${location} is a ${describeType(value)}, which is not a parsed JSON value. ` +
      'Rule 1 compares parsed values, so the runner parses before it compares'
  );
}

/**
 * A short name for a value's type, for a `ComparisonError` message.
 *
 * @param {*} value
 * @returns {string}
 */
function describeType(value) {
  if (value === undefined) {
    return 'undefined';
  }
  if (typeof value !== 'object') {
    return typeof value;
  }
  const name = value.constructor?.name;
  return typeof name === 'string' && name.length > 0 ? name : 'object';
}

// ---------------------------------------------------------------------------
// Rules 2 and 5: scalars
// ---------------------------------------------------------------------------

/**
 * Compare two scalars of the same JSON type.
 *
 * @param {*} left
 * @param {*} right
 * @param {JsonType} type
 * @param {string} path
 * @param {Difference[]} out
 */
function compareScalar(left, right, type, path, out) {
  if (type === JsonType.NULL) {
    return; // null equals null, and rule 3 keeps absent out of this branch entirely.
  }
  if (type === JsonType.NUMBER) {
    if (!numbersEqual(left, right)) {
      out.push(new Difference(DifferenceKind.VALUE, path, left, right));
    }
    return;
  }
  // Rule 5 for strings, exact sequences of scalar values, no case folding, no trimming, no
  // collapsing of internal whitespace, no Unicode normalisation, no normalisation of line endings.
  // Booleans take the same single comparison.
  if (left !== right) {
    out.push(new Difference(DifferenceKind.VALUE, path, left, right));
  }
}

/**
 * Rule 2. Integer against float is not a difference, which JavaScript gets for free. The tolerance
 * is relative only.
 *
 * NaN and the infinities are always differences, including NaN against NaN and infinity against
 * infinity: JSON cannot carry them, so one reaching the comparator is itself the finding.
 * `Number.isFinite` is false for all three, so the first test covers every one of those cases,
 * including the tolerance branch that would otherwise evaluate `Infinity - Infinity`.
 *
 * @param {number} left
 * @param {number} right
 * @returns {boolean}
 */
function numbersEqual(left, right) {
  if (!Number.isFinite(left) || !Number.isFinite(right)) {
    return false;
  }
  if (left === right) {
    // Covers zero against zero, and either sign of zero, exactly.
    return true;
  }
  // Two finite values far enough apart overflow to Infinity here, which no tolerance can close, so
  // the comparison below is false and the two differ. That is the verdict `compare.py` reaches
  // through its OverflowError branch.
  return Math.abs(left - right) <= TOLERANCE * Math.max(Math.abs(left), Math.abs(right));
}

// ---------------------------------------------------------------------------
// Rule 3: object key order is not compared
// ---------------------------------------------------------------------------

/**
 * Compare two objects as key sets plus per-key values.
 *
 * Left keys are visited in document order, so a difference inside the left side is reported where
 * it sits. Keys absent from the left are visited afterwards, sorted by code point, because document
 * order cannot order what the document does not contain.
 *
 * @param {Record<string, *>} left
 * @param {Record<string, *>} right
 * @param {string} path
 * @param {Difference[]} out
 */
function compareObject(left, right, path, out) {
  checkKeys(left, path, 'left');
  checkKeys(right, path, 'right');
  for (const key of Object.keys(left)) {
    const pointer = child(path, key);
    if (Object.hasOwn(right, key)) {
      compare(left[key], right[key], pointer, out);
    } else {
      out.push(new Difference(DifferenceKind.EXTRA, pointer, left[key], ABSENT));
    }
  }
  const missing = Object.keys(right).filter((key) => !Object.hasOwn(left, key));
  for (const key of missing.sort(codePointOrder)) {
    out.push(new Difference(DifferenceKind.MISSING, child(path, key), ABSENT, right[key]));
  }
}

/**
 * JSON object keys are strings. Anything else is a runner bug, not a finding.
 *
 * `Object.keys` yields only string keys, so the check JavaScript can still fail is a symbol key:
 * one would be invisible to the whole walk and the two sides would compare as equal without it.
 *
 * @param {Record<string, *>} value
 * @param {string} path
 * @param {string} side
 */
function checkKeys(value, path, side) {
  const symbols = Object.getOwnPropertySymbols(value);
  if (symbols.length > 0) {
    const location = path ? path : '(document root)';
    throw new ComparisonError(
      `${side} object at ${location} has a symbol key ${String(symbols[0])}. ` +
        'JSON object keys are strings'
    );
  }
}

/**
 * Order two strings by Unicode code point, which is what Python's `sorted` does.
 *
 * JavaScript's default sort orders by UTF-16 code unit instead, so it puts a surrogate pair, any
 * character above U+FFFF, below U+E000. The two orders agree everywhere else, and rule 6 confines
 * today's corpus to latin-1, but the missing-key order is part of the diffable contract between
 * the two comparators and must not depend on that.
 *
 * @param {string} left
 * @param {string} right
 * @returns {number}
 */
function codePointOrder(left, right) {
  if (left === right) {
    return 0;
  }
  const leftPoints = [...left];
  const rightPoints = [...right];
  const shared = Math.min(leftPoints.length, rightPoints.length);
  for (let index = 0; index < shared; index += 1) {
    const difference =
      /** @type {number} */ (leftPoints[index].codePointAt(0)) -
      /** @type {number} */ (rightPoints[index].codePointAt(0));
    if (difference !== 0) {
      return difference < 0 ? -1 : 1;
    }
  }
  if (leftPoints.length === rightPoints.length) {
    return 0;
  }
  return leftPoints.length < rightPoints.length ? -1 : 1;
}

// ---------------------------------------------------------------------------
// Rule 7: arrays are ordered, unordered collections are compared unordered
// ---------------------------------------------------------------------------

/**
 * Compare two arrays index by index. Every JSON array is ordered.
 *
 * A length disagreement is one `length` difference at the array's own path, carrying both arrays
 * verbatim, and the shared prefix is still compared so a value difference inside it is not hidden
 * behind the length.
 *
 * @param {readonly *[]} left
 * @param {readonly *[]} right
 * @param {string} path
 * @param {Difference[]} out
 */
function compareArray(left, right, path, out) {
  if (left.length !== right.length) {
    out.push(new Difference(DifferenceKind.LENGTH, path, left, right));
  }
  const shared = Math.min(left.length, right.length);
  for (let index = 0; index < shared; index += 1) {
    compare(left[index], right[index], child(path, String(index)), out);
  }
}

/**
 * Compare a collection whose structure has no defined order, as a multiset.
 *
 * `compare.md` lists exactly two unordered collections, and extending the list is an amendment to
 * that file made before either comparator changes. Object key sets are the first, handled by rule 3
 * in `compareObject`. The assertion 4 diagnostic collection is the second, and this is where it
 * will be compared once assertion 4 ships.
 *
 * A multiset, not a set: a duplicated element is a difference. Elements pair by exact equality of
 * the whole element, so no tolerance is involved and the pairing is unambiguous. An element
 * carrying NaN or an infinity pairs with nothing, because rule 2 makes those always a difference.
 * Unmatched elements are reported individually, left side first in document order, then right. The
 * path is the collection's own, since an element with no partner has no index on the other side to
 * point at.
 *
 * Each left element takes the lowest-indexed right element it matches, the same pairing
 * `compare.py` gets by popping the front of a per-key index list. The scan is quadratic where the
 * Python side hashes, because a structural key cannot be a `Map` key in JavaScript and turning it
 * into one would mean serializing, which rule 1 forbids anywhere in a comparator. Diagnostic
 * collections are tens of elements.
 *
 * @param {readonly *[]} left
 * @param {readonly *[]} right
 * @param {{ path?: string }} [options]
 * @returns {Comparison}
 */
export function compareUnordered(left, right, { path = ROOT_POINTER } = {}) {
  // The right keys are built first, so that an element neither comparator can read is reported
  // from the same side in both languages.
  const rightKeys = Array.from(right, (element) => pairingKey(element, path));
  const matched = new Array(right.length).fill(false);
  /** @type {Difference[]} */
  const differences = [];

  for (const element of left) {
    const key = pairingKey(element, path);
    let partner = -1;
    if (key !== null) {
      for (let index = 0; index < right.length; index += 1) {
        if (!matched[index] && rightKeys[index] !== null && sameKey(rightKeys[index], key)) {
          partner = index;
          break;
        }
      }
    }
    if (partner >= 0) {
      matched[partner] = true;
    } else {
      differences.push(new Difference(DifferenceKind.UNMATCHED, path, element, ABSENT));
    }
  }
  for (let index = 0; index < right.length; index += 1) {
    if (!matched[index]) {
      differences.push(new Difference(DifferenceKind.UNMATCHED, path, ABSENT, right[index]));
    }
  }
  return new Comparison(differences);
}

/**
 * @typedef {readonly [JsonType, ...*]} PairingKey
 */

/**
 * A structural canonical form for unordered pairing, or `null` for an element that cannot pair.
 *
 * Object keys sort, because rule 3 never compares key order. Arrays keep their order, because rule
 * 7 keeps every array ordered. NaN and the infinities make the whole element unpairable, under rule
 * 2. Numbers need no normalisation here: JavaScript has one number type, which is the value Python
 * reaches by casting to a double.
 *
 * The key is a nested array and never a string. Comparing two elements by their serialized form
 * would be rule 1's forbidden shortcut wearing a different name, and it would also make `3` and
 * `3.0` pair in one language and not the other.
 *
 * @param {*} value
 * @param {string} path
 * @returns {PairingKey | null}
 */
function pairingKey(value, path) {
  const type = jsonTypeOf(value, path, 'element');
  if (type === JsonType.NULL) {
    return [JsonType.NULL];
  }
  if (type === JsonType.BOOLEAN) {
    return [JsonType.BOOLEAN, value];
  }
  if (type === JsonType.NUMBER) {
    return Number.isFinite(value) ? [JsonType.NUMBER, value] : null;
  }
  if (type === JsonType.STRING) {
    return [JsonType.STRING, value];
  }
  if (type === JsonType.ARRAY) {
    const elements = value.map((element) => pairingKey(element, path));
    return elements.some((element) => element === null) ? null : [JsonType.ARRAY, elements];
  }
  checkKeys(value, path, 'element');
  /** @type {[string, PairingKey][]} */
  const entries = [];
  for (const key of Object.keys(value).sort(codePointOrder)) {
    const member = pairingKey(value[key], path);
    if (member === null) {
      return null;
    }
    entries.push([key, member]);
  }
  return [JsonType.OBJECT, entries];
}

/**
 * Exact structural equality of two pairing keys.
 *
 * The Python side gets this from tuple equality. Neither version applies a tolerance: rule 7 pairs
 * by exact equality so that the pairing is unambiguous.
 *
 * @param {*} left
 * @param {*} right
 * @returns {boolean}
 */
function sameKey(left, right) {
  if (Array.isArray(left) || Array.isArray(right)) {
    if (!Array.isArray(left) || !Array.isArray(right) || left.length !== right.length) {
      return false;
    }
    return left.every((element, index) => sameKey(element, right[index]));
  }
  return left === right;
}

// ---------------------------------------------------------------------------
// Rendering, which decides nothing
// ---------------------------------------------------------------------------

/**
 * A value as one report line's worth of text. Display only, never a verdict.
 *
 * The truncation counts and cuts code points rather than UTF-16 code units, so a long value reads
 * the same length in both languages.
 *
 * @param {*} value
 * @param {number} limit
 * @returns {string}
 */
function renderValue(value, limit) {
  if (value instanceof Absent) {
    return '<absent>';
  }
  const text = renderJson(value);
  const points = [...text];
  if (limit > 0 && limit < points.length) {
    return `${points.slice(0, limit).join('')}... (${points.length} characters)`;
  }
  return text;
}

/**
 * JSON text for a report line, with the separators Python's `json.dumps` uses by default.
 *
 * `JSON.stringify` writes NaN and the infinities as `null`, which is a real JSON value and a
 * different finding, so those three are written by name here as Python writes them. Number
 * formatting still differs between the two languages, `3.0` against `3`, and that is rule 1's own
 * example: a report line is text about a value and never the comparison of one.
 *
 * @param {*} value
 * @returns {string}
 */
function renderJson(value) {
  if (value === null) {
    return 'null';
  }
  if (typeof value === 'boolean') {
    return value ? 'true' : 'false';
  }
  if (typeof value === 'number') {
    if (Number.isNaN(value)) {
      return 'NaN';
    }
    if (value === Number.POSITIVE_INFINITY) {
      return 'Infinity';
    }
    if (value === Number.NEGATIVE_INFINITY) {
      return '-Infinity';
    }
    return JSON.stringify(value);
  }
  if (typeof value === 'string') {
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) {
    return `[${value.map((element) => renderJson(element)).join(', ')}]`;
  }
  if (isPlainObject(value)) {
    const entries = Object.keys(value).map(
      (key) => `${JSON.stringify(key)}: ${renderJson(value[key])}`
    );
    return `{${entries.join(', ')}}`;
  }
  return String(value);
}
