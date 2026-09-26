from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ReviewRequest(BaseModel):
    reviewer: str = Field(min_length=1, examples=["jane.ops"])
    corrections: dict[str, Any] = Field(
        examples=[{"insurance_member_id": "CIG5521907", "referring_npi": "1234567893"}])


class RedeliverRequest(BaseModel):
    actor: str = Field(min_length=1)


class ResolveTicketRequest(BaseModel):
    actor: str = Field(min_length=1)
    note: str = ""


class InboundFax(BaseModel):
    client_id: str
    filename: str = "fax.pdf"
    content_base64: str
