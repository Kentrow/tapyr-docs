"""Tests of tools/site.py. Run: python -m unittest discover -s tools"""

import json
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
            {"version": "dev", "aliases": [], "properties": {"source": "abc"}},
        ]
        p = site.plan(tags=["v0.1.1", "v0.1.2"], main_sha="abc", deployed=deployed)
        self.assertEqual(p["builds"], [])

    def test_a_new_patch_rebuilds_its_minor_only(self):
        deployed = [
            {"version": "0.2", "aliases": ["latest"], "properties": {"source": "v0.2.0"}},
            {"version": "0.1", "aliases": [], "properties": {"source": "v0.1.2"}},
            {"version": "dev", "aliases": [], "properties": {"source": "abc"}},
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
        ] + [{"version": "dev", "aliases": [], "properties": {"source": "abc"}}]
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
        site.publish(
            the_plan, tapyr="/t", work="/w", push=push,
            run=lambda cmd, **kw: calls.append(cmd),
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
        self.assertIn("--push", deploy)
        self.assertEqual(deploy[-2:], ["0.1", "latest"])
        self.assertIn(["mike", "props", "-r", "origin", "-b", "gh-pages", "--push", "--set", "hidden=true", "0.0"], calls)
        self.assertEqual(calls[-1], ["mike", "set-default", "-r", "origin", "-b", "gh-pages", "--push", "latest"])

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


class LinksTest(unittest.TestCase):
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

    def test_external_links_and_version_links_are_skipped(self):
        root = self.site({
            "index.html": '<a href="https://github.com/Kentrow/tapyr">gh</a><a href="mailto:a@b.c">m</a>'
                          '<a class="tapyr-version-link" href="../latest/">latest</a>',
        })
        self.assertEqual(site.check_links(root), [])

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
        extra = site.generated_config(index, version="0.1", channel="release", has_release=True)
        self.assertEqual(extra["nav"][0], {"Home": "index.md"})
        self.assertEqual(extra["nav"][1], {"Guides": ["install.md"]})
        # keyed by page URL: the templates see page.url, not the source file
        self.assertEqual(extra["extra"]["tapyr"]["sources"], {
            "install/": "docs/install.md",
            "providers/meraki-dashboard/": "providers/meraki-dashboard/README.md",
        })
        self.assertEqual(extra["extra"]["version"], {"provider": "mike", "default": "latest", "alias": True})
        json.dumps(extra)


if __name__ == "__main__":
    unittest.main()
