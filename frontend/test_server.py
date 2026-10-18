"""Offline self-check for frontend/server.py (no API calls, no real pipeline).

Runs the HTTP server against a temp project root whose main.py is a stub.
    python -m unittest frontend/test_server.py
"""
import json
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import server  # noqa: E402

STUB = """import os, sys
print("✓ Preflight passed.")
print("→ Phase 2: stub")
print("key=" + os.environ.get("GEMINI_API_KEY", ""))
print("args=" + " ".join(sys.argv[1:]))
print("🎉 Done")
"""


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        (root / "main.py").write_text(STUB, encoding="utf-8")
        (root / "output").mkdir()
        (root / "output" / "a_LaTeX_compiled.pdf").write_bytes(b"%PDF-1.4 out")
        server.ROOT, server.INPUT_DIR, server.OUTPUT_DIR = root, root / "input", root / "output"
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.tmp.cleanup()

    def req(self, path, data=None, headers=None):
        r = urllib.request.Request(self.base + path, data=data, headers=headers or {})
        try:
            with urllib.request.urlopen(r) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_flow(self):
        self.assertEqual(self.req("/")[0], 200)
        self.assertEqual(self.req("/api/upload?name=../evil.pdf", b"%PDF-1.4 x")[0], 200)
        self.assertTrue((server.INPUT_DIR / "evil.pdf").exists())  # traversal stripped to basename
        self.assertEqual(self.req("/api/upload?name=a.pdf", b"not a pdf")[0], 400)
        self.assertEqual(self.req("/api/upload?name=a.txt", b"%PDF")[0], 400)
        self.assertEqual(self.req("/api/upload?name=a.pdf", b"%PDF-1.4 a")[0], 200)

        files = json.loads(self.req("/api/files")[1])
        self.assertEqual([f["name"] for f in files], ["a.pdf", "evil.pdf"])
        self.assertEqual(files[0]["pdf"], "a_LaTeX_compiled.pdf")
        self.assertEqual(self.req("/output/a_LaTeX_compiled.pdf")[1], b"%PDF-1.4 out")
        self.assertEqual(self.req("/output/..%2Fmain.py")[0], 404)

        body = json.dumps({"file": "a.pdf", "force": True, "api_key": "k123"}).encode()
        self.assertEqual(self.req("/api/run", body, {"Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.req("/api/run", json.dumps({"file": "nope.pdf"}).encode())[0], 400)
        self.assertEqual(self.req("/api/run", body)[0], 200)
        for _ in range(50):
            job = json.loads(self.req("/api/job")[1])
            if not job["running"]:
                break
            time.sleep(0.1)
        self.assertEqual(job["code"], 0)
        self.assertIn("key=k123", job["log"])
        self.assertIn("args=--input a.pdf --force", job["log"])
        tail = json.loads(self.req(f"/api/job?since={job['offset']}")[1])
        self.assertEqual(tail["log"], "")

    def test_uppercase_extension_is_listed(self):
        # Scanners often write "Scan.PDF"; the upload accepts it, so the list must show it.
        self.assertEqual(self.req("/api/upload?name=Scan.PDF", b"%PDF-1.4 s")[0], 200)
        names = [f["name"] for f in json.loads(self.req("/api/files")[1])]
        self.assertIn("Scan.PDF", names)

    def test_bad_since_param(self):
        self.assertEqual(self.req("/api/job?since=abc")[0], 200)


if __name__ == "__main__":
    unittest.main()
