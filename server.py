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
  * Batch removal of duplicates always keeps at least one copy of every group,
    and re-checks each file's contents just before it is recycled.
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
__version__ = "1.1.0"
HOST = "127.0.0.1"
DEFAULT_PORT = 8765

FROZEN = bool(getattr(sys, "frozen", False))  # running as the packaged PDFCleaner.exe


def app_dir() -> str:
    """Where the program's own files live (read-only). Inside the .exe, PyInstaller unpacks them to _MEIPASS."""
    if FROZEN and hasattr(sys, "_MEIPASS"):
        return sys._MEIPASS
    return os.path.dirname(os.path.abspath(__file__))


def user_data_dir() -> str:
    """Where saved progress lives: %LOCALAPPDATA%\\PDF Cleaner on Windows.

    PDF_CLEANER_DATA_DIR overrides it (used by the tests, and for portable setups).
    """
    override = os.environ.get("PDF_CLEANER_DATA_DIR")
    if override:
        return os.path.abspath(override)
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, APP_NAME)


APP_DIR = app_dir()
STATIC_DIR = os.path.join(APP_DIR, "static")
DATA_DIR = user_data_dir()
PROGRESS_FILE = os.path.join(DATA_DIR, "progress.json")
# v1.0 kept progress in data/ next to server.py. It is copied (never moved) to DATA_DIR on first
# start. The .exe looks for it next to itself, in case it was put in the old PDF Cleaner folder.
LEGACY_PROGRESS_FILE = os.path.join(os.path.dirname(sys.executable) if FROZEN else APP_DIR, "data", "progress.json")

# A fresh secret per launch. The page receives it; other websites cannot read it,
# so they cannot send requests to this server.
TOKEN = secrets.token_urlsafe(32)

MAX_BODY_BYTES = 256 * 1024
MAX_FILES = 25_000
MAX_PAGECOUNT_BYTES = 512 * 1024 * 1024
DECISIONS = {"keep", "skip"}
DELETE_CONFIRM_PHRASE = "move-to-recycle-bin"
BATCH_DELETE_CONFIRM_PHRASE = "move-duplicates-to-recycle-bin"
MAX_BATCH_DELETE = 5_000
HASH_CHUNK = 1 << 20

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


def migrate_legacy_progress(legacy: str | None, target: str) -> bool:
    """Copy v1.0's data/progress.json to the user data folder, once. The original is left in place."""
    if not legacy or os.path.exists(target) or not os.path.isfile(legacy):
        return False
    os.makedirs(os.path.dirname(target), exist_ok=True)
    tmp = target + ".tmp"
    with open(legacy, "rb") as src, open(tmp, "wb") as dst:
        dst.write(src.read())
    os.replace(tmp, target)
    return True


class ScanSession:
    """The most recent scan. File operations are only allowed on files listed here."""

    def __init__(self):
        self.lock = threading.Lock()
        self.root: str | None = None
        self.files: dict[str, dict] = {}
        self.generation = 0  # bumps on every scan, so stale duplicate results are never acted on


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


def recycle_scanned_file(fid: str, path: str, entry: dict, root: str, save: bool = True) -> dict:
    """Recycle one file from the current scan and remember it as 'deleted'. Raises ApiError on failure."""
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
    if save:
        STORE.save()
    with SESSION.lock:
        SESSION.files.pop(fid, None)
    return {"id": fid, "rel": entry["rel"]}


# --------------------------------------------------------------------------- duplicate finder (read-only)

HASH_CACHE: dict = {}
HASH_CACHE_LOCK = threading.Lock()


class Cancelled(Exception):
    pass


def sha256_file(path: str, cancel: threading.Event | None = None, on_bytes=None) -> str:
    """SHA-256 of a file's contents, read in chunks. Opens the file read-only."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            chunk = fh.read(HASH_CHUNK)
            if not chunk:
                break
            h.update(chunk)
            if on_bytes:
                on_bytes(len(chunk))
    return h.hexdigest()


def sha256_cached(path: str, st: os.stat_result, cancel=None, on_bytes=None) -> str:
    key = (_norm(path), st.st_size, st.st_mtime_ns)
    with HASH_CACHE_LOCK:
        if key in HASH_CACHE:
            if on_bytes:
                on_bytes(st.st_size)
            return HASH_CACHE[key]
    digest = sha256_file(path, cancel, on_bytes)
    with HASH_CACHE_LOCK:
        HASH_CACHE[key] = digest
    return digest


class DuplicateJob:
    """One background duplicate search at a time, over the files of the current scan."""

    def __init__(self):
        self.lock = threading.Lock()
        self.cancel = threading.Event()
        self.thread: threading.Thread | None = None
        self.reset()

    def reset(self) -> None:
        self.state = "idle"          # idle | running | done | cancelled | error
        self.generation = -1
        self.root: str | None = None
        self.phase = ""
        self.files_done = self.files_total = 0
        self.bytes_done = self.bytes_total = 0
        self.scanned = 0
        self.skipped_cloud = 0
        self.unreadable = 0
        self.groups: list[dict] = []
        self.rels: dict[str, str] = {}
        self.error = ""

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "state": self.state, "phase": self.phase, "generation": self.generation,
                "filesDone": self.files_done, "filesTotal": self.files_total,
                "bytesDone": self.bytes_done, "bytesTotal": self.bytes_total,
                "scanned": self.scanned, "skippedCloud": self.skipped_cloud, "unreadable": self.unreadable,
                "groups": [dict(g, ids=list(g["ids"])) for g in self.groups] if self.state == "done" else [],
                "error": self.error,
            }


DUP_JOB = DuplicateJob()


def find_duplicate_groups(entries: dict, job: DuplicateJob | None = None) -> list[dict]:
    """Group files with identical contents.

    Step 1: group by exact size (cheap; a file with a unique size can't have a duplicate).
    Step 2: SHA-256 only the files that share a size, and group by hash.
    Cloud-only (OneDrive online-only) files are skipped, so nothing is downloaded.
    """
    cancel = job.cancel if job else None
    by_size: dict[int, list] = {}
    scanned = skipped_cloud = unreadable = 0
    for fid, entry in entries.items():
        path = entry["path"]
        try:
            st = os.lstat(path)
        except OSError:
            unreadable += 1
            continue
        if not stat.S_ISREG(st.st_mode) or is_link_like(st):
            continue
        if is_cloud_only(st):
            skipped_cloud += 1
            continue
        scanned += 1
        by_size.setdefault(st.st_size, []).append((fid, path, st))

    candidates = [c for same in by_size.values() if len(same) > 1 for c in same]
    if job:
        with job.lock:
            job.phase = "hashing"
            job.scanned, job.skipped_cloud = scanned, skipped_cloud
            job.files_total = len(candidates)
            job.bytes_total = sum(c[2].st_size for c in candidates)

    def on_bytes(n: int) -> None:
        if job:
            with job.lock:
                job.bytes_done += n

    by_hash: dict[tuple, list] = {}
    for fid, path, st in candidates:
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        try:
            digest = sha256_cached(path, st, cancel, on_bytes)
        except OSError:
            unreadable += 1
            digest = None
        if job:
            with job.lock:
                job.files_done += 1
                job.unreadable = unreadable
        if digest:
            by_hash.setdefault((st.st_size, digest), []).append(fid)

    groups = [{"size": size, "sha256": digest, "ids": sorted(ids, key=lambda i: entries[i]["rel"].lower())}
              for (size, digest), ids in by_hash.items() if len(ids) > 1]
    groups.sort(key=lambda g: (-(g["size"] * (len(g["ids"]) - 1)), entries[g["ids"][0]]["rel"].lower()))
    if job:
        with job.lock:
            job.unreadable = unreadable
    return groups


def _run_duplicate_job(entries: dict) -> None:
    job = DUP_JOB
    try:
        groups = find_duplicate_groups(entries, job)
        with job.lock:
            job.groups = groups
            job.state = "done"
            job.phase = ""
    except Cancelled:
        with job.lock:
            job.state = "cancelled"
    except Exception as exc:  # report, never crash the server
        traceback.print_exc()
        with job.lock:
            job.state = "error"
            job.error = f"Finding duplicates failed: {exc}"


# --------------------------------------------------------------------------- show in File Explorer

def reveal_in_explorer(path: str) -> None:
    """Open File Explorer at the file's folder with the file selected. Never opens or runs the file."""
    if os.name != "nt":
        raise ApiError(501, "Showing files in File Explorer is only supported on Windows.")
    try:
        ole32, shell32 = ctypes.windll.ole32, ctypes.windll.shell32
        shell32.ILCreateFromPathW.argtypes = [ctypes.c_wchar_p]
        shell32.ILCreateFromPathW.restype = ctypes.c_void_p
        shell32.SHOpenFolderAndSelectItems.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p,
                                                       ctypes.c_ulong]
        shell32.SHOpenFolderAndSelectItems.restype = ctypes.c_long
        shell32.ILFree.argtypes = [ctypes.c_void_p]
        ole32.CoInitialize(None)
        try:
            pidl = shell32.ILCreateFromPathW(path)
            if pidl:
                try:
                    if shell32.SHOpenFolderAndSelectItems(pidl, 0, None, 0) == 0:
                        return
                finally:
                    shell32.ILFree(pidl)
        finally:
            ole32.CoUninitialize()
    except (OSError, AttributeError):
        pass
    # Fallback. Windows paths can't contain a double quote, so quoting is safe here.
    subprocess.Popen(f'explorer.exe /select,"{path}"', creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


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


def run_folder_picker(initial: str) -> None:
    """Child-process entry point inside the .exe (`PDFCleaner.exe --pick-folder <initial>`).

    Same dialog as PICKER_SCRIPT. It runs in its own process so Tk never shares the server's threads.
    """
    import tkinter as tk
    from tkinter import filedialog
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    path = filedialog.askdirectory(parent=root, title="Choose a folder to scan for PDFs",
                                   initialdir=initial or None, mustexist=True)
    root.destroy()
    sys.stdout.buffer.write((path or "").encode("utf-8"))
    sys.stdout.flush()


def picker_command(initial: str) -> list[str]:
    if FROZEN:
        return [sys.executable, "--pick-folder", initial]
    exe = sys.executable
    pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.exists(pyw):
        exe = pyw
    return [exe, "-I", "-c", PICKER_SCRIPT, initial]


# --------------------------------------------------------------------------- API

def api_state(_body):
    with STORE.lock:
        return {"settings": dict(STORE.data["settings"]), "app": APP_NAME, "version": __version__}


def api_pick_folder(body):
    initial = body.get("initial") if isinstance(body.get("initial"), str) else ""
    if initial and not os.path.isdir(initial):
        initial = ""
    if not PICKER_LOCK.acquire(blocking=False):
        raise ApiError(409, "The folder picker is already open. Check your taskbar.")
    try:
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.run(picker_command(initial), capture_output=True,
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
        SESSION.generation += 1
    DUP_JOB.cancel.set()  # results from an older scan must never be acted on
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
    fid = body.get("id")
    path, entry = resolve_id(fid)
    with SESSION.lock:
        root = SESSION.root
    recycle_scanned_file(fid, path, entry, root)
    return {"status": "deleted"}


def api_reveal(body):
    """'Show in folder': open File Explorer with this PDF selected."""
    path, _entry = resolve_id(body.get("id"))
    reveal_in_explorer(path)
    return {"path": path}


def api_duplicates_start(_body):
    with SESSION.lock:
        if not SESSION.root:
            raise ApiError(400, "Scan a folder first.")
        entries = {fid: dict(e) for fid, e in SESSION.files.items()}
        generation, root = SESSION.generation, SESSION.root
    old = DUP_JOB.thread
    if old and old.is_alive() and DUP_JOB.generation != generation:
        DUP_JOB.cancel.set()
        old.join(5)  # an outdated search stops within one 1 MB chunk
    with DUP_JOB.lock:
        if DUP_JOB.thread and DUP_JOB.thread.is_alive():
            if DUP_JOB.generation == generation:
                return {"started": False}  # already searching this scan
            raise ApiError(409, "Still stopping the previous search. Try again in a moment.")
        DUP_JOB.reset()
        DUP_JOB.cancel = threading.Event()
        DUP_JOB.state, DUP_JOB.phase = "running", "sizing"
        DUP_JOB.generation, DUP_JOB.root = generation, root
        DUP_JOB.rels = {fid: e["rel"] for fid, e in entries.items()}
        DUP_JOB.thread = threading.Thread(target=_run_duplicate_job, args=(entries,), daemon=True)
        DUP_JOB.thread.start()
    return {"started": True}


def api_duplicates_status(_body):
    snap = DUP_JOB.snapshot()
    with SESSION.lock:
        snap["stale"] = snap["generation"] != SESSION.generation
    return snap


def api_duplicates_cancel(_body):
    DUP_JOB.cancel.set()
    return {}


def api_delete_duplicates(body):
    """Move chosen duplicate copies to the Recycle Bin, keeping at least one copy of every group.

    All request checks happen before anything is touched. Then each file is re-hashed just before
    it is recycled. One failure doesn't stop the rest, and the reply lists exactly which files were
    recycled and which were not (and why).
    """
    if body.get("confirm") != BATCH_DELETE_CONFIRM_PHRASE:
        raise ApiError(400, "Deletion needs confirmation.")
    remove = body.get("remove")
    if (not isinstance(remove, list) or not remove or len(remove) > MAX_BATCH_DELETE
            or not all(isinstance(i, str) and re.fullmatch(r"[0-9a-f]{16}", i) for i in remove)):
        raise ApiError(400, "Invalid request.")
    remove_set = set(remove)

    with SESSION.lock:
        generation, root = SESSION.generation, SESSION.root
    with DUP_JOB.lock:
        if DUP_JOB.state != "done" or DUP_JOB.generation != generation:
            raise ApiError(409, "These duplicate results are out of date. Click Find duplicates again.")
        groups = [dict(g, ids=list(g["ids"])) for g in DUP_JOB.groups]
        rels = dict(DUP_JOB.rels)
    group_of = {fid: g for g in groups for fid in g["ids"]}

    unknown = [i for i in remove_set if i not in group_of]
    if unknown:
        raise ApiError(400, "Some of those files aren't in the duplicate list. Nothing was moved.")
    plan = []
    for g in groups:
        doomed = [i for i in g["ids"] if i in remove_set]
        if not doomed:
            continue
        if len(doomed) >= len(g["ids"]):
            raise ApiError(400, "Every group must keep at least one copy. Nothing was moved.")
        plan.append((g, doomed, [i for i in g["ids"] if i not in remove_set]))

    def matches(fid: str, g: dict) -> tuple[str, dict]:
        path, entry = resolve_id(fid)
        st = os.stat(path)
        if st.st_size != g["size"] or sha256_file(path) != g["sha256"]:
            raise ApiError(409, "This file changed since the duplicate search, so it was left in place.")
        return path, entry

    recycled, failed = [], []

    def fail(fid: str, message: str) -> None:
        failed.append({"id": fid, "rel": rels.get(fid), "error": message})

    try:
        for g, doomed, keepers in plan:
            keeper_ok = False
            for k in keepers:  # make sure a copy that stays really is still there, unchanged
                try:
                    matches(k, g)
                    keeper_ok = True
                    break
                except Exception:  # noqa: BLE001 (any doubt means: don't touch this group)
                    continue
            if not keeper_ok:
                for fid in doomed:
                    fail(fid, "The copy you chose to keep is missing or changed, so this group was left alone.")
                continue
            for fid in doomed:
                try:
                    path, entry = matches(fid, g)
                    recycled.append(recycle_scanned_file(fid, path, entry, root, save=False))
                except ApiError as exc:
                    fail(fid, exc.message)
                except OSError as exc:
                    fail(fid, f"File error: {exc.strerror or exc}")
                except Exception:  # noqa: BLE001 (keep going so the report stays exact)
                    traceback.print_exc()
                    fail(fid, "Unexpected error. See the PDF Cleaner window for details.")
    finally:
        warning = None
        if recycled:
            try:
                STORE.save()
            except OSError as exc:  # the files did move; say so rather than hide the report
                warning = f"The files were moved, but saved progress couldn't be updated: {exc.strerror or exc}"
            gone = {r["id"] for r in recycled}
            with DUP_JOB.lock:
                if DUP_JOB.generation == generation:
                    for g in DUP_JOB.groups:
                        g["ids"] = [i for i in g["ids"] if i not in gone]
                    DUP_JOB.groups = [g for g in DUP_JOB.groups if len(g["ids"]) > 1]
    return {"recycled": recycled, "failed": failed, "warning": warning}


ROUTES = {
    "/api/state": api_state,
    "/api/pick-folder": api_pick_folder,
    "/api/scan": api_scan,
    "/api/pages": api_pages,
    "/api/status": api_status,
    "/api/delete": api_delete,
    "/api/reveal": api_reveal,
    "/api/duplicates/start": api_duplicates_start,
    "/api/duplicates/status": api_duplicates_status,
    "/api/duplicates/cancel": api_duplicates_cancel,
    "/api/delete-duplicates": api_delete_duplicates,
}


# --------------------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = f"PDFCleaner/{__version__}"
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


def pause_if_double_clicked() -> None:
    """Keep the console open after an error when the .exe was started by double-click."""
    if FROZEN and os.name == "nt":
        try:
            input("  Press Enter to close this window.")
        except (EOFError, KeyboardInterrupt):
            pass


def main() -> None:
    global STORE
    if len(sys.argv) >= 2 and sys.argv[1] == "--pick-folder":  # child process for the folder dialog
        run_folder_picker(sys.argv[2] if len(sys.argv) > 2 else "")
        return

    parser = argparse.ArgumentParser(description="PDF Cleaner - review local PDFs privately.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="port to use (default 8765)")
    parser.add_argument("--no-browser", action="store_true", help="don't open the browser automatically")
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    args = parser.parse_args()

    try:
        lock = acquire_instance_lock(DATA_DIR)  # noqa: F841 (held open until exit)
    except OSError as exc:
        print(f"\n  PDF Cleaner can't save its progress in {DATA_DIR}\n  ({exc.strerror or exc}).\n"
              "  Check that your user folder isn't full or read-only, then start it again.\n",
              file=sys.stderr)
        pause_if_double_clicked()
        sys.exit(1)
    if lock is None:
        print("\n  PDF Cleaner is already running.\n"
              "  Use the browser tab that's already open, or close the other PDF Cleaner\n"
              "  window first. (Two copies would overwrite each other's saved progress.)\n",
              file=sys.stderr)
        pause_if_double_clicked()
        sys.exit(1)

    try:
        if migrate_legacy_progress(LEGACY_PROGRESS_FILE, PROGRESS_FILE):
            print(f"  Copied your saved progress from {LEGACY_PROGRESS_FILE}\n  to {PROGRESS_FILE}.")
    except OSError as exc:
        print(f"Warning: couldn't copy saved progress from {LEGACY_PROGRESS_FILE} ({exc}).", file=sys.stderr)
    STORE = ProgressStore(PROGRESS_FILE)

    server = None
    for port in range(args.port, args.port + 20):
        try:
            server = LocalServer((HOST, port), Handler)
            break
        except OSError:
            continue
    if server is None:
        print(f"Could not find a free port near {args.port}.", file=sys.stderr)
        pause_if_double_clicked()
        sys.exit(1)

    url = f"http://{HOST}:{server.server_address[1]}/"
    print(f"\n  {APP_NAME} {__version__} is running at {url}")
    print(f"  Saved progress: {PROGRESS_FILE}")
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
