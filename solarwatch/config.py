"""Load config.yaml. Any string may reference an environment variable as ${NAME} or
${NAME:-default}, so secrets can stay out of the file."""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

_ENV = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

DEFAULTS: dict[str, Any] = {
    "server": {"host": "0.0.0.0", "port": 8080},
    "database": "data/solarwatch.db",
    "poll_interval": 300,          # seconds; most clouds only refresh every ~5 min
    "retention_days": 90,
    "alerts": {"low_soc": 20, "stale_minutes": 30},
    "auth": None,                  # {"username": ..., "password": ...} enables HTTP basic auth
    "accounts": [],
}


def _expand(v: Any) -> Any:
    if isinstance(v, str):
        return _ENV.sub(lambda m: os.environ.get(m.group(1), m.group(2) or ""), v)
    if isinstance(v, list):
        return [_expand(x) for x in v]
    if isinstance(v, dict):
        return {k: _expand(x) for k, x in v.items()}
    return v


def load_config(path: str | os.PathLike | None = None) -> dict[str, Any]:
    path = Path(path or os.environ.get("SOLARWATCH_CONFIG", "config.yaml"))
    if not path.exists():
        raise FileNotFoundError(f"{path} not found - copy config.example.yaml to {path} and edit it")
    raw = yaml.safe_load(path.read_text())
    cfg = {**DEFAULTS, **_expand(raw or {})}
    for k in ("server", "alerts"):
        cfg[k] = {**DEFAULTS[k], **(cfg.get(k) or {})}
    # Environment wins over the file, so a container can always listen where Docker expects it.
    if os.environ.get("SOLARWATCH_HOST"):
        cfg["server"]["host"] = os.environ["SOLARWATCH_HOST"]
    if os.environ.get("SOLARWATCH_PORT"):
        cfg["server"]["port"] = int(os.environ["SOLARWATCH_PORT"])
    seen = set()
    for acc in cfg["accounts"]:
        if not acc.get("id") or not acc.get("platform"):
            raise ValueError(f"each account needs an id and a platform: {acc!r}")
        if acc["id"] in seen:
            raise ValueError(f"duplicate account id {acc['id']!r}")
        seen.add(acc["id"])
        acc.setdefault("enabled", True)
    auth = cfg.get("auth")
    if auth and not (auth.get("username") and auth.get("password")):
        cfg["auth"] = None   # an unset ${SOLARWATCH_PASSWORD} should not lock everyone out silently
    return cfg
