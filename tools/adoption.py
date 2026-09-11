"""In what order a published level is adopted, and whether a proposed adoption may proceed.

Read by both libraries' `notify-downstream.yml`, which dispatch in the waves this computes, and by
every consumer's bump workflow, which asks it whether its providers have adopted first
(contracts/adoption.md).

WAVES. Wave 1 is every consumer of the library that released with no `depends_on` among that
library's consumers; wave n is every consumer whose providers all sit in earlier waves. The register's
schema check guarantees the edges are acyclic, so a wave order always exists. Waves are computed per
library and never across the two: the second language proceeds while the first is mid-adoption, and
the reverse, because lockstep between them is prohibited (FR-021, T071).

ORDER. A consumer that reaches the library through another consumer cannot adopt a level its provider
has not published a release on. `idfkit-plugin` delivers idfkit-mcp to an editor; until an idfkit-mcp
release pins the new idfkit, there is nothing for the plugin to pin, and a pull request that bumped
it anyway would ship a server on the old level while claiming the new one. So the proposal is
rejected, naming the provider (FR-020, T070).

ONE LEVEL ACROSS TWO DOORS. In the second language every governed package in a manifest moves to the
same version in one change, the shared name and the scoped packages alike, because they are one
release (FR-039, T107). A manifest left holding two releases is half adopted.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from consumers import DELIVERED_PACKAGES, GOVERNED_PACKAGES, Register, level_of, normalize  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

#: The workflow each library dispatches in a consumer. One name per library, so the dispatch needs
#: nothing from the register beyond the roster, and a consumer without it fails the dispatch loudly.
BUMP_WORKFLOW = {"python": "bump-idfkit.yml", "javascript": "bump-idfkit-js.yml"}


@dataclass(frozen=True, slots=True)
class Target:
    consumer: str
    repository: str
    workflow: str
    wave: int


def waves(register: Register, library: str) -> list[list[Target]]:
    members = {c.id: c for c in register.consumers if c.role != "teaches" and c.resolution(library)}
    placed: dict[str, int] = {}
    ordered: list[list[Target]] = []
    while len(placed) < len(members):
        wave = len(ordered) + 1
        ready = sorted(
            cid
            for cid, consumer in members.items()
            if cid not in placed and all(dep in placed for dep in consumer.depends_on if dep in members)
        )
        if not ready:
            raise ValueError(f"no wave order for {library}: depends_on has a cycle among {sorted(set(members) - set(placed))}")
        for cid in ready:
            placed[cid] = wave
        ordered.append([Target(cid, members[cid].repository, BUMP_WORKFLOW[library], wave) for cid in ready])
    return ordered


def check_order(
    register: Register,
    consumer_id: str,
    library: str,
    level: str,
    released_on: Callable[[str, str], str | None],
) -> list[str]:
    """Reasons *consumer_id* may not adopt *level* yet. Empty means it may.

    *released_on(package, level)* answers which published release of a delivered server pins the
    library at *level*, or None when there is none yet.
    """
    consumer = register.consumer(consumer_id)
    resolution = consumer.resolution(library)
    if resolution is None:
        return [f"{consumer_id} does not resolve {library}, so it has nothing to adopt."]
    rejections: list[str] = []
    for declaration in resolution.declared_at:
        package = declaration.package
        if package in DELIVERED_PACKAGES and declaration.via:
            if released_on(package, level) is None:
                rejections.append(
                    f"{consumer_id} cannot adopt {library} {level} yet: its provider {declaration.via} has published "
                    f"no {package} release on that level. Adopt it there first, release it, and this proposal "
                    "follows (FR-020)."
                )
    if resolution.means == "through-consumer":
        for provider in consumer.depends_on:
            if released_on(provider, level) is None:
                rejections.append(f"{consumer_id} cannot adopt {library} {level} before {provider} has (FR-020).")
    return rejections


def version_key(version: str) -> tuple[int, ...]:
    """Order releases numerically, a final release after every candidate of the same number."""
    text = normalize(version)
    release, _, candidate = text.partition("rc")
    parts = tuple(int(p) for p in re.findall(r"\d+", re.split(r"[a-z]", release, maxsplit=1)[0]))
    return (*parts, 0 if candidate else 1, int(candidate) if candidate.isdigit() else 0)


def pypi_released_on(fetch: Callable[[str], Mapping[str, Any]]) -> Callable[[str, str], str | None]:
    """A `released_on` reading a server's own published metadata from PyPI."""

    def released_on(package: str, level: str) -> str | None:
        document = fetch(f"https://pypi.org/pypi/{package}/json")
        # Newest first by version, never by string: "0.9.3" sorts after "0.10.0" as text, and the
        # newest release pinning the level is the one a delivery path should move to.
        for version in sorted((document.get("releases") or {}), key=version_key, reverse=True):
            meta = fetch(f"https://pypi.org/pypi/{package}/{version}/json")
            for requirement in (meta.get("info") or {}).get("requires_dist") or []:
                head = requirement.split(";", 1)[0].strip()
                if head.lower().startswith("idfkit") and not head.lower().startswith(("idfkit-", "idfkit_")):
                    pinned = level_of(head, "idfkit").level
                    if pinned and normalize(pinned) == normalize(level):
                        return version
        return None

    return released_on


def plan_javascript(manifest: Mapping[str, Any], version: str) -> dict[str, Any]:
    """Every governed package this manifest depends on, moved to *version* in one change (FR-039).

    Only `dependencies`. An optional peer on the shared name, like the lsp model server's, is a
    statement about what may be installed beside it, and moving it would publish a requirement.
    """
    updated = json.loads(json.dumps(manifest))
    for package in list((updated.get("dependencies") or {}).keys()):
        if package in GOVERNED_PACKAGES["javascript"]:
            updated["dependencies"][package] = version
    return updated


def half_adopted(manifest: Mapping[str, Any]) -> list[str]:
    """The governed packages whose versions disagree, when more than one release is present."""
    pins = {p: v for p, v in (manifest.get("dependencies") or {}).items() if p in GOVERNED_PACKAGES["javascript"]}
    if len({normalize(v) for v in pins.values()}) <= 1:
        return []
    return [f"{package}@{version}" for package, version in sorted(pins.items())]


def _fetch_json(url: str) -> Mapping[str, Any]:
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(url, timeout=30) as response:  # noqa: S310
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return {}
        raise


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Adoption order for a published level.")
    parser.add_argument("--register", type=Path, default=ROOT / "governance" / "consumers.toml")
    commands = parser.add_subparsers(dest="command", required=True)

    wave_cmd = commands.add_parser("waves", help="print the dispatch waves for one library as JSON")
    wave_cmd.add_argument("--library", choices=["python", "javascript"], required=True)

    order_cmd = commands.add_parser("check-order", help="fail unless this consumer's providers adopted first")
    order_cmd.add_argument("--consumer", required=True)
    order_cmd.add_argument("--library", choices=["python", "javascript"], required=True)
    order_cmd.add_argument("--level", required=True)

    plan_cmd = commands.add_parser("plan-js", help="move every governed package in a manifest to one version")
    plan_cmd.add_argument("--manifest", type=Path, required=True)
    plan_cmd.add_argument("--version", required=True)

    args = parser.parse_args(argv)

    if args.command == "plan-js":
        manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
        updated = plan_javascript(manifest, args.version)
        if half := half_adopted(updated):
            print(f"half adopted after planning: {half}", file=sys.stderr)
            return 1
        args.manifest.write_text(json.dumps(updated, indent=2) + "\n", encoding="utf-8")
        print(f"{args.manifest}: every governed package at {args.version}")
        return 0

    register = Register.load(args.register)
    if args.command == "waves":
        print(json.dumps([[t.__dict__ for t in wave] for wave in waves(register, args.library)]))
        return 0

    rejections = check_order(register, args.consumer, args.library, args.level, pypi_released_on(_fetch_json))
    for rejection in rejections:
        print(rejection, file=sys.stderr)
    if not rejections:
        print(f"{args.consumer} may adopt {args.library} {args.level}: every provider it resolves through is there.")
    return 1 if rejections else 0


if __name__ == "__main__":
    sys.exit(main())
