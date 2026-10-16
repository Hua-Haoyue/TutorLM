"""Run the in-container application: ``python -m backend`` from instance-app/.

Only stdlib + uvicorn are used here; the ASGI app is assembled by
``backend.adapters.http.app.create_app`` (the same factory the image
ENTRYPOINT targets). There is no ``backend.host.app`` module: the Host layer
holds the composition root (``backend.host.state``) and the HTTP assembly lives
in ``backend.adapters.http.app``.
"""
from __future__ import annotations

import argparse
import os
import sys


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m backend", description="TutorLM instance application host")
    parser.add_argument("--host", default=os.environ.get("TUTORLM_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("TUTORLM_PORT", "8000")))
    parser.add_argument("--log-level", default=os.environ.get("TUTORLM_LOG_LEVEL", "info"))
    parser.add_argument("--reload", action="store_true", help="development only: reload on source changes")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        import uvicorn  # imported lazily so tests never need a server runtime
    except Exception as exc:  # pragma: no cover - deployment dependency
        print(f"uvicorn is required to serve the app: {exc}", file=sys.stderr)
        return 2

    from .adapters.http.app import create_app

    uvicorn.run(create_app(), host=args.host, port=args.port, log_level=args.log_level, reload=args.reload)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
