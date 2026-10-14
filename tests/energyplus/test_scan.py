"""The scan, against a fake organisation: stale, missing and stranger, and the three exit codes."""

from __future__ import annotations

import pytest
from energyplus.roster import Release, Roster
from energyplus.scan import FindingKind, glob_pattern, scan, spellings
from support import ROSTER, FakeSource, organisation

ROSTERED = Roster.from_text(ROSTER)


def _scan(change: dict[str, str | None] | None = None, roster: Roster = ROSTERED):
    repos = organisation()
    for path, text in (change or {}).items():
        files = repos["o/lib"]
        assert files is not None
        if text is None:
            del files[path]
        else:
            files[path] = text
    return scan(roster, FakeSource(repos))


def test_a_current_organisation_is_clean() -> None:
    report = _scan()
    assert report.findings == () and report.unread == ()
    assert (report.newest, report.count, report.exit_code()) == (Release(26, 1, 0), 2, 0)


def test_a_statement_naming_another_release_is_stale() -> None:
    report = _scan({"README.md": "Supports v8.9 through v25.2 (2 versions).\n"})
    (finding,) = report.findings
    assert (finding.kind, finding.path, finding.line) == (FindingKind.STALE, "README.md", 1)
    assert "'25.2'" in finding.detail and "'26.1'" in finding.detail
    assert report.exit_code() == 1


def test_a_count_is_checked_as_a_statement() -> None:
    (finding,) = _scan({"README.md": "Supports v8.9 through v26.1 (16 versions).\n"}).findings
    assert finding.kind is FindingKind.STALE and "'16'" in finding.detail and "'2'" in finding.detail


def test_a_count_word_is_checked_as_a_statement() -> None:
    text = ROSTER.replace("'\\((?P<release>\\d+) versions\\)'\n  form = \"count\"", "'(?P<release>[a-z]+) versions'\n  form = \"count-word\"")
    assert text != ROSTER
    roster = Roster.from_text(text)
    assert _scan({"README.md": "Supports v8.9 through v26.1, two versions.\n"}, roster).findings == ()
    (finding,) = _scan({"README.md": "Supports v8.9 through v26.1, sixteen versions.\n"}, roster).findings
    assert finding.kind is FindingKind.STALE and "'two'" in finding.detail


def test_a_wrong_match_count_is_missing() -> None:
    (finding,) = _scan({"README.md": "Supports many versions (2 versions).\n"}).findings
    assert finding.kind is FindingKind.MISSING and "matches 0 time(s), declared 1" in finding.detail


def test_a_declared_file_that_does_not_exist_is_missing() -> None:
    kinds = {finding.kind for finding in _scan({"README.md": None}).findings}
    assert kinds == {FindingKind.MISSING}


def test_an_undeclared_occurrence_is_a_stranger() -> None:
    (finding,) = _scan({"docs/guide.md": "line one\nThe latest release is 26.1.0.\n"}).findings
    assert (finding.kind, finding.project, finding.path, finding.line) == (FindingKind.STRANGER, "lib", "docs/guide.md", 2)
    assert finding.text == "The latest release is 26.1.0."


def test_an_occurrence_beside_a_statement_in_the_same_file_is_a_stranger() -> None:
    report = _scan({"README.md": "Supports v8.9 through v26.1 (2 versions).\nInstall 26.1.0 first.\n"})
    assert [(finding.kind, finding.line) for finding in report.findings] == [(FindingKind.STRANGER, 2)]


def test_pins_generated_files_and_ignores_are_accounted_for() -> None:
    text = ROSTER.replace(
        '  [[project.pin]]\n  glob = "tests/**"',
        '  [[project.generated]]\n  glob = "stubs.pyi"\n  by_step = "registry"\n\n  [[project.ignore]]\n  pattern = "mkdocs-material"\n  reason = "A package version."\n\n'
        '  [[project.pin]]\n  glob = "tests/**"',
        1,
    )
    roster = Roster.from_text(text)
    change = {"stubs.pyi": "# Generated for 26.1.0\n", "requirements.txt": "mkdocs-material==26.1.0\n", "tests/deep/x.py": "V26-1-0\n"}
    assert _scan(change, roster).findings == ()
    assert len(_scan(change).findings) == 2


def test_a_concrete_declaration_that_names_no_file_is_missing() -> None:
    roster = Roster.from_text(ROSTER.replace('glob = "tests/**"', 'glob = "tests/gone.py"'))
    kinds = [finding.kind for finding in scan(roster, FakeSource(organisation())).findings]
    assert FindingKind.MISSING in kinds and FindingKind.STRANGER in kinds


@pytest.mark.parametrize(
    "text",
    ["26.1.0", "26.1", "26-1-0", "26-1", "26_1", "v26.1.0", "V26-1-0", "(26, 1, 0)", "=v26.1.0", "/v26.1/", "types-v26-1", "IDD_26_1"],
)
def test_every_spelling_is_found(text: str) -> None:
    (finding,) = _scan({"notes.md": f"see {text} here\n"}).findings
    assert finding.kind is FindingKind.STRANGER


@pytest.mark.parametrize("text", ["0.26.1", "26.10", "126.1", "2026-10-13", "26.1.5", "1.26.1.0", "26-11", "x226_1"])
def test_what_is_not_the_release_is_not_found(text: str) -> None:
    assert not spellings(Release(26, 1, 0)).search(f"see {text} here")


def test_a_project_that_cannot_be_read_is_exit_two_and_never_zero() -> None:
    repos = organisation()
    repos["o/app"] = None
    report = scan(ROSTERED, FakeSource(repos))
    assert report.findings == () and [unread.project for unread in report.unread] == ["app"]
    assert report.exit_code() == 2


def test_a_finding_outranks_an_unread_project() -> None:
    repos = organisation()
    repos["o/app"] = None
    files = repos["o/lib"]
    assert files is not None
    files["notes.md"] = "26.1.0\n"
    assert scan(ROSTERED, FakeSource(repos)).exit_code() == 1


def test_globs() -> None:
    assert glob_pattern("**/*.lock").match("uv.lock") and glob_pattern("**/*.lock").match("a/b/uv.lock")
    assert glob_pattern("tests/**").match("tests/a/b.py") and not glob_pattern("tests/**").match("src/tests/a.py")
    assert glob_pattern("docs/*.md").match("docs/a.md") and not glob_pattern("docs/*.md").match("docs/a/b.md")
