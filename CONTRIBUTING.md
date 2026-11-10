# Contributing a template

The easiest way to add a template is the
[submission form](https://github.com/SpecsFirst/gallery/issues/new?template=submit-template.yml).
You need a GitHub account and a public git repository (on GitHub, GitLab or any other
forge, over https).

## Templates must be model- and provider-agnostic

The gallery lists specs for humans and any agent. A template that ships files for one
agent tool ties its users to that tool and its provider, so the gallery refuses a
repository that holds any of these tool-specific files:

```
CLAUDE.md
.claude/
GEMINI.md
.gemini/
.cursor/
.cursorrules
.kiro/
.windsurf/
.github/copilot-instructions.md
.github/prompts/
```

`AGENTS.md` is the agnostic entry point and is the place for agent instructions: it is
allowed, and recommended.

## Submit with the form

1. Open the form and fill in the fields: the repository URL, a name (3 to 60 characters;
   it gives the template's id, shown in its URL), a one-line description (20 to 200
   characters: who it is for, what is different), where the specs live (their own repo or
   the code repo) and the tags: exactly one kind and up to 4 tags from
   [`taxonomy.yml`](taxonomy.yml), separated by commas. Optionally give the exact install
   command, and check the box to confirm the template is model- and provider-agnostic.
2. A bot answers on your issue. It checks your repository, then shows the entry it would
   add, or lists what to fix. Edit your issue to fix it: the check runs again and the bot
   updates its comment.
3. When the entry is valid, the comment has a link. It opens GitHub's "new file" page,
   with `templates/<id>.yml` already filled in. Click **Commit changes…**, then **Propose
   changes**: GitHub forks the repository for you and opens a pull request. Keep (or add)
   `Closes #N` in the pull request description, where `N` is your issue number.
4. The same checks run on your pull request, and a maintainer merges it. The bot never
   writes to the repository: your pull request is the only change.
5. The site is rebuilt after the merge, then every six hours. The template gets its own
   Discussion, and a star is a 👍 on it (with a GitHub account). Stars are refreshed with
   each rebuild.

The `<id>` is the name in lowercase, with letters and digits separated by single hyphens
(`Research thesis` gives `research-thesis`). Pick the name once and keep it: the id is in
the template's URL and in the title of its Discussion.

## The placement and the install command

"Where do the specs live?" sets the template's `placement`:

- **In their own repo** (`standalone`): the specs are a separate repository from the code
  they drive.
- **In the code repo** (`in-repo`): the specs live in the same repository as the code.

The optional **install command** is the exact one-line command that starts a new project
from the template, shown as is on the template's page. If you leave it empty, the gallery
builds one from a `copier.yml`.

## Copier

[Copier](https://copier.readthedocs.io/) is the recommended way to ship a template: a
`copier.yml` at the root of the repository gives visitors the one-command install
(`copier copy <url> my-project`, or `copier copy <url> .` for specs that live in the code
repo). The gallery detects the `copier.yml` and builds the
command for you.

## What the gallery detects, so you do not declare it

- the license
- whether a `copier.yml` file exists
- the date of the last update
- the file tree

Put a `LICENSE` file at the root of the template (or of its `path`). The gallery only reads
git objects: it never runs your code or checks it out, and it shows no preview of your
files. If the repository cannot be fetched, the template's page still shows the fields of
your entry.

## Without the form: a pull request

You can also open a pull request that adds `templates/<id>.yml`:

1. Fork this repository and create `templates/<id>.yml` (example below).
2. Run the check on your machine (see "Check it locally").
3. Open a pull request. The same check runs automatically, and a maintainer merges it.

Example, `templates/teaching-lab.yml`:

```yaml
name: Teaching lab notebook
url: https://github.com/example/template-teaching-lab
description: A specs repo for a lab course, where students and agents share one task board.
kind: research
tags: [teaching, bench]
placement: standalone
```

| Field         | Required | Limits                                                          |
| ------------- | -------- | --------------------------------------------------------------- |
| `name`        | yes      | 3 to 60 characters.                                             |
| `url`         | yes      | Starts with `https://`, with no spaces and no user or password. |
| `description` | yes      | 20 to 200 characters. One sentence: who it is for, what is different. |
| `kind`        | yes      | One kind from `taxonomy.yml`.                                   |
| `tags`        | no       | Up to 4 different tags from `taxonomy.yml`.                     |
| `path`        | no       | Subfolder holding the template. Relative, with no `..`.         |
| `ref`         | no       | Tag or commit to show (it cannot start with `-`). Default: the default branch. |
| `placement`   | no       | `standalone` (default) or `in-repo`: where the template's specs live. Shown as "Lives in": "Own repo" or "Code repo". |
| `command`     | no       | The exact install command to show, on one line, 1 to 300 characters, with no control character (it is pasted into terminals). |
| `language`    | no       | Two lowercase letters, such as `fr`. Default: `en`.             |

Any other field is refused (the form fills only the fields above).

## Kinds and tags

They come from [`taxonomy.yml`](taxonomy.yml). If you need a new tag, open a pull request on
`taxonomy.yml` that says which templates need it. Tags are never deleted or renamed: an old
one is marked deprecated and points to its replacement, so existing entries keep working
(the form replaces a deprecated tag by its replacement).

## Check it locally

```bash
uv sync
uv run python -m gallery.validate
```

It reports every problem in your file, not only the first one.

For code changes, run `uv run pytest` and `uv run ruff check .`.
