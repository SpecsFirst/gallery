"""Tests for the ``tree`` renderer (pure, offline)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from gallery.detect import Options, detect_entry
from gallery.registry import Entry
from gallery.tree import render_tree

# The tree list that ``python -m gallery.detect`` produces for
# SpecsFirst/template-research-thesis, and the exact ``LC_ALL=C tree`` text
# it renders to (the reference from TASK-006).
RESEARCH_THESIS_TREE = [
    "LICENSE",
    "README.md",
    "analysis-code/",
    "analysis-code/main.md",
    "analysis-code/tasks/",
    "analysis-code/tasks/TASK-002-pipeline-example.md",
    "analysis-code/tasks/TASK-003-analysis-example.md",
    "analysis-code/tasks/board.md",
    "conventions.md",
    "copier.yml",
    "environment.md",
    "hypotheses/",
    "hypotheses/H1-short-name.md",
    "lab/",
    "lab/protocols/",
    "lab/protocols/short-name.md",
    "lab/tasks/",
    "lab/tasks/TASK-001-bench-example.md",
    "lab/tasks/board.md",
    "main.md",
]

RESEARCH_THESIS_TEXT = (
    ".\n"
    "├── LICENSE\n"
    "├── README.md\n"
    "├── analysis-code\n"
    "│   ├── main.md\n"
    "│   └── tasks\n"
    "│       ├── TASK-002-pipeline-example.md\n"
    "│       ├── TASK-003-analysis-example.md\n"
    "│       └── board.md\n"
    "├── conventions.md\n"
    "├── copier.yml\n"
    "├── environment.md\n"
    "├── hypotheses\n"
    "│   └── H1-short-name.md\n"
    "├── lab\n"
    "│   ├── protocols\n"
    "│   │   └── short-name.md\n"
    "│   └── tasks\n"
    "│       ├── TASK-001-bench-example.md\n"
    "│       └── board.md\n"
    "└── main.md"
)


def test_research_thesis_reference_byte_for_byte():
    rendered = render_tree(RESEARCH_THESIS_TREE)
    assert rendered.text == RESEARCH_THESIS_TEXT
    assert rendered.directories == 6
    assert rendered.files == 14
    assert rendered.summary == "6 directories, 14 files"


def test_children_sorted_in_byte_order():
    # Input deliberately unsorted; output must be byte order (LC_ALL=C).
    rendered = render_tree(
        [
            "analysis-code/",
            "README.md",
            "Z.txt",
            "_x.txt",
            "a.txt",
            "é.txt",
        ]
    )
    assert rendered.text == (
        ".\n"
        "├── README.md\n"
        "├── Z.txt\n"
        "├── _x.txt\n"
        "├── a.txt\n"
        "├── analysis-code\n"
        "└── é.txt"
    )
    assert rendered.directories == 1
    assert rendered.files == 5


def test_three_levels_with_bar_and_last_directory_with_content():
    rendered = render_tree(["a/", "a/b.txt", "a/c/", "a/c/d.txt", "e.txt"])
    assert rendered.text == (
        ".\n"
        "├── a\n"
        "│   ├── b.txt\n"
        "│   └── c\n"
        "│       └── d.txt\n"
        "└── e.txt"
    )
    assert rendered.directories == 2
    assert rendered.files == 3
    assert rendered.summary == "2 directories, 3 files"


def test_hidden_entries_are_dropped_with_their_subtree():
    rendered = render_tree(
        [
            ".github/",
            ".github/workflows/",
            ".github/workflows/x.yml",
            ".gitignore",
            "a/",
            "a/.hidden",
            "a/visible.txt",
            "a.b",
        ]
    )
    assert rendered.text == (
        ".\n"
        "├── a\n"
        "│   └── visible.txt\n"
        "└── a.b"
    )
    assert rendered.directories == 1
    assert rendered.files == 2


def test_truncated_listing_shows_missing_parents_as_directories():
    rendered = render_tree(["deep/nested/file.txt", "top.txt"])
    assert rendered.text == (
        ".\n"
        "├── deep\n"
        "│   └── nested\n"
        "│       └── file.txt\n"
        "└── top.txt"
    )
    assert rendered.directories == 2
    assert rendered.files == 2


def test_empty_input():
    rendered = render_tree([])
    assert rendered.text == "."
    assert rendered.directories == 0
    assert rendered.files == 0
    assert rendered.summary == "0 directories, 0 files"


def test_singular_summaries():
    assert render_tree(["only.txt"]).summary == "0 directories, 1 file"
    assert render_tree(["only-dir/"]).summary == "1 directory, 0 files"
    assert render_tree(["only-dir/", "only.txt"]).summary == "1 directory, 1 file"


def test_control_characters_are_replaced():
    rendered = render_tree(["with\nnewline.txt", "tab\there.txt"])
    assert rendered.text == ".\n├── tab?here.txt\n└── with?newline.txt"
    assert rendered.files == 2


def test_hostile_segments_are_ignored():
    rendered = render_tree(
        [
            "",
            "/",
            "./a.txt",
            "..",
            "../x.txt",
            "b//c.txt",
            "b/c.txt",
            "b/c.txt",
            "d/",
            "d/",
        ]
    )
    assert rendered.text == (
        ".\n"
        "├── a.txt\n"
        "├── b\n"
        "│   └── c.txt\n"
        "├── d\n"
        "└── x.txt"
    )
    assert rendered.directories == 2
    assert rendered.files == 3


def test_text_has_no_trailing_newline():
    assert not render_tree(["a.txt"]).text.endswith("\n")
    assert render_tree(["a.txt"]).text == ".\n└── a.txt"


def test_rendered_tree_is_frozen():
    rendered = render_tree([])
    try:
        rendered.text = "x"  # type: ignore[misc]
    except AttributeError:
        pass
    else:
        raise AssertionError("RenderedTree should be frozen")


# --- Integration with detect --------------------------------------------------

# The research-thesis file structure, as created in a real git repo, so that
# ``python -m gallery.detect``'s ``tree`` list feeds ``render_tree``.
_RESEARCH_THESIS_FILES = {
    "LICENSE": "MIT",
    "README.md": "# Research thesis",
    "analysis-code/main.md": "# analysis",
    "analysis-code/tasks/TASK-002-pipeline-example.md": "pipeline",
    "analysis-code/tasks/TASK-003-analysis-example.md": "analysis",
    "analysis-code/tasks/board.md": "board",
    "conventions.md": "conventions",
    "copier.yml": "_",
    "environment.md": "env",
    "hypotheses/H1-short-name.md": "H1",
    "lab/protocols/short-name.md": "short",
    "lab/tasks/TASK-001-bench-example.md": "bench",
    "lab/tasks/board.md": "board",
    "main.md": "# Main",
}


def _run_git(cwd: Path, *args: str) -> None:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }
    subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True)


def test_research_thesis_through_detect(tmp_path: Path):
    repo = tmp_path / "remote"
    repo.mkdir()
    _run_git(repo, "init", "-q", "-b", "main")
    _run_git(repo, "config", "uploadpack.allowFilter", "true")
    _run_git(repo, "config", "uploadpack.allowAnySHA1InWant", "true")
    for name, content in _RESEARCH_THESIS_FILES.items():
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    _run_git(repo, "add", "-A")
    _run_git(repo, "commit", "-q", "-m", "init")

    result = detect_entry(
        Entry(id="t", fields={"url": f"file://{repo}"}, language="en"),
        Options(extra_protocols=("file",), log=lambda message: None),
    )
    assert result["status"] == "ok", result

    # The real detect list must match the hardcoded reference exactly.
    assert result["tree"] == RESEARCH_THESIS_TREE
    rendered = render_tree(result["tree"])
    assert rendered.text == RESEARCH_THESIS_TEXT
    assert rendered.summary == "6 directories, 14 files"
    assert not result["tree_truncated"]

