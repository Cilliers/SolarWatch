"""Run with:  python -m solarwatch [--config config.yaml]"""
import argparse
import logging
import os

import uvicorn

from .config import load_config
from .server import create_app


def load_dotenv(path: str = ".env") -> None:
    """Minimal .env support so secrets work the same with and without Docker."""
    if not os.path.exists(path):
        return
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def main() -> None:
    load_dotenv()
    p = argparse.ArgumentParser(prog="solarwatch")
    p.add_argument("--config", default=os.environ.get("SOLARWATCH_CONFIG", "config.yaml"))
    p.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "info"))
    args = p.parse_args()
    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config(args.config)
    if not cfg.get("auth") and cfg["server"]["host"] not in ("127.0.0.1", "localhost"):
        logging.warning("No auth configured and listening on %s - anyone on your network can see "
                        "customer sites. Set auth.username/password in the config.", cfg["server"]["host"])
    uvicorn.run(create_app(cfg), host=cfg["server"]["host"], port=int(cfg["server"]["port"]),
                log_level=args.log_level.lower())


if __name__ == "__main__":
    main()
