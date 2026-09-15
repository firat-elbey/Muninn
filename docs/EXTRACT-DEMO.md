# Deterministic extraction demonstration

`muninn extract` converts supported source structure into nodes and typed edges
without a model or network request. The optional `extract` dependency provides
tree-sitter and its language pack. Core Muninn remains usable when this
dependency is absent.

## Run the check

Install Muninn with extraction support and inspect the current specification:

```bash
pip install -e ".[extract]"
muninn extract SPEC.md
```

At the reviewed revision, the command reports:

```text
extracted 1 file(s) from SPEC.md: 14 nodes, 21 edges
  files by language: markdown 1
  nodes by kind:     heading 14
  edges by relation: contains 13, references 8
  (dry run; add --json <out.json> to write the graph, or --into <bundle> to import notes)
```

The exact counts may change when the specification changes. The required
properties are deterministic output for an unchanged input, no model call,
heading containment edges, and reference edges for Markdown links and
wikilinks.

## Export or import the graph

The dry run does not write files. Two options materialize the result:

```bash
muninn extract SPEC.md --json /tmp/spec-graph.json
muninn extract SPEC.md --into /tmp/spec-bundle
```

The JSON output uses the same node-link shape accepted by `muninn import`.
Import writes OKF notes below `extracted/<language>/`. Each node has
`provenance: extracted`, and each deterministic edge has
`confidence: extracted`, which maps to weight 1.0.

For Markdown, headings become nodes and nesting produces `contains` edges.
Markdown links and wikilinks produce `references` edges. For Python, modules,
classes, functions, methods, and imports become nodes. Containment and import
relationships become typed edges. Parsers for JSON, YAML, TOML, and SQL expose
format-appropriate structural records.

Generated links use the same `# Connections` line grammar that `store.py`
parses. A generated bundle can therefore be loaded, inspected, and exported
through the ordinary Muninn paths. Repeating an import skips byte-identical
notes, and import never overwrites curated or unlabeled content.

## Optional model enrichment

Extraction is complete without a model. `muninn enrich` may later add concept
nodes and cross-file edges with `provenance: inferred` and weight 0.5. It does
not rewrite the deterministic nodes or edges. [ENRICH.md](ENRICH.md) defines
the provider, privacy, digest, and response-validation rules for this optional
step.
