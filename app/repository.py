"""Data access. All SQL lives here; the pipeline never writes SQL directly."""
from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import asdict
from datetime import datetime

from app.models import (
    Delivery, DeliveryStatus, ExtractedField, Referral, ReferralStatus, Ticket,
    TicketCategory, TicketStatus, ValidationIssue,
)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


class Repository:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # ---------------------------------------------------------------- referrals
    def insert_referral(self, r: Referral) -> None:
        with self.conn:
            self.conn.execute(
                """INSERT INTO referrals (id, client_id, filename, document_sha256, status,
                       text_method, raw_text, fields_json, issues_json, queue, priority,
                       routing_rule, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (r.id, r.client_id, r.filename, r.document_sha256, r.status.value,
                 r.text_method, r.raw_text, self._fields_json(r), self._issues_json(r),
                 r.queue, r.priority, r.routing_rule,
                 r.created_at.isoformat(), r.updated_at.isoformat()),
            )

    def update_referral(self, r: Referral) -> None:
        with self.conn:
            self.conn.execute(
                """UPDATE referrals SET status=?, text_method=?, raw_text=?, fields_json=?,
                       issues_json=?, queue=?, priority=?, routing_rule=?, updated_at=?
                   WHERE id=?""",
                (r.status.value, r.text_method, r.raw_text, self._fields_json(r),
                 self._issues_json(r), r.queue, r.priority, r.routing_rule,
                 r.updated_at.isoformat(), r.id),
            )

    def get_referral(self, referral_id: str) -> Referral | None:
        row = self.conn.execute("SELECT * FROM referrals WHERE id=?", (referral_id,)).fetchone()
        return self._row_to_referral(row) if row else None

    def find_by_hash(self, client_id: str, sha256: str) -> Referral | None:
        row = self.conn.execute(
            "SELECT * FROM referrals WHERE client_id=? AND document_sha256=?",
            (client_id, sha256)).fetchone()
        return self._row_to_referral(row) if row else None

    def list_referrals(self, status: str | None = None, client_id: str | None = None,
                       limit: int = 100) -> list[Referral]:
        sql, args = "SELECT * FROM referrals WHERE 1=1", []
        if status:
            sql += " AND status=?"
            args.append(status)
        if client_id:
            sql += " AND client_id=?"
            args.append(client_id)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        return [self._row_to_referral(r) for r in self.conn.execute(sql, args).fetchall()]

    def status_counts(self) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT status, COUNT(*) AS n FROM referrals GROUP BY status").fetchall()
        return {r["status"]: r["n"] for r in rows}

    # ------------------------------------------------------------------- events
    def add_event(self, referral_id: str, at: datetime, from_status: str | None,
                  to_status: str | None, actor: str, note: str = "") -> None:
        with self.conn:
            self.conn.execute(
                """INSERT INTO referral_events (referral_id, at, from_status, to_status, actor, note)
                   VALUES (?,?,?,?,?,?)""",
                (referral_id, at.isoformat(), from_status, to_status, actor, note))

    def list_events(self, referral_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT at, from_status, to_status, actor, note FROM referral_events "
            "WHERE referral_id=? ORDER BY id", (referral_id,)).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ tickets
    def find_open_ticket(self, referral_id: str, category: TicketCategory) -> Ticket | None:
        row = self.conn.execute(
            "SELECT * FROM tickets WHERE referral_id=? AND category=? AND status='open'",
            (referral_id, category.value)).fetchone()
        return self._row_to_ticket(row) if row else None

    def insert_ticket(self, t: Ticket) -> None:
        with self.conn:
            self.conn.execute(
                """INSERT INTO tickets (id, referral_id, client_id, category, summary, details,
                       status, created_at, resolved_at, resolution_note)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (t.id, t.referral_id, t.client_id, t.category.value, t.summary, t.details,
                 t.status.value, t.created_at.isoformat(), None, None))

    def update_ticket(self, t: Ticket) -> None:
        with self.conn:
            self.conn.execute(
                """UPDATE tickets SET summary=?, details=?, status=?, resolved_at=?,
                       resolution_note=? WHERE id=?""",
                (t.summary, t.details, t.status.value,
                 t.resolved_at.isoformat() if t.resolved_at else None, t.resolution_note, t.id))

    def get_ticket(self, ticket_id: str) -> Ticket | None:
        row = self.conn.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone()
        return self._row_to_ticket(row) if row else None

    def list_tickets(self, status: str | None = None, referral_id: str | None = None) -> list[Ticket]:
        sql, args = "SELECT * FROM tickets WHERE 1=1", []
        if status:
            sql += " AND status=?"
            args.append(status)
        if referral_id:
            sql += " AND referral_id=?"
            args.append(referral_id)
        sql += " ORDER BY created_at DESC"
        return [self._row_to_ticket(r) for r in self.conn.execute(sql, args).fetchall()]

    # --------------------------------------------------------------- deliveries
    def insert_delivery(self, d: Delivery) -> None:
        with self.conn:
            self.conn.execute(
                """INSERT INTO deliveries (id, referral_id, client_id, url, payload_json, status,
                       attempts, max_attempts, next_attempt_at, last_status_code, last_error,
                       created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (d.id, d.referral_id, d.client_id, d.url, json.dumps(d.payload), d.status.value,
                 d.attempts, d.max_attempts, d.next_attempt_at.isoformat(), d.last_status_code,
                 d.last_error, d.created_at.isoformat(), d.updated_at.isoformat()))

    def update_delivery(self, d: Delivery) -> None:
        with self.conn:
            self.conn.execute(
                """UPDATE deliveries SET status=?, attempts=?, next_attempt_at=?,
                       last_status_code=?, last_error=?, updated_at=? WHERE id=?""",
                (d.status.value, d.attempts, d.next_attempt_at.isoformat(), d.last_status_code,
                 d.last_error, d.updated_at.isoformat(), d.id))

    def due_deliveries(self, now: datetime, limit: int = 50) -> list[Delivery]:
        rows = self.conn.execute(
            "SELECT * FROM deliveries WHERE status='pending' AND next_attempt_at<=? "
            "ORDER BY next_attempt_at LIMIT ?", (now.isoformat(), limit)).fetchall()
        return [self._row_to_delivery(r) for r in rows]

    def list_deliveries(self, referral_id: str | None = None, limit: int = 100) -> list[Delivery]:
        sql, args = "SELECT * FROM deliveries", []
        if referral_id:
            sql += " WHERE referral_id=?"
            args.append(referral_id)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        return [self._row_to_delivery(r) for r in self.conn.execute(sql, args).fetchall()]

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _fields_json(r: Referral) -> str:
        return json.dumps({k: asdict(v) for k, v in r.fields.items()})

    @staticmethod
    def _issues_json(r: Referral) -> str:
        return json.dumps([i.to_dict() for i in r.issues])

    @staticmethod
    def _row_to_referral(row: sqlite3.Row) -> Referral:
        return Referral(
            id=row["id"], client_id=row["client_id"], filename=row["filename"],
            document_sha256=row["document_sha256"], status=ReferralStatus(row["status"]),
            text_method=row["text_method"], raw_text=row["raw_text"],
            fields={k: ExtractedField.from_dict(v) for k, v in json.loads(row["fields_json"]).items()},
            issues=[ValidationIssue.from_dict(i) for i in json.loads(row["issues_json"])],
            queue=row["queue"], priority=row["priority"], routing_rule=row["routing_rule"],
            created_at=_dt(row["created_at"]), updated_at=_dt(row["updated_at"]),
        )

    @staticmethod
    def _row_to_ticket(row: sqlite3.Row) -> Ticket:
        return Ticket(
            id=row["id"], referral_id=row["referral_id"], client_id=row["client_id"],
            category=TicketCategory(row["category"]), summary=row["summary"],
            details=row["details"], status=TicketStatus(row["status"]),
            created_at=_dt(row["created_at"]), resolved_at=_dt(row["resolved_at"]),
            resolution_note=row["resolution_note"],
        )

    @staticmethod
    def _row_to_delivery(row: sqlite3.Row) -> Delivery:
        return Delivery(
            id=row["id"], referral_id=row["referral_id"], client_id=row["client_id"],
            url=row["url"], payload=json.loads(row["payload_json"]),
            status=DeliveryStatus(row["status"]), attempts=row["attempts"],
            max_attempts=row["max_attempts"], next_attempt_at=_dt(row["next_attempt_at"]),
            last_status_code=row["last_status_code"], last_error=row["last_error"],
            created_at=_dt(row["created_at"]), updated_at=_dt(row["updated_at"]),
        )
