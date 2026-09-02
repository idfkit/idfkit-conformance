# Comparison rules

**Normative.** This file is the single written source that `runners/compare.py` and
`runners/compare.mjs` implement. Each comparator must be readable as a direct transcription of the
sections below, in the same order, using the same names. Neither comparator may decide anything this
file leaves unstated: if a case raises a question the rules do not answer, amend this file first and
change both comparators in the same change.

This is where the suite earns its keep. Getting the rules wrong produces either noise, which trains
maintainers to ignore the suite, or false confidence, which is worse.

## Vocabulary

| Term | Meaning |
| ---- | ------- |
| **left** | The value produced by the library under test |
| **right** | The expectation: `expected.epJSON` in assertion 2, the original parsed document in assertion 3 |
| **difference** | One reported disagreement, with a path, a kind, and both values |
| **path** | The location of a difference inside the compared value (see [Reporting](#reporting)) |

Left and right are fixed, never swapped. A report saying a key is missing therefore always means the
library omitted it, and never the reverse.

## The rules

| Rule | Contract |
| ---- | -------- |
| Values, not text | Compare parsed JSON values. Never compare JSON text |
| Numbers | Relative tolerance 1e-12. Integer against float is **not** a difference. epJSON has one number type |
| Object key order | Not compared. JSON objects are unordered |
| IDF field order | Compared, but only in assertion 3, where it matters |
| Strings | Case-sensitive. Both libraries claim to preserve as-written casing, so any disagreement is a finding, not noise |
| Encoding | latin-1. At least one case carries high bytes in a station name |
| Collections | Unordered collections are compared unordered (FR-016) |

Each rule is stated in full below, with its reason. The reasons are part of the contract: a
comparator that satisfies the letter of a rule while defeating its reason is wrong.

### 1. Compare parsed JSON values, never JSON text

Parse both sides into in-memory values first, then compare the values. Never compare the two
documents as strings, never compare pretty-printed or re-serialized forms, never shell out to a
textual diff, and never canonicalize by serializing with sorted keys and comparing the result.

**Reason, concretely.** One fixture in the corpus today yields `0.0 / 3.0 / 1e-05` from Python and
`0 / 3 / 0.00001` from JavaScript. That is one JSON number and two JSON texts: every pair is the
same value, and every pair differs as text. A textual diff reports three differences where there are
none, and the real finding in that same file gets buried under float repr noise. The suite exists to
surface real findings, so anything that generates noise at this volume defeats it.

The corollary matters as much: a comparator that is textual anywhere is textual everywhere. Do not
compare a nested subtree by serializing it, not even for a fast path or a cheap hash.

### 2. Numbers

epJSON has one number type. Python distinguishes `int` from `float`, JavaScript has neither
distinction nor a way to express it in JSON, and `ConvertInputFormat` emits whichever the schema
type implies. None of that is a claim about the model, so none of it is a difference.

Two numbers `a` and `b` are equal when:

1. `a == b` exactly, which covers integer against float (`3` equals `3.0`) and zero against zero, or
2. `abs(a - b) <= 1e-12 * max(abs(a), abs(b))`.

Otherwise they differ.

Details, all normative:

- **The tolerance is relative only. There is no absolute floor.** Exact zero is equal only to exact
  zero. An absolute epsilon would make a genuine zero-against-nonzero disagreement invisible, and
  that disagreement is exactly what the `numeric` and `positional` cases pin: an unset field read as
  `0` instead of omitted is a real bug, not rounding.
- **A boolean is not a number.** `true` against `1` is a difference. JSON keeps them distinct and so
  does the schema.
- **A numeric string is not a number.** `"3"` against `3` is a difference, and an important one: a
  field typed as numeric that one library leaves as text is a parse bug, and `autosize` cases live
  next door to this.
- **NaN and the infinities are always differences**, including NaN against NaN. JSON cannot carry
  them, so one reaching the comparator is itself the finding and must be reported, not silently
  compared away.
- Compare in the language's native double precision. Do not round, quantize, or format either side
  before comparing.

### 3. Object key order is not compared

JSON objects are unordered. Compare two objects as key sets plus per-key values:

- Every key on the left must be present on the right, and the reverse.
- A key present on one side and absent on the other is a difference, whatever its value.
- **Absent is not `null`.** A key present with value `null` against a key that is absent is a
  difference, because in epJSON an omitted field and a field set to nothing are different documents,
  and telling them apart is the point of the `positional` and `naming` cases.
- Key comparison is exact and case-sensitive, under rule 5.

**Reason.** Neither library promises a key order in its epJSON output, and `ConvertInputFormat`
emits its own. Comparing order would fail every case for a property nobody claims.

### 4. IDF field order is compared, but only in assertion 3

Assertions 1 and 2 operate on epJSON, where a field is a named key and its order is not observable.
Rule 3 applies there in full.

Assertion 3 compares two parsed documents, and a parsed document retains, per object, the sequence
of its fields as written. That sequence **is** compared, position by position, including trailing
unset fields and the boundaries of extensible groups.

**Reason.** IDF fields are positional: reordering them silently changes the model. Nothing else in
the suite catches a round trip that emits the right values in the wrong slots, because epJSON has
already thrown the ordering away. Comparing that order in assertion 2 instead would test the
oracle's key order, which is not a claim either library makes.

### 5. Strings are case-sensitive

Compare strings as exact sequences of scalar values. No case folding, no trimming, no collapsing of
internal whitespace, no Unicode normalization, no normalization of line endings inside a value.

**Reason.** Both libraries claim to preserve as-written casing. When they disagree on the casing of
a name, a type, or a comment, one of them has broken that claim, so the disagreement is a finding
and must be reported rather than absorbed. Case-insensitive **reference resolution** is a separate
matter, pinned by the `references` cases, and it is tested through the resolved values, never by
loosening this comparison.

Normalization is safe to omit because every string in the corpus originates from latin-1 input (rule
6), whose bytes map one to one onto U+0000 to U+00FF. No combining sequence can arise, so NFC and
NFD cannot both appear for the same source text. A comparator that normalizes would hide the case
where one library re-encodes a value and the other does not.

### 6. Encoding is latin-1

- `input.idf` and any IDF a library writes during assertion 3 are read and written as **latin-1**
  (ISO-8859-1).
- `expected.epJSON`, `expected.diag.json`, `manifest.json`, and every other JSON file are UTF-8, as
  JSON requires.

In Python: `path.read_text(encoding="latin-1")` and `path.write_text(..., encoding="latin-1")`. In
JavaScript: `readFileSync(path).toString('latin1')` and `Buffer.from(text, 'latin1')`. Never
`readFileSync(path, 'utf8')` for an IDF, and never let a platform default decide.

**Reason.** At least one case carries high bytes in a station name. Decoded as UTF-8 those bytes are
invalid, so a UTF-8 comparator crashes or substitutes replacement characters on that case rather
than reporting what the libraries actually did. latin-1 never fails on any byte sequence, which is
what makes it the right choice here: any garbling that a library introduces then shows up as a value
difference, and a value difference is the finding.

### 7. Unordered collections are compared unordered

A collection is compared unordered only when the structure that produced it has no defined order.
Today exactly two do:

1. **Object key sets** (rule 3).
2. **The assertion 4 diagnostic collection**, compared as an unordered multiset of
   `(code, line, typeName)` tuples, once assertion 4 ships.

Everything else, in particular **every JSON array**, is ordered and compared index by index: epJSON
extensible groups are arrays whose order is the field order, and losing it is a bug.

Unordered comparison is a multiset comparison, not a set comparison: a duplicated element is a
difference. Pair elements by exact equality of the whole tuple, so no tolerance is involved and the
pairing is unambiguous. Unmatched elements on either side are reported individually under rule 3's
missing and extra kinds.

**Reason.** Adding a third unordered collection by guess is how the two comparators drift apart
while both stay green. Extending this list is an amendment to this file, made before either
comparator changes.

## Reporting

The comparator returns **every** difference it finds, not the first. The runner may truncate what it
prints, and must print the total count when it does.

The order of the returned differences is deterministic and identical in both implementations:
document order of the left side, and where a difference concerns keys absent from the left, the keys
are visited sorted by code point. Two comparators whose output cannot be diffed against each other
are two comparators nobody can reconcile.

Each difference carries a **path**, written as an RFC 6901 JSON Pointer against the compared value:
segments separated by `/`, array indices as decimal, `~` escaped as `~0` and `/` as `~1`. An object
type and an object name are ordinary segments, so a difference inside a zone reads
`/Zone/Zone 1/ceiling_height`.

| Kind | Meaning |
| ---- | ------- |
| `value` | Both sides have a value at the path and they differ under the rules above |
| `type` | The JSON types at the path differ, for example number against string |
| `missing` | The path exists on the right and not on the left: the library omitted it |
| `extra` | The path exists on the left and not on the right: the library invented it |
| `length` | Two ordered arrays have different lengths |
| `unmatched` | An element of an unordered collection has no partner on the other side |

Every difference reports both values verbatim as parsed, never reformatted, so the exit contract's
"the differing value" is the value the library actually produced.

## The four assertions

| # | Assertion | Ships |
| - | --------- | ----- |
| 1 | Parse outcome matches the declared expectation, that is it succeeds, or fails with diagnostics | now |
| 2 | Canonical epJSON of the parsed document equals `expected.epJSON` | now |
| 3 | Round trip: re-parsing the library's own IDF output deep-equals the original document | now |
| 4 | Diagnostics match `expected.diag.json` as an unordered set of `(code, line, typeName)`, never on message text | deferred |

Rules 1, 2, 3, 5, 6, and 7 apply to assertions 2 and 3. Rule 4 applies to assertion 3 only.
Assertion 1 compares an outcome, not a value, and uses no rule but rule 6 for reading the input.

Assertions 1 to 3 need no changes inside either library, which is why they ship now. Assertion 4
does need changes: `ParseDiagnostic` in JavaScript carries free-text `message`, `line`, and
`typeName` with no code, and Python raises typed exceptions instead. Deferring it costs nothing
later, because adding `code` is additive on both sides.

Assertion 4 compares on `(code, line, typeName)` and never on message text, because message wording
is a presentation choice each library is free to improve. Pinning it would turn every wording
improvement into a conformance failure, and the suite would be edited to match rather than believed.

### Shared code vocabulary, when assertion 4 lands

Derived from Python's existing exception hierarchy by stripping the suffix, so neither language has
to invent a vocabulary and the mapping stays mechanical:

| Python exception | Shared code |
| ---------------- | ----------- |
| `UnknownObjectTypeError` | `UnknownObjectType` |
| `InvalidFieldError` | `InvalidField` |
| `RangeError` | `Range` |
| `DuplicateObjectError` | `DuplicateObject` |
| `IDFParseError` | `ParseError` |
| `VersionMismatchError` | `VersionMismatch` |
| `UnsupportedVersionError` | `UnsupportedVersion` |
| `SchemaNotFoundError` | `SchemaNotFound` |

Codes are compared exactly, under rule 5. A diagnostic a library emits with no code, or with a code
outside this table, is a difference and not a near match.
