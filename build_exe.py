"""
Build dist/PDFCleaner.exe: one file that runs on Windows 10/11 without Python installed.

Usage (from the project folder):
    py -3 -m venv .venv
    .venv\\Scripts\\python -m pip install -r requirements-build.txt
    .venv\\Scripts\\python build_exe.py

Everything the build creates goes into build/ and dist/, which are never committed.
Only server.py and the static/ folder go into the .exe. No PDFs, saved progress or
settings from this computer are included.
"""
import hashlib
import os
import re
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BUILD = os.path.join(HERE, "build")
DIST = os.path.join(HERE, "dist")
STATIC = os.path.join(HERE, "static")
STATIC_FILES = ["index.html", "app.css", "app.js", "theme.js", "icon.svg"]


def read_version() -> str:
    with open(os.path.join(HERE, "server.py"), encoding="utf-8") as fh:
        return re.search(r'^__version__ = "([\d.]+)"', fh.read(), re.M).group(1)


def make_icon(path: str) -> None:
    """Draw the app icon (same shapes as static/icon.svg) as a multi-size .ico."""
    from PIL import Image, ImageDraw

    s = 256 / 64  # the SVG is drawn on a 64x64 grid
    img = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    sc = lambda *v: [round(x * s) for x in v]  # noqa: E731
    d.rounded_rectangle(sc(10, 4, 48, 54), radius=round(7 * s), fill="#6366f1")
    d.polygon([tuple(sc(38, 4)), tuple(sc(48, 14)), tuple(sc(48, 18)), tuple(sc(44, 18)), tuple(sc(38, 12))],
              fill="#4f46e5")
    for x, y, w, alpha in ((17, 18, 18, 217), (17, 27, 24, 153), (17, 36, 14, 153)):
        layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
        ImageDraw.Draw(layer).rounded_rectangle(sc(x, y, x + w, y + 4), radius=round(2 * s),
                                                fill=(255, 255, 255, alpha))
        img = Image.alpha_composite(img, layer)
    d = ImageDraw.Draw(img)
    d.ellipse(sc(32, 32, 60, 60), fill="#16a34a")
    d.line([tuple(sc(39.5, 46)), tuple(sc(44, 50.5)), tuple(sc(52.5, 41.5))], fill="white",
           width=round(4 * s), joint="curve")
    img.save(path, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])


def write_version_file(path: str, version: str) -> None:
    parts = (version.split(".") + ["0", "0", "0"])[:4]
    tup = ", ".join(parts)
    dotted = ".".join(parts)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers=({tup}), prodvers=({tup}), mask=0x3f, flags=0x0, OS=0x40004,
                    fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'PDF Cleaner contributors'),
      StringStruct('FileDescription', 'PDF Cleaner - review local PDFs privately'),
      StringStruct('FileVersion', '{dotted}'),
      StringStruct('InternalName', 'PDFCleaner'),
      StringStruct('LegalCopyright', 'MIT License'),
      StringStruct('OriginalFilename', 'PDFCleaner.exe'),
      StringStruct('ProductName', 'PDF Cleaner'),
      StringStruct('ProductVersion', '{dotted}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
""")


def main() -> None:
    if os.name != "nt":
        sys.exit("PDFCleaner.exe can only be built on Windows.")
    import PyInstaller.__main__

    version = read_version()
    shutil.rmtree(BUILD, ignore_errors=True)
    os.makedirs(BUILD)
    icon = os.path.join(BUILD, "icon.ico")
    make_icon(icon)
    version_file = os.path.join(BUILD, "version.txt")
    write_version_file(version_file, version)

    # Stage exactly the files the app serves, so nothing else from static/ can slip in.
    staged = os.path.join(BUILD, "static")
    os.makedirs(staged)
    for name in STATIC_FILES:
        shutil.copy2(os.path.join(STATIC, name), staged)

    PyInstaller.__main__.run([
        os.path.join(HERE, "server.py"),
        "--name", "PDFCleaner",
        "--onefile",
        "--console",  # the window people close to stop the app, as with start.bat
        "--icon", icon,
        "--version-file", version_file,
        "--add-data", f"{staged}{os.pathsep}static",
        "--hidden-import", "tkinter.filedialog",
        "--exclude-module", "PIL",
        "--distpath", DIST,
        "--workpath", os.path.join(BUILD, "pyinstaller"),
        "--specpath", BUILD,
        "--noconfirm",
        "--clean",
    ])

    exe = os.path.join(DIST, "PDFCleaner.exe")
    h = hashlib.sha256()
    with open(exe, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    print(f"\nBuilt {exe}\n  version {version}, {os.path.getsize(exe) / 1e6:.1f} MB\n  SHA-256 {h.hexdigest()}")


if __name__ == "__main__":
    main()
