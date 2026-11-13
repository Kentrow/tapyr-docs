"""Tests of tools/site.py. Run: python -m unittest discover -s tools"""

import json
import re
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path

import site_tool as site


class PlanTest(unittest.TestCase):
    def test_before_any_release_only_dev_is_built_and_is_the_default(self):
        p = site.plan(tags=["v0.1.0", "v0.1.0-rc.1"], main_sha="abc", deployed=[])
        self.assertEqual(p["builds"], [{"version": "dev", "ref": "main", "source": "abc", "aliases": []}])
        self.assertEqual(p["default"], "dev")
        self.assertEqual(p["hide"], [])

    def test_latest_stable_tag_of_each_minor_from_v0_1_1(self):
        tags = ["v0.1.0", "v0.1.1", "v0.1.2", "v0.1.3-rc.1", "v0.2.0-rc.1"]
        p = site.plan(tags=tags, main_sha="abc", deployed=[])
        self.assertEqual(p["builds"][0], {"version": "0.1", "ref": "v0.1.2", "source": "v0.1.2", "aliases": ["latest"]})
        self.assertEqual(p["default"], "latest")

    def test_nothing_to_build_when_deployed_sources_match(self):
        deployed = [
            {"version": "0.1", "aliases": ["latest"], "properties": {"source": "v0.1.2"}},
            {"version": "dev", "aliases": [], "properties": {"source": "abc", "has_release": True}},
        ]
        p = site.plan(tags=["v0.1.1", "v0.1.2"], main_sha="abc", deployed=deployed)
        self.assertEqual(p["builds"], [])

    def test_a_new_patch_rebuilds_its_minor_only(self):
        deployed = [
            {"version": "0.2", "aliases": ["latest"], "properties": {"source": "v0.2.0"}},
            {"version": "0.1", "aliases": [], "properties": {"source": "v0.1.2"}},
            {"version": "dev", "aliases": [], "properties": {"source": "abc", "has_release": True}},
        ]
        p = site.plan(tags=["v0.1.2", "v0.1.3", "v0.2.0"], main_sha="abc", deployed=deployed)
        self.assertEqual(p["builds"], [{"version": "0.1", "ref": "v0.1.3", "source": "v0.1.3", "aliases": []}])

    def test_a_new_minor_takes_the_latest_alias_and_main_moving_rebuilds_dev(self):
        deployed = [
            {"version": "0.1", "aliases": ["latest"], "properties": {"source": "v0.1.2"}},
            {"version": "dev", "aliases": [], "properties": {"source": "old"}},
        ]
        p = site.plan(tags=["v0.1.2", "v0.2.0"], main_sha="new", deployed=deployed)
        self.assertEqual([b["version"] for b in p["builds"]], ["0.2", "dev"])
        self.assertEqual(p["builds"][0]["aliases"], ["latest"])

    def test_dev_is_rebuilt_when_the_first_release_appears_so_its_banner_links_it(self):
        deployed = [{"version": "dev", "aliases": [], "properties": {"source": "abc", "has_release": False}}]
        p = site.plan(tags=["v0.1.1"], main_sha="abc", deployed=deployed, deployed_default="dev")
        self.assertEqual([b["version"] for b in p["builds"]], ["0.1", "dev"])

    def test_a_change_of_the_engine_rebuilds_every_version(self):
        deployed = [
            {"version": "0.1", "aliases": ["latest"], "properties": {"source": "v0.1.1", "engine": "old"}},
            {"version": "dev", "aliases": [], "properties": {"source": "abc", "has_release": True, "engine": "old"}},
        ]
        p = site.plan(tags=["v0.1.1"], main_sha="abc", deployed=deployed, deployed_default="latest", engine="new")
        self.assertEqual([b["version"] for b in p["builds"]], ["0.1", "dev"])
        same = site.plan(tags=["v0.1.1"], main_sha="abc", deployed=deployed, deployed_default="latest", engine="old")
        self.assertEqual(same["builds"], [])

    def test_the_selector_keeps_the_last_three_minors(self):
        tags = ["v0.1.1", "v0.2.0", "v0.3.0", "v0.4.0", "v1.0.0"]
        p = site.plan(tags=tags, main_sha="abc", deployed=[])
        self.assertEqual(p["hide"], ["0.2", "0.1"])
        self.assertEqual(p["unhide"], [])

    def test_already_hidden_minors_are_left_alone_and_shown_ones_unhidden(self):
        deployed = [
            {"version": v, "aliases": ["latest"] if v == "1.0" else [],
             "properties": {"source": f"v{v}.1", **({"hidden": True} if v in ("0.1", "0.3") else {})}}
            for v in ("1.0", "0.4", "0.3", "0.2", "0.1")
        ] + [{"version": "dev", "aliases": [], "properties": {"source": "abc", "has_release": True}}]
        tags = ["v0.1.1", "v0.2.1", "v0.3.1", "v0.4.1", "v1.0.1"]
        p = site.plan(tags=tags, main_sha="abc", deployed=deployed, deployed_default="latest")
        self.assertEqual(p["builds"], [])
        self.assertEqual(p["hide"], ["0.2"])
        self.assertEqual(p["unhide"], ["0.3"])
        self.assertIsNone(p["set_default"])

    def test_the_default_moves_from_dev_to_latest_with_the_first_release(self):
        p = site.plan(tags=["v0.1.1"], main_sha="abc", deployed=[], deployed_default="dev")
        self.assertEqual(p["set_default"], "latest")

    def test_minors_sort_by_number_not_text(self):
        p = site.plan(tags=["v0.9.0", "v0.10.0"], main_sha="abc", deployed=[])
        self.assertEqual(p["builds"][0]["version"], "0.10")
        self.assertEqual(p["builds"][0]["aliases"], ["latest"])


class PublishTest(unittest.TestCase):
    def run_publish(self, the_plan, push):
        calls = []

        def run(cmd, **kw):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 1)  # git diff --quiet: there are changes

        site.publish(
            the_plan, tapyr="/t", work="/w", push=push, engine="e1",
            run=run,
            prepare=lambda *a, **kw: calls.append(["prepare", *a, kw.get("has_release")]),
            links=lambda d: [],
        )
        return calls

    def test_each_build_is_checked_out_built_strictly_checked_then_deployed(self):
        the_plan = {"builds": [{"version": "0.1", "ref": "v0.1.1", "source": "v0.1.1", "aliases": ["latest"]}],
                    "default": "latest", "set_default": "latest", "hide": ["0.0"], "unhide": []}
        calls = self.run_publish(the_plan, push=True)
        self.assertEqual(calls[0], ["git", "-C", "/t", "checkout", "--quiet", "--detach", "v0.1.1"])
        self.assertEqual(calls[1][0], "prepare")
        self.assertTrue(calls[1][-1])  # has_release
        self.assertEqual(calls[2], ["zensical", "build", "--strict", "-f", "/w/mkdocs.yml"])
        deploy = calls[3]
        self.assertEqual(deploy[:2], ["mike", "deploy"])
        self.assertIn("--update-aliases", deploy)
        self.assertIn("source=v0.1.1", deploy)
        self.assertIn("has_release=true", deploy)
        self.assertIn("engine=e1", deploy)
        self.assertIn("--push", deploy)
        self.assertEqual(deploy[-2:], ["0.1", "latest"])
        props = next(c for c in calls if c[:2] == ["mike", "props"])
        self.assertEqual(props[-3:], ["--set", "hidden=true", "0.0"])
        self.assertIn("-F", props)  # mike reads a configuration even when it builds nothing
        self.assertIn("--push", props)
        default = next(c for c in calls if c[:2] == ["mike", "set-default"])
        self.assertEqual(default[-1], "latest")
        self.assertIn("-F", default)

    def test_dev_builds_from_origin_main_and_nothing_is_pushed_in_a_dry_run(self):
        the_plan = {"builds": [{"version": "dev", "ref": "main", "source": "abc", "aliases": []}],
                    "default": "dev", "set_default": None, "hide": [], "unhide": []}
        calls = self.run_publish(the_plan, push=False)
        self.assertEqual(calls[0][-1], "origin/main")
        self.assertFalse(calls[1][-1])
        self.assertFalse(any("--push" in c for c in calls if isinstance(c, list)))

    def test_a_broken_link_stops_before_deploying(self):
        the_plan = {"builds": [{"version": "dev", "ref": "main", "source": "abc", "aliases": []}],
                    "default": "dev", "set_default": None, "hide": [], "unhide": []}
        calls = []
        with self.assertRaises(SystemExit):
            site.publish(the_plan, tapyr="/t", work="/w", push=True,
                         run=lambda cmd, **kw: calls.append(cmd),
                         prepare=lambda *a, **kw: None, links=lambda d: ["x: no such page"])
        self.assertFalse(any(c[:2] == ["mike", "deploy"] for c in calls))


    def test_robots_txt_at_the_root_points_at_the_sitemap_of_the_default_version(self):
        the_plan = {"builds": [], "default": "latest", "set_default": None, "hide": [], "unhide": []}
        written = {}
        calls = []

        def run(cmd, **kw):
            calls.append(cmd)
            if cmd[-2] == "add":
                written[cmd[-1]] = (Path(cmd[2]) / cmd[-1]).read_bytes()
            return subprocess.CompletedProcess(cmd, 1)

        site.publish(the_plan, tapyr="/t", work="/w", push=True, run=run,
                     prepare=lambda *a, **kw: None, links=lambda d: [])
        self.assertEqual(written["robots.txt"], b"User-agent: *\nAllow: /\n\nSitemap: https://docs.tapyr.eu/latest/sitemap.xml\n")
        self.assertEqual(sorted(written), ["404.html", "favicon-96.png", "favicon.ico", "favicon.svg", "index.html", "og.png", "robots.txt"])
        add = next(c for c in calls if c[1:3] == ["worktree", "add"] or c[3:5] == ["worktree", "add"])
        self.assertEqual(add[-1], "gh-pages")
        self.assertTrue(any(c[-2:] == ["origin", "gh-pages"] and "push" in c for c in calls))

    def test_the_root_page_redirects_to_the_default_version_and_names_the_icons(self):
        # Search engines take the icon of a site from its home page, the root redirect here.
        for default in ["latest", "dev"]:
            index = site.root_files(default)["index.html"]
            self.assertEqual(re.search(r'url=([^/"]+)/', index).group(1), default)  # read back by deployed_state
            self.assertIn(f'window.location.replace(\n      "{default}/"', index)
            self.assertIn('<link rel="icon" href="favicon.svg" type="image/svg+xml">', index)
            self.assertIn('<link rel="icon" href="favicon-96.png" sizes="96x96" type="image/png">', index)

    def test_an_unknown_url_gets_a_page_of_the_site_that_leads_to_the_latest_version(self):
        # GitHub Pages serves /404.html for every URL outside a version, at any depth.
        page = site.root_files("latest")["404.html"]
        self.assertIn("<title>Page not found - Tapyr docs</title>", page)
        self.assertIn('<meta name="robots" content="noindex">', page)
        self.assertIn('<a href="/latest/">', page)
        self.assertIn('<link rel="icon" href="/favicon.svg" type="image/svg+xml">', page)
        self.assertNotIn('href="favicon', page)
        self.assertIn('<a href="/dev/">', site.root_files("dev")["404.html"])

    def test_the_image_of_shared_links_sits_at_the_root(self):
        image = site.root_files("latest")["og.png"]
        self.assertTrue(image.startswith(b"\x89PNG"))
        self.assertEqual(struct.unpack(">II", image[16:24]), (1200, 630))

    def test_the_icons_at_the_root_are_those_of_the_theme(self):
        files = site.root_files("latest")
        images = Path(__file__).parent.parent / "theme/assets/images"
        for name in ["favicon.svg", "favicon-96.png", "favicon.ico"]:
            self.assertEqual(files[name], (images / name).read_bytes(), name)
        self.assertTrue(files["favicon.ico"].startswith(b"\x00\x00\x01\x00"))
        self.assertTrue(files["favicon-96.png"].startswith(b"\x89PNG"))

    def test_robots_txt_is_neither_committed_nor_pushed_when_unchanged_or_in_a_dry_run(self):
        the_plan = {"builds": [], "default": "latest", "set_default": None, "hide": [], "unhide": []}
        for changed, push in [(False, True), (True, False)]:
            calls = []

            def run(cmd, **kw):
                calls.append(cmd)
                return subprocess.CompletedProcess(cmd, 1 if changed else 0)

            site.publish(the_plan, tapyr="/t", work="/w", push=push, run=run,
                         prepare=lambda *a, **kw: None, links=lambda d: [])
            self.assertFalse(any("push" in c for c in calls), (changed, push))
            self.assertEqual(any("commit" in c for c in calls), changed, (changed, push))


class DescriptionTest(unittest.TestCase):
    def test_the_first_paragraph_after_the_title_as_plain_text(self):
        md = "# Concepts\n\nTapyr polls the [APIs](x.md) of `vendor`\n**platforms**.\n\n## More\n\nNot this.\n"
        self.assertEqual(site.description(md), "Tapyr polls the APIs of vendor platforms.")

    def test_comments_headings_lists_tables_code_and_html_are_skipped(self):
        md = ("<!-- Generated. Do not edit. -->\n\n# ADR 0001\n\n- Status: accepted\n- Date: 2026\n\n"
              "| a | b |\n|---|---|\n\n```sh\ntapyr version\n```\n\n<div class=\"x\">\n\n## Context\n\n"
              "Tapyr is a long-running collector.\n")
        self.assertEqual(site.description(md), "Tapyr is a long-running collector.")

    def test_a_long_paragraph_is_cut_at_a_word_under_160_characters(self):
        md = "# T\n\n" + " ".join(["quota"] * 60) + "\n"
        d = site.description(md)
        self.assertLessEqual(len(d), 160)
        self.assertTrue(d.endswith("quota…"))

    def test_no_paragraph_gives_none(self):
        self.assertIsNone(site.description("# Reference\n\n```yaml\na: 1\n```\n"))

    def test_descriptions_of_the_pages_keyed_by_url(self):
        docs = Path(tempfile.mkdtemp())
        (docs / "index.md").write_text("# Home\n\nThe home.\n")
        (docs / "providers").mkdir()
        (docs / "providers" / "index.md").write_text("# Providers\n\nFour packages.\n")
        (docs / "cli.md").write_text("# CLI\n\n| a |\n")
        index = {"pages": [{"path": "providers/index.md"}, {"path": "cli.md"}]}
        self.assertEqual(site.page_descriptions(docs, index), {"": "The home.", "providers/": "Four packages."})


class LinksTest(unittest.TestCase):
    def test_a_missing_or_empty_site_is_a_problem_not_a_pass(self):
        empty = Path(tempfile.mkdtemp())
        for d in [empty, empty / "missing"]:
            self.assertEqual(site.check_links(d), [f"{d.resolve()}: no HTML page"])

    def site(self, files):
        root = Path(tempfile.mkdtemp())
        for name, html in files.items():
            f = root / name
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(html)
        return root

    def test_links_to_existing_pages_and_ids_pass(self):
        root = self.site({
            "index.html": '<a href="install/">x</a><a href="install/#docker">y</a><a href="#top">z</a><h1 id="top">t</h1>',
            "install/index.html": '<h2 id="docker">Docker</h2><a href="../">home</a>',
        })
        self.assertEqual(site.check_links(root), [])

    def test_missing_page_and_missing_id_are_reported(self):
        root = self.site({
            "index.html": '<a href="nope/">x</a><a href="install/#gone">y</a>',
            "install/index.html": '<h2 id="docker">Docker</h2>',
        })
        problems = site.check_links(root)
        self.assertEqual(len(problems), 2)
        self.assertIn("nope/", problems[0])
        self.assertIn("#gone", problems[1])

    def test_external_links_are_skipped(self):
        root = self.site({
            "index.html": '<a href="https://github.com/Kentrow/tapyr">gh</a><a href="mailto:a@b.c">m</a>',
        })
        self.assertEqual(site.check_links(root), [])

    def test_a_version_link_reaches_the_latest_version_next_to_the_site_from_any_depth(self):
        link = '<a class="tapyr-version-link" href="{}">latest</a>'
        root = self.site({
            "index.html": link.format("../latest/"),
            "install/index.html": link.format("../../latest/"),
            "providers/rms/index.html": link.format("../../../latest/"),
            "404.html": link.format("/../latest/"),
        })
        self.assertEqual(site.check_links(root), [])

    def test_a_version_link_that_stays_inside_the_version_is_reported(self):
        # "../latest/" from a page one level down is /<version>/latest/: a 404 once deployed
        link = '<a class="tapyr-version-link" href="../latest/">latest</a>'
        root = self.site({"index.html": "", "install/index.html": link, "providers/rms/index.html": link})
        problems = site.check_links(root)
        self.assertEqual(len(problems), 2)
        self.assertIn("install/index.html: ../latest/: not the latest version", problems[0])

    def test_root_absolute_links_resolve_from_the_site_root(self):
        root = self.site({
            "404.html": '<a href="/install/">i</a><a href="/gone/">g</a>',
            "install/index.html": '',
        })
        problems = site.check_links(root)
        self.assertEqual(len(problems), 1)
        self.assertIn("/gone/", problems[0])

    def test_a_link_leaving_the_site_is_reported(self):
        root = self.site({"index.html": '<a href="../elsewhere/">x</a>'})
        self.assertEqual(len(site.check_links(root)), 1)


class ConfigTest(unittest.TestCase):
    def test_home_comes_first_and_edit_sources_follow_the_index(self):
        index = {"ref": "v0.1.1", "nav": [{"Guides": ["install.md"]}],
                 "pages": [{"path": "install.md", "source": "docs/install.md", "title": "Install"},
                           {"path": "providers/meraki-dashboard/index.md", "source": "providers/meraki-dashboard/README.md", "title": "Meraki"}]}
        extra = site.generated_config(index, version="0.1", channel="release", has_release=True,
                                      descriptions={"install/": "How to install."})
        self.assertEqual(extra["extra"]["tapyr"]["descriptions"], {"install/": "How to install."})
        self.assertEqual(extra["nav"][0], {"Home": "index.md"})
        self.assertEqual(extra["nav"][1], {"Guides": ["install.md"]})
        # keyed by page URL: the templates see page.url, not the source file
        self.assertEqual(extra["extra"]["tapyr"]["sources"], {
            "install/": "docs/install.md",
            "providers/meraki-dashboard/": "providers/meraki-dashboard/README.md",
        })
        self.assertEqual(extra["extra"]["version"], {"provider": "mike", "default": "latest", "alias": True})
        # footer icons: the project site first, then the repository
        self.assertEqual([s["link"] for s in extra["extra"]["social"]], ["https://tapyr.eu", "https://github.com/Kentrow/tapyr"])
        json.dumps(extra)

    def test_released_pages_name_their_latest_url_as_canonical_and_dev_keeps_its_own(self):
        index = {"ref": "v0.1.2", "nav": [], "pages": []}
        release = site.generated_config(index, version="0.1", channel="release", has_release=True)
        # every minor, old ones included, sends search engines to /latest/: links to 0.1 count for the latest
        self.assertEqual(release["plugins"], [{"mike": {"canonical_version": "latest"}}])
        dev = site.generated_config(dict(index, ref="main"), version="dev", channel="dev", has_release=True)
        self.assertNotIn("plugins", dev)

    def test_the_generated_keys_are_not_in_the_base_configuration(self):
        # appended as top-level keys: one also in mkdocs.yml would be a duplicate
        base = (site.ROOT / "mkdocs.yml").read_text()
        index = {"ref": "v0.1.2", "nav": [], "pages": []}
        for key in site.generated_config(index, version="0.1", channel="release", has_release=True):
            self.assertNotRegex(base, rf"(?m)^{key}:")


class CommitsTest(unittest.TestCase):
    SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check-commits.sh"

    def repo(self, *messages):
        d = tempfile.mkdtemp()
        git = lambda *a: subprocess.run(["git", "-C", d, *a], check=True, capture_output=True)
        git("init", "-q", "-b", "main")
        git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "chore: base")
        for m in messages:
            git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", m)
        return d

    def check(self, repo, rev_range):
        return subprocess.run([str(self.SCRIPT), rev_range], cwd=repo, capture_output=True, text=True)

    def test_conventional_subjects_pass(self):
        r = self.check(self.repo("feat(seo): describe pages", "fix!: break", "build(deps): bump"), "HEAD~3..HEAD")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_other_subjects_and_ai_co_authors_fail(self):
        repo = self.repo("Update README", "feat: ok\n\nCo-Authored-By: Claude <noreply@anthropic.com>")
        r = self.check(repo, "HEAD~2..HEAD")
        self.assertEqual(r.returncode, 1)
        self.assertIn("does not follow Conventional Commits: Update README", r.stderr)
        self.assertIn("AI co-author trailers are not allowed", r.stderr)

    def test_an_unknown_range_fails_instead_of_checking_nothing(self):
        self.assertEqual(self.check(self.repo(), "origin/nope..HEAD").returncode, 2)


class ThemeTest(unittest.TestCase):
    def test_outdated_banner_script_resolves_its_base_against_the_page(self):
        # base_url is relative ("." or ".."): new URL() throws without a base, and the
        # banner of an older version then waits for the main script to show it. Fixed in
        # zensical-ui 0.0.42 (zensical/ui#306): the template used is the theme's own.
        import zensical_ui
        name = "partials/javascripts/outdated.html"
        self.assertFalse((Path(__file__).parent.parent / "theme/overrides" / name).exists())
        template = Path(zensical_ui.__file__).parent / "templates" / name
        self.assertIn('base=new URL("{{ base_url }}",location)', template.read_text())

    def test_banner_links_to_the_latest_version_are_relative_to_the_page(self):
        # A bare "../latest/" only works from the home page of a version.
        main = (Path(__file__).parent.parent / "theme/overrides/main.html").read_text()
        self.assertEqual(main.count("""href="{{ '../latest/' | url }}\""""), 2)
        self.assertNotIn("""href="{{ '../latest/' }}\"""", main)

    def test_pages_describe_themselves_to_link_previews(self):
        main = (Path(__file__).parent.parent / "theme/overrides/main.html").read_text()
        for tag in ['<meta property="og:type" content="article">', '<meta property="og:site_name" content="{{ config.site_name }}">',
                    '<meta property="og:image" content="https://docs.tapyr.eu/og.png">', '<meta name="twitter:card" content="summary_large_image">',
                    'property="og:title"', 'property="og:description"', 'property="og:url"']:
            self.assertIn(tag, main)

    def test_the_home_page_names_no_provider_so_it_fits_every_version(self):
        home = (Path(__file__).parent.parent / "home/index.md").read_text()
        for vendor in ["Aruba", "Meraki", "Teltonika", "Mist", "ThousandEyes"]:
            self.assertNotIn(vendor, home)

    def test_pages_name_a_png_icon_besides_the_svg(self):
        main = (Path(__file__).parent.parent / "theme/overrides/main.html").read_text()
        self.assertIn('<link rel="icon" href="{{ \'tapyr-theme/images/favicon-96.png\' | url }}" sizes="96x96" type="image/png">', main)


if __name__ == "__main__":
    unittest.main()
