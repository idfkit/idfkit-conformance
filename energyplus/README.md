# EnergyPlus release adoption

This directory and `tools/energyplus/` hold the procedure by which the idfkit suite adopts a new
EnergyPlus release. It is the only procedure. It replaces the `update-energyplus-version` skill,
which was kept on one machine under no version control (section 8).

## 1. Defined terms

| Term | Meaning |
| --- | --- |
| Release | A non-draft upstream release whose tag is exactly `vX.Y.Z`, not marked as a prerelease, with at least one asset |
| Version roster | `governance/energyplus.toml`. The projects, their steps, and where each states the latest release |
| Adoption | Every rostered project carrying a release, verified step by step |
| Tracking issue | One issue per release in this repository, titled `Adopt EnergyPlus X.Y.Z`, whose body is the checklist |
| Checklist | One evaluation of one release against the roster, rendered as Markdown |
| Adoption record | `energyplus/adoptions/X.Y.Z.toml`, written once when an adoption completes |
| Opening | What must be observable before a project's steps may start |

## 2. How an adoption starts

Detection is automatic. `energyplus-watch.yml` runs daily, lists upstream releases, and creates the
tracking issue for each release that is newer than the newest supported release and has no record.

To start or advance one by hand:

```bash
gh workflow run energyplus-watch.yml --repo idfkit/idfkit-conformance -f release=X.Y.Z
```

That is the one starting action. A release that upstream did not publish as a release is refused
with the cause stated, as is a release that would skip an older one whose issue is neither completed
nor set aside.

The watch dispatches each project's `adopt-energyplus.yml` when the project's openings hold, if the
repository variable `ENERGYPLUS_DISPATCH` is `true`. Without the variable it evaluates and renders
only, and a maintainer dispatches a project by hand:

```bash
gh workflow run adopt-energyplus.yml --repo idfkit/<project> -f release=X.Y.Z
```

A run dispatched by hand is held to the conditions the watch applies before it dispatches:

1. It is dispatched on the project's default branch. Any other ref is refused.
2. The project's openings hold. Otherwise the run reports `not-open` and changes nothing.
3. The release is the newest. A release older than the newest supported one, which the project does
   not carry, is refused.
4. One run at a time adopts a release in a project. A second run for the same release waits.

A run that finds nothing it can propose reports `waiting` and opens no pull request. The watch does
not dispatch that project again until its default branch has moved, which is what the merge of a
library bump does. Run by hand outside a workflow, the tool leaves the rewritten statements in the
working tree in that case; in the workflow nothing is pushed unless the result is `proposed`.

The engine takes at least three runs, because its builds outlast a run: the first proposes the
branch, the second dispatches the builds, and the third composes the asset set. A maintainer
dispatches the adoption again when a build has completed; the checklist says so on the step.

### What bounds a run

A caller's `permissions:` block bounds only the default token, which the run uses for nothing but
reading. What a run can do is bounded by four tokens of the idfkit-bot application, each minted for
one purpose:

| Token | Scope | Used by |
| --- | --- | --- |
| reading | Every repository, read only | The roster's commands and the project's gate. A project's tests, hooks and dependencies never hold a token that can write |
| dispatch | Actions write in the project's repository only, and only where the roster states `dispatches = true` | The engine's commands that start its own builds, through `ADOPTION_DISPATCH_TOKEN` |
| proposing | Contents, pull requests and workflows write in the project's repository only | The two steps that push the branch and open the pull request, after the gate has passed |
| watch | Actions write in idfkit-conformance only | The last step, which asks the watch to evaluate again |

The roster and the tools are read at the pinned tag into a directory outside the project's tree, so
that a gate which reads the whole tree does not judge them.

### Two governance levels in one project

A project's adoption caller pins the tag at which it reads this roster and these tools. Its other
workflows pin the tag at which they read the consumer register, and each library pins the level its
naming and parity gates read. These are separate readers of separate files and move separately. A
caller's tag is raised when the roster's entry for that project, or the tools, change.

## 3. How to read the checklist

1. The line under the title names exactly one next action and who can take it, or states that none
   is owed.
2. Sections are ordered by what opened them. A section headed "Waiting" names what it waits on.
3. Each step has one of five verdicts.

| Verdict | Meaning |
| --- | --- |
| passed | The step's verification holds. The landing column says whether on a default branch (`merged`), on a package index (`released`), or only on an open pull request (`proposed`) |
| failed | A check or a run failed. The row names it. `pre-existing` means the default branch was failing before any adoption |
| awaiting person | A reviewed or manual step whose content is proposed and whose acceptance has not been given |
| not applicable | The roster records a lag for the project, with its reason |
| not started | The project's openings do not hold yet, or nothing has been dispatched |

4. The verdicts are computed from observable state at every evaluation. No file records that a step
   passed, so a verdict cannot be set by hand and an interrupted adoption has nothing to recover.
5. A line prefixed `STALE` means the evaluation is older than 3 days and the watch has stopped.

To produce the checklist locally, from sibling checkouts read at `origin/HEAD`:

```bash
python -m tools.energyplus.evaluate --release X.Y.Z --workspace .. --out evaluation.json
python -m tools.energyplus.checklist evaluation.json
```

## 4. What each step class asks of a person

| Class | Count at first writing | What a person does |
| --- | --- | --- |
| automated | 52 | Nothing. Merge the pull request that carries it |
| reviewed | 7 | Inspect what the row names, against the acceptance condition stated under the table, and approve or merge the pull request. The approval is the acceptance |
| manual | 3 | Perform the step from what the run prepared, then approve or merge |

The engine's builds are identified by what they hold, not by their tag. Its prereleases are tagged
`wasm-vX.Y.Z`, and the patch slot counts that project's own rebuilds, because a published package
version can never be replaced: `wasm-vX.Y.1` is a rebuild of release X.Y.0. The `Energy+.idd` a prerelease
carries states the release it was built from, and the roster verifies that. Should upstream publish
a patch release, the engine's dispatch command refuses to write to a tag that already holds another
release, and a maintainer chooses the tag.

A step is classed other than automated only for one of three stated conditions: its effect is
silent when wrong, it requires a choice no recorded rule decides, or it publishes. The roster states
the condition and the reason for each.

A run writes the declared statements first, then runs each step's command in roster order, and stops
at the first step whose content another workflow of the project moves, such as a library level. No
later command runs on a tree that lacks what it needs. What the command of a reviewed or manual step
prints is carried into the pull request body, and what it writes under `.energyplus-adoption/` is
uploaded as the artifact `adoption-prepared` and is no part of the change. The gate runs on the
staged tree. A file a step is declared to generate may change even where a pin's glob covers it.

An approval stands only for the commit it was given on. When the adoption branch is regenerated
after an approval, the step returns to awaiting a person, and the approval is given again on the new
head. A reviewer's later request for changes replaces their approval.

Where upstream publishes builds for two Ubuntu releases, a project consumes the build for the oldest
one unless its opening in the roster states another with `ubuntu = "24.04"`. Two projects cannot
state different builds for the same architecture.

After a project's pull request merges, the checklist names what remains for a maintainer: a
publication, a release, a deployment. No tool performs those. The run pauses at each one, and
resumes at the next evaluation after the publication is observable.

## 5. Setting a release aside

Close its tracking issue as not planned, add the label `set-aside`, and state the reason in a
comment. The watch then evaluates and writes nothing for that release, and a newer release is no
longer held back by it. A release set aside has no adoption record.

## 6. How completion is recorded

When every step is passed on a default branch or a package index, or not applicable, when no
occurrence is unaccounted for, and when the upstream source is unchanged since the first evaluation,
the watch writes `energyplus/adoptions/X.Y.Z.toml` and opens a pull request that adds it. The record
holds every step's verdict and evidence, the upstream tag, the commit it points to, each consumed
asset with its digest, and the revision of this repository that evaluated it. `record.py` refuses to
write a record in any other state.

Merging that pull request marks the adoption complete. Close the tracking issue with it. A record
is never edited after merge. A correction is a new file beside it, `X.Y.Z.correction-N.toml`, with
a reason.

## 7. Keeping the roster true

`python -m tools.energyplus.scan --workspace ..` searches every tracked file of every rostered
project for the newest supported release in eight spellings. Each occurrence must be a declared
`statement`, or lie under a `generated`, `pin` or `ignore` declaration. An occurrence under none is
a stranger and is reported with its project, file and line. The weekly sweep runs the same scan over
the organisation and also reports any scheduled workflow that GitHub disabled for inactivity.

| Command | Purpose |
| --- | --- |
| `python -m tools.energyplus.check_roster` | The roster is well formed (rules V1 to V13). Reads only the file |
| `python -m tools.energyplus.scan --workspace ..` | Every project matches the roster |
| `python -m tools.energyplus.scan --workspace .. --working-tree <repo>` | The same, with one checkout read as it is on disk, for checking an edit |
| `python -m pytest tests/energyplus -q` | The checks themselves can fail |

Three kinds of text are outside what a statement can express, and need a person at each adoption:

1. A count beside a measurement ("13060 fields across the 17 bundled versions"). The count is
   rewritten and the measurement is not re-measured.
2. A list that needs an element appended (a version table, a printed list). The pattern matches the
   last entry, so it holds after a correct append and a rewrite alone would replace the entry.
3. A count that is not the number of supported releases (the number of releases minus one, an
   upper-case word).

## 8. Carried from the previous procedure

The previous procedure was the skill `update-energyplus-version`, a single unversioned file in the
workspace root's `.claude/skills/`, last modified on 2026-04-16. It covered 5 of the 13 projects
that carry version-dependent content.

| Kept, restated as roster steps | Reason |
| --- | --- |
| idfkit: add the release to the registry, bundle the schema, regenerate stubs, update the sweep matrix | Still how the first library adopts. The registry step precedes the schema step because the downloader refuses an unlisted version |
| idfkit-mcp and idfkit-lsp: move the library level, run the gate | Still correct. The level is moved by each project's existing bump workflow, which the adoption verifies and does not perform |
| idfkit-docs: add the release to the target list and designate it latest | Still correct, with one rule added: the pinned library must carry the release first, because the library returns its newest schema for a release it does not list |

| Not kept | Reason |
| --- | --- |
| The envelop phase | The code it describes was removed on 2026-09-01. The application reads the supported range from the schema bundle at run time and discovers engine assets at run time |
| The transition rule guidance for envelop | No project in the suite translates transition rules by hand. Migration uses upstream's own transition programs, natively and compiled to WebAssembly |
| `generate_variable_mappings.py` | It generated a mapping for code that no longer exists. Renamed output variables are reported in the checklist's change summary, read from upstream's transition table |

The skill's directory was removed on 2026-10-13, after this file was merged. It was under no version
control and no copy was kept: what remained valid is in the first table above.
