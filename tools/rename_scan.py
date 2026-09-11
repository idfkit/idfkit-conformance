"""Find every appearance of a name that survived a rename with a different meaning (research R5).

FR-011 is about a call that still resolves. The registered writing rename does not remove a name, it
narrows one: `write_idf` keeps existing and loses its `(doc, filepath)` form, and writing to disk
moves to `save_idf`. An import check cannot see that, and a type checker sees it only in code. A
guidance file that teaches `write_idf(doc, "output.idf")` to every agent reading a repository keeps
parsing and stops being true, and no checker will ever open it.

WHICH NAMES. Not every rename, and not every name: a plain grep over all register entries reports
noise nobody reads. A name is AT RISK when its entry says in its notes what the operation "was
spelled" before, and that old spelling is still a registered name on the same side, for a different
concept. That is the whole definition of "survived with a different meaning", and it selects exactly
the two writing entries today. A spelling that disappeared is a missing import, which every build
already reports loudly.

WHAT IT REPORTS. File and line, for the consumer's source in the rehearsed language and for every
path in the register's `prose_paths`. It does not decide whether an appearance is wrong, because a
scan that judges will be tuned until it is quiet.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tomllib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

#: The register's side for each library, since naming.toml keys on the language's name.
SIDE = {"python": "python", "javascript": "typescript"}
SOURCE_SUFFIXES = {"python": (".py", ".pyi"), "javascript": (".ts", ".tsx", ".js", ".mjs", ".cjs", ".jsx")}
_OLD_SPELLING = re.compile(r"(?:was spelled|name was)\s+`(?P<old>[^`]+)`", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class AtRisk:
    """A name that still resolves after a rename and no longer does what it did."""

    name: str
    #: What the old spelling looked like in use, as the register's notes wrote it.
    was: str
    #: The concept the old behaviour moved to, and its new spelling.
    moved_to_concept: str
    moved_to: str
    #: The concept the old spelling names now.
    now_means: str


@dataclass(frozen=True, slots=True)
class Appearance:
    path: str
    line: int
    name: str
    text: str
    #: `source` or `prose`, so a reader can see which instrument this finding stands in for.
    kind: str


def at_risk(register: Mapping[str, Any], library: str) -> list[AtRisk]:
    side = SIDE[library]
    entries = register.get("entry", [])
    names = {e.get(side): e.get("concept") for e in entries if e.get(side)}
    selected: list[AtRisk] = []
    for entry in entries:
        if not (entry.get("rename_count") or {}).get(side):
            continue
        for match in _OLD_SPELLING.finditer(entry.get("notes") or ""):
            spelling = match.group("old")
            old = re.split(r"[(\s]", spelling, maxsplit=1)[0]
            concept = names.get(old)
            if concept and concept != entry.get("concept"):
                selected.append(AtRisk(old, spelling, entry["concept"], entry[side], concept))
    return selected


def _tracked(root: Path) -> list[str]:
    listing = subprocess.run(["git", "-C", str(root), "ls-files"], capture_output=True, text=True, check=False)  # noqa: S603, S607
    if listing.returncode == 0:
        return listing.stdout.split("\n")
    return [p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()]


def scan(root: Path, library: str, names: Sequence[AtRisk], prose_paths: Iterable[str] = ()) -> list[Appearance]:
    if not names:
        return []
    pattern = re.compile(r"\b(" + "|".join(re.escape(n.name) for n in names) + r")\b")
    prose = set(prose_paths)
    targets = [(p, "source") for p in _tracked(root) if p.endswith(SOURCE_SUFFIXES[library]) and p not in prose]
    targets += [(p, "prose") for p in sorted(prose)]
    found: list[Appearance] = []
    for path, kind in targets:
        file = root / path
        if not file.is_file():
            continue
        for number, line in enumerate(file.read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
            for match in pattern.finditer(line):
                found.append(Appearance(path, number, match.group(1), line.strip(), kind))
    return found


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Report every appearance of a name that survived a rename.")
    parser.add_argument("--naming", type=Path, required=True, help="naming.toml, checked out at the pinned tag")
    parser.add_argument("--library", choices=sorted(SIDE), required=True)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--prose", action="append", default=[], help="a prose path from the register; repeatable")
    parser.add_argument("--json", type=Path, help="write the appearances here as JSON")
    args = parser.parse_args(argv)

    with args.naming.open("rb") as handle:
        names = at_risk(tomllib.load(handle), args.library)
    found = scan(args.root, args.library, names, args.prose)

    print(f"rename scan ({args.library}): {len(names)} name(s) survived a rename with a different meaning")
    for name in names:
        print(f"  {name.name}: was `{name.was}`; that job is now {name.moved_to}, and {name.name} now means {name.now_means!r}")
    for appearance in found:
        print(f"  {appearance.path}:{appearance.line} [{appearance.kind}] {appearance.text}")
    print(f"{len(found)} appearance(s). Each is a place to look, not a verdict.")
    if args.json:
        args.json.write_text(json.dumps([asdict(a) for a in found], indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
