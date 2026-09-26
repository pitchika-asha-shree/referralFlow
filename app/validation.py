"""Normalize extracted values and decide whether a referral can go out automatically.

Every normalizer returns the cleaned value or raises ValueError with a message a
human reviewer can act on.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Callable

from app.models import ExtractedField, Severity, ValidationIssue

# ICD-10-CM: letter (not U), digit, alphanumeric, optional dot + 1-4 alphanumerics.
ICD10_RE = re.compile(r"^[A-TV-Z][0-9][0-9A-Z](?:\.[0-9A-Z]{1,4})?$")

_DATE_FORMATS = ("%m/%d/%Y", "%m-%d-%Y", "%Y-%m-%d", "%m/%d/%y", "%B %d, %Y",
                 "%b %d, %Y", "%d %B %Y", "%d %b %Y", "%b. %d, %Y")


def normalize_date(value: str, today: date | None = None) -> str:
    today = today or date.today()
    cleaned = re.sub(r"\s+", " ", value.strip().rstrip(".,"))
    for fmt in _DATE_FORMATS:
        try:
            parsed = datetime.strptime(cleaned, fmt).date()
            break
        except ValueError:
            continue
    else:
        raise ValueError(f"unrecognized date '{value}'")
    if fmt == "%m/%d/%y" and parsed > today:
        # Two-digit years: Python maps '62' to 2062, but a birth date can't be in the future.
        parsed = parsed.replace(year=parsed.year - 100)
    if parsed > today:
        raise ValueError(f"date '{value}' is in the future")
    if (today.year - parsed.year) > 120:
        raise ValueError(f"date '{value}' implies an age over 120")
    return parsed.isoformat()


def normalize_phone(value: str) -> str:
    digits = re.sub(r"\D", "", value)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10 or digits[0] in "01":
        raise ValueError(f"'{value}' is not a valid US phone number")
    return f"+1{digits}"


def npi_is_valid(npi: str) -> bool:
    """NPI check digit = Luhn over the 9 base digits prefixed with 80840 (CMS spec)."""
    if not re.fullmatch(r"\d{10}", npi):
        return False
    digits = [int(c) for c in "80840" + npi[:9]]
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2 == 0:          # double every other digit starting from the rightmost base digit
            d *= 2
            if d > 9:
                d -= 9
        total += d
    check = (10 - total % 10) % 10
    return check == int(npi[9])


def npi_check_digit(base9: str) -> str:
    """Helper for generating test data: returns the full valid 10-digit NPI."""
    for d in "0123456789":
        if npi_is_valid(base9 + d):
            return base9 + d
    raise ValueError("unreachable")


def normalize_npi(value: str) -> str:
    npi = re.sub(r"\D", "", value)
    if len(npi) != 10:
        raise ValueError(f"NPI '{value}' must be 10 digits")
    if not npi_is_valid(npi):
        raise ValueError(f"NPI '{npi}' fails the check-digit test (likely an OCR or typing error)")
    return npi


def normalize_icd10(code: str) -> str:
    c = code.strip().upper().replace(" ", "")
    if "." not in c and len(c) > 3:
        c = f"{c[:3]}.{c[3:]}"
    if not ICD10_RE.match(c):
        raise ValueError(f"'{code}' is not a valid ICD-10 code")
    return c


def normalize_icd10_list(value: Any) -> list[str]:
    codes = value if isinstance(value, list) else re.split(r"[,;\s]+", str(value))
    out: list[str] = []
    for c in codes:
        if c and (n := normalize_icd10(c)) not in out:
            out.append(n)
    if not out:
        raise ValueError("no diagnosis codes found")
    return out


def normalize_name(value: str) -> str:
    v = re.sub(r"\s+", " ", value.strip().strip(",."))
    if "," in v:                       # "DOE, JANE" -> "JANE DOE"
        last, first = [p.strip() for p in v.split(",", 1)]
        v = f"{first} {last}"
    if not re.fullmatch(r"[A-Za-z][A-Za-z .'\-]{1,80}", v) or len(v.split()) < 2:
        raise ValueError(f"'{value}' does not look like a full name")
    return " ".join(p.capitalize() if p.isupper() or p.islower() else p for p in v.split())


def normalize_member_id(value: str) -> str:
    v = re.sub(r"\s+", "", value).upper()
    if not re.fullmatch(r"[A-Z0-9\-]{5,20}", v):
        raise ValueError(f"member ID '{value}' should be 5-20 letters/digits")
    return v


def normalize_urgency(value: str) -> str:
    v = value.strip().lower()
    if any(k in v for k in ("urgent", "stat", "asap", "expedite")):
        return "urgent"
    if any(k in v for k in ("routine", "standard", "normal")):
        return "routine"
    raise ValueError(f"unknown urgency '{value}'")


def normalize_text(value: str) -> str:
    v = re.sub(r"\s+", " ", str(value)).strip()
    if not v:
        raise ValueError("empty value")
    return v


NORMALIZERS: dict[str, Callable[[Any], Any]] = {
    "patient_name": normalize_name,
    "patient_dob": normalize_date,
    "patient_phone": normalize_phone,
    "insurance_payer": normalize_text,
    "insurance_member_id": normalize_member_id,
    "referring_provider": normalize_text,
    "referring_npi": normalize_npi,
    "diagnosis_codes": normalize_icd10_list,
    "reason_for_referral": normalize_text,
    "urgency": normalize_urgency,
}


def validate_fields(
    fields: dict[str, ExtractedField],
    required: list[str],
    min_confidence: float,
) -> tuple[dict[str, ExtractedField], list[ValidationIssue]]:
    """Normalize every field and collect issues. Never raises on bad data."""
    out: dict[str, ExtractedField] = {}
    issues: list[ValidationIssue] = []

    for name, f in fields.items():
        normalizer = NORMALIZERS.get(name, normalize_text)
        try:
            value = normalizer(f.value)
        except ValueError as exc:
            sev = Severity.ERROR if name in required else Severity.WARNING
            issues.append(ValidationIssue(name, "invalid_format", str(exc), sev))
            out[name] = f
            continue
        out[name] = ExtractedField(name, value, f.confidence, f.source, f.raw)
        if f.confidence < min_confidence and f.source != "human":
            sev = Severity.ERROR if name in required else Severity.WARNING
            issues.append(ValidationIssue(
                name, "low_confidence",
                f"confidence {f.confidence:.2f} is below the client threshold {min_confidence:.2f}",
                sev))

    for name in required:
        if name not in out:
            issues.append(ValidationIssue(name, "missing_required",
                                          f"required field '{name}' was not found in the document"))
    return out, issues
