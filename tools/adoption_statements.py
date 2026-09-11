"""The statements one adoption surfaces: every marker resting on an entry the new level closed.

Called by every consumer's bump workflow between moving its level and opening its pull request
(FR-025, T072, T093). An adoption moves a consumer from one library version to another, and each
version declares the governance tag it was built against: `[tool.idfkit.governance] level` in
idfkit's pyproject.toml, `idfkit.governance` in idfkit-js's packages/core/package.json. The ledger at
those two tags is the before and after that `surface_statements.surface` compares.

Nothing here is declared twice. The two tags are read from the library at its own release tags, the
ledger is read from this repository at those tags, and the markers are read from the consumer.
"""

from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
import tomllib
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from surface_statements import _ledger, find_markers, surface  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
LIBRARY_REPOSITORY = {"python": "idfkit/idfkit", "javascript": "idfkit/idfkit-js"}
DECLARED_IN = {"python": "pyproject.toml", "javascript": "packages/core/package.json"}


def release_tags(version: str) -> list[str]:
    """The tag spellings a release of *version* may carry. PyPI and uv write `1.0.0rc4`; the tag is
    `v1.0.0-rc.4`. Both are tried, semver first, because that is what both libraries tag."""
    import re

    bare = version.removeprefix("v")
    semver = re.sub(r"(\d)(a|b|rc)(\d+)$", r"\1-\2.\3", bare)
    return list(dict.fromkeys([f"v{semver}", f"v{bare}"]))


def governance_level_at(library: str, version: str, fetch: Callable[[str, str, str], str | None]) -> str:
    """The governance tag the library at *version* declares, read from its own release tag."""
    for tag in release_tags(version):
        text = fetch(LIBRARY_REPOSITORY[library], DECLARED_IN[library], tag)
        if text is None:
            continue
        if library == "python":
            level = tomllib.loads(text).get("tool", {}).get("idfkit", {}).get("governance", {}).get("level")
        else:
            level = (json.loads(text).get("idfkit") or {}).get("governance")
        if isinstance(level, str) and level:
            return level
    raise LookupError(
        f"{LIBRARY_REPOSITORY[library]} at {release_tags(version)} declares no governance level, so the statements "
        "this adoption surfaces cannot be known. Stop rather than report none."
    )


def _gh_fetch(repository: str, path: str, ref: str) -> str | None:
    result = subprocess.run(  # noqa: S603
        ["gh", "api", f"repos/{repository}/contents/{path}?ref={ref}"],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    return base64.b64decode(json.loads(result.stdout)["content"]).decode("utf-8")


def ledger_at(conformance: Path, tag: str) -> dict[str, Mapping[str, Any]]:
    shown = subprocess.run(  # noqa: S603
        ["git", "-C", str(conformance), "show", f"{tag}:governance/parity.toml"],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    if shown.returncode != 0:
        raise LookupError(f"cannot read governance/parity.toml at {tag}: {shown.stderr.strip()}")
    return _ledger(tomllib.loads(shown.stdout))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="List the statements an adoption surfaces, and fail while any is unreviewed.")
    parser.add_argument("--library", choices=sorted(LIBRARY_REPOSITORY), required=True)
    parser.add_argument("--from", dest="old", required=True, help="the level the consumer is moving from")
    parser.add_argument("--to", dest="new", required=True, help="the level it is moving to")
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="the consumer's checkout")
    parser.add_argument("--conformance", type=Path, default=ROOT, help="an idfkit-conformance checkout with its tags")
    args = parser.parse_args(argv)

    try:
        before_tag = governance_level_at(args.library, args.old, _gh_fetch)
        after_tag = governance_level_at(args.library, args.new, _gh_fetch)
        before, after = ledger_at(args.conformance, before_tag), ledger_at(args.conformance, after_tag)
    except LookupError as error:
        print(f"CANNOT SURFACE: {error}", file=sys.stderr)
        return 2

    listed = surface(find_markers(args.root), before, after)
    print(f"statements surfaced by {args.library} {args.old} -> {args.new} ({before_tag} -> {after_tag}): {len(listed)}")
    for line in listed:
        print(f"  {line}")
    return 1 if listed else 0


if __name__ == "__main__":
    sys.exit(main())
