# Security policy

PDF Cleaner handles people's personal files, so security and privacy problems are taken seriously.

## What PDF Cleaner promises

- It only listens on `127.0.0.1`, so other computers can't connect to it.
- It makes no internet requests and sends no file contents, names or details anywhere.
- It never permanently deletes anything. **Delete moves a file to the Windows Recycle Bin**, only after the user confirms.
- It only acts on `.pdf` files inside the folder the user chose. It never deletes folders or other file types.
- It never changes the contents of a PDF.
- When removing duplicates, it always keeps at least one copy of every file.
- Release `.exe` files are built only from the source in this repository with `build_exe.py`, and each release lists the file's SHA-256 checksum.

Anything that breaks one of these promises is a security bug. Examples:

- another website or another computer being able to control PDF Cleaner or read files through it
- a way to make it delete, or recycle, something other than a confirmed PDF inside the chosen folder
- a way to make it permanently delete a file
- a way to make the duplicate remover recycle every copy of a file
- a way to make it send data off the computer

## Supported versions

Only the latest release (and the `main` branch) gets security fixes.

## Reporting a vulnerability

**Please don't open a public issue for security problems.**

Report it privately instead: on this repository's GitHub page, go to the **Security** tab and click **Report a vulnerability**. Please include:

- what you found and why it matters
- steps to reproduce it
- your Windows, Python and browser versions

Please don't include real personal documents in a report. Use made-up files. `tests/sample_pdfs.py` can generate some.

You should get a reply within 7 days. Once a fix is ready, you'll be credited in the release notes unless you'd rather not be.
