# Contributing a template

You can add a template to the gallery with one pull request that adds one small file.

## Steps

1. Fork this repository and create `templates/<id>.yml`.
2. Fill it in (example below) and run the check on your machine (see "Check it locally").
3. Open a pull request. The same check runs automatically, and a maintainer merges it.

The `<id>` is the file name without `.yml`. It uses lowercase letters and digits, with
single hyphens between them (`research-thesis`, not `Research_Thesis`). It ends up in the
template's URL and in the title of its Discussion, so pick it once and keep it.

## Example

Save this as `templates/teaching-lab.yml`:

```yaml
name: Teaching lab notebook
url: https://github.com/example/template-teaching-lab
description: A specs repo for a lab course, where students and agents share one task board.
kind: research
tags: [teaching, bench]
```

## Fields

| Field         | Required | Limits                                                          |
| ------------- | -------- | --------------------------------------------------------------- |
| `name`        | yes      | 3 to 60 characters.                                             |
| `url`         | yes      | Starts with `https://`, with no spaces and no user or password. |
| `description` | yes      | 20 to 200 characters. One sentence: who it is for, what is different. |
| `kind`        | yes      | One kind from `taxonomy.yml`.                                   |
| `tags`        | no       | Up to 4 different tags from `taxonomy.yml`.                     |
| `path`        | no       | Subfolder holding the template. Relative, with no `..`.         |
| `ref`         | no       | Tag or commit to show (it cannot start with `-`). Default: the default branch. |
| `language`    | no       | Two lowercase letters, such as `fr`. Default: `en`.             |

Any other field is refused. Kinds and tags come from [`taxonomy.yml`](taxonomy.yml), so look
there for the current lists. If you need a new tag, open a pull request on `taxonomy.yml`
that says which templates need it. Tags are never deleted or renamed: an old one is marked
deprecated and points to its replacement, so existing entries keep working.

## What your template must be

A public git repository, reachable over https, on any forge (GitHub, GitLab, Codeberg...).
The gallery only reads its git objects. It never runs your code or checks it out. If the
repository cannot be fetched, the template's page still shows the fields of your entry.

## What the gallery detects, so you do not declare it

- the license, the `copier.yml` file, and the tool-specific files (`CLAUDE.md`,
  `AGENTS.md`, `.opencode/`...)
- the date of the last update and the file tree
- the preview, taken from `main.md`

Put a `main.md` and a license file (`LICENSE`) at the root of the template (or of its `path`):
they fill the preview and the license of its page.

## After the merge

The site is rebuilt on each push to `main` and every six hours. Votes are 👍 reactions on the
template's page, through giscus (you sign in with GitHub). They refresh with each rebuild.

## Check it locally

```bash
uv sync
uv run python -m gallery.validate
```

It reports every problem in your file, not only the first one.

For code changes, run `uv run pytest` and `uv run ruff check .`.
