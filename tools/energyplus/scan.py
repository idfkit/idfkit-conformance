"""Compare every rostered project with what the roster declares about it.

contracts/version-roster.md section 5. For each project, with N the newest supported release:

    stale     a declared statement names a release other than N
    missing   a declared statement matches the wrong number of times, or a declared file is absent
    stranger  an occurrence of N, in any of its eight spellings, that no declaration accounts for

    python -m tools.energyplus.scan --workspace ..        sibling checkouts, each at origin/HEAD
    python -m tools.energyplus.scan --org idfkit          default branches, through `gh`

Exit 0 when nothing is stale, missing or strange. Exit 1 when something is. Exit 2 when nothing was
found and at least one project could not be read; the two are never merged, for the reason
`sweep_consumers.py` gives.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from sweep_consumers import LocalSource, Source, Unreachable

from .roster import Ignore, Pin, Project, Release, Roster, Statement, Target

ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_ROSTER = ROOT / "governance" / "energyplus.toml"
#: Files larger than this are data, not prose or code, and are not searched for strangers.
MAX_BYTES = 2_000_000
#: Checkout directories whose name differs from the repository's name.
ALIASES = {"idfkit-app": "envelop"}


class FindingKind(Enum):
    STALE = "stale"
    MISSING = "missing"
    STRANGER = "stranger"


@dataclass(frozen=True, slots=True)
class Finding:
    kind: FindingKind
    project: str
    path: str
    line: int
    text: str
    detail: str

    def stated(self) -> str:
        place = f"{self.path}:{self.line}" if self.line else self.path
        return f"{self.kind.value:8} {self.project} {place}  {self.detail}" + (f"\n         {self.text}" if self.text else "")


@dataclass(frozen=True, slots=True)
class Unread:
    project: str
    cause: str


@dataclass(frozen=True, slots=True)
class ScanReport:
    newest: Release
    count: int
    findings: tuple[Finding, ...]
    unread: tuple[Unread, ...]

    def of(self, kind: FindingKind) -> tuple[Finding, ...]:
        return tuple(finding for finding in self.findings if finding.kind is kind)

    def exit_code(self) -> int:
        if self.findings:
            return 1
        return 2 if self.unread else 0


# ── Globs and spellings ────────────────────────────────────────────────────────────────────────────


def glob_pattern(glob: str) -> re.Pattern[str]:
    """`**/` matches any number of directories, `**` anything, `*` anything within one directory."""
    parts = []
    index = 0
    while index < len(glob):
        if glob.startswith("**/", index):
            parts.append("(?:.*/)?")
            index += 3
        elif glob.startswith("**", index):
            parts.append(".*")
            index += 2
        elif glob[index] == "*":
            parts.append("[^/]*")
            index += 1
        elif glob[index] == "?":
            parts.append("[^/]")
            index += 1
        else:
            parts.append(re.escape(glob[index]))
            index += 1
    return re.compile("".join(parts) + r"\Z")


def is_concrete(glob: str) -> bool:
    return not any(character in glob for character in "*?")


def spellings(release: Release) -> re.Pattern[str]:
    """One pattern for the eight spellings of data-model 1.1.

    The lookarounds keep `0.26.1` (a package version), `26.10` and `2026-10-13` (a date) out.
    """
    major, minor, patch = release.major, release.minor, release.patch
    return re.compile(
        rf"(?<![\d.])[vV]?{major}\.{minor}(?:\.{patch})?(?!\.?\d)"
        rf"|(?<![\d-])[vV]?{major}-{minor}(?:-{patch})?(?!-?\d)"
        rf"|(?<!\d){major}_{minor}(?!\d)"
        rf"|(?<!\d){major},\s*{minor},\s*{patch}(?!\d)"
    )


# ── The checks ─────────────────────────────────────────────────────────────────────────────────────

Read = Callable[[str], str]


def _line_of(text: str, offset: int) -> tuple[int, str]:
    number = text.count("\n", 0, offset) + 1
    start = text.rfind("\n", 0, offset) + 1
    end = text.find("\n", offset)
    return number, text[start : end if end != -1 else len(text)].strip()[:200]


def check_statement(project: str, statement: Statement, text: str, target: Target) -> list[Finding]:
    matches = list(statement.compiled().finditer(text))
    if len(matches) != statement.occurrences:
        detail = f"pattern {statement.pattern!r} matches {len(matches)} time(s), declared {statement.occurrences}"
        return [Finding(FindingKind.MISSING, project, statement.path, 0, "", detail)]
    findings = []
    for match in matches:
        found = match.group("release")
        if not statement.form.agrees(found, target):
            number, line = _line_of(text, match.start("release"))
            expected = statement.form.rewrite(found, target)
            findings.append(Finding(FindingKind.STALE, project, statement.path, number, line, f"states {found!r}, expected {expected!r}"))
    return findings


def statement_findings(project: Project, read: Read, target: Target) -> list[Finding]:
    """Check 1, for one project. Also what `StatementsCurrent` evaluates for a release under adoption."""
    findings = []
    for statement in project.statements:
        try:
            text = read(statement.path)
        except FileNotFoundError:
            findings.append(Finding(FindingKind.MISSING, project.id, statement.path, 0, "", "the declared file does not exist"))
            continue
        findings.extend(check_statement(project.id, statement, text, target))
    return findings


def _accounted(project: Project, defaults: Sequence[Ignore], default_pins: Sequence[Pin]) -> list[re.Pattern[str]]:
    globs = [generated.glob for generated in project.generated] + [pin.glob for pin in (*default_pins, *project.pins)]
    globs += [ignore.glob for ignore in (*defaults, *project.ignores) if ignore.glob]
    return [glob_pattern(glob) for glob in globs]


def stranger_findings(
    project: Project, files: Sequence[str], read: Read, newest: Release, defaults: Sequence[Ignore], default_pins: Sequence[Pin] = ()
) -> list[Finding]:
    """Check 2, for one project: every occurrence of the newest release is declared."""
    accounted = _accounted(project, defaults, default_pins)
    line_ignores = [re.compile(ignore.pattern) for ignore in (*defaults, *project.ignores) if ignore.pattern]
    spelled = spellings(newest)
    statements: dict[str, list[Statement]] = {}
    for statement in project.statements:
        statements.setdefault(statement.path, []).append(statement)
    findings = []
    for path in files:
        if any(pattern.match(path) for pattern in accounted):
            continue
        try:
            text = read(path)
        except (FileNotFoundError, UnicodeDecodeError, OSError):
            continue
        if len(text) > MAX_BYTES:
            continue
        spans = [match.span() for statement in statements.get(path, ()) for match in statement.compiled().finditer(text)]
        for match in spelled.finditer(text):
            if any(start <= match.start() and match.end() <= end for start, end in spans):
                continue
            number, line = _line_of(text, match.start())
            if any(pattern.search(line) for pattern in line_ignores):
                continue
            findings.append(Finding(FindingKind.STRANGER, project.id, path, number, line, f"{match.group(0)!r} is under no declaration"))
    return findings


def existence_findings(project: Project, files: Sequence[str]) -> list[Finding]:
    """Check 3, for one project: every declared glob that names one file names a file that exists."""
    present = set(files)
    declared = [(generated.glob, "generated") for generated in project.generated] + [(pin.glob, "pin") for pin in project.pins]
    return [
        Finding(FindingKind.MISSING, project.id, glob, 0, "", f"the {kind} declaration names a file that does not exist")
        for glob, kind in declared
        if is_concrete(glob) and glob not in present
    ]


def supported(roster: Roster, source: Source) -> tuple[Release, ...]:
    found = roster.supported(source.read(roster.supported_at.repository, roster.supported_at.path))
    if not found:
        raise Unreachable(f"no supported release found at {roster.supported_at.repository} {roster.supported_at.path}")
    return found


def scan(roster: Roster, source: Source, only: Sequence[str] = ()) -> ScanReport:
    releases = supported(roster, source)
    target = Target(releases[-1], len(releases))
    findings: list[Finding] = []
    unread: list[Unread] = []
    for project in roster.rostered():
        if only and project.id not in only:
            continue

        def read(path: str, repository: str = project.repository) -> str:
            return source.read(repository, path)

        try:
            files = source.files(project.repository)
            if not files:
                raise Unreachable("no tracked files were listed")
            # A project with a recorded lag is behind by declaration, so its statements are not judged.
            if project.lag is None:
                findings.extend(statement_findings(project, read, target))
            findings.extend(stranger_findings(project, files, read, target.release, roster.default_ignores, roster.default_pins))
            findings.extend(existence_findings(project, files))
        except Unreachable as error:
            unread.append(Unread(project.id, str(error)))
    return ScanReport(target.release, target.count, tuple(findings), tuple(unread))


# ── Reading the projects ───────────────────────────────────────────────────────────────────────────


class ArchiveSource:
    """Default branches, each fetched once as an archive and read from disk.

    The stranger check reads every tracked file of every project. One request per file would exceed
    any rate limit, so each repository is extracted once into a scratch directory.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.roots: dict[str, Path] = {}

    def _archive(self, repository: str, archive: Path) -> None:
        """Write a tar archive of the repository's default branch to `archive`."""
        raise NotImplementedError

    def _root(self, repository: str) -> Path:
        if repository not in self.roots:
            archive = self.directory / (repository.replace("/", "__") + ".tar")
            self._archive(repository, archive)
            target = self.directory / repository.replace("/", "__")
            with tarfile.open(archive) as bundle:
                bundle.extractall(target, filter="data")
            archive.unlink()
            inner = [path for path in target.iterdir()]
            # A GitHub tarball wraps the tree in one directory. `git archive` does not.
            self.roots[repository] = inner[0] if len(inner) == 1 and inner[0].is_dir() and self.wrapped else target
        return self.roots[repository]

    wrapped = False

    def read(self, repository: str, path: str) -> str:
        target = self._root(repository) / path
        if not target.is_file() or target.stat().st_size > MAX_BYTES:
            raise FileNotFoundError(path)
        return target.read_text(encoding="utf-8")

    def read_bytes(self, repository: str, path: str) -> bytes:
        target = self._root(repository) / path
        if not target.is_file():
            raise FileNotFoundError(path)
        return target.read_bytes()

    def files(self, repository: str) -> list[str]:
        root = self._root(repository)
        return sorted(str(path.relative_to(root)) for path in root.rglob("*") if path.is_file())

    def repositories(self) -> list[str]:
        return []


class GitHubArchive(ArchiveSource):
    """The organisation's default branches, through `gh`. Used by the scheduled runs."""

    wrapped = True

    def _archive(self, repository: str, archive: Path) -> None:
        with archive.open("wb") as handle:
            result = subprocess.run(["gh", "api", f"repos/{repository}/tarball"], stdout=handle, stderr=subprocess.PIPE, check=False)  # noqa: S603, S607
        if result.returncode != 0:
            raise Unreachable(result.stderr.decode().strip().splitlines()[-1] if result.stderr.strip() else "gh api failed")


class WorkspaceArchive(ArchiveSource):
    """Sibling checkouts, read at the default branch each last fetched and never at the working tree.

    A checkout is often on a feature branch. The roster is written against default branches, so the
    scan reads `origin/HEAD` and is unaffected by what is checked out or uncommitted.
    """

    def __init__(self, directory: Path, workspace: Path, aliases: Mapping[str, str], ref: str = "origin/HEAD") -> None:
        super().__init__(directory)
        self.workspace = workspace
        self.aliases = aliases
        self.ref = ref

    def _archive(self, repository: str, archive: Path) -> None:
        name = repository.split("/", 1)[1]
        checkout = self.workspace / self.aliases.get(name, name)
        if not checkout.is_dir():
            raise Unreachable(f"no checkout at {checkout}")
        result = subprocess.run(["git", "-C", str(checkout), "archive", "--format=tar", "-o", str(archive), self.ref], capture_output=True, text=True, check=False)  # noqa: S603, S607
        if result.returncode != 0:
            raise Unreachable(f"{checkout}: {result.stderr.strip()}")


class WorkingTree(LocalSource):
    """Sibling checkouts as they are on disk, uncommitted changes included. For checking an edit."""

    def files(self, repository: str) -> list[str]:
        root = self._dir(repository)
        listing = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True, text=True, check=False)  # noqa: S603, S607
        if listing.returncode != 0:
            raise Unreachable(f"git ls-files failed in {root}")
        return [name for name in listing.stdout.split("\0") if name and (root / name).is_file()]

    def read(self, repository: str, path: str) -> str:
        target = self._dir(repository) / path
        if target.is_file() and target.stat().st_size > MAX_BYTES:
            raise FileNotFoundError(path)
        return super().read(repository, path)

    def read_bytes(self, repository: str, path: str) -> bytes:
        target = self._dir(repository) / path
        if not target.is_file():
            raise FileNotFoundError(path)
        return target.read_bytes()


def source_for(workspace: Path | None, scratch: Path, working_tree: Sequence[str] = ()) -> Source:
    """The source a command line asks for. `working_tree` names checkouts to read as they are on disk."""
    if workspace is None:
        return GitHubArchive(scratch)
    archive = WorkspaceArchive(scratch, workspace.resolve(), ALIASES)
    return Mixed(archive, WorkingTree(workspace.resolve(), ALIASES), tuple(working_tree)) if working_tree else archive


class Mixed:
    """Default branches, except for the named repositories, which are read from the working tree."""

    def __init__(self, default: Source, tree: Source, names: tuple[str, ...]) -> None:
        self.default = default
        self.tree = tree
        self.names = names

    def _pick(self, repository: str) -> Source:
        return self.tree if repository.split("/", 1)[1] in self.names or "*" in self.names else self.default

    def read(self, repository: str, path: str) -> str:
        return self._pick(repository).read(repository, path)

    def files(self, repository: str) -> list[str]:
        return self._pick(repository).files(repository)

    def read_bytes(self, repository: str, path: str) -> bytes:
        reader = getattr(self._pick(repository), "read_bytes", None)
        if reader is None:
            raise FileNotFoundError(path)
        return reader(repository, path)

    def repositories(self) -> list[str]:
        return []


def render(report: ScanReport) -> str:
    lines = [f"newest supported release {report.newest.full()}, {report.count} supported"]
    lines += [finding.stated() for finding in report.findings]
    lines += [f"unread   {unread.project}  {unread.cause}" for unread in report.unread]
    counts = ", ".join(f"{len(report.of(kind))} {kind.value}" for kind in FindingKind)
    lines.append(f"{counts}, {len(report.unread)} unread")
    return "\n".join(lines)


def summary(report: ScanReport) -> str:
    """Strangers per project and top-level directory, for declaring them in bulk."""
    groups: dict[tuple[str, str], int] = {}
    for finding in report.of(FindingKind.STRANGER):
        head = finding.path.split("/", 1)[0] if "/" in finding.path else finding.path
        groups[(finding.project, head)] = groups.get((finding.project, head), 0) + 1
    return "\n".join(f"{count:6} {project} {head}" for (project, head), count in sorted(groups.items()))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--roster", type=Path, default=DEFAULT_ROSTER)
    where = parser.add_mutually_exclusive_group(required=True)
    where.add_argument("--workspace", type=Path, help="a directory of sibling checkouts, each read at origin/HEAD")
    where.add_argument("--org", help="read default branches of this organisation through gh")
    parser.add_argument("--working-tree", action="append", default=[], metavar="REPOSITORY", help="with --workspace: read this checkout as it is on disk (repeatable; * for all)")
    parser.add_argument("--project", action="append", default=[], help="limit the scan to this project id (repeatable)")
    parser.add_argument("--summary", action="store_true", help="print strangers grouped by project and directory")
    arguments = parser.parse_args(argv)
    roster = Roster.load(arguments.roster)
    with tempfile.TemporaryDirectory() as scratch:
        source = source_for(arguments.workspace, Path(scratch), arguments.working_tree)
        try:
            report = scan(roster, source, arguments.project)
        except Unreachable as error:
            print(f"the supported set could not be read: {error}", file=sys.stderr)
            return 2
    print(summary(report) if arguments.summary else render(report))
    return report.exit_code()


if __name__ == "__main__":
    sys.exit(main())
