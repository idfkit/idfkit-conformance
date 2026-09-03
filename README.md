# idfkit-conformance

A cross-language conformance corpus for the two idfkit libraries:

- **`idfkit`**, the Python library.
- **`@idfkit/core`**, the TypeScript library.

Both read and write the same EnergyPlus formats, IDF and epJSON. Two implementations of one
schema-driven format drift silently, because each side passes its own suite. Passing your own tests
is not evidence. This repository holds the evidence that is external to both.

Expectations are produced by EnergyPlus `ConvertInputFormat`, generated **offline** and committed
alongside each case. Neither library's CI needs EnergyPlus installed, and neither library's output
is ever the expectation. The corpus can therefore report both libraries as wrong, which is what
happened on the first fixture written.

## Read this first: the coverage this corpus does NOT have

Partial coverage is easy to mistake for complete coverage. Five gaps are open.

### EnergyPlus 26.1.0 only

Every case declares `energyplus_version = "26.1.0"`, and every committed expectation was produced by
that release. Bracketing the corpus with an older release, 9.2.0 for example, would catch
version-sensitive drift: schema changes, field renames, extensible-group reshaping. Nothing here
catches that today. This is a known gap, not a closed question.

### Diagnostics are not compared

Assertion 4, matching parse diagnostics against `expected.diag.json`, is deferred to a second phase.
Assertions 1 to 3 need no changes inside either library. Assertion 4 does: `ParseDiagnostic` in
TypeScript carries free-text `message`, `line`, and `typeName` with no stable code, and Python
raises typed exceptions instead. Until a shared code vocabulary exists on both sides, a case tagged
`malformed` proves only that the parse failed, not that it failed for the stated reason.

Deferring costs nothing later, because adding a `code` field is additive.

### Neither library's file reading is tested

The runners decode the input file themselves and hand a string to the parser, so that comparison
rule 6 fixes the encoding rather than leaving it to each library:

```js
const text = readFileSync(job.inputPath).toString('latin1');
library.core.parseIdf(text, schema);
```

```python
document = idfkit.parse_idf(job.input_path, encoding=IDF_ENCODING)
```

That is deliberate, and it is what makes an encoding difference a finding rather than noise. The
cost is that the read-from-disk entry points, `loadIdf` in TypeScript and the path-taking form in
Python, are never exercised. A bug that lives in the decode step rather than in the parse step is
invisible to every case in this corpus, no matter how many cases are added.

This is not hypothetical. `loadIdf` refuses any IDF file carrying a UTF-8 byte-order mark, because
the mark survives a latin-1 decode as three visible characters and the version detector then anchors
its pattern past them, so the file is reported as versionless. Python reads the same file correctly.
That is a one-sided divergence of exactly the kind this corpus exists to catch, and it was reported
from downstream rather than found here.
See [idfkit-js#28](https://github.com/idfkit/idfkit-js/issues/28).

Closing this gap needs a check that calls each library's own reader, which no `input.idf` plus
`expected.epJSON` pair can express. That is what the `checks/` directory the contract reserves is
for. It has no members yet.

A second consequence, worth stating separately: cases are curated from a sweep of the EnergyPlus
example files, so the corpus only sees hazards that EnergyPlus's own files exhibit. Byte-order marks,
CRLF line endings, and the other things real editors emit are not in that set.

### Weather retrieval is not covered, and `tier1` does not claim it

Four Tier 1 capabilities were ported into the JavaScript library under the API unification.
`conformance-2026.6` proves three of them: validation, type introspection and documentation
addresses. The fourth, `weather-index`, has no case and cannot have one in this corpus's shape.

A case is an input file, a parsed document, and an assertion about what the library made of it.
Resolving a weather station is none of those. There is no IDF that names a station, no document
state that changes when one is found, and nothing for an expectation file to hold. Closing this
needs the same `checks/` directory the file-reading gap needs, and for the same reason: the claim
is about a library's behaviour rather than about a document's content.

`weather-index` is `partial` for TypeScript in the parity ledger, which records what the two
libraries do and does not depend on this corpus. What proves the port instead is a documented
build-time warm-up and a run with the network switched off, which is where the specification puts
it. The `tier1` tag therefore means "the Tier 1 capabilities this corpus can express", not "every
Tier 1 capability", and a green `--tag tier1` is not a statement about weather.

### Writer output is not compared as text, and cannot be

The naming register once said this corpus proves that both libraries render the same string for
the same model. It does not, and it is not able to.

`runners/compare.md` forbids textual comparison outright, for a good reason set out there: a
formatting difference and a value difference are not the same finding, and a comparator that is
textual anywhere is textual everywhere. The assertion enum has no writer kind. What assertion 3
does instead is re-parse each library's own IDF output and compare the resulting *document* to the
original, which catches a field that moved or a value that was lost and says nothing about the
bytes in between.

The bytes do differ, and the differences are real. Round-tripping one file through both writers
gives 2-space indentation against 4, insertion order against sorted order, a `!-Generator idfkit`
header on one side only, and different comment capitalisation. None of it is a defect, none of it
is hidden, and neither writer was changed to make the other's output appear: the parity ledger
records `write` as `partial` on both sides with the differences stated, which is where a claim
about output belongs.

So a green run is a statement about what each library understood, never about what it typed.

## Why `known-divergence.toml` ships populated

The corpus lands with real, currently failing disagreements already recorded in
`known-divergence.toml`, each carrying a link to the issue tracking its resolution. That is
deliberate. It lets the corpus go green on arrival and start blocking **new** drift immediately,
before the bugs it already found are fixed. A suite that must be green before it can land never
lands.

The register is not a mute button:

- `issue` is mandatory. An exception without a tracked resolution is indistinguishable from an
  accepted bug.
- The runners **report** every outstanding exception in their output rather than suppressing it. A
  silent allowlist is how a temporary exception becomes permanent.
- An entry whose case now passes is a **failure**, not a pass. Stale entries are removed by the
  change that fixes the bug.

## Running the corpus

Requirements: Python 3.10+ and Node 20+. No EnergyPlus, no simulation software of any kind.

```bash
# From the root of this repository, with checkouts of both libraries available.
python runners/run.py   --library /path/to/idfkit
node   runners/run.mjs  --library /path/to/idfkit-js
```

One case, or one tag, when you are chasing a single hazard:

```bash
python runners/run.py   --library /path/to/idfkit    --case naming-blank-vs-absent
node   runners/run.mjs  --library /path/to/idfkit-js --tag  extensible
```

Both runners implement the same exit contract.

| Condition | Exit | Output |
| --------- | ---- | ------ |
| All cases pass, no exceptions outstanding | 0 | Case count, level, elapsed |
| All failures are listed in `known-divergence.toml` | 0 | Same, plus each outstanding exception with its issue link |
| A failure is not in the allowlist | 1 | Case id, library, assertion, the differing value, and the path to both sides |
| An allowlisted case now passes | 1 | Case id, and the instruction to remove the stale entry |
| A case's `expected.epJSON` is missing while `truth = "oracle"` | 1 | Case id |

**Case count**: 48 cases, 132 assertions, at level `conformance-2026.7`.

**Measured runtime** (SC-005), measured 2026-09-03. No runner invokes EnergyPlus: every assertion
reads, writes, validates or introspects, so the corpus runs anywhere the libraries install.

| Library | Wall clock | Result |
| ------- | ---------- | ------ |
| Python `idfkit` | 0.65 to 0.96 s | 116 passed, 13 failed and allowlisted, 3 skipped |
| TypeScript `@idfkit/core` | 0.11 to 0.15 s | 122 passed, 7 failed and allowlisted, 3 skipped |

A range rather than a figure, from repeated runs on one machine. A single number implies a
precision that a warm cache and a busy laptop do not support, and the only question the budget
asks is which side of five minutes this falls on.

The budget is under 5 minutes per library. Both runners are three orders of magnitude inside it, so
the budget is not a constraint on how the corpus grows: at this rate the case set could grow past
10,000 cases before the limit came into view. The 3 skipped assertions on each side are the deferred
diagnostics assertion on the three malformed cases.

Every failure above is a recorded entry in `known-divergence.toml`, which is why both runners exit 0.
That is the corpus landing green on arrival with its disagreements visible, not hidden.

## Layout

```text
cases/
  <case-id>/
    case.toml                   # title, tags, EnergyPlus version, why this case exists
    input.idf                   # or input.epJSON
    expected.epJSON             # generated by ConvertInputFormat, committed
    expected.diag.json          # expected diagnostics; malformed cases only, not yet compared
    expected.validation.json    # expected validation findings; tier1 cases
    expected.introspection.json # expected type descriptions; tier1 cases
    expected.docs-url.json      # expected documentation addresses; tier1 cases
    expected.type-lookup.json   # expected collection lookups by type name
manifest.json            # index: every case, its tags, which assertions apply
runners/
  run.py                 # Python runner
  run.mjs                # JavaScript runner
  compare.md             # the normative comparison rules
  validate.md            # the normative validation semantics, which assertion 5 checks
tools/
  regenerate.sh          # re-run ConvertInputFormat over every case (maintainer task)
  seed_tier1.py          # draft the Tier 1 expectations (maintainer task; read its header)
governance/              # naming.toml and parity.toml, read at a pinned governance tag
known-divergence.toml    # accepted failures, each with an issue link
```

There is no `checks/` directory. Some cross-library claims are not expressible as an input file with
an expected epJSON, and such a check would live there. None exists at landing, so the directory is
created when the first one is written, not before.

## The assertions

| # | Assertion | Ships |
| - | --------- | ----- |
| 1 | Parse outcome matches the declared expectation, succeeds or fails as stated | now |
| 2 | Canonical epJSON of the parsed document equals `expected.epJSON` | now |
| 3 | Round trip: re-parsing the library's own IDF output deep-equals the original document | now |
| 4 | Diagnostics match `expected.diag.json` as an unordered set of `(code, line, typeName)`, never on message text | deferred |
| 5 | Validation findings match `expected.validation.json` as an unordered multiset, never on message text | now |
| 6 | Type descriptions match `expected.introspection.json` | now |
| 7 | Documentation addresses match `expected.docs-url.json` | now |
| 8 | Collection lookups by object type name match `expected.type-lookup.json` | now |

Assertion 8 is the one assertion that is not about a document at all. It asks what a library
returns when a caller names an object type, which is where the two libraries disagreed most
plainly: `d["zone"]` was empty in Python while `doc.all('zone')` returned every zone in
TypeScript, on the same file, neither raising. No parse-level case can catch that, because the
disagreement lives on the accessor rather than in what either library parsed. `runners/compare.md`
holds its normative rule, including why an unknown type name is empty rather than an error.

Each case declares which assertions apply to it. `runners/compare.md` holds the normative
comparison rules, and it is the file to read before arguing about a result: values rather than text,
a relative tolerance of 1e-12, integer against float is not a difference, object key order is not
compared, IDF field order is compared only in assertion 3, strings are case-sensitive, and the
encoding is latin-1.

Assertions 5 to 7 are the Tier 1 assertions and they differ from the first four in one way that
matters: **they have no oracle.** `ConvertInputFormat` converts a file. It does not validate one,
does not describe a type, and does not build a documentation address, so no authority outside the
two libraries can rule on what they should produce. What rules instead is written down:
`runners/validate.md` for assertion 5, and the epJSON schema itself for assertions 6 and 7. Every
Tier 1 case is `truth = convention` and a failure is not automatically a port bug. Read the rule
first.

## Case taxonomy

Cases are grouped by the hazard each one pins, not by feature area.

| Tag | Pins |
| --- | ---- |
| `positional` | Trailing unset fields, with and without a following extensible group |
| `naming` | Blank name against absent name, synthetic key generation, collision with a real name |
| `extensible` | Empty groups, partial groups, wrapper keys, single against multiple |
| `numeric` | autosize, autocalculate, scientific notation, integer-typed fields, zero |
| `types` | Unknown object types, case-variant type names (`ZONE` against `Zone`), and the collection a caller reaches when naming one |
| `references` | Dangling, self-referential, case-insensitive name matching |
| `versions` | At least one case per supported version, including `Version, 9.0` mapping to schema 9.0.1 |
| `encoding` | latin-1 high bytes in names and comments |
| `malformed` | Missing semicolon, truncated object, stray comment. Diagnostics only |
| `tier1` | The capabilities ported into the JavaScript library under the API unification: validation, type introspection, documentation addresses (FR-023) |

Cases are not invented from the specification. The initial set came from running both libraries and
`ConvertInputFormat` across the full EnergyPlus example set, recording every three-way disagreement,
and hand-curating a minimal reproducer per distinct disagreement. Hand-written cases fill the
taxonomy's remaining gaps afterwards, never before.

## Regenerating expectations

Maintainer task, and the only step that needs EnergyPlus:

```bash
tools/regenerate.sh
```

It runs `ConvertInputFormat -f epJSON -o expected/ cases/<id>/input.idf` over every case, skips
versions it cannot find locally, and reports which it skipped. A full set of EnergyPlus installs is
roughly 13.6 GB, which is exactly why CI never does this.

Note that `ConvertInputFormat` validates before it converts, so most cases carry `Building` and
`GlobalGeometryRules` even when testing something unrelated to either. The `tier1` cases do not:
they have no oracle, so nothing needs satisfying but the schema, and carrying inert objects would
have added them to every type description and every documentation address the case compares.

The Tier 1 expectations are drafted by a different tool, and it is a typing aid rather than an
authority:

```bash
tools/seed_tier1.py --library ../idfkit <case-id>
```

It writes the three `expected.*.json` files from the Python library. Read its header before using
it. The output is a draft that a maintainer must check against the rule that governs it, and
`regenerate.sh`'s prohibition on copying an expectation out of a library still holds for every
assertion that has an oracle.

## Levels and pinning

A level is an immutable git tag, `conformance-YYYY.N`. Each library declares the level it passes,
from one value read by both its CI job and its release check, so the declaration and the pin cannot
diverge:

- Python: `[tool.idfkit.conformance] level` in `pyproject.toml`.
- JavaScript: `idfkit.conformance` in `packages/core/package.json`.

Neither library exposes that value at runtime yet. `idfkit.CONFORMANCE_LEVEL` and the exported
`CONFORMANCE_LEVEL` constant are specified and not written, so a caller who wants to know which
level a build claims has to read the config file the way CI does. An earlier version of this
section described them as though they existed, which is the failure this corpus is here to catch,
committed in its own README.

The two libraries release on independent schedules. Matching version numbers must never be read as
agreement. The declared level is the claim, and it is the stronger claim precisely because it is
tested.

## Contributing

Changes to `cases/`, `known-divergence.toml`, and `governance/` require review from a maintainer of
the other language. A case that only one language's maintainer has read is a case that encodes one
language's assumptions.

Every case must record, in `case.toml`, **why** it exists: the hazard it pins, in prose. A case
whose reason is unrecorded cannot be maintained, because nobody later can tell a deliberate
expectation from a frozen accident.
