"""Adoption order: waves from the register, providers first, languages never waiting on each other."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from adoption import check_order, half_adopted, plan_javascript, waves
from consumers import Register

ROOT = Path(__file__).resolve().parent.parent


def _real() -> Register:
    return Register.load(ROOT / "governance" / "consumers.toml")


def _ids(register: Register, library: str) -> list[list[str]]:
    return [[t.consumer for t in wave] for wave in waves(register, library)]


def test_first_language_waves_put_the_delivery_paths_second() -> None:
    assert _ids(_real(), "python") == [
        ["idfkit-developers", "idfkit-docs", "idfkit-lsp", "idfkit-mcp"],
        ["idfkit-mcp-deployment", "idfkit-plugin"],
    ]


def test_second_language_waves_ignore_first_language_edges() -> None:
    # idfkit-plugin depends on idfkit-lsp, which resolves both languages. The plugin resolves only
    # the first, so the second language's adoption neither includes it nor waits for it (FR-021).
    assert _ids(_real(), "javascript") == [["envelop", "idfkit-lsp", "idfkit-shoebox"]]


def test_every_target_dispatches_the_workflow_for_its_library() -> None:
    for library, workflow in (("python", "bump-idfkit.yml"), ("javascript", "bump-idfkit-js.yml")):
        assert {t.workflow for wave in waves(_real(), library) for t in wave} == {workflow}


def test_an_adoption_before_its_provider_is_rejected_naming_the_provider() -> None:
    rejections = check_order(_real(), "idfkit-plugin", "python", "1.0.0rc4", lambda package, level: None)
    assert rejections and all("idfkit-mcp" in r or "idfkit-lsp" in r for r in rejections)
    assert any("provider idfkit-mcp" in r for r in rejections)


def test_an_adoption_after_its_provider_proceeds() -> None:
    released = {("idfkit-mcp", "1.0.0rc4"): "1.0.0", ("idfkit-lsp", "1.0.0rc4"): "0.2.0"}
    assert check_order(_real(), "idfkit-plugin", "python", "1.0.0rc4", lambda p, lv: released.get((p, lv))) == []


def test_a_direct_consumer_never_waits() -> None:
    assert check_order(_real(), "idfkit-mcp", "python", "1.0.0rc4", lambda package, level: None) == []


def test_the_second_language_proceeds_while_the_first_is_mid_adoption() -> None:
    # No first-language provider has released anything; the second language's consumers are unaffected.
    for consumer in ("envelop", "idfkit-shoebox", "idfkit-lsp"):
        assert check_order(_real(), consumer, "javascript", "0.3.0", lambda package, level: None) == []


def test_one_release_moves_both_doors_together() -> None:
    manifest = {
        "dependencies": {"@idfkit/core": "0.2.0", "@idfkit/language": "0.2.0", "idfkit": "0.2.0", "react": "18"},
        "peerDependencies": {"idfkit": "0.0.0"},
    }
    planned = plan_javascript(manifest, "0.3.0")
    assert planned["dependencies"] == {"@idfkit/core": "0.3.0", "@idfkit/language": "0.3.0", "idfkit": "0.3.0", "react": "18"}
    assert planned["peerDependencies"] == {"idfkit": "0.0.0"}
    assert half_adopted(planned) == []


def test_a_release_cannot_be_half_adopted() -> None:
    assert half_adopted({"dependencies": {"@idfkit/core": "0.3.0", "idfkit": "0.2.0"}}) == ["@idfkit/core@0.3.0", "idfkit@0.2.0"]


def test_waves_refuse_a_cycle() -> None:
    data = {
        "register": {"schema_version": "1", "libraries": {"python": "o/a", "javascript": "o/b"}},
        "consumer": [
            {"id": "a", "repository": "o/x", "role": "builds", "depends_on": ["b"], "libraries": [{"library": "python", "entry_point": "pypi", "means": "through-consumer"}]},
            {"id": "b", "repository": "o/y", "role": "builds", "depends_on": ["a"], "libraries": [{"library": "python", "entry_point": "pypi", "means": "through-consumer"}]},
        ],
    }
    with pytest.raises(ValueError, match="cycle"):
        waves(Register.from_toml(data), "python")


def test_plan_cli_round_trip(tmp_path: Path) -> None:
    from adoption import main

    manifest = tmp_path / "package.json"
    manifest.write_text(json.dumps({"dependencies": {"@idfkit/core": "0.2.0", "@idfkit/schemas": "0.2.0"}}))
    assert main(["plan-js", "--manifest", str(manifest), "--version", "0.3.0-rc.3"]) == 0
    assert json.loads(manifest.read_text())["dependencies"] == {"@idfkit/core": "0.3.0-rc.3", "@idfkit/schemas": "0.3.0-rc.3"}
