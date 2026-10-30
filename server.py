#!/usr/bin/env python3
"""
PDF Cleaner - a private, local tool for reviewing PDFs one at a time.

Safety model:
  * Listens on 127.0.0.1 only and makes no outgoing network requests.
  * Uses only the Python standard library.
  * Opens PDFs read-only and never changes them.
  * Removes a file only by moving it to the Windows Recycle Bin, only after the
    browser sends an explicit confirmation, and only for .pdf files inside the
    folder that was scanned. Folders and other file types are never touched.
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import html
import json
import os
import re
import secrets
import stat
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

APP_NAME = "PDF Cleaner"
HOST = "127.0.0.1"
DEFAULT_PORT = 8765

APP_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(APP_DIR, "static")
DATA_DIR = os.path.join(APP_DIR, "data")
PROGRESS_FILE = os.path.join(DATA_DIR, "progress.json")

# A fresh secret per launch. The page receives it; other websites cannot read it,
# so they cannot send requests to this server.
TOKEN = secrets.token_urlsafe(32)

MAX_BODY_BYTES = 256 * 1024
MAX_FILES = 25_000
MAX_PAGECOUNT_BYTES = 512 * 1024 * 1024
DECISIONS = {"keep", "skip"}
DELETE_CONFIRM_PHRASE = "move-to-recycle-bin"

SKIP_DIR_NAMES = {"$recycle.bin", "system volume information", "$windows.~bt", "$windows.~ws"}
REPARSE_TAG_MOUNT_POINT = 0xA0000003  # directory junction
REPARSE_TAG_SYMLINK = 0xA000000C
FILE_ATTRIBUTE_OFFLINE = 0x1000
FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x40000
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x400000
CLOUD_ATTRS = FILE_ATTRIBUTE_OFFLINE | FILE_ATTRIBUTE_RECALL_ON_OPEN | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS

STATIC_FILES = {
    "app.css": "text/css; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
    "theme.js": "text/javascript; charset=utf-8",
    "icon.svg": "image/svg+xml",
}

INDEX_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; frame-src 'self'; object-src 'none'; base-uri 'none'; "
    "form-action 'none'; frame-ancestors 'none'"
)


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


# --------------------------------------------------------------------------- paths

def _norm(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))


def is_within(path: str, root: str) -> bool:
    """True if `path` is `root` or inside it (case-insensitive on Windows)."""
    try:
        return os.path.commonpath([_norm(path), _norm(root)]) == _norm(root)
    except ValueError:  # different drives
        return False


def is_link_like(st: os.stat_result) -> bool:
    """Symlinks and directory junctions (but not OneDrive cloud placeholders)."""
    if stat.S_ISLNK(st.st_mode):
        return True
    return getattr(st, "st_reparse_tag", 0) in (REPARSE_TAG_MOUNT_POINT, REPARSE_TAG_SYMLINK)


def is_cloud_only(st: os.stat_result) -> bool:
    return bool(getattr(st, "st_file_attributes", 0) & CLOUD_ATTRS)


def file_id(rel: str) -> str:
    return hashlib.sha1(os.path.normcase(rel).encode("utf-8", "surrogatepass")).hexdigest()[:16]


# --------------------------------------------------------------------------- saved progress

class ProgressStore:
    """Review decisions saved to data/progress.json, keyed by folder then relative path."""

    def __init__(self, path: str):
        self.path = path
        self.lock = threading.RLock()
        self.data = {"version": 1, "settings": {}, "folders": {}}
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                self.data["settings"] = loaded.get("settings") or {}
                self.data["folders"] = loaded.get("folders") or {}
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as exc:
            backup = f"{self.path}.unreadable-{int(time.time())}"
            try:
                os.replace(self.path, backup)
            except OSError:
                pass
            print(f"Warning: saved progress could not be read ({exc}). A copy was kept at {backup}",
                  file=sys.stderr)

    def save(self) -> None:
        with self.lock:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)  # atomic: never leaves a half-written file

    def records(self, root: str) -> dict:
        with self.lock:
            folder = self.data["folders"].setdefault(os.path.normcase(root), {"path": root, "files": {}})
            folder["path"] = root
            return folder.setdefault("files", {})


class ScanSession:
    """The most recent scan. File operations are only allowed on files listed here."""

    def __init__(self):
        self.lock = threading.Lock()
        self.root: str | None = None
        self.files: dict[str, dict] = {}


STORE = ProgressStore(PROGRESS_FILE)
SESSION = ScanSession()
PAGE_CACHE: dict = {}
PAGE_CACHE_LOCK = threading.Lock()


def resolve_id(fid) -> tuple[str, dict]:
    """Map a file id from the last scan back to a path, re-checking every safety rule."""
    if not isinstance(fid, str) or not re.fullmatch(r"[0-9a-f]{16}", fid):
        raise ApiError(400, "Invalid file id.")
    with SESSION.lock:
        root = SESSION.root
        entry = SESSION.files.get(fid)
    if not root or not entry:
        raise ApiError(404, "This file isn't in the current scan. Try scanning the folder again.")
    path = entry["path"]
    if not path.lower().endswith(".pdf"):
        raise ApiError(400, "Only PDF files can be handled.")
    if not is_within(os.path.realpath(path), root):
        raise ApiError(403, "This file is outside the selected folder.")
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        raise ApiError(410, "This file no longer exists. It may have been moved or deleted outside PDF Cleaner.")
    except OSError as exc:
        raise ApiError(500, f"This file can't be read: {exc.strerror or exc}")
    if not stat.S_ISREG(st.st_mode) or is_link_like(st):
        raise ApiError(400, "Only regular PDF files can be handled.")
    return path, entry


# --------------------------------------------------------------------------- scanning

def validate_folder(folder) -> str:
    if not isinstance(folder, str) or not folder.strip():
        raise ApiError(400, "Please choose a folder first.")
    folder = os.path.expandvars(os.path.expanduser(folder.strip().strip('"')))
    if not os.path.isabs(folder):
        raise ApiError(400, "Please enter a full folder path, for example C:\\Users\\you\\Documents.")
    if not os.path.isdir(folder):
        raise ApiError(404, "That folder doesn't exist or can't be opened.")
    return os.path.realpath(folder)


def scan_folder(root: str, recursive: bool) -> tuple[list[dict], bool]:
    found: list[dict] = []
    truncated = False

    def add(dirpath: str, name: str) -> None:
        nonlocal truncated
        if not name.lower().endswith(".pdf"):
            return
        if len(found) >= MAX_FILES:
            truncated = True
            return
        full = os.path.join(dirpath, name)
        try:
            st = os.lstat(full)
        except OSError:
            return
        if not stat.S_ISREG(st.st_mode) or is_link_like(st):
            return
        if not is_within(os.path.realpath(full), root):
            return
        rel = os.path.relpath(full, root)
        found.append({
            "id": file_id(rel),
            "name": name,
            "rel": rel,
            "folder": os.path.dirname(rel) or ".",
            "size": st.st_size,
            "mtime": st.st_mtime,
            "cloud": is_cloud_only(st),
        })

    if recursive:
        for dirpath, dirnames, filenames in os.walk(root, onerror=lambda _e: None):
            keep = []
            for d in dirnames:
                if d.lower() in SKIP_DIR_NAMES:
                    continue
                try:
                    st = os.lstat(os.path.join(dirpath, d))
                except OSError:
                    continue
                if not is_link_like(st):  # never follow links/junctions out of the folder
                    keep.append(d)
            dirnames[:] = sorted(keep)
            for name in filenames:
                add(dirpath, name)
            if truncated:
                break
    else:
        try:
            names = os.listdir(root)
        except OSError as exc:
            raise ApiError(403, f"Can't read that folder: {exc.strerror or exc}")
        for name in names:
            add(root, name)
    return found, truncated


# --------------------------------------------------------------------------- page counting (read-only)

RE_TYPE_PAGES = re.compile(rb"/Type\s*/Pages(?![A-Za-z0-9])")
RE_TYPE_PAGE = re.compile(rb"/Type\s*/Page(?![A-Za-z0-9])")
RE_COUNT = re.compile(rb"/Count\s+(\d{1,7})")
RE_OBJSTM = re.compile(rb"/Type\s*/ObjStm(?![A-Za-z0-9])")


MAX_PAGES_DICTS = 2000  # cap work on crafted files with huge numbers of page-tree nodes
MAX_DECOMPRESSED_BYTES = 128 * 1024 * 1024


def _enclosing_dict(buf: bytes, pos: int):
    """Return the bytes of the << ... >> dictionary that contains position `pos`."""
    lo, i, depth, start = max(0, pos - 8192), pos, 0, None
    while True:  # walk backwards to the opening "<<"
        a = buf.rfind(b"<<", lo, i)
        b = buf.rfind(b">>", lo, i)
        if a < 0:
            return None
        if b > a:
            depth += 1
            i = b
        elif depth == 0:
            start = a
            break
        else:
            depth -= 1
            i = a
    hi, j, depth = min(len(buf), start + 2_000_000), start, 0
    while True:  # walk forwards to the matching ">>"
        a = buf.find(b"<<", j, hi)
        b = buf.find(b">>", j, hi)
        if b < 0:
            return None
        if 0 <= a < b:
            depth += 1
            j = a + 2
        else:
            depth -= 1
            j = b + 2
            if depth == 0:
                return buf[start:j]


def _pages_from(buf: bytes):
    best = None
    for k, m in enumerate(RE_TYPE_PAGES.finditer(buf)):
        if k >= MAX_PAGES_DICTS:
            break
        d = _enclosing_dict(buf, m.start())
        if not d:
            continue
        for c in RE_COUNT.finditer(d):
            n = int(c.group(1))
            best = n if best is None else max(best, n)
    return best


def count_pdf_pages(path: str):
    """Best-effort page count. Reads the file only; returns None if unsure."""
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError:
        return None
    if b"%PDF-" not in data[:1024]:
        return None
    n = _pages_from(data)
    extra = []
    if not n:
        # Modern PDFs often keep the page tree inside compressed "object streams".
        view = memoryview(data)
        budget = MAX_DECOMPRESSED_BYTES  # total, so a crafted file can't make us inflate gigabytes
        for idx, m in enumerate(RE_OBJSTM.finditer(data)):
            if idx >= MAX_PAGES_DICTS or budget <= 0:
                break
            s = data.find(b"stream", m.end(), m.end() + 4096)
            if s < 0:
                continue
            s += 6
            if data[s:s + 2] == b"\r\n":
                s += 2
            elif data[s:s + 1] in (b"\n", b"\r"):
                s += 1
            try:
                out = zlib.decompressobj().decompress(view[s:s + budget], budget)
            except zlib.error:
                continue
            budget -= len(out)
            extra.append(out)
            found = _pages_from(out)
            if found:
                n = max(n or 0, found)
    if not n:
        total = len(RE_TYPE_PAGE.findall(data)) + sum(len(RE_TYPE_PAGE.findall(b)) for b in extra)
        n = total or None
    return n


def page_count_cached(path: str, allow_cloud: bool):
    try:
        st = os.stat(path)
    except OSError:
        return None
    if is_cloud_only(st) and not allow_cloud:
        return None  # don't force OneDrive to download every file just for a page count
    key = (_norm(path), st.st_size, st.st_mtime_ns)
    with PAGE_CACHE_LOCK:
        if key in PAGE_CACHE:
            return PAGE_CACHE[key]
    n = count_pdf_pages(path) if st.st_size <= MAX_PAGECOUNT_BYTES else None
    with PAGE_CACHE_LOCK:
        PAGE_CACHE[key] = n
    return n


# --------------------------------------------------------------------------- Recycle Bin

def move_to_recycle_bin(path: str) -> None:
    """Move one file to the Recycle Bin. Refuses anywhere a recycle could become a permanent delete."""
    if os.name != "nt":
        raise ApiError(501, "Moving files to the Recycle Bin is only supported on Windows.")
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _pack_ = 1 if ctypes.sizeof(ctypes.c_void_p) == 4 else 8
        _fields_ = [
            ("hwnd", wintypes.HWND),
            ("wFunc", wintypes.UINT),
            ("pFrom", ctypes.c_wchar_p),
            ("pTo", ctypes.c_wchar_p),
            ("fFlags", ctypes.c_ushort),
            ("fAnyOperationsAborted", wintypes.BOOL),
            ("hNameMappings", ctypes.c_void_p),
            ("lpszProgressTitle", ctypes.c_wchar_p),
        ]

    FO_DELETE = 0x0003
    FOF_SILENT = 0x0004
    FOF_NOCONFIRMATION = 0x0010
    FOF_ALLOWUNDO = 0x0040            # = send to Recycle Bin instead of deleting
    FOF_WANTNUKEWARNING = 0x4000      # Windows asks before any permanent delete (e.g. file too big)
    DRIVE_FIXED = 3

    drive = os.path.splitdrive(path)[0]
    if not re.fullmatch(r"[A-Za-z]:", drive or ""):
        raise ApiError(400, "Only files on a local drive (like C:) can go to the Recycle Bin. "
                            "Network locations aren't supported, so the file was left in place.")
    if ctypes.windll.kernel32.GetDriveTypeW(drive + "\\") != DRIVE_FIXED:
        raise ApiError(400, "This file is on a removable or network drive, which has no Recycle Bin. "
                            "PDF Cleaner left it in place to avoid a permanent delete.")

    buf = ctypes.create_unicode_buffer(path + "\0")  # list must end with a double null
    op = SHFILEOPSTRUCTW()
    op.hwnd = None
    op.wFunc = FO_DELETE
    op.pFrom = ctypes.cast(buf, ctypes.c_wchar_p)
    op.pTo = None
    op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_WANTNUKEWARNING

    shfileop = ctypes.windll.shell32.SHFileOperationW
    shfileop.argtypes = [ctypes.POINTER(SHFILEOPSTRUCTW)]
    shfileop.restype = ctypes.c_int
    rc = shfileop(ctypes.byref(op))
    if op.fAnyOperationsAborted:
        raise ApiError(409, "Cancelled. The file couldn't go to the Recycle Bin, so it was left in place.")
    if rc != 0:
        raise ApiError(500, f"Windows couldn't move the file to the Recycle Bin (error {rc:#x}). "
                            "It may be open in another program or read-only.")


# --------------------------------------------------------------------------- folder picker

PICKER_SCRIPT = r"""
import sys, tkinter as tk
from tkinter import filedialog
root = tk.Tk()
root.withdraw()
root.attributes("-topmost", True)
initial = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] else None
path = filedialog.askdirectory(parent=root, title="Choose a folder to scan for PDFs",
                               initialdir=initial, mustexist=True)
root.destroy()
sys.stdout.write(path or "")
"""
PICKER_LOCK = threading.Lock()


# --------------------------------------------------------------------------- API

def api_state(_body):
    with STORE.lock:
        return {"settings": dict(STORE.data["settings"]), "app": APP_NAME}


def api_pick_folder(body):
    initial = body.get("initial") if isinstance(body.get("initial"), str) else ""
    if initial and not os.path.isdir(initial):
        initial = ""
    if not PICKER_LOCK.acquire(blocking=False):
        raise ApiError(409, "The folder picker is already open. Check your taskbar.")
    try:
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        exe = sys.executable
        pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.exists(pyw):
            exe = pyw
        proc = subprocess.run([exe, "-I", "-c", PICKER_SCRIPT, initial], capture_output=True,
                              env=env, timeout=900, creationflags=flags)
        chosen = proc.stdout.decode("utf-8", "replace").strip()
    except subprocess.TimeoutExpired:
        raise ApiError(408, "The folder picker timed out.")
    finally:
        PICKER_LOCK.release()
    return {"folder": os.path.normpath(chosen) if chosen else None}


def api_scan(body):
    root = validate_folder(body.get("folder"))
    recursive = bool(body.get("recursive", True))
    found, truncated = scan_folder(root, recursive)
    with STORE.lock:
        records = STORE.records(root)
        present = set()
        for f in found:
            key = os.path.normcase(f["rel"])
            present.add(key)
            rec = records.get(key)
            status = rec.get("status") if rec else None
            if status == "deleted":  # the file is back, e.g. restored from the Recycle Bin
                records.pop(key, None)
                status = None
            f["status"] = status if status in DECISIONS else None
            if rec and status in DECISIONS:
                f["decidedAt"] = rec.get("at")
        ghosts = []
        for key, rec in records.items():
            if rec.get("status") != "deleted" or key in present:
                continue
            rel = rec.get("rel") or key
            if not recursive and os.path.dirname(rel):
                continue
            ghosts.append({
                "id": file_id(rel), "name": os.path.basename(rel), "rel": rel,
                "folder": os.path.dirname(rel) or ".", "size": rec.get("size"),
                "mtime": rec.get("mtime"), "pages": rec.get("pages"),
                "status": "deleted", "ghost": True, "decidedAt": rec.get("at"),
            })
        STORE.data["settings"].update({"lastFolder": root, "recursive": recursive})
    STORE.save()
    with SESSION.lock:
        SESSION.root = root
        SESSION.files = {f["id"]: {"path": os.path.join(root, f["rel"]), "rel": f["rel"]} for f in found}
    return {"root": root, "recursive": recursive, "files": found + ghosts,
            "truncated": truncated, "limit": MAX_FILES}


def api_pages(body):
    ids = body.get("ids")
    if not isinstance(ids, list) or len(ids) > 64:
        raise ApiError(400, "Invalid request.")
    force = bool(body.get("force"))
    out = {}
    for fid in ids:
        try:
            path, _entry = resolve_id(fid)
        except ApiError:
            continue
        out[fid] = page_count_cached(path, allow_cloud=force)
    return {"pages": out}


def api_status(body):
    status = body.get("status")
    if status is not None and status not in DECISIONS:
        raise ApiError(400, "Unknown decision.")
    path, entry = resolve_id(body.get("id"))
    with SESSION.lock:
        root = SESSION.root
    with STORE.lock:
        records = STORE.records(root)
        key = os.path.normcase(entry["rel"])
        if status is None:
            records.pop(key, None)
        else:
            records[key] = {"rel": entry["rel"], "status": status, "at": time.time()}
    STORE.save()
    return {"status": status}


def api_delete(body):
    """'Delete' in the UI. Moves the PDF to the Windows Recycle Bin; nothing is erased."""
    if body.get("confirm") != DELETE_CONFIRM_PHRASE:
        raise ApiError(400, "Deletion needs confirmation.")
    path, entry = resolve_id(body.get("id"))
    with SESSION.lock:
        root = SESSION.root
    st = os.stat(path)
    pages = page_count_cached(path, allow_cloud=False)
    move_to_recycle_bin(path)
    if os.path.lexists(path):
        raise ApiError(500, "Windows reported success but the file is still there. Nothing else was changed.")
    with STORE.lock:
        STORE.records(root)[os.path.normcase(entry["rel"])] = {
            "rel": entry["rel"], "status": "deleted", "at": time.time(),
            "size": st.st_size, "mtime": st.st_mtime, "pages": pages,
        }
    STORE.save()
    with SESSION.lock:
        SESSION.files.pop(body.get("id"), None)
    return {"status": "deleted"}


ROUTES = {
    "/api/state": api_state,
    "/api/pick-folder": api_pick_folder,
    "/api/scan": api_scan,
    "/api/pages": api_pages,
    "/api/status": api_status,
    "/api/delete": api_delete,
}


# --------------------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "PDFCleaner/1.0"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):  # keep file names out of the console
        pass

    # -- checks
    def _port(self) -> int:
        return self.server.server_address[1]

    def _host_ok(self) -> bool:
        p = self._port()
        return self.headers.get("Host", "") in {f"127.0.0.1:{p}", f"localhost:{p}"}

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        p = self._port()
        return origin is None or origin in {f"http://127.0.0.1:{p}", f"http://localhost:{p}"}

    @staticmethod
    def _token_ok(value: str) -> bool:
        return secrets.compare_digest(value.encode("utf-8", "replace"), TOKEN.encode())

    # -- responses
    def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, obj: dict) -> None:
        self._send(status, json.dumps(obj).encode("utf-8"), "application/json; charset=utf-8")

    def _message_page(self, status: int, message: str) -> None:
        page = (
            "<!doctype html><meta charset=utf-8><meta name=color-scheme content='light dark'>"
            "<body style=\"font:15px 'Segoe UI',system-ui,sans-serif;display:grid;place-items:center;"
            "height:90vh;margin:0;padding:16px;text-align:center\"><p>" + html.escape(message) + "</p>"
        )
        self._send(status, page.encode("utf-8"), "text/html; charset=utf-8",
                   {"X-Frame-Options": "SAMEORIGIN"})

    # -- GET: page, static files, PDF bytes
    def do_GET(self):
        try:
            if not self._host_ok():
                return self._send(403, b"Forbidden", "text/plain; charset=utf-8")
            url = urlparse(self.path)
            if url.path in ("/", "/index.html"):
                return self._index()
            if url.path.startswith("/static/"):
                return self._static(url.path[len("/static/"):])
            if url.path == "/api/file" or url.path.startswith("/api/file/"):
                # /api/file/<id>/<name>.pdf?t=... (the name is only there so the viewer shows it)
                query = parse_qs(url.query)
                parts = url.path.split("/")
                if len(parts) >= 4:
                    query["id"] = [parts[3]]
                return self._pdf(query)
            return self._send(404, b"Not found", "text/plain; charset=utf-8")
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    do_HEAD = do_GET

    def _index(self):
        with open(os.path.join(STATIC_DIR, "index.html"), "r", encoding="utf-8") as fh:
            page = fh.read().replace("{{TOKEN}}", TOKEN)
        self._send(200, page.encode("utf-8"), "text/html; charset=utf-8",
                   {"Content-Security-Policy": INDEX_CSP, "X-Frame-Options": "DENY"})

    def _static(self, name: str):
        ctype = STATIC_FILES.get(name)  # fixed allow-list, so no path tricks
        if not ctype:
            return self._send(404, b"Not found", "text/plain; charset=utf-8")
        with open(os.path.join(STATIC_DIR, name), "rb") as fh:
            body = fh.read()
        self._send(200, body, ctype)

    def _pdf(self, query: dict):
        if not self._token_ok(query.get("t", [""])[0]):
            return self._message_page(403, "This link has expired. Reload PDF Cleaner.")
        try:
            path, _entry = resolve_id(query.get("id", [""])[0])
        except ApiError as exc:
            return self._message_page(exc.status, exc.message)
        try:
            fh = open(path, "rb")  # read-only
        except OSError as exc:
            return self._message_page(500, f"This PDF couldn't be opened: {exc.strerror or exc}")
        with fh:
            size = os.fstat(fh.fileno()).st_size
            name = os.path.basename(path)
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition", f"inline; filename*=UTF-8''{quote(name)}")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "SAMEORIGIN")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command == "HEAD":
                return
            while True:
                chunk = fh.read(1 << 20)
                if not chunk:
                    break
                self.wfile.write(chunk)

    # -- POST: JSON API
    def do_POST(self):
        try:
            if not self._host_ok() or not self._origin_ok():
                return self._json(403, {"ok": False, "error": "Forbidden."})
            if not self._token_ok(self.headers.get("X-Token", "")):
                return self._json(403, {"ok": False, "error": "PDF Cleaner was restarted. Please reload the page."})
            route = ROUTES.get(urlparse(self.path).path)
            if not route:
                return self._json(404, {"ok": False, "error": "Not found."})
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if length < 0 or length > MAX_BODY_BYTES:
                    # Discard a modest excess so the client can read our reply, then close the
                    # connection so leftover bytes are never parsed as a new request.
                    remaining = min(max(length, 0), 8 * 1024 * 1024)
                    while remaining > 0:
                        chunk = self.rfile.read(min(remaining, 65536))
                        if not chunk:
                            break
                        remaining -= len(chunk)
                    self.close_connection = True
                    raise ApiError(413, "Request too large.")
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    body = json.loads(raw.decode("utf-8") or "{}")
                except ValueError:
                    raise ApiError(400, "Invalid request.")
                if not isinstance(body, dict):
                    raise ApiError(400, "Invalid request.")
                result = route(body)
                result.setdefault("ok", True)
                self._json(200, result)
            except ApiError as exc:
                self._json(exc.status, {"ok": False, "error": exc.message})
            except OSError as exc:
                self._json(500, {"ok": False, "error": f"File error: {exc.strerror or exc}"})
            except Exception:
                traceback.print_exc()
                self._json(500, {"ok": False, "error": "Unexpected error. See the PDF Cleaner window for details."})
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass


def acquire_instance_lock(data_dir: str):
    """Allow one running copy per folder, so two copies can't overwrite each other's progress.

    Returns an open file handle that holds the lock (keep it open), or None if another
    copy already holds it. Windows releases the lock automatically when the process exits.
    """
    os.makedirs(data_dir, exist_ok=True)
    fh = open(os.path.join(data_dir, "instance.lock"), "a+")
    try:
        if os.name == "nt":
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


class LocalServer(ThreadingHTTPServer):
    allow_reuse_address = False  # on Windows, reuse would let two servers share a port
    daemon_threads = True


def main() -> None:
    parser = argparse.ArgumentParser(description="PDF Cleaner - review local PDFs privately.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="port to use (default 8765)")
    parser.add_argument("--no-browser", action="store_true", help="don't open the browser automatically")
    args = parser.parse_args()

    try:
        lock = acquire_instance_lock(DATA_DIR)  # noqa: F841 (held open until exit)
    except OSError as exc:
        print(f"\n  PDF Cleaner can't save its progress in this folder ({exc.strerror or exc}).\n"
              "  Move the whole PDF Cleaner folder somewhere you can write to, such as\n"
              "  Documents or the Desktop, and start it again.\n", file=sys.stderr)
        sys.exit(1)
    if lock is None:
        print("\n  PDF Cleaner is already running from this folder.\n"
              "  Use the browser tab that's already open, or close the other PDF Cleaner\n"
              "  window first. (Two copies would overwrite each other's saved progress.)\n",
              file=sys.stderr)
        sys.exit(1)

    server = None
    for port in range(args.port, args.port + 20):
        try:
            server = LocalServer((HOST, port), Handler)
            break
        except OSError:
            continue
    if server is None:
        print(f"Could not find a free port near {args.port}.", file=sys.stderr)
        sys.exit(1)

    url = f"http://{HOST}:{server.server_address[1]}/"
    print(f"\n  {APP_NAME} is running at {url}")
    print("  It only accepts connections from this computer.")
    print("  Keep this window open while you use it. Press Ctrl+C to stop.\n")
    if not args.no_browser:
        threading.Timer(0.7, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
