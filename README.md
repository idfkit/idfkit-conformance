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

Partial coverage is easy to mistake for complete coverage. Three gaps are open at landing.

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

**Case count**: 41 cases, 117 assertions, at level `conformance-2026.3`.

**Measured runtime** (SC-005), on a machine with no EnergyPlus installed, measured 2026-09-02:

| Library | Wall clock | Result |
| ------- | ---------- | ------ |
| Python `idfkit` | 0.57 s | 97 passed, 17 failed and allowlisted, 3 skipped |
| TypeScript `@idfkit/core` | 0.13 s | 106 passed, 8 failed and allowlisted, 3 skipped |

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
    case.toml            # title, tags, EnergyPlus version, why this case exists
    input.idf            # or input.epJSON
    expected.epJSON      # generated by ConvertInputFormat, committed
    expected.diag.json   # expected diagnostics; malformed cases only, not yet compared
manifest.json            # index: every case, its tags, which assertions apply
runners/
  run.py                 # Python runner
  run.mjs                # JavaScript runner
  compare.md             # the normative comparison rules
tools/
  regenerate.sh          # re-run ConvertInputFormat over every case (maintainer task)
governance/              # naming.toml and parity.toml, read at a pinned governance tag
known-divergence.toml    # accepted failures, each with an issue link
```

There is no `checks/` directory. Some cross-library claims are not expressible as an input file with
an expected epJSON, and such a check would live there. None exists at landing, so the directory is
created when the first one is written, not before.

## The four assertions

| # | Assertion | Ships |
| - | --------- | ----- |
| 1 | Parse outcome matches the declared expectation, succeeds or fails as stated | now |
| 2 | Canonical epJSON of the parsed document equals `expected.epJSON` | now |
| 3 | Round trip: re-parsing the library's own IDF output deep-equals the original document | now |
| 4 | Diagnostics match `expected.diag.json` as an unordered set of `(code, line, typeName)`, never on message text | deferred |

Each case declares which assertions apply to it. `runners/compare.md` holds the normative
comparison rules, and it is the file to read before arguing about a result: values rather than text,
a relative tolerance of 1e-12, integer against float is not a difference, object key order is not
compared, IDF field order is compared only in assertion 3, strings are case-sensitive, and the
encoding is latin-1.

## Case taxonomy

Cases are grouped by the hazard each one pins, not by feature area.

| Tag | Pins |
| --- | ---- |
| `positional` | Trailing unset fields, with and without a following extensible group |
| `naming` | Blank name against absent name, synthetic key generation, collision with a real name |
| `extensible` | Empty groups, partial groups, wrapper keys, single against multiple |
| `numeric` | autosize, autocalculate, scientific notation, integer-typed fields, zero |
| `types` | Unknown object types, case-variant type names (`ZONE` against `Zone`) |
| `references` | Dangling, self-referential, case-insensitive name matching |
| `versions` | At least one case per supported version, including `Version, 9.0` mapping to schema 9.0.1 |
| `encoding` | latin-1 high bytes in names and comments |
| `malformed` | Missing semicolon, truncated object, stray comment. Diagnostics only |

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
`GlobalGeometryRules` even when testing something unrelated to either.

## Levels and pinning

A level is an immutable git tag, `conformance-YYYY.N`. Each library declares the level it passes,
from one value read by both its CI job and its release check, so the declaration and the pin cannot
diverge:

- Python: `[tool.idfkit.conformance] level` in `pyproject.toml`, exposed as
  `idfkit.CONFORMANCE_LEVEL`.
- JavaScript: `idfkit.conformance` in `packages/core/package.json`, exposed as `CONFORMANCE_LEVEL`.

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
