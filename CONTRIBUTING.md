# Contributing a template

The easiest way to add a template is the
[submission form](https://github.com/SpecsFirst/gallery/issues/new?template=submit-template.yml).
You need a GitHub account and a public git repository (on GitHub, GitLab or any other
forge, over https).

## Submit with the form

1. Open the form and fill in four fields: the repository URL, a name (3 to 60 characters;
   it gives the template's id, shown in its URL), a one-line description (20 to 200
   characters: who it is for, what is different) and the tags: exactly one kind and up to
   4 tags from [`taxonomy.yml`](taxonomy.yml), separated by commas.
2. A bot answers on your issue. It checks your repository, then
   shows the entry it would add, or lists what to fix. Edit your issue to fix it: the
   check runs again and the bot updates its comment.
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
| `language`    | no       | Two lowercase letters, such as `fr`. Default: `en`.             |

Any other field is refused (the form fills only the first five).

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
