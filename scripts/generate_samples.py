"""Generate synthetic referral documents (all people, IDs and practices are fictional).

    python scripts/generate_samples.py            # writes ./samples/*.pdf

Covers the messy reality of referral intake: different sender templates, a
free-text letter, a fax with missing/invalid data, and a scanned (image-only)
fax that forces the OCR path.
"""
from __future__ import annotations

import random
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from reportlab.lib.pagesizes import letter
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.validation import npi_check_digit  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "samples"
W, H = letter

NPI_SMITH = npi_check_digit("123456789")    # 1234567893
NPI_RAMAN = npi_check_digit("198765432")
NPI_BROOKS = npi_check_digit("176543210")
NPI_OKAFOR = npi_check_digit("154321098")


def _draw_row(c: canvas.Canvas, pairs: list[tuple[str, str]], y: float) -> float:
    if not pairs:
        return y
    for col, (label, value) in enumerate(pairs):
        x = 54 + col * 270
        c.setFont("Helvetica-Bold", 10.5)
        c.drawString(x, y, f"{label}:")
        c.setFont("Helvetica", 10.5)
        c.drawString(x + c.stringWidth(f"{label}: ", "Helvetica-Bold", 10.5), y, value)
    return y - 16


def _form(path: Path, practice: str, rows: list[tuple[str, list[tuple[str, str]]]],
          footer: str = "") -> None:
    """A two-column label/value referral cover sheet."""
    c = canvas.Canvas(str(path), pagesize=letter)
    c.setFont("Helvetica-Bold", 15)
    c.drawString(54, H - 60, practice)
    c.setFont("Helvetica", 10)
    c.drawString(54, H - 76, "Outpatient Referral Form        Fax: (555) 200-1000")
    c.line(54, H - 84, W - 54, H - 84)
    y = H - 110
    for section, pairs in rows:
        c.setFont("Helvetica-Bold", 11)
        c.drawString(54, y, section)
        y -= 18
        c.setFont("Helvetica", 10.5)
        # Short values sit two per row; long ones get a full row so columns never collide.
        row: list[tuple[str, str]] = []
        for label, value in pairs:
            if len(value) > 34:
                y = _draw_row(c, row, y)
                y = _draw_row(c, [(label, value)], y)
                row = []
                continue
            row.append((label, value))
            if len(row) == 2:
                y = _draw_row(c, row, y)
                row = []
        y = _draw_row(c, row, y)
        y -= 10
    if footer:
        c.setFont("Helvetica-Oblique", 9)
        c.drawString(54, 60, footer)
    c.save()


def _letter(path: Path, paragraphs: list[str], signature: list[str]) -> None:
    c = canvas.Canvas(str(path), pagesize=letter)
    c.setFont("Helvetica-Bold", 13)
    c.drawString(72, H - 72, "Lakeside Primary Care Associates")
    c.setFont("Helvetica", 10)
    c.drawString(72, H - 88, "410 Harbor Road, Suite 2   |   Fax (555) 410-7700")
    t = c.beginText(72, H - 130)
    t.setFont("Times-Roman", 12)
    t.setLeading(16)
    for p in paragraphs:
        words, line = p.split(), ""
        for w in words:                           # naive wrap at ~88 chars, like a real letter
            if len(line) + len(w) + 1 > 88:
                t.textLine(line)
                line = w
            else:
                line = f"{line} {w}".strip()
        t.textLine(line)
        t.textLine("")
    for s in signature:
        t.textLine(s)
    c.drawText(t)
    c.save()


def _scan(src: Path, dst: Path, seed: int = 7) -> None:
    """Rasterize a PDF and degrade it like a fax: grayscale, 200 DPI, slight skew, speckle."""
    from PIL import Image, ImageFilter

    rng = random.Random(seed)
    with tempfile.TemporaryDirectory() as tmp:
        prefix = Path(tmp) / "page"
        subprocess.run(["pdftoppm", "-r", "200", "-gray", "-png", "-singlefile", str(src), str(prefix)],
                       check=True)
        img = Image.open(prefix.with_suffix(".png")).convert("L")
        img = img.rotate(0.8, expand=False, fillcolor=255, resample=Image.BICUBIC)
        px = img.load()
        for _ in range(img.width * img.height // 400):   # salt-and-pepper speckle
            px[rng.randrange(img.width), rng.randrange(img.height)] = rng.choice((0, 255))
        img = img.filter(ImageFilter.GaussianBlur(0.4))
        c = canvas.Canvas(str(dst), pagesize=letter)
        c.drawImage(ImageReader(img), 0, 0, width=W, height=H)
        c.save()


def main() -> None:
    OUT.mkdir(exist_ok=True)

    # 1. Clean form -> routes to joint-replacement and is delivered
    _form(OUT / "acme_clean_form.pdf", "Sunnyvale Family Medicine", [
        ("PATIENT INFORMATION", [("Patient Name", "Doe, Jane"), ("DOB", "03/14/1962"),
                                 ("Phone", "(555) 201-3344")]),
        ("INSURANCE", [("Primary Insurance", "Aetna PPO"), ("Member ID", "W123456789")]),
        ("REFERRING PROVIDER", [("Referring Provider", "Dr. Alan Smith, MD"), ("NPI", NPI_SMITH)]),
        ("CLINICAL", [("Diagnosis (ICD-10)", "M17.11 Primary osteoarthritis, right knee"),
                      ("Urgency", "Routine")]),
        ("", [("Reason for Referral", "Evaluate for total knee arthroplasty after failed PT")]),
    ], footer="Confidential health information. If received in error, notify sender.")

    # 2. Different template + urgent -> ortho-urgent / high priority
    _form(OUT / "acme_urgent_spine.pdf", "Riverside Internal Medicine", [
        ("Patient", [("Pt Name", "ROBERT KLINE"), ("Date of Birth", "1975-11-02"),
                     ("Cell Phone", "555.614.2290")]),
        ("Coverage", [("Insurance", "UnitedHealthcare Choice Plus"), ("Subscriber ID", "UHC-88213377")]),
        ("Ordering", [("Ordering Physician", "Priya Raman, DO"), ("NPI #", NPI_RAMAN)]),
        ("Clinical", [("Dx", "M54.16, M51.26"), ("Priority", "URGENT - schedule within 48 hrs")]),
        ("", [("Reason", "Progressive lumbar radiculopathy with new foot weakness")]),
    ])

    # 3. Free-text letter -> narrative extraction -> spine-clinic
    _letter(OUT / "acme_referral_letter.pdf", [
        "September 8, 2026",
        "Dear Dr. Patel,",
        "I am referring my patient, Marcus Webb (DOB 07/22/1958), for evaluation and management "
        "of worsening lumbar spinal stenosis (M48.061) with neurogenic claudication. Symptoms "
        "have progressed despite eight weeks of physical therapy and NSAIDs.",
        "He is insured through Blue Cross Blue Shield, member ID XJH448120973. He can be reached "
        "at (555) 331-9012. Recent MRI images are available on request.",
        "Thank you for seeing him.",
    ], ["Sincerely,", "Dr. Hannah Brooks, MD", f"NPI: {NPI_BROOKS}"])

    # 4. Missing member ID + NPI with a bad check digit -> needs_review + ticket
    _form(OUT / "acme_missing_info.pdf", "Oak Valley Clinic", [
        ("PATIENT", [("Patient Name", "Tomasz Nowak"), ("DOB", "12/01/1990")]),
        ("INSURANCE", [("Insurance", "Cigna Open Access")]),
        ("PROVIDER", [("Referring Provider", "Dr. Lena Ortiz"), ("NPI", "1234567890")]),
        ("CLINICAL", [("Diagnosis", "M25.561 Pain in right knee"), ("Urgency", "Routine")]),
    ])

    # 5. Image-only scanned fax -> OCR path
    with tempfile.TemporaryDirectory() as tmp:
        clean = Path(tmp) / "clean.pdf"
        _form(clean, "Northgate Medical Group", [
            ("PATIENT INFORMATION", [("Patient Name", "Elena Vasquez"), ("DOB", "08/30/1967"),
                                     ("Phone", "(555) 718-4402")]),
            ("INSURANCE", [("Primary Insurance", "Humana Gold"), ("Member ID", "H44729018")]),
            ("REFERRING PROVIDER", [("Referring Provider", "Dr. Samuel Okafor"), ("NPI", NPI_OKAFOR)]),
            ("CLINICAL", [("Diagnosis", "M17.12 Primary osteoarthritis, left knee"),
                          ("Urgency", "Routine")]),
        ])
        if shutil.which("pdftoppm"):
            _scan(clean, OUT / "acme_scanned_fax.pdf")
        else:
            print("pdftoppm not found; skipping scanned sample")

    # 6. DME order for a second client with different rules -> medicare-documentation
    _form(OUT / "sunrise_cpap_order.pdf", "Pinecrest Sleep Center", [
        ("PATIENT", [("Patient", "Harold Jensen"), ("DOB", "5/9/1949"),
                     ("Phone", "(555) 902-1188")]),
        ("INSURANCE", [("Insurance", "Medicare Part B"), ("Member ID", "1EG4TE5MK73")]),
        ("PRESCRIBER", [("Prescriber", "Dr. Alan Smith, MD"), ("NPI", NPI_SMITH)]),
        ("ORDER", [("Diagnosis", "G47.33 Obstructive sleep apnea")]),
        ("", [("Equipment Requested", "CPAP with heated humidifier and full face mask")]),
    ])

    for p in sorted(OUT.glob("*.pdf")):
        print(f"wrote {p.relative_to(OUT.parent)}")


if __name__ == "__main__":
    main()
