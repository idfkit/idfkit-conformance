# Validation semantics

**Normative.** This file governs what both libraries report when they validate a field whose schema
declares `anyOf`. Where an implementation disagrees with this file, the implementation is wrong.
`compare.md` governs how two results are compared; this file governs what a result should contain,
and assertion 5 is where the two meet.

Written 2026-09-02, after both libraries were found to UNDER-validate. Python never read the
constraints inside an `anyOf` branch at all, and TypeScript suppressed its own hoisted copies of
them in order to stay in agreement with Python. Both were green against each other throughout.

## The shapes that exist

Measured across all 17 bundled EnergyPlus schemas. These are all of them; there is no other shape.

| branch types        | branch 0 enum                       | branch 1 enum         | count |
| ------------------- | ----------------------------------- | --------------------- | ----- |
| `number`, `string`  | none                                | `Autosize`            | 6056  |
| `number`, `string`  | none                                | `''`, `Autosize`      | 4501  |
| `number`, `string`  | none                                | `''`, `Autocalculate` | 1152  |
| `number`, `string`  | none                                | none, so any string   | 646   |
| `number`, `string`  | none                                | `Autocalculate`       | 629   |
| `number`, `string`  | `0,1` / `1,2` / `1,3` / `0,1,2,3,5` | `''`                  | 68    |
| `integer`, `string` | none                                | `''`, `Autosize`      | 8     |

Three consequences an implementation must respect:

1. The sentinel is not always `Autosize`. 1,781 fields accept `Autocalculate` instead. A library
   that accepts either word on every field accepts values EnergyPlus rejects.
2. 646 fields put no enum on the string branch at all, so any string is legal there.
3. 68 fields carry a numeric enum on the number branch. `3` is invalid where the enum is `0,1`.
   That is a choice field expressed as numbers, and it must be enforced.

## The rule

A field value is valid when it fully satisfies at least one branch. "Fully" means the branch's type,
its enum if it has one, and its bounds if it has any. This is ordinary JSON Schema `anyOf`. The
defect in both libraries was checking type in one place and constraints in another, so a value could
satisfy branch A's type and branch B's absence of bounds and be accepted having satisfied neither
branch completely.

Evaluate as follows, and emit at most one finding per field.

1. Collect the branches whose **type** the value satisfies.
2. If that set is empty, emit `E002`. Stop.
3. If any branch in the set also satisfies its enum and its bounds, the value is valid. Emit
   nothing. Stop.
4. Otherwise the value matched a branch on type and failed it on a constraint. Take the **first**
   branch in declaration order from the set and emit its most specific failure, using the same codes
   a field with no `anyOf` would use:
   - enum violated: `E004`
   - below `minimum`: `E005`; not above an exclusive minimum: `E006`
   - above `maximum`: `E007`; not below an exclusive maximum: `E008`

Step 4 is what keeps the diagnostics useful. Emitting `E002` for an out-of-range number would tell
the user their number is not a number.

## Type satisfaction

- `number` is satisfied by an int or a float, and **not** by a bool.
- `integer` is satisfied by an int, or by a float whose value is integral. Not by a bool.
- `string` is satisfied by a string.
- An empty string is a value like any other here. It satisfies `string`, and where the branch enum
  contains `''` it satisfies that enum too.

Callers skip validation entirely for a field that is absent or empty, so in practice `''` reaches
this rule only when a caller validates a value directly.

## Enum satisfaction

String comparison is case-insensitive, matching what both libraries already do for a plain enum
field. Numeric enum members compare by value.

## Bounds and the two JSON Schema dialects

Schemas for 8.9.0 through 9.5.0 are draft-04, where `exclusiveMinimum` and `exclusiveMaximum` are
**booleans** qualifying the sibling `minimum` and `maximum`. From 9.6.0 they are draft-06 or later,
where the same keys carry the bound itself. Measured against the epJSON schemas: 9,893 boolean
occurrences in the older seven versions and 15,180 numeric ones in the newer ten, with no version
mixing the two. 144 of the numeric bounds are JSON integers rather than floats, so a type guard must
accept both.

A boolean `true` compared as a bound silently becomes `1` and rejects every value at or below 1 in a
positive-bounded field. Both libraries must branch on the **type of the value**, never on the
version.

## What this changed

Under the old behaviour `view_factor_to_ground = 5.0` produced no finding at all, against a schema
declaring `maximum: 1.0`. Under this rule it produces `E007`. Roughly 598 fields per version were
unbounded in practice and are now bounded.

`ceiling_height = "Bogus"` also produced no finding, because the string branch's type matched and
its enum was never consulted. Under this rule it produces `E004`, not `E002`: the string branch
matches on type, so step 1 collects it and step 2 never fires; the branch then fails on its enum,
which is step 4. `E002` is reserved for a value that matches no branch on type, such as a list where
only a number or a string is legal. For the 646 fields whose string branch carries no enum, any
string stays valid.

`cases/tier1-validation-anyof-branch-constraints` is this section made observable.

## The codes

Fixed vocabulary, shared by both libraries and compared exactly.

| Code   | Meaning                                        |
| ------ | ---------------------------------------------- |
| `E001` | required field missing or blank                |
| `E002` | value matches none of a field's permitted types |
| `E003` | type mismatch on a single-typed field          |
| `E004` | value not among a field's permitted values     |
| `E005` | below the minimum                              |
| `E006` | not above an exclusive minimum                 |
| `E007` | above the maximum                              |
| `E008` | not below an exclusive maximum                 |
| `E009` | reference to a name no object declares         |
| `E010` | singleton constraint violated                  |
| `W002` | unknown object type                            |
| `W003` | unknown field                                  |

## Declaring a name, and pointing at one

`E009` rests on knowing which names a document declares, and the schema states that in two places,
not one.

- A field carrying `object_list` **points into** the reference lists it names.
- A field carrying `reference` **contributes** its value to the reference lists it names.

The second is independent of whether the object has a name field. `FluidProperties:Name` declares no
name field and names a refrigerant through `fluid_name`, which carries
`reference: ["FluidAndGlycolNames", "FluidNames"]`. Eleven fields in the 26.1.0 schema declare a
reference this way. A validator that indexes only name fields reports every pointer at such a name
as dangling, which produced 3,640 false `E009` findings across the 760 EnergyPlus example files.

Both keywords apply inside extensible groups exactly as they do at the top level. 90 schema fields
carry `object_list` inside a repeated group, including `Schedule:Year`, `Schedule:Week:Compact`,
`SpaceList` and `ZoneList`, and a reference index built from top-level fields alone can never report
any of them.

Four reference lists are pointed into and never contributed to: `validBranchEquipmentTypes`,
`validCondenserEquipmentTypes`, `validOASysEquipmentTypes` and `validPlantEquipmentTypes`. They
enumerate object **type** names rather than object names, so nothing in a document can ever populate
them, and a pointer into one of them is not a dangling reference. Treating them as ordinary lists
produced 992 further false findings.

`cases/tier1-validation-reference-declared-by-field` and
`cases/tier1-validation-reference-in-extensible-group` pin this section.

## What is not settled here

EnergyPlus knows a set of built-in fluids, `PropyleneGlycol` and friends, that a model may point at
without declaring. Neither library models them, so roughly 40 `E009` findings remain against the
example files in both languages. They agree, and they are both wrong in the same way. Closing it
means hardcoding a domain list, which is a decision this file does not take.
