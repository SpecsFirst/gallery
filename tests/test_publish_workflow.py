"""Structure of .github/workflows/publish.yml, as far as a parse can show it.

Actions cannot run here: the workflow is checked by actionlint and by these tests.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "publish.yml"
TEXT = WORKFLOW.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def wf() -> dict[str, Any]:
    document = yaml.safe_load(TEXT)
    assert isinstance(document, dict)
    return document


@pytest.fixture(scope="module")
def build(wf: dict[str, Any]) -> dict[str, Any]:
    return wf["jobs"]["build"]


@pytest.fixture(scope="module")
def deploy(wf: dict[str, Any]) -> dict[str, Any]:
    return wf["jobs"]["deploy"]


def step_index(steps: list[dict[str, Any]], needle: str) -> int:
    matches = [
        i
        for i, step in enumerate(steps)
        if needle in step.get("uses", "") or needle in step.get("run", "")
    ]
    assert len(matches) == 1, f"{needle!r} must appear in exactly one step"
    return matches[0]


def test_triggers_are_exactly_push_schedule_dispatch(wf: dict[str, Any]) -> None:
    triggers = wf[True]  # PyYAML reads the key `on` as True
    assert set(triggers) == {"push", "schedule", "workflow_dispatch"}
    assert triggers["push"] == {"branches": ["main"]}
    assert triggers["schedule"] == [{"cron": "17 */6 * * *"}]


def test_top_level_permissions_are_empty(wf: dict[str, Any]) -> None:
    assert wf["permissions"] == {}


def test_concurrency_serializes_pages_runs(wf: dict[str, Any]) -> None:
    assert wf["concurrency"] == {"group": "pages", "cancel-in-progress": False}


def test_jobs(wf: dict[str, Any]) -> None:
    assert set(wf["jobs"]) == {"build", "deploy"}


def test_build_permissions_and_timeout(build: dict[str, Any]) -> None:
    assert build["permissions"] == {"contents": "read", "discussions": "read"}
    assert build["timeout-minutes"] == 30


def test_checkout_does_not_persist_credentials(build: dict[str, Any]) -> None:
    first = build["steps"][0]
    assert first["uses"] == "actions/checkout@v4"
    assert first["with"]["persist-credentials"] is False


def test_build_steps_in_order(build: dict[str, Any]) -> None:
    steps = build["steps"]
    order = [
        step_index(steps, "actions/checkout@v4"),
        step_index(steps, "astral-sh/setup-uv@v5"),
        step_index(steps, "actions/setup-python@v5"),
        step_index(steps, "uv sync --frozen"),
        step_index(steps, "gallery.detect"),
        step_index(steps, "jq -e"),
        step_index(steps, "gallery.votes"),
        step_index(steps, "gallery.build"),
        step_index(steps, "actions/upload-pages-artifact@v5"),
    ]
    assert order == list(range(9))
    assert len(steps) == 9


def test_detect_step_has_no_token(build: dict[str, Any]) -> None:
    step = build["steps"][step_index(build["steps"], "gallery.detect")]
    assert "env" not in step
    assert "token" not in yaml.safe_dump(step).lower()
    assert "--output" in step["run"]


def test_jq_step_fails_with_an_error_annotation(build: dict[str, Any]) -> None:
    step = build["steps"][step_index(build["steps"], "jq -e")]
    assert "::error::" in step["run"]
    assert "detected.json" in step["run"]
    assert '.status == "ok"' in step["run"]
    assert "length) == 0" in step["run"]


def test_token_only_on_the_votes_step(
    wf: dict[str, Any], build: dict[str, Any], deploy: dict[str, Any]
) -> None:
    holders = [
        step
        for job in (build, deploy)
        for step in job["steps"]
        if "GITHUB_TOKEN" in yaml.safe_dump(step.get("env", {}))
    ]
    assert len(holders) == 1
    assert "gallery.votes" in holders[0]["run"]
    assert holders[0]["env"] == {"GITHUB_TOKEN": "${{ github.token }}"}
    assert TEXT.count("github.token") == 1
    assert TEXT.count("GITHUB_TOKEN") == 1
    assert "secrets." not in TEXT
    for job in (build, deploy):
        assert "env" not in job
    assert "env" not in wf


def test_data_files_are_outside_the_site(build: dict[str, Any]) -> None:
    steps = build["steps"]
    for needle in ("gallery.detect", "gallery.votes"):
        run = steps[step_index(steps, needle)]["run"]
        assert "$RUNNER_TEMP/" in run
        assert "_site" not in run
    run = steps[step_index(steps, "gallery.build")]["run"]
    assert "--detected" in run and "--votes" in run
    assert "--output _site" in run
    assert run.count("$RUNNER_TEMP/") == 2
    upload = steps[step_index(steps, "upload-pages-artifact")]
    assert upload["with"] == {"path": "_site"}


def test_python_setup_matches_validate_workflow(build: dict[str, Any]) -> None:
    steps = build["steps"]
    setup = steps[step_index(steps, "actions/setup-python@v5")]
    assert setup["with"] == {"python-version-file": ".python-version"}


def test_deploy_job(deploy: dict[str, Any]) -> None:
    assert deploy["needs"] == "build"
    assert deploy["permissions"] == {"pages": "write", "id-token": "write"}
    assert deploy["timeout-minutes"] == 10
    assert deploy["environment"] == {
        "name": "github-pages",
        "url": "${{ steps.deployment.outputs.page_url }}",
    }
    (step,) = deploy["steps"]
    assert step["uses"] == "actions/deploy-pages@v5"
    assert step["id"] == "deployment"


def test_deploy_runs_no_repository_code(deploy: dict[str, Any]) -> None:
    for step in deploy["steps"]:
        assert "run" not in step
        assert "checkout" not in step.get("uses", "")


def test_site_directory_is_git_ignored() -> None:
    ignore = (WORKFLOW.parents[2] / ".gitignore").read_text(encoding="utf-8")
    assert "_site/" in ignore.splitlines()
