"""The sweep, against a fake organisation: every case whose failure would read as an all-clear (T032, T103)."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from consumers import Register
from sweep_consumers import Registry, Unreachable, exit_code, scoped_level, sweep


class FakeSource:
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


class FakeRegistry:
    def __init__(self, facade: dict[str, Any] | None = None) -> None:
        self.facade = facade or {}

    def pypi(self, package: str) -> Mapping[str, Any]:
        if package == "idfkit":
            return {"info": {"version": "0.15.0"}, "releases": {"0.15.0": [1], "1.0.0rc4": [1]}}
        if package == "idfkit-mcp/0.9.3":
            return {"info": {"requires_dist": ["idfkit==0.12.1", "mcp>=1.2.0"]}}
        return {}

    def npm(self, package: str) -> Mapping[str, Any]:
        if package == "@idfkit/core":
            return {"dist-tags": {"latest": "0.2.0", "next": "0.3.0-rc.3"}, "versions": {"0.2.0": {}, "0.3.0-rc.3": {}}}
        if package == "idfkit":
            return self.facade
        return {}


def _register() -> Register:
    return Register.from_toml(
        {
            "register": {"schema_version": "1", "libraries": {"python": "o/idfkit", "javascript": "o/idfkit-js"}},
            "consumer": [
                {
                    "id": "ranged",
                    "repository": "o/ranged",
                    "role": "builds",
                    "libraries": [
                        {
                            "library": "javascript",
                            "entry_point": "scoped",
                            "means": "direct",
                            "declared_at": [{"path": "package.json", "locator": 'dependencies["@idfkit/core"]', "form": "range"}],
                            "lag": {"kind": "not-yet", "issue": "https://example/1"},
                        }
                    ],
                },
                {
                    "id": "facade",
                    "repository": "o/facade",
                    "role": "builds",
                    "libraries": [
                        {
                            "library": "javascript",
                            "entry_point": "shared-name",
                            "means": "direct",
                            "declared_at": [{"path": "package.json", "locator": 'dependencies["idfkit"]', "form": "exact"}],
                        }
                    ],
                },
                {"id": "server", "repository": "o/server", "role": "builds", "libraries": [
                    {"library": "python", "entry_point": "pypi", "means": "direct",
                     "declared_at": [{"path": "pyproject.toml", "locator": "project.dependencies[idfkit]", "form": "exact"}]}]},
                {
                    "id": "hosted",
                    "repository": "o/hosted",
                    "role": "delivers",
                    "depends_on": ["server"],
                    "libraries": [{"library": "python", "entry_point": "pypi", "means": "through-consumer"}],
                },
                {
                    "id": "plugin",
                    "repository": "o/plugin",
                    "role": "delivers",
                    "depends_on": ["server"],
                    "libraries": [
                        {
                            "library": "python",
                            "entry_point": "pypi",
                            "means": "runtime-fetch",
                            "declared_at": [
                                {"path": ".mcp.json", "locator": "mcpServers.idfkit.args[0]", "form": "exact", "package": "idfkit-mcp", "via": "server"}
                            ],
                        }
                    ],
                },
            ],
        }
    )


def _org(**extra: dict[str, str] | None) -> FakeSource:
    repos: dict[str, dict[str, str] | None] = {
        "o/ranged": {
            "package.json": json.dumps({"dependencies": {"@idfkit/core": "^0.2.0"}}),
            "package-lock.json": json.dumps({"packages": {"node_modules/@idfkit/core": {"version": "0.2.0"}}}),
        },
        "o/facade": {"package.json": json.dumps({"dependencies": {"idfkit": "1.0.0"}})},
        "o/server": {"pyproject.toml": '[project]\ndependencies = ["idfkit==1.0.0rc4"]\n'},
        "o/hosted": {},
        "o/plugin": {".mcp.json": json.dumps({"mcpServers": {"idfkit": {"command": "uvx", "args": ["idfkit-mcp@0.9.3"]}}})},
        "o/idfkit": {"pyproject.toml": '[project]\nname = "idfkit"\n'},
    }
    repos.update(extra)
    return FakeSource(repos)


def _standing(report: Any, consumer: str) -> Any:
    return next(s for s in report.standings if s.consumer == consumer)


def test_a_range_is_read_from_the_lockfile() -> None:
    report = sweep(_register(), _org(), FakeRegistry(), "test")
    row = next(o for o in report.observed if o.consumer == "ranged")
    assert row.level is None and row.locked == "0.2.0"
    assert _standing(report, "ranged").level == "0.2.0"


def test_a_through_consumer_has_no_declaration_and_is_not_unexplained() -> None:
    report = sweep(_register(), _org(), FakeRegistry(), "test")
    assert not [o for o in report.observed if o.consumer == "hosted"]
    assert not any("hosted" in u for u in report.unexplained)


def test_a_delivery_path_reports_what_it_delivers() -> None:
    report = sweep(_register(), _org(), FakeRegistry(), "test")
    row = next(o for o in report.observed if o.consumer == "plugin")
    assert (row.level, row.delivers, row.via) == ("0.9.3", "0.12.1", "server")
    assert _standing(report, "plugin").standing == "via-provider"


def test_current_is_the_newest_release_of_any_kind() -> None:
    report = sweep(_register(), _org(), FakeRegistry(), "test")
    assert _standing(report, "server").standing == "current"


def test_behind_with_no_lag_is_unexplained() -> None:
    org = _org(**{"o/server": {"pyproject.toml": '[project]\ndependencies = ["idfkit==0.15.0"]\n'}})
    report = sweep(_register(), org, FakeRegistry(), "test")
    assert any(u.startswith("server") for u in report.unexplained)


def test_an_unreachable_repository_is_never_a_stranger_and_never_clean() -> None:
    report = sweep(_register(), _org(**{"o/private": None}), FakeRegistry(), "test")
    assert [u["repository"] for u in report.unreachable] == ["o/private"]
    assert report.strangers == []
    assert exit_code(report) == 2


def test_a_stranger_is_found_and_fails_the_sweep() -> None:
    org = _org(**{"o/new-tool": {"tools/pyproject.toml": '[project]\ndependencies = ["idfkit>=0.15"]\n'}})
    report = sweep(_register(), org, FakeRegistry(), "test")
    assert [s["repository"] for s in report.strangers] == ["o/new-tool"]
    assert exit_code(report) == 1


def test_a_library_repository_is_not_a_stranger() -> None:
    report = sweep(_register(), _org(), FakeRegistry(), "test")
    assert all(s["repository"] != "o/idfkit" for s in report.strangers)


def test_an_out_of_scope_dependency_alone_does_not_make_a_stranger() -> None:
    org = _org(**{"o/engine-demo": {"package.json": json.dumps({"dependencies": {"@idfkit/engine": "1.0.0"}})}})
    assert sweep(_register(), org, FakeRegistry(), "test").strangers == []


def test_both_doors_compare_through_the_published_facade() -> None:
    # SC-013: whichever door each came through, a maintainer can tell two consumers are level.
    facade = {"versions": {"1.0.0": {"dependencies": {"@idfkit/core": "0.3.0-rc.3", "@idfkit/schemas": "0.3.0-rc.3"}}}}
    report = sweep(_register(), _org(**{"o/ranged": {"package.json": json.dumps({"dependencies": {"@idfkit/core": "0.3.0-rc.3"}})}}), FakeRegistry(facade), "test")
    rows = {r.consumer: r for r in report.entry_points}
    assert rows["facade"].scoped_level == rows["ranged"].scoped_level == "0.3.0rc3"
    assert report.facade_mapping == {"1.0.0": {"@idfkit/core": "0.3.0-rc.3", "@idfkit/schemas": "0.3.0-rc.3"}}


def test_the_facade_mapping_is_empty_while_the_shared_name_is_unpublished() -> None:
    assert scoped_level("1.0.0", "shared-name", {}) is None
    assert scoped_level("0.3.0-rc.3", "scoped", {}) == "0.3.0rc3"


def test_a_scoped_consumer_is_never_lagging_for_its_door() -> None:
    report = sweep(_register(), _org(), FakeRegistry(), "test")
    assert all("door" not in u and "scoped" not in u for u in report.unexplained)


def test_registry_protocol_is_satisfied() -> None:
    registry: Registry = FakeRegistry()
    assert registry.npm("@idfkit/core")
