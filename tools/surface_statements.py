"""Downstream statements of unavailability: resolve them, and surface them when their entry closes.

A product that tells its users "this cannot be done" for a library reason is quoting the parity
ledger, whether or not it says so. When the capability closes, that true statement becomes a false
one, and until this file nothing connected the ledger entry to the interface that quotes it
(User Story 6, research R8).

THE MARKER. One comment beside the statement, in whatever comment syntax the file uses:

    idfkit:unavailable parity_id=write
    idfkit:unavailable parity_id=write own_reason="The editor saves object notation only, by design."

`parity_id` names the ledger entry the statement rests on (FR-033). `own_reason`, when present, says
the consumer keeps the statement for a reason of its own, and the statement stops citing the library
(FR-035). The format is the same in both languages, documented in governance/README.md.

THREE COMMANDS, one per moment.

    check      At every build. Every marker's parity_id resolves in parity.toml at the pinned tag,
               or the build fails, exactly as the `parity(id)` documentation macro fails a page.
    surface    At an adoption. Given the ledger at the old and the new governance tags, list every
               marker resting on an entry that moved to complete ON THE MARKER'S SIDE. Adoption is
               not complete while one is listed (FR-025, FR-034).
    list       For a reader: every marker, and the entry state it rests on.

A MARKER IS SURFACED ONLY BY ITS OWN SIDE. A capability can close in one language only. A statement in
the second language's editor rests on the `typescript` side of an entry and is surfaced by that side
moving, never by the first language's. The side is the marker's file: Python source rests on
`python`, anything else on `typescript`, and `side=` overrides that for a file that is neither.

A `never` ENTRY SURFACES NOTHING. There is nothing coming, so there is nothing to review (T092).

REVIEWING means one of two changes to the consumer: the statement and its marker are removed, or the
marker gains `own_reason`. Either makes it disappear from `surface`, and nothing else does.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MARKER = re.compile(
    r"idfkit:unavailable\s+parity_id=(?P<id>[a-z0-9][a-z0-9-]*)"
    r"(?:\s+side=(?P<side>python|typescript))?"
    r'(?:\s+own_reason="(?P<reason>[^"]+)")?'
)
_PYTHON_SUFFIXES = (".py", ".pyi")
_SKIP = {"node_modules", ".venv", "dist", "build", ".git"}


@dataclass(frozen=True, slots=True)
class Marker:
    path: str
    line: int
    parity_id: str
    side: str
    own_reason: str | None


def find_markers(root: Path) -> list[Marker]:
    listing = subprocess.run(["git", "-C", str(root), "ls-files"], capture_output=True, text=True, check=False)  # noqa: S603, S607
    paths = listing.stdout.split("\n") if listing.returncode == 0 else [p.relative_to(root).as_posix() for p in root.rglob("*")]
    markers: list[Marker] = []
    for path in paths:
        file = root / path
        if not path or set(Path(path).parts) & _SKIP or not file.is_file():
            continue
        try:
            text = file.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if "idfkit:unavailable" not in text:
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            for match in MARKER.finditer(line):
                side = match.group("side") or ("python" if path.endswith(_PYTHON_SUFFIXES) else "typescript")
                markers.append(Marker(path, number, match.group("id"), side, match.group("reason")))
    return markers


def _ledger(data: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {entry["id"]: entry for entry in data.get("capability", []) if "id" in entry}


def is_never(entry: Mapping[str, Any]) -> bool:
    return (
        entry.get("tier") == "never"
        or entry.get("absence_kind") == "never"
        or entry.get("difference_kind") == "never"
    )


def check(markers: Sequence[Marker], ledger: Mapping[str, Mapping[str, Any]]) -> list[str]:
    """Every marker resolves, or the build fails naming it."""
    return [
        f"{m.path}:{m.line}: parity_id {m.parity_id!r} is not an entry in parity.toml at the pinned governance tag."
        for m in markers
        if m.parity_id not in ledger
    ]


def surface(
    markers: Sequence[Marker], before: Mapping[str, Mapping[str, Any]], after: Mapping[str, Mapping[str, Any]]
) -> list[str]:
    """Every marker resting on an entry that closed on its side between two tags, and not yet reviewed."""
    listed: list[str] = []
    for marker in markers:
        if marker.own_reason:
            continue
        old, new = before.get(marker.parity_id), after.get(marker.parity_id)
        if old is None or new is None or is_never(old):
            continue
        if old.get(marker.side) != "complete" and new.get(marker.side) == "complete":
            listed.append(
                f"{marker.path}:{marker.line}: rests on {marker.parity_id!r}, which is now complete in {marker.side}. "
                "Remove the statement, or keep it with own_reason set so it stops citing the library (FR-035)."
            )
    return listed


def _read(path: Path) -> dict[str, Mapping[str, Any]]:
    with path.open("rb") as handle:
        return _ledger(tomllib.load(handle))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Resolve and surface downstream statements of unavailability.")
    parser.add_argument("command", choices=["check", "surface", "list"])
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="the consumer's checkout")
    parser.add_argument("--parity", type=Path, required=True, help="parity.toml at the consumer's pinned tag (after, for surface)")
    parser.add_argument("--parity-before", type=Path, help="parity.toml at the tag being moved away from (surface only)")
    args = parser.parse_args(argv)

    markers = find_markers(args.root)
    ledger = _read(args.parity)

    if args.command == "list":
        for marker in markers:
            entry = ledger.get(marker.parity_id, {})
            state = f"python={entry.get('python')} typescript={entry.get('typescript')}" if entry else "UNKNOWN"
            kept = f' own_reason="{marker.own_reason}"' if marker.own_reason else ""
            print(f"{marker.path}:{marker.line} {marker.parity_id} ({marker.side}) {state}{kept}")
        print(f"{len(markers)} statement marker(s).")
        return 0

    problems = check(markers, ledger)
    if args.command == "surface":
        if args.parity_before is None:
            parser.error("surface needs --parity-before")
        problems += surface(markers, _read(args.parity_before), ledger)

    print(f"statement markers: {len(markers)} found")
    for problem in problems:
        print(f"  {problem}")
    if problems:
        verb = "unresolved or unreviewed" if args.command == "surface" else "unresolved"
        print(f"FAILED: {len(problems)} {verb} statement(s).")
        return 1
    print("PASSED.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
