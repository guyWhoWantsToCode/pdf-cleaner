# Contributing to PDF Cleaner

Thanks for helping. Bug reports, ideas, documentation fixes and code are all welcome.

## Ground rules

PDF Cleaner exists to be safe to point at personal files. Every change must keep these rules:

1. **Never permanently delete.** Removing a file always means moving it to the Windows Recycle Bin, and only after the user confirms. Batch removal of duplicates must always keep at least one copy of every group, re-check contents right before recycling, and report exactly which files were and weren't moved.
2. **Only `.pdf` files inside the chosen folder.** Never folders, never other file types, never anything reached through a link or junction.
3. **Never modify PDFs.** Files are opened read-only.
4. **Local only.** The server listens on `127.0.0.1` only. No internet requests, no analytics, no cloud services, no AI APIs. The page must not load anything from outside (fonts, scripts, images).
5. **No runtime dependencies.** The app is Python standard library only, plus plain HTML, CSS and JavaScript with no build step. PyInstaller (in `requirements-build.txt`) is used only to package `PDFCleaner.exe`, and must never become something `server.py` needs to run.
6. **Personal data stays in the user's data folder.** Saved progress lives in `%LOCALAPPDATA%\PDF Cleaner`, never next to the program, and is never committed or bundled into the `.exe`.
7. **Beginner-friendly.** Error messages and docs are written in plain language.

Pull requests that break these rules won't be merged, however useful the feature.

## Reporting bugs

Open an issue with:

- what you did, what you expected, and what happened
- your PDF Cleaner version (shown at the bottom of the side panel), your Windows version, how you run it (`PDFCleaner.exe` or Python, with `py --version`) and your browser
- any message shown in the black PDF Cleaner window

**Don't attach your real PDFs, file names or `progress.json` (in `%LOCALAPPDATA%\PDF Cleaner`).** They can contain personal information. Make a sample with `py -3 tests/sample_pdfs.py <empty-folder>` if you need one.

Security problems go through private reporting instead. See [SECURITY.md](SECURITY.md).

## Making changes

1. Fork the repository and create a branch.
2. Make your change. Keep it small and focused.
3. Run the tests:

   ```bash
   py -3 -m unittest discover -s tests -v
   ```

   If you change deletion code, also run the real Recycle Bin tests. They move two generated test PDFs to your Recycle Bin:

   ```bash
   set PDF_CLEANER_TEST_RECYCLE=1
   py -3 -m unittest discover -s tests -v
   ```

   If you change anything that affects packaging (imports, file paths, the folder picker, `build_exe.py`), build the `.exe` and run its smoke tests too. See "Build the .exe" in the README.

4. Try the app by hand with `py -3 server.py`, using a folder of sample PDFs, in light and dark mode and at a narrow window width.
5. Open a pull request explaining what changed and how you tested it.

## Code style

- Python: clear names, short functions, PEP 8. Keep `server.py` readable top to bottom.
- JavaScript: plain modern JS, no frameworks. Use `textContent` or `escapeHtml` for anything that comes from a file name.
- CSS: use the color variables at the top of `app.css`, so light and dark mode stay in sync.
- The page's Content-Security-Policy forbids inline scripts and inline `style=""` attributes. Keep it that way.
- Comments explain *why*, not *what*.

## License

By contributing, you agree that your contributions are licensed under the [MIT License](LICENSE).
