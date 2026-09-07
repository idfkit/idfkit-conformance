# Check: weather-monthly

**Opens `checks/`.** The corpus contract has reserved this directory since the corpus landed. It
named two claims as needing it, reading a file from disk and retrieving weather, and gave the
criterion both meet: the claim is about a library's behaviour rather than about a document's
content. This check is neither of those two. It is a third claim meeting the same criterion, and it
is the one that arrives first. Both named gaps stay open, as `checks/README.md` records. Feature 006
examined the directory and declined to open it; this check opens it.

## What is claimed

Each library, handed the text of an EPW file, computes the same monthly figures that the EnergyPlus
Weather Converter computed from the same file, and reports a measurement that was not taken as
absent rather than as the value the format reserves for it.

## Why it cannot be a case

A case is an input file, a parsed document, and an assertion about what the library made of it,
compared against an expectation `ConvertInputFormat` produced. A weather file is none of that. There
is no model, nothing for `expected.epJSON` to hold, and `ConvertInputFormat` has nothing to say about
a weather file at all.

## The oracle, and why it qualifies

The `.stat` member of a station archive is produced by the EnergyPlus Weather Converter, not by
either library, and it ships in the same archive as the `.epw` it describes, so the expectation and
the input cannot drift apart. That is the same species of authority the corpus requires everywhere
else, and it is the reason this capability has evidence at all rather than two libraries agreeing
with themselves.

Without it this would be the first capability in the unification proven only by its own tests.

**The agreement was measured before it was relied on.** Over the thirty-four archives sampled while
specifying this, monthly means computed from the EPW matched the summary's own `Daily Avg` row on
dry bulb, dew point, wind speed and relative humidity, every station, within the summary's printed
precision. Over the six committed here it is 288 comparisons and no failures, the largest
disagreement being 0.4933 on a field the summary prints to zero decimal places.

## The two assertions

They need different files, which is why the fixture set has two groups.

**A. The aggregates.** For each of the six summary-bearing stations, each library computes the
monthly mean of dry bulb, dew point, relative humidity and wind speed from the `.epw`, and the result
is compared to `expected/<station>.json`. 288 comparisons.

**B. The absent values.** For the one sentinel-bearing file, each library's reader reports the
reserved missing values as absent and leaves the reserved observations alone. Checked against
`sentinels.toml` rather than against an external aggregate, because an absence has no external
aggregate.

Assertion B exists because assertion A cannot see it. The six summary-bearing archives are close to
sentinel-free: across all thirty-four sampled, the only reserved missing values occurring anywhere
are zenith luminance in six rows across five stations, and a flag field set everywhere. **A check
built on those alone would pass a reader that ignored sentinels entirely.**

## The tolerance, and where it comes from

Derived, not chosen. Half of the last place the summary prints, per field per station, recorded in
each expectation file as `tolerance` beside the `decimal_places` it came from. The summary prints dry
bulb, dew point and wind speed to one decimal and relative humidity to none, so the tolerances are
0.05 and 0.5 respectively. A station whose summary lacks a compared section is written with
`"covered": false` and a reason, and is reported as uncovered rather than passing silently or failing
as a defect.

## The fixtures

Committed, never fetched. A check that downloads changes its verdict when a website changes, which is
what pinning exists to prevent.

**Group A, six archives, `.epw.gz` and `.stat.gz`.** Chosen to span all three weather-converter
versions in the sample and the climate range that makes the summary's own sections vary, because the
summary emits different sections for different climates and a comparison keyed on a section only some
archives carry has to fail against the fixtures rather than against a user.

| Station | Converter | Why it is here |
| ------- | --------- | -------------- |
| Wuhan | 2026.01.31 | The oldest converter version in the sample. Humid subtropical. |
| Isachsen | 2026.06.01 | The newest. High arctic. |
| Miami | 2026.03.17 | Tropical monsoon. Emits wet and dry period sections and no seasons. |
| Fairbanks | 2026.03.17 | Subarctic. Widest annual dry-bulb range in the set. |
| Tucson | 2026.03.17 | Hot dry. Radiation near its ceiling. |
| Denver | 2026.03.17 | Cool dry at elevation, where station pressure is far from sea level. |

**Group B, one file, `.epw.gz` only.** A TMY3 file from the other common producer, carrying the
precipitation sentinel in 8,041 of 8,760 rows, the liquid precipitation quantity sentinel in 8,041,
and the albedo sentinel in 8,040. It has no summary, which is not a defect but the division of
labour above.

**Gzip, and not xz or brotli.** The only compression both standard libraries decode: Node 22's
`zlib` has no xz and Python has no brotli, and the runners take no dependency. Measured on one file
at 1.51 MB raw it costs 229 KB against 174 for xz and 171 for brotli, which is the right trade for a
format both runtimes already hold.

**The set does not grow without a reason recorded here.** At 1.7 MB it is already more than twice
the whole `cases/` tree.

**The summaries are committed even though the expectations are precomputed**, so the derivation is
auditable and re-derivable rather than a number somebody typed. The expectations are generated
offline, following the corpus's habit of committing expectations so CI needs no external tool.
Parsing the summary at check time would put back the brittleness this feature exists to avoid: it is
a report written for a person, about forty heterogeneous section shapes, tab-delimited with
inconsistent alignment, whose section set varies with the station's climate.

## What this check does not cover

Stated here rather than left implied, alongside the corpus's other declared coverage gaps.

- **Intervals the sample does not contain.** Every fixture is 8,760 hourly rows from 1 January to 31
  December. Leap years, sub-hourly intervals and partial periods are permitted by the format and
  absent from every archive published upstream. That the reader must read what the data period
  declares is checked in each library's own tests against a constructed file, not here, because a
  file neither EnergyPlus nor the Weather Converter produced has no oracle behind it.
- **The divisor of a mean.** Excluding absent hours from the sum but dividing by the month's calendar
  hours yields a mean biased towards zero, and **no committed fixture can detect it**. In every
  sampled file, from both producers, a field is either absent for a whole month or absent in exactly
  one hour of it, and a one-hour difference is about 0.14%, inside the summary's own rounding. That
  requirement carries its own guard in each library, as a constructed case asserted against the
  arithmetic.
- **Fields the summary does not aggregate.** Four are compared. The rest are covered only by the two
  libraries agreeing with each other, which is weaker, and this says so rather than implying the
  whole table is oracled.
- **Reading from disk.** The runners hand each library a string, as they do for every case, so
  neither library's own file reading is exercised. That is a separate `checks/` member the corpus
  already tracks.

## An observation, deliberately not a rule

Days Since Last Snowfall reserves 99 for missing. Both sampled producers write 88 for every hour of
the year, which is neither the missing value nor a documented reserved value nor plausibly a
measurement. It is recorded here rather than in `sentinels.toml`, because inferring a meaning from a
constant is the empirical derivation that table exists to avoid.

## Provenance of the fixtures

The six TMYx archives come from climate.onebuilding.org and the TMY3 file from the EnergyPlus sample
weather set. **Redistribution is confirmed**, so they are committed here rather than fetched, which is
what FR-014 requires: a check that downloads changes its verdict when a website changes.

Each fixture keeps the filename the archive published it under, so a reader can find the upstream
original from the fixture alone.
