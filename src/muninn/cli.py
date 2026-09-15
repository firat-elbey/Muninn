"""Expose Muninn's knowledge, source retrieval, memory, and setup commands."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
import time

from . import enrich, extract, home, skill, source_retrieval, style, viz
from .demo import run_demo
from .dynamics import Dynamics
from .export import export_graph
from .ingest import import_graphify
from .observe import (KINDS, TOOL_KINDS, current_branch, hook_config,
                      hook_payload, infer_session, inflight_section,
                      lessons_section, observe_event, stdin_text)
from .sync import SYNC_REF, SyncError, sync as run_sync
from .recall import (context_pack, goal_alignment, prime_cue, recall,
                     recall_explain, tokens)
from .evolve import evolve_if_due, evolve_once, note_feedback, whisper_section
from .journal import (add_episode, import_transcripts, scrub, threads_of,
                      threads_section)
from .review import (recent_reviews, review_if_due, review_session,
                     session_metrics)
from .store import Bundle, generated_stamp
from .volunteer import volunteer_pack


def _bundle(args) -> Bundle:
    return Bundle(args.root)


def cmd_demo(args):
    """Narrated one-minute tour on a throwaway bundle (never your notes)."""
    run_demo()


def cmd_init(args):
    os.makedirs(args.root, exist_ok=True)
    # the sidecar is per-consumer usage memory: gitignored by default
    # (SPEC §2), so a `git init`-ed bundle never commits a personal ledger
    try:
        home._ensure_gitignore(args.root)
    except (OSError, ValueError) as error:
        raise SystemExit(f"muninn init: cannot update .gitignore safely: {error}") from None
    b = Bundle(args.root)
    b.generate_index()
    Dynamics(args.root)._save()
    print(f"initialized muninn bundle at {os.path.abspath(args.root)}")


def cmd_add(args):
    b = _bundle(args)
    meta = {"type": args.type, "title": scrub(args.title),
            "generated": generated_stamp()}
    if args.description:
        meta["description"] = scrub(args.description)
    if args.tags:
        meta["tags"] = [scrub(t.strip()) for t in args.tags.split(",")]
    if args.supersedes:
        meta["supersedes"] = [args.supersedes]
    body = scrub(args.body or sys.stdin.read())
    rel = args.path
    rel = b.write_note(rel, meta, body)
    d = Dynamics(args.root)
    d.touch(rel, kind="encode")
    if args.supersedes:
        d.supersede(args.supersedes, rel)
    b.generate_index()
    print(f"added {rel}")


def cmd_import(args):
    """Import a graphify graph.json into the bundle as notes."""
    b = _bundle(args)
    try:
        written, skipped = import_graphify(b, args.graph_json, subdir=args.subdir,
                                          include_code=args.include_code,
                                          source_root=args.source_root,
                                          code_cap=args.max_code_notes)
    except (OSError, ValueError, TypeError) as error:
        raise SystemExit(f"muninn import: {error}") from None
    b.generate_index()
    print(f"imported {written} notes into {args.subdir}/ from "
          f"{args.graph_json}"
          + (f" ({skipped} unchanged, skipped)" if skipped else ""))
    _refresh_viz(args.root)


def _emit_graph(graph, args):
    """The shared output tail of `extract` and `enrich`: the config-secret
    notice at the point of use, the --json write, the --into import, and the
    dry-run hint. One place, so the two commands can never drift."""
    s = extract.summarize(graph)
    cfg = extract.config_value_count(graph)
    if cfg and (args.json or args.into):  # secret-exposure notice at point of use
        print(f"note: captured {cfg} config value(s) into note bodies: review "
              "before committing or packing if any config may hold secrets",
              file=sys.stderr)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(graph, fh, indent=1)
            fh.write("\n")
        print(f"wrote graph.json ({s['nodes']} nodes / {s['edges']} edges) "
              f"to {args.json}")
    if args.into:
        os.makedirs(args.into, exist_ok=True)
        b = Bundle(args.into)
        written, skipped = extract.import_graph(b, graph, subdir=args.subdir)
        b.generate_index()
        print(f"imported {written} notes into {args.subdir}/ under {args.into}"
              + (f" ({skipped} unchanged, skipped)" if skipped else ""))
        _refresh_viz(args.into)
    if not args.json and not args.into:
        print("  (dry run: add --json <out.json> to write the graph, or "
              "--into <bundle> to import notes)")


def cmd_extract(args):
    """Deterministic tree-sitter pass over a file or folder (zero LLM):
    print a summary, then write graph.json (--json) and/or import notes into
    a bundle (--into). Neither → a dry-run summary only."""
    if not extract.available():
        print("tree-sitter not installed: pip install tree-sitter "
              "tree-sitter-language-pack (the deterministic extractor is an "
              "optional dependency; core muninn stays stdlib-only)")
        return
    # when importing into a bundle, never sweep that bundle's own output back in
    exclude = ({os.path.join(os.path.abspath(args.into), args.subdir)}
               if args.into else None)
    only = None
    if args.only:
        only = {("." + e.strip().lstrip(".")).lower()
                for e in args.only.split(",") if e.strip()}
    graph, stats = extract.extract_path(args.path, exclude=exclude,
                                        exclude_globs=args.exclude,
                                        only_exts=only)
    s = extract.summarize(graph)
    print(f"extracted {stats['files']} file(s) from {args.path}: "
          f"{s['nodes']} nodes, {s['edges']} edges")
    if stats["by_lang"]:
        print("  files by language: " + ", ".join(
            f"{k} {v}" for k, v in sorted(stats["by_lang"].items())))
    if s["by_kind"]:
        print("  nodes by kind:     " + ", ".join(
            f"{k} {v}" for k, v in s["by_kind"].items()))
    if s["by_relation"]:
        print("  edges by relation: " + ", ".join(
            f"{k} {v}" for k, v in s["by_relation"].items()))
    _emit_graph(graph, args)


def cmd_enrich(args):
    """Deterministic tree-sitter BASE, then the OPTIONAL LLM enrichment layer:
    add provenance: inferred concept nodes + cross-file edges ON TOP of the
    extracted base (never rewriting it). Who the model is, in order:
    an agent fulfilling the OFFLINE HANDSHAKE (--request prints the task,
    --apply ingests the JSON: the default when a coding agent is already in
    the repo), MUNINN_ENRICH_CMD (your agent CLI, prompt on stdin), or
    MUNINN_ENRICH_URL (a local /v1/chat/completions server). With none of
    them the base is written and nothing is inferred (fail-soft)."""
    if not extract.available():
        print("tree-sitter not installed: pip install tree-sitter "
              "tree-sitter-language-pack (the deterministic base is required "
              "before enrichment)")
        return
    # the exclude set must be IDENTICAL at --request and --apply time (the
    # digest fingerprints the extracted node table), so derive it from
    # path+root only: never from --into, which is absent at request time
    root_abs = os.path.abspath(args.root)
    path_abs = os.path.abspath(args.path)
    exclude = {os.path.join(path_abs, args.subdir),
               os.path.join(path_abs, "inferred"),
               os.path.join(root_abs, args.subdir),
               os.path.join(root_abs, "inferred"),
               os.path.join(root_abs, "imported")}
    graph, stats = extract.extract_path(args.path, exclude=exclude)
    # the handshake's own artifact must never enter the graph: an agent
    # writing response.json inside the scanned tree would otherwise shift
    # the handle table between --request and --apply (permanent digest
    # refusal); an --apply file under the tree is pruned by name too
    extra = []
    if args.apply and args.apply != "-":
        rel = os.path.relpath(os.path.abspath(args.apply),
                              os.path.abspath(args.path))
        if not rel.startswith(".."):
            extra.append(rel.replace(os.sep, "/"))
    graph = enrich.prune_handshake_files(graph, extra)
    base = extract.summarize(graph)

    if args.request:  # emit the handshake task; the reading agent is the model
        print(enrich.render_request(graph, path=args.path,
                                    into=args.into or args.root,
                                    root=args.root))
        return

    print(f"tree-sitter base: {stats['files']} file(s) → {base['nodes']} "
          f"nodes, {base['edges']} edges")
    if args.apply:
        try:  # `-` reads the response from stdin, so a pipe works
            if args.apply == "-":
                reply = sys.stdin.read()
            else:
                with open(args.apply, encoding="utf-8") as response_file:
                    reply = response_file.read()
        except OSError as error:
            raise SystemExit(f"muninn enrich: Could not read the response: {error}") from error
        try:
            graph = enrich.apply_response(graph, reply, strict=True)
        except ValueError as error:
            raise SystemExit(f"muninn enrich: Response refused: {error}") from error
        if not args.into and not args.json:
            # the agent's work must never fall through to a dry run: the
            # bundle at --root is where an unspecified apply lands
            args.into = args.root
            print(f"(no --into given: importing into the bundle at "
                  f"{root_abs})")
    else:
        try:
            graph = extract.enrich(graph)  # provider ladder; fail-soft without one
        except enrich.PrivacyError as e:
            print(f"enrichment endpoint refused (base kept): {e}",
                  file=sys.stderr)
    inf_nodes, inf_edges = enrich.count_inferred(graph)
    s = extract.summarize(graph)
    print(f"LLM enrichment: +{inf_nodes} inferred concept node(s), "
          f"+{inf_edges} inferred edge(s) → {s['nodes']} nodes, "
          f"{s['edges']} edges total")
    _emit_graph(graph, args)


# graphify runs no-LLM (--no-cluster); strip any cloud API keys from its inherited env so muninn's
# "nothing leaves the machine" guarantee holds regardless of graphify's version or behavior.
# The list tracks every remote-capable backend graphify has grown (0.9.3x adds
# AWS Bedrock, Azure OpenAI, DeepSeek, Moonshot, and remote-Ollama auth) :
# best-effort defense in depth: env credentials are stripped, file-based
# credential chains are out of scope.
_CLOUD_KEY_VARS = ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS",
                   "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "AZURE_OPENAI_API_KEY",
                   "DEEPSEEK_API_KEY", "MOONSHOT_API_KEY", "OLLAMA_API_KEY",
                   "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
                   "AWS_SESSION_TOKEN", "AWS_PROFILE", "AWS_BEARER_TOKEN_BEDROCK")


def _graphify_env(out_dir):
    env = {k: v for k, v in os.environ.items() if k not in _CLOUD_KEY_VARS}
    env["GRAPHIFY_OUT"] = out_dir
    return env


def cmd_build(args):
    """Deterministic BASE + optional graphify layer. The tree-sitter pass
    (zero LLM) is the base: code AND semi-structured no longer need graphify
    or an LLM. If graphify is ALSO installed it adds deep call-graph nodes on
    top; either alone still builds. graphify's scratch goes OUTSIDE the
    scanned folder (the bundle's sidecar, or $GRAPHIFY_OUT) so scanning a repo
    never dirties it."""
    folder = os.path.abspath(args.folder)
    # 1. deterministic base: tree-sitter over the folder, no model in the loop
    if extract.available():
        os.makedirs(args.root, exist_ok=True)
        b = _bundle(args)
        # exclude our own generated-note output (tree-sitter `extracted/` and
        # graphify `imported/`) so `build .` never re-ingests notes it wrote
        root_abs = os.path.abspath(args.root)
        exclude = {os.path.join(root_abs, "extracted"),
                   os.path.join(root_abs, "inferred"),
                   os.path.join(root_abs, "imported")}
        graph, stats = extract.extract_path(folder, exclude=exclude)
        s = extract.summarize(graph)
        if args.enrich:  # optional LLM layer on top of the deterministic base
            try:
                graph = extract.enrich(graph)
            except enrich.PrivacyError as e:
                print(f"enrichment endpoint refused (base kept): {e}",
                      file=sys.stderr)
            inf_nodes, inf_edges = enrich.count_inferred(graph)
            s = extract.summarize(graph)
            print(f"LLM enrichment: +{inf_nodes} inferred concept node(s), "
                  f"+{inf_edges} inferred edge(s)")
        written, skipped = extract.import_graph(b, graph)
        b.generate_index()
        print(f"tree-sitter base: parsed {stats['files']} file(s) → "
              f"{s['nodes']} nodes, {s['edges']} edges; imported {written} "
              f"notes into extracted/"
              + (f" ({skipped} unchanged, skipped)" if skipped else ""))
        cfg = extract.config_value_count(graph)
        if cfg:  # secret-exposure notice at point of use
            print(f"note: captured {cfg} config value(s) into note bodies: "
                  "review before committing or packing if any config may hold "
                  "secrets", file=sys.stderr)
    sys.stdout.flush()  # keep our base line ahead of graphify's own output
    _graphify_layer(args, folder)
    _refresh_viz(args.root)  # one refresh covers every exit path above


def _graphify_layer(args, folder: str) -> None:
    """cmd_build's optional second layer: deep graphify code nodes on
    top of the tree-sitter base. Every early return here still lands on
    cmd_build's viz refresh."""
    exe = shutil.which("graphify")
    if not exe:
        extra = (" (optional: adds deep call-graph nodes on top of the "
                 "tree-sitter base)" if extract.available() else "")
        print(f"graphify not found{extra}: pip install graphifyy "
              "(see github.com/safishamsi/graphify); "
              "or run: muninn import <graph.json>")
        return
    out_dir = os.environ.get("GRAPHIFY_OUT") or os.path.join(
        os.path.abspath(args.root), ".muninn", "graphify-out")
    try:
        os.makedirs(out_dir, exist_ok=True)
        stale = os.path.join(out_dir, "graph.json")
        if os.path.exists(stale):
            os.remove(stale)  # a previous build's graph must never shadow
        code = subprocess.run(  # the fresh one if graphify writes elsewhere
            [exe, "update", folder, "--no-cluster"],
            env=_graphify_env(out_dir)).returncode
    except OSError as e:
        print(f"could not run graphify: {e}")
        return
    if code != 0:
        print(f"graphify exited with status {code}; nothing imported")
        return
    candidates = [os.path.join(out_dir, "graph.json"),
                  # older graphify may ignore GRAPHIFY_OUT and write into
                  # the scanned folder: still find (and warn about) that
                  os.path.join(folder, "graphify-out", "graph.json")]
    graph = next((c for c in candidates if os.path.exists(c)), None)
    if graph is None:
        print("graphify ran but no graph.json was found (looked in: "
              + ", ".join(candidates) + ")")
        return
    if graph == candidates[1]:
        print(f"note: this graphify wrote its scratch into the scanned "
              f"folder ({os.path.dirname(graph)}): you may want to "
              f"delete or gitignore it")
    os.makedirs(args.root, exist_ok=True)
    b = _bundle(args)
    written, skipped = import_graphify(b, graph,
                                       include_code=args.include_code,
                                       source_root=folder,
                                       code_cap=args.max_code_notes)
    b.generate_index()
    print(f"built: imported {written} notes into imported/ from {graph}"
          + (f" ({skipped} unchanged, skipped)" if skipped else ""))


VIZ_AUTO_CAP = 3000  # auto-refresh bound: past this, viz is a deliberate act


def _refresh_viz(root: str) -> None:
    """The nice graph, kept fresh: any command that grows the bundle
    rewrites <root>/.muninn/graph.html so 'see what you just built' is
    zero extra commands. Bounded (a huge bundle gets a hint instead of
    a multi-minute render) and fail-soft (a viz problem must never fail
    a build)."""
    try:
        b = Bundle(root)
        if not b.notes:
            return
        if len(b.notes) > VIZ_AUTO_CAP:
            print(f"  (bundle has {len(b.notes)} notes: auto-viz skips "
                  f"past {VIZ_AUTO_CAP}; run `muninn viz` when you want "
                  "the graph)")
            return
        g = export_graph(b, Dynamics(root))
        out = os.path.join(root, ".muninn", "graph.html")
        title = f"muninn: {os.path.basename(os.path.abspath(root))}"
        path, n, e = viz.write_html(g, out, title=title)
        print(f"  graph refreshed: {n} notes / {e} edges → "
              f"{os.path.abspath(path)} (open in any browser)")
    except Exception as e:
        print(f"(viz refresh skipped: {e})", file=sys.stderr)


def cmd_viz(args):
    """SEE the knowledge: the fused graph (notes x links x usage) as one
    self-contained offline HTML file: or, with --mermaid, a bounded top-N
    diagram that renders inline in chat/markdown (inside a coding agent)."""
    b = _bundle(args)
    d = Dynamics(args.root)
    g = export_graph(b, d)
    if args.mermaid:
        print(viz.render_mermaid(g, top=args.top))
        return
    title = f"muninn: {os.path.basename(os.path.abspath(args.root))}"
    out = args.out or os.path.join(args.root, ".muninn", "graph.html")
    path, n, e = viz.write_html(g, out, title=title)
    print(f"wrote {n} notes / {e} edges to {os.path.abspath(path)}")
    print("  open it in any browser (fully offline, one file): size is "
          "recall strength, color is provenance, dotted edges were learned "
          "from use; `muninn viz --out graph.html` keeps a copy beside the "
          "notes, `muninn viz --mermaid` prints a paste-able view")


def cmd_skill(args):
    """Print the agent protocol for retrieval, judgments, enrichment, and views."""
    root = args.root if args.root != "." else os.environ.get(
        "MUNINN_ROOT", args.root)
    print(skill.render(os.path.abspath(os.path.expanduser(root))
                       if root != "." else None))


def cmd_export_graph(args):
    """Emit the fused knowledge graph (notes x links x usage) as JSON."""
    b = _bundle(args)
    d = Dynamics(args.root)
    g = export_graph(b, d)
    text = json.dumps(g, indent=1)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
        print(f"wrote {len(g['nodes'])} nodes / {len(g['links'])} edges "
              f"to {args.out}")
    else:
        print(text)


def cmd_touch(args):
    Dynamics(args.root).touch(args.note)
    print(f"touched {args.note}")


def cmd_outcome(args):
    d = Dynamics(args.root)
    n = d.outcome(args.valence, note=args.note, why=args.why or "")
    print(f"outcome valence={args.valence}: captured window of {n} notes")


def cmd_pin(args):
    Dynamics(args.root).pin(args.note, not args.unpin)
    print(f"{'unpinned' if args.unpin else 'pinned'} {args.note}")


def cmd_supersede(args):
    Dynamics(args.root).supersede(args.old, args.new)
    print(f"{args.old} superseded by {args.new}")


def cmd_recall(args):
    b = _bundle(args)
    d = Dynamics(args.root)
    react = not args.no_reactivate
    if not args.explain:
        for note, score, why in recall(b, d, args.cue, k=args.k,
                                       reactivate=react):
            print(f"{score:6.2f}  {note.path}  ({why})")
        return
    for note, score, why, e in recall_explain(b, d, args.cue, k=args.k,
                                              reactivate=react):
        print(f"{score:6.2f}  {note.path}  ({why})")
        print(f"        direct {e.direct:.2f}  walk {e.walk:.2f}  "
              f"strength {e.strength:.2f}")
        for facet in e.facets:
            print(f"        facet hit: {facet}")
        if e.carrier:
            src, kind = e.carrier
            print(f"        carried by: {src} ({kind}, hop {e.hop})")


def cmd_pack(args):
    b = _bundle(args)
    d = Dynamics(args.root)
    sys.stdout.write(context_pack(b, d, args.cue, budget=args.budget, k=args.k,
                       mode=args.mode, reactivate=not args.no_reactivate,
                       index=not args.no_index, compact=args.compact))


def cmd_source_index(args):
    """Build the private persistent index for one source directory."""
    try:
        index = source_retrieval.build_source_index(args.root, args.path)
    except (OSError, ValueError) as error:
        raise SystemExit(f"muninn source index: {error}") from error
    try:
        print(
            f"indexed {index.document_count} source files from "
            f"{index.metadata['source_root']} ({index.size_bytes} bytes)"
        )
    finally:
        index.close()


def _source_json(result, query: str, mode: str) -> dict:
    """Return the stable machine-readable form of one source search."""
    excerpts = {excerpt.path: excerpt for excerpt in result.pack.excerpts}
    hits = []
    for hit in result.hits:
        excerpt = excerpts.get(hit.path)
        item = {
            "path": hit.path,
            "score": hit.score,
            "arms": {name: rank for name, rank in hit.arms},
        }
        if excerpt is not None:
            item.update({
                "start_line": excerpt.start_line,
                "end_line": excerpt.end_line,
                "text": excerpt.text,
            })
        hits.append(item)
    return {
        "query": query,
        "mode": mode,
        "source_root": result.source_root,
        "index_rebuilt": result.rebuilt,
        "indexed_files": result.document_count,
        "estimated_tokens": result.pack.estimated_tokens,
        "hits": hits,
        "omitted_paths": list(result.pack.omitted_paths),
    }


def cmd_source_search(args):
    """Search source through the validated BM25F-led hybrid ranking."""
    try:
        result = source_retrieval.search_source(
            args.root,
            args.path,
            args.query,
            budget=args.budget,
            limit=args.limit,
            mode=args.mode,
            refresh=not args.no_refresh,
        )
    except (OSError, ValueError) as error:
        raise SystemExit(f"muninn source search: {error}") from error
    if args.json:
        print(json.dumps(_source_json(result, args.query, args.mode), indent=2))
        return
    if result.rebuilt:
        print(
            f"source index refreshed: {result.document_count} files",
            file=sys.stderr,
        )
    if not result.hits or not result.pack.excerpts:
        reason = ("No source file matched the query." if not result.hits
                  else "No source excerpt fit the budget.")
        text = "# Retrieved code context\n\n" + reason
        if len(text.encode("utf-8")) <= args.budget * 4:
            sys.stdout.write(text)
        else:
            print(reason, file=sys.stderr)
    else:
        sys.stdout.write(result.pack.text)


def cmd_volunteer(args):
    """Return precision-gated passive context and suppress repeated serves."""

    b = _bundle(args)
    d = Dynamics(args.root)
    session = args.session or os.environ.get("MUNINN_SESSION") or infer_session(d)
    served = session_metrics(d.ledger_path, session)["served"]
    text = volunteer_pack(
        b,
        d,
        args.cue,
        session=session,
        served_paths=served,
        k=args.k,
        budget=args.budget,
        reactivate=not args.no_reactivate,
    )
    if text:
        sys.stdout.write(text)


def cmd_goal(args):
    args.text = scrub(args.text) if args.text else args.text
    d = Dynamics(args.root)
    if not args.text:
        if args.off:  # retire without text: only when unambiguous,
            if not d.goals:  # and NEVER a silent no-op
                print("no active goal to retire")
                sys.exit(1)
            if len(d.goals) > 1:
                for g, w in sorted(d.goals.items(), key=lambda x: -x[1]):
                    print(f"  {w:.1f}  {g}")
                print('specify which goal to retire: '
                      'muninn goal "<text>" --off')
                sys.exit(1)
            (text,) = d.goals
            d.goal(text, 0.0)
            print(f"retired goal: {text}")
            return
        b = _bundle(args)  # per-goal: how many notes it can actually reach
        for g, w in sorted(d.goals.items(), key=lambda x: -x[1]):
            toks = tokens(g)
            n = sum(1 for note in b.notes.values()
                    if goal_alignment(toks, note) > 0)
            print(f"  {w:.1f}  {g}  (aligns with {n} of "
                  f"{len(b.notes)} notes)")
        return
    if args.off and args.text not in d.goals:
        # Retiring a goal that is not active must report an error rather than print
        # a success message over unchanged state
        for g, w in sorted(d.goals.items(), key=lambda x: -x[1]):
            print(f"  {w:.1f}  {g}")
        print(f"no active goal matches: {args.text}")
        sys.exit(1)
    d.goal(args.text, 0.0 if args.off else args.weight)
    print(f"{'retired' if args.off else 'active'} goal: {args.text}")


def _parse_ttl(text: str | None) -> float | None:
    """'90m' / '4h' / '2d' / plain seconds → seconds; None passes through
    (Dynamics applies its default). A malformed ttl fails loud."""
    if text is None:
        return None
    t = str(text).strip().lower()
    mult = {"m": 60, "h": 3600, "d": 86400}.get(t[-1:], None)
    try:
        value = float(t[:-1]) * mult if mult else float(t)
        if not math.isfinite(value):
            raise ValueError("duration must be finite")
        return value
    except ValueError:
        raise SystemExit(f"muninn intent: bad --ttl {text!r} "
                         "(want e.g. 90m, 4h, 2d, or seconds)")


def cmd_intent(args):
    """Announce/list/retire in-flight work. Awareness, never a lock: other
    consumers of a shared or synced ledger see active intents in their
    primed packs, overlap-flagged against their own working set."""
    d = Dynamics(args.root)
    branch = args.branch or current_branch(
        os.path.abspath(args.cwd or os.getcwd()))
    if args.done:
        if not branch:
            print("muninn intent --done: no --branch given and no git "
                  "branch detected")
            sys.exit(1)
        if branch not in d.intents:
            # Retiring no goal must report an error rather than print success (goal --off
            # discipline)
            for b_, i in sorted(d.active_intents().items()):
                print(f"  {b_}: {i.get('goal', '')}")
            print(f"no announced intent for branch: {branch}")
            sys.exit(1)
        d.intent_done(branch)
        print(f"intent retired: {branch}")
        return
    if not args.goal_text:
        active = d.active_intents()
        if not active:
            print("no active intents")
            return
        now = time.time()
        for b_, i in sorted(active.items(), key=lambda kv: -kv[1]["ts"]):
            age_m = max(1, int((now - i["ts"]) // 60))
            paths = ", ".join(i["paths"][:6]) or "-"
            print(f"  {b_}  ({age_m}m ago)  {i.get('goal', '')}  [{paths}]")
        return
    if not branch:
        print("muninn intent: no --branch given and no git branch detected "
              "(announce needs a branch to key on)")
        sys.exit(1)
    paths = [p.strip() for p in (args.paths or "").split(",") if p.strip()]
    d.intent(branch, paths=paths, goal=args.goal_text,
             ttl=_parse_ttl(args.ttl))
    print(f"intent announced: {branch}: {args.goal_text}"
          + (f" (touches {len(paths)} path(s))" if paths else "")
          + ": run `muninn sync` to share it")


def cmd_sync(args):
    """Synchronize append-only ledger events through a dedicated Git reference."""
    try:
        r = run_sync(args.root, remote=args.remote, ref=args.ref,
                     push=not args.pull_only, repo=args.repo)
    except SyncError as e:
        print(f"muninn sync: {e}", file=sys.stderr)
        sys.exit(1)
    bits = [f"{r['events']} events total",
            f"pulled {r['pulled']} new from {args.remote}"]
    if not args.pull_only:
        bits.append("nothing to publish yet" if r["events"] == 0
                    else "pushed" if r["pushed"] else "push skipped")
    if not r["fetched"]:
        bits.append("(remote ref not readable yet: first sync, or offline)")
    print("synced: " + "; ".join(bits))


PROTOCOL_BEGIN = "<!-- muninn:protocol:begin (managed: muninn install refreshes this block) -->"
PROTOCOL_END = "<!-- muninn:protocol:end -->"
PROTOCOL_HEADING = "# Muninn agent protocol"
LEGACY_PROTOCOL_HEADING = "muninn: use the knowledge"


def _contains_protocol(text: str) -> bool:
    """Check managed structure before accepting current or legacy content."""
    if PROTOCOL_BEGIN in text or PROTOCOL_END in text:
        span = style.find_block(text, PROTOCOL_BEGIN, PROTOCOL_END)
        if span is None:
            return False
        text = text[span[0] + len(PROTOCOL_BEGIN):span[1] - len(PROTOCOL_END)]
        return any(heading in text and text.partition(heading)[2].strip()
                   for heading in (PROTOCOL_HEADING, LEGACY_PROTOCOL_HEADING))
    return PROTOCOL_HEADING in text or LEGACY_PROTOCOL_HEADING in text


def _managed_block(root: str) -> str:
    return f"{PROTOCOL_BEGIN}\n{skill.render(root)}\n{PROTOCOL_END}\n"


def _protocol_matches_root(text: str, root: str) -> bool:
    """Verify managed command roots while retaining markerless legacy support."""
    if not _contains_protocol(text):
        return False
    if PROTOCOL_BEGIN not in text and PROTOCOL_END not in text:
        return True
    span = style.find_block(text, PROTOCOL_BEGIN, PROTOCOL_END)
    if span is None:
        return False
    prime = False
    for line in text[span[0]:span[1]].splitlines():
        if not line.startswith("    muninn --root "):
            continue
        try:
            command = shlex.split(line)
        except ValueError:
            return False
        if (len(command) < 4 or os.path.realpath(os.path.expanduser(command[2]))
                != os.path.realpath(root)):
            return False
        prime |= command[3:] == ["prime"]
    return prime


def _write_copy(path: str, root: str) -> str:
    """Write/refresh the managed protocol block in a REAL file, keeping
    any user prose outside the markers. This is the copy fallback for
    platforms or clients that cannot use symbolic links. Damaged markers
    leave the file unchanged because replacing across a broken
    span could swallow user prose."""
    from .style import find_block  # one marker discipline, one implementation
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        text = ""
    span = find_block(text, PROTOCOL_BEGIN, PROTOCOL_END)
    if span:
        new = text[:span[0]] + _managed_block(root).rstrip("\n") + text[span[1]:]
        verb = "refreshed"
    elif PROTOCOL_BEGIN in text or PROTOCOL_END in text:
        return ("left untouched: its muninn protocol markers are damaged "
                "(repair or delete the old block, then re-run)")
    else:
        new = (text + ("\n" if text and not text.endswith("\n") else "")
               + _managed_block(root))
        verb = "appended to" if text else "wrote"
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(new)
    return verb


GLOBAL_SLOTS = (  # Each client reads its slot at session initialization.
    # Wiring these paths does not require import syntax or model cooperation.
    os.path.join("~", ".claude", "CLAUDE.md"),
    os.path.join("~", ".codex", "AGENTS.md"),
    os.path.join("~", ".gemini", "GEMINI.md"),
    os.path.join("~", ".grok", "AGENTS.md"),
)


def _install_global(args) -> bool:
    canonical = os.path.abspath(os.path.expanduser(
        args.source or os.path.join("~", "AGENTS.md")))
    if not os.path.isfile(canonical):
        print(f"muninn install --global: canonical file not found: "
              f"{canonical}\n  create your personal AGENTS.md there (or "
              "point --from at it): personality/style prose, plus any "
              "personal-bundle pointer")
        sys.exit(1)
    wired = 0
    with open(canonical, encoding="utf-8") as fh:
        canonical_text = fh.read()
    for slot in GLOBAL_SLOTS:
        p = os.path.expanduser(slot)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        if os.path.islink(p):
            if os.readlink(p) == canonical:
                wired += 1
                continue
            print(f"  {slot}: links elsewhere ({os.readlink(p)}): "
                  "left as-is")
            continue
        if os.path.exists(p):
            if os.path.isfile(p):
                with open(p, encoding="utf-8") as fh:
                    if fh.read() == canonical_text:
                        wired += 1
                        print(f"  {slot}: existing copy is current.")
                        continue
            print(f"  {slot}: real file exists: left as-is (merge your "
                  f"content into {canonical} through a reviewed merge)")
            continue
        try:
            os.symlink(canonical, p)
            print(f"  {slot} -> {canonical}")
            wired += 1
        except OSError:
            shutil.copyfile(canonical, p)
            print(f"  {slot}: symlink unavailable: copied (refresh with "
                  "install --global after editing the canonical file)")
            wired += 1
    print(f"global slots wired: {wired}/{len(GLOBAL_SLOTS)}. "
          "Check instruction discovery in each client.")
    return wired == len(GLOBAL_SLOTS)


def _global_wiring_status(canonical: str) -> bool:
    """Print every global instruction slot and return whether all agree."""
    ok = True
    try:
        with open(canonical, encoding="utf-8") as fh:
            canonical_text = fh.read()
    except OSError:
        canonical_text = None
    for slot in GLOBAL_SLOTS:
        p = os.path.expanduser(slot)
        if os.path.islink(p) and os.readlink(p) == canonical:
            print(f"  {slot}: symlink -> canonical")
        elif os.path.isfile(p):
            try:
                with open(p, encoding="utf-8") as fh:
                    same = (canonical_text is not None
                            and fh.read() == canonical_text)
            except OSError:
                same = False
            if same:
                print(f"  {slot}: copy, in sync")
            else:
                print(f"  {slot}: DIVERGED from canonical: this harness "
                      "sees different global instructions. Preserve both "
                      "files and review a merge before running installation again.")
                ok = False
        else:
            print(f"  {slot}: missing (fix: muninn install --global)")
            ok = False
    return ok


def _doctor_global(args) -> None:
    canonical = os.path.abspath(os.path.expanduser(
        args.source or os.path.join("~", "AGENTS.md")))
    ok = _global_wiring_status(canonical)
    print("global wiring: " + ("OK" if ok else "BROKEN"))
    if not ok:
        sys.exit(1)


def _install_home(args, show_paste: bool = True) -> bool:
    """Initialize the home bundle and connect supported agent clients.

    A new canonical file receives the managed protocol. An existing file
    receives a short pointer. The function preserves unmanaged content and
    prints hook configuration when requested.
    """
    brain = os.path.abspath(os.path.expanduser(
        args.brain or home.home_root()))
    canonical = os.path.abspath(os.path.expanduser(
        args.source or os.path.join("~", "AGENTS.md")))
    ok = True
    try:
        with open(canonical, encoding="utf-8") as fh:
            text = fh.read()
    except FileNotFoundError:
        if os.path.lexists(canonical) or not os.path.isdir(os.path.dirname(canonical)):
            raise SystemExit("muninn install: the canonical instruction path is invalid. "
                             "Select an existing parent directory and a regular file.") from None
        text = None
    except (OSError, UnicodeError) as error:
        raise SystemExit(f"muninn install: cannot read the canonical instruction file: {error}") from None
    if (text is not None and home.POINTER_MARK in text
            and not home.pointer_matches(text, brain)):
        raise SystemExit("muninn install: the canonical home pointer is damaged or selects "
                         "another root. Preserve and review it before setup.")
    try:
        fresh = home.init_home(brain)
    except (OSError, ValueError) as error:
        raise SystemExit(f"muninn install: cannot initialize the home bundle: {error}") from None
    print(("Initialized" if fresh else "Found")
          + f" the home knowledge base at {brain}. Project rooms appear "
            "under projects/ after qualifying activity.")
    if text is None:
        with open(canonical, "w", encoding="utf-8") as fh:
            fh.write("# Global agent instructions\n\n"
                     + _managed_block(brain))
        print(f"Wrote the Muninn protocol to the canonical file at {canonical}.")
    elif PROTOCOL_BEGIN in text or PROTOCOL_END in text:
        verdict = _write_copy(canonical, brain)
        ok = verdict == "refreshed"
        if ok:
            print(f"Refreshed the managed protocol block in {canonical}.")
        else:
            print(f"{canonical}: {verdict}")
    elif home.POINTER_MARK in text:
        print(f"The canonical file at {canonical} already points to Muninn.")
    else:  # a real personal file: the two-line pointer, nothing more
        with open(canonical, "a", encoding="utf-8") as fh:
            if not text.endswith("\n"):
                fh.write("\n")
            fh.write("\n" + home.pointer_lines(brain))
        print(f"Appended the two-line Muninn pointer to {canonical}. "
              "Existing prose was preserved.")
    args.source = canonical
    ok = _install_global(args) and ok
    if show_paste:
        print("\nAdd these Claude Code hooks to ~/.claude/settings.json, or "
              "run `muninn setup` to merge them automatically:")
        print(hook_config(brain))
    print("\nRegister an existing project with `muninn adopt <dir>`. "
          "Qualifying observed activity can also create its room.")
    print("Optional style adoption: run `muninn style adopt <repo>` to give "
          "each agent the declared core and document routes.")
    return ok


def _is_muninn_hook_entry(entry) -> bool:
    """An entry setup owns (and may replace on re-run): any of its hook
    commands invokes `muninn ... hook ...`. Everything else in the
    user's settings is never touched."""
    if not isinstance(entry, dict):
        return False
    for h in entry.get("hooks", []) if isinstance(entry.get("hooks"), list) else []:
        cmd = h.get("command") if isinstance(h, dict) else None
        if isinstance(cmd, str) and "muninn" in cmd and " hook " in cmd:
            return True
    return False


def _wire_hook_file(brain: str, path: str, adapter: str) -> str:
    """Merge one adapter's hooks without replacing user configuration."""
    path = os.path.expanduser(path)
    try:
        with open(path, encoding="utf-8") as fh:
            raw = fh.read()
    except OSError:
        raw = None
    try:
        data = json.loads(raw) if raw and raw.strip() else {}
    except ValueError:
        return "left untouched: it is not valid JSON; repair it and re-run"
    if not isinstance(data, dict) or not isinstance(data.get("hooks", {}), dict):
        return "left untouched: its hooks object has an unexpected shape"
    ours = json.loads(hook_config(brain, adapter=adapter))["hooks"]
    hooks = data.setdefault("hooks", {})
    changed = False
    for event, entries in ours.items():
        cur = hooks.get(event, [])
        if not isinstance(cur, list):
            return f"left untouched: hooks.{event} is not a list"
        merged = [e for e in cur if not _is_muninn_hook_entry(e)] + entries
        if merged != cur:
            hooks[event] = merged
            changed = True
    if not changed:
        return "already wired"
    if raw is not None and not os.path.exists(path + ".muninn-bak"):
        with open(path + ".muninn-bak", "w", encoding="utf-8") as fh:
            fh.write(raw)  # the pre-muninn state, kept exactly once
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)
    return ("wired: your existing settings preserved"
            + (f" (pre-muninn backup: {path}.muninn-bak)"
               if raw is not None else ""))


HOOK_TARGETS = (
    ("claude", os.path.join("~", ".claude", "settings.json"), "claude"),
    ("codex", os.path.join("~", ".codex", "hooks.json"), "codex"),
    ("grok", os.path.join("~", ".grok", "hooks", "muninn.json"), "grok"),
)

def _install_claude_skill(brain: str) -> str:
    """The muninn protocol as a Claude skill, discovered automatically."""
    path = os.path.expanduser(os.path.join("~", ".claude", "skills",
                                           "muninn", "SKILL.md"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(skill.render(brain))
    return path


def _setup_manifest(args) -> None:
    """Print every path that setup would create or modify."""
    brain = os.path.abspath(os.path.expanduser(
        args.brain or home.home_root()))
    canonical = os.path.abspath(os.path.expanduser(
        args.source or os.path.join("~", "AGENTS.md")))
    print("Muninn setup would modify only these paths:")
    print(f"  {brain}/: home knowledge base "
          + ("(exists)" if home.is_home(brain) else "(would create)"))
    print(f"  {canonical}: canonical global file "
          + ("(exists: gains a two-line pointer at most)"
             if os.path.isfile(canonical) else "(would create)"))
    for slot in GLOBAL_SLOTS:
        p = os.path.expanduser(slot)
        state = ("symlink present" if os.path.islink(p)
                 else "REAL FILE: left as-is" if os.path.exists(p)
                 else "would symlink -> canonical")
        print(f"  {slot}: {state}")
    for label, target, _adapter in HOOK_TARGETS:
        path = os.path.expanduser(target)
        print(f"  {path}: {label} lifecycle hooks merged in "
              "(your entries kept; backup once; refused if unparseable)")
    print("  ~/.claude/skills/muninn/SKILL.md: written")
    print("\nNo path was changed. Run `muninn setup` to apply this manifest. "
          "Run `muninn uninstall` to remove managed configuration later.")


def cmd_setup(args):
    """Create the home bundle and connect supported agent clients.

    Repeated execution changes only Muninn-managed content. ``--dry-run``
    prints the complete manifest without modifying a path.
    """
    if args.dry_run:
        _setup_manifest(args)
        return
    ok = _install_home(args, show_paste=False)
    brain = os.path.abspath(os.path.expanduser(
        args.brain or home.home_root()))
    for label, target, adapter in HOOK_TARGETS:
        verdict = _wire_hook_file(brain, target, adapter)
        print(f"\n{os.path.expanduser(target)}: {verdict}")
        ok = _hook_file_is_wired(brain, target, adapter) and ok
    print(f"Claude skill: {_install_claude_skill(brain)}")
    print("\nCodex users must review the new global hooks through `/hooks`. "
          "Grok records session startup without treating hook output as "
          "model context; its global instructions require an explicit prime.")
    if not ok:
        print("Setup is incomplete. Preserve the reported conflicts. "
              "Resolve them through a reviewed change, then run setup and "
              "`muninn doctor --home` again.")
        sys.exit(1)
    print("Setup is complete. Start a session in any project. "
          "`muninn doctor --home` verifies the configuration, and "
          "`muninn uninstall` removes managed wiring without deleting knowledge.")


def _unwire_hook_file(path: str) -> bool:
    """Remove Muninn hook entries and preserve every other JSON value."""
    path = os.path.expanduser(path)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict) or not isinstance(data.get("hooks"), dict):
        return False
    changed = False
    for event in list(data["hooks"]):
        cur = data["hooks"][event]
        if not isinstance(cur, list):
            continue
        kept = [entry for entry in cur if not _is_muninn_hook_entry(entry)]
        if kept == cur:
            continue
        changed = True
        if kept:
            data["hooks"][event] = kept
        else:
            del data["hooks"][event]
    if not changed:
        return False
    if data == {"hooks": {}} and not os.path.exists(path + ".muninn-bak"):
        os.unlink(path)
        return True
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)
    return True


def cmd_uninstall(args):
    """Remove setup-managed configuration while preserving knowledge."""
    removed = []
    for _label, target, _adapter in HOOK_TARGETS:
        if _unwire_hook_file(target):
            removed.append(f"muninn hook entries from {target}")
    skl = os.path.expanduser(os.path.join("~", ".claude", "skills", "muninn"))
    if os.path.isdir(skl):
        shutil.rmtree(skl, ignore_errors=True)
        removed.append("~/.claude/skills/muninn/")
    canonical = os.path.abspath(os.path.expanduser(
        args.source or os.path.join("~", "AGENTS.md")))
    for slot in GLOBAL_SLOTS:
        p = os.path.expanduser(slot)
        if os.path.islink(p) and os.readlink(p) == canonical:
            os.unlink(p)  # only the symlinks setup made; copies and
            removed.append(slot)  # real files are the user's
    for r in removed:
        print(f"  removed: {r}")
    if not removed:
        print("nothing to remove: no muninn wiring found")
    brain = os.path.abspath(os.path.expanduser(
        args.brain or home.home_root()))
    print(f"Knowledge was preserved at {brain}, and the canonical file was "
          f"preserved at {canonical}. Remove either path separately if required.")


def cmd_adopt(args):
    """Register a project room and preserve existing agent instructions."""
    brain = os.path.abspath(os.path.expanduser(
        args.brain or home.home_root()))
    if not home.is_home(brain):
        print(f"No home knowledge base exists at {brain}. Run `muninn setup`.")
        sys.exit(1)
    room, verb = home.adopt(brain, args.dir)
    proj = os.path.abspath(args.dir)
    if room is None:
        if verb == "conflicting pointer":
            raise SystemExit("muninn adopt: the existing home pointer is damaged or "
                             "selects another root. Preserve and review it before adoption.")
        real, br = os.path.realpath(proj), os.path.realpath(brain)
        if real == br or real.startswith(br + os.sep):
            print(f"{proj} is inside the home knowledge base and cannot be a room.")
        else:
            print(f"room limit reached ({home.ROOM_KEYS}): prune "
                  "stale entries in .muninn/rooms.json, then re-run")
        sys.exit(1)
    print(f"adopted {proj} -> {room}")
    print(f"  AGENTS.md: {verb}"
          + ("" if verb == "already wired" else " (two-line pointer)"))


def cmd_style(args):
    """Adopt, refresh, or inspect a public style-repository contract."""
    brain = os.path.abspath(os.path.expanduser(
        args.brain or home.home_root()))
    if args.action in ("adopt", "refresh"):
        if not home.is_home(brain):
            print(f"No home knowledge base exists at {brain}. Run `muninn setup`.")
            sys.exit(1)
        if args.action == "adopt":
            if not args.repo or not os.path.isdir(args.repo):
                print("muninn style adopt: provide a style repository that "
                      f"contains {style.MANIFEST}")
                sys.exit(1)
            repo = os.path.abspath(os.path.expanduser(args.repo))
        else:
            mounted = style.mounted_repo(brain)
            if not mounted:
                print("muninn style refresh: no repository is adopted; run "
                      "muninn style adopt <repo>")
                sys.exit(1)
            repo = os.path.abspath(os.path.expanduser(args.repo or mounted))
            if os.path.realpath(repo) != os.path.realpath(mounted):
                print("muninn style refresh: the supplied repository is not "
                      "the adopted repository")
                sys.exit(1)
        try:
            if args.pull:
                print(f"style source: {style.pull_ff_only(repo)}")
            contract = style.load_contract(repo)
            if args.action == "adopt":
                verb = style.mount(brain, repo)
                print(f"style/: {verb} ({repo})")
            router = style.render_router(repo, brain, contract)
        except (style.ContractError, style.MountError) as e:
            print(f"muninn style {args.action}: {e}")
            sys.exit(1)
        canonical = os.path.abspath(os.path.expanduser(
            args.source or os.path.join("~", "AGENTS.md")))
        try:
            verb = style.wire_block(canonical, router)
            skill_path = style.write_skill(repo, brain, router)
        except style.MountError as e:
            print(f"muninn style {args.action}: {e}")
            sys.exit(1)
        style.record_adoption(brain, repo, contract, router)
        print(f"style instructions: {verb} {canonical}")
        print(f"style skill: {skill_path}")
        print(f"routed {len(contract.guides)} guide(s) and "
              f"{len(contract.templates)} template(s); learned preferences "
              f"remain in {os.path.join(brain, style.LEARNED_OVERLAY)}/")
        return
    if args.action not in ("", "status"):
        print(f"muninn style: unknown action {args.action!r}; use adopt, "
              "refresh, or status")
        sys.exit(1)
    repo = style.mounted_repo(brain)
    if not repo:
        print("No style repository is mounted. Run `muninn style adopt <repo>` "
              "to add its core rules and routes to the global instructions.")
        return
    print(f"style repo: {repo}")
    try:
        contract = style.load_contract(repo)
    except style.ContractError as e:
        print(f"  INVALID: {e}")
        sys.exit(1)
    revision = style.source_revision(repo)
    print(f"  {len(contract.guides)} guide(s), "
          f"{len(contract.templates)} template(s)")
    if revision.get("kind") == "git":
        print(f"  revision: {revision.get('commit', '')[:12]}; "
              f"ahead {revision.get('ahead', 0)}, "
              f"behind {revision.get('behind', 0)}")
    print("  refresh: muninn style refresh --pull")


def cmd_install(args):
    """Connect supported project instruction files to one Muninn protocol.

    The command adds the protocol to `AGENTS.md` and points `CLAUDE.md` and
    `GEMINI.md` to that file when safe. Existing regular files remain in place,
    and repeated execution is idempotent. `--home` configures the shared home
    knowledge base instead.
    """
    if args.home_:
        if not _install_home(args):
            print("Home installation is incomplete. Preserve and resolve the reported conflicts.")
            sys.exit(1)
        return
    if args.global_:
        if not _install_global(args):
            print("Global installation is incomplete. Preserve and resolve the reported conflicts.")
            sys.exit(1)
        return
    proj = os.path.abspath(args.dir)
    root = os.path.abspath(args.root)
    agents = os.path.join(proj, "AGENTS.md")
    try:
        with open(agents, encoding="utf-8") as fh:
            existing = fh.read()
    except OSError:
        existing = ""
    if ((PROTOCOL_BEGIN in existing or PROTOCOL_END in existing)
            and style.find_block(existing, PROTOCOL_BEGIN, PROTOCOL_END) is None):
        print(f"{agents}: the protocol markers are damaged. "
              "The file remains unchanged. Repair the markers before installation.")
        sys.exit(1)
    if _contains_protocol(existing):
        print(f"AGENTS.md already carries the muninn protocol ({agents})")
    else:
        with open(agents, "a", encoding="utf-8") as fh:
            if existing and not existing.endswith("\n"):
                fh.write("\n")
            if existing:
                fh.write("\n")
            fh.write(skill.render(root))
        print(("appended to" if existing else "wrote") + f" {agents}")
    ok = True
    for name in ("CLAUDE.md", "GEMINI.md"):
        p = os.path.join(proj, name)
        if os.path.islink(p):
            continue  # already linked (to AGENTS.md or wherever the user chose)
        if args.copy or os.path.exists(p):
            # copy mode, or a real file already there: manage a marker
            # block inside it instead. This preserves user prose on platforms
            # that cannot use symbolic links.
            if args.copy:
                verb = _write_copy(p, root)
                ok = not verb.startswith("left untouched") and ok
                print(f"  {name}: {verb} managed protocol block")
            else:
                print(f"  {name} exists and is a real file: left as-is; "
                      "re-run with --copy to manage a protocol block in it")
            continue
        try:
            os.symlink("AGENTS.md", p)
            print(f"  {name} -> AGENTS.md")
        except OSError as e:  # e.g. Windows without link privileges:
            verb = _write_copy(p, root)  # fall back to the managed copy
            ok = not verb.startswith("left untouched") and ok
            print(f"  {name}: symlink unavailable ({e.__class__.__name__}) "
                  f": {verb} managed protocol block instead")
    if not ok:
        print("Project installation is incomplete. Preserve and repair the damaged markers before retrying.")
        sys.exit(1)
    print(f"AGENTS.md is now the canonical entry point; bundle: {root}")
    print("  (Claude Code: also install the hooks: muninn hook "
          "print-config: for ambient capture + boot packs; other "
          "harnesses follow the protocol text itself)")


def _hook_file_is_wired(brain: str, target: str, adapter: str) -> bool:
    """Return whether one hook file contains the current Muninn entries."""
    try:
        with open(os.path.expanduser(target), encoding="utf-8") as fh:
            data = json.load(fh)
        actual = data["hooks"]
        expected = json.loads(hook_config(brain, adapter=adapter))["hooks"]
    except (OSError, ValueError, KeyError, TypeError):
        return False
    if not isinstance(actual, dict):
        return False
    for event, entries in expected.items():
        current = actual.get(event)
        if not isinstance(current, list):
            return False
        if [entry for entry in current if _is_muninn_hook_entry(entry)] != entries:
            return False
    return True


def _doctor_home(args) -> None:
    """Verify the home bundle, canonical instructions, and project rooms."""
    brain = os.path.abspath(os.path.expanduser(
        args.brain or home.home_root()))
    if not home.is_home(brain):
        print(f"  home knowledge base: missing at {brain} (run `muninn setup`)")
        print("home configuration: invalid")
        sys.exit(1)
    ok = True
    print(f"  home knowledge base: {brain}")
    for d in (home.ROOMS_DIR,) + home.CROSS_DIRS:
        good = os.path.isdir(os.path.join(brain, d))
        print(f"  {d}/: " + ("present" if good else
                             "MISSING (fix: muninn install --home)"))
        ok &= good
    print(f"  rooms: {len(home.rooms(brain))} registered")
    canonical = os.path.abspath(os.path.expanduser(
        args.source or os.path.join("~", "AGENTS.md")))
    try:
        with open(canonical, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        text = ""
    wired = _protocol_matches_root(text, brain)
    if PROTOCOL_BEGIN not in text and PROTOCOL_END not in text:
        wired = wired or home.pointer_matches(text, brain)
    print(f"  {canonical}: "
          + ("points at muninn" if wired
             else "NO muninn pointer (fix: muninn install --home)"))
    ok &= wired
    ok &= _global_wiring_status(canonical)
    # install --home prints hook configuration without modifying clients.
    # Once setup creates any managed surface, all setup surfaces are required.
    skl = os.path.expanduser(os.path.join("~", ".claude", "skills",
                                          "muninn", "SKILL.md"))
    setup_expected = (os.path.isfile(skl) or any(
        os.path.lexists(os.path.expanduser(target))
        for _label, target, _adapter in HOOK_TARGETS))
    hooks_ok = True
    for label, target, adapter in HOOK_TARGETS:
        wired_hooks = _hook_file_is_wired(brain, target, adapter)
        hooks_ok &= wired_hooks
        basename = os.path.basename(os.path.expanduser(target))
        print(f"  {label} hooks: "
              + (f"written into {basename}" if wired_hooks
                 else "not written or stale (muninn setup wires them)"))
    skill_installed = os.path.isfile(skl)
    print("  claude skill: " + ("installed" if skill_installed
                                else "not installed (muninn setup)"))
    if setup_expected:
        ok &= hooks_ok and skill_installed
    srepo = style.mounted_repo(brain)
    if srepo:
        print(f"  style: mounted from {srepo}")
        issues = style.verify_adoption(brain, srepo, canonical)
        if issues:
            print("    style contract: STALE")
            for issue in issues:
                print(f"      - {issue}")
            print("      fix: muninn style refresh --pull")
            ok = False
        else:
            print("    style contract: core, routes, skill, and revision current")
    else:
        print("  style: no repo mounted (optional: "
              "muninn style adopt <repo>)")
    print("home configuration: " + ("valid" if ok else "invalid"))
    if not ok:
        sys.exit(1)


def cmd_doctor(args):
    """Verify agent instruction files and confirm that the bundle loads."""
    if args.home_:
        _doctor_home(args)
        return
    if args.global_:
        _doctor_global(args)
        return
    proj = os.path.abspath(args.dir)
    ok = True
    agents = os.path.join(proj, "AGENTS.md")
    try:
        with open(agents, encoding="utf-8") as fh:
            ok_agents = _contains_protocol(fh.read())
    except OSError:
        ok_agents = False
    print(f"  AGENTS.md: {'protocol present' if ok_agents else 'MISSING or no protocol: run: muninn install'}")
    ok &= ok_agents
    for name in ("CLAUDE.md", "GEMINI.md"):
        p = os.path.join(proj, name)
        if os.path.islink(p):
            tgt = os.readlink(p)
            good = tgt == "AGENTS.md"
            print(f"  {name}: symlink -> {tgt}"
                  + ("" if good else "  (expected AGENTS.md)"))
            ok &= good
        elif os.path.exists(p):
            with open(p, encoding="utf-8") as fh:
                text = fh.read()
            if PROTOCOL_BEGIN in text:
                print(f"  {name}: managed copy (refresh: muninn install "
                      "--copy)")
            else:
                print(f"  {name}: real file WITHOUT the protocol: this "
                      "harness will not see muninn (fix: muninn install "
                      "--copy)")
                ok = False
        else:
            print(f"  {name}: missing (fix: muninn install)")
            ok = False
    b = _bundle(args)
    print(f"  bundle: {len(b.notes)} note(s) at {os.path.abspath(args.root)}")
    print("entry-point wiring: " + ("OK" if ok else "BROKEN"))
    if not ok:
        sys.exit(1)


def cmd_feedback(args):
    """One preference observation: the agent's reflex whenever the user
    corrects, rewrites, or reacts to output. Raw signal only: files
    change later, and only when the pattern repeats (muninn evolve)."""
    note_feedback(Dynamics(args.root), args.text, domain=args.domain,
                  polarity=1 if args.positive else -1, session=args.session)
    print("feedback noted (rules promote after repeated evidence: "
          "muninn evolve shows the state)")


def cmd_evolve(args):
    """The evolution loop's pass + status: cluster feedback, promote
    what repeated across sessions, supersede what got contradicted.
    Runs ambiently at session end; this is the manual pass/report."""
    b = _bundle(args)
    d = Dynamics(args.root)
    r = evolve_once(b, d)
    for t in r["promoted"]:
        print(f"  promoted: {t}")
    for t in r["demoted"]:
        print(f"  superseded: {t}")
    for c in r["candidates"]:
        print(f"  candidate ({c['count']}x, needs repetition across "
              f"sessions): {c['text']}")
    active = [x for x in d.rules.values() if x.get("status") != "superseded"]
    print(f"learned rules: {len(active)} active "
          f"({len(d.rules) - len(active)} superseded): style-learned/")


def cmd_journal(args):
    """Episodic memory: distill a session into a thread: the compiled
    head (--state) over dated episodes (--body). The agent writes these
    at milestones and wrap-up; the next session's prime OPENS with the
    matching thread, so a fresh session picks up where the last left
    off. No thread argument: list threads."""
    b = _bundle(args)
    d = Dynamics(args.root)
    if not args.thread:
        threads = threads_of(b)
        if not threads:
            print('no threads yet: muninn journal "<topic>" '
                  '--body "<what happened>" --state "<current state>"')
            return
        for slug, t in sorted(threads.items()):
            title = t["head"].title if t["head"] else slug
            print(f"  {slug}  ({len(t['episodes'])} episode(s))  {title}")
        return
    if args.show:
        t = threads_of(b).get(args.thread) or threads_of(b).get(
            args.thread.lower().replace(" ", "-"))
        if not t:
            print(f"no thread matches: {args.thread}")
            sys.exit(1)
        if t["head"]:
            print(f"# {t['head'].title}\n\n{t['head'].body.strip()}\n")
        for p, ep in t["episodes"][:5]:
            print(f"--- {ep.title}\n{ep.body.strip()}\n")
        return
    body = args.body or (stdin_text() if not sys.stdin.isatty() else "")
    if not body and not args.state:
        print("muninn journal: give --body (the episode) and/or --state "
              "(the compiled head)")
        sys.exit(1)
    slug = add_episode(b, d, args.thread, body or "(state update only)",
                       state=args.state or None)
    b.generate_index()
    print(f"journaled: {slug} (+1 episode"
          + (", head updated)" if args.state else ")"))


def cmd_import_transcripts(args):
    """Encode an existing conversation archive (~/.claude JSONL) into
    episode notes: explicit and user-invoked, never ambient. Secrets
    are scrubbed; re-runs skip already-imported sessions."""
    b = _bundle(args)
    d = Dynamics(args.root)
    imported, skipped = import_transcripts(b, d, args.path,
                                           thread=args.thread or None,
                                           cap=args.max_sessions)
    b.generate_index()
    print(f"imported {imported} session(s) as episodes"
          + (f" ({skipped} skipped: already imported or empty)"
             if skipped else ""))


def cmd_lesson(args):
    """Record a corrective note with optional path guards.

    The agent identifies a relevant failure or user correction and records
    the prevention rule. Muninn does not read the conversation itself.
    """
    d = Dynamics(args.root)
    if not args.title:
        b = _bundle(args)
        lessons = [(p, n) for p, n in sorted(b.notes.items())
                   if str(n.meta.get("type", "")) == "lesson"]
        if not lessons:
            print("no lessons yet: muninn lesson \"what went wrong\" "
                  "--guards <paths> --body \"how to avoid it\"")
            return
        for p, n in lessons:
            gs = ", ".join(n.guards()) or "(no guards: every session)"
            print(f"  {n.title}  [{gs}]  ({p})")
        return
    b = _bundle(args)
    title = scrub(args.title)
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60]
    rel = f"lessons/{slug or 'lesson'}.md"
    guards = [g.strip() for g in (args.guards or "").split(",") if g.strip()]
    meta = {"type": "lesson", "title": title, "pinned": True,
            "provenance": "curated", "generated": generated_stamp()}
    if guards:
        meta["guards"] = guards
    body = args.body or (stdin_text() if not sys.stdin.isatty() else "")
    b.write_note(rel, meta, scrub(body) if body else title)
    # the burn marks its precursors: what was being worked on when the
    # user got burned is salient context for the lesson
    d.outcome(float(args.valence), note=rel, why=args.title)
    b.generate_index()
    print(f"lesson recorded: {rel}"
          + (f" (guards: {', '.join(guards)})" if guards
             else " (no guards: served every session)"))


def cmd_review(args):
    """The reflection loop's observability: recent served-vs-used metrics
    and the active adaptations. --session runs one review by hand (the
    session-end hook normally does this ambiently)."""
    b = _bundle(args)
    d = Dynamics(args.root)
    if args.session:
        m = review_session(b, d, args.session)
        if m is None:
            print(f"nothing to review for session {args.session} "
                  "(unknown, empty, or already reviewed)")
            sys.exit(1)
        print(f"reviewed {args.session}: {len(m['hits'])} hit(s), "
              f"{len(m['misses'])} miss(es), {len(m['waste'])} wasted"
              + (f", mapped {len(m['built'])} gap file(s)" if m["built"]
                 else ""))
        return
    revs = recent_reviews(d.ledger_path, k=args.k)
    if not revs:
        print("no reviews yet: the session-end hook seals one per session"
              " (or run: muninn review --session <id>)")
        return
    hits = misses = waste = 0
    for ev in revs:
        h, mi, wa = (len(ev.get("hits", [])), len(ev.get("misses", [])),
                     len(ev.get("waste", [])))
        hits, misses, waste = hits + h, misses + mi, waste + wa
        line = (f"  {ev.get('session')}  {h} hit(s), {mi} miss(es), "
                f"{wa} wasted")
        if ev.get("built"):
            line += f", mapped {len(ev['built'])} gap file(s)"
        print(line)
    total = max(1, hits + misses)
    print(f"last {len(revs)} session(s): pack hit rate "
          f"{100 * hits / total:.0f}% ({hits}/{total} used notes served)")
    if d.assocs:
        print(f"active learned associations: {len(d.assocs)}"
              " (bounded lifts; decay when unreinforced)")
    damped = sorted(n for n, c in d.serve_miss.items() if c >= 3)
    if damped:
        print("damped from packs (served, never used: resets on use): "
              + ", ".join(damped[:6]))
    gaps = sorted((p for p, c in d.gap_counts.items() if c >= 2))
    if gaps:
        print("unmapped files in active use: " + ", ".join(gaps[:6]))


def cmd_prime(args):
    """Involuntary recall: cue from the situation, not a typed query."""
    b = _bundle(args)
    d = Dynamics(args.root)
    d.consolidate_if_due()  # prime is a Sense intake: decay is ambient too
    cue = prime_cue(b, d, workdir=args.cwd)
    where = threads_section(b, d, cue=cue, session=args.session)
    if where:  # continuity FIRST: where we left off, then repo facts
        print(where + "\n")
    print(context_pack(b, d, cue, budget=args.budget, k=args.k, primed=True,
                       session=args.session, index=not args.no_index,
                       compact=args.compact))
    lessons = lessons_section(b, d, workdir=args.cwd, session=args.session)
    if lessons:  # Corrective knowledge appears when its path guard matches.
        print("\n" + lessons)
    w = whisper_section(d)
    if w:  # unconfirmed preferences: lean this way, not yet law
        print("\n" + w)
    inflight = inflight_section(b, d, workdir=args.cwd)
    if inflight:  # awareness rides outside the pack budget (bounded)
        print("\n" + inflight)


def cmd_observe(args):
    """The Sense intake. Ambient senses must never error a hook: this
    command stays silent and exits 0 no matter what (stderr one-liner on
    real failures only)."""
    try:
        kind, note = args.kind, args.note
        valence, session = args.valence, args.session
        if args.json:  # one event as JSON on stdin instead of flags
            ev = json.loads(stdin_text() or "{}")
            if not isinstance(ev, dict):
                return
            kind = ev.get("kind", kind)
            note = ev.get("note", note)
            valence = float(ev.get("valence", valence) or 0.0)
            session = ev.get("session", session)
        if kind not in KINDS:
            print(f"muninn observe: unknown kind {kind!r}", file=sys.stderr)
            return
        if not math.isfinite(valence):
            raise ValueError("valence must be finite")
        observe_event(_bundle(args), Dynamics(args.root), kind, note,
                      valence, session)
    except Exception as e:  # never a traceback, never a nonzero exit
        print(f"muninn observe: {e}", file=sys.stderr)


def _hook(args):
    if args.event == "print-config":
        root = args.root if args.root != "." else os.environ.get(
            "MUNINN_ROOT", args.root)
        print(hook_config(os.path.abspath(os.path.expanduser(root)),
                          adapter=args.adapter))
        return
    # PRIVACY: session_id and file_path are the ONLY hook-JSON fields that
    # may flow onward (tool_name picks touch-vs-encode, then dies here)
    sid, tool, fp, adapter = hook_payload(stdin_text())
    if args.event == "session-end":  # ambient decay + the reflection loop
        d = Dynamics(args.root)
        d.consolidate_if_due()
        b = _bundle(args)
        if sid:
            if not os.environ.get("MUNINN_NO_REVIEW"):
                review_session(b, d, sid, workdir=os.getcwd())
        else:
            review_if_due(b, d, workdir=os.getcwd())
        evolve_if_due(b, d)
        return
    b = _bundle(args)
    d = Dynamics(args.root)
    if args.event == "post-tool":
        kind = TOOL_KINDS.get(tool or "")
        if kind and fp:
            prefer = None
            if home.is_home(args.root):  # A home bundle maps the file through
                # its project room. The first qualifying activity registers
                # the project, and room preference distinguishes files with
                # the same name. Ambient registration accepts only Git
                # projects, so an unrelated file cannot create a room.
                prefer = home.room_for(args.root, os.path.dirname(
                    os.path.abspath(os.path.expanduser(fp))), create=True,
                    ambient=True)
            observe_event(b, d, kind, fp, session=sid, prefer=prefer)
        return
    # session-start: decay check, mark the session, emit the prime pack
    if home.is_home(args.root):  # a session opening in a new project is
        home.room_for(args.root, os.getcwd(), create=True,  # its room's
                      ambient=True)                          # birth
    d.consolidate_if_due()
    sid = sid or infer_session(d)
    cue = prime_cue(b, d, workdir=os.getcwd())
    d.session_begin(sid, cue=cue)  # the cue is what review learns FROM
    if args.record_only or adapter == "grok":
        return
    where = threads_section(b, d, cue=cue, session=sid)
    if where:  # continuity FIRST: where we left off, then repo facts
        print(where + "\n")
    print(context_pack(b, d, cue, budget=args.budget, primed=True,
                       session=sid))
    lessons = lessons_section(b, d, workdir=os.getcwd(), session=sid)
    if lessons:  # Corrective knowledge appears when its path guard matches.
        print("\n" + lessons)
    w = whisper_section(d)
    if w:  # unconfirmed preferences: lean this way, not yet law
        print("\n" + w)
    inflight = inflight_section(b, d, workdir=os.getcwd())
    if inflight:  # other agents' announced work lands in the session too
        print("\n" + inflight)


def cmd_hook(args):
    """Agent hook adapter that always exits successfully and fails softly."""
    try:
        _hook(args)
    except Exception as e:
        print(f"muninn hook: {e}", file=sys.stderr)


def cmd_consolidate(args):
    d = Dynamics(args.root)
    active, _dormant = d.consolidate()
    faded, recall_only = d.dormant_split()
    baseline = sum(1 for p in _bundle(args).notes if p not in d.entries)
    print(f"consolidated: {active} active, {faded} faded, {recall_only} "
          f"recall-only (never touched): {len(d.entries)} tracked "
          f"({baseline} notes at baseline)")


def cmd_stats(args):
    b = _bundle(args)
    d = Dynamics(args.root)
    faded, recall_only = d.dormant_split()
    active = len(d.entries) - faded - recall_only
    print(f"notes: {len(b.notes)}  tracked: {len(d.entries)} "
          f"({active} active, {faded} faded, {recall_only} recall-only)")
    print("strongest:")
    for path, s in d.strongest(args.k):
        mark = "*" if d.entries.get(path, {}).get("pinned") else " "
        print(f"  {s:5.2f}{mark} {path}")


def cmd_index(args):
    _bundle(args).generate_index()
    print("index.md regenerated")


def _hoist_root(argv: list) -> list:
    """--root is global, but people naturally type it AFTER the subcommand
    (`muninn skill --root ~/kb`). Hoist it to the front so both orders work
   : one function instead of re-registering the flag on 23 subparsers."""
    out, root, i = [], [], 0
    while i < len(argv):
        a = argv[i]
        if a == "--root" and i + 1 < len(argv):
            root = [a, argv[i + 1]]
            i += 2
            continue
        if isinstance(a, str) and a.startswith("--root="):
            root = [a]
            i += 1
            continue
        out.append(a)
        i += 1
    return root + out


def _positive_integer(value: str) -> int:
    """Parse one strictly positive command-line integer."""
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an integer") from error
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def _finite_number(value: str) -> float:
    """Parse a finite command-line number before any memory writes."""
    try:
        parsed = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a number") from error
    if not math.isfinite(parsed):
        raise argparse.ArgumentTypeError("must be finite")
    return parsed


def _nonempty_text(value: str) -> str:
    """Reject empty required note fields without changing their spelling."""
    if not value.strip():
        raise argparse.ArgumentTypeError("must contain non-whitespace text")
    return value


def main(argv=None):
    p = argparse.ArgumentParser(prog="muninn",
                                description=(
                                    "Muninn manages personal knowledge and "
                                    "strengthens recall through use."
                                ))
    p.add_argument("--root", default=os.environ.get("MUNINN_ROOT", "."),
                   help="Set the bundle root. The default is $MUNINN_ROOT, "
                        "or the current directory when it is unset.")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="COMMAND")

    sub.add_parser(
        "demo",
        help="Run a short demonstration in a temporary bundle.",
    ).set_defaults(fn=cmd_demo)

    sub.add_parser(
        "init",
        help="Create a bundle at --root with an index and private sidecar.",
    ).set_defaults(fn=cmd_init)

    a = sub.add_parser(
        "add",
        help="Write a note; use --supersedes to correct an earlier note.",
    )
    a.add_argument("path")
    a.add_argument("--title", required=True, type=_nonempty_text)
    a.add_argument("--type", default="note", type=_nonempty_text)
    a.add_argument("--description", default="")
    a.add_argument("--tags", default="")
    a.add_argument("--supersedes", default="")
    a.add_argument("--body", default="")
    a.set_defaults(fn=cmd_add)

    bd = sub.add_parser(
        "build",
        help=(
            "Build a deterministic syntax graph for a directory and add "
            "Graphify nodes when Graphify is installed."
        ),
    )
    bd.add_argument("folder")
    bd.add_argument("--include-code", action="store_true",
                    help="Also import Graphify code and file nodes as stubs.")
    bd.add_argument("--enrich", action="store_true",
                    help="Add inferred concepts and cross-file edges through "
                         "the endpoint set in MUNINN_ENRICH_URL.")
    bd.add_argument("--max-code-notes", type=int, default=None, metavar="N",
                    help="Set the maximum number of code stubs. The default "
                         "is 500.")
    bd.set_defaults(fn=cmd_build)

    source = sub.add_parser(
        "source",
        help="Index and search source code with persistent hybrid retrieval.",
    )
    source_sub = source.add_subparsers(
        dest="source_command",
        required=True,
        metavar="COMMAND",
    )
    source_index = source_sub.add_parser(
        "index",
        help="Rebuild the private source index atomically.",
    )
    source_index.add_argument("path", nargs="?", default=".")
    source_index.set_defaults(fn=cmd_source_index)

    source_search = source_sub.add_parser(
        "search",
        help="Return ranked source excerpts within a fixed token budget.",
    )
    source_search.add_argument("query")
    source_search.add_argument(
        "--path",
        default=".",
        help="Set the source directory. The default is the current directory.",
    )
    source_search.add_argument("--budget", type=_positive_integer, default=1200)
    source_search.add_argument("--limit", type=_positive_integer, default=20)
    source_search.add_argument(
        "--mode",
        choices=(
            "hybrid",
            "bm25f",
            "lexical-hybrid",
            "graph-fusion",
            "symbol",
            "structural-fusion",
        ),
        default="hybrid",
        help="Select the ranking mode. The default is the validated hybrid.",
    )
    source_search.add_argument(
        "--no-refresh",
        action="store_true",
        help="Use an existing index without checking source freshness.",
    )
    source_search.add_argument(
        "--json",
        action="store_true",
        help="Write a machine-readable result.",
    )
    source_search.set_defaults(fn=cmd_source_search)

    ex = sub.add_parser(
        "extract",
        help=(
            "Extract nodes and typed edges from a file or directory with a "
            "deterministic tree-sitter pass."
        ),
    )
    ex.add_argument("path")
    ex.add_argument("--json", default=None,
                    help="Write the extracted graph in Graphify node-link "
                         "JSON format.")
    ex.add_argument("--into", default=None,
                    help="Import the extracted nodes into the selected bundle.")
    ex.add_argument("--subdir", default="extracted",
                    help="Set the imported-note directory. The default is "
                         "extracted.")
    ex.add_argument("--exclude", action="append", default=None, metavar="GLOB",
                    help="Exclude matching base names or relative paths. The "
                         "option may be repeated.")
    ex.add_argument("--only", default=None, metavar="EXT,EXT",
                    help="Restrict extraction to a comma-separated list of "
                         "file extensions.")
    ex.set_defaults(fn=cmd_extract)

    en = sub.add_parser(
        "enrich",
        help=(
            "Add inferred concepts and cross-file edges to a deterministic "
            "syntax graph. Use --request and --apply, MUNINN_ENRICH_CMD, or "
            "MUNINN_ENRICH_URL."
        ),
    )
    en.add_argument("path")
    en.add_argument("--request", action="store_true",
                    help="Print the enrichment contract, node handles, and "
                         "digest, then exit. This is the first step.")
    en.add_argument("--apply", default=None, metavar="RESPONSE",
                    help="Apply a JSON response from a file or standard input. "
                         "A digest mismatch rejects the response.")
    en.add_argument("--json", default=None,
                    help="Write the combined extracted and inferred graph.")
    en.add_argument("--into", default=None,
                    help="Import the enriched nodes into the selected bundle.")
    en.add_argument("--subdir", default="extracted",
                    help="Set the imported-note directory. The default is "
                         "extracted.")
    en.set_defaults(fn=cmd_enrich)

    im = sub.add_parser(
        "import",
        help="Import a Graphify graph.json file as notes.",
    )
    im.add_argument("graph_json")
    im.add_argument("--subdir", default="imported")
    im.add_argument("--include-code", action="store_true",
                    help="Also import code and file nodes as stub notes.")
    im.add_argument("--source-root", default=None,
                    help="Read source excerpts from the directory used to "
                         "build the graph.")
    im.add_argument("--max-code-notes", type=int, default=None, metavar="N",
                    help="Set the maximum number of code stubs. The default "
                         "is 500.")
    im.set_defaults(fn=cmd_import)

    vz = sub.add_parser(
        "viz",
        help=(
            "Render the fused knowledge graph as an offline HTML file or a "
            "bounded Mermaid diagram."
        ),
    )
    vz.add_argument("--out", default=None,
                    help="Set the HTML output path. The default is "
                         "<root>/.muninn/graph.html.")
    vz.add_argument("--mermaid", action="store_true",
                    help="Print a bounded Mermaid diagram instead of HTML.")
    vz.add_argument("--top", type=int, default=20,
                    help="Set the Mermaid node limit. The default is 20.")
    vz.set_defaults(fn=cmd_viz)

    sk = sub.add_parser(
        "skill",
        help=(
            "Print the Muninn agent protocol and enrichment procedure. For "
            "example: muninn skill > .claude/skills/muninn/SKILL.md."
        ),
    )
    sk.set_defaults(fn=cmd_skill)

    eg = sub.add_parser(
        "export-graph",
        help="Write the fused knowledge graph as node-link JSON.",
    )
    eg.add_argument("out", nargs="?", default=None,
                    help="Set an output file. The default is standard output.")
    eg.set_defaults(fn=cmd_export_graph)

    t = sub.add_parser(
        "touch",
        help="Record that a note was used, which strengthens its recall score.",
    )
    t.add_argument("note")
    t.set_defaults(fn=cmd_touch)

    o = sub.add_parser(
        "outcome",
        help="Record an outcome from -1 to 1 for recently used notes.",
    )
    o.add_argument("valence", type=_finite_number,
                   help="Set outcome valence from -1 to 1. Negative values "
                        "record adverse outcomes.")
    o.add_argument("--note", default=None)
    o.add_argument("--why", default="")
    o.set_defaults(fn=cmd_outcome)

    pi = sub.add_parser(
        "pin",
        help="Set a minimum recall strength for a policy or safety note.",
    )
    pi.add_argument("note")
    pi.add_argument("--unpin", action="store_true")
    pi.set_defaults(fn=cmd_pin)

    su = sub.add_parser(
        "supersede",
        help="Record that NEW corrects OLD so that OLD no longer ranks first.",
    )
    su.add_argument("old")
    su.add_argument("new")
    su.set_defaults(fn=cmd_supersede)

    r = sub.add_parser(
        "recall",
        help="Return ranked notes and the evidence for each selection.",
    )
    r.add_argument("cue")
    r.add_argument("-k", type=int, default=5)
    r.add_argument("--explain", action="store_true",
                   help="Print query facets and graph paths for each result.")
    r.add_argument("--no-reactivate", action="store_true",
                   help="Do not record recall events.")
    r.set_defaults(fn=cmd_recall)

    pk = sub.add_parser(
        "pack",
        help="Build a context pack for a cue within a token budget.",
    )
    pk.add_argument("cue")
    pk.add_argument("--budget", type=int, default=900)
    pk.add_argument("-k", type=int, default=5)
    pk.add_argument("--mode", choices=["muninn", "muninn-walk", "flat", "dump"], default="muninn")
    pk.add_argument("--no-reactivate", action="store_true",
                    help="Do not record recall events.")
    pk.add_argument("--no-index", action="store_true",
                    help="Omit the compact index and allocate the full budget "
                         "to focused notes.")
    pk.add_argument("--compact", action="store_true",
                    help="Return exact query-focused body excerpts without the index.")
    pk.set_defaults(fn=cmd_pack)

    vo = sub.add_parser(
        "volunteer",
        help="Return passive context only for an exact entity identity.",
    )
    vo.add_argument("cue")
    vo.add_argument("--budget", type=int, default=400)
    vo.add_argument("-k", type=int, choices=(1, 2, 3), default=1)
    vo.add_argument("--session", default=None,
                    help="Suppress pages already served in this session.")
    vo.add_argument("--no-reactivate", action="store_true",
                    help="Do not record recall events.")
    vo.set_defaults(fn=cmd_volunteer)

    g = sub.add_parser(
        "goal",
        help="Declare, list, or retire an active goal.",
    )
    g.add_argument("text", nargs="?", default="")
    g.add_argument("--weight", type=_finite_number, default=0.7)
    g.add_argument("--off", action="store_true",
                   help="Retire the named goal. Without text, retire the only "
                        "active goal or list multiple active goals.")
    g.set_defaults(fn=cmd_goal)

    it = sub.add_parser(
        "intent",
        help=(
            "Announce or list work in progress on a branch. An intent informs "
            "other agents and does not lock files."
        ),
    )
    it.add_argument("goal_text", nargs="?", default="",
                    help="Describe the work performed on this branch.")
    it.add_argument("--branch", default=None,
                    help="Set the associated branch. The default is the "
                         "current Git branch.")
    it.add_argument("--paths", default="",
                    help="List affected paths as comma-separated values. "
                         "Other agents receive overlap warnings.")
    it.add_argument("--ttl", default=None,
                    help="Set the expiry in seconds or as 90m, 4h, or 2d. The "
                         "default is 24h.")
    it.add_argument("--done", action="store_true",
                    help="Retire the intent after completion or abandonment.")
    it.add_argument("--cwd", default=None,
                    help="Set the directory used to detect the Git branch.")
    it.set_defaults(fn=cmd_intent)

    sy = sub.add_parser(
        "sync",
        help=(
            "Merge the local ledger with the ledger stored at "
            "refs/muninn/ledger."
        ),
    )
    sy.add_argument("--remote", default="origin")
    sy.add_argument("--ref", default=SYNC_REF)
    sy.add_argument("--pull-only", action="store_true",
                    help="Fetch and merge the remote ledger without publishing.")
    sy.add_argument("--repo", default=None,
                    help="Set the Git repository that carries the ledger. The "
                         "default is the bundle root.")
    sy.set_defaults(fn=cmd_sync)

    ins = sub.add_parser(
        "install",
        help=(
            "Configure AGENTS.md as the canonical instruction file in a "
            "project directory."
        ),
    )
    ins.add_argument("dir", nargs="?", default=".",
                     help="Set the project directory. The default is the "
                          "current directory.")
    ins.add_argument("--global", dest="global_", action="store_true",
                     help="Connect supported home-level instruction files to "
                          "one canonical personal file.")
    ins.add_argument("--from", dest="source", default=None,
                     help="Set the canonical personal file. The default is "
                          "~/AGENTS.md.")
    ins.add_argument("--copy", action="store_true",
                     help="Manage an instruction block inside supported files "
                          "instead of creating symbolic links.")
    ins.add_argument("--home", dest="home_", action="store_true",
                     help="Initialize the home bundle and configure global "
                          "instructions. Print hook configuration; use setup to install hooks.")
    ins.add_argument("--brain", default=None,
                     help="Set the home bundle path. The default is "
                          "$MUNINN_HOME or ~/muninn.")
    ins.set_defaults(fn=cmd_install)

    se = sub.add_parser(
        "setup",
        help=(
            "Configure a home bundle, global instructions, supported agent "
            "hooks, and the Muninn skill without replacing configuration."
        ),
    )
    se.add_argument("--brain", default=None,
                    help="Set the home bundle path. The default is "
                         "$MUNINN_HOME or ~/muninn.")
    se.add_argument("--from", dest="source", default=None,
                    help="Set the canonical personal file. The default is "
                         "~/AGENTS.md.")
    se.add_argument("--dry-run", dest="dry_run", action="store_true",
                    help="Print the affected paths without changing them.")
    se.set_defaults(fn=cmd_setup, home_=True, global_=False, copy=False,
                    dir=".")

    un = sub.add_parser(
        "uninstall",
        help="Remove setup-managed files and preserve all knowledge files.",
    )
    un.add_argument("--brain", default=None,
                    help="Set the home bundle path. The default is "
                         "$MUNINN_HOME or ~/muninn.")
    un.add_argument("--from", dest="source", default=None,
                    help="Set the canonical personal file. The default is "
                         "~/AGENTS.md.")
    un.set_defaults(fn=cmd_uninstall)

    ad = sub.add_parser(
        "adopt",
        help="Register a project in the home bundle and update its AGENTS.md.",
    )
    ad.add_argument("dir", nargs="?", default=".",
                    help="Set the project directory. The default is the "
                         "current directory.")
    ad.add_argument("--brain", default=None,
                    help="Set the home bundle path. The default is "
                         "$MUNINN_HOME or ~/muninn.")
    ad.set_defaults(fn=cmd_adopt)

    sty = sub.add_parser(
        "style",
        help="Adopt, refresh, or inspect a manifest-based style repository.",
    )
    sty.add_argument("action", nargs="?", default="",
                     help="Select adopt, refresh, or status. The default is "
                          "status.")
    sty.add_argument("repo", nargs="?", default=None,
                     help="Set the style repository for adoption or refresh.")
    sty.add_argument("--brain", default=None,
                     help="Set the home bundle path. The default is "
                          "$MUNINN_HOME or ~/muninn.")
    sty.add_argument("--from", dest="source", default=None,
                     help="Set the canonical file that receives the style "
                          "core and routes. The default is ~/AGENTS.md.")
    sty.add_argument("--pull", action="store_true",
                     help="Fast-forward the adopted Git repository before "
                          "updating instructions.")
    sty.set_defaults(fn=cmd_style)

    dr = sub.add_parser(
        "doctor",
        help="Verify instruction wiring and return exit status 1 on drift.",
    )
    dr.add_argument("dir", nargs="?", default=".",
                    help="Set the project directory. The default is the "
                         "current directory.")
    dr.add_argument("--global", dest="global_", action="store_true",
                    help="Verify the supported home-level instruction files.")
    dr.add_argument("--home", dest="home_", action="store_true",
                    help="Verify the home bundle, canonical pointer, and "
                         "project registry.")
    dr.add_argument("--brain", default=None,
                    help="Set the home bundle path. The default is "
                         "$MUNINN_HOME or ~/muninn.")
    dr.add_argument("--from", dest="source", default=None,
                    help="Set the canonical personal file. The default is "
                         "~/AGENTS.md.")
    dr.set_defaults(fn=cmd_doctor)

    fb = sub.add_parser(
        "feedback",
        help=(
            "Record one preference observation. Repeated evidence is required "
            "before a rule is promoted."
        ),
    )
    fb.add_argument("text", help="Describe the observed preference.")
    fb.add_argument("--domain", default="general",
                    help="Set the writing domain. The default is general.")
    fb.add_argument("--positive", action="store_true",
                    help="Record approval instead of a correction.")
    fb.add_argument("--session", default=None,
                    help="Set the caller's session identifier. Otherwise use MUNINN_SESSION.")
    fb.set_defaults(fn=cmd_feedback)

    ev = sub.add_parser(
        "evolve",
        help="Evaluate repeated feedback and report the resulting style rules.",
    )
    ev.set_defaults(fn=cmd_evolve)

    jn = sub.add_parser(
        "journal",
        help=(
            "Record a session in a thread, or list threads when no thread is "
            "provided."
        ),
    )
    jn.add_argument("thread", nargs="?", default="",
                    help="Set the thread topic.")
    jn.add_argument("--body", default="",
                    help="Record this session's decisions, reasons, state, "
                         "next steps, and open questions.")
    jn.add_argument("--state", default="",
                    help="Replace the thread's compiled current state.")
    jn.add_argument("--show", action="store_true",
                    help="Print the current state and recent episodes.")
    jn.set_defaults(fn=cmd_journal)

    itr = sub.add_parser(
        "import-transcripts",
        help=(
            "Convert an explicitly selected Claude JSONL archive into "
            "secret-scrubbed episode notes."
        ),
    )
    itr.add_argument("path", help="Set a JSONL file or directory to import.")
    itr.add_argument("--thread", default="",
                     help="Set the destination thread. The default is the "
                          "source directory name.")
    itr.add_argument("--max-sessions", type=int, default=50)
    itr.set_defaults(fn=cmd_import_transcripts)

    ls = sub.add_parser(
        "lesson",
        help=(
            "Record a guard note from a failure or correction, or list guard "
            "notes when no title is provided."
        ),
    )
    ls.add_argument("title", nargs="?", default="",
                    help="State the failure or correction in one line.")
    ls.add_argument("--guards", default="",
                    help="Set comma-separated paths that trigger this guard. "
                         "An empty value applies to every session.")
    ls.add_argument("--body", default="",
                    help="State how to prevent recurrence, or read it from "
                         "standard input.")
    ls.add_argument("--valence", type=_finite_number, default=-0.6,
                    help="Set the associated outcome valence. The default is "
                         "-0.6.")
    ls.set_defaults(fn=cmd_lesson)

    rv = sub.add_parser(
        "review",
        help=(
            "Report how served context compared with context used during "
            "recent sessions."
        ),
    )
    rv.add_argument("--session", default=None,
                    help="Review one session identifier.")
    rv.add_argument("-k", type=int, default=10,
                    help="Set the number of recent sessions. The default is 10.")
    rv.set_defaults(fn=cmd_review)

    pr = sub.add_parser(
        "prime",
        help="Build a context pack from the current working state.",
    )
    pr.add_argument("--cwd", default=None)
    pr.add_argument("--budget", type=int, default=900)
    pr.add_argument("-k", type=int, default=5)
    pr.add_argument("--session", default=None,
                    help="Associate recall events with a session identifier.")
    pr.add_argument("--no-index", action="store_true",
                    help="Omit the compact index and allocate the full budget "
                         "to focused notes.")
    pr.add_argument("--compact", action="store_true",
                    help="Return exact query-focused body excerpts without the index.")
    pr.set_defaults(fn=cmd_prime)

    ob = sub.add_parser(
        "observe",
        help=(
            "Record one ambient event from flags or JSON on standard input. "
            "Observation errors do not interrupt the calling process."
        ),
    )
    ob.add_argument("--kind", default=None,
                    help="Set the event kind to touch, encode, or outcome.")
    ob.add_argument("--note", default=None,
                    help="Set a bundle note or a source path that maps to a "
                         "bundle note.")
    ob.add_argument("--valence", type=float, default=0.0)
    ob.add_argument("--session", default=None,
                    help="Set the session identifier. The default is a "
                         "30-minute sliding window.")
    ob.add_argument("--json", action="store_true",
                    help="Read the event as JSON from standard input.")
    ob.set_defaults(fn=cmd_observe)

    hk = sub.add_parser(
        "hook",
        help=(
            "Process an agent hook event from JSON on standard input. "
            "The command always exits successfully."
        ),
    )
    hk.add_argument("event", choices=["session-start", "post-tool",
                                      "session-end", "print-config"])
    hk.add_argument("--budget", type=int, default=900,
                    help="Set the context budget for session-start.")
    hk.add_argument("--adapter", choices=["claude", "codex", "grok"],
                    default="claude",
                    help="Select the format emitted by print-config.")
    hk.add_argument("--record-only", action="store_true",
                    help="Record session-start without emitting a context pack.")
    hk.set_defaults(fn=cmd_hook)

    sub.add_parser(
        "consolidate",
        help="Apply one decay step to unused notes.",
    ).set_defaults(fn=cmd_consolidate)

    st = sub.add_parser(
        "stats",
        help="Report tracked notes and the highest recall strengths.",
    )
    st.add_argument("-k", type=int, default=10)
    st.set_defaults(fn=cmd_stats)

    sub.add_parser(
        "index",
        help="Regenerate the OKF index.md file.",
    ).set_defaults(fn=cmd_index)

    args = p.parse_args(_hoist_root(
        list(argv) if argv is not None else sys.argv[1:]))
    args.fn(args)


if __name__ == "__main__":
    main()
