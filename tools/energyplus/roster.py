"""The version roster: its typed model and the only reader of `governance/energyplus.toml`.

The roster names every project that carries EnergyPlus-version-dependent content, what must be
observable before each may adopt a release, the steps each performs, and where each states the
latest release. It never states an EnergyPlus version: the supported set is read from the location
`[roster].supported_at` names.

Every entity is a frozen dataclass. A malformed file raises `RosterError`, which names the
validation rule (V1 to V13 of contracts/version-roster.md) that the file breaks. Rules that need
the whole model are in `check_roster.py`.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "1"
#: The pseudo-project that owns the end-to-end verifications in an evaluation.
END_TO_END = "end-to-end"
#: The step whose verification is the adoption record itself. `record.py` writes it as passed.
RECORD_STEP = "record-adoption"
#: The seven end-to-end verifications of spec section 3.3, by id (rule V13).
END_TO_END_IDS = (
    "parses-and-validates",
    "round-trips",
    "migrates",
    "simulates",
    "documentation-published",
    "range-stated",
    "conformance-passes",
)

_TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
_FULL = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
_COUNT_WORDS = (
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
    "seventeen eighteen nineteen twenty twenty-one twenty-two twenty-three twenty-four twenty-five twenty-six "
    "twenty-seven twenty-eight twenty-nine thirty thirty-one thirty-two thirty-three thirty-four thirty-five "
    "thirty-six thirty-seven thirty-eight thirty-nine forty"
).split()


class RosterError(ValueError):
    """The roster file breaks a validation rule."""

    def __init__(self, rule: str, where: str, message: str) -> None:
        super().__init__(f"{rule} {where}: {message}")
        self.rule = rule
        self.where = where
        self.message = message


# ── Release ────────────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, order=True, slots=True)
class Release:
    """An EnergyPlus version."""

    major: int
    minor: int
    patch: int

    @classmethod
    def parse_tag(cls, text: str) -> Release | None:
        """The single implementation of research R1: exactly `vX.Y.Z`, and nothing else."""
        match = _TAG.match(text)
        return cls(*(int(part) for part in match.groups())) if match else None

    @classmethod
    def parse(cls, text: str) -> Release:
        """`X.Y.Z`, with or without a leading `v`. Raises `ValueError` on anything else."""
        match = _FULL.match(text.removeprefix("v"))
        if not match:
            raise ValueError(f"{text!r} is not an EnergyPlus release of the form X.Y.Z")
        return cls(*(int(part) for part in match.groups()))

    def full(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"

    def line(self) -> str:
        return f"{self.major}.{self.minor}"

    def dashed(self) -> str:
        return f"{self.major}-{self.minor}-{self.patch}"

    def dashed_line(self) -> str:
        return f"{self.major}-{self.minor}"

    def underscored_line(self) -> str:
        return f"{self.major}_{self.minor}"

    def tag(self) -> str:
        return f"v{self.full()}"

    def directory(self) -> str:
        return f"V{self.dashed()}"

    def tuple_literal(self) -> str:
        return f"({self.major}, {self.minor}, {self.patch})"

    def substitute(self, template: str) -> str:
        """Replace `{full}`, `{tag}` and the other six rendering names in a command or a path."""
        for form in RELEASE_FORMS:
            template = template.replace("{" + form.placeholder() + "}", form.render(Target(self, 0)))
        return template


@dataclass(frozen=True, slots=True)
class Target:
    """What a statement must say: the release, and how many releases are supported with it."""

    release: Release
    count: int


class Form(Enum):
    """How a statement's `release` group renders a release (data-model 1.1 and 1.9)."""

    FULL = "full"
    LINE = "line"
    DASHED = "dashed"
    DASHED_LINE = "dashed-line"
    UNDERSCORED_LINE = "underscored-line"
    TAG = "tag"
    DIRECTORY = "directory"
    TUPLE = "tuple"
    COUNT = "count"
    COUNT_WORD = "count-word"

    def placeholder(self) -> str:
        return {"tuple": "tuple_literal"}.get(self.value, self.value.replace("-", "_"))

    def render(self, target: Target) -> str:
        release = target.release
        match self:
            case Form.FULL:
                return release.full()
            case Form.LINE:
                return release.line()
            case Form.DASHED:
                return release.dashed()
            case Form.DASHED_LINE:
                return release.dashed_line()
            case Form.UNDERSCORED_LINE:
                return release.underscored_line()
            case Form.TAG:
                return release.tag()
            case Form.DIRECTORY:
                return release.directory()
            case Form.TUPLE:
                return release.tuple_literal()
            case Form.COUNT:
                return str(target.count)
            case Form.COUNT_WORD:
                return _COUNT_WORDS[target.count]

    def rewrite(self, found: str, target: Target) -> str:
        """The text that replaces `found`, keeping the delimiters and the case the file uses."""
        text = self.render(target)
        if self is Form.TUPLE and not found.startswith("("):
            return text[1:-1]
        if self is Form.COUNT_WORD and found[:1].isupper():
            return text.capitalize()
        return text

    def agrees(self, found: str, target: Target) -> bool:
        return found == self.rewrite(found, target)

    def read(self, found: str) -> Release | None:
        """The release a matched group names, or `None` for a count or an unreadable group."""
        if self in (Form.COUNT, Form.COUNT_WORD):
            return None
        numbers = [int(number) for number in re.findall(r"\d+", found)]
        if len(numbers) == 2:
            numbers.append(0)
        return Release(*numbers) if len(numbers) == 3 else None


RELEASE_FORMS = tuple(form for form in Form if form not in (Form.COUNT, Form.COUNT_WORD))


# ── Vocabulary ─────────────────────────────────────────────────────────────────────────────────────


class ContentKind(Enum):
    SCHEMA = "schema"
    TYPES = "types"
    ENGINE = "engine"
    DOCUMENTATION = "documentation"
    LIBRARY_LEVEL = "library-level"
    RUNTIME_IMAGE = "runtime-image"
    PROSE = "prose"
    EXPECTATIONS = "expectations"


class StepClass(Enum):
    AUTOMATED = "automated"
    REVIEWED = "reviewed"
    MANUAL = "manual"


class ClassCondition(Enum):
    """The three conditions of FR-021 under which a step may be other than automated."""

    SILENT_EFFECT = "silent-effect"
    UNDECIDED_CHOICE = "undecided-choice"
    PUBLISHES = "publishes"


class OpeningKind(Enum):
    UPSTREAM_ASSETS = "upstream-assets"
    MERGED = "merged"
    PUBLISHED = "published"
    PROJECT_COMPLETE = "project-complete"


class LagKind(Enum):
    NOT_YET = "not-yet"
    DELIBERATE = "deliberate"


class AssetRole(Enum):
    """An upstream asset the suite consumes, with the pattern that finds it (data-model 1.3)."""

    LINUX_X86_64 = "linux-x86_64"
    LINUX_ARM64 = "linux-arm64"
    CHECKSUMS = "checksums"

    def pattern(self, release: Release) -> str:
        match self:
            case AssetRole.LINUX_X86_64:
                return f"EnergyPlus-{release.full()}-*-Linux-Ubuntu*-x86_64.tar.gz"
            case AssetRole.LINUX_ARM64:
                return f"EnergyPlus-{release.full()}-*-Linux-Ubuntu*-arm64.tar.gz"
            case AssetRole.CHECKSUMS:
                return "sha256sums.txt"


# ── Locations and the four declarations ────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Location:
    """A place where a release is written: a file, and a pattern whose `release` group holds it."""

    repository: str
    path: str
    pattern: str
    form: Form

    def compiled(self) -> re.Pattern[str]:
        return re.compile(self.pattern, re.MULTILINE)

    def releases(self, text: str) -> tuple[Release, ...]:
        found = (self.form.read(match.group("release")) for match in self.compiled().finditer(text))
        return tuple(release for release in found if release is not None)


@dataclass(frozen=True, slots=True)
class Statement:
    """A statement of the latest release, rewritten at adoption."""

    path: str
    pattern: str
    form: Form
    occurrences: int = 1

    def compiled(self) -> re.Pattern[str]:
        return re.compile(self.pattern, re.MULTILINE)


@dataclass(frozen=True, slots=True)
class Generated:
    """Files produced by a step. Never rewritten directly."""

    glob: str
    by_step: str


@dataclass(frozen=True, slots=True)
class Pin:
    """Files that name a release on purpose. Never rewritten."""

    glob: str
    reason: str


@dataclass(frozen=True, slots=True)
class Ignore:
    """Files, or lines, in which a matching text is not an EnergyPlus version."""

    glob: str | None
    pattern: str | None
    reason: str


@dataclass(frozen=True, slots=True)
class Lag:
    kind: LagKind
    issue: str | None
    reason: str | None

    def stated(self) -> str:
        if self.kind is LagKind.DELIBERATE:
            return f"Deliberate lag: {self.reason}"
        return f"Not yet adopted: {self.issue}"


# ── Verifications ──────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class RegistryLists:
    """The release appears at a location. `repository` is `None` for the step's own project."""

    repository: str | None
    path: str
    pattern: str
    form: Form


@dataclass(frozen=True, slots=True)
class PathExists:
    repository: str | None
    path: str


@dataclass(frozen=True, slots=True)
class StatementsCurrent:
    """Every declared statement renders the release. `project` is `None` for the step's own, `*` for all."""

    project: str | None


@dataclass(frozen=True, slots=True)
class ChecksGreen:
    """The checks concluded successfully, on a commit that carries the release."""

    repository: str | None
    workflow: str | None


@dataclass(frozen=True, slots=True)
class WorkflowSucceeded:
    repository: str | None
    workflow: str


@dataclass(frozen=True, slots=True)
class TagExists:
    """A tag exists in a repository. `tag` is a glob with renderings substituted.

    Used where a build's observable result is a tag and not a file on a branch.
    """

    repository: str | None
    tag: str


@dataclass(frozen=True, slots=True)
class Level:
    """Where a consumer writes the version of a package it resolves. The pattern's group is `level`.

    `via` names an intermediate package on the same index: the consumer pins `via`, and the version
    of `via` that it pins declares the version of the package in its own published metadata.
    """

    path: str
    pattern: str
    via: str | None = None


@dataclass(frozen=True, slots=True)
class PublishedCarries:
    """A published version of a package carries the release.

    The version examined is the most recently published one, or the one a consumer pins when
    `through` is given. It carries the release when the published artifact contains `member` (a
    path with renderings substituted), or, when `version_names_release` is set, when the published
    version itself is on the release's line.
    """

    index: str
    package: str
    member: str | None
    version_names_release: bool
    through: Level | None


@dataclass(frozen=True, slots=True)
class HttpContains:
    url: str
    pattern: str
    form: Form


@dataclass(frozen=True, slots=True)
class Accepted:
    """A person merged or approved the pull request carrying the step."""


@dataclass(frozen=True, slots=True)
class All:
    of: tuple[Verification, ...]


Verification = (
    RegistryLists | PathExists | StatementsCurrent | ChecksGreen | WorkflowSucceeded | TagExists | PublishedCarries | HttpContains | Accepted | All
)


def members(verification: Verification) -> Iterator[Verification]:
    """The verification and, for `All`, every member at any depth."""
    if isinstance(verification, All):
        for member in verification.of:
            yield from members(member)
    else:
        yield verification


# ── Openings, steps, projects ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Opening:
    """What must be observable before a project's steps may start (data-model 1.6)."""

    kind: OpeningKind
    roles: tuple[AssetRole, ...] = ()
    project: str | None = None
    step: str | None = None
    published: PublishedCarries | None = None

    def stated(self) -> str:
        match self.kind:
            case OpeningKind.UPSTREAM_ASSETS:
                return "upstream assets present"
            case OpeningKind.MERGED:
                return f"the {self.project} adoption merged"
            case OpeningKind.PUBLISHED:
                assert self.published is not None
                return f"a release of {self.published.package} that carries the release"
            case OpeningKind.PROJECT_COMPLETE:
                return f"{self.project} complete"


@dataclass(frozen=True, slots=True)
class Step:
    id: str
    title: str
    step_class: StepClass
    verify: Verification
    class_reason: str | None = None
    condition: ClassCondition | None = None
    run: str | None = None
    prepares: str | None = None
    inspect: str | None = None
    needs_energyplus: bool = False


@dataclass(frozen=True, slots=True)
class Project:
    id: str
    repository: str
    content: tuple[ContentKind, ...] = ()
    carries_none_reason: str | None = None
    opens_on: tuple[Opening, ...] = ()
    steps: tuple[Step, ...] = ()
    statements: tuple[Statement, ...] = ()
    generated: tuple[Generated, ...] = ()
    pins: tuple[Pin, ...] = ()
    ignores: tuple[Ignore, ...] = ()
    gate: str = ""
    workdir: str = "."
    lag: Lag | None = None
    #: The workflow whose conclusion on the default branch is the project's own checks. When absent,
    #: every check on the head commit counts, including jobs that are not quality checks.
    checks: str | None = None
    #: The packages a maintainer of this project publishes. Names who owes a publication others wait on.
    publishes: tuple[str, ...] = ()
    #: False for the host, whose one step is performed by the watch and not by a caller workflow.
    caller: bool = True
    #: What merging the adoption pull request does beyond changing the branch (rule C8).
    merge_effect: str | None = None
    #: The action that remains for a maintainer after the merge. Never performed by the run.
    after_merge: str | None = None

    def step(self, step_id: str) -> Step:
        for step in self.steps:
            if step.id == step_id:
                return step
        raise KeyError(f"{self.id} has no step {step_id!r}")


@dataclass(frozen=True, slots=True)
class Roster:
    schema_version: str
    upstream: str
    supported_at: Location
    default_ignores: tuple[Ignore, ...]
    default_pins: tuple[Pin, ...]
    projects: tuple[Project, ...]
    verifications: tuple[Step, ...]

    @classmethod
    def load(cls, path: Path) -> Roster:
        with path.open("rb") as handle:
            return _roster(tomllib.load(handle))

    @classmethod
    def from_text(cls, text: str) -> Roster:
        return _roster(tomllib.loads(text))

    def project(self, project_id: str) -> Project:
        for project in self.projects:
            if project.id == project_id:
                return project
        raise KeyError(f"the roster has no project {project_id!r}")

    def by_repository(self, repository: str) -> Project | None:
        return next((project for project in self.projects if project.repository == repository), None)

    def rostered(self) -> tuple[Project, ...]:
        """The projects that carry version-dependent content."""
        return tuple(project for project in self.projects if project.content)

    def supported(self, text: str) -> tuple[Release, ...]:
        """The supported releases, ascending, from the text of the `supported_at` file."""
        return tuple(sorted(set(self.supported_at.releases(text))))


# ── Reading the file ───────────────────────────────────────────────────────────────────────────────

Table = Mapping[str, Any]


def _need(table: Table, key: str, rule: str, where: str) -> Any:
    if key not in table:
        raise RosterError(rule, where, f"`{key}` is required")
    return table[key]


def _enum(kind: type[Enum], value: Any, rule: str, where: str) -> Any:
    try:
        return kind(value)
    except ValueError:
        allowed = ", ".join(member.value for member in kind)
        raise RosterError(rule, where, f"{value!r} is not one of: {allowed}") from None


def _pattern(pattern: str, group: str, where: str) -> str:
    try:
        compiled = re.compile(pattern, re.MULTILINE)
    except re.error as error:
        raise RosterError("V12", where, f"pattern does not compile: {error}") from None
    if tuple(compiled.groupindex) != (group,):
        raise RosterError("V12", where, f"pattern must have exactly one named group, `{group}`")
    return pattern


def _location(table: Table, repository: str | None, where: str) -> Location:
    repository = table.get("repository", repository)
    if repository is None:
        raise RosterError("V9", where, "`repository` is required")
    return Location(
        repository=repository,
        path=_need(table, "path", "V9", where),
        pattern=_pattern(_need(table, "pattern", "V12", where), "release", where),
        form=_enum(Form, _need(table, "form", "V12", where), "V12", where),
    )


def _published(table: Table, where: str) -> PublishedCarries:
    through = table.get("through")
    index = _need(table, "index", "V9", where)
    if index not in ("pypi", "npm"):
        raise RosterError("V9", where, f"index {index!r} is not one of: pypi, npm")
    member = table.get("member")
    names = bool(table.get("version_names_release", False))
    if (member is None) == (not names):
        raise RosterError("V9", where, "state exactly one of `member` and `version_names_release`")
    return PublishedCarries(
        index=index,
        package=_need(table, "package", "V9", where),
        member=member,
        version_names_release=names,
        through=None
        if through is None
        else Level(
            _need(through, "path", "V9", where),
            _pattern(_need(through, "pattern", "V12", where), "level", f"{where}.through"),
            through.get("via"),
        ),
    )


def _verification(table: Table, where: str) -> Verification:
    kind = _need(table, "kind", "V3", where)
    match kind:
        case "registry-lists":
            located = _location({"repository": "", **table}, None, where)
            return RegistryLists(table.get("repository"), located.path, located.pattern, located.form)
        case "path-exists":
            return PathExists(table.get("repository"), _need(table, "path", "V9", where))
        case "statements-current":
            return StatementsCurrent(table.get("project"))
        case "checks-green":
            return ChecksGreen(table.get("repository"), table.get("workflow"))
        case "workflow-succeeded":
            return WorkflowSucceeded(table.get("repository"), _need(table, "workflow", "V9", where))
        case "tag-exists":
            return TagExists(table.get("repository"), _need(table, "tag", "V9", where))
        case "published-carries":
            return _published(table, where)
        case "http-contains":
            form = _enum(Form, _need(table, "form", "V12", where), "V12", where)
            return HttpContains(_need(table, "url", "V9", where), _pattern(_need(table, "pattern", "V12", where), "release", where), form)
        case "accepted":
            return Accepted()
        case "all":
            of = tuple(_verification(member, f"{where}.of[{index}]") for index, member in enumerate(_need(table, "of", "V9", where)))
            return All(of)
    raise RosterError("V9", where, f"unknown verification kind {kind!r}")


def _step(table: Table, where: str) -> Step:
    step_id = _need(table, "id", "V1", where)
    where = f"{where} step {step_id}"
    condition = table.get("condition")
    return Step(
        id=step_id,
        title=_need(table, "title", "V1", where),
        step_class=_enum(StepClass, _need(table, "class", "V3", where), "V3", where),
        verify=_verification(_need(table, "verify", "V9", where), where),
        class_reason=table.get("class_reason"),
        condition=None if condition is None else _enum(ClassCondition, condition, "V4", where),
        run=table.get("run"),
        prepares=table.get("prepares"),
        inspect=table.get("inspect"),
        needs_energyplus=bool(table.get("needs_energyplus", False)),
    )


def _opening(table: Table, where: str) -> Opening:
    kind = _enum(OpeningKind, _need(table, "kind", "V9", where), "V9", where)
    match kind:
        case OpeningKind.UPSTREAM_ASSETS:
            roles = tuple(_enum(AssetRole, role, "V9", where) for role in _need(table, "roles", "V9", where))
            return Opening(kind, roles=roles)
        case OpeningKind.MERGED:
            return Opening(kind, project=_need(table, "project", "V9", where), step=_need(table, "step", "V9", where))
        case OpeningKind.PUBLISHED:
            return Opening(kind, published=_published(table, where))
        case OpeningKind.PROJECT_COMPLETE:
            return Opening(kind, project=_need(table, "project", "V9", where))
    raise RosterError("V9", where, f"unknown opening kind {kind!r}")


def _ignore(table: Table, where: str) -> Ignore:
    pattern = table.get("pattern")
    if pattern is not None:
        try:
            re.compile(pattern)
        except re.error as error:
            raise RosterError("V12", where, f"pattern does not compile: {error}") from None
    return Ignore(table.get("glob"), pattern, table.get("reason", ""))


def _lag(table: Table, where: str) -> Lag:
    return Lag(_enum(LagKind, _need(table, "kind", "V10", where), "V10", where), table.get("issue"), table.get("reason"))


def _project(table: Table, index: int) -> Project:
    project_id = _need(table, "id", "V1", f"project[{index}]")
    where = f"project {project_id}"
    lag = table.get("lag")
    return Project(
        id=project_id,
        repository=_need(table, "repository", "V1", where),
        content=tuple(_enum(ContentKind, kind, "V2", where) for kind in table.get("content", ())),
        carries_none_reason=table.get("carries_none_reason"),
        opens_on=tuple(_opening(opening, f"{where} opens_on[{n}]") for n, opening in enumerate(table.get("opens_on", ()))),
        steps=tuple(_step(step, where) for step in table.get("step", ())),
        statements=tuple(
            Statement(
                path=_need(statement, "path", "V9", f"{where} statement[{n}]"),
                pattern=_pattern(_need(statement, "pattern", "V12", f"{where} statement[{n}]"), "release", f"{where} statement[{n}]"),
                form=_enum(Form, _need(statement, "form", "V12", f"{where} statement[{n}]"), "V12", f"{where} statement[{n}]"),
                occurrences=int(statement.get("occurrences", 1)),
            )
            for n, statement in enumerate(table.get("statement", ()))
        ),
        generated=tuple(
            Generated(_need(g, "glob", "V8", f"{where} generated[{n}]"), _need(g, "by_step", "V8", f"{where} generated[{n}]"))
            for n, g in enumerate(table.get("generated", ()))
        ),
        pins=tuple(Pin(_need(pin, "glob", "V7", f"{where} pin[{n}]"), pin.get("reason", "")) for n, pin in enumerate(table.get("pin", ()))),
        ignores=tuple(_ignore(ignore, f"{where} ignore[{n}]") for n, ignore in enumerate(table.get("ignore", ()))),
        gate=table.get("gate", ""),
        workdir=table.get("workdir", "."),
        lag=None if lag is None else _lag(lag, where),
        caller=bool(table.get("caller", True)),
        checks=table.get("checks"),
        publishes=tuple(table.get("publishes", ())),
        merge_effect=table.get("merge_effect"),
        after_merge=table.get("after_merge"),
    )


def _roster(data: Table) -> Roster:
    header = _need(data, "roster", "V1", "file")
    return Roster(
        schema_version=str(_need(header, "schema_version", "V1", "[roster]")),
        upstream=_need(header, "upstream", "V1", "[roster]"),
        supported_at=_location(_need(header, "supported_at", "V9", "[roster]"), None, "[roster.supported_at]"),
        default_ignores=tuple(_ignore(ignore, f"[roster] ignore[{n}]") for n, ignore in enumerate(header.get("ignore", ()))),
        default_pins=tuple(Pin(_need(pin, "glob", "V7", "[roster] pin"), pin.get("reason", "")) for pin in header.get("pin", ())),
        projects=tuple(_project(project, index) for index, project in enumerate(data.get("project", ()))),
        verifications=tuple(_step(step, END_TO_END) for step in data.get("verification", ())),
    )


# ── For rule V11 ───────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class StringValue:
    """One string in the file, with the key path that holds it. Read only by rule V11."""

    where: str
    key: str
    text: str


def string_values(text: str) -> tuple[StringValue, ...]:
    found: list[StringValue] = []

    def walk(node: Any, where: str, key: str) -> None:
        if isinstance(node, str):
            found.append(StringValue(where, key, node))
        elif isinstance(node, Mapping):
            label = node.get("id") or node.get("glob") or node.get("path")
            inner = f"{where}/{key}[{label}]" if label else f"{where}/{key}"
            for name, value in node.items():
                walk(value, inner, name)
        elif isinstance(node, Sequence):
            for item in node:
                walk(item, where, key)

    for name, value in tomllib.loads(text).items():
        walk(value, "", name)
    return tuple(found)
