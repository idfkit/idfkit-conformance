# Governance files

This directory holds the two files that fix the shared vocabulary of `idfkit` (Python) and
`idfkit-js` (TypeScript):

| File | What it records | Rendered at |
| ---- | --------------- | ----------- |
| `naming.toml` | The naming register: every concept shared across the two libraries, its spelling on each side, and every accepted difference | `idfkit-developers/docs/explanation/naming-map.md` |
| `parity.toml` | The parity ledger: every capability, its tier, and whether each language implements it completely, partially, or not at all | `idfkit-developers/docs/explanation/parity.md` |

Neither file is written by hand in a library. Both are written here, released under a tag, and
read from that tag by whatever needs them.

## Why they live here

They live in this repository precisely because it belongs to neither language (amendment A4,
FR-081).

Housing them inside `idfkit` would make Python the owner of a vocabulary that governs both
libraries, and would leave TypeScript reading them across a repository boundary with nothing to
pin. Housing them inside `idfkit-js` inverts the same problem. The corpus repository already
exists for this reason, already requires cross-language review on its contents (see
`.github/CODEOWNERS`), and already has both CIs checking it out. Putting the register and the
ledger here costs one more directory and removes the ownership asymmetry entirely.

The consequence is worth stating plainly: no library can rename a public concept, or claim a
capability, by editing a file it controls. It has to come here, and it has to get a review from a
maintainer of the other language.

## Two tag series, kept separate

This repository publishes two independent series of immutable git tags.

| Series | Governs | Declared by |
| ------ | ------- | ----------- |
| `conformance-YYYY.N` | The corpus level: one state of the case set under `cases/`, plus `manifest.json` and `known-divergence.toml` | `[tool.idfkit.conformance] level` in `idfkit/pyproject.toml`, `idfkit.conformance` in `idfkit-js/packages/core/package.json` |
| `governance-YYYY.N` | This directory: `naming.toml` and `parity.toml` | `[tool.idfkit.governance] level` in `idfkit/pyproject.toml`, `idfkit.governance` in `idfkit-js/packages/core/package.json` |

They are separate on purpose, and the purpose runs in both directions:

- **A rename must not force a corpus release.** Registering a new name, or recording a divergence
  reason, changes nothing about what the runners execute. If the register shared the corpus series,
  every naming decision would publish a new corpus level, and both libraries would have to move a
  pin that asserts something about test coverage they did not change. Corpus levels would stop
  meaning "this set of cases" and start meaning "some edit happened somewhere".
- **A corpus level advance must not force the register to move.** Adding cases is routine and
  frequent. If cases and the register shared a series, each new case would invalidate the
  governance pin, and a library that only wanted the new cases would have to re-review the whole
  shared vocabulary to take them. Worse, a library that was not ready to move its corpus pin would
  be stuck on an old register.

So the two pins move independently. It is normal and expected for a library to sit at
`conformance-2026.4` and `governance-2026.2` at the same time.

Both series are immutable once pushed. A tag is never moved, never deleted, and never re-pointed.
A mistake in a released tag is fixed by cutting the next one.

## Always read at a pinned tag, never from the default branch

Every consumer of `naming.toml` and `parity.toml` reads them at the `governance-YYYY.N` tag its own
repository declares. This holds on both sides, for gates and for documentation builds alike:

- The Python naming and parity gates read this directory at the tag named by
  `[tool.idfkit.governance] level` in `idfkit/pyproject.toml`.
- The TypeScript naming and parity gates read this directory at the tag named by
  `idfkit.governance` in `idfkit-js/packages/core/package.json`.
- The documentation macro that marks an absence at the point of use resolves capability ids against
  `parity.toml` at the same pinned tag, not against whatever is on `main` at build time.

Reading from the default branch is prohibited (FR-081). An unpinned read would let a merge in this
repository turn either library's checks red without any review on the consuming side, which is
exactly the failure the cross-language review requirement exists to prevent. It would also make a
green build unreproducible: the same commit would pass on Monday and fail on Tuesday because a
third repository moved.

Two rules follow, and the tooling enforces them (FR-084):

1. **Publish before you pin.** A tag is cut here first. Only then may a library point at it. A pin
   naming a tag that does not exist is an error, not a warning.
2. **Fail, do not fall back.** A consuming build that cannot resolve its pinned tag fails. It never
   falls back to the default branch, to a cached copy, or to the last tag it can find. A missing or
   unpinned governance artefact is a red build.

## Cutting a governance tag

A tag is cut from `main` after the change has merged, never from a branch.

1. **Land the change.** Edit `naming.toml` or `parity.toml` on a branch and open a pull request.
   `.github/CODEOWNERS` requests review from both `@idfkit/python-maintainers` and
   `@idfkit/javascript-maintainers`. That review has no override (FR-091): if a maintainer of the
   other language is unavailable, the merge waits.
2. **Check the validation rules still hold.** Concepts are unique, `divergent` entries carry a
   `divergence_reason`, `partial` availabilities carry `differences`, a capability that is no longer
   `partial` carries none, `not-yet` absences carry a tracking issue, and every `names` entry in
   `parity.toml` resolves to a concept in `naming.toml`. The repository's own CI runs these checks
   on the pull request, in `.github/workflows/governance.yml`, which runs
   `tools/validate_governance.py`. Run that script locally before opening the pull request:

   ```bash
   python tools/validate_governance.py
   ```

   It reads only these files and never imports either library, so it cannot go red because a
   library drifted. Whether the code matches the record is the library's own gate, run there
   against a pinned tag.
3. **Choose the number.** `YYYY` is the current calendar year. `N` is the next integer in that year,
   starting at 1. The series does not reset on a library release, and it has no relationship to
   either library's version number.
4. **Tag the merge commit** and push the tag:

   ```bash
   git checkout main && git pull
   git tag -a governance-2026.3 -m "governance-2026.3"
   git push origin governance-2026.3
   ```

5. **Write the release notes.** State what changed, whether any existing name moved, and whether
   any capability changed availability. A rename is the item consumers most need to see, because it
   is the one that can break their gate.

## Adopting a governance tag

Adoption is per library, deliberate, and reviewed. Neither side is pulled forward automatically.

1. Open a pull request in the library that bumps its pin, and only its pin:
   `[tool.idfkit.governance] level` in `idfkit/pyproject.toml`, or `idfkit.governance` in
   `idfkit-js/packages/core/package.json`.
2. Let the naming and parity gates run against the new tag. If the tag renamed a concept the library
   still exports under its old spelling, the gate fails here, in the library, on a pull request whose
   whole subject is the bump. That is the intended place to discover it.
3. Land any code or documentation change the new tag requires in the same pull request, so the
   library is never merged in a state where its pin and its public surface disagree.
4. Merge. The two libraries adopt on their own schedules, and a difference in adopted tag between
   them is a normal intermediate state rather than a fault.

The one ordering constraint that is not optional: a capability's entry in `parity.toml` must be
published and pinned before any documentation page renders an absence from it (FR-083). The macro
fails on an unresolvable capability id, which is what keeps that ordering honest after the first
time.
