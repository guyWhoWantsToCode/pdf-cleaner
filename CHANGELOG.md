# Changelog

All notable changes to PDF Cleaner. Versions follow [Semantic Versioning](https://semver.org/).

## [1.1.0] - Unreleased

### Added

- **Standalone Windows app.** `PDFCleaner.exe` is a single file that runs without Python. Build it with `build_exe.py` (PyInstaller, build-time only). Running from source with `start.bat` still works.
- **Find duplicates.** Finds PDFs with identical contents, whatever their names. Files are grouped by exact size first, then only same-size files are fingerprinted with SHA-256. Runs in the background with progress and a Stop button. Online-only OneDrive files are skipped so nothing is downloaded.
- **Remove duplicates in one go.** Choose which copies to keep (the oldest is suggested), confirm the full list, and the other copies move to the Recycle Bin. Nothing is moved until you confirm.
- **Show in folder.** Opens File Explorer with the file selected, from the details panel, the viewer bar, the duplicates list, or the `O` key.
- `--version` command-line option, and the version shown at the bottom of the side panel.

### Changed

- **Saved progress moved to `%LOCALAPPDATA%\PDF Cleaner\progress.json`** (it used to be `data\progress.json` next to the program). An existing `data\progress.json` is copied over on first start and the original is left in place. `PDF_CLEANER_DATA_DIR` overrides the location.
- "Already running" now applies per Windows user instead of per program folder, because progress is now stored per user.
- Error messages that would close the `.exe` window now wait for Enter, so they can be read.

### Safety

- Batch removal never removes every copy: the server refuses any request that would empty a group.
- Just before recycling each copy, its size and SHA-256 are checked again, and so is at least one copy being kept. A changed file, or a group whose kept copy is missing or changed, is left alone.
- Duplicate results are tied to the scan they came from; acting on results from an older scan is refused.
- If some files in a batch can't be moved, the rest still go, and the result lists exactly which files were recycled and which weren't, with the reason.
- All existing single-file safeguards still apply to every file in a batch: `.pdf` only, inside the scanned folder, regular files only (no links or junctions), fixed local drives only, Recycle Bin only.

## [1.0.0]

- First public release: review PDFs one at a time with Keep, Skip and Delete (to the Recycle Bin), saved progress, search, sort, filter, keyboard shortcuts, dark and light mode.
