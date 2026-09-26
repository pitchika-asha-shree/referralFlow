"""Turn an uploaded document into text.

Most referrals arrive as faxes, i.e. scanned images wrapped in a PDF with no text
layer. Strategy:
  1. Try the embedded text layer (pdfplumber) - fast and exact.
  2. If a page has almost no text, rasterize it (poppler's pdftoppm) and OCR it
     (tesseract). Both are CLI tools, so there are no heavy Python deps.
"""
from __future__ import annotations

import io
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

MIN_CHARS_PER_PAGE = 40   # below this we assume the page is a scan


class UnsupportedDocument(Exception):
    pass


@dataclass
class TextResult:
    text: str
    method: str          # pdf_text | ocr | plain_text
    pages: int


def ocr_available() -> bool:
    return bool(shutil.which("tesseract") and shutil.which("pdftoppm"))


def extract_text(content: bytes, filename: str) -> TextResult:
    suffix = Path(filename).suffix.lower()
    if suffix in {".txt", ".text"}:
        return TextResult(content.decode("utf-8", errors="replace"), "plain_text", 1)
    if suffix in {".png", ".jpg", ".jpeg", ".tif", ".tiff"}:
        return TextResult(_ocr_image_bytes(content, suffix), "ocr", 1)
    if suffix == ".pdf" or content[:5] == b"%PDF-":
        return _extract_pdf(content)
    raise UnsupportedDocument(f"unsupported file type: {suffix or 'unknown'}")


def _extract_pdf(content: bytes) -> TextResult:
    import pdfplumber  # imported lazily so the rest of the app works without it

    page_texts: list[str] = []
    scanned_pages: list[int] = []
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        for i, page in enumerate(pdf.pages):
            text = page.extract_text() or ""
            page_texts.append(text)
            if len(text.strip()) < MIN_CHARS_PER_PAGE:
                scanned_pages.append(i)

    if not scanned_pages:
        return TextResult("\n".join(page_texts), "pdf_text", len(page_texts))
    if not ocr_available():
        # Degrade gracefully: the validator will flag missing fields for human review.
        return TextResult("\n".join(page_texts), "pdf_text", len(page_texts))

    ocr_texts = _ocr_pdf_pages(content, scanned_pages)
    for idx, text in zip(scanned_pages, ocr_texts):
        page_texts[idx] = text
    return TextResult("\n".join(page_texts), "ocr", len(page_texts))


def _ocr_pdf_pages(content: bytes, pages: list[int]) -> list[str]:
    results = []
    with tempfile.TemporaryDirectory() as tmp:
        pdf_path = Path(tmp) / "doc.pdf"
        pdf_path.write_bytes(content)
        for idx in pages:
            prefix = Path(tmp) / f"p{idx}"
            # 300 DPI grayscale is the usual sweet spot for fax OCR.
            subprocess.run(
                ["pdftoppm", "-r", "300", "-gray", "-png", "-singlefile",
                 "-f", str(idx + 1), "-l", str(idx + 1), str(pdf_path), str(prefix)],
                check=True, capture_output=True, timeout=60)
            results.append(_tesseract(prefix.with_suffix(".png")))
    return results


def _ocr_image_bytes(content: bytes, suffix: str) -> str:
    if not shutil.which("tesseract"):
        raise UnsupportedDocument("image upload requires tesseract to be installed")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / f"img{suffix}"
        path.write_bytes(content)
        return _tesseract(path)


def _tesseract(image_path: Path) -> str:
    # --psm 6 = "assume a uniform block of text", which suits form-style faxes.
    out = subprocess.run(["tesseract", str(image_path), "stdout", "--psm", "6"],
                         check=True, capture_output=True, timeout=120)
    return out.stdout.decode("utf-8", errors="replace")
