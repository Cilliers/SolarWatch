"""A stand-in for requests.Session that answers from registered handlers."""
import json as _json
from urllib.parse import parse_qs, urlparse

import requests


class FakeResponse:
    def __init__(self, status=200, payload=None, headers=None):
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}

    @property
    def text(self):
        return self._payload if isinstance(self._payload, str) else _json.dumps(self._payload)

    def json(self):
        if isinstance(self._payload, str):
            raise ValueError("not json")
        return self._payload


class FakeSession:
    def __init__(self):
        self.headers = {}
        self.routes = []      # (method, path_substring, handler)
        self.calls = []

    def on(self, method, path, handler):
        """handler: payload, (status, payload[, headers]), FakeResponse, or callable(call) -> either."""
        self.routes.insert(0, (method.upper(), path, handler))
        return self

    def request(self, method, url, params=None, json=None, data=None, headers=None, timeout=None, **_):
        parsed = urlparse(url)
        call = {"method": method.upper(), "url": url, "host": parsed.hostname, "path": parsed.path,
                "params": {**{k: v[0] for k, v in parse_qs(parsed.query).items()}, **(params or {})},
                "json": json, "data": data, "headers": headers or {}}
        self.calls.append(call)
        for m, p, h in self.routes:
            if m == call["method"] and p in parsed.path:
                res = h(call) if callable(h) else h
                if isinstance(res, Exception):
                    raise res
                if isinstance(res, FakeResponse):
                    return res
                if isinstance(res, tuple):
                    return FakeResponse(*res)
                return FakeResponse(200, res)
        raise requests.ConnectionError(f"no fake route for {method} {url}")

    def paths(self):
        return [c["path"] for c in self.calls]
