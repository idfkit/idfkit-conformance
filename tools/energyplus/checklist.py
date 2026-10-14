"""Render an evaluation as the adoption checklist (contracts/adoption-record.md section 1).

The checklist is the body of the tracking issue. It is regenerated in full from one `Evaluation`
and is never patched or edited by hand. Rules C1 to C9 of the contract are properties of this
rendering and are tested as such.

    python -m tools.energyplus.checklist evaluation.json
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .evaluate import Evaluation, Landing, ProjectState, Recorded, SourceAsset, StepResult, Verdict
from .roster import END_TO_END, AssetRole, Release, StepClass
from .serial import decode

STALE_AFTER = timedelta(days=3)
MARKER = "<!-- energyplus-adoption: {release} -->"
_MARKER = re.compile(r"<!-- energyplus-adoption: (\d+\.\d+\.\d+) -->")
_SOURCE_ROW = re.compile(r"^\| (?P<role>[a-z0-9_-]+) \| `(?P<name>[^`]+)` \| (?P<size>\d+) \| `(?P<sha>[0-9a-f]{64})` \|$", re.MULTILINE)
_SOURCE_TAG = re.compile(r"^Tag `(?P<tag>[^`]+)` at commit `(?P<commit>[0-9a-f]{40})`", re.MULTILINE)
HEADER = "| Project | Step | Class | Verdict | Landing | Evidence | Accepted |\n| --- | --- | --- | --- | --- | --- | --- |"
MAX_LISTED = 25


def marker(release: Release) -> str:
    return MARKER.format(release=release.full())


def marked_release(body: str) -> Release | None:
    """The release an issue body tracks, from the marker on its first line."""
    found = _MARKER.match(body.lstrip())
    return Release.parse(found.group(1)) if found else None


def recorded_source(body: str) -> Recorded | None:
    """The source identity a previously rendered checklist states (FR-019)."""
    tag = _SOURCE_TAG.search(body)
    assets = tuple(SourceAsset(AssetRole(m["role"]), m["name"], int(m["size"]), m["sha"]) for m in _SOURCE_ROW.finditer(body))
    return Recorded(tag["commit"], assets) if tag and assets else None


def _cell(text: str | None) -> str:
    return (text or "").replace("|", "\\|").replace("\n", " ")


def _label(url: str) -> str:
    if found := re.search(r"/pull/(\d+)", url):
        return f"#{found.group(1)}"
    if found := re.search(r"/blob/[^/]+/(.+?)(#L\d+)?$", url):
        return found.group(1).rsplit("/", 1)[-1] + (found.group(2) or "")
    if found := re.search(r"/(actions/runs/\d+|releases/tag/[^/]+|project/[^/]+/[^/]+|package/.+)$", url):
        return found.group(1).split("/", 1)[-1] if "runs" not in found.group(1) else "run"
    return "evidence"


def _row(row: StepResult, state: ProjectState | None) -> str:
    title = row.title + (f" **{state.merge_effect}**" if state and state.merge_effect else "")
    evidence = f"[{_label(row.evidence)}]({row.evidence})" if row.evidence and row.verdict is not Verdict.NOT_STARTED else ""
    if row.verdict in (Verdict.NOT_STARTED, Verdict.FAILED) and row.reason:
        evidence = f"{evidence} {row.reason}".strip()
    accepted = f"@{row.accepted_by}, {row.accepted_at:%Y-%m-%d}" if row.accepted_by and row.accepted_at else (f"@{row.accepted_by}" if row.accepted_by else "")
    landing = "" if row.landing is Landing.NONE else row.landing.value
    cells = (row.project, title, row.step_class.value, row.verdict.value.replace("-", " "), landing, evidence, accepted)
    return "| " + " | ".join(_cell(cell) for cell in cells) + " |"


def _inspections(rows: Sequence[StepResult]) -> list[str]:
    """Rule C4: what a person inspects, after the table that holds the row."""
    lines = []
    for row in rows:
        if row.step_class is not StepClass.AUTOMATED and row.verdict is not Verdict.PASSED and row.verdict is not Verdict.NOT_APPLICABLE and row.inspect:
            lines += [f"> **{row.project}, {row.title}.** Inspect: {row.inspect}", ""]
    return lines


def _sections(evaluation: Evaluation) -> list[tuple[str, list[ProjectState]]]:
    """Rule C9: projects grouped by what opened them, opened groups first, in roster order."""
    groups: dict[tuple[bool, str], list[ProjectState]] = {}
    for state in evaluation.projects:
        if state.lag is None:
            groups.setdefault((not state.opened, state.waits_on), []).append(state)
    ordered = sorted(groups.items(), key=lambda item: item[0][0])
    return [(f"{'Waiting' if waiting else 'Opened'}: {what}", states) for (waiting, what), states in ordered]


def render(evaluation: Evaluation, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    release = evaluation.release.full()
    states = {state.project: state for state in evaluation.projects}
    lines = [marker(evaluation.release), f"# Adopt EnergyPlus {release}", ""]
    action = evaluation.next_action
    if action is None:
        owed = "none is owed." if evaluation.complete() else "none is owed by a person. The run advances at its next evaluation."
        lines.append(f"**Next action**: {owed}")
    else:
        link = f" [{_label(action.url)}]({action.url})" if action.url else ""
        lines.append(f"**Next action**: {action.text}{link} ({action.who})")
    lines += ["", "| Passed | Failed | Awaiting person | Not applicable | Not started |", "| --- | --- | --- | --- | --- |"]
    lines.append("| " + " | ".join(str(evaluation.count(verdict)) for verdict in Verdict) + " |")
    stale = "STALE: " if now - evaluation.evaluated_at > STALE_AFTER else ""
    level = f"roster at `{evaluation.governance_level}`" if evaluation.governance_level else "roster at the working tree"
    lines += ["", f"{stale}Evaluated {evaluation.evaluated_at:%Y-%m-%d %H:%M} UTC at idfkit-conformance `{evaluation.process_revision[:7]}`, {level}."]
    if evaluation.upstream is None:
        lines.append("Upstream has published no release with this tag.")
    else:
        changed = "**SOURCE CHANGED since first evaluation.**" if evaluation.source_changed else "Source unchanged since first evaluation."
        lines.append(f"Upstream published {evaluation.upstream.published_at:%Y-%m-%d}. {changed}")
    for change in evaluation.source_changed:
        lines.append(f"- {change.what}: recorded `{change.recorded}`, now `{change.live}`")
    extra = [row for row in evaluation.rows if row.project == END_TO_END and row.step == "source"]
    for title, members in _sections(evaluation):
        rows = [row for state in members for row in evaluation.of(state.project)]
        lines += ["", f"## {title}", "", HEADER, *(_row(row, states[row.project]) for row in rows), ""]
        lines += _inspections(rows)
        for state in members:
            if state.after_merge and not state.after_merge.startswith("None"):
                lines.append(f"After the {state.project} merge, a maintainer: {state.after_merge}")
    lagging = [state for state in evaluation.projects if state.lag is not None]
    if lagging:
        lines += ["", "## Not applicable", "", "| Project | Step | Class | Reason |", "| --- | --- | --- | --- |"]
        for state in lagging:
            for row in evaluation.of(state.project):
                title = row.title + (f" **{state.merge_effect}**" if state.merge_effect else "")
                lines.append(f"| {_cell(row.project)} | {_cell(title)} | {row.step_class.value} | {_cell(row.reason)} |")
    lines += ["", "## End-to-end verifications", "", HEADER]
    lines += [_row(row, None) for row in evaluation.of(END_TO_END) if row not in extra] + [_row(row, None) for row in extra]
    if evaluation.strangers or evaluation.unread:
        lines += ["", "## Occurrences the roster does not account for", ""]
        lines += [f"- {o.project} `{o.path}:{o.line}` {_cell(o.text)[:120]}" for o in evaluation.strangers[:MAX_LISTED]]
        if len(evaluation.strangers) > MAX_LISTED:
            lines.append(f"- and {len(evaluation.strangers) - MAX_LISTED} more. Run `python -m tools.energyplus.scan` for the full list.")
        lines += [f"- could not be read: {cause}" for cause in evaluation.unread]
    changes = evaluation.changes
    if changes is not None:
        lines += ["", f"## Change summary, {changes.previous.full()} to {changes.release.full()}", ""]
        lines.append(
            f"{len(changes.types_added)} object types added, {len(changes.types_removed)} removed; "
            f"{len(changes.fields_added)} fields added, {len(changes.fields_removed)} removed in object types present in both; "
            f"{len(changes.variables_renamed)} output variables renamed. A rename of a type or field appears as one removal and one addition."
        )
        for heading, names in (("Object types added", changes.types_added), ("Object types removed", changes.types_removed)):
            if names:
                lines += ["", f"**{heading}**: " + ", ".join(f"`{name}`" for name in names)]
        for heading, fields in (("Fields added", changes.fields_added), ("Fields removed", changes.fields_removed)):
            if fields:
                shown = ", ".join(f"`{change.object_type}.{change.field}`" for change in fields[:MAX_LISTED])
                more = f", and {len(fields) - MAX_LISTED} more" if len(fields) > MAX_LISTED else ""
                lines += ["", f"**{heading}**: {shown}{more}"]
        if changes.variables_renamed:
            shown = "; ".join(f"`{rename.previous}` to `{rename.current}`" for rename in changes.variables_renamed[:MAX_LISTED])
            lines += ["", f"**Output variables renamed**: {shown}"]
        lines += ["", *changes.notes] if changes.notes else []
    lines += ["", "## Source", ""]
    if evaluation.upstream is not None:
        lines += [f"Tag `{evaluation.upstream.tag}` at commit `{evaluation.upstream.commit}` of {evaluation.upstream.repository}.", ""]
    lines += ["| Role | Asset | Size | sha256 |", "| --- | --- | --- | --- |"]
    lines += [f"| {asset.role.value} | `{asset.name}` | {asset.size} | `{asset.sha256}` |" for asset in evaluation.sources]
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("evaluation", type=Path)
    arguments = parser.parse_args(argv)
    sys.stdout.write(render(decode(Evaluation, arguments.evaluation.read_text(encoding="utf-8"))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
