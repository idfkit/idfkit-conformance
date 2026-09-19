# Check: geometry-vertices

The second member of `checks/`. `weather-monthly` opened the directory and stated what a check owes;
this one follows that shape without restating it.

## What is claimed

Each library, handed a model, resolves each surface's vertices to the same world coordinates that
EnergyPlus resolves them to for the same model, and associates each fenestration surface to the same
parent surface EnergyPlus associates it to.

## Why it cannot be a case

A case is an input file, a parsed document, and an assertion about what the library made of it,
compared against an expectation `ConvertInputFormat` produced. `ConvertInputFormat` converts between
the two model formats and never resolves a coordinate: handed a relative-coordinate model it returns
the authored numbers in the other format. It has nothing to say about where a surface is.

The claim here is about what a library **computes** from a document rather than about what the
document contains, which is the criterion `checks/README.md` states for this directory.

## The oracle, and why it qualifies

`Output:Surfaces:List` with the vertex report, read from `eplusout.eio`. EnergyPlus writes it after
applying the coordinate system, the zone origin, the zone's relative north and the building's north
axis, which is exactly the computation under test, and it states its own frame in the header it
emits:

```text
! <Zone Surfaces>,Zone Name,# Surfaces, Vertices are shown starting at
  Upper-Left-Corner => Counter-Clockwise => World Coordinates
```

Every expectation carries that sentence in the engine's own words, beside the engine version that
produced it.

It is produced by neither library, which is the corpus's oracle rule unchanged. It matters more here
than usual: the first language currently holds **two** resolutions that disagree with each other and
with the engine, so comparing the libraries to each other would have ratified a defect rather than
caught it.

## The comparison

Per surface, the resolved polygon against the reported polygon:

- **As a ring**, insensitive to which vertex starts it. The engine renormalises every reported
  surface to an upper-left start, and a faithful extractor preserves the author's order. Comparing
  index by index fails the 11 example models declaring a lower-left start for a reason that has
  nothing to do with resolution. `lower-left-start` is here to keep that honest.
- **Orientation preserved.** Reversing a ring is a real difference, and it is the difference the
  clockwise clause exists to produce. The comparison takes the smallest maximum vertex error over
  the cyclic rotations of the ring and never over its reversal.
- **Tolerance 0.005 m, per coordinate**, derived rather than chosen. The report prints each
  coordinate to two decimals, so half the last printed place is the finest agreement the authority
  can express about one number. The comparison is therefore the largest disagreement on any single
  coordinate and **not the distance between the two points**: Euclidean distance mixes three
  independently rounded numbers, and three coordinates each a legal 0.005 out give a distance of
  0.00866, over the tolerance without a single coordinate disagreeing by more than the report can
  state. That is not theoretical. Comparing by distance failed 44 of the 234 surfaces here, every
  one of them between 0.0054 and 0.0073 m; per coordinate all 234 pass. Stated with its derivation
  so that a later reader neither tightens it nor reaches for a distance.

Fenestration additionally compares its parent surface name against the report's base surface column,
which is how the oracle states a window's parent.

## The engine's own surfaces, and why they are not in the expectations

The report describes the surfaces EnergyPlus **ended up with**, which is not the set the model
states. Three kinds of row are the engine's rather than the model's, and no extractor should produce
any of them.

| row | what it is | where |
| --- | ---------- | ----- |
| `Mir-<name>` | a mirrored twin of a detailed shading surface, so it shades from both sides | 36 rows across the set |
| `iz-<name>` | the reciprocal of a surface whose outside boundary condition is `Zone` rather than `Surface`: the model names the adjacent zone and the engine synthesises the other side of the wall, same vertices wound the other way | 2 rows, `lower-left-start` |
| no vertices | `InternalMass` states a surface area and a construction and no geometry at all, and is reported with `#Sides` of zero | 14 rows across the set |

**Every count is written into each expectation's header** rather than left silent, so each exclusion
is auditable from the expectation alone.

`regenerate.py` recognises the first two by the engine's own naming conventions and **deliberately
does not parse the model** to decide what is in it. Using a library under test to shape an
expectation is how an oracle stops being one. No fixture declares a surface under either prefix.

Together they are 52 rows, and they are why the engine reports 331 surfaces for these seven models
where 279 are under expectation. The specification's 331 was right about the report and wrong about
what an extractor should produce.

## The fixtures

Seven models, each present for a behaviour it isolates rather than for coverage. Committed, never
fetched: a check that downloads changes its verdict when a website changes.

| fixture | upstream model | declared rules | isolates |
| ------- | -------------- | -------------- | -------- |
| `relative-zone-origin` | `5ZoneVAV-ChilledWaterStorage-Mixed` | Relative, CounterClockWise, north 0 | the zone origin under `Relative`, 3 zones of 5 off the origin, with no north axis to confound it |
| `relative-zone-rotation` | `CmplxGlz_SingleZone_DoubleClearAir` | Relative, Counterclockwise, north 0 | a zone's own relative north, one zone at 180 degrees |
| `north-axis-multizone` | `MicroCogeneration` | Relative, CounterClockWise, north 158.434 | the building rotation, 5 zones of which 3 are off the origin |
| `world-nonzero-zone-origin` | `CooltowerSimpleTest` | World, CounterClockWise, north 0 | **the negative case**: 3 zones all carrying a non-zero origin that must NOT be applied. Also 24 fenestration and 21 detailed shading surfaces |
| `clockwise-entry` | `CrossVent_1Zone_AirflowNetwork` | Relative, **ClockWise**, north 45 | the vertex entry direction clause |
| `lower-left-start` | `SmOffPSZ-MultiModeDX` | World, CounterClockWise, **LowerLeftCorner** | the engine's renormalisation, which the ring comparison exists to absorb |
| `simplified-only-unread` | `4ZoneWithShading_Simple_1` | World, CounterClockWise, north 0 | a model this slice reads **nothing** of: every geometry object is in the simplified family, so extraction must resolve none while naming the types it did not attempt |

Each fixture is a byte-for-byte copy of the example file it came from, gzipped. The vertex report is
requested by `regenerate.py` appending the object before the run, so that a reader can diff a fixture
against the original and find nothing.

**Sizes, measured rather than asserted.** Models 87.4 kB gzipped, expectations 38.1 kB plain text,
125 kB in total.

Expectations are committed as plain text and not compressed. 279 rows are readable in a diff, and a
check whose expectations cannot be read is a check nobody audits. Only the models are gzipped, being
copies of files that exist elsewhere. Gzip and not xz or brotli, for the reason `weather-monthly`
recorded: it is the only compression both standard libraries decode, and the runners take no
dependency.

**The set does not grow without a reason recorded here.** The first such reason is already on the
record: the feature that specified this check estimated 94 kB, counting the expectations at their
gzipped size while committing them uncompressed, and counting the engine's own generated surfaces
among the 331 it expected to hold. The measured figures above are what the set actually is. Neither
number changed the design, and the estimate is left in the specification rather than quietly
corrected, because a check that restates its own past figures is a check whose history cannot be
read.

## The guards

A check that only passes proves less than one that is shown to fail. Each clause of the resolution
rule has a guard that removes it and names the fixture that then fails, and one guard works the other
way. They are listed in full once Phase 9 lands them; the shape is fixed here so the check documents
its own coverage rather than asserting it.

| guard | fails on | by |
| ----- | -------- | -- |
| remove the coordinate system clause | `world-nonzero-zone-origin` | the zone origin applied where the engine does not apply it |
| remove the building rotation clause | `north-axis-multizone` | the building not rotated as a rigid body |
| remove the vertex entry direction clause | `clockwise-entry` | up to 4.0 m, with every ring reversed |
| **add** a starting-vertex normalisation | `lower-left-start` | the author's order discarded to match a reporting convention |

**The last one is deliberate and must stay failing.** Normalising the extractor's starting vertex to
match the engine's will look like an improvement to a future maintainer, because it makes a
comparison simpler. It makes extraction lossy for no reader's benefit, and `lower-left-start` is in
the set for exactly that. Read this paragraph before touching it.

## Regenerating

`regenerate.py`, with EnergyPlus installed. CI never runs it: the expectations are committed, which
is the corpus's habit everywhere, so the check needs no external tool and cannot change its verdict
because a tool was upgraded underneath it.

```bash
python checks/geometry-vertices/regenerate.py
```

It appends the report request, runs each model with `-x -D`, and writes `expected/<fixture>.csv`.
`-x` expands `HVACTemplate:*`, without which such a model stops before it reports. `-D` runs design
days only: the vertex report is written during input processing, so a full annual run produces
identical rows for a great deal more time.

## Running it

Each language has its own entry point, beside the case runners and not inside them, following the
pair `weather-monthly` established:

```bash
python runners/geometry_check.py --library /path/to/idfkit
node runners/geometry-check.mjs --library /path/to/idfkit-js
```

Both take a **path**, never a language word: the runner file already fixes the language. Both exit 0
when every comparison is green, 1 on any disagreement, and 2 when the run could not start, which is
what a library without the capability reports rather than a failure.

Neither runner reads the other language's output. The corpus is where the cross-language claim is
made, and each runner proves only its own side against the engine.

## What this check does not cover

Stated here rather than left implied, alongside the corpus's other declared coverage gaps.

- **The simplified surface family.** `Wall:Exterior`, `Window`, `Roof` and their siblings are keyed
  on origin, width, height and tilt rather than on vertices, and this slice does not read them.
  `simplified-only-unread` checks that a library **says so** rather than looking empty, which is the
  whole of the coverage here. Resolving them needs its own rule set, its own fixtures and its own
  oracle rows.
- **The three per-class detailed forms.** `Wall:Detailed`, `Floor:Detailed` and
  `RoofCeiling:Detailed` state explicit vertices and would resolve by the rule this check already
  proves. No fixture holds one, so nothing here constrains them and a library must report them as
  not attempted rather than read them. Promoting them means a fixture and its oracle rows first,
  which is the cheapest gap in this list to close.
- **Models the engine cannot run.** Expectations are generated offline, so a model that will not run
  is a model not chosen. Of 27 sampled while specifying this, 17 produced a report; the failures
  have identifiable causes rather than being arbitrary, being `HVACTemplate:*` without the expansion
  flag, `GroundHeatTransfer:*` needing a preprocessor the flag does not cover, and models with no
  sizing period. All seven fixtures run.
- **Reading from disk.** The runners hand each library a string, as they do for every case, so
  neither library's own file reading is exercised. That is a separate `checks/` member the corpus
  already tracks.
- **The starting vertex.** Not covered on purpose, and guarded against being covered. See the guards
  above.
