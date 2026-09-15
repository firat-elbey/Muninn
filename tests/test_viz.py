"""Verify offline HTML, bounded Mermaid output, and skill rendering.

The HTML contains no external requests and safely encodes untrusted note
titles inside its script data. The Mermaid view remains bounded and can render
in a chat client. These tests use hand-written Markdown and do not require the
optional extractor.
"""

import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from muninn import cli, skill, viz  # noqa: E402
from muninn.dynamics import Dynamics  # noqa: E402
from muninn.export import export_graph  # noqa: E402
from muninn.store import Bundle  # noqa: E402


def _graph():
    """A small fused graph exercising every encoding: provenance tiers, a
    supersedes edge, a learned co-use edge, strengths."""
    return {
        "muninn_export": "0.1", "directed": True,
        "nodes": [
            {"id": "notes/a.md", "label": "Alpha", "provenance": "curated",
             "strength": 1.2},
            {"id": "notes/b.md", "label": "Beta", "provenance": "extracted",
             "strength": 0.4},
            {"id": "notes/c.md", "label": "Gamma", "provenance": "inferred",
             "strength": 0.0},
            {"id": "notes/old.md", "label": "Old fact",
             "provenance": "curated", "strength": 0.1},
        ],
        "links": [
            {"source": "notes/a.md", "target": "notes/b.md",
             "relation": "uses", "weight": 1.0, "kind": "typed"},
            {"source": "notes/b.md", "target": "notes/c.md",
             "relation": "related_to", "weight": 1.0, "kind": "link"},
            {"source": "notes/a.md", "target": "notes/c.md",
             "relation": "used_with", "weight": 0.66, "kind": "used_with"},
            {"source": "notes/a.md", "target": "notes/old.md",
             "relation": "supersedes", "weight": 1.0, "kind": "supersedes"},
        ],
    }


class TestHtml(unittest.TestCase):

    def test_self_contained_no_external_requests(self):
        html = viz.render_html(_graph())
        # nothing the browser could fetch: no external URLs, no src/href
        self.assertNotIn("https://", html)
        self.assertNotIn("http://", html)
        self.assertNotIn("src=", html)
        self.assertNotIn("href=", html)
        self.assertNotIn("@import", html)

    def test_graph_json_embeds_and_roundtrips(self):
        html = viz.render_html(_graph())
        m = re.search(r"const GRAPH = (.*);\n", html)
        self.assertIsNotNone(m)
        parsed = json.loads(m.group(1))  # \/ is a valid JSON escape
        self.assertEqual(len(parsed["nodes"]), 4)
        self.assertEqual(len(parsed["links"]), 4)

    def test_hostile_title_cannot_break_out_of_script(self):
        g = _graph()
        g["nodes"][0]["label"] = '</script><script>alert(1)</script>'
        g["nodes"][1]["label"] = '<!--<script>'   # script-data-escape trick
        html = viz.render_html(g, title="x</title><script>y</script>")
        # exactly one closing and one opening script tag: the template's own
        self.assertEqual(html.count("</script>"), 1)
        self.assertEqual(html.count("<script>"), 1)
        self.assertNotIn("<!--", html)
        self.assertNotIn("<script>y</script>", html)  # title is escaped
        # and the payload still roundtrips to the original labels
        m = re.search(r"const GRAPH = (.*);\n", html)
        parsed = json.loads(m.group(1))
        self.assertEqual(parsed["nodes"][0]["label"],
                         '</script><script>alert(1)</script>')

    def test_percent_formatting_left_js_intact(self):
        # a %-template regression guard: braces/percent in CSS+JS survived
        html = viz.render_html(_graph())
        self.assertIn("border-radius: 50%", html)
        self.assertIn("requestAnimationFrame(tick)", html)
        self.assertIn("mulberry32", html)

    def test_empty_graph_renders(self):
        html = viz.render_html({"nodes": [], "links": []})
        self.assertIn("const GRAPH", html)

    def test_template_tolerates_missing_keys_and_zoom_hit_fix(self):
        # regressions from the correctness review: a graph without
        # nodes/links keys must not TypeError in the page, unknown edge
        # kinds stay visible, and the hit radius no longer shrinks with zoom
        html = viz.render_html({})
        self.assertIn("(GRAPH.nodes || [])", html)
        self.assertIn("(GRAPH.links || [])", html)
        self.assertIn("=== false", html)        # only explicit false hides
        self.assertIn("n.r + 6 / scale", html)  # world radius + screen slop

    def test_write_html_creates_dirs_and_counts(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        out = os.path.join(d, "deep", "nested", "graph.html")
        path, n, e = viz.write_html(_graph(), out)
        self.assertTrue(os.path.exists(path))
        self.assertEqual((n, e), (4, 4))


class TestMermaid(unittest.TestCase):

    def test_top_cap_keeps_strongest_and_pulls_supersedes_partner(self):
        mm = viz.render_mermaid(_graph(), top=2)
        self.assertIn('m0["Alpha"]', mm)   # strongest first
        self.assertNotIn("Gamma", mm)      # capped out (no story-edge tie)
        # the kept correction's superseded partner is pulled IN so the
        # supersedes edge renders instead of an inexplicable island
        self.assertIn("Old fact ⊘", mm)
        self.assertIn("-->|supersedes|", mm)

    def test_edge_styles_by_kind(self):
        mm = viz.render_mermaid(_graph(), top=10)
        self.assertIn("-->|supersedes|", mm)   # the correction graph
        self.assertIn("-.-", mm)               # learned from use: dotted
        self.assertIn(" --- ", mm)             # authored: solid

    def test_superseded_marker_and_quote_escape(self):
        g = _graph()
        g["nodes"][3]["label"] = 'He said "no"'
        mm = viz.render_mermaid(g, top=10)
        self.assertIn("#quot;", mm)
        self.assertIn("⊘", mm)                 # superseded marker

    def test_long_labels_truncated(self):
        g = _graph()
        g["nodes"][0]["label"] = "x" * 200
        mm = viz.render_mermaid(g, top=10)
        line = next(ln for ln in mm.splitlines() if 'm0[' in ln)
        self.assertLess(len(line), 80)

    def test_empty_graph_yields_header_only(self):
        self.assertEqual(viz.render_mermaid({"nodes": [], "links": []})
                         .splitlines()[0], "graph LR")


class TestVizCLI(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        b = Bundle(self.root)
        b.write_note("notes/a.md", {"type": "note", "title": "Alpha"},
                     "links to [[Beta]]")
        b.write_note("notes/b.md", {"type": "note", "title": "Beta"}, "body")

    def _cli(self, *argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.main(list(argv))
        return buf.getvalue()

    def test_viz_writes_default_path_under_sidecar(self):
        out = self._cli("--root", self.root, "viz")
        expected = os.path.join(self.root, ".muninn", "graph.html")
        self.assertIn("graph.html", out)
        self.assertTrue(os.path.exists(expected))
        with open(expected, encoding="utf-8") as fh:
            self.assertIn("const GRAPH", fh.read())

    def test_viz_mermaid_prints_diagram(self):
        out = self._cli("--root", self.root, "viz", "--mermaid", "--top", "5")
        self.assertTrue(out.startswith("graph LR"))
        self.assertIn("Alpha", out)

    def test_viz_out_flag_overrides_path(self):
        target = os.path.join(self.root, "elsewhere.html")
        self._cli("--root", self.root, "viz", "--out", target)
        self.assertTrue(os.path.exists(target))

    def test_fused_graph_feeds_viz(self):
        # usage (co-use) edges from the sidecar reach the rendered graph
        d = Dynamics(self.root)
        d.touch("notes/a.md", session="s1")
        d.touch("notes/b.md", session="s1")
        g = export_graph(Bundle(self.root), d)
        kinds = {e["kind"] for e in g["links"]}
        self.assertIn("used_with", kinds)
        html = viz.render_html(g)
        self.assertIn("used_with", html)


class TestSkill(unittest.TestCase):

    def _cli(self, *argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.main(list(argv))
        return buf.getvalue()

    def test_skill_teaches_the_whole_loop(self):
        out = self._cli("skill")
        for verb in ("prime", "pack", "outcome", "goal", "build",
                     "--request", "--apply", "viz", "supersedes"):
            self.assertIn(verb, out)
        self.assertIn("name: muninn", out)       # skill frontmatter
        self.assertIn("provenance", out)          # the data-not-instructions rule

    def test_skill_substitutes_root(self):
        out = self._cli("--root", "/srv/team-kb", "skill")
        self.assertIn("/srv/team-kb", out)
        self.assertNotIn("<kb>", out)

    def test_skill_default_keeps_placeholder(self):
        out = self._cli("skill")
        self.assertIn("<kb>", out)

    def test_render_is_pure(self):
        self.assertEqual(skill.render(), skill.SKILL_MD)
        self.assertIn("/x/y", skill.render("/x/y"))

    def test_generated_commands_quote_concrete_paths(self):
        for root in ("/tmp/shared memory", "/tmp/Firat's memory",
                     "/tmp/$(printf altered);memory", "/tmp/<kb> memory"):
            with self.subTest(root=root):
                rendered = skill.render(root)
                commands = [line.strip() for line in rendered.splitlines()
                            if line.startswith("    muninn ")]
                for command in commands:
                    argv = shlex.split(command)
                    self.assertEqual(argv[:3], ["muninn", "--root", root])
                    if "--apply" in argv:
                        self.assertEqual(argv[argv.index("--apply") + 1],
                                         root + "/response.json")
                        self.assertEqual(argv[argv.index("--into") + 1], root)
                prime = next(command for command in commands
                             if command.endswith(" prime"))
                result = subprocess.run(
                    ["/bin/sh", "-c", 'muninn() { printf "%s" "$2"; }\n' + prime],
                    text=True, capture_output=True, check=False)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, root)


if __name__ == "__main__":
    unittest.main(verbosity=2)
