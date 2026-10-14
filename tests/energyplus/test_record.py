"""Rules R1 to R3 of contracts/adoption-record.md: what a record refuses, and that it reads back equal."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from energyplus.evaluate import Evaluation, Occurrence, SourceChange, Verdict, evaluate
from energyplus.record import AdoptionRecord, RecordRefused, from_evaluation, from_toml, main, to_toml, validate
from energyplus.roster import Roster
from energyplus.serial import encode
from support import NEXT, NOW, ROSTER, UPSTREAM, FakeHub, FakeSource, adopted, organisation, packages

ISSUE = "https://github.com/idfkit/idfkit-conformance/issues/99"


def _evaluation(complete: bool = True) -> Evaluation:
    repos = adopted() if complete else organisation()
    return evaluate(
        Roster.from_text(ROSTER), NEXT, FakeSource(repos), FakeHub(), packages(released=complete), UPSTREAM,
        now=NOW, process_revision="a1b2c3d4" * 5, governance_level="governance-2026.23",
    )  # fmt: skip


def _record() -> AdoptionRecord:
    return from_evaluation(_evaluation(), NOW, ISSUE)


def test_a_complete_evaluation_is_recorded_with_every_step() -> None:
    record = _record()
    assert len(record.steps) == 13 and {step.verdict for step in record.steps} == {Verdict.PASSED}
    assert (record.release, record.tracking_issue, record.completed_at) == (NEXT, ISSUE, NOW)


def test_r1_an_unresolved_step_is_refused_and_named() -> None:
    with pytest.raises(RecordRefused) as refused:
        from_evaluation(_evaluation(complete=False), NOW, ISSUE)
    assert "unresolved step: lib registry is not started" in refused.value.causes
    assert len([cause for cause in refused.value.causes if cause.startswith("unresolved step")]) == 13


def test_r1_a_stranger_is_refused_and_named() -> None:
    strange = replace(_evaluation(), strangers=(Occurrence("lib", "notes.md", 4, "26.1.0"),))
    with pytest.raises(RecordRefused) as refused:
        from_evaluation(strange, NOW, ISSUE)
    assert refused.value.causes == ("unaccounted occurrence: lib notes.md:4",)


def test_r1_a_changed_source_is_refused_and_named() -> None:
    changed = replace(_evaluation(), source_changed=(SourceChange("the commit the tag points to", "a", "b"),))
    with pytest.raises(RecordRefused) as refused:
        from_evaluation(changed, NOW, ISSUE)
    assert refused.value.causes == ("changed source: the commit the tag points to",)


def test_r2_every_consumed_asset_and_the_tag_commit_are_recorded() -> None:
    record = _record()
    assert record.upstream.commit == "4bd7a1f26fb749a83257855aba6760e26c8a3c8f" and record.upstream.tag == "v26.2.0"
    assert len(record.assets) == 3 and all(asset.sha256 and len(asset.sha256) == 64 for asset in record.assets)
    with pytest.raises(RecordRefused, match="no consumed upstream asset"):
        from_evaluation(replace(_evaluation(), sources=()), NOW, ISSUE)


def test_r3_the_process_revision_and_the_governance_level_are_recorded() -> None:
    text = to_toml(_record())
    assert f'process_revision = "{"a1b2c3d4" * 5}"' in text and 'governance_level = "governance-2026.23"' in text
    assert validate(replace(_record(), process_revision="")) == ["the process revision is not recorded"]


def test_a_written_record_reads_back_equal() -> None:
    record = _record()
    assert from_toml(to_toml(record)) == record


def test_a_record_that_was_edited_to_hold_an_unresolved_step_does_not_read() -> None:
    text = to_toml(_record()).replace('verdict = "passed"', 'verdict = "failed"', 1)
    with pytest.raises(RecordRefused, match="is failed"):
        from_toml(text)


def test_the_command_writes_once_and_refuses_an_incomplete_evaluation(tmp_path: Path) -> None:
    complete, partial = tmp_path / "complete.json", tmp_path / "partial.json"
    complete.write_text(encode(_evaluation()))
    partial.write_text(encode(_evaluation(complete=False)))
    assert main([str(partial), "--out", str(tmp_path)]) == 1 and not (tmp_path / "26.2.0.toml").exists()
    assert main([str(complete), "--out", str(tmp_path), "--issue", ISSUE]) == 0
    assert from_toml((tmp_path / "26.2.0.toml").read_text()).tracking_issue == ISSUE
    assert main([str(complete), "--out", str(tmp_path)]) == 1
