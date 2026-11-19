<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/idfkit/idfkit-brand/identity-2026.5/marks/mark-on-dark.svg">
    <source media="(prefers-color-scheme: light)" srcset="https://raw.githubusercontent.com/idfkit/idfkit-brand/identity-2026.5/marks/mark-on-light.svg">
    <img alt="idfkit" width="72" src="https://raw.githubusercontent.com/idfkit/idfkit-brand/identity-2026.5/marks/tile.png">
  </picture>
</p>

# idfkit-conformance

The conformance corpus and governance registers that hold idfkit and idfkit-js to one behaviour.

[idfkit](https://idfkit.com) · [Developers](https://developers.idfkit.com) · [EnergyPlus docs](https://docs.idfkit.com) · [Envelop](https://app.idfkit.com) · [MCP](https://mcp.idfkit.com) · [Shoebox](https://shoebox.idfkit.com) · [GitHub](https://github.com/idfkit)

[![Release](https://img.shields.io/github/v/release/idfkit/idfkit-conformance?filter=governance-*&label=governance)](https://github.com/idfkit/idfkit-conformance/releases) [![Build status](https://img.shields.io/github/actions/workflow/status/idfkit/idfkit-conformance/governance.yml?branch=main)](https://github.com/idfkit/idfkit-conformance/actions/workflows/governance.yml?query=branch%3Amain) [![License](https://img.shields.io/github/license/idfkit/idfkit-conformance)](https://github.com/idfkit/idfkit-conformance/blob/main/LICENSE)

## Read this first: the coverage this corpus does NOT have

Partial coverage is easy to mistake for complete coverage. Six gaps are open.

### EnergyPlus 26.1.0 only, outside the version cases

62 of the 74 cases declare `energyplus_version = "26.1.0"`. The other twelve declare one older
release each: eleven are `versions` cases (9.0.1, 9.5.0, 9.6.0, and 22.1.0 through 25.2.0), which
hold the same three objects and differ only in the version identifier, so they pin how a version
resolves to a schema rather than how content changes between releases; the twelfth,
`tier1-introspection-field-order`, declares 9.4.0. Bracketing the corpus with an older release, 9.2.0 for example, would catch
version-sensitive drift: schema changes, field renames, extensible-group reshaping. Nothing here
catches that today. This is a known gap, not a closed question.

### Diagnostics are compared, since feature 002

Assertion 4 matches parse findings against `expected.diag.json`. It was deferred through the first
phase and both runners skipped it, because it was the one assertion that needed changes inside the
libraries: `ParseDiagnostic` carried free-text `message`, `line` and `typeName` with no stable code,
so a case tagged `malformed` proved only that the parse failed, not that it failed for the stated
reason. Both sides now carry a `code` from the shared vocabulary, and the assertion runs.

It compares an unordered multiset of `(code, line, typeName)` and **never** message text. Wording is
a presentation choice each library should stay free to improve, and pinning it would turn every
improvement into a conformance failure. A multiset rather than a set, because two skips of the same
type at different lines are two findings.

Both paths are compared. `diagnostics` in the expectation is the findings from whichever path the
input takes: what the error carried if the read failed, what the returning path gave if it did not.
`recoverable` names the returning path explicitly, which a case needs when its input fails a strict
read and the recoverable behaviour is still worth checking.

### The TypeScript library's file reading is not tested

The two runners fix the encoding of comparison rule 6 in different ways. `run.mjs` decodes the
input file itself and hands a string to the parser. `run.py` hands `idfkit.parse_idf` the path and
passes the encoding explicitly, so Python's path-taking reader runs, but with the encoding the
runner chose; for assertion 4 it calls `idfkit.load_idf_with_diagnostics` with the path and the
library's default encoding:

```js
const text = readFileSync(job.inputPath).toString('latin1');
library.core.parseIdf(text, schema);
```

```python
document = idfkit.parse_idf(job.input_path, encoding=IDF_ENCODING)
```

That is deliberate, and it is what makes an encoding difference a finding rather than noise. The
cost is that the TypeScript read-from-disk entry point, `loadIdf`, is never exercised. A bug that lives in the decode step rather than in the parse step is
invisible to every TypeScript run of this corpus, no matter how many cases are added.

This is not hypothetical. `loadIdf` refuses any IDF file carrying a UTF-8 byte-order mark, because
the mark survives a latin-1 decode as three visible characters and the version detector then anchors
its pattern past them, so the file is reported as versionless. Python reads the same file correctly.
That is a one-sided divergence of exactly the kind this corpus exists to catch, and it was reported
from downstream rather than found here.
See [idfkit-js#28](https://github.com/idfkit/idfkit-js/issues/28).

Closing this gap needs a check that calls each library's own reader, which no `input.idf` plus
`expected.epJSON` pair can express. That is what the `checks/` directory is for. It exists now, and
its two members, `weather-monthly` and `geometry-vertices`, are about a weather file and about
surface coordinates rather than about this gap: reading an IDF from disk is still open.

A second consequence, worth stating separately: cases are curated from a sweep of the EnergyPlus
example files, so the corpus only sees hazards that EnergyPlus's own files exhibit. Byte-order marks,
CRLF line endings, and the other things real editors emit are not in that set. Nine `preserve-*`
cases were donated in `conformance-2026.9` to close part of that, and they close it for the parse
and the write; they do not close it for the read from disk, which is this section's subject.

**Assertion 9 lives on this side of the boundary too.** `preserved-text` proves that a library
reproduces the text its read was GIVEN, not that it reproduces a file's bytes. The runners fix the
encoding, as above, and compare the written string against the input as the runner decoded it, so
what the assertion holds each library to is a string in, the same string out. Two defects live on the other side of that line and neither is closed by it: the
TypeScript reader refuses an IDF file carrying a byte-order mark, recorded above against
idfkit-js#28, and Python's `save_idf` opens its destination without `newline=""`, so the standard
library translates every line feed to the platform's own convention on the way out. On Windows a
byte-identical write therefore becomes a byte-different file, and the first language's byte-identity
claim is platform-dependent. Both need the reserved `checks/` directory to close.

### Weather retrieval is not covered, and `tier1` does not claim it

Four Tier 1 capabilities were ported into the JavaScript library under the API unification.
`conformance-2026.6` proves three of them: validation, type introspection and documentation
addresses. The fourth, `weather-index`, has no case and cannot have one in this corpus's shape.

A case is an input file, a parsed document, and an assertion about what the library made of it.
Resolving a weather station is none of those. There is no IDF that names a station, no document
state that changes when one is found, and nothing for an expectation file to hold. Closing this
needs the same `checks/` directory the file-reading gap needs, and for the same reason: the claim
is about a library's behaviour rather than about a document's content.

**The directory now exists and does not close this.** `weather-monthly` is about reading the EPW
text a retrieval produced, not about the retrieval that produced it: it hands each library committed
bytes and asks what it made of them. Resolving a station still has no check, and `weather-index`
stays where this section puts it.

`weather-index` is `partial` for TypeScript in the parity ledger, which records what the two
libraries do and does not depend on this corpus. What proves the port instead is a documented
build-time warm-up and a run with the network switched off, which is where the specification puts
it. The `tier1` tag therefore means "the Tier 1 capabilities this corpus can express", not "every
Tier 1 capability", and a green `--tag tier1` is not a statement about weather.

### The two writers' output is not compared against each other, and cannot be

The naming register once said this corpus proves that both libraries render the same string for
the same model. It does not, and it is not able to.

`runners/compare.md` forbids comparing the two libraries textually, for a good reason set out
there: a formatting difference and a value difference are not the same finding, and a comparator
that is textual anywhere is textual everywhere. What assertion 3 does instead is re-parse each
library's own IDF output and compare the resulting *document* to the original, which catches a field
that moved or a value that was lost and says nothing about the bytes in between.

Assertion 9 is not an exception to this and must not be read as one. It compares a library's output
to **that same library's own input**, never to the other library's output, and its whole content is
that nothing was re-rendered. Rule 1 states the exception as a direction rather than as an assertion
number precisely so the next byte comparison cannot be added by precedent.

The bytes do differ, and the differences are real. Round-tripping one file through both writers
gives 2-space indentation against 4, insertion order against sorted order, a `!-Generator idfkit`
header on one side only, and different comment capitalisation. None of it is a defect, none of it
is hidden, and neither writer was changed to make the other's output appear. The parity ledger records
`write` as `complete` on both sides, and its comment on that entry names where the default-output
differences are now stated: `docs/explanation/two-writers-one-model.md` in idfkit-developers. The
five `writer-option-*` cases prove the writer controls structurally, by re-reading, not by bytes.

So a green run is a statement about what each library understood, never about what it typed.

### Four byte-hazard cases declare an assertion neither library can evaluate

`preserve-empty`, `preserve-comments-only` and `preserve-duplicate-name` declare `preserved-text`
and it is skipped in both languages, because both readers refuse their inputs and a refused read
leaves nothing to write. `preserve-unterminated-final` is now skipped in both languages too: its
declared parse outcome is a failure, and the run of 2026-11-06 recorded below skipped it on each
side.

The first three fail for one cause between them: both libraries resolve their schema from a Version
statement in the file, and an empty file, a file of comments and a file whose duplicate name stops
the read have no readable model. The declared parse outcome is that refusal, which both libraries
agree on exactly, so those cases still earn their keep on assertion 1.

What they do not yet pin is the writer hazard they were donated for: that a file with nothing to
anchor comes back whole, and that the characters of a statement the read REJECTED survive a write.
The second needs a read that succeeds while rejecting a statement, which neither library offers
today. The assertion is declared rather than dropped so that a library which later reads one of
these files starts being held to the property with no corpus change, and so that the gap is visible
as a skip in every run rather than as an absence nobody sees.

## What this repository holds

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
| An assertion could not be evaluated, or an allowlist entry can never be exercised | 1 | Case id, assertion, and the reason |
| The run cannot start: `--library` names no usable checkout, or `--case`/`--tag` selects no case | 2 | The reason, on standard error |

**Case count**: 74 cases, 226 assertions, at level `conformance-2026.21`, the newest corpus tag on
`main`. The case set has not changed since `conformance-2026.17`.

**Measured runtime** (SC-005), measured 2026-11-06 at `conformance-2026.21` (74 cases, 226
assertions), against idfkit `v1.2.0` and against `@idfkit/core` at idfkit-js `v0.4.0` and at
idfkit-js `main` commit `06f739a`. No runner invokes EnergyPlus: every assertion reads, writes,
validates or introspects, so the corpus runs anywhere the libraries install.

| Library | Wall clock | Result | Exit |
| ------- | ---------- | ------ | ---- |
| Python `idfkit` `v1.2.0` (declares `conformance-2026.21`) | 0.82 to 1.13 s | 220 passed, 2 failed, 4 skipped; 2 allowlisted | 0 |
| TypeScript `@idfkit/core`, idfkit-js `main` `06f739a` (declares `conformance-2026.19`) | 0.16 to 2.27 s | 217 passed, 5 failed, 4 skipped; 5 allowlisted | 0 |
| TypeScript `@idfkit/core`, idfkit-js `v0.4.0` (declares `conformance-2026.15`) | 0.16 to 0.27 s | 211 passed, 11 failed, 4 skipped; 5 allowlisted | 1 |

A range rather than a figure, from repeated runs on one machine. A single number implies a
precision that a warm cache and a busy laptop do not support, and the only question the budget
asks is which side of five minutes this falls on. One TypeScript run of nine against `main` took
593 s and did not recur in the next three; it is excluded from the range.

The budget is under 5 minutes per library. Both runners are more than two orders of
magnitude inside it, so the budget is not a constraint on how the corpus grows. Four assertions are
skipped in each language: `preserved-text` on the four cases described under "Four byte-hazard
cases declare an assertion neither library can evaluate", whose declared parse outcome is a
failure.

Every failure in the runs against idfkit `v1.2.0` and idfkit-js `main` is a recorded entry in
`known-divergence.toml`, so both exit 0. The run against idfkit-js `v0.4.0` exits 1: that release
declares `conformance-2026.15`, and the six failures outside the allowlist are on three `update-*`
cases (`preserved-text` and `post-operation-epjson` on each), whose TypeScript entries were removed
in `conformance-2026.19`.

## Layout

```text
cases/
  <case-id>/
    case.toml                   # title, tags, EnergyPlus version, why this case exists
    input.idf                   # or input.epJSON
    expected.epJSON             # generated by ConvertInputFormat, committed; truth = oracle cases
    expected.diag.json          # expected diagnostics; cases declaring assertion 4
    expected.validation.json    # expected validation findings; tier1 cases
    expected.introspection.json # expected type descriptions; tier1 cases
    expected.docs-url.json      # expected documentation addresses; tier1 cases
    expected.type-lookup.json   # expected collection lookups by type name
    after.idf                   # the input with the case's operations made textually
    expected.after.epJSON       # generated by ConvertInputFormat from after.idf, committed
manifest.json            # index: every case, its tags, which assertions apply
manifest.schema.json     # the JSON Schema manifest.json declares
runners/
  run.py                 # Python runner, over cases/
  run.mjs                # JavaScript runner, over cases/
  model.py, model.mjs    # case, manifest and register loading shared by each language's runners
  compare.py, compare.mjs # the comparator that implements compare.md
  weather_check.py       # Python entry point for checks/weather-monthly
  weather-check.mjs      # JavaScript entry point for checks/weather-monthly
  geometry_check.py      # Python entry point for checks/geometry-vertices
  geometry-check.mjs     # JavaScript entry point for checks/geometry-vertices
  compare.md             # the normative comparison rules
  validate.md            # the normative validation semantics, which assertion 5 checks
  tests/                 # the runners' own tests, in both languages
checks/
  README.md              # why a claim lands here rather than in cases/, and what a check owes
  <check-id>/            # weather-monthly, geometry-vertices
    check.md             # what is claimed, the oracle, the assertions, the coverage it lacks
    fixtures/            # the check's own inputs, committed rather than fetched
    expected/            # the check's own committed expectations
tools/
  regenerate.sh          # re-run ConvertInputFormat over oracle inputs and after.idf files (maintainer task)
  seed_tier1.py          # draft the Tier 1 expectations (maintainer task; read its header)
  bootstrap_sweep.py, sweep_js.mjs # the bootstrap sweep that wrote sweep-report.json
  validate_governance.py, check_consumers.py # well-formedness of the governance files
  consumers.py, check_consumer_self.py, sweep_consumers.py # the consumer register and its checks
  rehearse.py, rename_scan.py # the candidate rehearsal run by rehearse.yml
  adoption.py, adoption_statements.py, dispatch_waves.py, record_adoption.py, surface_statements.py
  energyplus/            # the EnergyPlus release adoption procedure (roster, watch, scan, adopt)
governance/              # naming.toml, parity.toml, consumers.toml, energyplus.toml; README.md
energyplus/              # the adoption procedure's README and one record per adopted release
tests/                   # tests for tools/ and the governance files
.github/workflows/       # this repository's CI and the reusable workflows consumers call
sweep-report.json        # output of the bootstrap sweep the initial cases were curated from
known-divergence.toml    # accepted failures, each with an issue link
```

`checks/` holds the cross-library claims that are not expressible as an input file with an expected
epJSON. The contract reserved it at landing and said the directory would be created when the first
such check was written; `weather-monthly` is that first one and `geometry-vertices` the second.
`manifest.json` has its own `checks` section, separate from the two case sections and shaped
differently, because a check has no input document, no epJSON expectation and no place in either.
`checks/README.md` says what a check owes. The manifest's `checks` section lists `weather-monthly`
only; `geometry-vertices` is described in `checks/README.md` and its own `check.md` but has no
manifest entry.

A check runs from its own entry point rather than from `run.py` or `run.mjs`. Those run cases, and
a case is a shape this claim does not have.

    node runners/weather-check.mjs --library /path/to/idfkit-js
    python runners/weather_check.py --library /path/to/idfkit
    node runners/geometry-check.mjs --library /path/to/idfkit-js
    python runners/geometry_check.py --library /path/to/idfkit

## The assertions

| # | Assertion | Ships |
| - | --------- | ----- |
| 1 | Parse outcome matches the declared expectation, succeeds or fails as stated | now |
| 2 | Canonical epJSON of the parsed document equals `expected.epJSON` | now |
| 3 | Round trip: re-parsing the library's own IDF output deep-equals the original document | now |
| 4 | Diagnostics match `expected.diag.json` as an unordered multiset of `(code, line, typeName)`, never on message text | now |
| 5 | Validation findings match `expected.validation.json` as an unordered multiset, never on message text | now |
| 6 | Type descriptions match `expected.introspection.json` | now |
| 7 | Documentation addresses match `expected.docs-url.json` | now |
| 8 | Collection lookups by object type name match `expected.type-lookup.json` | now |
| 9 | `preserved-text`: a preserving read, the case's operations and a write reproduce the input byte for byte outside the objects the operations touched | now |
| 10 | `post-operation-epjson`: after the case's operations, canonical epJSON equals `expected.after.epJSON`, and an `update` declared refused is refused | now |

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
| `versions` | One case each for 9.0.1 (including `Version, 9.0` mapping to schema 9.0.1), 9.5.0, 9.6.0 and 22.1.0 through 26.1.0, plus two 26.1.0 cases on reading the version statement (its canonical string form, and a `Version` object late in the file); 8.9.0, 9.1.0 to 9.4.0 and 26.2.0, which idfkit `v1.2.0` supports, have no `versions` case |
| `encoding` | latin-1 high bytes in names and comments |
| `malformed` | Missing semicolon, truncated object, stray comment and unknown object type, compared on diagnostics; and three `preserve-*` inputs both readers refuse (empty, comments only, unterminated final statement), which declare `preserved-text` |
| `tier1` | The Tier 1 capabilities this corpus can express: validation, type introspection and documentation addresses (FR-023), the writer controls (`writer-option-*`), multi-field object update (`update-*`), and unknown-type diagnostics (`malformed-unknown-type-*`) |

Cases are not invented from the specification. The initial set came from running both libraries and
`ConvertInputFormat` across the full EnergyPlus example set, recording every three-way disagreement,
and hand-curating a minimal reproducer per distinct disagreement. Hand-written cases fill the
taxonomy's remaining gaps afterwards, never before.

## Regenerating expectations

Maintainer task, and the only step that needs EnergyPlus:

```bash
tools/regenerate.sh
```

It runs `ConvertInputFormat -f epJSON` on `cases/<id>/input.idf` for every case with
`truth = "oracle"`, writing `expected.epJSON`, and on `cases/<id>/after.idf` for every case that
declares `post-operation-epjson`, writing `expected.after.epJSON`. Convention cases without that
assertion are skipped without error. It skips versions it cannot find locally and reports which it
skipped. A full set of EnergyPlus installs is large, which is why CI never does this.

Note that `ConvertInputFormat` validates before it converts, so most cases carry `Building` and
`GlobalGeometryRules` even when testing something unrelated to either. The `tier1` cases do not:
they have no oracle, so nothing needs satisfying but the schema, and carrying inert objects would
have added them to every type description and every documentation address the case compares.

The Tier 1 expectations are drafted by a different tool, and it is a typing aid rather than an
authority:

```bash
tools/seed_tier1.py --library ../idfkit <case-id>
```

It imports `tomllib` and therefore needs Python 3.11 or newer, unlike the runners.

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

Both libraries also expose the value at runtime: `idfkit.CONFORMANCE_LEVEL` (generated into
`src/idfkit/_conformance.py` from `pyproject.toml`; exported in idfkit `v1.2.0`) and the
`CONFORMANCE_LEVEL` constant exported by `@idfkit/core` (`packages/core/src/conformance.ts`; exported
in idfkit-js `v0.4.0`).

The two libraries release on independent schedules. Matching version numbers must never be read as
agreement. The declared level is the claim, and it is the stronger claim precisely because it is
tested.

## Governance registers

`governance/` holds four files, released together under a second tag series, `governance-YYYY.N`,
separate from the corpus series. `governance/README.md` is the full description.

| File | What it records |
| ---- | --------------- |
| `naming.toml` | The naming register: each concept shared by the two libraries, its spelling on each side, and each accepted difference |
| `parity.toml` | The parity ledger: each capability, its tier, and whether each language implements it completely, partially or not at all |
| `consumers.toml` | The consumer register: each workspace project that resolves either library or teaches a reader to install one, and where that project declares its level |
| `energyplus.toml` | The version roster: each project with content that depends on the EnergyPlus version, and the steps and checks for adopting a release; it names no EnergyPlus version |

Each library declares the governance tag it reads (`[tool.idfkit.governance] level` in
`pyproject.toml`, `idfkit.governance` in `packages/core/package.json`), and consumers call this
repository's reusable workflows at a pinned governance tag:

| Workflow | Trigger | What it does |
| -------- | ------- | ------------ |
| `check-consumer.yml` | `workflow_call` | Fails a consumer's build when its repository no longer matches its entry in `consumers.toml` |
| `rehearse.yml` | `workflow_call` | Runs one consumer against an unpublished candidate of one library; consumers call it from their own `rehearse-candidate.yml` |
| `adopt-energyplus.yml` | `workflow_call` | Adopts one EnergyPlus release inside one project, following `energyplus.toml` |
| `governance.yml` | pull request, push | Validates the governance files and runs `tests/` and `runners/tests/` |
| `sweep-consumers.yml` | schedule, dispatch | Reads every registered consumer's level and looks for unregistered dependants |
| `energyplus-watch.yml` | schedule, dispatch | Detects upstream EnergyPlus releases and advances open adoptions |
| `record-adoption.yml` | dispatch | Opens a pull request recording what an adoption changed in `consumers.toml` |
| `check-identity.yml` | pull request, push | Calls idfkit-brand's `check-touchpoint.yml` |

## Contributing

Changes to `cases/`, `known-divergence.toml`, and `governance/` require review from a maintainer of
the other language. A case that only one language's maintainer has read is a case that encodes one
language's assumptions.

Every case must record, in `case.toml`, **why** it exists: the hazard it pins, in prose. A case
whose reason is unrecorded cannot be maintained, because nobody later can tell a deliberate
expectation from a frozen accident.
