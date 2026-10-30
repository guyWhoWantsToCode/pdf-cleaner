"""
Generate small, made-up sample PDFs for tests and screenshots.

Usage:  py -3 tests/sample_pdfs.py <empty-folder>

Everything here is invented placeholder text. No real documents are used.
"""
import os
import sys
import zlib

SENTENCES = [
    "This is a sample document made for testing PDF Cleaner.",
    "The text on this page is placeholder content and means nothing.",
    "Each page has a heading, a few lines of text and a page number.",
    "Use the viewer to scroll, zoom and move between pages.",
    "Keep it, skip it, or move it to the Recycle Bin.",
    "Nothing in this file is real. It exists only to fill the page.",
]


def _pdf_string(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def make_pdf(path: str, title: str, pages: int) -> None:
    """A simple, valid PDF with readable text on every page."""
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>"]
    kids = " ".join(f"{3 + i * 2} 0 R" for i in range(pages))
    objs.append(f"<< /Type /Pages /Kids [{kids}] /Count {pages} >>".encode())
    font_id = 3 + pages * 2
    for i in range(pages):
        lines = [f"BT /F1 26 Tf 72 700 Td ({_pdf_string(title)}) Tj ET",
                 f"BT /F1 12 Tf 72 670 Td (Page {i + 1} of {pages}) Tj ET"]
        y = 630
        for k in range(12):
            sentence = SENTENCES[(i + k) % len(SENTENCES)]
            lines.append(f"BT /F1 12 Tf 72 {y} Td ({_pdf_string(sentence)}) Tj ET")
            y -= 22
        content = "\n".join(lines).encode()
        objs.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 {font_id} 0 R >> >> /Contents {4 + i * 2} 0 R >>".encode())
        objs.append(b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream")
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for n, obj in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{n} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    with open(path, "wb") as fh:
        fh.write(out)


def make_objstm_pdf(path: str, pages: int) -> None:
    """Page tree hidden in a compressed object stream (page-count test only, not viewable)."""
    inner = f"<< /Type /Pages /Kids [] /Count {pages} >>".encode()
    data = zlib.compress(b"2 0 " + inner)
    body = b"%PDF-1.5\n1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
    body += (b"5 0 obj\n<< /Type /ObjStm /N 1 /First 4 /Filter /FlateDecode /Length %d >>\nstream\n"
             % len(data)) + data + b"\nendstream\nendobj\n%%EOF\n"
    with open(path, "wb") as fh:
        fh.write(body)


DEMO_FILES = [
    ("", "Field Trip Permission Slip.pdf", "Field Trip Permission Slip", 1),
    ("", "Study Notes - World History.pdf", "Study Notes: World History", 4),
    ("", "Recipe - Banana Bread.pdf", "Banana Bread", 2),
    ("", "Old Wi-Fi Setup Guide.pdf", "Wi-Fi Setup Guide", 3),
    ("School", "Book Report - Island Adventure.pdf", "Book Report", 5),
    ("School\\Grade 7", "Science Fair Report - Plant Growth.pdf", "Science Fair Report", 6),
    ("School\\Grade 8\\Math", "Algebra Homework 3.pdf", "Algebra Homework 3", 2),
    ("School\\Grade 8\\Math", "Algebra Homework 12.pdf", "Algebra Homework 12", 3),
    ("Stories", "The Lighthouse Keeper.pdf", "The Lighthouse Keeper", 14),
    ("Stories", "Dragon Mountain (draft).pdf", "Dragon Mountain (draft)", 9),
]


def make_demo_folder(root: str) -> None:
    for sub, name, title, pages in DEMO_FILES:
        folder = os.path.join(root, *sub.split("\\")) if sub else root
        os.makedirs(folder, exist_ok=True)
        make_pdf(os.path.join(folder, name), title, pages)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: sample_pdfs.py <empty-folder>")
    make_demo_folder(sys.argv[1])
    print("Sample PDFs written to", sys.argv[1])
