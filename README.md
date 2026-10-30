# PDF Cleaner

**Review the PDFs on your Windows computer one at a time, and decide what to keep.**

PDF Cleaner is a small, free app that finds every PDF in a folder you choose, shows them to you one by one, and lets you **Keep**, **Skip**, or **Delete** each one. It runs entirely on your own computer: nothing is uploaded, and nothing is ever permanently erased. **Delete always moves the file to the Windows Recycle Bin**, so you can restore it later.

![PDF Cleaner in dark mode, showing a file list, a PDF in the viewer, and Keep / Skip / Delete buttons](docs/screenshots/main-dark.jpg)

---

## Contents

- [Features](#features)
- [Screenshots](#screenshots)
- [Privacy and safety](#privacy-and-safety)
- [Supported systems](#supported-systems)
- [Install and run (step by step)](#install-and-run-step-by-step)
- [How to use it](#how-to-use-it)
- [Troubleshooting](#troubleshooting)
- [For developers](#for-developers)
- [License](#license)

## Features

- **One PDF at a time.** A built-in viewer lets you scroll, zoom, change pages and read the whole document.
- **Clear details.** File name, size, page count, folder location and last-modified date.
- **Three simple choices:**
  - **Keep**: marks the file as reviewed. The file isn't changed.
  - **Skip**: leaves the file alone and moves on. You can come back later.
  - **Delete**: asks you to confirm, then **moves the file to the Windows Recycle Bin**. It is never permanently erased.
- **Undo and change your mind.** Every Keep or Skip can be undone, and any decision can be changed later.
- **Remembers your progress.** Decisions are saved on your computer after every click, and the last folder reopens next time.
- **Search, sort and filter.** Search by file or folder name. Sort by name, date, size, pages or folder. Filter by not reviewed, kept, skipped or in Recycle Bin.
- **Subfolders.** Optionally include every folder inside the one you choose.
- **Keyboard shortcuts.** `K` keep, `S` skip, `D` delete, `U` clear decision, `←` / `→` previous / next, `/` search.
- **Dark and light mode**, and a layout that adapts from wide screens down to small windows.

## Screenshots

| Light mode | Delete always asks first |
|---|---|
| ![Light mode](docs/screenshots/main-light.jpg) | ![Confirmation dialog: Move this PDF to the Recycle Bin?](docs/screenshots/delete-confirm.jpg) |

<p align="center"><img src="docs/screenshots/phone.jpg" alt="PDF Cleaner in a narrow window" width="260"></p>

*The screenshots use made-up sample PDFs (created by `tests/sample_pdfs.py`). The folder path shown was replaced with `C:\Users\you\Documents\Old PDFs` for privacy.*

## Privacy and safety

PDF Cleaner was built to be safe to point at your personal files.

| | |
|---|---|
| **Stays on your computer** | The app is a tiny web server that only listens on `127.0.0.1`, the address your computer uses to talk to itself. Other computers, even on your home network, can't connect to it. |
| **No internet, no cloud, no AI** | It makes no internet requests. Your PDFs, file names and details are never uploaded, sent to an AI service, or shared with anyone. No analytics, no tracking. |
| **Never permanently deletes** | **Delete moves files to the Windows Recycle Bin**, the same as pressing Delete in File Explorer. You can restore them from there. Files on USB sticks or network drives, which have no Recycle Bin, are refused. If Windows can't recycle a file (for example, it's larger than the Recycle Bin allows), Windows asks you first. |
| **Always asks first** | Every delete shows a confirmation with the full file path. The default button is **Cancel**. |
| **Only PDFs, only in your folder** | It only ever acts on `.pdf` files inside the folder you chose. It never deletes folders or any other kind of file, and it doesn't follow shortcuts or links out of the folder. |
| **Never changes your PDFs** | PDFs are only opened for reading. |
| **Protected from other websites** | Each time it starts, it creates a random secret key that's needed for every action. Other websites open in your browser can't see that key, so they can't control PDF Cleaner. |

Your decisions are saved in `data/progress.json` inside the PDF Cleaner folder. That file contains the names of your PDFs, so keep it private. To start over, close the app and delete it.

## Supported systems

| | Status |
|---|---|
| **Windows 11** | ✅ Tested |
| **Windows 10** | Should work, but not yet tested |
| **macOS / Linux** | ❌ Not supported. Moving files to the Recycle Bin is Windows-only, and the app hasn't been tested elsewhere. |
| **Python** | 3.9 or newer, from the python.org Windows installer. Tested with 3.9, 3.10 and 3.13. Not yet tested with the "Python install manager" or Microsoft Store Python. |
| **Browser** | Microsoft Edge or Google Chrome (recommended). Tested with a Chromium-based browser and Microsoft Edge 156. Firefox hasn't been tested. |

## Install and run (step by step)

You don't need any programming experience. This takes about five minutes the first time.

### Step 1: Install Python (one time only)

PDF Cleaner is written in Python, a free programming language. You only install it once.

1. Go to **<https://www.python.org/downloads/windows/>**.
2. Under the first release listed under **Stable Releases**, click **Download Windows installer (64-bit)**.
3. Open the file you downloaded. It's usually in your **Downloads** folder.
4. **Important:** on the first screen of the installer, tick the box **"Add python.exe to PATH"** at the bottom.
5. Click **Install Now** and wait until it says **"Setup was successful"**. Then click **Close**.

> Already have Python 3.9 or newer? Skip this step.
>
> python.org also offers a newer **"Python install manager"**. PDF Cleaner hasn't been tested with it yet, so the classic installer above is the recommended way. If you already use the install manager, make sure it has installed at least one Python version before you start PDF Cleaner.

### Step 2: Download PDF Cleaner

1. At the top of this GitHub page, click the green **Code** button.
2. Click **Download ZIP**.
3. Open your **Downloads** folder, **right-click** the ZIP file (`pdf-cleaner-main.zip`) and choose **Extract All…**, then **Extract**.
4. Move the extracted **`pdf-cleaner-main`** folder somewhere you'll find it again, like **Documents** or the **Desktop**.

> Don't run it from inside the ZIP file, and don't put it in `C:\Program Files`. Windows doesn't let apps save files there.

### Step 3: Start PDF Cleaner

1. Open the `pdf-cleaner-main` folder.
2. Double-click **`start.bat`**. On some computers it's shown as just **`start`**.
3. A black window opens, and your web browser opens PDF Cleaner. **Leave the black window open** while you use the app.

> **"Windows protected your PC"?** Windows shows this for files downloaded from the internet that aren't from a big company. Click **More info**, then **Run anyway**. You can read every line of `start.bat` and `server.py` first. They're short, plain text files.

If the browser doesn't open by itself, open Edge or Chrome and go to **<http://127.0.0.1:8765>**.

### Step 4: Stop PDF Cleaner

Close the black window, or click it and press **Ctrl + C**. Your progress is already saved.

## How to use it

1. Click **Choose folder** and pick a folder with PDFs. You can also paste a folder path into the box and click **Scan**.
   Tick **Include subfolders** to also search every folder inside it.
2. The first PDF you haven't reviewed opens. Use the viewer's toolbar to zoom and change pages, or click **New tab** to read it full-screen.
3. Choose **Keep**, **Skip** or **Delete**. PDF Cleaner moves on to the next file automatically.
4. Watch the progress bar at the top. You can close the app at any time and continue later.

**Changing your mind**

- Right after Keep or Skip, click **Undo** in the message at the bottom of the screen.
- Later: select the file in the list and choose a different action, or click **Clear decision**.
- Deleted files are listed as **In Recycle Bin**. To get one back, open the **Recycle Bin** on your desktop, right-click the file, choose **Restore**, then click **Scan** again in PDF Cleaner.

**Keyboard shortcuts** work when the PDF itself isn't focused. If a shortcut doesn't work, click on the file list or the side panel first.

| Key | Action |
|---|---|
| `K` | Keep |
| `S` | Skip |
| `D` or `Delete` | Delete (asks first) |
| `U` | Clear decision |
| `←` `→` | Previous / next file |
| `/` | Search |

## Troubleshooting

<details>
<summary><strong>"PDF Cleaner needs Python 3.9 or newer, and it wasn't found"</strong></summary>

- Install Python as described in [Step 1](#step-1-install-python-one-time-only), and make sure **"Add python.exe to PATH"** is ticked.
- If you installed Python without that box: run the Python installer again, choose **Modify**, then **Next**, tick **"Add Python to environment variables"**, and click **Install**.
- After installing, **close and reopen** the PDF Cleaner folder, then double-click `start.bat` again.
- If the Microsoft Store opens when you type `python`, Python isn't really installed yet. Install it from python.org as above.
</details>

<details>
<summary><strong>The black window flashes and closes, or says it can't find its files</strong></summary>

- You're probably running `start.bat` from inside the ZIP. Right-click the ZIP, choose **Extract All…**, and run `start.bat` from the extracted folder.
- Make sure `server.py` and the `static` folder are next to `start.bat`.
</details>

<details>
<summary><strong>The browser doesn't open, or shows "This site can't be reached"</strong></summary>

- Check that the black PDF Cleaner window is still open. The app only works while it's running.
- Look in the black window for the exact address, for example `http://127.0.0.1:8766/`. If port 8765 was busy, it uses the next free one. Type that address into Edge or Chrome.
- If you see "PDF Cleaner was restarted. Please reload the page", press **F5**.
</details>

<details>
<summary><strong>"PDF Cleaner is already running from this folder"</strong></summary>

Only one copy can run at a time, so two copies can't overwrite each other's saved progress. Switch to the browser tab that's already open, or close the other black PDF Cleaner window (check the taskbar) and start it again.
</details>

<details>
<summary><strong>The PDF area is blank, or the PDF downloads instead of showing</strong></summary>

- Use **Microsoft Edge** or **Google Chrome**. Both have a built-in PDF viewer.
- In Chrome: go to `chrome://settings/content/pdfDocuments` and choose **Open PDFs in Chrome**.
  In Edge: go to `edge://settings/content/pdfDocuments` and turn **off** "Always download PDF files".
- Some damaged or password-protected PDFs can't be shown. You can still Keep, Skip or Delete them.
</details>

<details>
<summary><strong>"Can't read that folder" or files are missing from the list</strong></summary>

- Windows may not let your account read some folders (for example other users' folders or system folders). Choose a folder you can open in File Explorer.
- Files that OneDrive keeps "online-only" are listed, but their page count only appears when you open them.
- Shortcuts and links to other folders are not followed. This is on purpose, so the app never leaves the folder you chose.
- PDF Cleaner shows up to 25,000 PDFs at a time. For more, choose a smaller folder.
</details>

<details>
<summary><strong>Delete doesn't work, or shows a Recycle Bin error</strong></summary>

- **"Removable or network drive, which has no Recycle Bin"**: USB sticks, SD cards and network drives have no Recycle Bin, so deleting there would be permanent. PDF Cleaner refuses on purpose. Copy the files to your computer first, or delete them yourself in File Explorer.
- **"It may be open in another program or read-only"**: close the PDF in any other app (Adobe Reader, Edge tabs, Word) and try again. If the file is read-only, right-click it in File Explorer → **Properties** → untick **Read-only**.
- **"Cancelled. The file couldn't go to the Recycle Bin"**: Windows warned that the file can't be recycled (often because it's larger than the Recycle Bin's size limit), and PDF Cleaner left it alone. You can raise the limit: right-click the **Recycle Bin** → **Properties** → set a larger **Custom size**.
- **"This file no longer exists"**: it was moved or deleted outside PDF Cleaner. Click **Scan** to refresh the list.
</details>

<details>
<summary><strong>"File error" when choosing Keep or Skip</strong></summary>

PDF Cleaner saves progress in its own `data` folder. If it lives somewhere Windows protects (like `C:\Program Files`), it can't save. Move the whole `pdf-cleaner-main` folder to **Documents** or the **Desktop**.
</details>

<details>
<summary><strong>The "Choose folder" window doesn't appear</strong></summary>

It may have opened behind your browser. Check the taskbar for a Python icon. You can also paste the folder path into the box and click **Scan**. To copy a folder's path, open it in File Explorer, click the address bar, and copy.
</details>

## For developers

Everything is plain Python 3 (standard library only) plus plain HTML, CSS and JavaScript. There are no dependencies and no build step.

```
server.py              local web server: scanning, page counts, Recycle Bin, saved progress
static/                the interface (index.html, app.css, app.js, theme.js, icon.svg)
start.bat              Windows launcher
tests/test_server.py   automated tests (unittest)
tests/sample_pdfs.py   generates made-up PDFs for tests and screenshots
data/progress.json     your saved decisions (created on first use, never committed)
```

Run it from a terminal:

```bash
py -3 server.py
```

Options: `--port 9000` picks another port, and `--no-browser` stops it opening the browser.

Run the tests:

```bash
py -3 -m unittest discover -s tests -v
```

The tests work in a temporary folder and never touch your files or your progress. One test really moves a small generated PDF to your Recycle Bin, so it only runs when you set `PDF_CLEANER_TEST_RECYCLE=1`.

See [CONTRIBUTING.md](CONTRIBUTING.md) for how to help, and [SECURITY.md](SECURITY.md) to report a security problem.

## License

[MIT](LICENSE). Free to use, change and share.
