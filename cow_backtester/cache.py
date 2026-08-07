#!/usr/bin/env python3
"""Content cache for cow-backtester.

Only IMMUTABLE facts are cached: archived auction bodies (an auction's /solve
body never changes), settlement transactions/receipts once they are safely
below the chain head, and block timestamps. Nothing here caches solver
responses or anything derived from live liquidity.

Layout: <dir>/<chain>/<kind>/<key>.json.gz — one small gzipped JSON per entry,
so a cache is trivially inspectable and safely deletable at any time.
"""

import gzip
import json
import os
import re
import threading

_SAFE = re.compile(r"[^A-Za-z0-9_.-]")


class Cache:
    def __init__(self, root, chain, enabled=True):
        self.root = root
        self.chain = chain
        self.enabled = bool(enabled and root)
        self.hits = 0
        self.misses = 0
        self.writes = 0

    def _path(self, kind, key):
        safe = _SAFE.sub("_", str(key))[:120]
        return os.path.join(self.root, self.chain, kind, f"{safe}.json.gz")

    def get(self, kind, key):
        if not self.enabled:
            return None
        p = self._path(kind, key)
        try:
            with gzip.open(p, "rt") as f:
                v = json.load(f)
            self.hits += 1
            return v
        except Exception:
            self.misses += 1
            return None

    def put(self, kind, key, value):
        if not self.enabled or value is None:
            return
        p = self._path(kind, key)
        try:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            # unique per process+thread: two writers (e.g. a --watch run plus a
            # manual run sharing a cache dir) can never corrupt an entry
            tmp = f"{p}.{os.getpid()}.{threading.get_ident()}.tmp"
            with gzip.open(tmp, "wt") as f:
                json.dump(value, f)
            os.replace(tmp, p)          # atomic: never a half-written entry
            self.writes += 1
        except Exception:
            pass                        # cache failures must never break a run

    def stats(self):
        return {"hits": self.hits, "misses": self.misses, "writes": self.writes}
