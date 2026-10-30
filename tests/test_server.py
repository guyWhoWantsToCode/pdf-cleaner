"""
Tests for PDF Cleaner's local server. Standard library only.

Run from the project folder:
    py -3 -m unittest discover -s tests -v

Everything happens in a temporary folder with made-up PDFs. Saved progress goes to a
temporary file, never to data/progress.json.

The Recycle Bin test is off by default, because it really moves a small test file to
your Recycle Bin. To run it:  set PDF_CLEANER_TEST_RECYCLE=1
"""
import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
# Never read or write the real saved progress, even at import time.
os.environ.setdefault("PDF_CLEANER_DATA_DIR", tempfile.mkdtemp(prefix="pdfcleaner-testdata-"))

import server  # noqa: E402
from sample_pdfs import make_objstm_pdf, make_pdf  # noqa: E402


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="pdfcleaner-test-")
        cls.root = os.path.join(cls.tmp, "My PDFs")
        os.makedirs(os.path.join(cls.root, "School", "Grade 9"))
        os.makedirs(os.path.join(cls.root, "Stories"))
        make_pdf(os.path.join(cls.root, "Notes.pdf"), "Notes", 3)
        make_pdf(os.path.join(cls.root, "Receipt.PDF"), "Receipt", 1)
        make_pdf(os.path.join(cls.root, "School", "Essay & 'Rivers'.pdf"), "Essay", 5)
        make_pdf(os.path.join(cls.root, "School", "Grade 9", "Math 2.pdf"), "Math 2", 2)
        make_pdf(os.path.join(cls.root, "Stories", "Dragon.pdf"), "Dragon", 12)
        make_objstm_pdf(os.path.join(cls.root, "Stories", "Compressed.pdf"), 7)
        with open(os.path.join(cls.root, "notes.txt"), "w") as fh:
            fh.write("not a pdf")
        with open(os.path.join(cls.root, "fake.pdf.txt"), "w") as fh:
            fh.write("not a pdf")
        os.makedirs(os.path.join(cls.root, "folder.pdf"))  # a folder named like a PDF

        # A PDF outside the chosen folder, reachable only through a junction.
        cls.outside = os.path.join(cls.tmp, "Outside")
        os.makedirs(cls.outside)
        make_pdf(os.path.join(cls.outside, "Secret.pdf"), "Outside", 1)
        cls.has_junction = False
        if os.name == "nt":
            r = subprocess.run(["cmd", "/c", "mklink", "/J", os.path.join(cls.root, "Link"), cls.outside],
                               capture_output=True)
            cls.has_junction = r.returncode == 0

        server.STORE = server.ProgressStore(os.path.join(cls.tmp, "progress.json"))
        cls.httpd = server.LocalServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        status, body, _ = cls.request("GET", "/")
        cls.token = re.search(rb'name="app-token" content="([^"]+)"', body).group(1).decode()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        if cls.has_junction:
            os.rmdir(os.path.join(cls.root, "Link"))  # removes the link only, not its target
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # ---------------------------------------------------------------- helpers

    @classmethod
    def request(cls, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", cls.port, timeout=30)
        hdrs = {"Host": f"127.0.0.1:{cls.port}"}
        hdrs.update(headers or {})
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            hdrs["Content-Type"] = "application/json"
        conn.request(method, path, body=data, headers=hdrs)
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        return resp.status, raw, {k.lower(): v for k, v in resp.getheaders()}

    def api(self, path, body):
        status, raw, _ = self.request("POST", path, body, {"X-Token": self.token})
        return status, json.loads(raw)

    def scan(self, recursive=True):
        status, data = self.api("/api/scan", {"folder": self.root, "recursive": recursive})
        self.assertEqual(status, 200, data)
        return {f["name"]: f for f in data["files"]}

    # ---------------------------------------------------------------- access control

    def test_page_has_strict_headers(self):
        status, _, headers = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("default-src 'self'", headers["content-security-policy"])
        self.assertEqual(headers["x-frame-options"], "DENY")

    def test_other_host_names_refused(self):
        self.assertEqual(self.request("GET", "/", headers={"Host": "evil.example"})[0], 403)
        self.assertEqual(self.request("GET", "/", headers={"Host": f"127.0.0.1.evil.example:{self.port}"})[0], 403)

    def test_api_needs_token(self):
        self.assertEqual(self.request("POST", "/api/state", {})[0], 403)
        self.assertEqual(self.request("POST", "/api/state", {}, {"X-Token": "wrong"})[0], 403)

    def test_other_origins_refused(self):
        for origin in ("http://evil.example", "null"):
            status = self.request("POST", "/api/state", {}, {"X-Token": self.token, "Origin": origin})[0]
            self.assertEqual(status, 403, origin)

    def test_only_listed_static_files(self):
        self.assertEqual(self.request("GET", "/static/app.js")[0], 200)
        for path in ("/static/../server.py", "/static/..%5Cserver.py", "/static/index.html", "/server.py"):
            self.assertEqual(self.request("GET", path)[0], 404, path)

    def test_oversized_body_refused(self):
        big = {"x": "a" * (server.MAX_BODY_BYTES + 10)}
        self.assertEqual(self.api("/api/state", big)[0], 413)

    # ---------------------------------------------------------------- scanning

    def test_bad_folders_refused(self):
        self.assertEqual(self.api("/api/scan", {"folder": "relative\\path"})[0], 400)
        self.assertEqual(self.api("/api/scan", {"folder": os.path.join(self.root, "missing")})[0], 404)
        self.assertEqual(self.api("/api/scan", {"folder": ""})[0], 400)

    def test_scan_top_folder_only(self):
        self.assertEqual(sorted(self.scan(recursive=False)), ["Notes.pdf", "Receipt.PDF"])

    def test_scan_finds_only_pdf_files(self):
        names = set(self.scan())
        self.assertEqual(names, {"Notes.pdf", "Receipt.PDF", "Essay & 'Rivers'.pdf", "Math 2.pdf",
                                 "Dragon.pdf", "Compressed.pdf"})

    def test_scan_does_not_follow_junctions(self):
        if not self.has_junction:
            self.skipTest("could not create a junction")
        self.assertNotIn("Secret.pdf", self.scan())

    # ---------------------------------------------------------------- reading

    def test_page_counts(self):
        files = self.scan()
        ids = [f["id"] for f in files.values()]
        _, data = self.api("/api/pages", {"ids": ids})
        pages = {name: data["pages"].get(f["id"]) for name, f in files.items()}
        self.assertEqual(pages["Notes.pdf"], 3)
        self.assertEqual(pages["Dragon.pdf"], 12)
        self.assertEqual(pages["Compressed.pdf"], 7)

    def test_page_counter_is_fast_on_crafted_input(self):
        path = os.path.join(self.tmp, "crafted.pdf")
        with open(path, "wb") as fh:
            fh.write(b"%PDF-1.4\n" + b"<< " * 2000 + b"/Type /Pages " * 200_000)
        start = time.monotonic()
        server.count_pdf_pages(path)
        self.assertLess(time.monotonic() - start, 5)

    def test_pdf_is_served_read_only_with_safe_headers(self):
        f = self.scan()["Notes.pdf"]
        before = os.path.getmtime(os.path.join(self.root, "Notes.pdf"))
        status, body, headers = self.request("GET", f"/api/file/{f['id']}/Notes.pdf?t={self.token}")
        self.assertEqual(status, 200)
        self.assertTrue(body.startswith(b"%PDF"))
        self.assertEqual(headers["content-type"], "application/pdf")
        self.assertEqual(headers["x-content-type-options"], "nosniff")
        self.assertEqual(before, os.path.getmtime(os.path.join(self.root, "Notes.pdf")))

    def test_pdf_needs_token_and_known_id(self):
        f = self.scan()["Notes.pdf"]
        self.assertEqual(self.request("GET", f"/api/file/{f['id']}/x.pdf?t=wrong")[0], 403)
        self.assertEqual(self.request("GET", f"/api/file/0123456789abcdef/x.pdf?t={self.token}")[0], 404)
        self.assertEqual(self.request("GET", f"/api/file/..%5C..%5Cx/x.pdf?t={self.token}")[0], 400)

    # ---------------------------------------------------------------- decisions

    def test_decisions_are_saved_and_can_be_cleared(self):
        files = self.scan()
        notes, math = files["Notes.pdf"], files["Math 2.pdf"]
        self.assertEqual(self.api("/api/status", {"id": notes["id"], "status": "keep"})[0], 200)
        self.assertEqual(self.api("/api/status", {"id": math["id"], "status": "skip"})[0], 200)
        again = self.scan()
        self.assertEqual(again["Notes.pdf"]["status"], "keep")
        self.assertEqual(again["Math 2.pdf"]["status"], "skip")
        with open(server.STORE.path, encoding="utf-8") as fh:
            self.assertIn("Notes.pdf", fh.read())
        self.api("/api/status", {"id": notes["id"], "status": None})
        self.api("/api/status", {"id": math["id"], "status": None})
        self.assertIsNone(self.scan()["Notes.pdf"]["status"])

    def test_unknown_decision_refused(self):
        f = self.scan()["Notes.pdf"]
        self.assertEqual(self.api("/api/status", {"id": f["id"], "status": "deleted"})[0], 400)
        self.assertEqual(self.api("/api/status", {"id": "../../etc", "status": "keep"})[0], 400)

    # ---------------------------------------------------------------- deletion safety

    def test_delete_needs_confirmation(self):
        f = self.scan()["Receipt.PDF"]
        self.assertEqual(self.api("/api/delete", {"id": f["id"]})[0], 400)
        self.assertEqual(self.api("/api/delete", {"id": f["id"], "confirm": "yes"})[0], 400)
        self.assertTrue(os.path.exists(os.path.join(self.root, "Receipt.PDF")))

    def test_delete_refuses_a_folder_swapped_in(self):
        f = self.scan()["Essay & 'Rivers'.pdf"]
        path = os.path.join(self.root, f["rel"])
        os.replace(path, path + ".bak")
        os.mkdir(path)
        try:
            status, _ = self.api("/api/delete", {"id": f["id"], "confirm": server.DELETE_CONFIRM_PHRASE})
            self.assertEqual(status, 400)
            self.assertTrue(os.path.isdir(path))
        finally:
            os.rmdir(path)
            os.replace(path + ".bak", path)

    def test_delete_refuses_files_gone_since_scan(self):
        f = self.scan()["Dragon.pdf"]
        path = os.path.join(self.root, f["rel"])
        os.replace(path, path + ".moved")
        try:
            status, _ = self.api("/api/delete", {"id": f["id"], "confirm": server.DELETE_CONFIRM_PHRASE})
            self.assertEqual(status, 410)
        finally:
            os.replace(path + ".moved", path)

    def test_only_one_copy_per_folder(self):
        lock_dir = os.path.join(self.tmp, "lockdata")
        first = server.acquire_instance_lock(lock_dir)
        self.assertIsNotNone(first)
        try:
            self.assertIsNone(server.acquire_instance_lock(lock_dir))
        finally:
            first.close()
        again = server.acquire_instance_lock(lock_dir)
        self.assertIsNotNone(again)
        again.close()

    @unittest.skipUnless(os.name == "nt" and os.environ.get("PDF_CLEANER_TEST_RECYCLE") == "1",
                         "set PDF_CLEANER_TEST_RECYCLE=1 to test the real Recycle Bin")
    def test_zz_delete_moves_to_recycle_bin(self):
        make_pdf(os.path.join(self.root, "Recycle me.pdf"), "Recycle me", 1)
        f = self.scan()["Recycle me.pdf"]
        status, data = self.api("/api/delete", {"id": f["id"], "confirm": server.DELETE_CONFIRM_PHRASE})
        self.assertEqual(status, 200, data)
        self.assertFalse(os.path.exists(os.path.join(self.root, "Recycle me.pdf")))
        files = self.scan()
        self.assertTrue(files["Recycle me.pdf"]["ghost"])
        self.assertEqual(files["Recycle me.pdf"]["status"], "deleted")


if __name__ == "__main__":
    unittest.main()
