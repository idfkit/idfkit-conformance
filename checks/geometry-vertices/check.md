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

**And the engine's side is exhausted, not just the library's.** The comparison walks the surfaces
the library returned and looks each one up in the expectation, so on its own it cannot fail on an
omission: a library that dropped a wall from `north-axis-multizone` would compare 233 surfaces
instead of 234, every one of them green, and exit 0. The engine's report is the authority on what
the model holds, so every row in it must be matched by a resolved surface.

**One exemption, derived rather than named.** A library reporting unattempted types is saying the
model holds geometry this slice does not read, and the engine reported those surfaces anyway.
`simplified-only-unread` is such a model: 45 reported surfaces against 43 unattempted objects, which
is not an accounting error but the engine's own expansion, since a `Shading:Fin` becomes two
surfaces and the model holds two of them. Counting objects against surfaces there would mean
teaching this check the engine's expansion rules, which is the knowledge `regenerate.py` refuses to
hold. So the rule asks its question only of a model the library attempted in full: six of the seven
fixtures, and every one of the 234 surfaces the check compares.

The `unresolved` count in the verdict line is read from the library's own scene. It is zero on all
seven fixtures, and it is a measurement rather than a constant: a library that could not place an
object names it there and the count says so.

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
rule has a guard that removes it and names the fixture that then fails, and a fourth guard works the
other way round. All four are implemented in both runners and every magnitude below is measured
rather than asserted.

| guard | removes | fails on | by | over |
| ----- | ------- | -------- | -- | ---- |
| `--without coordinate-system` | clause one, so the zone origin is applied where the model declares `World` | `world-nonzero-zone-origin` | **201.9800 m** | 78 of its 99 surfaces |
| `--without north-axis` | clause two, so the building is not turned by its north axis | `north-axis-multizone` | **22.5571 m** | all 34 of its surfaces |
| `--without entry-direction` | clause three, so a clockwise ring is not wound back | `clockwise-entry` | **4.0000 m** | all 8 of its surfaces |
| `--without starting-vertex` | the ring comparison's insensitivity to where a ring starts | `lower-left-start` | **17.5900 m** | all 41 of its surfaces |

```bash
python runners/geometry_check.py --library /path/to/idfkit --without north-axis
node runners/geometry-check.mjs --library /path/to/idfkit-js --without north-axis
```

Both runners print the same transcript for the same guard, magnitudes included.

### How a guard removes a clause

From the library's **output**, never from its source: the corpus cannot reach into either library
and has to ask the same question of both. Two of the three undoings are exact. The entry direction
clause reverses a ring while holding its first vertex, so applying it twice is applying it never.
The north axis clause is a rotation about the world origin, and a rotation is exactly invertible.

The coordinate system clause is the one that is undone by **applying** something rather than by
reversing it: a library that never wrote the clause applies the zone origin whatever the model
declares, so the guard adds the origin back on the models that declare `World`. That makes it exact
on those models and a no-op on the others, which is why it is applied only where `World` is
declared: applying it on a model already resolved under `Relative` would measure a double shift, a
third answer neither library would ever give.

**The origin is turned by the building axis before it is added**, and that is what makes the undoing
exact rather than nearly exact. Clause one runs before clause two, so a library missing clause one
returns `R(v + o)` where `R` is the building rotation, while the guard is handed `R(v)` and can only
add. `R(v + o)` is `R(v) + R(o)`, so the origin is turned by the same angle first. Adding it unturned
is right on every committed fixture, all three of which declare `World` with a zero axis, and 2.74 m
wrong at an axis of 45 degrees on a zone origin of (1.98, 4.58). A guard that is exact only where the
fixture set happens to be silent is a guard that will mislead the first model that breaks the
silence, so `runners/tests/guard_fixtures.json` carries a case at 45 degrees.

Two things that guard does not do, one because the fixture set does not exercise it and one because
the scene does not state it. Both recorded here rather than left to a reader of the source:

- It does not apply the zone's `direction_of_relative_north`, which clause one also governs. No
  model in the set declares `World` and carries a non-zero zone rotation, so a branch for it would
  be an untested path standing in for a proof.
- It does not move a surface the library placed in no zone. Twenty-one of the ninety-nine surfaces
  in `world-nonzero-zone-origin` are `Shading:Zone:Detailed`, which resolve against the zone of the
  surface they are attached to and which a scene reports with no zone of their own. The guard moves
  the other seventy-eight, which is enough at 201.98 m. Under-reaching can only make a guard harder
  to satisfy, which is why it is the safe direction to err in here.

`runners/tests/guard_fixtures.json` is the table both runners' guards are driven over, and it is
what holds the two to the same clause. It carries 15 cases across the four clauses, and each case
states both halves of a guard: whether the clause is in scope for that model, and what removing it
produces.

### Which fixtures may fail under a guard

Not "only the named one", and the reason is a measurement. A clause fires wherever the model
declares the condition it reads, and **two fixtures declare a non-zero building north axis**:
`north-axis-multizone` at 158.434 degrees and `clockwise-entry` at 45. Removing clause two fails
both, the second by 5.7100 m, and that is the clause doing its job in a second model rather than a
second bug.

So each guard states which models it applies to, reading the library's own declaration of the model
rather than a second parse by the runner: a library that misreads its own declaration then leaves
its guard a visible no-op instead of a quiet pass. A guarded run requires three things, the last two
being the ones a weaker guard would skip:

1. the named fixture must fail;
2. it must fail by **at least the magnitude recorded above**, so that a clause reduced to a rounding
   difference cannot pass as one that matters;
3. **no fixture the clause never touched may fail**, because a clause firing on a model that
   declared no such thing is a different bug wearing this one's clothes.

A fixture the clause did touch is allowed to fail and is named in the transcript as expected
company. Exit 0 when the guard holds, 1 when the clause turned out not to matter, which is the
finding worth reporting.

**A fixture out of scope is judged exactly as an unguarded run judges it.** That holds for the
fourth guard too, which changes the comparison rather than the answer: it compares by index only on
the models it applies to. Were it to do so everywhere, a fixture declaring an upper-left start whose
authored order did not in fact begin there would fail and be reported as untouched by the clause,
which would diagnose the guard's own doing as a foreign bug.

**The set holds one model declaring clockwise entry and the specification says two.** The 4.0000 m
above is measured on the one that is committed, `clockwise-entry`, over all eight of its surfaces.
Three of the 726 geometry-bearing example models declare it, so a second fixture is available to
whoever wants the criterion met as written; nothing else here depends on the count.

### The fourth guard is deliberate and must stay failing

Read this section before touching it. It is here because the change it blocks will look like an
improvement.

`--without starting-vertex` is not like the other three. It removes a clause of the **comparison**
rather than of the resolution rule, so the library's answer is left exactly as it came: the guard
compares vertex one against vertex one instead of taking the smallest error over the ring's
rotations. That is the same thing as requiring the extractor's first vertex to be the engine's,
because an index comparison is exactly that requirement written as arithmetic.

`lower-left-start` then fails by **17.5900 m on all 41 of its surfaces**, and not one of those
surfaces is in the wrong place. The model declares `LowerLeftCorner`; the engine renormalises every
surface it reports to begin at the upper-left corner; the rings are the same rings, rotated.

The tempting change is to normalise the extractor's starting vertex to match the engine's, which
makes the comparison a single pass instead of a rotation search and makes this guard's failure go
away. **Do not.** It discards the author's own vertex order to reproduce a reporting convention,
which makes extraction lossy for no reader's benefit, and the specification that asked for this
capability asked for the author's order to survive it. `lower-left-start` is in the fixture set for
exactly this and for nothing else, and this guard is the only thing standing between that
requirement and a maintainer who is sure they are cleaning something up.

Eleven of the 726 geometry-bearing example models declare a starting corner other than the upper
left, so the cost of the change is not confined to one fixture.

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
what a library without the capability reports rather than a failure. Both also take
`--without <clause>`, which reverses the verdict and is documented under the guards above.

Neither runner reads the other language's output. The corpus is where the cross-language claim is
made, and each runner proves only its own side against the engine.

### The first language's second resolution

The first language holds two functions that resolve geometry. `get_scene` returns a scene and leaves
the document alone; `idfkit.geometry.translate_to_world` rewrites the document in place. The Python
runner takes `--via` so that the same comparison can be made of either:

```bash
python runners/geometry_check.py --library /path/to/idfkit --via translate-to-world
```

A library holding two answers to one question is the thing this check exists to stop, and this one
was holding the wrong one. The rule `translate_to_world` applied before it was corrected failed **60
of these 234 surfaces** over four of the seven fixtures, worst case **40.00 m**, and left
`clockwise-entry` unreadable by declaring the world system while leaving clockwise entry in place.
Both paths now pass all 234.

Under `--via translate-to-world` the runner reads the vertices back out of the mutated document,
which is sound only because the mutation restates the declarations it consumed: the reread document
declares the world system, a zero north axis and counter-clockwise entry, so every clause of the
rule is a no-op on it. The runner asserts that before trusting the reread and reports the run
unusable otherwise, because a reread that resolved anything would measure the rule twice instead of
the mutation once. The **declarations** it reports to the guards are the fixture's own, read before
the mutation; after it they are what the mutation left behind, which would put every fixture out of
every guard's scope and turn a guarded run into no run at all.

The flag is the first language's alone. `geometry-check.mjs` refuses it by name and exits 2, because
the second language has no mutating counterpart to choose. An unqualified run of either prints
nothing about it, so the two transcripts stay identical.

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
