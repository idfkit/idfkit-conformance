"""The permanent adoption record (contracts/adoption-record.md section 2).

A record is written once, when an adoption completes, to `energyplus/adoptions/X.Y.Z.toml`. It is
the same evaluation the checklist renders, with the identity of every upstream artifact consumed
and the revision of this repository that evaluated it. `from_evaluation` refuses to produce a record
while any step is unresolved, any occurrence is unaccounted for, or the upstream source changed.

    python -m tools.energyplus.record evaluation.json --out energyplus/adoptions [--issue URL]
"""

from __future__ import annotations

import argparse
import sys
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .evaluate import Evaluation, Landing, SourceAsset, UpstreamIdentity, Verdict
from .roster import END_TO_END, RECORD_STEP, AssetRole, Release, StepClass
from .serial import decode

BASELINE_ISSUE = "none: the baseline record, written for the release already supported when the process began"


class RecordRefused(ValueError):
    """The evaluation is not a complete adoption. Every cause is named."""

    def __init__(self, causes: Sequence[str]) -> None:
        super().__init__("the record was refused:\n" + "\n".join(f"  - {cause}" for cause in causes))
        self.causes = tuple(causes)


@dataclass(frozen=True, slots=True)
class RecordedStep:
    project: str
    id: str
    step_class: StepClass
    verdict: Verdict
    evidence: str | None = None
    reason: str | None = None
    accepted_by: str | None = None
    accepted_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class AdoptionRecord:
    release: Release
    completed_at: datetime
    tracking_issue: str
    process_revision: str
    governance_level: str | None
    upstream: UpstreamIdentity
    assets: tuple[SourceAsset, ...]
    steps: tuple[RecordedStep, ...]

    def path(self, directory: Path) -> Path:
        return directory / f"{self.release.full()}.toml"


def refusals(evaluation: Evaluation) -> list[str]:
    """Rule R1: why this evaluation cannot be recorded. Empty when it can."""
    causes = []
    for row in evaluation.rows:
        if row.step == RECORD_STEP and row.project != END_TO_END:
            continue
        if not row.resolved():
            landing = ", proposed and not merged" if row.landing is Landing.PROPOSED else ""
            causes.append(f"unresolved step: {row.project} {row.step} is {row.verdict.value.replace('-', ' ')}{landing}")
    causes += [f"unaccounted occurrence: {o.project} {o.path}:{o.line}" for o in evaluation.strangers]
    causes += [f"changed source: {change.what}" for change in evaluation.source_changed]
    causes += [f"unread project: {cause}" for cause in evaluation.unread]
    if evaluation.upstream is None:
        causes.append("upstream has published no release with this tag")
    elif not evaluation.upstream.commit:
        causes.append("the commit the upstream tag points to is not recorded")
    causes += [f"asset without a digest: {asset.name}" for asset in evaluation.sources if not asset.sha256]
    if not evaluation.sources:
        causes.append("no consumed upstream asset is recorded")
    return causes


def _seconds(moment: datetime | None) -> datetime | None:
    return moment.astimezone(timezone.utc).replace(microsecond=0) if moment else None


def from_evaluation(evaluation: Evaluation, completed_at: datetime, tracking_issue: str) -> AdoptionRecord:
    causes = refusals(evaluation)
    if causes:
        raise RecordRefused(causes)
    assert evaluation.upstream is not None
    release = evaluation.release
    steps = []
    for row in evaluation.rows:
        if row.step == RECORD_STEP and row.project != END_TO_END:
            evidence = f"energyplus/adoptions/{release.full()}.toml"
            steps.append(RecordedStep(row.project, row.step, row.step_class, Verdict.PASSED, evidence))
        else:
            steps.append(RecordedStep(row.project, row.step, row.step_class, row.verdict, row.evidence, row.reason, row.accepted_by, _seconds(row.accepted_at)))
    upstream = evaluation.upstream
    identity = UpstreamIdentity(upstream.repository, upstream.tag, upstream.commit, _seconds(upstream.published_at) or upstream.published_at)
    completed = _seconds(completed_at) or completed_at
    return AdoptionRecord(release, completed, tracking_issue, evaluation.process_revision, evaluation.governance_level, identity, evaluation.sources, tuple(steps))


# ── TOML ───────────────────────────────────────────────────────────────────────────────────────────


def _text(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ") + '"'


def _moment(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def to_toml(record: AdoptionRecord) -> str:
    lines = [
        f"# The adoption record of EnergyPlus {record.release.full()}. Written once by tools/energyplus/record.py.",
        "# Never edited after merge. A correction is a new file beside it with a reason.",
        "",
        "[adoption]",
        f"release = {_text(record.release.full())}",
        f"completed_at = {_moment(record.completed_at)}",
        f"tracking_issue = {_text(record.tracking_issue)}",
        f"process_revision = {_text(record.process_revision)}",
    ]
    if record.governance_level:
        lines.append(f"governance_level = {_text(record.governance_level)}")
    lines += [
        "",
        "[upstream]",
        f"repository = {_text(record.upstream.repository)}",
        f"tag = {_text(record.upstream.tag)}",
        f"commit = {_text(record.upstream.commit)}",
        f"published_at = {_moment(record.upstream.published_at)}",
    ]
    for asset in record.assets:
        lines += ["", "  [[upstream.asset]]", f"  role = {_text(asset.role.value)}", f"  name = {_text(asset.name)}", f"  size = {asset.size}", f"  sha256 = {_text(asset.sha256 or '')}"]
    for step in record.steps:
        lines += ["", "[[step]]", f"project = {_text(step.project)}", f"id = {_text(step.id)}", f"class = {_text(step.step_class.value)}", f"verdict = {_text(step.verdict.value)}"]
        for key, value in (("evidence", step.evidence), ("reason", step.reason), ("accepted_by", step.accepted_by)):
            if value:
                lines.append(f"{key} = {_text(value)}")
        if step.accepted_at:
            lines.append(f"accepted_at = {_moment(step.accepted_at)}")
    return "\n".join(lines) + "\n"


def from_toml(text: str) -> AdoptionRecord:
    data = tomllib.loads(text)
    adoption, upstream = data["adoption"], data["upstream"]
    record = AdoptionRecord(
        release=Release.parse(adoption["release"]),
        completed_at=adoption["completed_at"],
        tracking_issue=adoption["tracking_issue"],
        process_revision=adoption["process_revision"],
        governance_level=adoption.get("governance_level"),
        upstream=UpstreamIdentity(upstream["repository"], upstream["tag"], upstream["commit"], upstream["published_at"]),
        assets=tuple(SourceAsset(AssetRole(a["role"]), a["name"], a["size"], a["sha256"] or None) for a in upstream.get("asset", ())),
        steps=tuple(
            RecordedStep(s["project"], s["id"], StepClass(s["class"]), Verdict(s["verdict"]), s.get("evidence"), s.get("reason"), s.get("accepted_by"), s.get("accepted_at"))
            for s in data.get("step", ())
        ),
    )
    problems = validate(record)
    if problems:
        raise RecordRefused(problems)
    return record


def validate(record: AdoptionRecord) -> list[str]:
    """Rules R1 to R3, on a record as it is read back."""
    problems = [f"step {s.project} {s.id} is {s.verdict.value}" for s in record.steps if s.verdict not in (Verdict.PASSED, Verdict.NOT_APPLICABLE)]
    problems += [f"step {s.project} {s.id} is not applicable without a reason" for s in record.steps if s.verdict is Verdict.NOT_APPLICABLE and not s.reason]
    if len(record.upstream.commit) != 40:
        problems.append("the upstream commit is not a full commit identifier")
    problems += [f"asset {asset.name} has no digest" for asset in record.assets if not asset.sha256 or len(asset.sha256) != 64]
    if not record.assets:
        problems.append("no consumed asset is recorded")
    if not record.process_revision:
        problems.append("the process revision is not recorded")
    return problems


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("evaluation", type=Path)
    parser.add_argument("--out", type=Path, required=True, help="the directory that holds the records")
    parser.add_argument("--issue", default=BASELINE_ISSUE, help="the URL of the tracking issue")
    arguments = parser.parse_args(argv)
    evaluation = decode(Evaluation, arguments.evaluation.read_text(encoding="utf-8"))
    try:
        record = from_evaluation(evaluation, datetime.now(timezone.utc), arguments.issue)
    except RecordRefused as refused:
        print(refused, file=sys.stderr)
        return 1
    target = record.path(arguments.out)
    if target.exists():
        print(f"{target} exists. A record is never rewritten; a correction is a new file beside it.", file=sys.stderr)
        return 1
    target.write_text(to_toml(record), encoding="utf-8")
    print(f"wrote {target}: {len(record.steps)} steps")
    return 0


if __name__ == "__main__":
    sys.exit(main())
