"""Tests for template detection. Offline: real git repos built in ``tmp_path``."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest

from gallery import detect
from gallery.detect import Limits, Options, detect_all, detect_entry, main
from gallery.registry import Entry
from tests.helpers import write_entry, write_registry

# --- Local git repos used as "remote templates" -------------------------------

SOURCE_ENV = {
    "PATH": os.environ.get("PATH", ""),
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}

MIT = """\
MIT License

Copyright (c) 2026 Someone

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND.
"""


def run_git(cwd: Path, *args: str, date: str | None = None) -> str:
    env = dict(SOURCE_ENV)
    if date:
        env["GIT_COMMITTER_DATE"] = date
        env["GIT_AUTHOR_DATE"] = date
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


class Source:
    """A git repo playing the role of a template's remote."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.mkdir(parents=True)
        run_git(path, "init", "-q", "-b", "main")
        run_git(path, "config", "uploadpack.allowFilter", "true")
        run_git(path, "config", "uploadpack.allowAnySHA1InWant", "true")

    @property
    def url(self) -> str:
        return f"file://{self.path}"

    def commit(
        self,
        files: dict[str, bytes | str | None],
        date: str = "2026-01-01T00:00:00+00:00",
        message: str = "commit",
    ) -> str:
        """Write ``files`` (``None`` deletes), commit, return the sha."""
        for name, content in files.items():
            target = self.path / name
            if content is None:
                target.unlink()
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            data = content.encode() if isinstance(content, str) else content
            target.write_bytes(data)
        run_git(self.path, "add", "-A")
        run_git(self.path, "commit", "-q", "--allow-empty", "-m", message, date=date)
        return run_git(self.path, "rev-parse", "HEAD")


@pytest.fixture
def source(tmp_path: Path) -> Source:
    return Source(tmp_path / "remote")


def make_entry(url: str, entry_id: str = "t", **extra: object) -> Entry:
    return Entry(id=entry_id, fields={"url": url, **extra}, language="en")


def make_options(tmp_path: Path | None = None, **kwargs: object) -> Options:
    kwargs.setdefault("extra_protocols", ("file",))
    kwargs.setdefault("log", lambda message: None)
    return Options(**kwargs)  # type: ignore[arg-type]


def detect_source(source: Source, **kwargs: object) -> dict:
    options = kwargs.pop("options", None) or make_options()
    return detect_entry(make_entry(source.url, **kwargs), options)  # type: ignore[arg-type]


FAILED_KEYS = {
    "status",
    "error",
    "license",
    "copier",
    "tools",
    "last_update",
    "commit",
    "tree",
    "tree_truncated",
    "preview",
}


# --- A normal repo -------------------------------------------------------------


def test_normal_repo(source: Source):
    source.commit({"README.md": "old"}, date="2025-06-01T00:00:00+00:00")
    sha = source.commit(
        {
            "main.md": "# Hello\n\nA template.\n",
            "LICENSE": MIT,
            "copier.yml": "x: 1\n",
            "CLAUDE.md": "claude",
            "AGENTS.md": "agents",
            ".claude/settings.json": "{}",
            ".github/copilot-instructions.md": "hi",
            "subrepos/a/main.md": "a",
        },
        date="2026-10-01T11:30:00+02:00",
    )

    result = detect_source(source)

    assert set(result) == FAILED_KEYS
    assert result["status"] == "ok"
    assert result["error"] is None
    assert result["license"] == "MIT"
    assert result["copier"] is True
    assert result["tools"] == [
        ".claude/",
        ".github/copilot-instructions.md",
        "AGENTS.md",
        "CLAUDE.md",
    ]
    assert result["last_update"] == "2026-10-01T09:30:00Z"
    assert result["commit"] == sha
    assert result["tree"] == sorted(result["tree"])
    assert result["tree"] == [
        ".claude/",
        ".claude/settings.json",
        ".github/",
        ".github/copilot-instructions.md",
        "AGENTS.md",
        "CLAUDE.md",
        "LICENSE",
        "README.md",
        "copier.yml",
        "main.md",
        "subrepos/",
        "subrepos/a/",
        "subrepos/a/main.md",
    ]
    assert result["tree_truncated"] is False
    assert result["preview"] == "# Hello\n\nA template.\n"


def test_no_license_no_copier_no_main(source: Source):
    source.commit({"README.md": "hi"})
    result = detect_source(source)
    assert result["status"] == "ok"
    assert result["license"] is None
    assert result["copier"] is False
    assert result["tools"] == []
    assert result["preview"] is None


def test_copier_yaml_extension_and_symlinked_copier(source: Source, tmp_path: Path):
    source.commit({"copier.yaml": "x: 1\n"})
    assert detect_source(source)["copier"] is True

    other = Source(tmp_path / "other")
    other.commit({"real.yml": "x"})
    os.symlink("real.yml", other.path / "copier.yml")
    other.commit({}, message="link")
    assert detect_entry(make_entry(other.url), make_options())["copier"] is False


# --- Symlinks, submodules ------------------------------------------------------


def test_main_md_symlink_is_never_followed(source: Source, tmp_path: Path):
    secret = tmp_path / "runner-secret.txt"
    secret.write_text("TOP-SECRET-RUNNER-DATA")
    source.commit({"README.md": "hi"})
    os.symlink(secret, source.path / "main.md")
    source.commit({}, date="2026-01-02T00:00:00Z", message="link")

    result = detect_source(source)

    assert result["status"] == "ok"
    assert result["preview"] is None
    assert "main.md" in result["tree"]  # listed by name
    dump = json.dumps(result)
    assert "TOP-SECRET-RUNNER-DATA" not in dump
    assert str(secret) not in dump


def test_submodule_and_symlink_listed_by_name(source: Source, tmp_path: Path):
    sub = Source(tmp_path / "subrepo")
    sub_sha = sub.commit({"x": "1"})
    source.commit({"main.md": "hi"})
    run_git(
        source.path,
        "update-index",
        "--add",
        "--cacheinfo",
        f"160000,{sub_sha},vendor",
    )
    os.symlink("../outside", source.path / "link")
    run_git(source.path, "add", "link")
    run_git(source.path, "commit", "-q", "-m", "more")

    result = detect_source(source)

    assert result["status"] == "ok"
    assert "vendor" in result["tree"] and "link" in result["tree"]
    assert not any(name.startswith("vendor/") for name in result["tree"])


# --- ref and path -------------------------------------------------------------


def test_ref_as_tag_and_as_commit(source: Source):
    first = source.commit({"main.md": "first"}, date="2025-01-01T00:00:00+00:00")
    run_git(source.path, "tag", "light")
    run_git(source.path, "tag", "-a", "-m", "annotated", "annotated")
    source.commit({"main.md": "second"}, date="2026-01-01T00:00:00+00:00")

    for ref in ("light", "annotated", first):
        result = detect_source(source, ref=ref)
        assert result["status"] == "ok", ref
        assert result["commit"] == first
        assert result["preview"] == "first"
        assert result["last_update"] == "2025-01-01T00:00:00Z"

    head = detect_source(source)
    assert head["preview"] == "second"


def test_path_detection_is_relative_to_path(source: Source):
    source.commit(
        {
            "LICENSE": "Apache License\nVersion 2.0, January 2004\n",
            "main.md": "root preview",
            "tools/AGENTS.md": "not at the path",
            "sub/main.md": "# Sub template\n",
            "sub/LICENSE": MIT,
            "sub/copier.yml": "x: 1",
            "sub/CLAUDE.md": "c",
            "sub/deep/file.txt": "f",
        },
        date="2025-03-01T00:00:00+00:00",
    )
    latest = source.commit({"unrelated.txt": "x"}, date="2026-09-09T00:00:00+00:00")

    result = detect_source(source, path="sub/")

    assert result["status"] == "ok"
    assert result["last_update"] == "2025-03-01T00:00:00Z"
    assert result["commit"] == latest
    assert result["license"] == "MIT"
    assert result["copier"] is True
    assert result["tools"] == ["CLAUDE.md"]
    assert result["preview"] == "# Sub template\n"
    assert result["tree"] == [
        "CLAUDE.md",
        "LICENSE",
        "copier.yml",
        "deep/",
        "deep/file.txt",
        "main.md",
    ]


def test_path_last_update_follows_commits_touching_the_path(source: Source):
    source.commit({"sub/main.md": "v1"}, date="2025-03-01T00:00:00+00:00")
    source.commit({"sub/main.md": "v2"}, date="2025-07-01T00:00:00+00:00")
    source.commit({"other.txt": "o"}, date="2026-01-01T00:00:00+00:00")

    result = detect_source(source, path="sub")

    assert result["last_update"] == "2025-07-01T00:00:00Z"
    assert result["preview"] == "v2"


def test_path_license_falls_back_to_repo_root(source: Source):
    source.commit({"LICENSE": MIT, "sub/main.md": "x"})
    result = detect_source(source, path="sub")
    assert result["license"] == "MIT"


def test_failures_are_classified(source: Source, tmp_path: Path):
    source.commit({"main.md": "x"})

    assert detect_source(source, ref="nope")["error"] == "ref_not_found"
    assert detect_source(source, ref="0" * 40)["error"] == "ref_not_found"
    assert detect_source(source, path="missing")["error"] == "path_not_found"
    assert detect_source(source, path="main.md")["error"] == "path_not_found"

    gone = Source(tmp_path / "gone")
    gone_url = gone.url
    unreachable = detect_entry(make_entry(gone_url + "-missing"), make_options())
    assert unreachable["error"] == "unreachable"

    for result in (unreachable, detect_source(source, ref="nope")):
        assert result["status"] == "failed"
        assert set(result) == FAILED_KEYS
        assert all(result[k] is None for k in FAILED_KEYS - {"status", "error"})
        assert result["error"] in detect.ERRORS


@pytest.mark.parametrize(
    "fields",
    [
        {"url": "http://example.com/repo"},
        {"url": "git@github.com:a/b.git"},
        {"url": "ssh://git@github.com/a/b"},
        {"url": "file:///tmp/whatever"},
        {"url": "ext::sh -c touch% /tmp/pwned"},
        {"url": "https://example.com/repo", "path": "/etc"},
        {"url": "https://example.com/repo", "path": "../x"},
        {"url": "https://example.com/repo", "path": "a/../../x"},
        {"url": "https://example.com/repo", "path": "C:/x"},
        {"url": "https://example.com/repo", "ref": "--upload-pack=touch /tmp/x"},
        {"url": "https://example.com/repo", "ref": "-x"},
        {"url": "https://example.com/repo", "ref": "a:b"},
        {"url": "https://example.com/repo", "ref": "refs/heads/*"},
        {"url": None},
        {},
        {"url": "https://example.com/repo", "path": 12},
    ],
)
def test_invalid_entries_fetch_nothing(fields, monkeypatch):
    spawned = []
    monkeypatch.setattr(detect, "_spawn", lambda cmd, **kw: spawned.append(cmd))
    options = Options(log=lambda message: None)

    result = detect_entry(Entry(id="t", fields=fields, language="en"), options)

    assert result["status"] == "failed"
    assert result["error"] == "invalid_entry"
    assert spawned == []


def test_extra_protocol_is_not_reachable_from_the_command_line(tmp_path: Path):
    root = write_registry(tmp_path / "reg")
    write_entry(
        root,
        "local",
        {"name": "Local one", "url": "file:///tmp/x", "description": "d" * 20},
    )
    out = tmp_path / "detected.json"
    assert main(["--root", str(root), "--output", str(out)]) == 0
    data = json.loads(out.read_text())
    assert data["templates"]["local"]["error"] == "invalid_entry"


# --- Licenses -----------------------------------------------------------------

BSD_BODY = """\
Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

1. Redistributions of source code must retain the above copyright notice, this
   list of conditions and the following disclaimer.

2. Redistributions in binary form must reproduce the above copyright notice,
   this list of conditions and the following disclaimer in the documentation
   and/or other materials provided with the distribution.
"""
BSD3_EXTRA = """
3. Neither the name of the copyright holder nor the names of its
   contributors may be used to endorse or promote products derived from
   this software without specific prior written permission.
"""

LICENSES = {
    "MIT": MIT,
    "Apache-2.0": (
        "                                 Apache License\n"
        "                           Version 2.0, January 2004\n"
        "                        http://www.apache.org/licenses/\n"
    ),
    "BSD-2-Clause": "BSD 2-Clause License\n\nCopyright (c) 2026\n\n" + BSD_BODY,
    "BSD-3-Clause": "BSD 3-Clause License\n\n" + BSD_BODY + BSD3_EXTRA,
    "GPL-2.0": (
        "                    GNU GENERAL PUBLIC LICENSE\n"
        "                       Version 2, June 1991\n\n"
        "the GNU Library General Public License instead of this License.\n"
        "the GNU Lesser General Public License instead.\n"
    ),
    "GPL-3.0": (
        "                    GNU GENERAL PUBLIC LICENSE\n"
        "                       Version 3, 29 June 2007\n\n"
        "13. Use with the GNU Affero General Public License.\n"
        "use under the terms of the GNU Affero General Public License version 3\n"
    ),
    "LGPL-3.0": (
        "                   GNU LESSER GENERAL PUBLIC LICENSE\n"
        "                       Version 3, 29 June 2007\n\n"
        "This version of the GNU Lesser General Public License incorporates\n"
        "the terms and conditions of version 3 of the GNU General Public\n"
        "License, supplemented by the additional permissions listed below.\n"
        "GNU General Public License, version 3\n"
    ),
    "AGPL-3.0": (
        "                    GNU AFFERO GENERAL PUBLIC LICENSE\n"
        "                       Version 3, 19 November 2007\n\n"
        "your version of the GNU General Public License, version 3\n"
        "a work based on the GNU General Public License, version 3.\n"
    ),
    "MPL-2.0": (
        "Mozilla Public License Version 2.0\n==================================\n\n"
        '"Secondary License" means either the GNU General Public License,\n'
        "Version 2.0, the GNU Lesser General Public License, Version 2.1, the GNU\n"
        "Affero General Public License, Version 3.0, or any later versions.\n"
    ),
    "ISC": (
        "ISC License\n\nCopyright (c) 2026\n\nPermission to use, copy, modify, and/or "
        "distribute this software for any purpose\nwith or without fee is hereby "
        "granted, provided that the above notice appears.\n"
    ),
    "Unlicense": (
        "This is free and unencumbered software released into the public domain.\n"
    ),
    "CC0-1.0": "CC0 1.0 Universal\n\nCreative Commons Legal Code\n",
    "CC-BY-4.0": (
        "Attribution 4.0 International\n\n"
        "Creative Commons Attribution 4.0 International Public License\n"
    ),
    "CC-BY-SA-4.0": (
        "Attribution-ShareAlike 4.0 International\n\n"
        "Creative Commons Attribution-ShareAlike 4.0 International Public License\n"
    ),
}


@pytest.mark.parametrize("spdx", sorted(LICENSES))
def test_identify_license(spdx: str):
    assert detect.identify_license(LICENSES[spdx]) == spdx


def test_unidentified_license_text_is_unknown():
    assert detect.identify_license("All rights reserved. Ask me.") == "unknown"
    assert detect.identify_license("") == "unknown"


@pytest.mark.parametrize("spdx", ["GPL-3.0", "AGPL-3.0", "LGPL-3.0", "GPL-2.0"])
def test_gnu_licenses_are_never_confused(source: Source, spdx: str):
    source.commit({"LICENSE": LICENSES[spdx]})
    assert detect_source(source)["license"] == spdx


@pytest.mark.parametrize("name", ["LICENSE", "LICENCE", "COPYING", "LICENSE.md"])
def test_license_file_names(source: Source, name: str):
    source.commit({name: MIT})
    assert detect_source(source)["license"] == "MIT"


def test_unreadable_license_is_unknown(source: Source):
    source.commit({"LICENSE": "Proprietary. All rights reserved.\n"})
    assert detect_source(source)["license"] == "unknown"


@pytest.mark.skipif(
    not Path("/usr/share/common-licenses/GPL-3").exists(),
    reason="no system license texts",
)
@pytest.mark.parametrize(
    ("file", "spdx"),
    [
        ("Apache-2.0", "Apache-2.0"),
        ("GPL-2", "GPL-2.0"),
        ("GPL-3", "GPL-3.0"),
        ("LGPL-3", "LGPL-3.0"),
        ("MPL-2.0", "MPL-2.0"),
        ("CC0-1.0", "CC0-1.0"),
    ],
)
def test_identify_real_license_texts(file: str, spdx: str):
    text = (Path("/usr/share/common-licenses") / file).read_text(errors="replace")
    assert detect.identify_license(text) == spdx


# --- Tree and preview ---------------------------------------------------------


def test_tree_is_truncated_beyond_the_limit(source: Source):
    source.commit({f"f{i:03d}.txt": "x" for i in range(30)})

    options = make_options(limits=Limits(max_tree_entries=10))
    result = detect_source(source, options=options)
    assert result["tree"] == [f"f{i:03d}.txt" for i in range(10)]
    assert result["tree_truncated"] is True

    full = detect_source(source)
    assert len(full["tree"]) == 30 and full["tree_truncated"] is False


def test_default_tree_limit_is_500(source: Source):
    assert detect.MAX_TREE_ENTRIES == 500
    source.commit({f"d/f{i:04d}.txt": "x" for i in range(600)})
    result = detect_source(source)
    assert len(result["tree"]) == 500
    assert result["tree_truncated"] is True
    assert result["tree"][0] == "d/"


def test_preview_replaces_invalid_utf8(source: Source):
    source.commit({"main.md": b"ok \xff\xfe bytes \xc3\x28 end"})
    result = detect_source(source)
    assert result["status"] == "ok"
    assert result["preview"].startswith("ok ")
    assert "\ufffd" in result["preview"]
    assert result["preview"].endswith(" end")


def test_oversized_preview_is_cut_on_a_character_boundary(source: Source):
    # "é" is two bytes: an odd limit cuts a character in two.
    source.commit({"main.md": "é" * 100})
    result = detect_source(
        source, options=make_options(limits=Limits(max_preview_bytes=11))
    )
    assert result["preview"] == "é" * 5
    assert "\ufffd" not in result["preview"]


def test_default_preview_limit_is_64kb(source: Source):
    assert detect.MAX_PREVIEW_BYTES == 64 * 1024
    source.commit({"main.md": "a" * 100_000})
    result = detect_source(source)
    assert result["preview"] == "a" * 64 * 1024


def test_invalid_utf8_preview_never_exceeds_the_limit():
    text = detect.decode_preview(b"\xff" * 100, True, 10)
    assert len(text.encode()) <= 10


def test_blob_filtered_out_is_absent_not_fetched_lazily(source: Source):
    # Blobs over 1 MB are filtered by the fetch; reading must not fetch them.
    source.commit({"main.md": "a" * 1_500_000, "LICENSE": MIT})
    result = detect_source(source)
    assert result["status"] == "ok"
    assert result["preview"] is None
    assert result["license"] == "MIT"


# --- Limits -------------------------------------------------------------------


def test_timeout_kills_a_hanging_server():
    # A server that accepts the connection and never answers.
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(5)
    port = server.getsockname()[1]
    held = []

    def accept() -> None:
        while True:
            try:
                held.append(server.accept()[0])
            except OSError:
                return

    threading.Thread(target=accept, daemon=True).start()
    try:
        options = make_options(
            extra_protocols=("http",), limits=Limits(git_timeout=1.5)
        )
        started = time.monotonic()
        result = detect_entry(make_entry(f"http://127.0.0.1:{port}/repo.git"), options)
        elapsed = time.monotonic() - started
    finally:
        server.close()
        for conn in held:
            conn.close()

    assert result["status"] == "failed"
    assert result["error"] == "timeout"
    assert elapsed < 20


def test_disk_overflow_is_too_large(source: Source):
    # Incompressible content, well over the (small) limit.
    source.commit({"blob.bin": os.urandom(400_000), "main.md": "x"})
    options = make_options(limits=Limits(max_disk_bytes=50_000))

    result = detect_source(source, options=options)

    assert result["status"] == "failed"
    assert result["error"] == "too_large"


def test_default_limits_are_the_documented_ones():
    limits = Limits()
    assert limits.git_timeout == detect.GIT_TIMEOUT_SECONDS == 120
    assert limits.max_disk_bytes == detect.MAX_DISK_BYTES == 200 * 1024 * 1024


# --- Isolation ----------------------------------------------------------------


def test_one_failing_template_does_not_affect_another(source: Source):
    source.commit({"main.md": "works"})
    entries = [
        make_entry(source.url + "-missing", "a-broken"),
        make_entry(source.url, "b-working"),
        make_entry("http://example.com/x", "c-invalid"),
    ]

    document = detect_all(entries, make_options(), jobs=3)

    templates = document["templates"]
    assert list(templates) == ["a-broken", "b-working", "c-invalid"]
    assert templates["a-broken"]["error"] == "unreachable"
    assert templates["b-working"]["status"] == "ok"
    assert templates["b-working"]["preview"] == "works"
    assert templates["c-invalid"]["error"] == "invalid_entry"
    assert document["generated_at"].endswith("Z")


def test_unexpected_exception_is_contained(source: Source, monkeypatch):
    source.commit({"main.md": "works"})

    def boom(session, target):
        raise RuntimeError("::error::boom\nsecond line")

    monkeypatch.setattr(detect, "_inspect", boom)
    logged = []
    result = detect_entry(
        make_entry(source.url), make_options(log=logged.append)
    )
    assert result["status"] == "failed"
    assert result["error"] in detect.ERRORS
    assert all("\n" not in line and "\r" not in line for line in logged)


@pytest.fixture
def spy(monkeypatch):
    calls = []
    real = detect._spawn

    def spawn(cmd, **kwargs):
        calls.append((list(cmd), dict(kwargs.get("env") or {})))
        return real(cmd, **kwargs)

    monkeypatch.setattr(detect, "_spawn", spawn)
    return calls


def test_git_environment_holds_no_runner_token(source: Source, spy, monkeypatch):
    for name in ("GITHUB_TOKEN", "GH_TOKEN", "ACTIONS_RUNTIME_TOKEN", "HTTPS_PROXY"):
        monkeypatch.setenv(name, "runner-secret-value")
    source.commit({"main.md": "x"})

    result = detect_source(source)

    assert result["status"] == "ok"
    assert spy
    allowed = {
        "PATH",
        "LANG",
        "LC_ALL",
        "HOME",
        "GIT_TERMINAL_PROMPT",
        "GIT_CONFIG_NOSYSTEM",
        "GIT_CONFIG_GLOBAL",
        "GIT_NO_LAZY_FETCH",
        "GIT_LITERAL_PATHSPECS",
    }
    for cmd, env in spy:
        assert set(env) <= allowed, set(env) - allowed
        assert "runner-secret-value" not in " ".join(env.values())
        assert env["GIT_TERMINAL_PROMPT"] == "0"
        assert env["GIT_CONFIG_NOSYSTEM"] == "1"
        assert env["GIT_CONFIG_GLOBAL"] == "/dev/null"
        assert env["HOME"] != os.environ.get("HOME")
        joined = " ".join(cmd)
        assert "protocol.allow=never" in joined
        assert "protocol.https.allow=always" in joined
        assert "credential.helper=" in cmd
        assert "core.hooksPath=/dev/null" in joined
        assert "core.fsmonitor=false" in joined


def test_default_protocols_are_https_only(source: Source, spy):
    source.commit({"main.md": "x"})
    # No extra protocol: even a well-formed local URL is refused before any fetch.
    result = detect_entry(make_entry(source.url), Options(log=lambda m: None))
    assert result["error"] == "invalid_entry"
    assert spy == []


def test_no_working_tree_is_created(source: Source, spy, tmp_path: Path):
    source.commit({"main.md": "preview", "LICENSE": MIT, "a/b.txt": "payload"})
    work = tmp_path / "work"
    work.mkdir()

    result = detect_source(
        source, options=make_options(tmp_root=work, keep_tmp=True)
    )

    assert result["status"] == "ok"
    (session_dir,) = list(work.iterdir())
    assert {p.name for p in session_dir.iterdir()} == {"repo.git", "home", "cwd", "io"}
    assert list((session_dir / "cwd").iterdir()) == []
    assert list((session_dir / "home").iterdir()) == []
    assert (session_dir / "repo.git" / "HEAD").exists()
    assert not (session_dir / "repo.git" / "index").exists()
    # Only object reads and the fetch itself ever ran.
    known = READ_COMMANDS | FORBIDDEN_COMMANDS
    for cmd, _env in spy:
        (subcommand,) = [c for c in cmd[1:] if c in known]
        assert subcommand in READ_COMMANDS, cmd


READ_COMMANDS = {"init", "config", "fetch", "rev-parse", "cat-file", "log", "ls-tree"}
FORBIDDEN_COMMANDS = {
    "checkout", "clone", "submodule", "read-tree", "worktree", "lfs",
    "archive", "restore", "reset", "show", "diff", "pull", "merge",
}  # fmt: skip


def test_temporary_directory_is_removed(source: Source, tmp_path: Path):
    source.commit({"main.md": "x"})
    work = tmp_path / "work"
    work.mkdir()
    detect_source(source, options=make_options(tmp_root=work))
    assert list(work.iterdir()) == []


# --- Log hygiene --------------------------------------------------------------


def test_sanitize_log_removes_newlines_and_leading_command_marker():
    cleaned = detect.sanitize_log("::error::bad\r\n::set-output name=x::y\nmore")
    assert "\n" not in cleaned and "\r" not in cleaned
    assert not cleaned.startswith("::")
    assert detect.sanitize_log(":: ::warning::x").startswith("warning")


def test_git_stderr_is_logged_clean_and_never_published(source: Source):
    logged = []
    missing = source.url + "-missing"
    result = detect_entry(make_entry(missing), make_options(log=logged.append))

    assert result["error"] == "unreachable"
    assert "fatal" not in json.dumps(result)
    assert logged
    assert all("\n" not in line and "\r" not in line for line in logged)
    assert all(missing not in line for line in logged)


# --- Command line -------------------------------------------------------------


def test_cli_writes_sorted_json_and_exits_zero_when_all_fail(tmp_path: Path):
    root = write_registry(tmp_path / "reg")
    for entry_id in ("zeta", "alpha"):
        write_entry(
            root,
            entry_id,
            {
                "name": entry_id,
                "url": "https://127.0.0.1:9/nothing.git",
                "description": "d" * 20,
            },
        )
    out = tmp_path / "out" / "detected.json"
    out.parent.mkdir()

    code = main(["--root", str(root), "--output", str(out), "--jobs", "2"])

    assert code == 0
    text = out.read_text()
    data = json.loads(text)
    assert list(data["templates"]) == ["alpha", "zeta"]
    assert {t["status"] for t in data["templates"].values()} == {"failed"}
    assert {t["error"] for t in data["templates"].values()} == {"unreachable"}
    assert text == json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def test_cli_fails_when_the_registry_cannot_be_loaded(tmp_path: Path):
    out = tmp_path / "detected.json"
    assert main(["--root", str(tmp_path / "nothing"), "--output", str(out)]) != 0
    assert not out.exists()


def test_cli_fails_when_the_output_cannot_be_written(tmp_path: Path):
    root = write_registry(tmp_path / "reg")
    out = tmp_path / "missing-dir" / "detected.json"
    assert main(["--root", str(root), "--output", str(out)]) != 0


def test_cli_requires_output():
    with pytest.raises(SystemExit):
        main([])


@pytest.mark.parametrize("url", ["https://[bad", "https://[bad/x"])
def test_unparsable_url_is_an_invalid_entry(url: str):
    result = detect_entry(make_entry(url), make_options())
    assert result["status"] == "failed"
    assert result["error"] == "invalid_entry"


def test_identify_lgpl_2_1():
    text = "GNU LESSER GENERAL PUBLIC LICENSE\nVersion 2.1, February 1999\n"
    assert detect.identify_license(text) == "LGPL-2.1"
