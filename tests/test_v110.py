"""
Tests for the v1.1.0 features: the duplicate finder, batch Recycle Bin removal of duplicates,
"Show in folder", the per-user data folder, and the packaged .exe.

Run from the project folder:
    py -3 -m unittest discover -s tests -v

Nothing here touches the real Recycle Bin or opens File Explorer: both are replaced with fakes.
Saved progress goes to a temporary folder, never to your real one.

The .exe smoke test only runs when PDF_CLEANER_TEST_EXE points at a built PDFCleaner.exe.
"""
import hashlib
import http.client
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
os.environ.setdefault("PDF_CLEANER_DATA_DIR", tempfile.mkdtemp(prefix="pdfcleaner-testdata-"))

import server  # noqa: E402
from sample_pdfs import make_demo_folder, make_pdf  # noqa: E402

CONFIRM = server.BATCH_DELETE_CONFIRM_PHRASE


def write(path, data: bytes):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)


class HttpMixin:
    port = token = None

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
        return resp.status, raw

    def api(self, path, body=None):
        status, raw = self.request("POST", path, body or {}, {"X-Token": self.token})
        return status, json.loads(raw)

    @classmethod
    def fetch_token(cls):
        _, body = cls.request("GET", "/")
        cls.token = re.search(rb'name="app-token" content="([^"]+)"', body).group(1).decode()


class DuplicateTests(HttpMixin, unittest.TestCase):
    """A fresh folder for every test, served by one in-process server."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="pdfcleaner-dup-")
        server.STORE = server.ProgressStore(os.path.join(cls.tmp, "progress.json"))
        cls.httpd = server.LocalServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.fetch_token()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="case-", dir=self.tmp)
        self.bin = tempfile.mkdtemp(prefix="bin-", dir=self.tmp)
        r = self.root
        self.x = os.path.join(r, "Report.pdf")
        make_pdf(self.x, "Report", 3)
        with open(self.x, "rb") as fh:
            self.x_bytes = fh.read()
        write(os.path.join(r, "Backup", "report copy.pdf"), self.x_bytes)
        write(os.path.join(r, "Backup", "Old", "final_v2.PDF"), self.x_bytes)
        make_pdf(os.path.join(r, "Letter.pdf"), "Letter", 1)
        with open(os.path.join(r, "Letter.pdf"), "rb") as fh:
            y = fh.read()
        write(os.path.join(r, "Mail", "Letter (1).pdf"), y)
        # Same size as Letter.pdf but different contents: must NOT be grouped with it.
        write(os.path.join(r, "Same size.pdf"), y[:-2] + b"X\n")
        make_pdf(os.path.join(r, "Unique.pdf"), "Unique", 7)
        write(os.path.join(r, "not a pdf.txt"), self.x_bytes)  # identical bytes, wrong type

        self.recycled_paths = []
        self.fail_paths = set()
        patcher = mock.patch.object(server, "move_to_recycle_bin", side_effect=self.fake_recycle)
        patcher.start()
        self.addCleanup(patcher.stop)

    def fake_recycle(self, path):
        if os.path.normcase(path) in {os.path.normcase(p) for p in self.fail_paths}:
            raise server.ApiError(500, "Windows couldn't move the file to the Recycle Bin (test).")
        shutil.move(path, os.path.join(self.bin, f"{len(self.recycled_paths)}-{os.path.basename(path)}"))
        self.recycled_paths.append(path)

    # ---------------------------------------------------------------- helpers

    def scan(self):
        status, data = self.api("/api/scan", {"folder": self.root, "recursive": True})
        self.assertEqual(status, 200, data)
        return {f["rel"]: f for f in data["files"]}

    def find_dupes(self):
        status, data = self.api("/api/duplicates/start")
        self.assertEqual(status, 200, data)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            _, s = self.api("/api/duplicates/status")
            if s["state"] != "running":
                self.assertEqual(s["state"], "done", s)
                return s
            time.sleep(0.05)
        self.fail("duplicate search did not finish")

    def named_groups(self, files, status):
        by_id = {f["id"]: rel for rel, f in files.items()}
        return sorted(sorted(by_id[i] for i in g["ids"]) for g in status["groups"])

    def ids(self, files, *rels):
        return [files[os.path.join(*r.split("/"))]["id"] for r in rels]

    # ---------------------------------------------------------------- finding

    def test_groups_by_content_not_name(self):
        files = self.scan()
        s = self.find_dupes()
        self.assertEqual(self.named_groups(files, s), [
            [os.path.join("Backup", "Old", "final_v2.PDF"), os.path.join("Backup", "report copy.pdf"), "Report.pdf"],
            ["Letter.pdf", os.path.join("Mail", "Letter (1).pdf")],
        ])
        report = next(g for g in s["groups"] if len(g["ids"]) == 3)
        self.assertEqual(report["sha256"], hashlib.sha256(self.x_bytes).hexdigest())
        self.assertEqual(report["size"], len(self.x_bytes))
        self.assertEqual(s["groups"][0], report, "largest savings first")

    def test_only_same_size_files_are_hashed(self):
        files = self.scan()
        entries = {f["id"]: {"path": os.path.join(self.root, rel), "rel": rel} for rel, f in files.items()}
        server.HASH_CACHE.clear()
        hashed = []
        real = server.sha256_file

        def spy(path, *a, **kw):
            hashed.append(os.path.basename(path))
            return real(path, *a, **kw)

        with mock.patch.object(server, "sha256_file", side_effect=spy):
            server.find_duplicate_groups(entries)
        self.assertNotIn("Unique.pdf", hashed)  # unique size: never read
        self.assertIn("Same size.pdf", hashed)  # same size: hashed, then told apart
        self.assertEqual(len(hashed), 6)

    def test_reading_never_changes_files(self):
        self.scan()
        before = {p: os.stat(p).st_mtime_ns for p in self.all_pdfs()}
        self.find_dupes()
        self.assertEqual(before, {p: os.stat(p).st_mtime_ns for p in self.all_pdfs()})

    def all_pdfs(self):
        return [os.path.join(d, n) for d, _, ns in os.walk(self.root) for n in ns if n.lower().endswith(".pdf")]

    def test_online_only_files_are_skipped(self):
        files = self.scan()
        entries = {f["id"]: {"path": os.path.join(self.root, rel), "rel": rel} for rel, f in files.items()}
        cloud_ino = os.stat(os.path.join(self.root, "Mail", "Letter (1).pdf")).st_ino
        real = server.is_cloud_only
        with mock.patch.object(server, "is_cloud_only", side_effect=lambda st: st.st_ino == cloud_ino or real(st)):
            job = server.DuplicateJob()
            groups = server.find_duplicate_groups(entries, job)
        self.assertEqual(job.skipped_cloud, 1)
        self.assertEqual(len(groups), 1)  # the Letter pair is gone; nothing was downloaded

    def test_search_can_be_cancelled(self):
        files = self.scan()
        entries = {f["id"]: {"path": os.path.join(self.root, rel), "rel": rel} for rel, f in files.items()}
        job = server.DuplicateJob()
        job.cancel.set()
        server.HASH_CACHE.clear()
        with self.assertRaises(server.Cancelled):
            server.find_duplicate_groups(entries, job)

    def test_needs_a_scan_first_and_a_token(self):
        self.assertEqual(self.request("POST", "/api/duplicates/start", {})[0], 403)
        self.assertEqual(self.request("POST", "/api/delete-duplicates", {}, {"X-Token": "wrong"})[0], 403)

    # ---------------------------------------------------------------- batch removal: refusals

    def assert_nothing_moved(self):
        self.assertEqual(self.recycled_paths, [])
        self.assertEqual(len(self.all_pdfs()), 7)

    def test_batch_needs_confirmation(self):
        files = self.scan()
        self.find_dupes()
        remove = self.ids(files, "Backup/report copy.pdf")
        for confirm in (None, "yes", server.DELETE_CONFIRM_PHRASE):
            self.assertEqual(self.api("/api/delete-duplicates", {"remove": remove, "confirm": confirm})[0], 400)
        self.assert_nothing_moved()

    def test_batch_never_removes_every_copy(self):
        files = self.scan()
        self.find_dupes()
        remove = self.ids(files, "Report.pdf", "Backup/report copy.pdf", "Backup/Old/final_v2.PDF")
        status, data = self.api("/api/delete-duplicates", {"remove": remove, "confirm": CONFIRM})
        self.assertEqual(status, 400, data)
        self.assertIn("at least one copy", data["error"])
        self.assert_nothing_moved()

    def test_batch_refuses_files_that_are_not_duplicates(self):
        files = self.scan()
        self.find_dupes()
        for rel in ("Unique.pdf", "Same size.pdf"):
            remove = self.ids(files, "Backup/report copy.pdf", rel)
            status, _ = self.api("/api/delete-duplicates", {"remove": remove, "confirm": CONFIRM})
            self.assertEqual(status, 400, rel)
        for bad in ([], ["../../x"], "abc", [1, 2]):
            self.assertEqual(self.api("/api/delete-duplicates", {"remove": bad, "confirm": CONFIRM})[0], 400)
        self.assert_nothing_moved()

    def test_batch_refuses_results_from_an_older_scan(self):
        files = self.scan()
        self.find_dupes()
        self.scan()  # the folder was scanned again
        status, _ = self.api("/api/delete-duplicates",
                             {"remove": self.ids(files, "Backup/report copy.pdf"), "confirm": CONFIRM})
        self.assertEqual(status, 409)
        self.assert_nothing_moved()

    def test_batch_refuses_before_any_search(self):
        server.DUP_JOB.reset()
        files = self.scan()
        status, _ = self.api("/api/delete-duplicates",
                             {"remove": self.ids(files, "Backup/report copy.pdf"), "confirm": CONFIRM})
        self.assertEqual(status, 409)
        self.assert_nothing_moved()

    # ---------------------------------------------------------------- batch removal: doing it

    def test_batch_moves_chosen_copies_and_keeps_the_rest(self):
        files = self.scan()
        self.find_dupes()
        remove = self.ids(files, "Backup/report copy.pdf", "Backup/Old/final_v2.PDF", "Mail/Letter (1).pdf")
        status, data = self.api("/api/delete-duplicates", {"remove": remove, "confirm": CONFIRM})
        self.assertEqual(status, 200, data)
        self.assertEqual(sorted(r["id"] for r in data["recycled"]), sorted(remove))
        self.assertEqual(data["failed"], [])
        self.assertTrue(os.path.exists(os.path.join(self.root, "Report.pdf")))
        self.assertTrue(os.path.exists(os.path.join(self.root, "Letter.pdf")))
        self.assertEqual(len(self.recycled_paths), 3)
        again = self.scan()
        for rel in ("Backup/report copy.pdf", "Backup/Old/final_v2.PDF", "Mail/Letter (1).pdf"):
            f = again[os.path.join(*rel.split("/"))]
            self.assertTrue(f["ghost"], rel)
            self.assertEqual(f["status"], "deleted")

    def test_partial_failure_reports_exactly_what_was_recycled(self):
        files = self.scan()
        self.find_dupes()
        stuck = os.path.join(self.root, "Backup", "Old", "final_v2.PDF")
        self.fail_paths.add(stuck)
        remove = self.ids(files, "Backup/report copy.pdf", "Backup/Old/final_v2.PDF", "Mail/Letter (1).pdf")
        status, data = self.api("/api/delete-duplicates", {"remove": remove, "confirm": CONFIRM})
        self.assertEqual(status, 200, data)
        self.assertEqual(sorted(r["rel"] for r in data["recycled"]),
                         sorted([os.path.join("Backup", "report copy.pdf"), os.path.join("Mail", "Letter (1).pdf")]))
        self.assertEqual([(f["rel"], f["id"]) for f in data["failed"]],
                         [(os.path.join("Backup", "Old", "final_v2.PDF"), files[os.path.join("Backup", "Old", "final_v2.PDF")]["id"])])
        self.assertIn("Recycle Bin", data["failed"][0]["error"])
        self.assertTrue(os.path.exists(stuck))
        self.assertEqual({os.path.normcase(p) for p in self.recycled_paths},
                         {os.path.normcase(os.path.join(self.root, *r["rel"].split(os.sep))) for r in data["recycled"]})
        # The failed file stays a normal, reviewable file; the others are remembered as recycled.
        again = self.scan()
        self.assertFalse(again[os.path.join("Backup", "Old", "final_v2.PDF")].get("ghost"))
        self.assertTrue(again[os.path.join("Backup", "report copy.pdf")]["ghost"])

    def test_file_changed_after_search_is_left_alone(self):
        files = self.scan()
        self.find_dupes()
        changed = os.path.join(self.root, "Backup", "report copy.pdf")
        write(changed, self.x_bytes[:-3] + b"ZZ\n")  # same size, different contents
        remove = self.ids(files, "Backup/report copy.pdf", "Backup/Old/final_v2.PDF")
        status, data = self.api("/api/delete-duplicates", {"remove": remove, "confirm": CONFIRM})
        self.assertEqual(status, 200, data)
        self.assertEqual([r["rel"] for r in data["recycled"]], [os.path.join("Backup", "Old", "final_v2.PDF")])
        self.assertEqual([f["rel"] for f in data["failed"]], [os.path.join("Backup", "report copy.pdf")])
        self.assertIn("changed", data["failed"][0]["error"])
        self.assertTrue(os.path.exists(changed))

    def test_group_left_alone_if_the_kept_copy_is_gone(self):
        files = self.scan()
        self.find_dupes()
        os.remove(os.path.join(self.root, "Letter.pdf"))  # the copy the user meant to keep
        status, data = self.api("/api/delete-duplicates",
                                {"remove": self.ids(files, "Mail/Letter (1).pdf"), "confirm": CONFIRM})
        self.assertEqual(status, 200, data)
        self.assertEqual(data["recycled"], [])
        self.assertEqual([f["rel"] for f in data["failed"]], [os.path.join("Mail", "Letter (1).pdf")])
        self.assertTrue(os.path.exists(os.path.join(self.root, "Mail", "Letter (1).pdf")))

    def test_results_update_after_a_batch(self):
        files = self.scan()
        self.find_dupes()
        self.api("/api/delete-duplicates", {"remove": self.ids(files, "Mail/Letter (1).pdf"), "confirm": CONFIRM})
        _, s = self.api("/api/duplicates/status")
        self.assertEqual(len(s["groups"]), 1)
        # A second batch on the remaining group still works without searching again.
        status, data = self.api("/api/delete-duplicates",
                                {"remove": self.ids(files, "Backup/report copy.pdf"), "confirm": CONFIRM})
        self.assertEqual((status, len(data["recycled"])), (200, 1))

    def test_single_delete_still_works(self):
        files = self.scan()
        status, data = self.api("/api/delete", {"id": files["Unique.pdf"]["id"],
                                                "confirm": server.DELETE_CONFIRM_PHRASE})
        self.assertEqual(status, 200, data)
        self.assertFalse(os.path.exists(os.path.join(self.root, "Unique.pdf")))
        self.assertTrue(self.scan()["Unique.pdf"]["ghost"])

    # ---------------------------------------------------------------- show in folder

    def test_show_in_folder(self):
        files = self.scan()
        with mock.patch.object(server, "reveal_in_explorer") as reveal:
            status, data = self.api("/api/reveal", {"id": files["Report.pdf"]["id"]})
        self.assertEqual(status, 200, data)
        reveal.assert_called_once_with(os.path.join(self.root, "Report.pdf"))

    def test_show_in_folder_refuses_unknown_and_recycled_files(self):
        files = self.scan()
        self.api("/api/delete", {"id": files["Unique.pdf"]["id"], "confirm": server.DELETE_CONFIRM_PHRASE})
        with mock.patch.object(server, "reveal_in_explorer") as reveal:
            self.assertEqual(self.api("/api/reveal", {"id": files["Unique.pdf"]["id"]})[0], 404)
            self.assertEqual(self.api("/api/reveal", {"id": "0123456789abcdef"})[0], 404)
            self.assertEqual(self.api("/api/reveal", {"id": "..\\..\\Windows"})[0], 400)
            os.remove(os.path.join(self.root, "Letter.pdf"))
            self.assertEqual(self.api("/api/reveal", {"id": files["Letter.pdf"]["id"]})[0], 410)
        reveal.assert_not_called()


@unittest.skipUnless(os.name == "nt" and os.environ.get("PDF_CLEANER_TEST_RECYCLE") == "1",
                     "set PDF_CLEANER_TEST_RECYCLE=1 to test the real Recycle Bin")
class RealRecycleBinBatchTest(HttpMixin, unittest.TestCase):
    """Really moves one generated duplicate to your Recycle Bin, keeping the other copy."""

    def test_batch_uses_the_real_recycle_bin(self):
        tmp = tempfile.mkdtemp(prefix="pdfcleaner-realbin-")
        self.addCleanup(shutil.rmtree, tmp, True)
        root = os.path.join(tmp, "PDFs")
        os.makedirs(root)
        make_pdf(os.path.join(root, "Keep me.pdf"), "Duplicate test", 1)
        shutil.copy(os.path.join(root, "Keep me.pdf"), os.path.join(root, "Recycle me (copy).pdf"))
        server.STORE = server.ProgressStore(os.path.join(tmp, "progress.json"))
        httpd = server.LocalServer(("127.0.0.1", 0), server.Handler)
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        type(self).port = httpd.server_address[1]
        self.fetch_token()
        _, data = self.api("/api/scan", {"folder": root, "recursive": True})
        ids = {f["name"]: f["id"] for f in data["files"]}
        self.api("/api/duplicates/start")
        for _ in range(200):
            _, s = self.api("/api/duplicates/status")
            if s["state"] != "running":
                break
            time.sleep(0.05)
        status, data = self.api("/api/delete-duplicates",
                                {"remove": [ids["Recycle me (copy).pdf"]], "confirm": CONFIRM})
        self.assertEqual(status, 200, data)
        self.assertEqual([r["rel"] for r in data["recycled"]], ["Recycle me (copy).pdf"])
        self.assertFalse(os.path.exists(os.path.join(root, "Recycle me (copy).pdf")))
        self.assertTrue(os.path.exists(os.path.join(root, "Keep me.pdf")))


class DataFolderTests(unittest.TestCase):
    def test_env_override(self):
        with mock.patch.dict(os.environ, {"PDF_CLEANER_DATA_DIR": "C:\\Somewhere\\Data"}):
            self.assertEqual(server.user_data_dir(), os.path.abspath("C:\\Somewhere\\Data"))

    @unittest.skipUnless(os.name == "nt", "Windows path rules")
    def test_default_is_local_appdata(self):
        env = {k: v for k, v in os.environ.items() if k != "PDF_CLEANER_DATA_DIR"}
        env["LOCALAPPDATA"] = "C:\\Users\\you\\AppData\\Local"
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(server.user_data_dir(), "C:\\Users\\you\\AppData\\Local\\PDF Cleaner")

    def test_data_never_lives_in_the_program_folder(self):
        self.assertFalse(server.is_within(server.DATA_DIR, server.APP_DIR))

    def test_legacy_progress_is_copied_once_and_kept(self):
        tmp = tempfile.mkdtemp(prefix="pdfcleaner-migrate-")
        self.addCleanup(shutil.rmtree, tmp, True)
        legacy = os.path.join(tmp, "old", "data", "progress.json")
        target = os.path.join(tmp, "new", "progress.json")
        write(legacy, b'{"version": 1, "settings": {"lastFolder": "C:\\\\x"}, "folders": {}}')
        self.assertTrue(server.migrate_legacy_progress(legacy, target))
        self.assertTrue(os.path.exists(legacy), "the original is never moved or deleted")
        self.assertEqual(server.ProgressStore(target).data["settings"]["lastFolder"], "C:\\x")
        write(legacy, b"{}")
        self.assertFalse(server.migrate_legacy_progress(legacy, target), "never overwrites newer progress")
        self.assertFalse(server.migrate_legacy_progress(None, target))
        self.assertFalse(server.migrate_legacy_progress(os.path.join(tmp, "missing.json"), target + "2"))

    def test_packaged_paths(self):
        with mock.patch.object(server, "FROZEN", True), mock.patch.object(sys, "_MEIPASS", "C:\\Temp\\_MEI123", create=True):
            self.assertEqual(server.app_dir(), "C:\\Temp\\_MEI123")
            self.assertEqual(server.picker_command("C:\\Docs"), [sys.executable, "--pick-folder", "C:\\Docs"])
        cmd = server.picker_command("")
        self.assertIn("-c", cmd)

    def test_version(self):
        self.assertEqual(server.__version__, "1.1.0")
        self.assertEqual(server.api_state({})["version"], "1.1.0")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@unittest.skipUnless(os.environ.get("PDF_CLEANER_TEST_EXE"), "set PDF_CLEANER_TEST_EXE to a built PDFCleaner.exe")
class ExeSmokeTest(HttpMixin, unittest.TestCase):
    """Starts the packaged app with no Python on its PATH and drives it over HTTP."""

    @classmethod
    def setUpClass(cls):
        cls.exe = os.path.abspath(os.environ["PDF_CLEANER_TEST_EXE"])
        cls.tmp = tempfile.mkdtemp(prefix="pdfcleaner-exe-")
        cls.data = os.path.join(cls.tmp, "data")
        cls.root = os.path.join(cls.tmp, "PDFs")
        make_demo_folder(cls.root)
        shutil.copy(os.path.join(cls.root, "Recipe - Banana Bread.pdf"), os.path.join(cls.root, "Stories", "copy.pdf"))
        cls.port = free_port()
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith("PYTHON")}
        env["PATH"] = os.path.join(os.environ.get("SystemRoot", "C:\\Windows"), "System32")
        env["PDF_CLEANER_DATA_DIR"] = cls.data
        cls.proc = subprocess.Popen([cls.exe, "--no-browser", "--port", str(cls.port)], env=env, cwd=cls.tmp,
                                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 60
        while True:
            try:
                cls.fetch_token()
                break
            except (OSError, AttributeError):
                if time.monotonic() > deadline or cls.proc.poll() is not None:
                    cls.proc.kill()
                    raise RuntimeError("PDFCleaner.exe did not start: " + cls.proc.stdout.read().decode("utf-8", "replace"))
                time.sleep(0.3)

    @classmethod
    def tearDownClass(cls):
        cls.proc.kill()
        cls.proc.wait(10)
        cls.proc.stdout.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_exe_version_flag(self):
        out = subprocess.run([self.exe, "--version"], capture_output=True, timeout=60)
        self.assertIn(b"PDF Cleaner 1.1.0", out.stdout + out.stderr)

    def test_exe_serves_the_app_and_finds_duplicates(self):
        status, data = self.api("/api/state")
        self.assertEqual((status, data["version"]), (200, "1.1.0"))
        for name in ("app.js", "app.css", "theme.js", "icon.svg"):
            self.assertEqual(self.request("GET", f"/static/{name}")[0], 200, name)
        status, data = self.api("/api/scan", {"folder": self.root, "recursive": True})
        self.assertEqual((status, len(data["files"])), (200, 11))
        f = data["files"][0]
        status, body = self.request("GET", f"/api/file/{f['id']}/x.pdf?t={self.token}")
        self.assertTrue(body.startswith(b"%PDF"))
        self.api("/api/duplicates/start")
        for _ in range(200):
            _, s = self.api("/api/duplicates/status")
            if s["state"] != "running":
                break
            time.sleep(0.05)
        self.assertEqual((s["state"], len(s["groups"]), len(s["groups"][0]["ids"])), ("done", 1, 2))
        self.assertTrue(os.path.exists(os.path.join(self.data, "progress.json")),
                        "progress is saved in the user data folder")

    def test_exe_only_one_copy_at_a_time(self):
        env = dict(os.environ, PDF_CLEANER_DATA_DIR=self.data)
        out = subprocess.run([self.exe, "--no-browser", "--port", str(free_port())], env=env,
                             input=b"\n", capture_output=True, timeout=60)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn(b"already running", out.stdout + out.stderr)


if __name__ == "__main__":
    unittest.main()
