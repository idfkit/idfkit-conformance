"""Shared fixtures for the EnergyPlus adoption tests: a minimal roster and a fake organisation."""

from __future__ import annotations

from pathlib import Path

from sweep_consumers import Unreachable

FIXTURES = Path(__file__).resolve().parent / "fixtures"

VERIFICATIONS = "\n".join(
    f'[[verification]]\nid = "{name}"\ntitle = "{name}"\nclass = "automated"\n  [verification.verify]\n  kind = "statements-current"\n  project = "*"\n'
    for name in (
        "parses-and-validates",
        "round-trips",
        "migrates",
        "simulates",
        "documentation-published",
        "range-stated",
        "conformance-passes",
    )
)

HEADER = """
[roster]
schema_version = "1"
upstream = "up/EnergyPlus"

  [roster.supported_at]
  repository = "o/lib"
  path = "versions.py"
  pattern = '^\\s*\\((?P<release>\\d+, \\d+, \\d+)\\),'
  form = "tuple"

  [[roster.ignore]]
  glob = "**/*.lock"
  reason = "Lock files name third-party package versions."
"""

LIB = """
[[project]]
id = "lib"
repository = "o/lib"
content = ["schema", "prose"]
publishes = ["lib"]
gate = "make check"

  [[project.opens_on]]
  kind = "upstream-assets"
  roles = ["linux-x86_64"]

  [[project.step]]
  id = "registry"
  title = "Add the release to the registry"
  class = "automated"
  run = "echo {full}"
    [project.step.verify]
    kind = "registry-lists"
    path = "versions.py"
    pattern = '^\\s*\\((?P<release>\\d+, \\d+, \\d+)\\),'
    form = "tuple"

  [[project.step]]
  id = "sweep-row"
  title = "Add the sweep row"
  class = "reviewed"
  condition = "silent-effect"
  class_reason = "A wrong ceiling hides defects."
  inspect = "The findings count in the new row."
    [project.step.verify]
    kind = "all"
      [[project.step.verify.of]]
      kind = "registry-lists"
      path = "ci.yml"
      pattern = 'version: "(?P<release>[\\d.]+)"'
      form = "full"
      [[project.step.verify.of]]
      kind = "accepted"

  [[project.step]]
  id = "statements"
  title = "Rewrite statements"
  class = "automated"
    [project.step.verify]
    kind = "statements-current"

  [[project.step]]
  id = "gate"
  title = "Pass the gate"
  class = "automated"
    [project.step.verify]
    kind = "checks-green"

  [[project.statement]]
  path = "README.md"
  pattern = 'through v(?P<release>\\d+\\.\\d+)'
  form = "line"

  [[project.statement]]
  path = "README.md"
  pattern = '\\((?P<release>\\d+) versions\\)'
  form = "count"

  [[project.generated]]
  glob = "versions.py"
  by_step = "registry"

  [[project.generated]]
  glob = "ci.yml"
  by_step = "sweep-row"

  [[project.pin]]
  glob = "tests/**"
  reason = "Fixtures name the release whose behaviour they assert."
"""

APP = """
[[project]]
id = "app"
repository = "o/app"
content = ["library-level"]
gate = "npm test"

  [[project.opens_on]]
  kind = "published"
  index = "pypi"
  package = "lib"
  member = "lib/schemas/{directory}/schema.gz"

  [[project.step]]
  id = "level"
  title = "Library level carries the release"
  class = "automated"
    [project.step.verify]
    kind = "published-carries"
    index = "pypi"
    package = "lib"
    member = "lib/schemas/{directory}/schema.gz"
      [project.step.verify.through]
      path = "pyproject.toml"
      pattern = 'lib==(?P<level>[\\w.]+)'

  [[project.step]]
  id = "smoke"
  title = "Verify by hand"
  class = "manual"
  condition = "undecided-choice"
  class_reason = "No recorded rule decides it."
  inspect = "The model on disk."
  prepares = "The model written to disk."
    [project.step.verify]
    kind = "accepted"
"""

NONE = """
[[project]]
id = "auth"
repository = "o/auth"
carries_none_reason = "It reads and writes no model."
"""

ROSTER = HEADER + LIB + APP + NONE + VERIFICATIONS

VERSIONS = "ENERGYPLUS_VERSIONS = (\n    (25, 2, 0),\n    (26, 1, 0),\n)\n"
VERSIONS_NEXT = "ENERGYPLUS_VERSIONS = (\n    (25, 2, 0),\n    (26, 1, 0),\n    (26, 2, 0),\n)\n"


class FakeSource:
    """Repositories as tables of path to text. `None` is a repository that cannot be read."""

    def __init__(self, repos: dict[str, dict[str, str] | None]) -> None:
        self.repos = repos

    def _repo(self, repository: str) -> dict[str, str]:
        files = self.repos.get(repository)
        if files is None:
            raise Unreachable(f"{repository}: HTTP 404")
        return files

    def read(self, repository: str, path: str) -> str:
        files = self._repo(repository)
        if path not in files:
            raise FileNotFoundError(path)
        return files[path]

    def files(self, repository: str) -> list[str]:
        return sorted(self._repo(repository))

    def repositories(self) -> list[str]:
        return sorted(self.repos)


def organisation() -> dict[str, dict[str, str] | None]:
    """An organisation in which 26.1.0 is the newest supported release and nothing is stale."""
    return {
        "o/lib": {
            "versions.py": VERSIONS,
            "ci.yml": 'version: "25.2.0"\nversion: "26.1.0"\n',
            "README.md": "Supports v8.9 through v26.1 (2 versions).\n",
            "tests/test_a.py": 'assert parse("26.1.0")\n',
            "uv.lock": 'name = "cattrs"\nversion = "26.1.0"\n',
        },
        "o/app": {"pyproject.toml": 'dependencies = ["lib==1.0.0"]\n'},
    }


# ── Fakes for the evaluation ───────────────────────────────────────────────────────────────────────

from dataclasses import dataclass, field  # noqa: E402
from datetime import datetime, timezone  # noqa: E402

from energyplus.evaluate import CheckOutcome, CheckState, PullRequest, Run  # noqa: E402
from energyplus.roster import Release  # noqa: E402
from energyplus.upstream import FixtureReleases, find, releases  # noqa: E402

NOW = datetime(2026, 10, 14, 5, 23, tzinfo=timezone.utc)
NEXT = Release(26, 2, 0)
UPSTREAM = find(releases(FixtureReleases(FIXTURES / "releases-2026-10-13.json", FIXTURES / "tag-commits-2026-10-13.json")), NEXT)
GREEN = CheckState(CheckOutcome.SUCCESS, "https://ci/green")
RED = CheckState(CheckOutcome.FAILURE, "https://ci/red", ("lint",))


@dataclass
class FakeHub:
    """GitHub facts as tables. A branch is a table of path to text, keyed by repository and head."""

    branches: dict[tuple[str, str], dict[str, str]] = field(default_factory=dict)
    states: dict[tuple[str, str | None], CheckState] = field(default_factory=dict)
    pulls: dict[str, PullRequest] = field(default_factory=dict)
    tagged: dict[str, tuple[str, ...]] = field(default_factory=dict)
    runs: dict[str, Run] = field(default_factory=dict)
    pages: dict[str, str] = field(default_factory=dict)

    def read(self, repository: str, path: str, ref: str) -> str:
        files = self.branches.get((repository, ref), {})
        if path not in files:
            raise FileNotFoundError(path)
        return files[path]

    def exists(self, repository: str, path: str, ref: str) -> bool:
        return any(name == path or name.startswith(path + "/") for name in self.branches.get((repository, ref), {}))

    def checks(self, repository: str, ref: str | None, workflow: str | None) -> CheckState:
        return self.states.get((repository, ref), GREEN)

    def pull_request(self, repository: str, branch: str) -> PullRequest | None:
        return self.pulls.get(repository)

    def tags(self, repository: str) -> tuple[str, ...]:
        return self.tagged.get(repository, ())

    def run(self, repository: str, workflow: str, release: Release) -> Run | None:
        return self.runs.get(repository)

    def get(self, url: str) -> str | None:
        return self.pages.get(url)


@dataclass
class FakePackages:
    """Published versions in publication order, each with the members its artifact holds."""

    published: dict[tuple[str, str], dict[str, tuple[str, ...]]] = field(default_factory=dict)
    pins: dict[tuple[str, str], str] = field(default_factory=dict)

    def versions(self, index: str, package: str) -> tuple[str, ...]:
        return tuple(self.published.get((index, package), {}))

    def newest(self, index: str, package: str) -> str | None:
        found = self.versions(index, package)
        return found[-1] if found else None

    def has_member(self, index: str, package: str, version: str, member: str) -> bool:
        return member in self.published.get((index, package), {}).get(version, ())

    def requires(self, index: str, package: str, version: str, dependency: str) -> str | None:
        return self.pins.get((package, version))


OLD_SCHEMA = "lib/schemas/V26-1-0/schema.gz"
NEW_SCHEMA = "lib/schemas/V26-2-0/schema.gz"


def adopted() -> dict[str, dict[str, str] | None]:
    """The organisation after 26.2.0 is adopted everywhere, with nothing left to do."""
    return {
        "o/lib": {
            "versions.py": VERSIONS_NEXT,
            "ci.yml": 'version: "25.2.0"\nversion: "26.1.0"\nversion: "26.2.0"\n',
            "README.md": "Supports v8.9 through v26.2 (3 versions).\n",
            "tests/test_a.py": 'assert parse("26.1.0")\n',
        },
        "o/app": {"pyproject.toml": 'dependencies = ["lib==1.1.0"]\n'},
    }


def packages(*, released: bool) -> FakePackages:
    versions = {"1.0.0": (OLD_SCHEMA,)}
    if released:
        versions["1.1.0"] = (OLD_SCHEMA, NEW_SCHEMA)
    return FakePackages({("pypi", "lib"): versions})
