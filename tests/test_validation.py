from datetime import date

import pytest

from app.models import ExtractedField, Severity
from app.validation import (
    normalize_date, normalize_icd10, normalize_icd10_list, normalize_member_id, normalize_name,
    normalize_npi, normalize_phone, normalize_urgency, npi_is_valid, validate_fields,
)

TODAY = date(2026, 9, 1)


def test_npi_check_digit():
    assert npi_is_valid("1234567893")          # CMS's published example
    assert not npi_is_valid("1234567890")      # one digit off
    assert not npi_is_valid("123456789")       # too short
    assert normalize_npi("NPI 1234-567-893") == "1234567893"
    with pytest.raises(ValueError, match="check-digit"):
        normalize_npi("1234567890")


def test_icd10_normalization():
    assert normalize_icd10("m17.11") == "M17.11"
    assert normalize_icd10("M545") == "M54.5"           # dot inserted
    assert normalize_icd10("G47.33") == "G47.33"
    for bad in ("U07.1", "17.11", "MM1.2", "M17.123456"):
        with pytest.raises(ValueError):
            normalize_icd10(bad)
    assert normalize_icd10_list("M17.11, M17.11; G47.33") == ["M17.11", "G47.33"]


def test_dates():
    assert normalize_date("03/14/1962", TODAY) == "1962-03-14"
    assert normalize_date("1975-11-02", TODAY) == "1975-11-02"
    assert normalize_date("July 22, 1958", TODAY) == "1958-07-22"
    assert normalize_date("5/9/49", TODAY) == "1949-05-09"   # two-digit year -> past century
    assert normalize_date("1/2/03", TODAY) == "2003-01-02"
    with pytest.raises(ValueError):
        normalize_date("12/31/2030", TODAY)
    with pytest.raises(ValueError):
        normalize_date("not a date", TODAY)


def test_phone_name_member_urgency():
    assert normalize_phone("(555) 201-3344") == "+15552013344"
    assert normalize_phone("1-555-201-3344") == "+15552013344"
    with pytest.raises(ValueError):
        normalize_phone("201-3344")
    assert normalize_name("DOE, JANE") == "Jane Doe"
    assert normalize_name("robert kline") == "Robert Kline"
    assert normalize_name("Mary-Kate O'Neil") == "Mary-Kate O'Neil"
    with pytest.raises(ValueError):
        normalize_name("Jane")
    assert normalize_member_id("uhc 882 133") == "UHC882133"
    assert normalize_urgency("URGENT - within 48 hrs") == "urgent"
    assert normalize_urgency("Routine") == "routine"


def test_normalizers_are_idempotent():
    """Review re-validates already-normalized values, so f(f(x)) must equal f(x)."""
    cases = [(normalize_date, "03/14/1962"), (normalize_phone, "(555) 201-3344"),
             (normalize_npi, "1234567893"), (normalize_name, "DOE, JANE"),
             (normalize_icd10_list, "M545"), (normalize_member_id, "w123 456"),
             (normalize_urgency, "STAT")]
    for fn, raw in cases:
        once = fn(raw)
        assert fn(once) == once, fn.__name__


def test_validate_fields_reports_each_problem_kind():
    fields = {
        "patient_name": ExtractedField("patient_name", "Jane Doe", 0.95),
        "patient_dob": ExtractedField("patient_dob", "03/14/1962", 0.6),        # low confidence
        "referring_npi": ExtractedField("referring_npi", "1234567890", 0.95),   # bad check digit
        "patient_phone": ExtractedField("patient_phone", "12", 0.95),           # optional + bad
    }
    required = ["patient_name", "patient_dob", "referring_npi", "insurance_member_id"]
    out, issues = validate_fields(fields, required, 0.8)
    by_field = {(i.field, i.code): i for i in issues}
    assert by_field[("patient_dob", "low_confidence")].severity == Severity.ERROR
    assert by_field[("referring_npi", "invalid_format")].severity == Severity.ERROR
    assert by_field[("insurance_member_id", "missing_required")].severity == Severity.ERROR
    assert by_field[("patient_phone", "invalid_format")].severity == Severity.WARNING
    assert out["patient_dob"].value == "1962-03-14"


def test_human_values_skip_confidence_check():
    fields = {"patient_dob": ExtractedField("patient_dob", "03/14/1962", 0.1, source="human")}
    _, issues = validate_fields(fields, ["patient_dob"], 0.9)
    assert issues == []
