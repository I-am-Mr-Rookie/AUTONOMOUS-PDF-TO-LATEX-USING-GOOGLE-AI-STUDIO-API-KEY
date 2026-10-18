"""Local web UI for the PDF -> LaTeX pipeline (standard library only).

Serves frontend/index.html, accepts PDF uploads into input/, runs `main.py` as a
subprocess (one job at a time) and streams its console output back to the page.
Binds to 127.0.0.1 only: it launches the pipeline and receives your API key.

Usage:
    python frontend/server.py            # http://127.0.0.1:8000
    python frontend/server.py --port 9000
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

ROOT = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent
INPUT_DIR = ROOT / "input"
OUTPUT_DIR = ROOT / "output"
MAX_UPLOAD = 100 * 1024 * 1024  # 100 MB

_job: dict = {"running": False, "log": "", "code": None, "file": None}
_lock = threading.Lock()


def safe_pdf_name(name: str) -> str | None:
    """Reduce an untrusted name to a bare *.pdf filename, or None if unusable."""
    name = Path(unquote(name or "")).name.strip()
    if not name.lower().endswith(".pdf") or name.startswith(".") or len(name) > 200:
        return None
    return name


def list_files() -> list[dict]:
    rows = []
    for pdf in sorted(p for p in INPUT_DIR.glob("*") if p.suffix.lower() == ".pdf"):
        out = OUTPUT_DIR / f"{pdf.stem}_LaTeX_compiled.pdf"
        tex = out.with_suffix(".tex")
        rows.append({
            "name": pdf.name,
            "size": pdf.stat().st_size,
            "pdf": out.name if out.exists() else None,
            "tex": tex.name if tex.exists() else None,
        })
    return rows


def _pump(proc: subprocess.Popen) -> None:
    with proc.stdout:
        for line in proc.stdout:
            with _lock:
                _job["log"] += line
    code = proc.wait()
    with _lock:
        _job["running"], _job["code"] = False, code


def start_job(file: str, force: bool, api_key: str) -> str | None:
    """Launch main.py for `file`. Returns an error message, or None on success."""
    with _lock:
        if _job["running"]:
            return "A run is already in progress."
        cmd = [sys.executable, "-u", str(ROOT / "main.py"), "--input", file]
        if force:
            cmd.append("--force")
        env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
        if api_key:
            env["GEMINI_API_KEY"] = api_key  # only lives in the child's environment
        # stdin=DEVNULL: without a key, main.py's getpass prompt gets EOF and exits cleanly.
        proc = subprocess.Popen(cmd, cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace")
        _job.update(running=True, log="", code=None, file=file)
    threading.Thread(target=_pump, args=(proc,), daemon=True).start()
    return None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # keep the console quiet; the UI shows the run log
        pass

    def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, status: int = 200) -> None:
        self._send(status, json.dumps(obj).encode(), "application/json")

    def _body(self) -> bytes | None:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_UPLOAD:
            return None
        return self.rfile.read(length)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path in ("/", "/index.html"):
            self._send(200, (HERE / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif url.path == "/api/files":
            self._json(list_files())
        elif url.path == "/api/job":
            since = parse_qs(url.query).get("since", ["0"])[0]
            since = int(since) if since.isdigit() else 0
            with _lock:
                self._json({**_job, "log": _job["log"][since:], "offset": len(_job["log"])})
        elif url.path.startswith("/output/"):
            name = Path(unquote(url.path[len("/output/"):])).name
            target = OUTPUT_DIR / name
            if name.endswith((".pdf", ".tex")) and target.is_file():
                ctype = "application/pdf" if name.endswith(".pdf") else "text/plain; charset=utf-8"
                self._send(200, target.read_bytes(), ctype,
                           {"Content-Disposition": f'inline; filename="{name}"'})
            else:
                self._json({"error": "Not found"}, 404)
        else:
            self._json({"error": "Not found"}, 404)

    def _same_origin(self) -> bool:
        # Blocks other websites (CSRF) and DNS-rebinding hosts from driving the pipeline.
        port = self.server.server_address[1]
        allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
        origin = self.headers.get("Origin")
        return self.headers.get("Host") in allowed and (
            origin is None or urlparse(origin).netloc in allowed)

    def do_POST(self):
        url = urlparse(self.path)
        if not self._same_origin():
            return self._json({"error": "Cross-origin request refused."}, 403)
        if url.path == "/api/upload":
            name = safe_pdf_name(parse_qs(url.query).get("name", [""])[0])
            data = self._body()
            if not name:
                return self._json({"error": "File name must end in .pdf."}, 400)
            if not data or not data.startswith(b"%PDF"):
                return self._json({"error": "Not a PDF, empty, or larger than 100 MB."}, 400)
            INPUT_DIR.mkdir(parents=True, exist_ok=True)
            (INPUT_DIR / name).write_bytes(data)
            self._json({"name": name})
        elif url.path == "/api/run":
            try:
                req = json.loads(self._body() or b"{}")
            except json.JSONDecodeError:
                return self._json({"error": "Bad request."}, 400)
            name = safe_pdf_name(str(req.get("file", "")))
            if not name or not (INPUT_DIR / name).is_file():
                return self._json({"error": "Pick a PDF from input/ first."}, 400)
            err = start_job(name, bool(req.get("force")), str(req.get("api_key", "")).strip())
            self._json({"error": err} if err else {"ok": True}, 409 if err else 200)
        else:
            self._json({"error": "Not found"}, 404)


def main() -> None:
    ap = argparse.ArgumentParser(description="Local web UI for the PDF->LaTeX pipeline.")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"PDF → LaTeX UI on http://127.0.0.1:{args.port}  (Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
