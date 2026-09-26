"""Deterministic extractor: label/value parsing for forms + regex patterns for letters.

Two passes, each with a different confidence:
  * Labeled fields ("Member ID: XYZ123") -> 0.95
  * Narrative patterns ("my patient, Jane Doe (DOB 3/14/1962)") -> 0.80
  * Unlabeled dotted ICD-10 codes found anywhere -> 0.80

There is deliberately no "grab any phone number" fallback: on a fax cover sheet the
first phone number is usually the *sender's* fax line. Wrong data delivered to an
EHR is worse than a missing field that a human fills in.

Confidence is what lets each client set its own automation threshold.
"""
from __future__ import annotations

import re

from app.models import ExtractedField

LABELED, NARRATIVE = 0.95, 0.80

# Field -> label phrases seen on real referral cover sheets. Longest match wins,
# so "patient phone" beats "patient" and "insurance id" beats "insurance".
LABELS: dict[str, list[str]] = {
    "patient_name": ["patient name", "name of patient", "pt name", "patient"],
    "patient_dob": ["date of birth", "patient dob", "birth date", "d.o.b.", "dob"],
    "patient_phone": ["patient phone", "home phone", "cell phone", "phone", "tel"],
    "insurance_payer": ["primary insurance", "insurance carrier", "insurance plan",
                        "insurance", "payer", "health plan"],
    "insurance_member_id": ["insurance id", "member id", "subscriber id", "policy number",
                            "member", "policy"],
    "referring_provider": ["referring provider", "referring physician", "ordering provider",
                           "ordering physician", "prescribing provider", "prescriber",
                           "referred by"],
    "referring_npi": ["provider npi", "referring npi", "prescriber npi", "npi"],
    "diagnosis_codes": ["diagnosis (icd-10)", "diagnosis codes", "diagnosis code", "diagnosis",
                        "icd-10", "icd10", "icd", "dx"],
    "reason_for_referral": ["reason for referral", "referral reason", "reason for visit",
                            "services requested", "equipment requested", "reason"],
    "urgency": ["urgency", "priority"],
}

_LABEL_TO_FIELD = {phrase: fld for fld, phrases in LABELS.items() for phrase in phrases}
_ALL_LABELS = sorted(_LABEL_TO_FIELD, key=len, reverse=True)
# A label must be followed by ':' '#' or ';' (OCR often reads ':' as ';') so that
# ordinary prose containing the word "patient" is not treated as a label.
LABEL_RE = re.compile(
    r"(?<![A-Za-z])(?P<label>" + "|".join(re.escape(l) for l in _ALL_LABELS) + r")\s*[:;#]",
    re.IGNORECASE,
)

ICD_IN_TEXT = re.compile(r"\b([A-TV-Z][0-9][0-9A-Z](?:\.[0-9A-Z]{1,4})?)\b")
DOTTED_ICD = re.compile(r"\b([A-TV-Z][0-9][0-9A-Z]\.[0-9A-Z]{1,4})\b")
PHONE = r"\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4}"
DATE = r"(?:[A-Z][a-z]{2,8}\.? \d{1,2}, \d{4}|\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{4}-\d{2}-\d{2})"

NARRATIVE_PATTERNS: dict[str, re.Pattern] = {
    "patient_name": re.compile(
        r"\b(?:my patient|the patient|patient|pt\.?)\s*,?\s+(?:(?:Mr|Mrs|Ms|Miss)\.?\s+)?"
        r"([A-Z][a-z]+(?:\s[A-Z]\.)?\s[A-Z][a-zA-Z'\-]+)"),
    "patient_dob": re.compile(r"\b(?:DOB|date of birth|born(?: on)?)\s*[:,]?\s*(" + DATE + ")",
                              re.IGNORECASE),
    "patient_phone": re.compile(r"\b(?:reached|phone|call|cell|tel)\b[^\d\n]{0,20}?(" + PHONE + ")",
                                re.IGNORECASE),
    "insurance_payer": re.compile(
        r"\b(?:insured (?:through|with|by)|covered by|insurance (?:is|carrier is))\s+"
        r"([A-Z][A-Za-z&.\- ]{2,40}?)(?=\s*(?:[,.(;]|\bwith\b|\bunder\b|\bmember\b|$))",
        re.MULTILINE),
    "insurance_member_id": re.compile(
        r"\b(?:member|subscriber|policy)\s*(?:id|#|number|no\.?)\s*(?:is\s*)?[:#]?\s*([A-Z0-9][A-Z0-9\-]{4,19})\b",
        re.IGNORECASE),
    "referring_npi": re.compile(r"\bNPI\s*(?:number|no\.?|#)?\s*(?:is\s*)?[:#]?\s*(\d{10})\b",
                                re.IGNORECASE),
    "referring_provider": re.compile(
        r"(?:Sincerely|Regards|Thank you),?\s*\n\s*((?:Dr\.\s)?[A-Z][a-z]+(?:\s[A-Z]\.)?\s[A-Z][a-zA-Z\-]+"
        r"(?:,\s?(?:MD|DO|NP|PA-C|PA|FNP))?)"),
    "reason_for_referral": re.compile(
        r"\breferr(?:ing|al)\b[^.\n]{0,80}?\bfor\s+((?:evaluation|consultation|management|"
        r"assessment|treatment)(?:[^.\n]|\.(?=\w)){3,200})", re.IGNORECASE),
}

URGENT_WORDS = re.compile(r"\b(urgent|stat|asap|expedite[d]?)\b", re.IGNORECASE)
ROUTINE_WORDS = re.compile(r"\broutine\b", re.IGNORECASE)


class RuleBasedExtractor:
    name = "rules"

    def extract(self, text: str) -> dict[str, ExtractedField]:
        fields = self._labeled_pass(text)
        self._narrative_pass(text, fields)
        self._loose_pass(text, fields)
        return fields

    # ------------------------------------------------------------------ passes
    def _labeled_pass(self, text: str) -> dict[str, ExtractedField]:
        fields: dict[str, ExtractedField] = {}
        dx_values: list[str] = []
        lines = text.splitlines()

        for i, line in enumerate(lines):
            matches = list(LABEL_RE.finditer(line))
            for j, m in enumerate(matches):
                end = matches[j + 1].start() if j + 1 < len(matches) else len(line)
                value = line[m.end():end].strip(" \t:;#-|,")
                if not value and j == len(matches) - 1 and i + 1 < len(lines) \
                        and not LABEL_RE.search(lines[i + 1]):
                    value = lines[i + 1].strip()          # value wrapped onto the next line
                if not value:
                    continue
                fld = _LABEL_TO_FIELD[re.sub(r"\s+", " ", m.group("label").lower())]
                if fld == "diagnosis_codes":
                    dx_values.append(value)
                elif fld not in fields:                   # first occurrence wins
                    fields[fld] = ExtractedField(fld, value, LABELED, "rules", value)

        codes = [c for v in dx_values for c in ICD_IN_TEXT.findall(v.upper())]
        if codes:
            fields["diagnosis_codes"] = ExtractedField(
                "diagnosis_codes", list(dict.fromkeys(codes)), LABELED, "rules", "; ".join(dx_values))
        return fields

    def _narrative_pass(self, text: str, fields: dict[str, ExtractedField]) -> None:
        # Letters wrap mid-sentence, so sentence patterns run on a single-line copy.
        # The signature pattern needs the real line breaks.
        flat = re.sub(r"[ \t]*\n[ \t]*", " ", text)
        for fld, pattern in NARRATIVE_PATTERNS.items():
            if fld in fields:
                continue
            m = pattern.search(text if fld == "referring_provider" else flat)
            if m:
                value = m.group(1).strip()
                fields[fld] = ExtractedField(fld, value, NARRATIVE, "rules", m.group(0).strip())

        if "urgency" not in fields:
            if (m := URGENT_WORDS.search(text)):
                fields["urgency"] = ExtractedField("urgency", "urgent", NARRATIVE, "rules", m.group(0))
            elif (m := ROUTINE_WORDS.search(text)):
                fields["urgency"] = ExtractedField("urgency", "routine", NARRATIVE, "rules", m.group(0))

    def _loose_pass(self, text: str, fields: dict[str, ExtractedField]) -> None:
        if "diagnosis_codes" not in fields:
            # The dotted ICD-10 shape (letter, digit, char, '.', 1-4 chars) rarely occurs by
            # accident, so an unlabeled dotted code still earns narrative-level confidence.
            codes = list(dict.fromkeys(DOTTED_ICD.findall(text)))
            if codes:
                fields["diagnosis_codes"] = ExtractedField("diagnosis_codes", codes, NARRATIVE, "rules",
                                                           ", ".join(codes))
