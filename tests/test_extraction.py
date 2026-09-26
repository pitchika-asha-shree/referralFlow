import json

from app.extraction.llm import HybridExtractor, LLMExtractor
from app.extraction.rules import LABELED, NARRATIVE, RuleBasedExtractor
from tests.helpers import CLEAN_TEXT

rules = RuleBasedExtractor()


def values(fields):
    return {k: v.value for k, v in fields.items()}


def test_form_with_two_labels_per_line():
    f = rules.extract(CLEAN_TEXT)
    v = values(f)
    assert v["patient_name"] == "Doe, Jane"
    assert v["patient_dob"] == "03/14/1962"
    assert v["insurance_payer"] == "Aetna PPO"
    assert v["insurance_member_id"] == "W123456789"
    assert v["referring_npi"] == "1234567893"
    assert v["diagnosis_codes"] == ["M17.11"]
    assert f["patient_name"].confidence == LABELED


def test_label_variants_and_ocr_noise():
    text = "Pt Name; ROBERT KLINE\nSubscriber ID: UHC-88213377\nNPI #: 1234567893\nDx: M54.16, m51.26"
    v = values(rules.extract(text))
    assert v["patient_name"] == "ROBERT KLINE"          # ';' read by OCR instead of ':'
    assert v["insurance_member_id"] == "UHC-88213377"
    assert v["referring_npi"] == "1234567893"
    assert v["diagnosis_codes"] == ["M54.16", "M51.26"]


def test_value_wrapped_to_next_line():
    v = values(rules.extract("Reason for Referral:\nChronic knee pain, failed PT\nUrgency: routine"))
    assert v["reason_for_referral"] == "Chronic knee pain, failed PT"


def test_first_occurrence_wins():
    v = values(rules.extract("Patient Name: Jane Doe\nPhone: 555-201-3344\n"
                             "Referring Provider: Dr. X\nPhone: 555-999-0000"))
    assert v["patient_phone"] == "555-201-3344"


def test_prose_word_patient_is_not_a_label():
    v = values(rules.extract("The patient was seen on Monday."))
    assert "patient_name" not in v


def test_narrative_letter():
    text = ("I am referring my patient, Marcus Webb (DOB 07/22/1958), for evaluation and\n"
            "management of lumbar spinal stenosis (M48.061). He is insured through Blue Cross\n"
            "Blue Shield, member ID XJH448120973. He can be reached at (555) 331-9012.\n"
            "Sincerely,\nDr. Hannah Brooks, MD\nNPI: 1234567893")
    f = rules.extract(text)
    v = values(f)
    assert v["patient_name"] == "Marcus Webb"
    assert v["patient_dob"] == "07/22/1958"
    assert v["insurance_payer"] == "Blue Cross Blue Shield"
    assert v["insurance_member_id"] == "XJH448120973"
    assert v["patient_phone"] == "(555) 331-9012"
    assert v["referring_provider"] == "Dr. Hannah Brooks, MD"
    assert v["diagnosis_codes"] == ["M48.061"]
    assert v["reason_for_referral"].startswith("evaluation and management of lumbar spinal stenosis")
    assert f["patient_name"].confidence == NARRATIVE
    assert f["referring_npi"].confidence == LABELED


def test_sender_fax_number_is_not_taken_as_patient_phone():
    v = values(rules.extract("Oak Valley Clinic   Fax: (555) 200-1000\nPatient Name: Tom Nowak"))
    assert "patient_phone" not in v


def test_urgency_keywords():
    assert values(rules.extract("Please see STAT."))["urgency"] == "urgent"
    assert values(rules.extract("Routine follow-up."))["urgency"] == "routine"
    assert "urgency" not in values(rules.extract("Nothing here"))


def test_hybrid_asks_llm_only_for_gaps():
    prompts = []

    def fake_llm(prompt):
        prompts.append(prompt)
        return "```json\n" + json.dumps({"insurance_payer": "Cigna", "patient_name": "WRONG"}) + "\n```"

    hybrid = HybridExtractor(rules, LLMExtractor(fake_llm))
    f = hybrid.extract("Patient Name: Jane Doe\nDOB: 03/14/1962")
    assert f["insurance_payer"].value == "Cigna" and f["insurance_payer"].source == "llm"
    assert f["patient_name"].value == "Jane Doe"       # confident rule value is never overwritten
    header = prompts[0].split("Document:")[0]
    assert "insurance_payer" in header and "patient_name" not in header


def test_hybrid_survives_llm_failures():
    def broken(prompt):
        raise TimeoutError("provider down")

    f = HybridExtractor(rules, LLMExtractor(broken)).extract("Patient Name: Jane Doe")
    assert f["patient_name"].value == "Jane Doe"
    assert LLMExtractor(lambda p: "not json").extract("x") == {}
