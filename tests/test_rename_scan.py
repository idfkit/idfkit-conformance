"""The rename scan selects exactly the names that survived with a different meaning (T047)."""

from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path

from rename_scan import at_risk, scan

ROOT = Path(__file__).resolve().parent.parent


def _naming() -> dict:
    with (ROOT / "governance" / "naming.toml").open("rb") as handle:
        return tomllib.load(handle)


def test_the_real_register_selects_the_two_writing_entries_and_nothing_else() -> None:
    register = _naming()
    selected = at_risk(register, "python")
    assert sorted(n.name for n in selected) == ["write_epjson", "write_idf"]
    assert {n.moved_to for n in selected} == {"save_idf", "save_epjson"}
    assert len(register["entry"]) - len(selected) == len(register["entry"]) - 2


def test_the_second_language_has_no_survivor_today() -> None:
    # Its renames all removed the old spelling, which a build reports loudly by itself.
    assert at_risk(_naming(), "javascript") == []


def test_a_disappeared_name_is_not_at_risk() -> None:
    register = {
        "entry": [
            {"concept": "new", "python": "new_name", "typescript": "", "rename_count": {"python": 1, "typescript": 0},
             "notes": "This was spelled `gone_name(x)` before."},
        ]
    }
    assert at_risk(register, "python") == []


def test_scan_reads_source_and_prose_and_judges_neither(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "tool.py").write_text("from idfkit import write_idf\nwrite_idf(doc, path)\n")
    (tmp_path / "CLAUDE.md").write_text('Save with `write_idf(doc, "output.idf")`.\n')
    (tmp_path / "notes.md").write_text("write_idf is not scanned here: not a prose path.\n")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    found = scan(tmp_path, "python", at_risk(_naming(), "python"), ["CLAUDE.md"])
    assert [(a.path, a.line, a.kind) for a in found] == [
        ("src/tool.py", 1, "source"),
        ("src/tool.py", 2, "source"),
        ("CLAUDE.md", 1, "prose"),
    ]
