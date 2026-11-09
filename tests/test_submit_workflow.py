"""Structure of .github/workflows/submit.yml, as far as a parse can show it.

Actions cannot run here: the workflow is checked by actionlint and by these tests.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "submit.yml"
TEXT = WORKFLOW.read_text(encoding="utf-8")
LABEL_CONDITION = "contains(github.event.issue.labels.*.name, 'submission')"


@pytest.fixture(scope="module")
def wf() -> dict[str, Any]:
    document = yaml.safe_load(TEXT)
    assert isinstance(document, dict)
    return document


@pytest.fixture(scope="module")
def check(wf: dict[str, Any]) -> dict[str, Any]:
    return wf["jobs"]["check"]


@pytest.fixture(scope="module")
def comment(wf: dict[str, Any]) -> dict[str, Any]:
    return wf["jobs"]["comment"]


def all_steps(wf: dict[str, Any]) -> list[dict[str, Any]]:
    return [step for job in wf["jobs"].values() for step in job["steps"]]


def step_with(steps: list[dict[str, Any]], needle: str) -> dict[str, Any]:
    matches = [
        s for s in steps if needle in s.get("uses", "") or needle in s.get("run", "")
    ]
    assert len(matches) == 1, f"{needle!r} must appear in exactly one step"
    return matches[0]


def test_trigger_is_issues_opened_and_edited(wf: dict[str, Any]) -> None:
    assert wf[True] == {"issues": {"types": ["opened", "edited"]}}  # `on` is True


def test_jobs(wf: dict[str, Any]) -> None:
    assert set(wf["jobs"]) == {"check", "comment"}


def test_jobs_run_only_for_the_submission_label(
    check: dict[str, Any], comment: dict[str, Any]
) -> None:
    assert check["if"] == LABEL_CONDITION
    assert LABEL_CONDITION in comment["if"]
    assert "needs.check.outputs.found == 'true'" in comment["if"]


def test_top_level_permissions_are_empty(wf: dict[str, Any]) -> None:
    assert wf["permissions"] == {}


def test_job_permissions(check: dict[str, Any], comment: dict[str, Any]) -> None:
    assert check["permissions"] == {"contents": "read"}
    assert comment["permissions"] == {"issues": "write"}


def test_concurrency_is_per_issue_and_cancels(wf: dict[str, Any]) -> None:
    assert wf["concurrency"] == {
        "group": "submit-${{ github.event.issue.number }}",
        "cancel-in-progress": True,
    }


def test_nothing_writes_to_the_repository(wf: dict[str, Any]) -> None:
    permissions = [wf["permissions"]] + [j["permissions"] for j in wf["jobs"].values()]
    for granted in permissions:
        assert granted.get("contents") != "write"
        assert "pull-requests" not in granted
    assert "contents: write" not in TEXT
    assert "pull-requests" not in TEXT
    assert "git push" not in TEXT
    assert "gh pr" not in TEXT
    assert "secrets." not in TEXT


def test_no_expression_inside_any_run(wf: dict[str, Any]) -> None:
    runs = [step["run"] for step in all_steps(wf) if "run" in step]
    assert runs
    for run in runs:
        assert "${{" not in run


def test_expressions_only_in_the_expected_places(wf: dict[str, Any]) -> None:
    found = sorted(set(re.findall(r"\$\{\{[^}]*\}\}", TEXT)))
    assert found == [
        "${{ github.event.issue.number }}",
        "${{ github.token }}",
        "${{ runner.temp }}",
        "${{ steps.check.outputs.found }}",
        "${{ steps.prepare.outputs.number }}",
    ]
    # nothing from the issue (title, body, user, labels' text) except the number
    assert "github.event.issue.title" not in TEXT
    assert "github.event.issue.body" not in TEXT


def test_check_checks_out_main_without_credentials(check: dict[str, Any]) -> None:
    first = check["steps"][0]
    assert first["uses"] == "actions/checkout@v4"
    assert first["with"] == {"ref": "main", "persist-credentials": False}


def test_check_steps_in_order(check: dict[str, Any]) -> None:
    steps = check["steps"]
    uses = [s.get("uses", "") for s in steps]
    assert uses[0] == "actions/checkout@v4"
    assert uses[1] == "astral-sh/setup-uv@v5"
    assert uses[2] == "actions/setup-python@v5"
    assert steps[2]["with"] == {"python-version-file": ".python-version"}
    assert steps[3]["run"] == "uv sync --frozen"
    assert "gallery.submit check" in steps[4]["run"]
    assert uses[5].startswith("actions/upload-artifact@v4")
    assert len(steps) == 6


def test_check_runs_the_event_file_with_no_token(check: dict[str, Any]) -> None:
    step = step_with(check["steps"], "gallery.submit")
    assert '--event "$GITHUB_EVENT_PATH"' in step["run"]
    assert "env" not in step
    assert "env" not in check
    for s in check["steps"]:
        assert "token" not in yaml.safe_dump(s.get("env", {})).lower()
    assert "github.token" not in yaml.safe_dump(check)


def test_check_uploads_with_short_retention(check: dict[str, Any]) -> None:
    step = step_with(check["steps"], "actions/upload-artifact@v4")
    assert step["with"]["name"] == "submission"
    assert step["with"]["retention-days"] <= 1
    assert step["with"]["path"].startswith("${{ runner.temp }}/submission")


def test_comment_has_no_checkout_and_runs_no_repository_code(
    comment: dict[str, Any],
) -> None:
    for step in comment["steps"]:
        assert "checkout" not in step.get("uses", "")
        assert "setup-uv" not in step.get("uses", "")
        run = step.get("run", "")
        assert "uv " not in run and "python" not in run
        assert "./" not in run


def test_comment_downloads_and_reads_the_number_from_the_event(
    comment: dict[str, Any],
) -> None:
    download = step_with(comment["steps"], "actions/download-artifact@v4")
    assert download["with"]["name"] == "submission"
    prepare = step_with(comment["steps"], "GITHUB_EVENT_PATH")
    assert ".issue.number" in prepare["run"]
    assert "submission.json" in prepare["run"]
    assert "<!-- gallery-submission -->" in prepare["run"]


def test_token_only_on_the_comment_step(
    wf: dict[str, Any], check: dict[str, Any], comment: dict[str, Any]
) -> None:
    holders = [s for s in all_steps(wf) if "github.token" in yaml.safe_dump(s)]
    assert len(holders) == 1
    (holder,) = holders
    assert holder is comment["steps"][-1]
    assert holder["env"]["GH_TOKEN"] == "${{ github.token }}"
    assert TEXT.count("github.token") == 1
    assert TEXT.count("GH_TOKEN") == 1
    for job in (check, comment):
        assert "env" not in job
    assert "env" not in wf


def test_comment_posts_from_a_file_and_updates_the_previous_one(
    comment: dict[str, Any],
) -> None:
    run = comment["steps"][-1]["run"]
    assert 'gh issue comment "$ISSUE_NUMBER" --repo "$GITHUB_REPOSITORY"' in run
    assert '--body-file "$file"' in run
    assert "--body " not in run
    assert "gh api" in run and "--method PATCH" in run
    assert "<!-- gallery-submission -->" in run
    assert "github-actions[bot]" in run
    assert comment["steps"][-1]["env"]["ISSUE_NUMBER"] == (
        "${{ steps.prepare.outputs.number }}"
    )


def test_python_setup_matches_validate_workflow(check: dict[str, Any]) -> None:
    validate = yaml.safe_load(
        (WORKFLOW.parent / "validate.yml").read_text(encoding="utf-8")
    )
    reference = validate["jobs"]["validate"]["steps"]
    for needle in ("setup-uv", "setup-python", "uv sync --frozen"):
        assert step_with(check["steps"], needle) == step_with(reference, needle)
