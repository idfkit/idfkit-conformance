"""The register's schema check must be able to fail, on every rule it claims (T026).

A schema check that never rejects anything is indistinguishable from no check, so every invariant
here is exercised by a crafted violation, starting from a register that passes. The last test runs
the real file, which is the one that ships.
"""

from __future__ import annotations

import copy
import json
import tomllib
from pathlib import Path
from typing import Any

import pytest

from check_consumer_self import check_self
from check_consumers import check_register
from consumers import (
    Declaration,
    LocatorError,
    Register,
    agreement_key,
    detect_text,
    detected_entry_point,
    level_of,
    normalize,
    resolve,
)

ROOT = Path(__file__).resolve().parent.parent
PARITY = {"write", "parse"}


def _valid() -> dict[str, Any]:
    return {
        "register": {"schema_version": "1", "libraries": {"python": "idfkit/idfkit", "javascript": "idfkit/idfkit-js"}},
        "consumer": [
            {
                "id": "server",
                "repository": "idfkit/server",
                "role": "builds",
                "libraries": [
                    {
                        "library": "python",
                        "entry_point": "pypi",
                        "means": "direct",
                        "declared_at": [{"path": "pyproject.toml", "locator": "project.dependencies[idfkit]", "form": "exact"}],
                    }
                ],
                "rehearsal": [{"library": "python", "install": "uv-wheel", "typecheck": "uv run pyright", "test": "uv run pytest"}],
                "writes_models": True,
                "preserves_formatting": "no",
                "formatting_note": "Reformats on save.",
            },
            {
                "id": "editor",
                "repository": "idfkit/editor",
                "role": "builds",
                "libraries": [
                    {
                        "library": "javascript",
                        "entry_point": "scoped",
                        "means": "direct",
                        "declared_at": [{"path": "package.json", "locator": 'dependencies["@idfkit/core"]', "form": "exact"}],
                    }
                ],
                "rehearsal": [
                    {"library": "javascript", "install": "npm-tarball", "typecheck": "npx tsc --noEmit", "test": "npm test"}
                ],
                "out_of_scope": [
                    {"package": "@idfkit/engine", "path": "package.json", "locator": 'dependencies["@idfkit/engine"]', "form": "exact", "note": "Recorded."}
                ],
            },
            {
                "id": "plugin",
                "repository": "idfkit/plugin",
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
                "rehearsal": [{"library": "python", "install": "through-provider"}],
            },
            {"id": "site", "repository": "idfkit/site", "role": "teaches"},
        ],
        "surface": [
            {"host": "docs.example", "status": "serving", "published_by": "site", "states_level": True},
            {"host": "old.example", "status": "retired", "published_by": "idfkit/idfkit", "redirects_to": "docs.example"},
        ],
    }


def _rules(data: dict[str, Any]) -> set[str]:
    return {finding.rule for finding in check_register(data, PARITY)}


def test_the_valid_fixture_passes() -> None:
    assert check_register(_valid(), PARITY) == []


def _consumer(data: dict[str, Any], consumer_id: str) -> dict[str, Any]:
    return next(c for c in data["consumer"] if c["id"] == consumer_id)


def test_invariant_1_duplicate_id() -> None:
    data = _valid()
    data["consumer"].append(copy.deepcopy(_consumer(data, "site")))
    assert "unique-id" in _rules(data)


def test_invariant_1_unresolved_depends_on() -> None:
    data = _valid()
    _consumer(data, "plugin")["depends_on"] = ["nobody"]
    assert "depends-on" in _rules(data)


def test_invariant_2_cycle() -> None:
    data = _valid()
    _consumer(data, "server")["depends_on"] = ["plugin"]
    assert "acyclic" in _rules(data)


def test_invariant_3_through_consumer_with_a_declaration() -> None:
    data = _valid()
    _consumer(data, "plugin")["libraries"][0]["means"] = "through-consumer"
    assert "through-consumer" in _rules(data)


def test_invariant_3_through_consumer_with_no_provider() -> None:
    data = _valid()
    resolution = _consumer(data, "server")["libraries"][0]
    resolution["means"] = "through-consumer"
    resolution["declared_at"] = []
    assert "through-consumer" in _rules(data)


def test_invariant_4_direct_with_no_declaration() -> None:
    data = _valid()
    _consumer(data, "server")["libraries"][0]["declared_at"] = []
    assert "declared-at" in _rules(data)


@pytest.mark.parametrize(
    "lag",
    [
        {"kind": "not-yet"},
        {"kind": "not-yet", "issue": "#12"},
        {"kind": "not-yet", "issue": "https://example/1", "reason": "and a reason"},
        {"kind": "deliberate"},
        {"kind": "deliberate", "reason": "policy", "issue": "https://example/1"},
        {"kind": "someday"},
    ],
)
def test_invariant_5_lag_evidence(lag: dict[str, str]) -> None:
    data = _valid()
    _consumer(data, "server")["libraries"][0]["lag"] = lag
    assert "lag" in _rules(data)


@pytest.mark.parametrize(
    "reason", ["uses the scoped packages", "its entry point is the shared name", "stays on @idfkit/core"]
)
def test_invariant_6_an_entry_point_is_never_a_lag(reason: str) -> None:
    data = _valid()
    _consumer(data, "editor")["libraries"][0]["lag"] = {"kind": "deliberate", "reason": reason}
    assert "entry-point-lag" in _rules(data)


def test_a_reason_states_a_policy_never_a_version() -> None:
    data = _valid()
    _consumer(data, "server")["libraries"][0]["lag"] = {"kind": "deliberate", "reason": "stays on 0.12.1"}
    assert "lag" in _rules(data)


def test_invariant_7_unknown_parity_id() -> None:
    data = _valid()
    _consumer(data, "server")["libraries"][0]["lag"] = {"kind": "deliberate", "reason": "waits", "parity_id": "nope"}
    data["surface"][0]["parity_id"] = "also-nope"
    findings = [f for f in check_register(data, PARITY) if f.rule == "parity-id"]
    assert len(findings) == 2


def test_invariant_8_no_rehearsal() -> None:
    data = _valid()
    del _consumer(data, "server")["rehearsal"]
    assert "rehearsal" in _rules(data)


def test_invariant_8_rehearsal_without_typecheck() -> None:
    data = _valid()
    del _consumer(data, "server")["rehearsal"][0]["typecheck"]
    assert "rehearsal" in _rules(data)


def test_invariant_8_through_provider_names_no_commands() -> None:
    data = _valid()
    _consumer(data, "plugin")["rehearsal"][0]["typecheck"] = "true"
    assert "rehearsal" in _rules(data)


def test_invariant_8_no_test_needs_a_reason() -> None:
    data = _valid()
    del _consumer(data, "editor")["rehearsal"][0]["test"]
    assert "rehearsal" in _rules(data)
    _consumer(data, "editor")["rehearsal"][0]["no_test_reason"] = "There is no suite; the build is the check."
    assert "rehearsal" not in _rules(data)


@pytest.mark.parametrize(
    ("index", "change"),
    [
        (0, {"states_level": False}),
        (1, {"redirects_to": None}),
        (0, {"status": "paused"}),
    ],
)
def test_invariant_9_surface_states(index: int, change: dict[str, Any]) -> None:
    data = _valid()
    data["surface"][index].update({k: v for k, v in change.items() if v is not None})
    for key in [k for k, v in change.items() if v is None]:
        del data["surface"][index][key]
    assert "surface" in _rules(data)


def test_invariant_10_published_by() -> None:
    data = _valid()
    data["surface"][1]["published_by"] = "not a repository"
    assert "surface" in _rules(data)


def test_invariant_10_a_library_repository_is_not_an_error() -> None:
    # The two retired hosts are published by the libraries, and neither library is a consumer.
    assert "surface" not in _rules(_valid())


def test_fr041_out_of_scope_as_a_declaration_is_rejected() -> None:
    data = _valid()
    _consumer(data, "editor")["libraries"][0]["declared_at"].append(
        {"path": "package.json", "locator": 'dependencies["@idfkit/engine"]', "form": "exact", "package": "@idfkit/engine"}
    )
    assert "out-of-scope" in _rules(data)


def test_fr041_a_gate_configured_on_an_out_of_scope_entry_is_rejected() -> None:
    data = _valid()
    _consumer(data, "editor")["out_of_scope"][0]["lag"] = {"kind": "not-yet", "issue": "https://example/1"}
    assert "out-of-scope" in _rules(data)


def test_fr041_a_governed_package_cannot_hide_under_out_of_scope() -> None:
    data = _valid()
    _consumer(data, "editor")["out_of_scope"][0]["package"] = "@idfkit/core"
    assert "out-of-scope" in _rules(data)


def test_fr018_a_delivery_path_names_its_provider() -> None:
    data = _valid()
    del _consumer(data, "plugin")["libraries"][0]["declared_at"][0]["via"]
    assert "via" in _rules(data)


def test_fr015_an_unpinned_delivery_path_must_be_stated() -> None:
    data = _valid()
    _consumer(data, "plugin")["libraries"][0]["declared_at"][0]["form"] = "range"
    assert "delivery-exact" in _rules(data)
    _consumer(data, "plugin")["libraries"][0]["lag"] = {"kind": "not-yet", "issue": "https://example/1"}
    assert "delivery-exact" not in _rules(data)


def test_formatting_answer_rules() -> None:
    data = _valid()
    _consumer(data, "server")["preserves_formatting"] = "not-applicable"
    del _consumer(data, "server")["formatting_note"]
    assert "formatting" in _rules(data)


# ── The shared module ────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "package", "level"),
    [
        ("idfkit==0.15.0", "idfkit", "0.15.0"),
        ("idfkit-mcp@0.9.3", "idfkit-mcp", "0.9.3"),
        ("idfkit-lsp==0.1.0", "idfkit-lsp", "0.1.0"),
        ("0.3.0-rc.2", None, "0.3.0-rc.2"),
        ("idfkit-lsp", "idfkit-lsp", None),
        ("^0.1.0", None, None),
        ("idfkit>=0.12", "idfkit", None),
    ],
)
def test_level_of(raw: str, package: str | None, level: str | None) -> None:
    assert level_of(raw, package).level == level


def test_normalize_spells_one_release_one_way() -> None:
    assert normalize("1.0.0-rc.4") == normalize("1.0.0rc4") == "1.0.0rc4"
    assert normalize("0.3.0-rc.3") == "0.3.0rc3"


def test_resolve_every_locator_form() -> None:
    pyproject = '[project]\ndependencies = ["idfkit==0.15.0", "mcp>=1"]\n[tool.idfkit.library]\nlevel = "0.15.0"\n'
    assert resolve("pyproject.toml", pyproject, "project.dependencies[idfkit]") == "idfkit==0.15.0"
    assert resolve("pyproject.toml", pyproject, "tool.idfkit.library.level") == "0.15.0"
    package = json.dumps({"dependencies": {"@idfkit/core": "0.2.0"}})
    assert resolve("package.json", package, 'dependencies["@idfkit/core"]') == "0.2.0"
    levels = json.dumps({"libraries": [{"name": "pygls", "level": "2"}, {"name": "idfkit", "level": "1.0.0rc1"}]})
    assert resolve("levels.json", levels, "libraries[name=idfkit].level") == "1.0.0rc1"
    assert resolve("Dockerfile", "RUN pip install idfkit-mcp==0.9.3\n", r"regex:idfkit-mcp==(?P<level>\S+)") == "0.9.3"
    with pytest.raises(LocatorError):
        resolve("package.json", package, 'dependencies["@idfkit/schemas"]')


def test_the_second_language_is_one_release_for_agreement() -> None:
    core = Declaration("package.json", 'dependencies["@idfkit/core"]', "exact")
    facade = Declaration("package.json", 'dependencies["idfkit"]', "exact")
    assert agreement_key(core, "javascript") == agreement_key(facade, "javascript")
    mcp = Declaration(".mcp.json", "mcpServers.idfkit.args[0]", "exact", package="idfkit-mcp")
    lsp = Declaration(".lsp.json", "idfkit.args[1]", "exact", package="idfkit-lsp")
    assert agreement_key(mcp, "python") != agreement_key(lsp, "python")


def test_an_optional_peer_does_not_decide_the_door() -> None:
    manifest = json.dumps({"dependencies": {"@idfkit/core": "0.2.0"}, "peerDependencies": {"idfkit": "0.0.0"}})
    assert detected_entry_point(detect_text("model-server/package.json", manifest)) == "scoped"
    assert detected_entry_point(detect_text("package.json", json.dumps({"dependencies": {"idfkit": "1.0.0"}}))) == "shared-name"


# ── The self-check ───────────────────────────────────────────────────────────────────────────────


def _checkout(tmp_path: Path, files: dict[str, str]) -> Path:
    for name, text in files.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(text)
    return tmp_path


def _self(tmp_path: Path, files: dict[str, str], repository: str = "idfkit/server") -> set[int]:
    register = Register.from_toml(_valid())
    return {f.rule for f in check_self(register, repository, _checkout(tmp_path, files))}


def test_self_check_passes_a_matching_consumer(tmp_path: Path) -> None:
    assert _self(tmp_path, {"pyproject.toml": '[project]\ndependencies = ["idfkit==0.15.0"]\n'}) == set()


def test_self_rule_1_unregistered(tmp_path: Path) -> None:
    assert _self(tmp_path, {}, repository="idfkit/stranger") == {1}


def test_self_rule_2_a_declaration_that_moved(tmp_path: Path) -> None:
    # The level now lives in a requirements file the register does not point at.
    assert 2 in _self(tmp_path, {"pyproject.toml": '[project]\ndependencies = ["mcp"]\n'})


def test_self_rule_3_two_declarations_disagree(tmp_path: Path) -> None:
    data = _valid()
    _consumer(data, "server")["libraries"][0]["declared_at"].append(
        {"path": "pyproject.toml", "locator": "tool.idfkit.library.level", "form": "exact"}
    )
    root = _checkout(
        tmp_path, {"pyproject.toml": '[project]\ndependencies = ["idfkit==0.15.0"]\n[tool.idfkit.library]\nlevel = "0.14.0"\n'}
    )
    assert 3 in {f.rule for f in check_self(Register.from_toml(data), "idfkit/server", root)}


def test_self_rule_3_agrees_across_spellings(tmp_path: Path) -> None:
    data = _valid()
    _consumer(data, "server")["libraries"][0]["declared_at"].append(
        {"path": "pyproject.toml", "locator": "tool.idfkit.library.level", "form": "exact"}
    )
    root = _checkout(
        tmp_path,
        {"pyproject.toml": '[project]\ndependencies = ["idfkit==1.0.0rc4"]\n[tool.idfkit.library]\nlevel = "1.0.0-rc.4"\n'},
    )
    assert check_self(Register.from_toml(data), "idfkit/server", root) == []


def test_self_rule_3_half_adopted_across_doors(tmp_path: Path) -> None:
    data = _valid()
    _consumer(data, "editor")["libraries"][0]["declared_at"].append(
        {"path": "package.json", "locator": 'dependencies["@idfkit/schemas"]', "form": "exact"}
    )
    manifest = json.dumps({"dependencies": {"@idfkit/core": "0.3.0", "@idfkit/schemas": "0.2.0", "@idfkit/engine": "1"}})
    root = _checkout(tmp_path, {"package.json": manifest})
    assert 3 in {f.rule for f in check_self(Register.from_toml(data), "idfkit/editor", root)}


def test_self_rule_4_a_new_declaration_the_register_does_not_name(tmp_path: Path) -> None:
    files = {
        "pyproject.toml": '[project]\ndependencies = ["idfkit==0.15.0"]\n',
        "sub/pyproject.toml": '[project]\ndependencies = ["idfkit==0.15.0"]\n',
    }
    assert 4 in _self(tmp_path, files)


def test_self_rule_4_the_door_changed(tmp_path: Path) -> None:
    manifest = json.dumps({"dependencies": {"idfkit": "0.3.0"}, "devDependencies": {"@idfkit/engine": "1"}})
    data = _valid()
    _consumer(data, "editor")["libraries"][0]["declared_at"] = [
        {"path": "package.json", "locator": 'dependencies["idfkit"]', "form": "exact"}
    ]
    root = _checkout(tmp_path, {"package.json": manifest})
    findings = check_self(Register.from_toml(data), "idfkit/editor", root)
    assert any(f.rule == 4 and "entry point" in f.message for f in findings)


def test_self_rule_4_an_unrecorded_out_of_scope_dependency(tmp_path: Path) -> None:
    data = _valid()
    _consumer(data, "editor")["out_of_scope"] = []
    root = _checkout(tmp_path, {"package.json": json.dumps({"dependencies": {"@idfkit/core": "0.3.0", "@idfkit/engine": "1"}})})
    assert 4 in {f.rule for f in check_self(Register.from_toml(data), "idfkit/editor", root)}


def test_self_check_does_not_care_about_the_level(tmp_path: Path) -> None:
    # R2: a bump changes nothing the register holds, so it must not fail anything here.
    assert _self(tmp_path, {"pyproject.toml": '[project]\ndependencies = ["idfkit==9.9.9"]\n'}) == set()


# ── The file that ships ──────────────────────────────────────────────────────────────────────────


def test_the_shipped_register_is_well_formed() -> None:
    with (ROOT / "governance" / "consumers.toml").open("rb") as handle:
        data = tomllib.load(handle)
    with (ROOT / "governance" / "parity.toml").open("rb") as handle:
        parity = {c["id"] for c in tomllib.load(handle)["capability"]}
    assert check_register(data, parity) == []
    register = Register.from_toml(data)
    assert len(register.consumers) == 9
    assert {s.host for s in register.surfaces} == {"developers.idfkit.com", "py.idfkit.com", "js.idfkit.com"}
