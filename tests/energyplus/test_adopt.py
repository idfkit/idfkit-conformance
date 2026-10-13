"""The adoption inside one project, over a temporary git repository."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from energyplus.adopt import Ran, Result, adopt, body, edited_comment
from energyplus.evaluate import Recorded, consumed
from energyplus.roster import Roster
from support import NEXT, RED, ROSTER, UPSTREAM, VERSIONS_NEXT, FakeHub, FakeSource, organisation, packages

ROSTERED = Roster.from_text(ROSTER)
LIB = ROSTERED.project("lib")


def _git(tree: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(tree), "-c", "user.name=github-actions[bot]", "-c", "user.email=bot@example.com", *args], capture_output=True, text=True, check=True).stdout


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    files = organisation()["o/lib"]
    assert files is not None
    for name, text in files.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(text)
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "initial")
    _git(tmp_path, "update-ref", "refs/remotes/origin/HEAD", "HEAD")
    return tmp_path


@dataclass
class FakeRunner:
    """Records every command. `echo 26.2.0` stands for the step that adds the registry row."""

    fail: str = ""
    touch: str = ""
    commands: list[str] = field(default_factory=list)

    def run(self, command: str, cwd: Path) -> Ran:
        self.commands.append(command)
        if command == self.fail:
            return Ran(3, "the command failed")
        if command.startswith("echo"):
            (cwd / "versions.py").write_text(VERSIONS_NEXT)
            (cwd / "ci.yml").write_text((cwd / "ci.yml").read_text() + 'version: "26.2.0"\n')
            if self.touch:
                (cwd / self.touch).write_text("changed\n")
        return Ran(0, "")


def _adopt(tree: Path, runner: FakeRunner, roster: Roster = ROSTERED, hub: FakeHub | None = None, **more):
    return adopt(roster, roster.project("lib"), NEXT, tree, FakeSource(organisation()), hub or FakeHub(), packages(released=False), UPSTREAM, runner, **more)


def test_steps_run_in_roster_order_and_only_the_declared_commands_are_executed(tree: Path) -> None:
    runner = FakeRunner()
    result = _adopt(tree, runner)
    assert result.result is Result.PROPOSED
    assert runner.commands == ["echo 26.2.0", "make check"]
    assert result.performed == ("registry",) and result.files_changed == ("README.md", "ci.yml", "versions.py")


def test_statements_are_rewritten_in_every_form(tree: Path) -> None:
    result = _adopt(tree, FakeRunner())
    assert (tree / "README.md").read_text() == "Supports v8.9 through v26.2 (3 versions).\n"
    assert result.statements_rewritten == 1


def test_a_pattern_that_matches_the_wrong_number_of_times_fails_and_leaves_the_tree_untouched(tree: Path) -> None:
    (tree / "README.md").write_text("Supports v8.9 through v26.1 and through v26.1 (2 versions).\n")
    _git(tree, "commit", "-q", "-am", "two matches")
    runner = FakeRunner()
    result = _adopt(tree, runner)
    assert result.result is Result.STATEMENT_MISMATCH and "matches 2 time(s), declared 1" in result.detail
    assert runner.commands == [] and _git(tree, "status", "--porcelain") == ""


def test_a_change_under_a_pin_fails_the_run(tree: Path) -> None:
    result = _adopt(tree, FakeRunner(touch="tests/test_a.py"))
    assert result.result is Result.PIN_TOUCHED and "tests/test_a.py (pin tests/**)" in result.detail
    assert result.result.failed()


def test_when_everything_holds_nothing_is_written(tree: Path) -> None:
    _adopt(tree, FakeRunner())
    _git(tree, "commit", "-q", "-am", "adopted")
    runner = FakeRunner()
    again = _adopt(tree, runner)
    assert again.result is Result.NOTHING_TO_DO and runner.commands == [] and _git(tree, "status", "--porcelain") == ""


def test_a_step_with_no_command_is_verified_and_not_performed(tree: Path) -> None:
    runner = FakeRunner()
    result = _adopt(tree, runner)
    assert "sweep-row" not in result.performed and "statements" not in result.performed
    app = adopt(ROSTERED, ROSTERED.project("app"), NEXT, tree, FakeSource(organisation()), FakeHub(), packages(released=True), UPSTREAM, FakeRunner())
    assert app.result is Result.WAITING and app.waiting == ("level",) and not app.result.failed()


def test_a_failing_default_branch_stops_the_run_as_pre_existing(tree: Path) -> None:
    runner = FakeRunner()
    result = _adopt(tree, runner, hub=FakeHub(states={("o/lib", None): RED}))
    assert result.result is Result.PRE_EXISTING and "lint" in result.detail and runner.commands == []


def test_a_failed_step_stops_the_run_with_its_log(tree: Path) -> None:
    result = _adopt(tree, FakeRunner(fail="echo 26.2.0"))
    assert result.result is Result.STEP_FAILED and result.log == "the command failed"


def test_a_failed_gate_is_reported(tree: Path) -> None:
    assert _adopt(tree, FakeRunner(fail="make check")).result is Result.GATE_FAILED


def test_a_changed_upstream_source_stops_the_run(tree: Path) -> None:
    runner = FakeRunner()
    result = _adopt(tree, runner, recorded=Recorded("0" * 40, consumed(UPSTREAM)))
    assert result.result is Result.SOURCE_CHANGED and runner.commands == []


def test_a_project_with_a_lag_reports_the_lag_and_does_nothing(tree: Path) -> None:
    lagging = Roster.from_text(ROSTER.replace('gate = "make check"', 'gate = "make check"\n\n  [project.lag]\n  kind = "deliberate"\n  reason = "It adopts with its platform."'))
    runner = FakeRunner()
    result = _adopt(tree, runner, roster=lagging)
    assert result.result is Result.LAG and runner.commands == [] and "It adopts with its platform." in result.detail


def test_fr018_a_branch_a_person_committed_to_is_not_pushed_to(tree: Path) -> None:
    _git(tree, "switch", "-q", "-c", "energyplus/26.2.0")
    (tree / "ci.yml").write_text('version: "25.2.0"\nversion: "26.1.0"\nversion: "26.2.0"  # corrected by hand\n')
    subprocess.run(["git", "-C", str(tree), "-c", "user.name=A Person", "-c", "user.email=p@example.com", "commit", "-q", "-am", "by hand"], check=True)
    _git(tree, "update-ref", "refs/remotes/origin/energyplus/26.2.0", "HEAD")
    _git(tree, "switch", "-q", "main")
    result = _adopt(tree, FakeRunner(), remote_branch="origin/energyplus/26.2.0")
    assert result.result is Result.BRANCH_EDITED and "A Person" in result.detail and not result.result.failed()
    assert "ci.yml" in result.differing and "versions.py" in result.differing
    comment = edited_comment(NEXT, result)
    assert "- `ci.yml`" in comment and "left as it is" in comment


def test_a_branch_holding_only_the_workflows_commits_is_regenerated(tree: Path) -> None:
    _git(tree, "switch", "-q", "-c", "energyplus/26.2.0")
    (tree / "notes.txt").write_text("generated\n")
    _git(tree, "add", "-A")
    _git(tree, "commit", "-q", "-m", "generated")
    _git(tree, "update-ref", "refs/remotes/origin/energyplus/26.2.0", "HEAD")
    _git(tree, "switch", "-q", "main")
    assert _adopt(tree, FakeRunner(), remote_branch="origin/energyplus/26.2.0").result is Result.PROPOSED


def test_the_pull_request_body_states_each_step_and_what_to_inspect(tree: Path) -> None:
    text = body(LIB, NEXT, _adopt(tree, FakeRunner()), "https://github.com/o/host/issues/1")
    assert "| Add the release to the registry | automated | performed by the run |" in text
    assert "| Add the sweep row | reviewed | to be accepted by a person |" in text
    assert "- **Add the sweep row** (reviewed). The findings count in the new row." in text
    assert "never merges, publishes, releases or deploys" in text and "https://github.com/o/host/issues/1" in text
