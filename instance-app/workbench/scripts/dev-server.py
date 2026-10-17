"""Local dev server for frontend_v2: static files + same-origin reverse proxy.

The instance app has no CORS, so the browser must reach it on the page's own
origin. This serves the frontend_v2 directory and forwards ``/api/*`` and
``/health*`` to the instance-app backend (default http://127.0.0.1:8091).

``/control/*`` is answered with a JSON 404 unless ``--control`` names a control
plane: ``RuntimeAPI.detect()`` treats exactly that 404 as "standalone, no
lease", so the page talks to the app with an invite-issued Bearer only.

The PDF reader comes from ``instance-app/frontend`` and is served same-origin,
the way the gateway hosts its export (``npm run build``):

* ``/static/reader/*`` -> ``instance-app/frontend/assets`` (reader adapters, PDF.js bridge)
* ``/static/pdfjs/*``  -> ``dist/static/pdfjs``, else the locked ``node_modules/pdfjs-dist``

Standard library only:

    python scripts/dev-server.py --port 8092 --api http://127.0.0.1:8091
"""
from __future__ import annotations

import argparse
import http.client
import json
import sys
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parent.parent
#: Request headers forwarded upstream. Everything else (Host, Origin, Cookie for
#: the app) is dropped: the app only trusts its own Bearer.
FORWARD_REQUEST = {"authorization", "content-type", "content-length", "accept", "range",
                   "if-none-match", "if-range", "idempotency-key", "cookie"}
HOP_BY_HOP = {"connection", "keep-alive", "transfer-encoding", "te", "trailer", "upgrade",
              "proxy-authenticate", "proxy-authorization"}


class Handler(SimpleHTTPRequestHandler):
    upstreams: dict[str, str] = {}
    #: (url prefix, candidate directories) -- first directory holding the file wins.
    mounts: list[tuple[str, list[Path]]] = []
    extensions_map = {**SimpleHTTPRequestHandler.extensions_map, ".mjs": "text/javascript",
                      ".js": "text/javascript", ".wasm": "application/wasm", ".bcmap": "application/octet-stream"}

    def translate_path(self, path: str) -> str:
        clean = urlsplit(path).path
        for prefix, dirs in self.mounts:
            if clean.startswith(prefix):
                parts = [x for x in unquote(clean[len(prefix):]).split("/") if x not in ("", ".", "..")]
                for base in dirs:
                    candidate = base.joinpath(*parts)
                    if candidate.is_file():
                        return str(candidate)
                return str(dirs[0].joinpath(*parts))
        return super().translate_path(path)

    def _target(self) -> str | None:
        path = self.path.split("?", 1)[0]
        if path.startswith("/api/") or path == "/api":
            return self.upstreams["api"]
        if path == "/health" or path.startswith("/health/"):
            return self.upstreams["api"]
        if path.startswith("/control/") or path == "/control":
            return self.upstreams.get("control") or ""
        return None

    def _proxy(self) -> bool:
        target = self._target()
        if target is None:
            return False
        if not target:
            body = json.dumps({"error": {"code": "not_found", "message": "No control plane in local dev"}}).encode()
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
            return True
        url = urlsplit(target)
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        headers = {k: v for k, v in self.headers.items() if k.lower() in FORWARD_REQUEST}
        conn = http.client.HTTPConnection(url.hostname, url.port or 80, timeout=120)
        try:
            conn.request(self.command, self.path, body=body, headers=headers)
            res = conn.getresponse()
        except OSError as exc:
            payload = json.dumps({"error": {"code": "upstream_unavailable", "message": f"Backend unreachable: {exc}"}}).encode()
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return True
        try:
            self.send_response(res.status, res.reason)
            for key, value in res.getheaders():
                if key.lower() not in HOP_BY_HOP:
                    self.send_header(key, value)
            self.end_headers()
            if self.command != "HEAD":
                while chunk := res.read(64 * 1024):
                    self.wfile.write(chunk)
        finally:
            conn.close()
        return True

    def do_GET(self):  # noqa: N802 - http.server naming
        if not self._proxy():
            super().do_GET()

    def do_HEAD(self):  # noqa: N802
        if not self._proxy():
            super().do_HEAD()

    def _proxy_only(self):
        if not self._proxy():
            self.send_error(405)

    do_POST = do_PUT = do_PATCH = do_DELETE = _proxy_only

    def end_headers(self):
        # Static assets must not be cached while iterating on them.
        if self._target() is None:
            self.send_header("Cache-Control", "no-store")
        super().end_headers()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8092)
    parser.add_argument("--api", default="http://127.0.0.1:8091", help="instance-app backend origin")
    parser.add_argument("--control", default="", help="optional control-plane origin for /control/*")
    parser.add_argument("--reader", default=str(ROOT.parent / "frontend"),
                        help="instance-app/frontend directory providing the PDF reader")
    args = parser.parse_args(argv)
    Handler.upstreams = {"api": args.api.rstrip("/"), "control": args.control.rstrip("/")}
    reader = Path(args.reader)
    pdfjs = reader / "node_modules" / "pdfjs-dist"
    Handler.mounts = [
        ("/static/reader/", [reader / "assets"]),
        ("/static/pdfjs/", [reader / "dist" / "static" / "pdfjs", pdfjs / "build", pdfjs]),
    ]
    if not (pdfjs / "build" / "pdf.min.mjs").is_file() and not (reader / "dist" / "static" / "pdfjs" / "pdf.min.mjs").is_file():
        print(f"warning: no PDF.js build under {reader}; run `npm ci` there", file=sys.stderr)
    server = ThreadingHTTPServer((args.host, args.port), partial(Handler, directory=str(ROOT)))
    print(f"frontend_v2 on http://{args.host}:{args.port}/  (/api, /health -> {args.api}"
          f"{', /control -> ' + args.control if args.control else ''})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
