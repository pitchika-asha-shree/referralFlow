from __future__ import annotations

from typing import Protocol

from app.models import ExtractedField

# The canonical schema every extractor fills. Client configs pick which are required
# and how they are renamed on the way out (field_map).
CANONICAL_FIELDS = (
    "patient_name",
    "patient_dob",
    "patient_phone",
    "insurance_payer",
    "insurance_member_id",
    "referring_provider",
    "referring_npi",
    "diagnosis_codes",
    "reason_for_referral",
    "urgency",
)


class Extractor(Protocol):
    name: str

    def extract(self, text: str) -> dict[str, ExtractedField]:
        """Return whatever fields could be found. Missing fields are simply absent."""
        ...
