"""Validate the version roster: rules V1 to V13 of contracts/version-roster.md.

The validator reads only the file. It imports neither library and reaches no network, so it cannot
fail because a project drifted. Whether the projects match the roster is the scan's job.

    python -m tools.energyplus.check_roster [--roster governance/energyplus.toml]

Exit 0 when every rule holds, 1 otherwise. Every failure names its rule and its location.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from .roster import (
    END_TO_END_IDS,
    AssetRole,
    SCHEMA_VERSION,
    LagKind,
    OpeningKind,
    Project,
    Roster,
    RosterError,
    Step,
    StepClass,
    members,
    string_values,
)

ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_ROSTER = ROOT / "governance" / "energyplus.toml"

_RELEASE_TEXT = re.compile(r"(?<![\d.])\d{1,2}\.\d{1,2}\.\d{1,2}(?![\d.])")
#: Commands that publish, deploy, release or merge. The run proposes changes and does nothing else (FR-025).
_PUBLISHING_COMMAND = re.compile(
    r"\b(npm publish|pnpm publish|yarn publish|uv publish|twine upload|gh release create|gh pr merge|"
    r"cdk deploy|docker push|git push|git tag)\b"
)
_PUBLISHING_TITLE = re.compile(r"^\s*(publish|deploy|release)\b", re.IGNORECASE)
#: Keys whose text may name a release: the reason a pin or an ignore exists.
_REASON_KEYS = ("pin", "ignore")


@dataclass(frozen=True, slots=True)
class Failure:
    rule: str
    where: str
    message: str

    def stated(self) -> str:
        return f"{self.rule} {self.where}: {self.message}"


def _duplicates(ids: Sequence[str]) -> list[str]:
    return sorted({item for item in ids if ids.count(item) > 1})


def _steps(roster: Roster) -> list[tuple[str, Step]]:
    owned = [(project.id, step) for project in roster.projects for step in project.steps]
    return owned + [("end-to-end", step) for step in roster.verifications]


def v1_unique_ids(roster: Roster) -> list[Failure]:
    failures = [Failure("V1", "file", f"project id {name!r} is used more than once") for name in _duplicates([p.id for p in roster.projects])]
    if roster.schema_version != SCHEMA_VERSION:
        failures.append(Failure("V1", "[roster]", f"schema_version is {roster.schema_version!r}, expected {SCHEMA_VERSION!r}"))
    for project in roster.projects:
        for name in _duplicates([step.id for step in project.steps]):
            failures.append(Failure("V1", f"project {project.id}", f"step id {name!r} is used more than once"))
    return failures


def v2_content_or_reason(roster: Roster) -> list[Failure]:
    failures = []
    for project in roster.projects:
        if bool(project.content) == bool(project.carries_none_reason):
            failures.append(Failure("V2", f"project {project.id}", "state at least one `content` kind or a `carries_none_reason`, and never both"))
        if not project.content and project.steps:
            failures.append(Failure("V2", f"project {project.id}", "a project that carries no content has no steps"))
    return failures


def v3_one_class(roster: Roster) -> list[Failure]:
    # A missing or unknown class is rejected while the file is read. What remains is consistency:
    # an automated step carries none of the fields that explain why a step is not automated.
    return [
        Failure("V3", f"project {owner} step {step.id}", "an automated step states no `condition` and no `class_reason`")
        for owner, step in _steps(roster)
        if step.step_class is StepClass.AUTOMATED and (step.condition or step.class_reason)
    ]


def v4_not_automated_is_explained(roster: Roster) -> list[Failure]:
    failures = []
    for owner, step in _steps(roster):
        if step.step_class is StepClass.AUTOMATED:
            continue
        for field, value in (("condition", step.condition), ("class_reason", step.class_reason), ("inspect", step.inspect)):
            if not value:
                failures.append(Failure("V4", f"project {owner} step {step.id}", f"a {step.step_class.value} step requires `{field}`"))
    return failures


def v5_manual_prepares(roster: Roster) -> list[Failure]:
    return [
        Failure("V5", f"project {owner} step {step.id}", "a manual step requires `prepares`")
        for owner, step in _steps(roster)
        if step.step_class is StepClass.MANUAL and not step.prepares
    ]


def v6_nothing_publishes(roster: Roster) -> list[Failure]:
    failures = []
    for owner, step in _steps(roster):
        where = f"project {owner} step {step.id}"
        if step.run and (found := _PUBLISHING_COMMAND.search(step.run)):
            failures.append(Failure("V6", where, f"`run` contains {found.group(0)!r}; a step never publishes, deploys, releases or merges"))
        if _PUBLISHING_TITLE.search(step.title):
            failures.append(Failure("V6", where, f"the title {step.title!r} names a publication; that action belongs to a maintainer"))
    for project in roster.projects:
        if found := _PUBLISHING_COMMAND.search(project.gate):
            failures.append(Failure("V6", f"project {project.id}", f"`gate` contains {found.group(0)!r}"))
    return failures


def v7_reasons(roster: Roster) -> list[Failure]:
    failures = []
    for where, ignores in [("[roster]", roster.default_ignores)] + [(f"project {p.id}", p.ignores) for p in roster.projects]:
        for ignore in ignores:
            label = ignore.glob or ignore.pattern
            if not ignore.reason.strip():
                failures.append(Failure("V7", where, f"ignore {label!r} has no reason"))
            if bool(ignore.glob) == bool(ignore.pattern):
                failures.append(Failure("V7", where, f"ignore {label!r} states exactly one of `glob` and `pattern`"))
    for where, pins in [("[roster]", roster.default_pins)] + [(f"project {p.id}", p.pins) for p in roster.projects]:
        for pin in pins:
            if not pin.reason.strip():
                failures.append(Failure("V7", where, f"pin {pin.glob!r} has no reason"))
    return failures


def v8_generated_by_a_step(roster: Roster) -> list[Failure]:
    return [
        Failure("V8", f"project {project.id}", f"generated {generated.glob!r} names step {generated.by_step!r}, which the project does not have")
        for project in roster.projects
        for generated in project.generated
        if generated.by_step not in {step.id for step in project.steps}
    ]


def _waits_on(project: Project) -> list[str]:
    return [o.project for o in project.opens_on if o.kind in (OpeningKind.MERGED, OpeningKind.PROJECT_COMPLETE) and o.project]


def v9_openings_resolve(roster: Roster) -> list[Failure]:
    failures = []
    by_id = {project.id: project for project in roster.projects}
    for project in roster.projects:
        for opening in project.opens_on:
            if opening.project is None:
                continue
            target = by_id.get(opening.project)
            if target is None:
                failures.append(Failure("V9", f"project {project.id}", f"opens on project {opening.project!r}, which the roster does not list"))
            elif opening.step is not None and opening.step not in {step.id for step in target.steps}:
                failures.append(Failure("V9", f"project {project.id}", f"opens on step {opening.step!r}, which {target.id} does not have"))
    stated: dict[AssetRole, tuple[str, str]] = {}
    for project in roster.projects:
        for opening in project.opens_on:
            for role in opening.roles if opening.ubuntu else ():
                first = stated.setdefault(role, (opening.ubuntu or "", project.id))
                if first[0] != opening.ubuntu:
                    # The source recorded for a release names one asset per role, so one build per role.
                    failures.append(Failure("V9", f"project {project.id}", f"states Ubuntu {opening.ubuntu} for {role.value}, and {first[1]} states {first[0]}"))
    if failures:
        return failures
    done: set[str] = set()

    def visit(project_id: str, trail: tuple[str, ...]) -> None:
        if project_id in trail:
            cycle = " -> ".join((*trail[trail.index(project_id) :], project_id))
            failures.append(Failure("V9", f"project {project_id}", f"the opening graph has a cycle: {cycle}"))
            return
        if project_id in done:
            return
        for upstream in _waits_on(by_id[project_id]):
            visit(upstream, (*trail, project_id))
        done.add(project_id)

    for project in roster.projects:
        if not failures:
            visit(project.id, ())
    return failures


def v10_lag(roster: Roster) -> list[Failure]:
    failures = []
    for project in roster.projects:
        lag = project.lag
        if lag is None:
            continue
        if lag.kind is LagKind.NOT_YET and not lag.issue:
            failures.append(Failure("V10", f"project {project.id}", "a `not-yet` lag requires an `issue`"))
        if lag.kind is LagKind.DELIBERATE:
            if not lag.reason:
                failures.append(Failure("V10", f"project {project.id}", "a `deliberate` lag requires a `reason`"))
            elif _RELEASE_TEXT.search(lag.reason):
                failures.append(Failure("V10", f"project {project.id}", "the reason of a lag states a policy and never a release"))
    return failures


def v11_states_no_release(text: str) -> list[Failure]:
    failures = []
    for value in string_values(text):
        if value.key == "reason" and any(f"/{kind}" in value.where for kind in _REASON_KEYS):
            continue
        if found := _RELEASE_TEXT.search(value.text):
            failures.append(Failure("V11", f"{value.where}/{value.key}".lstrip("/"), f"{found.group(0)!r} reads as an EnergyPlus release; the roster never states one"))
    return failures


def v12_patterns(roster: Roster) -> list[Failure]:
    # Compilation and the named group are rejected while the file is read. What remains is that a
    # verification which reads a file names a pattern that can match something.
    failures = []
    for owner, step in _steps(roster):
        for member in members(step.verify):
            pattern = getattr(member, "pattern", None)
            if pattern is not None and not pattern.strip():
                failures.append(Failure("V12", f"project {owner} step {step.id}", "an empty pattern matches everything"))
    return failures


def v13_end_to_end(roster: Roster) -> list[Failure]:
    present = [step.id for step in roster.verifications]
    failures = [Failure("V13", "[[verification]]", f"the end-to-end verification {name!r} is absent") for name in END_TO_END_IDS if name not in present]
    failures += [Failure("V13", "[[verification]]", f"{name!r} is not one of the seven end-to-end verifications") for name in present if name not in END_TO_END_IDS]
    return failures


RULES: tuple[Callable[[Roster], list[Failure]], ...] = (
    v1_unique_ids,
    v2_content_or_reason,
    v3_one_class,
    v4_not_automated_is_explained,
    v5_manual_prepares,
    v6_nothing_publishes,
    v7_reasons,
    v8_generated_by_a_step,
    v9_openings_resolve,
    v10_lag,
    v12_patterns,
    v13_end_to_end,
)


def check(text: str) -> list[Failure]:
    """Every failure in the roster text. A file that cannot be read as a roster yields one failure."""
    try:
        roster = Roster.from_text(text)
    except RosterError as error:
        return [Failure(error.rule, error.where, error.message)]
    failures = [failure for rule in RULES for failure in rule(roster)]
    return failures + v11_states_no_release(text)


@dataclass(frozen=True, slots=True)
class Tally:
    with_content: int
    without_content: int
    steps: int
    automated: int
    reviewed: int
    manual: int

    def stated(self) -> str:
        return (
            f"{self.with_content} projects with content, {self.without_content} without, {self.steps} steps: "
            f"{self.automated} automated, {self.reviewed} reviewed, {self.manual} manual"
        )


def tally(roster: Roster) -> Tally:
    steps = [step for _, step in _steps(roster)]

    def count(step_class: StepClass) -> int:
        return sum(1 for step in steps if step.step_class is step_class)

    with_content = len(roster.rostered())
    return Tally(with_content, len(roster.projects) - with_content, len(steps), count(StepClass.AUTOMATED), count(StepClass.REVIEWED), count(StepClass.MANUAL))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--roster", type=Path, default=DEFAULT_ROSTER)
    arguments = parser.parse_args(argv)
    text = arguments.roster.read_text(encoding="utf-8")
    failures = check(text)
    for failure in failures:
        print(failure.stated(), file=sys.stderr)
    if failures:
        print(f"{arguments.roster}: {len(failures)} rule failure(s)", file=sys.stderr)
        return 1
    print(f"{arguments.roster}: {tally(Roster.from_text(text)).stated()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
