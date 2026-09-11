"""Statement markers resolve, surface on their own side's closure, and fall silent for `never` (T088, T090 to T092)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from surface_statements import check, find_markers, surface

PARTIAL = {"write": {"id": "write", "tier": "tier-1", "python": "complete", "typescript": "partial"}}
CLOSED = {"write": {"id": "write", "tier": "tier-1", "python": "complete", "typescript": "complete"}}


def _repo(tmp_path: Path, files: dict[str, str]) -> Path:
    for name, text in files.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(text)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    return tmp_path


def test_markers_are_found_with_their_side(tmp_path: Path) -> None:
    root = _repo(
        tmp_path,
        {
            "src/Export.tsx": "// idfkit:unavailable parity_id=write\n",
            "tool/save.py": '# idfkit:unavailable parity_id=write own_reason="object notation only"\n',
        },
    )
    markers = sorted(find_markers(root), key=lambda m: m.path)
    assert [(m.path, m.side, m.own_reason) for m in markers] == [
        ("src/Export.tsx", "typescript", None),
        ("tool/save.py", "python", "object notation only"),
    ]


def test_an_unknown_parity_id_fails_the_build(tmp_path: Path) -> None:
    markers = find_markers(_repo(tmp_path, {"a.ts": "// idfkit:unavailable parity_id=wirte\n"}))
    assert check(markers, PARTIAL)


def test_closing_the_entry_surfaces_the_statement(tmp_path: Path) -> None:
    markers = find_markers(_repo(tmp_path, {"a.ts": "// idfkit:unavailable parity_id=write\n"}))
    assert len(surface(markers, PARTIAL, CLOSED)) == 1


def test_a_closure_in_the_other_language_surfaces_nothing(tmp_path: Path) -> None:
    before = {"write": {"id": "write", "python": "partial", "typescript": "partial"}}
    after = {"write": {"id": "write", "python": "complete", "typescript": "partial"}}
    markers = find_markers(_repo(tmp_path, {"a.ts": "// idfkit:unavailable parity_id=write\n"}))
    assert surface(markers, before, after) == []


def test_both_review_outcomes_clear_it(tmp_path: Path) -> None:
    removed = find_markers(_repo(tmp_path / "removed", {"a.ts": "// the statement is gone\n"}))
    kept = find_markers(_repo(tmp_path / "kept", {"a.ts": '// idfkit:unavailable parity_id=write own_reason="we choose not to"\n'}))
    assert surface(removed, PARTIAL, CLOSED) == []
    assert surface(kept, PARTIAL, CLOSED) == []


def test_a_never_entry_surfaces_nothing(tmp_path: Path) -> None:
    never = {"sim": {"id": "sim", "tier": "never", "python": "absent", "typescript": "complete", "absence_kind": "never"}}
    moved = {"sim": {"id": "sim", "tier": "never", "python": "complete", "typescript": "complete"}}
    markers = find_markers(_repo(tmp_path, {"a.py": "# idfkit:unavailable parity_id=sim\n"}))
    assert surface(markers, never, moved) == []
    assert check(markers, never) == []
