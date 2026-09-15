"""Export a deterministic graph that combines knowledge and usage.

The node-link result includes authored typed links, ordinary Markdown links,
supersession edges, and co-use relationships from the sidecar. Typed edges
retain their confidence weight, ordinary links use weight 1.0, supersession
uses weight 1.0, and co-use is limited to ``min(1, count / 3)``. The output
uses the same general node-link form as Graphify and requires no third-party
library.
"""

from __future__ import annotations

import json

from .dynamics import Dynamics, _canonical_supersession_path
from .store import Bundle


def export_graph(bundle: Bundle, dyn: Dynamics | None = None) -> dict:
    """Build the fused graph. Nodes are notes (id = bundle-relative path);
    ``strength`` is 0.0 when no sidecar entry exists (baseline)."""
    nodes = []
    for path in sorted(bundle.notes):
        note = bundle.notes[path]
        nodes.append({
            "id": path,
            "label": note.title,
            # OKF-M §2.2: absent provenance is treated as curated
            "provenance": str(note.meta.get("provenance", "curated")),
            "strength": round(dyn.strength(path), 4) if dyn else 0.0,
        })

    links: list[dict] = []
    for path in sorted(bundle.notes):
        note = bundle.notes[path]
        typed_targets = set()
        plain_targets = set(note.plain_links)
        for e in note.typed_links:
            links.append({"source": path, "target": e["target"],
                          "relation": e["relation"],
                          "weight": e["weight"],
                          "confidence": e["confidence_word"],
                          "kind": "typed"})
            typed_targets.add(e["target"])
        for tgt in note.links:
            if tgt in typed_targets and tgt not in plain_targets:
                continue
            links.append({"source": path, "target": tgt,
                          "relation": "related_to", "weight": 1.0,
                          "kind": "link"})

    corrections = {
        (_canonical_supersession_path(old), path)
        for path, note in bundle.notes.items()
        for old in note.supersedes()
    }
    if dyn is not None:
        try:
            with open(dyn.ledger_path, encoding="utf-8") as handle:
                for line in handle:
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(event, dict) and event.get("kind") == "supersede":
                        corrections.add((
                            _canonical_supersession_path(event.get("note", "")),
                            _canonical_supersession_path(event.get("by", "")),
                        ))
        except FileNotFoundError:
            pass
    for old, new in sorted(corrections):
        if old not in bundle.notes or new not in bundle.notes:
            continue
        links.append({"source": new, "target": old,
                      "relation": "supersedes", "weight": 1.0,
                      "kind": "supersedes"})

    if dyn is not None:
        adj = dyn.coactivation()
        for a in sorted(adj):
            for b, w in sorted(adj[a].items()):
                if a < b and a in bundle.notes and b in bundle.notes:
                    links.append({"source": a, "target": b,
                                  "relation": "used_with",
                                  "weight": round(min(1.0, w / 3.0), 4),
                                  "kind": "used_with"})

    return {"muninn_export": "0.1", "directed": True,
            "nodes": nodes, "links": links}
