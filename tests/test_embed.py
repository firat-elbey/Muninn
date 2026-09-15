"""Verify the optional semantic seed channel and its privacy limits.

A loopback server returns deterministic vectors in the embeddings response
format. The tests reject unapproved nonlocal endpoints, verify cache
invalidation, preserve lexical behavior after transport failure, admit a
semantic match without shared terms, and retain a strong exact lexical result
above a semantic-only candidate.
"""

import contextlib
import hashlib
import http.server
import io
import json
import os
import re
import shutil
import socket
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from muninn import embed  # noqa: E402
from muninn.activate import Cue, activate  # noqa: E402
from muninn.dynamics import Dynamics  # noqa: E402
from muninn.recall import recall_explain  # noqa: E402
from muninn.store import Bundle  # noqa: E402

# -- deterministic mock embeddings ------------------------------------------
# Concept keywords map a text onto a one-hot concept axis, so texts that share
# a MEANING (not words) are cosine-1 neighbors; everything else falls back to a
# stable sha256 hash vector (so cache/sha tests see content-dependent vectors).
_CONCEPTS = ("aviation", "finance", "cooking")
_KEYWORDS = {
    "airplane": "aviation", "aircraft": "aviation", "servicing": "aviation",
    "maintenance": "aviation", "aviation": "aviation", "flight": "aviation",
    "quarterly": "finance", "revenue": "finance", "report": "finance",
    "earnings": "finance", "fiscal": "finance", "profit": "finance",
    "menu": "cooking", "recipe": "cooking", "pasta": "cooking",
}
_HASH_DIMS = 8
_DIM = len(_CONCEPTS) + _HASH_DIMS


def _mock_vector(text):
    v = [0.0] * _DIM
    toks = re.findall(r"[a-z]+", text.lower())
    concept = False
    for t in toks:
        c = _KEYWORDS.get(t)
        if c:
            v[_CONCEPTS.index(c)] += 1.0
            concept = True
    if not concept:
        for t in toks:
            h = int(hashlib.sha256(t.encode()).hexdigest(), 16)
            v[len(_CONCEPTS) + (h % _HASH_DIMS)] += 1.0
    if not any(v):
        v[len(_CONCEPTS)] = 1.0  # never all-zero (cosine is defined)
    return v


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):  # keep the test output clean
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        try:
            doc = json.loads(self.rfile.read(n) or b"{}")
            inp = doc.get("input", [])
            if isinstance(inp, str):
                inp = [inp]
        except ValueError:
            inp = []
        self.server.requests.append(list(inp))
        self.close_connection = True  # one request per socket: no keep-alive
        # optional flaky mode: once ``fail_after`` POSTs have been answered,
        # every further POST returns HTTP 500: lets a test fail a specific
        # batch partway through a multi-batch embed (None = always healthy).
        fail_after = getattr(self.server, "fail_after", None)
        if fail_after is not None and len(self.server.requests) > fail_after:
            self.send_response(500)
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()
            return
        data = [{"object": "embedding", "index": i, "embedding": _mock_vector(t)}
                for i, t in enumerate(inp)]
        body = json.dumps({"object": "list", "model": "mock",
                           "data": data}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self):
        # HTTPServer.server_bind() calls socket.getfqdn(host), a reverse-DNS
        # lookup that can block for tens of seconds on some networks. We only
        # ever bind loopback, so skip it (a pure test-side speedup).
        import socketserver
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "127.0.0.1", self.server_address[1]


def _start_server():
    srv = _Server(("127.0.0.1", 0), _Handler)
    srv.requests = []
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}/v1/embeddings"


def _serve(tc):
    """Start a mock endpoint and register full teardown (stop + close the
    listening socket) so no thread or socket lingers into interpreter exit."""
    srv, url = _start_server()
    tc.addCleanup(srv.server_close)
    tc.addCleanup(srv.shutdown)
    return srv, url


def _serve_redirect(tc, location):
    """A loopback endpoint that answers every POST with a 302 to ``location``
   : to prove the client refuses to follow a redirect off the pinned host."""
    class _R(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self.close_connection = True
            self.send_response(302)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()

    srv = _Server(("127.0.0.1", 0), _R)
    srv.requests = []
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    tc.addCleanup(srv.server_close)
    tc.addCleanup(srv.shutdown)
    return f"http://127.0.0.1:{srv.server_address[1]}/v1/embeddings"


def _serve_flaky(tc, fail_after):
    """Like ``_serve`` but the endpoint answers ``fail_after`` POSTs normally
    then returns HTTP 500 for every one after: a live-but-flaky server that
    fails partway through a multi-batch embed. Set ``srv.fail_after = None`` to
    make it healthy again (the endpoint recovering on a later retry)."""
    srv, url = _start_server()
    srv.fail_after = fail_after
    tc.addCleanup(srv.server_close)
    tc.addCleanup(srv.shutdown)
    return srv, url


def _dead_url():
    """A loopback URL whose port is (almost certainly) closed."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}/v1/embeddings"


class _EnvGuard(unittest.TestCase):
    """Every test starts with the embedding env cleared and restores it."""

    def setUp(self):
        self._saved = {k: os.environ.get(k)
                       for k in ("MUNINN_EMBED_URL", "MUNINN_EMBED_MODEL",
                                 "MUNINN_EMBED_ALLOW_REMOTE",
                                 "MUNINN_EMBED_QUERY_PREFIX",
                                 "MUNINN_EMBED_DOC_PREFIX")}
        for k in self._saved:
            os.environ.pop(k, None)
        embed._WARNED_REMOTE.clear()  # reset the once-per-host warning state
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _scores(self, cue, weight=1.0):
        b, d = Bundle(self.root), Dynamics(self.root)
        return activate(b, d, [Cue(cue, weight, "query")]).scores


# -- (a) privacy: non-local URLs refused BY DEFAULT, allowed on opt-in -------

class TestConfigGuard(_EnvGuard):
    def test_non_local_url_refused_by_default(self):
        b = Bundle(self.root)
        b.write_note("a.md", {"title": "A"}, "hello world")
        b = Bundle(self.root)
        for bad in ("https://api.openai.com/v1/embeddings",
                    "http://10.0.0.90:1234/v1/embeddings",      # non-local IP
                    "http://127.0.0.1.evil.com/v1/embeddings",  # not loopback
                    "http://127.0.0.1@evil.com/v1/embeddings",  # userinfo trick
                    "http://0.0.0.0:1234/v1/embeddings",
                    "file://127.0.0.1/etc/passwd"):             # non-http scheme
            os.environ["MUNINN_EMBED_URL"] = bad
            # The raise happens at config resolution, BEFORE any socket opens :
            # no content egresses even to fail. PrivacyError is NOT a member of
            # _TRANSPORT, so the fail-soft handling can never swallow it.
            self.assertNotIsInstance(embed.PrivacyError(), embed._TRANSPORT)
            with self.assertRaises(embed.PrivacyError):
                embed.available(b)
            with self.assertRaises(embed.PrivacyError):
                embed.embed_notes(b)
            with self.assertRaises(embed.PrivacyError):
                embed.embed_text("x")

    def test_non_local_url_allowed_when_opted_in(self):
        # With MUNINN_EMBED_ALLOW_REMOTE set, a non-local host is accepted and
        # a single stderr line names it. available() resolves config WITHOUT
        # opening a socket, so this asserts the policy with zero egress.
        b = Bundle(self.root)
        os.environ["MUNINN_EMBED_ALLOW_REMOTE"] = "1"
        for url, host in (("https://api.openai.com/v1/embeddings",
                           "api.openai.com"),
                          ("http://10.0.0.90:1234/v1/embeddings", "10.0.0.90")):
            os.environ["MUNINN_EMBED_URL"] = url
            embed._WARNED_REMOTE.clear()
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertTrue(embed.available(b))     # no PrivacyError
            out = err.getvalue()
            self.assertIn(f"remote embedding host {host}", out)
            self.assertIn("MUNINN_EMBED_ALLOW_REMOTE set", out)
            self.assertEqual(out.count("remote embedding host"), 1)  # once only

    def test_opt_in_flag_accepts_1_true_yes_case_insensitive(self):
        b = Bundle(self.root)
        os.environ["MUNINN_EMBED_URL"] = "https://api.openai.com/v1/embeddings"
        for yes in ("1", "true", "TRUE", "Yes", "yes"):
            os.environ["MUNINN_EMBED_ALLOW_REMOTE"] = yes
            embed._WARNED_REMOTE.clear()
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertTrue(embed.available(b))     # opted in -> allowed
        for no in ("0", "false", "no", "", "off"):
            os.environ["MUNINN_EMBED_ALLOW_REMOTE"] = no
            with self.assertRaises(embed.PrivacyError):  # not opted in -> refused
                embed.available(b)

    def test_local_urls_accepted(self):
        b = Bundle(self.root)
        for ok in ("http://127.0.0.1:8181/v1/embeddings",
                   "http://localhost:8181/v1/embeddings",
                   "http://[::1]:8181/v1/embeddings"):
            os.environ["MUNINN_EMBED_URL"] = ok
            self.assertTrue(embed.available(b))  # configured, no raise


# -- prefixes: query/doc task prefixes are applied; empty by default --------

class TestPrefixes(_EnvGuard):
    def setUp(self):
        super().setUp()
        b = Bundle(self.root)
        b.write_note("n.md", {"title": "Note"}, "some body text here")
        self.srv, self.url = _serve(self)
        os.environ["MUNINN_EMBED_URL"] = self.url

    def test_query_and_doc_prefixes_are_prepended(self):
        os.environ["MUNINN_EMBED_QUERY_PREFIX"] = "search_query: "
        os.environ["MUNINN_EMBED_DOC_PREFIX"] = "search_document: "
        self.assertIsNotNone(embed.embed_text("a cue"))     # cue -> query prefix
        embed.embed_notes(Bundle(self.root))                # note -> doc prefix
        sent = [t for req in self.srv.requests for t in req]
        self.assertIn("search_query: a cue", sent)          # exact cue payload
        self.assertTrue(any(t.startswith("search_document: ") and
                            "some body text here" in t for t in sent))

    def test_no_prefix_by_default_sends_raw_text(self):
        self.assertIsNotNone(embed.embed_text("a cue"))
        sent = [t for req in self.srv.requests for t in req]
        self.assertIn("a cue", sent)                        # verbatim, no prefix
        self.assertFalse(any(t.startswith("search_") for t in sent))

    def test_empty_prefix_is_byte_identical_to_no_prefix(self):
        v_none = embed.embed_text("redis eviction policy tuning")
        self.srv.requests.clear()
        os.environ["MUNINN_EMBED_QUERY_PREFIX"] = ""        # explicitly empty
        os.environ["MUNINN_EMBED_DOC_PREFIX"] = ""
        v_empty = embed.embed_text("redis eviction policy tuning")
        self.assertEqual(v_none, v_empty)                   # identical vector
        self.assertEqual(self.srv.requests[-1],             # identical payload
                         ["redis eviction policy tuning"])


# -- (b) the sidecar cache round-trips and invalidates on change ------------

class TestCache(_EnvGuard):
    def setUp(self):
        super().setUp()
        b = Bundle(self.root)
        b.write_note("n1.md", {"title": "Note one"}, "banana quantum widget")
        b.write_note("n2.md", {"title": "Note two"}, "sailboat harbor lantern")
        self.srv, url = _serve(self)
        os.environ["MUNINN_EMBED_URL"] = url

    def test_round_trip_and_sha_invalidation(self):
        b = Bundle(self.root)
        v1 = embed.embed_notes(b)
        self.assertEqual(set(v1), {"n1.md", "n2.md"})
        cache = os.path.join(self.root, ".muninn", "embeddings.json")
        self.assertTrue(os.path.exists(cache))
        self.assertEqual(len(self.srv.requests), 1)          # one batched call
        self.assertEqual(sorted(self.srv.requests[0]), sorted(self.srv.requests[0]))
        self.assertEqual(len(self.srv.requests[0]), 2)       # both notes embedded

        self.srv.requests.clear()  # second call: fully served from cache
        v2 = embed.embed_notes(Bundle(self.root))
        self.assertEqual(v1, v2)
        self.assertEqual(self.srv.requests, [])              # zero new calls

        # change one note's body: only it re-embeds, its vector changes
        b.write_note("n1.md", {"title": "Note one"}, "banana quantum GIRAFFE")
        self.srv.requests.clear()
        v3 = embed.embed_notes(Bundle(self.root))
        self.assertEqual(len(self.srv.requests), 1)
        self.assertEqual(len(self.srv.requests[0]), 1)       # just the changed one
        self.assertIn("GIRAFFE", self.srv.requests[0][0])
        self.assertNotEqual(v3["n1.md"], v1["n1.md"])        # sha invalidated
        self.assertEqual(v3["n2.md"], v1["n2.md"])           # untouched reused

        with open(cache, encoding="utf-8") as fh:
            on_disk = json.load(fh)
        self.assertEqual(set(on_disk["notes"]), {"n1.md", "n2.md"})
        self.assertEqual(on_disk["notes"]["n1.md"]["vec"], v3["n1.md"])


# -- (b') the cache PERSISTS partial progress and is never discarded/truncated
#         by a failure: the real-eval regression (multi-batch) --------------

class TestCachePersistence(_EnvGuard):
    """The real eval embedded ~3.8k texts against a live-but-flaky endpoint yet
    wrote ZERO ``embeddings.json``: a single transient HTTP 500 anywhere in a
    multi-batch run made ``embed_notes`` discard EVERY vector it had already
    computed and skip the cache write, so each retry re-embedded from scratch
    and re-failed identically. These lock the fix: batches that landed are
    persisted, and a total failure still never creates or truncates a cache.
    ``_BATCH`` is shrunk to 2 so 5 notes span 3 batches and a mid-run failure
    is real rather than hypothetical."""

    _WORDS = ("banana", "sailboat", "quantum", "harbor", "lantern")

    def setUp(self):
        super().setUp()
        self._batch = embed._BATCH
        embed._BATCH = 2  # 5 notes -> 3 batches [2, 2, 1]
        self.addCleanup(setattr, embed, "_BATCH", self._batch)
        b = Bundle(self.root)
        for i, w in enumerate(self._WORDS):
            b.write_note(f"n{i}.md", {"title": f"Note {i}"}, w)
        self.cache = os.path.join(self.root, ".muninn", "embeddings.json")

    def _entries(self):
        with open(self.cache, encoding="utf-8") as fh:
            return json.load(fh)["notes"]

    def _text(self):
        with open(self.cache, encoding="utf-8") as fh:
            return fh.read()

    def test_first_call_batches_then_reuse_makes_zero_calls(self):
        # (a) COUNT: N=5 notes embed across ceil(5/2)=3 batched POSTs; an
        # identical second call makes ZERO endpoint calls: all cache hits.
        srv, url = _serve(self)
        os.environ["MUNINN_EMBED_URL"] = url
        v1 = embed.embed_notes(Bundle(self.root))
        self.assertEqual(len(v1), 5)
        self.assertEqual(len(srv.requests), 3)                  # 3 batched POSTs
        self.assertEqual(sum(len(r) for r in srv.requests), 5)  # 5 texts total
        self.assertEqual(len(self._entries()), 5)               # all cached
        srv.requests.clear()
        v2 = embed.embed_notes(Bundle(self.root))
        self.assertEqual(v1, v2)
        self.assertEqual(srv.requests, [])                      # ZERO on reuse

    def test_changing_one_note_reembeds_only_it(self):
        # (b) with a full cache, editing ONE note's body re-embeds exactly one.
        srv, url = _serve(self)
        os.environ["MUNINN_EMBED_URL"] = url
        embed.embed_notes(Bundle(self.root))
        b = Bundle(self.root)
        b.write_note("n2.md", {"title": "Note 2"}, "quantum GIRAFFE changed")
        srv.requests.clear()
        embed.embed_notes(Bundle(self.root))
        self.assertEqual(len(srv.requests), 1)                  # one batch
        self.assertEqual(sum(len(r) for r in srv.requests), 1)  # one note only
        self.assertIn("GIRAFFE", srv.requests[0][0])
        self.assertEqual(len(self._entries()), 5)               # still all five

    def test_partial_failure_persists_landed_batches(self):
        # THE fix: a 500 on the 2nd batch returns {} (fail-soft) but the first
        # batch's vectors are cached; when the endpoint recovers, only the
        # remaining notes are embedded: the cache fills MONOTONICALLY across
        # retries rather than re-embedding everything from scratch every time.
        srv, url = _serve_flaky(self, fail_after=1)  # batch 0 ok, batch 1 -> 500
        os.environ["MUNINN_EMBED_URL"] = url
        self.assertEqual(embed.embed_notes(Bundle(self.root)), {})  # fail-soft
        self.assertTrue(os.path.exists(self.cache))             # partial SURVIVES
        self.assertEqual(len(self._entries()), 2)               # exactly batch 0
        self.assertEqual(len(srv.requests), 2)                  # 2nd POST 500'd

        srv.fail_after = None                                   # endpoint recovers
        srv.requests.clear()
        v = embed.embed_notes(Bundle(self.root))
        self.assertEqual(len(v), 5)                             # fully served now
        self.assertEqual(sum(len(r) for r in srv.requests), 3)  # only the 3 left
        self.assertEqual(len(self._entries()), 5)

    def test_dead_endpoint_does_not_truncate_existing_cache(self):
        # (c) a pre-existing GOOD cache must survive a later dead endpoint: not
        # truncated, not erased. Build the cache, change a note so there is real
        # todo work, then point at a dead port.
        srv, url = _serve(self)
        os.environ["MUNINN_EMBED_URL"] = url
        embed.embed_notes(Bundle(self.root))
        before = self._text()
        self.assertEqual(len(json.loads(before)["notes"]), 5)

        b = Bundle(self.root)
        b.write_note("n0.md", {"title": "Note 0"}, "CHANGED body zero")
        os.environ["MUNINN_EMBED_URL"] = _dead_url()           # endpoint now dead
        self.assertEqual(embed.embed_notes(Bundle(self.root)), {})  # fail-soft
        self.assertEqual(self._text(), before)                 # byte-identical
        self.assertEqual(len(self._entries()), 5)              # intact, not cut

    def test_write_failure_leaves_prior_cache_intact(self):
        # (d) atomic write: the cache is committed by an os.replace of a tmp
        # file, so a failure at the swap leaves the prior good cache intact and
        # never a partial/corrupt embeddings.json: readers only see it whole.
        srv, url = _serve(self)
        os.environ["MUNINN_EMBED_URL"] = url
        embed.embed_notes(Bundle(self.root))
        before = self._text()

        b = Bundle(self.root)
        b.write_note("n1.md", {"title": "Note 1"}, "sailboat REVISED")  # 1 todo

        def _boom(*a, **k):
            raise OSError("simulated replace failure")

        real_replace = embed.os.replace
        embed.os.replace = _boom
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                embed.embed_notes(Bundle(self.root))           # must not raise
        finally:
            embed.os.replace = real_replace
        after = self._text()
        self.assertEqual(after, before)                        # never half-written
        self.assertEqual(len(json.loads(after)["notes"]), 5)   # valid JSON, intact


# -- (c) a dead / absent endpoint fails soft to pure lexical ----------------

class TestFailSoft(_EnvGuard):
    def setUp(self):
        super().setUp()
        b = Bundle(self.root)
        b.write_note("hero.md", {"title": "Redis eviction policy"},
                     "maxmemory eviction lru tuning")
        b.write_note("side.md", {"title": "Redis persistence"},
                     "rdb snapshots and aof")

    def test_dead_endpoint_identical_to_pure_lexical(self):
        base = self._scores("redis eviction")          # no endpoint at all
        os.environ["MUNINN_EMBED_URL"] = _dead_url()    # configured but dead
        b = Bundle(self.root)
        self.assertEqual(embed.embed_notes(b), {})      # fail-soft -> {}
        self.assertEqual(self._scores("redis eviction"), base)  # byte-identical
        cache = os.path.join(self.root, ".muninn", "embeddings.json")
        self.assertFalse(os.path.exists(cache))         # nothing written on fail

    def test_cache_present_but_no_endpoint_is_lexical(self):
        base = self._scores("redis eviction")
        srv, url = _serve(self)                         # populate the cache
        os.environ["MUNINN_EMBED_URL"] = url
        embed.embed_notes(Bundle(self.root))
        cache = os.path.join(self.root, ".muninn", "embeddings.json")
        self.assertTrue(os.path.exists(cache))
        os.environ.pop("MUNINN_EMBED_URL", None)        # cache stays, endpoint gone
        # available() is True (cache present) but no live endpoint can embed
        # the cue, so there is no semantic contribution -> still pure lexical
        self.assertTrue(embed.available(Bundle(self.root)))
        self.assertEqual(self._scores("redis eviction"), base)


# -- (d) a semantic hit surfaces where lexical alone cannot -----------------

class TestSemanticHit(_EnvGuard):
    def setUp(self):
        super().setUp()
        b = Bundle(self.root)
        # strong lexical anchor (all cue words) + a non-aviation lexical
        # distractor + the semantic-only target (no shared words with the cue)
        b.write_note("anchor.md", {"title": "Airplane servicing options desk"},
                     "front desk for airplane servicing options")
        b.write_note("menu.md", {"title": "Lunch menu"},
                     "menu options for the week")            # matches 'options'
        b.write_note("aircraft.md", {"title": "Aircraft maintenance log"},
                     "aircraft maintenance schedule and repair")  # semantic-only
        self.srv, self.url = _serve(self)

    def test_semantic_target_enters_topk(self):
        cue = "airplane servicing options"
        b, d = Bundle(self.root), Dynamics(self.root)
        # lexical alone: the semantic target does NOT match and is not returned
        lexical = [n.path for n, _s, _w, _e in
                   recall_explain(b, d, cue, k=2, reactivate=False)]
        self.assertNotIn("aircraft.md", lexical)
        self.assertIn("anchor.md", lexical)

        os.environ["MUNINN_EMBED_URL"] = self.url
        hits = recall_explain(Bundle(self.root), Dynamics(self.root), cue,
                              k=2, reactivate=False)
        paths = [n.path for n, _s, _w, _e in hits]
        self.assertEqual(paths[0], "anchor.md")             # lexical still leads
        self.assertIn("aircraft.md", paths)                 # semantic surfaced it
        ev = {n.path: e for n, _s, _w, e in hits}
        self.assertIn("semantic", ev["aircraft.md"].facets)


# -- (e) boundedness: a strong lexical hit outranks a semantic neighbor -----

class TestBoundedness(_EnvGuard):
    def setUp(self):
        super().setUp()
        b = Bundle(self.root)
        b.write_note("hero.md", {"title": "Quarterly revenue report"},
                     "the quarterly revenue report for the board")  # exact match
        b.write_note("neighbor.md", {"title": "Fiscal earnings summary"},
                     "fiscal earnings and profit summary")   # semantic-only
        self.srv, url = _serve(self)
        os.environ["MUNINN_EMBED_URL"] = url

    def test_lexical_hit_outranks_pure_semantic(self):
        hits = recall_explain(Bundle(self.root), Dynamics(self.root),
                              "quarterly revenue report", k=5, reactivate=False)
        scores = {n.path: s for n, s, _w, _e in hits}
        ev = {n.path: e for n, _s, _w, e in hits}
        self.assertIn("neighbor.md", scores)                 # it did surface...
        self.assertIn("semantic", ev["neighbor.md"].facets)  # ...via embeddings
        self.assertEqual(hits[0][0].path, "hero.md")         # lexical on top
        self.assertGreater(scores["hero.md"], scores["neighbor.md"])  # never buried


# -- byte-identical guarantee: the channel is a pure no-op when off ---------

class TestByteIdentical(_EnvGuard):
    def test_embed_block_is_gated_by_available(self):
        """available() is the ONLY gate: with it False the block is skipped,
        producing the same result as the natural no-URL/no-cache path."""
        b = Bundle(self.root)
        b.write_note("a.md", {"title": "Alpha"}, "alpha links [[Beta]]")
        b.write_note("b.md", {"title": "Beta"}, "beta content here")
        b.write_note("c.md", {"title": "Gamma"}, "unrelated gamma text")
        b = Bundle(self.root)
        d = Dynamics(self.root)
        d.touch("a.md", session="s1")
        d.touch("b.md", session="s1")
        cues = [Cue("alpha beta", 1.0, "query")]
        natural = activate(Bundle(self.root), Dynamics(self.root), cues)
        saved = embed.available
        embed.available = lambda _b: False
        try:
            forced = activate(Bundle(self.root), Dynamics(self.root), cues)
        finally:
            embed.available = saved
        self.assertEqual(natural.scores, forced.scores)
        self.assertEqual({p: e.facets for p, e in natural.evidence.items()},
                         {p: e.facets for p, e in forced.evidence.items()})

    def test_all_lexical_hits_superseded_drops_walk_neighbor(self):
        """Regression: when EVERY lexical hit is superseded (top_lexical == 0),
        a walk-only neighbor must NOT surface with the embed channel off :
        pre-embed recall dropped it, and the pure-lexical path must still drop
        it. (A too-broad orphan branch scored it; this locks the fix.)"""
        b = Bundle(self.root)
        # old.md is the ONLY note that matches "7433"; it is superseded, and it
        # links onward to a neighbor that shares no words with the cue.
        b.write_note("old.md", {"title": "Postgres port"},
                     "postgres listens on 7433 in prod; see [[Neighbor]]")
        b.write_note("new.md", {"title": "Postgres port v2",
                                "supersedes": ["old.md"]},
                     "the listener moved; the number is elsewhere now")
        b.write_note("neigh.md", {"title": "Neighbor"},
                     "completely unrelated prose about gardening")
        b = Bundle(self.root)
        scores = activate(b, None, [Cue("7433", 1.0, "query")]).scores
        self.assertIn("old.md", scores)          # the (superseded) match scores
        self.assertNotIn("neigh.md", scores)     # its walk-only neighbor drops


# -- privacy: content cannot egress via a proxy or a redirect ---------------

class TestPrivacyTransport(_EnvGuard):
    """The URL guard pins the HOST, but egress must also be blocked one layer
    down: a configured HTTP proxy (urllib does NOT bypass loopback) and a 3xx
    to an off-host target both have to be refused."""

    def test_proxy_is_bypassed(self):
        real, real_url = _serve(self)      # the intended local endpoint
        sink, sink_url = _serve(self)      # stands in for a proxy sink
        for k in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
            os.environ[k] = sink_url
            self.addCleanup(os.environ.pop, k, None)
        os.environ["MUNINN_EMBED_URL"] = real_url
        vec = embed.embed_text("PRIVATE journal entry")
        self.assertIsNotNone(vec)                 # the real endpoint answered
        self.assertEqual(len(real.requests), 1)   # request went there directly
        self.assertEqual(sink.requests, [])       # the proxy got nothing

    def test_redirect_is_not_followed(self):
        sink, sink_url = _serve(self)             # the redirect target
        os.environ["MUNINN_EMBED_URL"] = _serve_redirect(self, sink_url)
        self.assertIsNone(embed.embed_text("PRIVATE journal entry"))  # fail-soft
        self.assertEqual(sink.requests, [])       # never chased off-host


if __name__ == "__main__":
    unittest.main(verbosity=2)
