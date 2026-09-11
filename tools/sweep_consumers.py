"""Read every consumer's level from where the register says it lives, and find who is missing.

The second of the two checks research R3 calls for. Each consumer's self-check is synchronous and
blocks a registered consumer that drifts, and it cannot see a repository nobody registered, because
an unregistered repository runs no check. This sweep sees exactly that, on a schedule, and it is the
only thing that can (FR-008). It also answers SC-001's question in one file: which level of which
library is every project on, and is anyone off the current level without saying why.

WHAT IT PRODUCES. `consumer-report.json`, beside `sweep-report.json` and borrowing its shape. Never
hand-edited. The fields are the ones in data-model.md, "Consumer report":

    governance_level  the register the roster was read from. A report that does not say this
                      cannot be compared with another.
    current           what each library currently publishes, read from the registry
    observed          per consumer per library per declaration: the level found and where
    entry_points      the shared-name to scoped mapping, read from the published facade (FR-038)
    out_of_scope      what the constitution leaves ungoverned, visible and judged by nothing
    unexplained       consumers off the current level with no lag, which SC-003 says must be 0
    strangers         repositories resolving a library that the roster does not list
    unreachable       repositories the sweep could not read

`UNREACHABLE IS NOT STRANGERS`, and the two are never merged. A permissions failure that read as
"no strangers" would be the shape of the bug that was in notify-downstream.yml, where a dispatch that
failed and one that landed printed the same line. Exit codes keep them apart too: 0 clean, 1 at
least one stranger, 2 nothing is a stranger but something could not be checked.

The level is never judged against the consumer's own taste. A consumer is allowed to be behind; it
is not allowed to be behind silently, and it is not allowed to be absent.
"""

from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
import tomllib
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

sys.path.insert(0, str(Path(__file__).resolve().parent))

from consumers import (  # noqa: E402
    _MANIFEST_NAMES,
    DELIVERED_PACKAGES,
    GOVERNED_PACKAGES,
    OUT_OF_SCOPE_PACKAGES,
    SKIP_DIRECTORIES,
    Consumer,
    Declaration,
    LocatorError,
    Register,
    detect_text,
    level_of,
    normalize,
    resolve,
)

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_VERSION = 1


# ── Where the sweep reads from ─────────────────────────────────────────────────────────────────────


class Unreachable(Exception):
    """A repository or file the sweep was not permitted, or not able, to read."""


class Source(Protocol):
    """Where repository contents come from: GitHub in the scheduled run, checkouts in a local one."""

    def read(self, repository: str, path: str) -> str: ...

    def files(self, repository: str) -> list[str]: ...

    def repositories(self) -> list[str]: ...


class GitHubSource:
    """The organisation's default branches, through `gh api`."""

    def __init__(self, org: str) -> None:
        self.org = org

    def _api(self, *args: str) -> Any:
        result = subprocess.run(["gh", "api", *args], capture_output=True, text=True, check=False)  # noqa: S603, S607
        if result.returncode != 0:
            raise Unreachable(result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "gh api failed")
        return json.loads(result.stdout)

    def read(self, repository: str, path: str) -> str:
        payload = self._api(f"repos/{repository}/contents/{path}")
        return base64.b64decode(payload["content"]).decode("utf-8")

    def files(self, repository: str) -> list[str]:
        tree = self._api(f"repos/{repository}/git/trees/HEAD?recursive=1")
        return [entry["path"] for entry in tree.get("tree", []) if entry.get("type") == "blob"]

    def repositories(self) -> list[str]:
        listing = self._api("--paginate", f"orgs/{self.org}/repos?per_page=100")
        return sorted(r["full_name"] for r in listing if not r.get("archived"))


class LocalSource:
    """Sibling checkouts in one directory, keyed by the repository's name. For running by hand."""

    def __init__(self, workspace: Path, aliases: Mapping[str, str]) -> None:
        self.workspace = workspace
        self.aliases = aliases

    def _dir(self, repository: str) -> Path:
        name = repository.split("/", 1)[1]
        directory = self.workspace / self.aliases.get(name, name)
        if not directory.is_dir():
            raise Unreachable(f"no checkout at {directory}")
        return directory

    def read(self, repository: str, path: str) -> str:
        target = self._dir(repository) / path
        if not target.is_file():
            raise FileNotFoundError(path)
        return target.read_text(encoding="utf-8")

    def files(self, repository: str) -> list[str]:
        root = self._dir(repository)
        listing = subprocess.run(["git", "-C", str(root), "ls-files"], capture_output=True, text=True, check=False)  # noqa: S603, S607
        return listing.stdout.split() if listing.returncode == 0 else []

    def repositories(self) -> list[str]:
        return []


class Registry(Protocol):
    """The two package indexes, which say what each library currently publishes."""

    def pypi(self, package: str) -> Mapping[str, Any]: ...

    def npm(self, package: str) -> Mapping[str, Any]: ...


class HttpRegistry:
    def _get(self, url: str) -> Mapping[str, Any]:
        try:
            with urllib.request.urlopen(url, timeout=30) as response:  # noqa: S310
                return json.loads(response.read())
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return {}
            raise

    def pypi(self, package: str) -> Mapping[str, Any]:
        return self._get(f"https://pypi.org/pypi/{package}/json")

    def npm(self, package: str) -> Mapping[str, Any]:
        return self._get(f"https://registry.npmjs.org/{package.replace('/', '%2F')}")


# ── The report ─────────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Current:
    """What one library publishes now. Both the stable line and the newest release of any kind."""

    library: str
    stable: str | None
    newest: str | None


@dataclass(frozen=True, slots=True)
class Observation:
    consumer: str
    library: str
    entry_point: str
    means: str
    path: str
    locator: str
    form: str
    package: str
    raw: str | None
    level: str | None
    #: For a range, the level the lockfile holds, which is what is actually installed.
    locked: str | None = None
    #: For a delivery path, the library level the pinned server brings with it (FR-018).
    delivers: str | None = None
    via: str | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class Standing:
    """One consumer's position on one library, judged once from its observations."""

    consumer: str
    library: str
    level: str | None
    #: current, behind, ahead, via-provider, unpinned, or unread.
    standing: str
    lag: str | None
    #: A lag on a consumer that is current: it should have closed in the adoption (FR-023).
    stale_lag: bool = False


@dataclass(frozen=True, slots=True)
class EntryPointRow:
    """How a second-language consumer compares, whichever door it came through (FR-038, SC-013)."""

    consumer: str
    entry_point: str
    level: str | None
    #: The scoped release level this consumer is on, read through the facade when it came in by
    #: the shared name. Two consumers are level when this field is equal, whatever their doors.
    scoped_level: str | None


@dataclass
class Report:
    governance_level: str
    current: list[Current] = field(default_factory=list)
    observed: list[Observation] = field(default_factory=list)
    standings: list[Standing] = field(default_factory=list)
    entry_points: list[EntryPointRow] = field(default_factory=list)
    facade_mapping: dict[str, dict[str, str]] = field(default_factory=dict)
    out_of_scope: list[dict[str, str | None]] = field(default_factory=list)
    unexplained: list[str] = field(default_factory=list)
    strangers: list[dict[str, Any]] = field(default_factory=list)
    unreachable: list[dict[str, str]] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        body = asdict(self)
        return {"schema_version": SCHEMA_VERSION, "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), **body}


# ── Reading the registries ─────────────────────────────────────────────────────────────────────────


def _newest(versions: Iterable[str]) -> str | None:
    def key(version: str) -> tuple[Any, ...]:
        text = normalize(version)
        release, _, pre = text.partition("rc") if "rc" in text else (text, "", "")
        parts = tuple(int(p) for p in release.rstrip(".abdevpost").split(".") if p.isdigit())
        # A final release sorts after every candidate of the same number.
        return (*parts, 0 if pre else 1, int(pre) if pre.isdigit() else 0)

    candidates = [v for v in versions if v]
    return max(candidates, key=key) if candidates else None


def current_levels(registry: Registry) -> list[Current]:
    pypi = registry.pypi("idfkit")
    releases = [v for v, files in (pypi.get("releases") or {}).items() if files]
    npm = registry.npm("@idfkit/core")
    tags = npm.get("dist-tags") or {}
    return [
        Current("python", (pypi.get("info") or {}).get("version"), _newest(releases)),
        Current("javascript", tags.get("latest"), _newest(list((npm.get("versions") or {}).keys()))),
    ]


def facade_mapping(registry: Registry) -> dict[str, dict[str, str]]:
    """Which scoped versions each published version of the shared name delivers (FR-038, R10).

    Read from the facade's own published manifest, whose dependencies are exact pins, rather than
    declared anywhere: declaring it again would be R2's mistake in a second place. Empty while the
    shared name has no published version, which is the state on npm today.
    """
    document = registry.npm("idfkit")
    mapping: dict[str, dict[str, str]] = {}
    for version, manifest in (document.get("versions") or {}).items():
        pins = {k: v for k, v in (manifest.get("dependencies") or {}).items() if k in GOVERNED_PACKAGES["javascript"]}
        if pins:
            mapping[version] = pins
    return mapping


def scoped_level(level: str | None, entry_point: str, mapping: Mapping[str, Mapping[str, str]]) -> str | None:
    """The scoped release a consumer is on, whichever door it came through."""
    if level is None:
        return None
    if entry_point == "scoped":
        return normalize(level)
    delivered = mapping.get(level, {}).get("@idfkit/core")
    return normalize(delivered) if delivered else None


def provider_delivers(registry: Registry, package: str, version: str) -> str | None:
    """The idfkit level a published server version pins, from its own published metadata."""
    document = registry.pypi(f"{package}/{version.removeprefix('v')}")
    for requirement in (document.get("info") or {}).get("requires_dist") or []:
        observed = level_of(requirement.split(";", 1)[0], "idfkit")
        if requirement.lower().startswith("idfkit") and not requirement.lower().startswith(("idfkit-", "idfkit_")):
            return observed.level
    return None


# ── Following declarations ─────────────────────────────────────────────────────────────────────────


def _locked(source: Source, repository: str, declaration: Declaration, package: str) -> str | None:
    """For a range, the level the lockfile beside the manifest actually installs."""
    directory = declaration.path.rpartition("/")[0]
    prefix = f"{directory}/" if directory else ""
    try:
        if declaration.path.endswith("package.json"):
            lock = json.loads(source.read(repository, f"{prefix}package-lock.json"))
            return (lock.get("packages", {}).get(f"node_modules/{package}") or {}).get("version")
        if declaration.path.endswith("pyproject.toml"):
            lock = tomllib.loads(source.read(repository, f"{prefix}uv.lock"))
            return next((p["version"] for p in lock.get("package", []) if p.get("name") == package), None)
    except (FileNotFoundError, Unreachable, json.JSONDecodeError, tomllib.TOMLDecodeError):
        return None
    return None


def observe(consumer: Consumer, source: Source, registry: Registry) -> list[Observation]:
    rows: list[Observation] = []
    for resolution in consumer.libraries:
        for declaration in resolution.declared_at:
            package = declaration.package or _package_for(declaration, resolution.library)
            base = {
                "consumer": consumer.id,
                "library": resolution.library,
                "entry_point": resolution.entry_point,
                "means": resolution.means,
                "path": declaration.path,
                "locator": declaration.locator,
                "form": declaration.form,
                "package": package,
                "via": declaration.via,
            }
            try:
                raw = resolve(declaration.path, source.read(consumer.repository, declaration.path), declaration.locator)
            except (LocatorError, FileNotFoundError, Unreachable, ValueError) as error:
                rows.append(Observation(**base, raw=None, level=None, error=str(error)))
                continue
            observed = level_of(raw, package)
            locked = _locked(source, consumer.repository, declaration, package) if declaration.form == "range" else None
            delivers = None
            if package in DELIVERED_PACKAGES and observed.level:
                delivers = provider_delivers(registry, package, observed.level)
            rows.append(Observation(**base, raw=observed.raw, level=observed.level, locked=locked, delivers=delivers))
    return rows


def _package_for(declaration: Declaration, library: str) -> str:
    for candidate in sorted(GOVERNED_PACKAGES[library], key=len, reverse=True):
        if candidate in declaration.locator:
            return candidate
    return "idfkit"


def judge(consumer: Consumer, rows: Sequence[Observation], current: Mapping[str, Current]) -> list[Standing]:
    standings: list[Standing] = []
    for resolution in consumer.libraries:
        mine = [r for r in rows if r.library == resolution.library]
        lag = resolution.lag.kind if resolution.lag else None
        newest = current[resolution.library].newest
        stable = current[resolution.library].stable
        own = [r for r in mine if r.package in GOVERNED_PACKAGES[resolution.library]]
        delivered = [r for r in mine if r.package in DELIVERED_PACKAGES]
        if any(r.error for r in mine):
            standings.append(Standing(consumer.id, resolution.library, None, "unread", lag))
            continue
        if own:
            level = own[0].locked or own[0].level
            if level is None:
                standing = "unpinned"
            elif newest and normalize(level) == normalize(newest):
                standing = "current"
            elif stable and normalize(level) == normalize(stable) and newest and newest != stable:
                # On the stable line while candidates are out: behind the newest release, and the
                # register must still say whether that is a decision.
                standing = "behind"
            else:
                standing = "behind"
            standings.append(Standing(consumer.id, resolution.library, level, standing, lag, stale_lag=standing == "current" and lag is not None))
        elif delivered:
            # A delivery path's level is its provider's release. Its lag is the provider's to state,
            # except where the path itself is unpinned, which is this consumer's own gap (FR-015).
            unpinned = [r for r in delivered if r.level is None]
            levels = sorted({r.delivers for r in delivered if r.delivers})
            standing = "unpinned" if unpinned else "via-provider"
            standings.append(Standing(consumer.id, resolution.library, ", ".join(levels) or None, standing, lag))
    return standings


# ── Strangers ──────────────────────────────────────────────────────────────────────────────────────


def find_strangers(register: Register, source: Source) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Every repository resolving either library that the roster does not list (FR-008)."""
    known = {c.repository.lower() for c in register.consumers} | {r.lower() for r in register.libraries.values()}
    strangers: list[dict[str, Any]] = []
    unreachable: list[dict[str, str]] = []
    try:
        repositories = source.repositories()
    except Unreachable as error:
        return [], [{"repository": "(organisation listing)", "reason": str(error)}]
    for repository in repositories:
        if repository.lower() in known:
            continue
        try:
            manifests = [
                p
                for p in source.files(repository)
                if p.rsplit("/", 1)[-1] in _MANIFEST_NAMES and not set(p.split("/")[:-1]) & SKIP_DIRECTORIES
            ]
            found = []
            for path in manifests:
                found += [d for d in detect_text(path, source.read(repository, path)) if d.package not in OUT_OF_SCOPE_PACKAGES]
        except (Unreachable, FileNotFoundError) as error:
            unreachable.append({"repository": repository, "reason": str(error)})
            continue
        if found:
            strangers.append(
                {"repository": repository, "declarations": sorted({f"{d.path}: {d.package}" for d in found})}
            )
    return strangers, unreachable


# ── The sweep ──────────────────────────────────────────────────────────────────────────────────────


def sweep(register: Register, source: Source, registry: Registry, governance_level: str, *, strangers: bool = True) -> Report:
    report = Report(governance_level=governance_level)
    report.current = current_levels(registry)
    current = {c.library: c for c in report.current}
    report.facade_mapping = facade_mapping(registry)

    for consumer in register.consumers:
        try:
            rows = observe(consumer, source, registry)
        except Unreachable as error:
            report.unreachable.append({"repository": consumer.repository, "reason": str(error)})
            continue
        if rows and all(r.error and "no checkout" in r.error for r in rows):
            report.unreachable.append({"repository": consumer.repository, "reason": rows[0].error or ""})
            continue
        report.observed += rows
        standings = judge(consumer, rows, current)
        report.standings += standings
        for standing in standings:
            if standing.standing in {"behind", "unpinned"} and standing.lag is None:
                report.unexplained.append(f"{standing.consumer} ({standing.library}): {standing.standing} with no lag")
        javascript = consumer.resolution("javascript")
        if javascript:
            level = next((r.level for r in rows if r.library == "javascript" and r.package in GOVERNED_PACKAGES["javascript"]), None)
            report.entry_points.append(
                EntryPointRow(consumer.id, javascript.entry_point, level, scoped_level(level, javascript.entry_point, report.facade_mapping))
            )
        for entry in consumer.out_of_scope:
            try:
                raw = resolve(entry.path, source.read(consumer.repository, entry.path), entry.locator)
            except (LocatorError, FileNotFoundError, Unreachable) as error:
                raw = f"unreadable: {error}"
            report.out_of_scope.append({"consumer": consumer.id, "package": entry.package, "declared": raw, "note": entry.note})

    if strangers:
        report.strangers, unreachable = find_strangers(register, source)
        report.unreachable += unreachable
    return report


def render(report: Report) -> str:
    lines = [f"consumer sweep against governance/consumers.toml at {report.governance_level}", ""]
    for current in report.current:
        lines.append(f"  {current.library:<11} publishes stable {current.stable}, newest {current.newest}")
    lines.append("")
    for standing in report.standings:
        if standing.standing == "unread":
            flag = "   <- a declaration did not resolve; the consumer's self-check names which"
        elif standing.standing in {"current", "via-provider"} or standing.lag:
            flag = ""
        else:
            flag = "   <- unexplained"
        stale = "   <- lag should have closed" if standing.stale_lag else ""
        lines.append(
            f"  {standing.consumer:<22} {standing.library:<11} {str(standing.level):<14} {standing.standing:<13} lag={standing.lag}{flag}{stale}"
        )
    lines.append("")
    for row in report.entry_points:
        lines.append(f"  {row.consumer:<22} door={row.entry_point:<12} scoped release {row.scoped_level}")
    if not report.facade_mapping:
        lines.append("  the shared name has no published version, so every second-language consumer is on the scoped door")
    lines.append("")
    lines.append(f"  strangers:   {len(report.strangers)}" + "".join(f"\n    {s['repository']}: {s['declarations']}" for s in report.strangers))
    lines.append(f"  unreachable: {len(report.unreachable)}" + "".join(f"\n    {u['repository']}: {u['reason']}" for u in report.unreachable))
    lines.append(f"  unexplained: {len(report.unexplained)}" + "".join(f"\n    {u}" for u in report.unexplained))
    return "\n".join(lines)


def exit_code(report: Report) -> int:
    if report.strangers:
        return 1
    if report.unreachable:
        return 2
    return 0


def _governance_level() -> str:
    described = subprocess.run(  # noqa: S603
        ["git", "-C", str(ROOT), "describe", "--tags", "--always", "--dirty", "--match", "governance-*"],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    return described.stdout.strip() or "(unknown)"


def main(argv: Sequence[str] | None = None, *, registry_factory: Callable[[], Registry] = HttpRegistry) -> int:
    parser = argparse.ArgumentParser(description="Read every consumer's declared level and find unregistered consumers.")
    parser.add_argument("--register", type=Path, default=ROOT / "governance" / "consumers.toml")
    parser.add_argument("--out", type=Path, default=ROOT / "consumer-report.json")
    parser.add_argument("--org", default="idfkit")
    parser.add_argument(
        "--workspace", type=Path, help="read sibling checkouts in this directory instead of GitHub (strangers are skipped)"
    )
    args = parser.parse_args(argv)

    register = Register.load(args.register)
    if args.workspace:
        source: Source = LocalSource(args.workspace, {"idfkit-app": "envelop"})
    else:
        source = GitHubSource(args.org)
    report = sweep(register, source, registry_factory(), _governance_level(), strangers=not args.workspace)
    args.out.write_text(json.dumps(report.to_json(), indent=2) + "\n", encoding="utf-8")
    print(render(report))
    return exit_code(report)


if __name__ == "__main__":
    sys.exit(main())
