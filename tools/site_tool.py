"""Builds docs.tapyr.eu from the Markdown of Kentrow/tapyr (ADR 0010 of that repository).

    python tools/site_tool.py prepare --tapyr DIR --ref REF --version VERSION --out DIR [--has-release]
    python tools/site_tool.py plan --tapyr DIR
    python tools/site_tool.py publish --tapyr DIR [--work DIR] [--push]
    python tools/site_tool.py check-links DIR

prepare exports the pages of a Tapyr checkout with its docsite tool, adds the home
page and the theme, and writes the Zensical configuration. plan compares the tags
of Tapyr with the gh-pages branch and says which versions to build; publish builds,
checks and deploys them with mike. check-links verifies that every internal link of
a built site reaches an existing page and id. Standard library only.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parent.parent
STABLE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
FIRST = (0, 1, 1)  # first tag with docs/site.yml
SHOWN = 3  # minors listed in the version selector, plus dev
SKIP_CLASS = "tapyr-version-link"  # links to the latest version, next to the site of a single build
ENGINE_PATHS = ["mkdocs.yml", "home", "theme", "tools/site_tool.py", "requirements.txt"]  # what changes the output
SITE = "https://docs.tapyr.eu"
DESCRIPTION_MAX = 160  # characters of a meta description that search results show
NOT_PROSE = re.compile(r"^(#|\||<|[-*+] |\d+\. |> |!\[|-{3,}$|!!! |\?\?\? |=== )")


# ---------- plan ----------

def plan(tags, main_sha, deployed, deployed_default=None, engine=None):
    """What to do so that every minor shows its latest stable tag and dev shows main.

    deployed is the versions.json of the published site (mike); each version keeps the
    ref it was built from in its "source" property and "hidden" when the version
    selector leaves it out. deployed_default is the version the site root redirects to.
    engine is the last commit of this repository that changed the output: a version
    built by another engine is rebuilt, so a theme or home page change reaches every version.
    Only changes are planned, so a day without a new tag or commit does nothing.
    """
    latest_of = {}
    for tag in tags:
        m = STABLE.match(tag)
        if not m:
            continue
        v = tuple(int(x) for x in m.groups())
        if v < FIRST:
            continue
        minor = v[:2]
        if minor not in latest_of or v > latest_of[minor]:
            latest_of[minor] = v
    minors = sorted(latest_of, reverse=True)
    sources = {d["version"]: d.get("properties", {}).get("source") for d in deployed}
    engines = {d["version"]: d.get("properties", {}).get("engine") for d in deployed}
    stale = (lambda name: engine is not None and engines.get(name) != engine)
    aliased = {d["version"]: d.get("aliases", []) for d in deployed}

    builds = []
    for i, minor in enumerate(minors):
        name = f"{minor[0]}.{minor[1]}"
        tag = "v{}.{}.{}".format(*latest_of[minor])
        aliases = ["latest"] if i == 0 else []
        # An older minor losing "latest" needs no build: deploying the new one with
        # --update-aliases moves the alias.
        if sources.get(name) != tag or stale(name) or (aliases and "latest" not in aliased.get(name, [])):
            builds.append({"version": name, "ref": tag, "source": tag, "aliases": aliases})
    # dev is rebuilt when main moved, or when a first release appears or disappears:
    # its banner links the latest release only if there is one.
    dev = next((d for d in deployed if d["version"] == "dev"), {})
    dev_had_release = dev.get("properties", {}).get("has_release", False)
    if sources.get("dev") != main_sha or dev_had_release != bool(minors) or stale("dev"):
        builds.append({"version": "dev", "ref": "main", "source": main_sha, "aliases": []})

    names = [f"{m[0]}.{m[1]}" for m in minors]
    hidden = {d["version"] for d in deployed if d.get("properties", {}).get("hidden")}
    default = "latest" if minors else "dev"
    return {
        "builds": builds,
        "default": default,
        "set_default": None if deployed_default == default else default,
        "hide": [n for n in names[SHOWN:] if n not in hidden],
        "unhide": [n for n in names[:SHOWN] if n in hidden],
    }


# ---------- prepare ----------

def page_url(path):
    """The URL of a page relative to the site root, as templates see it (page.url)."""
    stem = path[: -len(".md")]
    if stem == "index":
        return ""
    if stem.endswith("/index"):
        return stem[: -len("index")]
    return stem + "/"

def description(markdown):
    """The first paragraph of a page as plain text, cut for a meta description; None if it has none."""
    text = re.sub(r"<!--.*?-->", "", markdown, flags=re.S)
    paragraph, fenced = [], False
    for line in text.splitlines():
        line = line.strip()
        if line.startswith(("```", "~~~")):
            fenced = not fenced
        elif not fenced and line and not NOT_PROSE.match(line):
            paragraph.append(line)
            continue
        if paragraph:
            break
    if not paragraph:
        return None
    plain = " ".join(paragraph)
    plain = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", plain)  # links keep their text
    plain = re.sub(r"`([^`]*)`|\*\*([^*]+)\*\*", lambda m: m.group(1) or m.group(2), plain)
    plain = re.sub(r"\s+", " ", plain).strip()
    if len(plain) > DESCRIPTION_MAX:
        plain = plain[: DESCRIPTION_MAX - 1].rsplit(" ", 1)[0].rstrip(",;:") + "…"
    return plain


def page_descriptions(docs, index):
    """The description of the home page and of every exported page, keyed by page URL."""
    found = {}
    for path in ["index.md"] + [p["path"] for p in index["pages"]]:
        text = description((Path(docs) / path).read_text())
        if text:
            found[page_url(path)] = text
    return found


def generated_config(index, version, channel, has_release, descriptions=None):
    """The part of the Zensical configuration that comes from the export index."""
    config = {
        "nav": [{"Home": "index.md"}] + index["nav"],
        "extra": {
            "version": {"provider": "mike", "default": "latest", "alias": True},
            # Footer icons. They live here because the generated extra replaces any in mkdocs.yml.
            "social": [
                {"icon": "lucide/globe", "link": "https://tapyr.eu", "name": "tapyr.eu, the project site"},
                {"icon": "fontawesome/brands/github", "link": "https://github.com/Kentrow/tapyr", "name": "Tapyr on GitHub"},
            ],
            "tapyr": {
                "version": version,
                "channel": channel,
                "ref": index["ref"],
                "has_release": has_release,
                "sources": {page_url(p["path"]): p["source"] for p in index["pages"]},
                "descriptions": descriptions or {},
            },
        },
    }
    if channel == "release":
        # The canonical URL of every page, in every minor, is its /latest/ one: an older
        # minor sends search engines to the current page, and that URL never moves.
        # dev keeps its own and stays out of search results (theme/overrides/main.html).
        config["plugins"] = [{"mike": {"canonical_version": "latest"}}]
    return config


def prepare(tapyr, ref, version, out, has_release=False):
    out = Path(out).resolve()
    if out.exists():
        shutil.rmtree(out)
    docs = out / "docs"
    index_file = out / "index.json"
    out.mkdir(parents=True)
    subprocess.run(
        ["go", "run", "./internal/tools/docsite", "export", "-ref", ref, "-out", str(docs), "-index", str(index_file)],
        cwd=tapyr, check=True,
    )
    home = docs / "index.md"
    if home.exists():
        sys.exit("the export holds an index.md at the root; the home page belongs to this repository")
    shutil.copy(ROOT / "home" / "index.md", home)
    shutil.copytree(ROOT / "theme" / "assets", docs / "tapyr-theme")
    shutil.copytree(ROOT / "theme" / "overrides", out / "overrides")
    index = json.loads(index_file.read_text())
    channel = "dev" if ref == "main" else "release"
    extra = generated_config(index, version, channel, has_release, page_descriptions(docs, index))
    # The base configuration stays text: its slug function is a Python tag that a
    # YAML round trip would not keep. JSON is valid YAML for the generated part.
    base = (ROOT / "mkdocs.yml").read_text()
    generated = "\n".join(f"{key}: {json.dumps(value)}" for key, value in extra.items())
    (out / "mkdocs.yml").write_text(base.rstrip() + "\n\n# Generated by tools/site_tool.py\n" + generated + "\n")
    return out


# ---------- check-links ----------

class _Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.ids, self.links, self.version_links = set(), [], []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if a.get("id"):
            self.ids.add(a["id"])
        if tag == "a" and a.get("name"):
            self.ids.add(a["name"])
        if tag == "a" and "href" in a:
            version = SKIP_CLASS in (a.get("class") or "").split()
            (self.version_links if version else self.links).append(a["href"])


def check_links(site_dir, web_root=None):
    """Problems with internal links: target page missing, outside the site, or id missing.

    web_root is the directory served at / when the site is one version among others (the
    gh-pages tree): links from the root resolve there, and may reach another version."""
    root = Path(site_dir).resolve()
    web_root = Path(web_root).resolve() if web_root else root
    pages = {}
    for f in sorted(root.rglob("*.html")):
        p = _Page()
        p.feed(f.read_text(errors="replace"))
        pages[f] = p
    if not pages:  # a build that wrote nothing has no broken link, and must not pass
        return [f"{root}: no HTML page"]
    problems = []
    latest = root.parent / "latest"
    for f, page in pages.items():
        # Once deployed, the versions sit side by side: the latest one is next to this site.
        for href in page.version_links:
            path = unquote(urlsplit(href).path)
            target = (web_root / path.lstrip("/")) if path.startswith("/") else (f.parent / path)
            if Path(os.path.normpath(target)) != latest:
                problems.append(f"{f.relative_to(root)}: {href}: not the latest version")
        for href in page.links:
            parts = urlsplit(href)
            if parts.scheme or parts.netloc or href.startswith("//"):
                continue
            if not parts.path:
                target = f
            elif parts.path.startswith("/"):  # 404.html links from the site root
                target = (web_root / unquote(parts.path).lstrip("/")).resolve()
            else:
                target = (f.parent / unquote(parts.path)).resolve()
            if target.is_dir() or parts.path.endswith("/"):
                target = target / "index.html"
            where = f"{f.relative_to(root)}: {href}"
            if root not in target.parents and target != root:
                if web_root in target.parents and target.is_file():
                    continue  # a page of another version, checked with its own version
                problems.append(f"{where}: leaves the site")
                continue
            if target not in pages:
                problems.append(f"{where}: no such page")
                continue
            if parts.fragment and unquote(parts.fragment) not in pages[target].ids:
                problems.append(f"{where}: no id #{parts.fragment}")
    return problems


# ---------- publish ----------

def publish(the_plan, tapyr, work, push, engine=None, run=subprocess.run, prepare=prepare, links=check_links,
            deployed=None, repo=ROOT):
    """Carries out a plan: each build is exported, built in strict mode, link checked,
    committed by mike to the local gh-pages branch, then checked again as committed.
    The branch is pushed once, at the end: a failure on the way publishes nothing."""
    deployed = deployed or check_deployed
    target = ["-r", "origin", "-b", "gh-pages"]
    has_release = the_plan["default"] == "latest"
    config = str(Path(work) / "mkdocs.yml")
    changed = False
    for b in the_plan["builds"]:
        ref = "origin/main" if b["ref"] == "main" else b["ref"]
        run(["git", "-C", tapyr, "checkout", "--quiet", "--detach", ref], check=True)
        prepare(tapyr, b["ref"], b["version"], work, has_release=has_release)
        run(["zensical", "build", "--strict", "-f", config], check=True)
        problems = links(Path(work) / "site")
        if problems:
            for line in problems:
                print(line, file=sys.stderr)
            sys.exit(f"{b['version']}: {len(problems)} broken internal links, not deployed")
        run(["mike", "deploy", "-F", config, *target, "--update-aliases",
             "--prop-set-string", f"source={b['source']}",
             "--prop-set", f"has_release={'true' if has_release else 'false'}",
             *(["--prop-set-string", f"engine={engine}"] if engine else []),
             "-m", f"docs: {b['version']} from {b['ref']} ({b['source']})",
             b["version"], *b["aliases"]], check=True)
        # mike builds again, with its own site URL: what it committed is what gets served.
        problems = deployed(repo, b["version"], dev=b["ref"] == "main", has_release=has_release)
        if problems:
            for line in problems:
                print(line, file=sys.stderr)
            sys.exit(f"{b['version']}: {len(problems)} problems in the deployed pages, nothing pushed")
        changed = True
    if the_plan["hide"] or the_plan["unhide"] or the_plan["set_default"]:
        # mike loads a configuration even for commands that build nothing.
        stub = Path(tempfile.mkdtemp(prefix="mike-"))
        (stub / "docs").mkdir()
        (stub / "mkdocs.yml").write_text("site_name: Tapyr docs\ndocs_dir: docs\n")
        meta = ["-F", str(stub / "mkdocs.yml"), *target]
        for v in the_plan["hide"]:
            run(["mike", "props", *meta, "--set", "hidden=true", v], check=True)
        for v in the_plan["unhide"]:
            run(["mike", "props", *meta, "--delete", "hidden", v], check=True)
        if the_plan["set_default"]:
            run(["mike", "set-default", *meta, the_plan["set_default"]], check=True)
        changed = True
    changed = write_root_files(root_files(the_plan["default"]), run=run, repo=repo) or changed
    if changed and push:
        run(["git", "-C", str(repo), "push", "--quiet", "origin", "gh-pages"], check=True)


def check_deployed(repo, version, dev, has_release):
    """Problems in a version as committed to the local gh-pages branch: its links, among
    the other versions, and what only the deployed build carries (canonical URL, noindex)."""
    tree = Path(tempfile.mkdtemp(prefix="deployed-"))
    try:
        archive = subprocess.run(["git", "-C", str(repo), "archive", "gh-pages"], check=True, capture_output=True).stdout
        subprocess.run(["tar", "-x", "-C", str(tree)], input=archive, check=True)
        return deployed_problems(tree, version, dev, has_release)
    finally:
        shutil.rmtree(tree, ignore_errors=True)


def deployed_problems(tree, version, dev, has_release):
    site = Path(tree) / version
    problems = check_links(site, web_root=tree)
    home = site / "index.html"
    if not home.is_file():
        return problems + [f"{version}: no home page"]
    html = home.read_text(errors="replace")
    canonical = f'<link rel="canonical" href="{SITE}/{"dev" if dev else "latest"}/">'
    if canonical not in html:
        problems.append(f"{version}/index.html: canonical URL is not {canonical}")
    noindex = '<meta name="robots" content="noindex">' in html
    if noindex != (dev and has_release):
        problems.append(f"{version}/index.html: noindex is {'set' if noindex else 'missing'}")
    return problems


def root_files(default):
    """robots.txt, the redirect to the default version, the page of unknown URLs, the icons
    of the site and the image of shared links. Search engines take the icon of a site from
    its home page, here the redirect: it names the icons at the root, next to favicon.ico."""
    images = ROOT / "theme" / "assets" / "images"
    files = {"robots.txt": robots_txt(default), "index.html": redirect_html(default), "404.html": not_found_html(default)}
    for name in ["favicon.svg", "favicon-96.png", "favicon.ico", "og.png"]:
        files[name] = (images / name).read_bytes()
    return files


def robots_txt(default):
    return f"User-agent: *\nAllow: /\n\nSitemap: {SITE}/{default}/sitemap.xml\n"


def redirect_html(default):
    """The redirect page of mike (set-default), with the icons."""
    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>Redirecting</title>
  <link rel="icon" href="favicon.svg" type="image/svg+xml">
  <link rel="icon" href="favicon-96.png" sizes="96x96" type="image/png">
  <noscript>
    <meta http-equiv="refresh" content="1; url={default}/" />
  </noscript>
  <script>
    window.location.replace(
      "{default}/" + window.location.search + window.location.hash
    );
  </script>
</head>
<body>
  Redirecting to <a href="{default}/">{default}/</a>...
</body>
</html>
"""


def not_found_html(default):
    """The page GitHub Pages serves for a URL outside every version, at any depth: its
    links start from the root, and it carries its own few styles."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="robots" content="noindex">
  <title>Page not found - Tapyr docs</title>
  <link rel="icon" href="/favicon.svg" type="image/svg+xml">
  <link rel="icon" href="/favicon-96.png" sizes="96x96" type="image/png">
  <style>
    :root {{ color-scheme: light dark; }}
    body {{ margin: 0; min-height: 100vh; display: grid; place-items: center; padding: 24px; box-sizing: border-box;
      font: 1.05rem/1.6 system-ui, sans-serif; background: light-dark(#f2f8fb, #0b1622); color: light-dark(#0f2a3f, #e6f0f5); }}
    main {{ max-width: 32em; }}
    h1 {{ font-size: 1.8rem; line-height: 1.2; margin: 0 0 12px; }}
    p {{ margin: 0 0 12px; }}
    a {{ color: light-dark(#0a6890, #7cd0ee); }}
  </style>
</head>
<body>
  <main>
    <h1>Page not found</h1>
    <p>This page does not exist, or it moved with a new version of Tapyr.</p>
    <p><a href="/{default}/">Go to the Tapyr documentation</a></p>
  </main>
</body>
</html>
"""


def write_root_files(files, run=subprocess.run, repo=ROOT):
    """Files at the root of the gh-pages branch, committed only when they change
    (index.html after mike). True when a commit was made; publish pushes."""
    tree = tempfile.mkdtemp(prefix="gh-pages-")
    run(["git", "-C", str(repo), "worktree", "add", "--quiet", tree, "gh-pages"], check=True)
    try:
        for name, content in files.items():
            if isinstance(content, bytes):
                (Path(tree) / name).write_bytes(content)
            else:
                (Path(tree) / name).write_text(content)
            run(["git", "-C", tree, "add", name], check=True)
        if not run(["git", "-C", tree, "diff", "--cached", "--quiet"]).returncode:
            return False
        run(["git", "-C", tree, "commit", "--quiet", "-m", f"docs: update {', '.join(files)}"], check=True)
        return True
    finally:
        run(["git", "-C", str(repo), "worktree", "remove", "--force", tree], check=True)


# ---------- command line ----------

def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def deployed_state(repo=ROOT):
    """versions.json and the root redirect of the gh-pages branch, if it exists."""
    try:
        versions = json.loads(_git("show", "origin/gh-pages:versions.json", cwd=repo))
    except subprocess.CalledProcessError:
        return [], None
    try:
        index = _git("show", "origin/gh-pages:index.html", cwd=repo)
        m = re.search(r'url=([^/"]+)/', index)
        default = m.group(1) if m else None
    except subprocess.CalledProcessError:
        default = None
    return versions, default


def engine_commit(repo=ROOT):
    """The last commit of this repository that changed the built site."""
    return _git("log", "-1", "--format=%H", "--", *ENGINE_PATHS, cwd=repo).strip()


def current_plan(tapyr):
    tags = _git("tag", "--list", "v*", cwd=tapyr).split()
    main_sha = _git("rev-parse", "origin/main", cwd=tapyr).strip()
    deployed, default = deployed_state()
    return plan(tags, main_sha, deployed, default, engine=engine_commit())


def released_versions(tags):
    """Every published minor with the tag it is built from, for the CI matrix."""
    builds = plan(tags, None, [])["builds"]
    return [{"version": b["version"], "ref": b["ref"]} for b in builds if b["ref"] != "main"]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prepare", help="export a ref and write the Zensical configuration")
    p.add_argument("--tapyr", required=True, help="checkout of Kentrow/tapyr at the ref")
    p.add_argument("--ref", required=True)
    p.add_argument("--version", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--has-release", action="store_true")
    q = sub.add_parser("plan", help="print what publish would do")
    q.add_argument("--tapyr", required=True, help="checkout of Kentrow/tapyr with its tags")
    u = sub.add_parser("publish", help="build and deploy what changed")
    u.add_argument("--tapyr", required=True, help="checkout of Kentrow/tapyr with its tags")
    u.add_argument("--work", default=str(ROOT / "build"))
    u.add_argument("--push", action="store_true", help="push gh-pages (CI); without it, commit locally only")
    r = sub.add_parser("versions", help="print the published minors and their tags, as JSON")
    r.add_argument("--tapyr", required=True, help="checkout of Kentrow/tapyr with its tags")
    c = sub.add_parser("check-links", help="check the internal links of a built site")
    c.add_argument("site")
    args = ap.parse_args(argv)

    if args.cmd == "prepare":
        print(prepare(args.tapyr, args.ref, args.version, args.out, args.has_release))
    elif args.cmd == "plan":
        print(json.dumps(current_plan(args.tapyr), indent=2))
    elif args.cmd == "publish":
        the_plan = current_plan(args.tapyr)
        print(json.dumps(the_plan, indent=2))
        publish(the_plan, args.tapyr, args.work, args.push, engine=engine_commit())
    elif args.cmd == "versions":
        print(json.dumps(released_versions(_git("tag", "--list", "v*", cwd=args.tapyr).split())))
    elif args.cmd == "check-links":
        problems = check_links(args.site)
        for line in problems:
            print(line, file=sys.stderr)
        if problems:
            sys.exit(f"{len(problems)} broken internal links")
        print(f"internal links of {args.site}: all resolve")


if __name__ == "__main__":
    main()
