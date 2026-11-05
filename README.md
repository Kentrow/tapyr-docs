# tapyr-docs

The engine of [docs.tapyr.eu](https://docs.tapyr.eu), the documentation of [Tapyr](https://github.com/Kentrow/tapyr): Zensical configuration, theme, home page, build, versions and deployment.

The documentation itself is written in [Kentrow/tapyr](https://github.com/Kentrow/tapyr) and frozen by its release tags. To fix or add a page, open a pull request there; this repository never edits the content ([ADR 0010](https://github.com/Kentrow/tapyr/blob/main/docs/adr/0010-documentation-site.md), [AGENTS.md](AGENTS.md)).

## Build locally

Needs Python 3.11 or later, Go (the version of Tapyr's `go.mod`) and Git.

```sh
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
git clone https://github.com/Kentrow/tapyr.git tapyr

python tools/site_tool.py prepare --tapyr tapyr --ref main --version dev --out build
zensical build --strict -f build/mkdocs.yml
python tools/site_tool.py check-links build/site
python -m http.server -d build/site
```

`prepare` runs Tapyr's export (`go run ./internal/tools/docsite export`), adds `home/index.md` and `theme/`, and writes `build/mkdocs.yml` from `mkdocs.yml` plus the generated navigation.

To try the versioned site as it is published, commit to a local `gh-pages` branch without pushing:

```sh
git -C tapyr fetch --tags
python tools/site_tool.py plan --tapyr tapyr      # what would change
python tools/site_tool.py publish --tapyr tapyr   # build, check, commit to gh-pages
mike serve -r origin -b gh-pages
```

## How it is published

- **CI** (`.github/workflows/ci.yml`), on every pull request: tool tests, a strict build of `main`, and a check that every internal link reaches a page and an anchor.
- **Publish** (`.github/workflows/publish.yml`), daily and on demand: builds the minors whose latest stable tag changed and `dev` when `main` moved, then pushes the `gh-pages` branch with mike.

| URL | Built from |
|---|---|
| `/0.1/`, `/0.2/`… | latest stable tag of the minor |
| `/latest/` | alias of the latest minor |
| `/dev/` | `main` |
| `/` | redirects to `/latest/`, or `/dev/` before the first version |

## License

Apache-2.0, like Tapyr. The fonts in `theme/assets/fonts` are under the SIL Open Font License 1.1 (license files next to them).
