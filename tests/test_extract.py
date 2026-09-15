"""Verify deterministic structural extraction and optional dependencies.

Grammar-dependent tests run only when tree-sitter is available. The remaining
tests verify that unsupported files, missing grammars, and absent dependencies
produce no exception. Coverage includes supported document and source forms,
provenance, typed relationships, idempotent import, and curated-note
ownership.
"""

import io
import os
import shutil
import sys
import tempfile
import unittest
from collections import Counter
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from muninn import cli, extract  # noqa: E402
from muninn.store import Bundle  # noqa: E402

HAVE_TS = extract.available()


def _write(text: str, ext: str) -> str:
    fd, path = tempfile.mkstemp(suffix=ext)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


def _extract(text: str, ext: str) -> dict:
    path = _write(text, ext)
    try:
        return extract.extract_file(path)
    finally:
        os.unlink(path)


@unittest.skipUnless(HAVE_TS, "tree-sitter not installed")
class TestGrammars(unittest.TestCase):

    def _labels(self, graph):
        return {n["label"] for n in graph["nodes"]}

    def _kinds(self, graph):
        return Counter(n["kind"] for n in graph["nodes"])

    def _rels(self, graph):
        return Counter(e["relation"] for e in graph["links"])

    # -- markdown -----------------------------------------------------------

    def test_markdown_headings_hierarchy_and_links(self):
        md = ("# Top\n\nintro with [a doc](notes/x.md) and [[Beta]] here.\n\n"
              "## Child\n\nchild body\n")
        g = _extract(md, ".md")
        self.assertIn("Top", self._labels(g))
        self.assertIn("Child", self._labels(g))
        self.assertEqual(self._kinds(g)["heading"], 2)
        # parent-section contains child-section
        by_id = {n["id"]: n["label"] for n in g["nodes"]}
        contains = [(by_id[e["source"]], by_id.get(e["target"], e["target"]))
                    for e in g["links"] if e["relation"] == "contains"]
        self.assertIn(("Top", "Child"), contains)
        # md link + wikilink in Top's prose become references
        refs = {e["target"] for e in g["links"] if e["relation"] == "references"}
        self.assertIn("notes/x.md", refs)
        self.assertIn("Beta", refs)

    def test_markdown_reference_links_are_structural_references(self):
        md = (
            "# Top\n\n[full][one], [collapsed][], and [shortcut].\n\n"
            "[one]: notes/full.md\n"
            "[collapsed]: notes/collapsed.md\n"
            "[shortcut]:\n  notes/shortcut.md\n  \"Shortcut\"\n"
        )
        graph = _extract(md, ".md")
        refs = {
            edge["target"]
            for edge in graph["links"]
            if edge["relation"] == "references"
        }
        self.assertEqual(
            refs,
            {"notes/full.md", "notes/collapsed.md", "notes/shortcut.md"},
        )

    def test_markdown_body_captures_prose(self):
        g = _extract("# H\n\nfirst para\n\nsecond para\n", ".md")
        body = g["nodes"][0]["body"]
        self.assertIn("first para", body)
        self.assertIn("second para", body)

    def test_mixed_setext_and_atx_headings_keep_their_own_evidence(self):
        graph = _extract(
            "# Manual\n\nIntroduction.\n\n## Restart\n\nRestart the service.\n\n"
            "```sh\nrestart-example\n```\n\nLimits\n------\n\n"
            "Only synthetic data is allowed. [Policy](policy.md)\n\n"
            "### Detail\n\nRetain the limit.\n\nNext\n====\n\nNext chapter.\n", ".md")
        notes = {node["label"]: node for node in graph["nodes"]}
        self.assertEqual(set(notes), {"Manual", "Restart", "Limits", "Detail", "Next"})
        self.assertIn("restart-example", notes["Restart"]["body"])
        self.assertNotIn("synthetic", notes["Restart"]["body"])
        self.assertIn("Only synthetic data", notes["Limits"]["body"])
        by_id = {node["id"]: node["label"] for node in graph["nodes"]}
        contains = {(by_id[edge["source"]], by_id[edge["target"]])
                    for edge in graph["links"] if edge["relation"] == "contains"}
        self.assertEqual(contains, {("Manual", "Restart"), ("Manual", "Limits"),
                                    ("Limits", "Detail")})
        self.assertTrue(any(edge["source"] == notes["Limits"]["id"]
                            and edge["target"] == "policy.md"
                            for edge in graph["links"]))

    def test_multiline_setext_title_retains_every_line(self):
        graph = _extract("Storage\nlimits\n------\n\nKeep this restriction.\n", ".md")
        self.assertEqual(graph["nodes"][0]["label"], "Storage limits")
        self.assertIn("Keep this restriction.", graph["nodes"][0]["body"])

    # -- rst (Sphinx docs: the knowledge in most Python repos) --------------

    def test_rst_sections_hierarchy_and_bodies(self):
        rst = ("The App Context\n===============\n\n"
               "It tracks application-level data during a request.\n\n"
               "Purpose of the Context\n----------------------\n\n"
               "The purpose paragraph, mentioning teardown.\n\n"
               "See :doc:`reqcontext` for the request side.\n")
        g = _extract(rst, ".rst")
        self.assertIn("The App Context", self._labels(g))
        self.assertIn("Purpose of the Context", self._labels(g))
        self.assertEqual(self._kinds(g)["heading"], 2)
        top = next(n for n in g["nodes"] if n["label"] == "The App Context")
        self.assertIn("application-level data", top["body"])
        sub = next(n for n in g["nodes"]
                   if n["label"] == "Purpose of the Context")
        self.assertIn("purpose paragraph", sub["body"])
        by_id = {n["id"]: n["label"] for n in g["nodes"]}
        contains = [(by_id[e["source"]], by_id.get(e["target"], e["target"]))
                    for e in g["links"] if e["relation"] == "contains"]
        self.assertIn(("The App Context", "Purpose of the Context"), contains)
        # a :doc:`target` role becomes a references edge
        refs = {e["target"] for e in g["links"]
                if e["relation"] == "references"}
        self.assertIn("reqcontext", refs)

    def test_rst_body_stays_prose_not_markup(self):
        rst = ("Title\n=====\n\nreal prose here.\n\n"
               ".. code-block:: python\n\n   x = 1\n\n"
               "more prose after the directive.\n")
        g = _extract(rst, ".rst")
        body = next(n for n in g["nodes"] if n["label"] == "Title")["body"]
        self.assertIn("real prose here.", body)
        self.assertIn("more prose", body)
        # the adornment line is markup, not content
        self.assertNotIn("=====", body)

    def test_rst_roles_unwrap_to_their_text(self):
        # field-tested on Flask: bodies full of :func:`~flask.cli.x` force
        # the model to read through Sphinx plumbing; unwrap the role to
        # what a reader sees (the ~ form shows only the last segment)
        rst = ("Title\n=====\n\n"
               "Use :func:`~flask.cli.with_appcontext` or "
               ":meth:`Flask.cli`: see :doc:`CLI Guide <cli>` and "
               ":data:`~flask.g`.\n")
        g = _extract(rst, ".rst")
        body = next(n for n in g["nodes"] if n["label"] == "Title")["body"]
        self.assertIn("with_appcontext", body)
        self.assertIn("Flask.cli", body)
        self.assertIn("CLI Guide", body)
        for markup in (":func:", ":meth:", ":doc:", "`~", "<cli>"):
            self.assertNotIn(markup, body)
        # the :doc: reference edge still comes from the RAW text
        refs = {e["target"] for e in g["links"]
                if e["relation"] == "references"}
        self.assertIn("cli", refs)

    def test_generic_patterns_and_specifiers_are_not_definitions(self):
        # field-tested on ripgrep: substring kind-matching turned
        # `mutable_specifier` into a "table" and `tuple_struct_pattern`
        # match arms into "structs": Some/Err/None/mut as index landmarks
        rs = ("fn run(mut args: Args) -> Result<u64> {\n"
              "    let mut count = 0;\n"
              "    match args.next() {\n"
              "        Some(x) => { count += 1; }\n"
              "        None => {}\n"
              "        Err(e) => return Err(e),\n"
              "    }\n    Ok(count)\n}\n")
        labels = self._labels(_extract(rs, ".rs"))
        self.assertIn("run", labels)
        for junk in ("mut", "Some", "None", "Err"):
            self.assertNotIn(junk, labels)

    def test_generic_fields_are_not_individual_notes(self):
        # field-tested on gson (28k notes) / ripgrep (9k): every struct or
        # class field became its own note: stubs that flood the bundle;
        # the struct itself is the unit of knowledge
        rs = ("struct Config {\n    doc_short: String,\n"
              "    doc_long: String,\n    is_switch: bool,\n}\n")
        g = _extract(rs, ".rs")
        self.assertIn("Config", self._labels(g))
        self.assertNotIn("doc_short", self._labels(g))
        self.assertEqual(self._kinds(g).get("field", 0), 0)

    def test_per_file_node_cap_bounds_fixture_explosions(self):
        # field-tested on redis: 447 JSON test fixtures became 260k+
        # config-key notes (94% stubs). One data file must never flood
        # the bundle: its contribution is capped, earlier files intact.
        big = "{\n" + ",\n".join(f'"k{i}": {{"a": 1, "b": 2}}'
                                 for i in range(300)) + "\n}"
        path = _write(big, ".json")
        try:
            g = extract.extract_file(path)
        finally:
            os.unlink(path)
        self.assertLessEqual(len(g["nodes"]), extract.MAX_FILE_NODES)
        # no dangling edges to dropped nodes
        ids = {n["id"] for n in g["nodes"]}
        for e in g["links"]:
            if str(e.get("source", "")).startswith("json:"):
                self.assertIn(e["source"], ids)

    def test_generic_skips_single_char_and_numeric_labels(self):
        # field-tested on redis: bash test helpers surfaced "0", "r", "1"
        # as the bundle's best-connected landmarks
        sh = "r() {\n  echo hi\n}\nlongname() {\n  echo ok\n}\n"
        labels = self._labels(_extract(sh, ".sh"))
        self.assertIn("longname", labels)
        self.assertNotIn("r", labels)

    def test_duplicate_headings_across_files_get_their_doc_stem(self):
        # field-tested on OWASP: 146 cheat sheets each have an
        # "Introduction": as landmarks they are indistinguishable
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        b = Bundle(root)
        graph = {"nodes": [
            {"id": "m1", "label": "Introduction", "kind": "heading",
             "lang": "markdown", "body": "about sql injection",
             "source_file": "cheatsheets/SQL_Injection.md"},
            {"id": "m2", "label": "Introduction", "kind": "heading",
             "lang": "markdown", "body": "about csrf",
             "source_file": "cheatsheets/CSRF_Prevention.md"},
            {"id": "m3", "label": "Unique Section", "kind": "heading",
             "lang": "markdown", "body": "one of a kind",
             "source_file": "cheatsheets/CSRF_Prevention.md"},
        ], "links": []}
        extract.import_graph(b, graph)
        titles = {n.title for n in Bundle(root).notes.values()}
        self.assertIn("Introduction (SQL_Injection)", titles)
        self.assertIn("Introduction (CSRF_Prevention)", titles)
        self.assertIn("Unique Section", titles)  # unique stays clean

    def test_import_graph_tags_test_sources(self):
        # field-tested on gin/gson/sinatra: test classes lexically mirror
        # every how-do-I question and swamp production code: importers
        # tag test-origin notes so recall can demote (never hide) them
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        b = Bundle(root)
        graph = {"nodes": [
            {"id": "n1", "label": "BindJSON", "kind": "function",
             "lang": "go", "source_file": "binding/binding.go"},
            {"id": "n2", "label": "TestBindJSON", "kind": "function",
             "lang": "go", "source_file": "binding/binding_test.go"},
            {"id": "n3", "label": "CustomTypeAdaptersTest", "kind": "class",
             "lang": "java",
             "source_file": "gson/src/test/java/CustomTypeAdaptersTest.java"},
        ], "links": []}
        extract.import_graph(b, graph)
        b = Bundle(root)
        by_title = {n.title: n for n in b.notes.values()}
        self.assertNotIn("test", by_title["BindJSON"].tags)
        self.assertIn("test", by_title["TestBindJSON"].tags)
        self.assertIn("test", by_title["CustomTypeAdaptersTest"].tags)

    def test_generic_labels_shed_trailing_brace_junk(self):
        # field-tested on Flask: MULTI-LINE css rules fall to the head-line
        # label fallback and read "a {" / "body {": index noise
        g = _extract("a {\n  color: #377ba8;\n}\n\nnav h1 {\n  margin: 0;\n}\n",
                     ".css")
        labels = self._labels(g)
        self.assertIn("a", labels)
        self.assertIn("nav h1", labels)
        self.assertFalse(any(lb.rstrip().endswith("{") for lb in labels))

    # -- yaml / json (config-as-graph) --------------------------------------

    def test_yaml_keys_contains_and_scalars(self):
        y = "database:\n  host: localhost\n  port: 5432\ndebug: true\n"
        g = _extract(y, ".yaml")
        labels = self._labels(g)
        self.assertLessEqual({"database", "database.host", "database.port",
                              "debug"}, labels)
        host = next(n for n in g["nodes"] if n["label"] == "database.host")
        self.assertEqual(host["body"], "localhost")
        # nested key is a contains child of its parent
        by_id = {n["id"]: n["label"] for n in g["nodes"]}
        contains = [(by_id[e["source"]], by_id[e["target"]])
                    for e in g["links"] if e["relation"] == "contains"]
        self.assertIn(("database", "database.host"), contains)

    def test_yaml_parent_inlines_scalar_children(self):
        # a parent key note used to be a connections-only stub; its body now
        # carries the values so a pack that loads it can actually answer
        y = "database:\n  host: localhost\n  port: 5432\n  pool:\n    max: 20\n"
        g = _extract(y, ".yaml")
        parent = next(n for n in g["nodes"] if n["label"] == "database")
        self.assertIn("host: localhost", parent["body"])
        self.assertIn("port: 5432", parent["body"])
        self.assertNotIn("max: 20", parent["body"])  # nested, not a direct child

    def test_toml_table_inlines_scalar_pairs(self):
        g = _extract('[db]\nhost = "x"\nport = 1\n', ".toml")
        table = next(n for n in g["nodes"] if n["kind"] == "table")
        self.assertIn("host: x", table["body"])

    def test_duplicate_import_targets_merge_into_one_note(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        for fn in ("a.py", "b.py"):
            with open(os.path.join(d, fn), "w") as fh:
                fh.write("import shared_helper\n")
        g, _ = extract.extract_path(d)
        extract.import_graph(Bundle(root), g)
        b = Bundle(root)
        helpers = [n for n in b.notes.values()
                   if n.meta.get("title") == "shared_helper"]
        self.assertEqual(len(helpers), 1)  # one note, not shared-helper-2.md

    def test_json_keys_contains_and_scalars(self):
        j = '{"name": "cfg", "server": {"host": "0.0.0.0", "port": 8080}}'
        g = _extract(j, ".json")
        labels = self._labels(g)
        self.assertLessEqual({"name", "server", "server.host", "server.port"},
                             labels)
        self.assertEqual(self._rels(g)["contains"], 2)

    def test_toml_tables_and_pairs(self):
        t = 'title = "cfg"\n\n[database]\nhost = "localhost"\nport = 5432\n'
        g = _extract(t, ".toml")
        self.assertEqual(self._kinds(g)["table"], 1)
        self.assertIn("database.host", self._labels(g))

    # -- sql (schema graph) -------------------------------------------------

    def test_sql_tables_columns_and_foreign_key(self):
        sql = (
            "CREATE TABLE users (\n  id INTEGER PRIMARY KEY,\n"
            "  email TEXT UNIQUE\n);\n\n"
            "CREATE TABLE orders (\n  id INTEGER PRIMARY KEY,\n"
            "  user_id INTEGER,\n"
            "  FOREIGN KEY (user_id) REFERENCES users(id)\n);\n")
        g = _extract(sql, ".sql")
        kinds = self._kinds(g)
        self.assertEqual(kinds["table"], 2)
        self.assertGreaterEqual(kinds["column"], 4)
        # the column's type lands in its body
        uid = next(n for n in g["nodes"] if n["label"] == "users.id")
        self.assertIn("INTEGER", uid["body"])
        # FK renders a references edge whose target is the referenced table
        refs = [e for e in g["links"] if e["relation"] == "references"]
        self.assertTrue(any(e["target"] == "users" for e in refs))

    # -- python (light structural pass) -------------------------------------

    def test_python_module_class_function_import(self):
        py = ("import os\nfrom a.b import c\n\n"
              "def top():\n    pass\n\n"
              "class Foo:\n    def m(self):\n        pass\n")
        g = _extract(py, ".py")
        kinds = self._kinds(g)
        self.assertEqual(kinds["module"], 1)
        self.assertEqual(kinds["function"], 1)
        self.assertEqual(kinds["class"], 1)
        self.assertEqual(kinds["method"], 1)
        self.assertGreaterEqual(kinds["import"], 2)
        self.assertIn("Foo.m", self._labels(g))  # methods are qualified
        rels = self._rels(g)
        self.assertGreaterEqual(rels["imports"], 2)
        self.assertGreaterEqual(rels["contains"], 3)

    def test_python_call_retains_its_relation_and_target(self):
        graph = _extract("def run():\n    return service.save_data()\n", ".py")
        calls = [edge for edge in graph["links"]
                 if edge["relation"] == "calls"]
        self.assertTrue(any(edge["target"] == "save_data" for edge in calls))

    # -- javascript / typescript / tsx (rich explicit pass) -----------------

    def test_typescript_react_module_component_class_arrow_import_export(self):
        # a realistic TS+React (.tsx) file: imports, an exported arrow-const
        # React component, a plain arrow-const function, an exported function,
        # an exported class with methods, and a bare `export { … }`
        tsx = (
            'import React from "react";\n'
            'import { useState } from "react";\n'
            'export const Button = (props) => {\n'
            '  return <button>{props.label}</button>;\n'
            '};\n'
            'const double = (x) => x * 2;\n'
            'export function calc(a, b) { return a + b; }\n'
            'export class Widget {\n'
            '  render() { return <Button label="x" />; }\n'
            '  tick() { return 1; }\n'
            '}\n'
            'export { double };\n'
        )
        g = _extract(tsx, ".tsx")
        kinds = self._kinds(g)
        self.assertEqual(kinds["module"], 1)
        self.assertGreaterEqual(kinds["import"], 2)   # two `from "react"`
        self.assertEqual(kinds["component"], 1)       # Button: Capitalized + JSX
        self.assertEqual(kinds["function"], 2)        # double (arrow) + calc
        self.assertEqual(kinds["class"], 1)           # Widget
        self.assertGreaterEqual(kinds["method"], 2)   # render + tick
        self.assertGreaterEqual(kinds["export"], 1)   # `export { double }`
        labels = self._labels(g)
        self.assertIn("Button", labels)
        self.assertIn("calc", labels)
        self.assertIn("Widget.render", labels)        # methods are qualified
        self.assertIn("react", labels)                # import label = module
        rels = self._rels(g)
        self.assertGreaterEqual(rels["imports"], 2)
        self.assertGreaterEqual(rels["contains"], 4)
        # every module→def / class→method edge is a `contains`
        by_id = {n["id"]: n["label"] for n in g["nodes"]}
        contains = {(by_id[e["source"]], by_id[e["target"]])
                    for e in g["links"] if e["relation"] == "contains"}
        self.assertIn(("Widget", "Widget.render"), contains)

    def test_jsx_grammar_gating_ts_vs_tsx(self):
        # SAME JSX source: under `.tsx` a Capitalized arrow returning JSX is a
        # component; under `.ts` the grammar does not parse JSX, so it is a
        # plain function: the grammar choice itself is the gate
        src = 'export const Panel = (p) => { return <section>{p.x}</section>; };\n'
        self.assertEqual(self._kinds(_extract(src, ".tsx"))["component"], 1)
        self.assertNotIn("component", self._kinds(_extract(src, ".ts")))
        # .ts still extracts a plain (non-JSX) arrow-const as a function
        self.assertEqual(
            self._kinds(_extract('const f = (x) => x + 1;\n', ".ts"))["function"], 1)
        # lowercase name returning JSX is a function, never a component (.tsx)
        low = self._kinds(_extract('const render = () => <div/>;\n', ".tsx"))
        self.assertEqual(low["function"], 1)
        self.assertNotIn("component", low)

    def test_typescript_interface_enum_type_captured(self):
        # core TS constructs, incl. under `export`: must not be silently dropped
        ts = ('export interface User { id: number; }\n'
              'export enum Color { Red, Green }\n'
              'export type ID = string;\n'
              'interface Bare {}\n')
        g = _extract(ts, ".ts")
        kinds = self._kinds(g)
        self.assertEqual(kinds["interface"], 2)        # User + Bare
        self.assertEqual(kinds["enum"], 1)             # Color
        self.assertEqual(kinds["type"], 1)             # ID
        labels = self._labels(g)
        self.assertLessEqual({"User", "Bare", "Color", "ID"}, labels)
        # each hangs off the module via `contains`
        self.assertGreaterEqual(self._rels(g)["contains"], 4)

    def test_typescript_call_retains_its_relation_and_target(self):
        graph = _extract(
            "export function run() { return service.saveData(); }\n", ".ts")
        calls = [edge for edge in graph["links"]
                 if edge["relation"] == "calls"]
        self.assertTrue(any(edge["target"] == "saveData" for edge in calls))

    def test_re_export_and_default_export(self):
        js = ('export { a, b } from "./mod";\n'      # re-export = import edge
              'import x from "dep";\n'
              'export default AppRoot;\n')            # default = export node
        g = _extract(js, ".js")
        imports = [e for e in g["links"] if e["relation"] == "imports"]
        import_labels = {n["label"] for n in g["nodes"]
                         if n["kind"] == "import"}
        self.assertIn("./mod", import_labels)          # re-export source
        self.assertIn("dep", import_labels)
        self.assertGreaterEqual(len(imports), 2)
        exports = {n["label"] for n in g["nodes"] if n["kind"] == "export"}
        self.assertIn("AppRoot", exports)              # default export name

    def test_javascript_require_becomes_import(self):
        js = ('const fs = require("fs");\n'
              'const { join } = require("path");\n'
              'function run() { return fs; }\n')
        g = _extract(js, ".js")
        kinds = self._kinds(g)
        self.assertEqual(kinds["module"], 1)
        self.assertGreaterEqual(kinds["import"], 2)   # fs + path
        self.assertEqual(kinds["function"], 1)        # run
        labels = self._labels(g)
        self.assertIn("fs", labels)
        self.assertIn("path", labels)
        self.assertGreaterEqual(self._rels(g)["imports"], 2)

    # -- generic fallback (any grammar without a hand-written extractor) -----

    def test_generic_fallback_yields_module_defs_and_imports(self):
        # whichever of rust/go actually loads: NO hand-written extractor runs,
        # yet the generic tree-walk must still yield a module node, function
        # and struct nodes, and an import/use edge: structure from any grammar
        candidates = [
            ("rust", ".rs",
             'use std::io::Read;\n'
             'struct User { id: i32 }\n'
             'fn main() { let _ = 1; }\n'),
            ("go", ".go",
             'package main\n'
             'import "fmt"\n'
             'type User struct { ID int }\n'
             'func main() { fmt.Println("x") }\n'),
        ]
        ran = 0
        for lang, ext, code in candidates:
            try:
                extract._parser(lang)
            except Exception:
                continue
            ran += 1  # exercise EVERY loadable candidate, not just the first
            g = _extract(code, ext)
            kinds = self._kinds(g)
            self.assertEqual(kinds["module"], 1, lang)
            self.assertGreaterEqual(kinds["function"], 1, lang)   # main
            self.assertGreaterEqual(kinds.get("struct", 0), 1, lang)  # User
            self.assertIn("User", self._labels(g), lang)
            self.assertGreaterEqual(self._rels(g)["imports"], 1, lang)
        if not ran:
            self.skipTest("neither rust nor go grammar available in the pack")

    def test_generic_fallback_nested_contains(self):
        # def → child-def nesting: an impl block CONTAINS its method (not the
        # module): the generic walk reparents into each matched def
        try:
            extract._parser("rust")
        except Exception:
            self.skipTest("rust grammar not available")
        g = _extract("impl User { fn build() -> u8 { 0 } }\n", ".rs")
        by_id = {n["id"]: n for n in g["nodes"]}
        nested = [(by_id[e["source"]], by_id[e["target"]])
                  for e in g["links"] if e["relation"] == "contains"
                  and by_id[e["source"]]["kind"] != "module"]
        self.assertTrue(nested, "expected a def→child-def edge")
        self.assertTrue(any(s["kind"] == "impl" and t["kind"] == "function"
                            for s, t in nested))

    def test_go_symbol_relations_retain_the_calling_function(self):
        try:
            extract._parser("go")
        except Exception:
            self.skipTest("go grammar not available")
        path = _write(
            "package service\n\n"
            "func run() { load() }\n"
            "func load() {}\n",
            ".go",
        )
        try:
            graph = extract.code_symbol_relations(path, src="service.go")
        finally:
            os.unlink(path)

        definitions = {
            symbol["qualified"] for symbol in graph["symbols"]
        }
        relations = {
            (edge["source"], edge["target"], edge["relation"])
            for edge in graph["relations"]
        }
        self.assertLessEqual({"run", "load"}, definitions)
        self.assertIn(("run", "load", "calls"), relations)

    def test_parser_history_does_not_change_complete_output(self):
        target = _write(
            "from client import Client\n\n"
            "class Service:\n"
            "    def run(self):\n"
            "        return Client().load()\n",
            ".py",
        )
        self.addCleanup(os.unlink, target)
        history = []
        for index in range(24):
            path = _write(
                f"import dependency_{index}\n\n"
                f"def task_{index}():\n"
                f"    return dependency_{index}.run()\n",
                ".py",
            )
            history.append(path)
            self.addCleanup(os.unlink, path)

        def complete_output(path, src):
            return {
                "graph": extract.extract_file(path, src=src),
                "symbol_graph": extract.code_symbol_relations(path, src=src),
            }

        with mock.patch.object(extract, "_PARSERS", {}):
            baseline = complete_output(target, "target.py")
        with mock.patch.object(extract, "_PARSERS", {}):
            for index, path in enumerate(history):
                complete_output(path, f"history_{index}.py")
            after_history = complete_output(target, "target.py")

        self.assertEqual(after_history, baseline)

    def test_java_generic_pass_extracts_code_relations(self):
        try:
            extract._parser("java")
        except Exception:
            self.skipTest("java grammar not available")
        code = (
            "class Service extends Base implements Worker {\n"
            "  Client client = new Client();\n"
            "  void run() { client.execute(); }\n"
            "}\n"
        )
        graph = _extract(code, ".java")
        relations = {(edge["relation"], edge["target"])
                     for edge in graph["links"]}
        self.assertIn(("inherits", "Base"), relations)
        self.assertIn(("implements", "Worker"), relations)
        self.assertIn(("calls", "execute"), relations)
        self.assertIn(("references", "Client"), relations)
        definitions = {node["label"] for node in graph["nodes"]}
        self.assertNotIn("execute", definitions)

    def test_csharp_generic_pass_extracts_code_relations(self):
        try:
            extract._parser("csharp")
        except Exception:
            self.skipTest("csharp grammar not available")
        code = (
            "class Service : Base, IWorker {\n"
            "  Client client = new Client();\n"
            "  void Run() { client.Execute(); }\n"
            "}\n"
        )
        graph = _extract(code, ".cs")
        relations = {(edge["relation"], edge["target"])
                     for edge in graph["links"]}
        self.assertIn(("references", "Base"), relations)
        self.assertIn(("references", "IWorker"), relations)
        self.assertIn(("references", "Client"), relations)
        self.assertIn(("calls", "Execute"), relations)

    # -- provenance / confidence marking ------------------------------------

    def test_every_node_and_edge_is_extracted(self):
        g = _extract("# H\n\nbody [x](y.md)\n", ".md")
        self.assertTrue(g["nodes"])
        for n in g["nodes"]:
            self.assertEqual(n["provenance"], "extracted")
            self.assertEqual(n["confidence"], "extracted")
        for e in g["links"]:
            self.assertEqual(e["confidence"], "extracted")

    # -- import into a bundle ----------------------------------------------

    def test_import_into_bundle_and_edges_resolve(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        g = _extract("database:\n  host: localhost\n  port: 5432\n", ".yaml")
        written, skipped = extract.import_graph(Bundle(root), g)
        self.assertGreater(written, 0)
        self.assertEqual(skipped, 0)
        b = Bundle(root)
        note = next(n for n in b.notes.values()
                    if n.meta.get("title") == "database")
        self.assertEqual(note.meta["provenance"], "extracted")
        self.assertEqual(note.meta["confidence"], "extracted")
        self.assertIn("tree-sitter", note.tags)
        # contains edges parse back as typed weighted edges (extracted -> 1.0)
        edges = [e for lst in b.typed_edges().values() for e in lst]
        self.assertTrue(edges)
        self.assertTrue(all(e["weight"] == 1.0 for e in edges))
        self.assertTrue(any(e["relation"] == "contains" for e in edges))

    def test_reimport_is_idempotent(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        g = _extract("a:\n  b: 1\n  c: 2\n", ".yaml")
        extract.import_graph(Bundle(root), g)
        written, skipped = extract.import_graph(Bundle(root), g)
        self.assertEqual(written, 0)
        self.assertGreater(skipped, 0)

    def test_new_kinds_import_into_bundle_and_reimport_idempotent(self):
        # the JS/TS kinds (component/class/method/import) write clean notes,
        # carry the tree-sitter tag + extracted provenance, and re-import clean
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        tsx = ('import React from "react";\n'
               'export const Card = () => <div/>;\n'
               'export class Store { load() { return 1; } }\n')
        g = _extract(tsx, ".tsx")
        written, skipped = extract.import_graph(Bundle(root), g)
        self.assertGreater(written, 0)
        self.assertEqual(skipped, 0)
        b = Bundle(root)
        card = next(n for n in b.notes.values()
                    if n.meta.get("title") == "Card")
        self.assertEqual(card.meta["type"], "component")   # kind → note type
        self.assertEqual(card.meta["provenance"], "extracted")
        self.assertIn("tree-sitter", card.tags)
        # a second import of the identical graph rewrites nothing
        w2, s2 = extract.import_graph(Bundle(root), g)
        self.assertEqual(w2, 0)
        self.assertGreater(s2, 0)

    def test_curated_note_is_never_overwritten(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        b = Bundle(root)
        # a hand-authored note squats the path the extractor would write
        b.write_note("extracted/yaml/service.md",
                     {"type": "note", "title": "service",
                      "provenance": "curated"}, "MINE: do not touch")
        g = _extract("service: api\n", ".yaml")
        extract.import_graph(b, g)
        with open(os.path.join(root, "extracted/yaml/service.md"),
                  encoding="utf-8") as fh:
            self.assertIn("MINE: do not touch", fh.read())

    def test_json_output_roundtrips_through_import_graphify(self):
        # a `--json` graph is graphify-shaped: ingest.import_graphify reads it
        from muninn.ingest import import_graphify
        import json
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        g = _extract("# Doc\n\nprose\n", ".md")
        gp = os.path.join(root, "graph.json")
        with open(gp, "w", encoding="utf-8") as fh:
            json.dump(g, fh)
        written, _ = import_graphify(Bundle(root), gp)
        self.assertGreater(written, 0)


@unittest.skipUnless(HAVE_TS, "tree-sitter not installed")
class TestExtractCLI(unittest.TestCase):
    def test_extract_prints_summary(self):
        path = _write("# H\n\nbody\n", ".md")
        self.addCleanup(os.unlink, path)
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.main(["extract", path])
        out = buf.getvalue()
        self.assertIn("nodes", out)
        self.assertIn("nodes by kind", out)

    def test_cli_only_and_exclude_flags_plumb_through(self):
        # exercises cli.cmd_extract's --only parsing (dot-normalize + lower)
        # and --exclude append-list wiring into extract_path, via a json dump
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        with open(os.path.join(d, "keep.ts"), "w") as fh:
            fh.write("export const Keep = 1;\n")
        with open(os.path.join(d, "drop.py"), "w") as fh:
            fh.write("def dropped():\n    pass\n")
        with open(os.path.join(d, "keep.test.ts"), "w") as fh:
            fh.write("export const Excluded = 1;\n")
        out_json = os.path.join(d, "g.json")
        buf = io.StringIO()
        with redirect_stdout(buf):
            # `ts` (no dot) + `.tsx` (dot) proves the normalization; exclude the
            # test file by glob
            cli.main(["extract", d, "--only", "ts,.tsx",
                      "--exclude", "*.test.ts", "--json", out_json])
        import json
        with open(out_json, encoding="utf-8") as fh:
            g = json.load(fh)
        labels = {n["label"] for n in g["nodes"]}
        self.assertIn("keep.ts", labels)         # kept (module node)
        self.assertNotIn("dropped", labels)      # --only excluded .py
        self.assertNotIn("keep.test.ts", labels)  # --exclude glob pruned it


class TestCarryInferred(unittest.TestCase):
    """No grammar needed. Enrichment's inferred connection lines must survive
    a base re-import: the deterministic pass knows nothing of the model
    layer's additions and used to silently destroy them on re-render."""

    def _node(self, nid, label, src):
        return {"id": nid, "label": label, "kind": "heading",
                "lang": "markdown", "file_type": "document", "body": "prose",
                "source_file": src, "provenance": "extracted",
                "confidence": "extracted"}

    def test_carry_is_pure_and_idempotent(self):
        old = ("prose\n\n# Connections\n\n- contains [[A]] (extracted)\n"
               "- documents [[B]] (inferred)")
        new = "prose\n\n# Connections\n\n- contains [[A]] (extracted)"
        merged = extract._carry_inferred(old, new)
        self.assertIn("- documents [[B]] (inferred)", merged)
        self.assertEqual(extract._carry_inferred(merged, merged), merged)
        # a new body with no connections section grows one for the carry
        bare = extract._carry_inferred(old, "prose only")
        self.assertIn("# Connections", bare)
        self.assertIn("(inferred)", bare)
        # extracted lines are NOT carried (the base owns those)
        self.assertNotIn("[[A]] (extracted)\n- contains [[A]]", bare)

    def test_reimport_of_plain_base_preserves_enrichment(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        base = {"nodes": [self._node("x", "XNote", "x.md"),
                          self._node("y", "YNote", "y.md")],
                "links": [{"source": "x", "target": "y",
                           "relation": "references",
                           "confidence": "extracted"}]}
        enriched = {**base, "nodes": [dict(n) for n in base["nodes"]],
                    "links": base["links"] + [
                        {"source": "x", "target": "y",
                         "relation": "documents", "confidence": "inferred"}]}
        extract.import_graph(Bundle(root), enriched)
        # the destructive step: re-import the PLAIN base over the bundle
        extract.import_graph(Bundle(root), base)
        b = Bundle(root)
        note = next(n for n in b.notes.values()
                    if n.meta.get("title") == "XNote")
        self.assertIn("- documents [[YNote]] (inferred)", note.body)
        # and the carried line parses back as a weight-0.5 typed edge
        edges = [e for lst in b.typed_edges().values() for e in lst
                 if e["confidence_word"] == "inferred"]
        self.assertTrue(any(e["weight"] == 0.5 for e in edges))


class TestFailSoft(unittest.TestCase):
    """No grammar needed: these must hold with OR without tree-sitter."""

    def test_available_returns_bool(self):
        self.assertIsInstance(extract.available(), bool)

    def test_unsupported_extension_is_noop(self):
        self.assertIsNone(extract.lang_for("notes.txt"))
        path = _write("plain text", ".txt")
        self.addCleanup(os.unlink, path)
        g = extract.extract_file(path)
        self.assertEqual(g["nodes"], [])
        self.assertEqual(g["links"], [])

    def test_extension_map_resolves_common_set(self):
        # the comprehensive ext -> grammar resolver (pure, needs no grammar)
        cases = {
            "a.py": "python", "a.ts": "typescript", "a.tsx": "tsx",
            "a.jsx": "javascript", "a.js": "javascript", "a.mjs": "javascript",
            "a.cjs": "javascript", "a.go": "go", "a.rs": "rust",
            "a.java": "java", "a.kt": "kotlin", "a.scala": "scala",
            "a.c": "c", "a.h": "c", "a.cpp": "cpp", "a.cc": "cpp",
            "a.hpp": "cpp", "a.cs": "csharp", "a.rb": "ruby", "a.php": "php",
            "a.swift": "swift", "a.sh": "bash", "a.lua": "lua", "a.r": "r",
            "a.html": "html", "a.css": "css", "a.scss": "scss", "a.vue": "vue",
            "a.svelte": "svelte", "a.xml": "xml", "a.rst": "rst",
            "a.tex": "latex", "a.hcl": "hcl", "a.tf": "terraform",
            "a.proto": "proto", "a.graphql": "graphql", "a.ex": "elixir",
            "a.erl": "erlang", "a.hs": "haskell", "a.ml": "ocaml",
            "a.clj": "clojure", "a.jl": "julia", "a.dart": "dart",
            "a.zig": "zig", "a.json": "json", "a.yaml": "yaml",
            "a.yml": "yaml", "a.toml": "toml", "a.sql": "sql",
            "a.md": "markdown", "Dockerfile": "dockerfile",
            "Makefile": "make", "go.mod": "gomod",
        }
        for path, lang in cases.items():
            self.assertEqual(extract.lang_for(path), lang, path)
        # bare-name match wins over extension and is case-insensitive
        self.assertEqual(extract.lang_for("/proj/DOCKERFILE"), "dockerfile")
        self.assertEqual(extract.lang_for("/proj/src/main.go"), "go")
        # an unmapped extension is still None (fail-soft)
        self.assertIsNone(extract.lang_for("a.unknownextzz"))

    def test_lockfiles_maps_and_minified_are_skipped(self):
        for fn in ["package-lock.json", "yarn.lock", "pnpm-lock.yaml",
                   "Cargo.lock", "poetry.lock", "app.min.js",
                   "vendor.min.css", "bundle.js.map"]:
            self.assertTrue(extract._skip_walk_file(fn), fn)
        for fn in ["app.js", "main.ts", "config.yaml", "notes.md",
                   "package.json"]:
            self.assertFalse(extract._skip_walk_file(fn), fn)

    def test_oversized_and_binary_files_are_skipped(self):
        # a file past the size cap is skipped without parsing (guard is pure,
        # holds with or without tree-sitter)
        big = _write("x = 1\n" * (extract.MAX_FILE_BYTES // 2), ".py")
        self.addCleanup(os.unlink, big)
        self.assertEqual(extract.extract_file(big)["nodes"], [])
        # a NUL byte marks binary: never parsed as source
        path = _write("a: 1\n\x00\x00binary", ".yaml")
        self.addCleanup(os.unlink, path)
        self.assertEqual(extract.extract_file(path)["nodes"], [])

    def test_grammar_load_failure_is_soft(self):
        # a grammar that will not load must skip the file, not raise
        path = _write("def x(): pass\n", ".py")
        self.addCleanup(os.unlink, path)
        with mock.patch.object(extract, "_parser",
                               side_effect=RuntimeError("no grammar")):
            g = extract.extract_file(path)
        self.assertEqual(g["nodes"], [])

    def test_cli_extract_soft_when_unavailable(self):
        path = _write("# H\n", ".md")
        self.addCleanup(os.unlink, path)
        buf = io.StringIO()
        with mock.patch.object(extract, "available", return_value=False), \
                redirect_stdout(buf):
            cli.main(["extract", path])
        self.assertIn("tree-sitter not installed", buf.getvalue())

    def test_enrich_seam_is_live_and_failsoft(self):
        # the LLM enrichment layer is now implemented (enrich.py); with no
        # MUNINN_ENRICH_URL configured it is FAIL-SOFT: the base graph comes
        # back unchanged, never an exception (full coverage in test_enrich.py)
        base = {"nodes": [{"id": "a", "label": "A", "provenance": "extracted"}],
                "links": []}
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MUNINN_ENRICH_URL", None)
            out = extract.enrich(base)
        self.assertEqual(len(out["nodes"]), 1)
        self.assertEqual(out["links"], [])

    def test_nesting_depth_guard_metric(self):
        # pure metric (no grammar): pathological nesting exceeds the cap, a
        # normal file does not: this is what pre-empts the native segfault
        deep_yaml = "".join("  " * i + f"k{i}:\n" for i in range(400)).encode()
        deep_json = (b"{" * 500) + b'"x":1' + (b"}" * 500)
        self.assertGreater(extract._nesting_depth(deep_yaml), extract.MAX_DEPTH)
        self.assertGreater(extract._nesting_depth(deep_json), extract.MAX_DEPTH)
        self.assertLessEqual(
            extract._nesting_depth(b"a:\n  b:\n    c: 1\n"), extract.MAX_DEPTH)

    def test_deep_file_skipped_before_parse(self):
        # extract_file returns cleanly on pathological input WITHOUT reaching
        # the parser: so this holds even with tree-sitter absent (guard first)
        deep = "".join("  " * i + f"k{i}:\n" for i in range(400))
        path = _write(deep, ".yaml")
        self.addCleanup(os.unlink, path)
        g = extract.extract_file(path)  # must not raise, must not segfault
        self.assertEqual(g["nodes"], [])


@unittest.skipUnless(HAVE_TS, "tree-sitter not installed")
class TestRobustness(unittest.TestCase):
    """Fail-soft and guard behaviors that need a working grammar to observe."""

    def test_directory_scan_does_not_follow_external_file_symlinks(self):
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        external = _write("def external_function(): pass\n", ".py")
        self.addCleanup(os.unlink, external)
        link = os.path.join(directory, "linked.py")
        os.symlink(external, link)
        graph, counts = extract.extract_path(directory)
        self.assertEqual(graph["nodes"], [])
        self.assertEqual(counts["files"], 0)
        explicit, _ = extract.extract_path(link)
        self.assertTrue(explicit["nodes"])

    @unittest.skipUnless(hasattr(os, "mkfifo"), "named pipes are unavailable")
    def test_directory_scan_does_not_open_named_pipes(self):
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        os.mkfifo(os.path.join(directory, "pipe.py"))
        with mock.patch.object(extract, "extract_file") as reader:
            extract.extract_path(directory)
        reader.assert_not_called()

    def test_deep_input_does_not_crash_normal_still_parses(self):
        deep = "".join("  " * i + f"k{i}:\n" for i in range(400))
        dpath = _write(deep, ".yaml")
        self.addCleanup(os.unlink, dpath)
        self.assertEqual(extract.extract_file(dpath)["nodes"], [])  # skipped
        self.assertTrue(_extract("a:\n  b: 1\n", ".yaml")["nodes"])  # normal ok

    def test_extractor_error_rolls_back_and_walk_continues(self):
        # a raising dispatcher must not abort the folder walk or leave a
        # half-extracted file behind: the earlier good file survives intact
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        with open(os.path.join(d, "a.md"), "w") as fh:
            fh.write("# Good\n\nbody\n")
        with open(os.path.join(d, "b.json"), "w") as fh:
            fh.write('{"k": 1}')

        def boom(*a, **k):
            raise RuntimeError("boom")
        # patch the dispatch entry itself (not the _json name: _DISPATCH
        # captured the original reference at import time)
        with mock.patch.dict(extract._DISPATCH, {"json": boom}):
            g, stats = extract.extract_path(d)  # must not raise
        labels = {n["label"] for n in g["nodes"]}
        self.assertIn("Good", labels)  # markdown file survived
        self.assertNotIn("k", labels)  # json rolled back cleanly

    def test_folder_walk_excludes_output_dir(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        os.makedirs(os.path.join(d, "extracted"))
        with open(os.path.join(d, "keep.md"), "w") as fh:
            fh.write("# Keep\n\nx\n")
        with open(os.path.join(d, "extracted", "skip.md"), "w") as fh:
            fh.write("# Skip\n\ny\n")
        g, _ = extract.extract_path(d, exclude={os.path.join(d, "extracted")})
        labels = {n["label"] for n in g["nodes"]}
        self.assertIn("Keep", labels)
        self.assertNotIn("Skip", labels)

    def test_reserved_index_file_skipped(self):
        # index.md/log.md are generated OKF artifacts: skipping them stops the
        # `build .` self-scan feedback loop
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        with open(os.path.join(d, "index.md"), "w") as fh:
            fh.write("# Index\n\n* [x](/x.md)\n")
        with open(os.path.join(d, "real.md"), "w") as fh:
            fh.write("# Real\n\nbody\n")
        labels = {n["label"] for n in extract.extract_path(d)[0]["nodes"]}
        self.assertIn("Real", labels)
        self.assertNotIn("Index", labels)

    def test_dotfiles_are_skipped_in_walk(self):
        # a dotfile config (.secret.yaml) must never be swept (secret guard)
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        with open(os.path.join(d, ".secret.yaml"), "w") as fh:
            fh.write("password: hunter2\n")
        with open(os.path.join(d, "public.yaml"), "w") as fh:
            fh.write("name: ok\n")
        g, _ = extract.extract_path(d)
        bodies = " ".join(n["body"] for n in g["nodes"])
        self.assertNotIn("hunter2", bodies)
        self.assertIn("ok", bodies)

    def test_config_value_count(self):
        cfg = _extract("host: localhost\nport: 5432\n", ".yaml")
        self.assertEqual(extract.config_value_count(cfg), 2)
        md = _extract("# H\n\nbody\n", ".md")
        self.assertEqual(extract.config_value_count(md), 0)

    def test_top_level_yaml_sequence_yields_no_partial(self):
        # a top-level list has no top-level keys: extract nothing rather than
        # misleadingly grabbing only the first item's keys
        g = _extract("- a: 1\n  b: 2\n- c: 3\n  d: 4\n", ".yaml")
        self.assertEqual(g["nodes"], [])

    def test_folder_walk_skips_node_modules_and_lockfiles(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        with open(os.path.join(d, "app.ts"), "w") as fh:
            fh.write('export const AppName = "x";\n'
                     'function run() { return 1; }\n')
        os.makedirs(os.path.join(d, "node_modules", "dep"))
        with open(os.path.join(d, "node_modules", "dep", "index.ts"),
                  "w") as fh:
            fh.write('export function leaked() { return 1; }\n')
        # a real (valid-JSON) lockfile that WOULD otherwise be swept as json
        with open(os.path.join(d, "package-lock.json"), "w") as fh:
            fh.write('{"name": "x", "lockfileVersion": 3, "packages": {}}')
        g, stats = extract.extract_path(d)
        labels = {n["label"] for n in g["nodes"]}
        self.assertIn("run", labels)               # our source parsed
        self.assertNotIn("leaked", labels)          # node_modules pruned
        self.assertNotIn("lockfileVersion", labels)  # lockfile skipped
        self.assertEqual(set(stats["by_lang"]), {"typescript"})

    def test_only_ext_filter(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        with open(os.path.join(d, "a.ts"), "w") as fh:
            fh.write("export function keep() { return 1; }\n")
        with open(os.path.join(d, "b.py"), "w") as fh:
            fh.write("def dropped():\n    pass\n")
        with open(os.path.join(d, "c.md"), "w") as fh:
            fh.write("# Heading\n\nbody\n")
        g, stats = extract.extract_path(d, only_exts={".ts"})
        self.assertEqual(set(stats["by_lang"]), {"typescript"})
        labels = {n["label"] for n in g["nodes"]}
        self.assertIn("keep", labels)           # the .ts function is present
        self.assertNotIn("dropped", labels)     # .py symbol absent
        self.assertNotIn("Heading", labels)     # .md symbol absent

    def test_exclude_glob_prunes_matching_paths(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        with open(os.path.join(d, "keep.ts"), "w") as fh:
            fh.write("export const Keep = 1;\n")
        with open(os.path.join(d, "skip.test.ts"), "w") as fh:
            fh.write("export const Skip = 1;\n")
        g, _ = extract.extract_path(d, exclude_globs=["*.test.ts"])
        labels = {n["label"] for n in g["nodes"]}
        self.assertIn("keep.ts", labels)           # module node of the keeper
        self.assertNotIn("skip.test.ts", labels)   # excluded by glob


if __name__ == "__main__":
    unittest.main(verbosity=2)
