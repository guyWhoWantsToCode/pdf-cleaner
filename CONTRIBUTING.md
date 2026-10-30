# Contributing to PDF Cleaner

Thanks for helping. Bug reports, ideas, documentation fixes and code are all welcome.

## Ground rules

PDF Cleaner exists to be safe to point at personal files. Every change must keep these rules:

1. **Never permanently delete.** Removing a file always means moving it to the Windows Recycle Bin, and only after the user confirms.
2. **Only `.pdf` files inside the chosen folder.** Never folders, never other file types, never anything reached through a link or junction.
3. **Never modify PDFs.** Files are opened read-only.
4. **Local only.** The server listens on `127.0.0.1` only. No internet requests, no analytics, no cloud services, no AI APIs. The page must not load anything from outside (fonts, scripts, images).
5. **No dependencies.** Python standard library only, plus plain HTML, CSS and JavaScript with no build step. This keeps installation simple for people with no programming experience.
6. **Beginner-friendly.** Error messages and docs are written in plain language.

Pull requests that break these rules won't be merged, however useful the feature.

## Reporting bugs

Open an issue with:

- what you did, what you expected, and what happened
- your Windows version, Python version (`py --version`) and browser
- any message shown in the black PDF Cleaner window

**Don't attach your real PDFs, file names or `data/progress.json`.** They can contain personal information. Make a sample with `py -3 tests/sample_pdfs.py <empty-folder>` if you need one.

Security problems go through private reporting instead. See [SECURITY.md](SECURITY.md).

## Making changes

1. Fork the repository and create a branch.
2. Make your change. Keep it small and focused.
3. Run the tests:

   ```bash
   py -3 -m unittest discover -s tests -v
   ```

   If you change deletion code, also run the real Recycle Bin test. It moves one generated test PDF to your Recycle Bin:

   ```bash
   set PDF_CLEANER_TEST_RECYCLE=1
   py -3 -m unittest discover -s tests -v
   ```

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
