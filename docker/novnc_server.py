#!/usr/bin/python3
"""
websockify launcher that adds `Cache-Control: no-cache` to every static-file
response, so the noVNC page (and the injected control panel) are never served
from a stale browser cache.

Background: stock websockify sends only Last-Modified with no Cache-Control, so
browsers apply heuristic caching and can keep serving an OLD vnc.html — which is
why a freshly-injected panel appears "missing" until a hard refresh. `no-cache`
forces the browser to revalidate every load; unchanged files still return a
cheap 304 (websockify honours If-Modified-Since), so there is no real overhead,
but an updated vnc.html / miri_panel.js is picked up immediately.

Runs on the SYSTEM python3 (where the apt `websockify` module lives), not the
app's bundled python. Invoked from docker/supervisord.conf.
"""

import sys

from websockify.websocketproxy import ProxyRequestHandler, websockify_init

_orig_end_headers = ProxyRequestHandler.end_headers


def _end_headers(self):
    try:
        self.send_header("Cache-Control", "no-cache")
    except Exception:
        pass
    _orig_end_headers(self)


ProxyRequestHandler.end_headers = _end_headers


if __name__ == "__main__":
    sys.exit(websockify_init())
