"""Define the egress policy for optional network channels.

Loopback endpoints use an opener that disables proxies and rejects redirects.
A nonlocal endpoint requires a channel-specific opt-in and uses the ordinary
remote opener. ``PrivacyError`` is intentionally excluded from
``TRANSPORT``, so transport fallback cannot hide an attempted privacy
boundary violation. Embedding and enrichment share these primitives while
retaining their own configuration and diagnostics.
"""

from __future__ import annotations

import os
import urllib.error
import urllib.request

LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

TRANSPORT = (urllib.error.URLError, OSError, ValueError, KeyError,
             IndexError, TypeError, TimeoutError)


class PrivacyError(Exception):
    """An endpoint would cross the network boundary without explicit consent."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None  # a redirect off the pinned loopback host is never followed


HARDENED_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}), _NoRedirect)
REMOTE_OPENER = urllib.request.build_opener()


def allow_remote(env_var: str) -> bool:
    """True when the user explicitly opted into a non-local endpoint via
    ``env_var`` (``1``/``true``/``yes``, case-insensitive)."""
    return os.environ.get(env_var, "").strip().lower() in ("1", "true", "yes")
