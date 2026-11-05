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
SKIP_CLASS = "tapyr-version-link"  # links to other versions, outside a single build
ENGINE_PATHS = ["mkdocs.yml", "home", "theme", "tools/site_tool.py", "requirements.txt"]  # what changes the output


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

def generated_config(index, version, channel, has_release):
    """The part of the Zensical configuration that comes from the export index."""
    return {
        "nav": [{"Home": "index.md"}] + index["nav"],
        "extra": {
            "version": {"provider": "mike", "default": "latest", "alias": True},
            "tapyr": {
                "version": version,
                "channel": channel,
                "ref": index["ref"],
                "has_release": has_release,
                "sources": {page_url(p["path"]): p["source"] for p in index["pages"]},
            },
        },
    }


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
    extra = generated_config(index, version, channel, has_release)
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
        self.ids, self.links = set(), []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if a.get("id"):
            self.ids.add(a["id"])
        if tag == "a" and a.get("name"):
            self.ids.add(a["name"])
        if tag == "a" and "href" in a and SKIP_CLASS not in (a.get("class") or "").split():
            self.links.append(a["href"])


def check_links(site_dir):
    """Problems with internal links: target page missing, outside the site, or id missing."""
    root = Path(site_dir).resolve()
    pages = {}
    for f in sorted(root.rglob("*.html")):
        p = _Page()
        p.feed(f.read_text(errors="replace"))
        pages[f] = p
    problems = []
    for f, page in pages.items():
        for href in page.links:
            parts = urlsplit(href)
            if parts.scheme or parts.netloc or href.startswith("//"):
                continue
            if not parts.path:
                target = f
            elif parts.path.startswith("/"):  # 404.html links from the site root
                target = (root / unquote(parts.path).lstrip("/")).resolve()
            else:
                target = (f.parent / unquote(parts.path)).resolve()
            if target.is_dir() or parts.path.endswith("/"):
                target = target / "index.html"
            where = f"{f.relative_to(root)}: {href}"
            if root not in target.parents and target != root:
                problems.append(f"{where}: leaves the site")
                continue
            if target not in pages:
                problems.append(f"{where}: no such page")
                continue
            if parts.fragment and unquote(parts.fragment) not in pages[target].ids:
                problems.append(f"{where}: no id #{parts.fragment}")
    return problems


# ---------- publish ----------

def publish(the_plan, tapyr, work, push, engine=None, run=subprocess.run, prepare=prepare, links=check_links):
    """Carries out a plan: each build is exported, built in strict mode, link checked,
    then deployed with mike to the gh-pages branch of this repository."""
    target = ["-r", "origin", "-b", "gh-pages"] + (["--push"] if push else [])
    has_release = the_plan["default"] == "latest"
    config = str(Path(work) / "mkdocs.yml")
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
    if not (the_plan["hide"] or the_plan["unhide"] or the_plan["set_default"]):
        return
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
    elif args.cmd == "check-links":
        problems = check_links(args.site)
        for line in problems:
            print(line, file=sys.stderr)
        if problems:
            sys.exit(f"{len(problems)} broken internal links")
        print(f"internal links of {args.site}: all resolve")


if __name__ == "__main__":
    main()
