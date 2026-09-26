"""Operator CLI.

    python -m app.cli lint-configs                 # validate every client YAML (runs in CI)
    python -m app.cli ingest acme_ortho samples/*.pdf
    python -m app.cli list --status needs_review
    python -m app.cli retry                        # run the webhook outbox once
    python -m app.cli tickets
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from app.client_config import ConfigError, load_client_configs
from app.db import connect
from app.extraction.llm import build_extractor
from app.pipeline import IntakePipeline
from app.repository import Repository
from app.settings import get_settings



def _pipeline() -> IntakePipeline:
    s = get_settings()
    return IntakePipeline(Repository(connect(s.database_path)),
                          load_client_configs(s.configs_dir), extractor=build_extractor())


def cmd_lint(args) -> int:
    directory = args.dir or get_settings().configs_dir
    try:
        configs = load_client_configs(directory)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 1
    for cfg in configs.values():
        print(f"ok  {cfg.client_id:14} {len(cfg.routing_rules)} rule(s), "
              f"{len(cfg.required_fields)} required field(s) -> {cfg.webhook.url}")
    return 0


def cmd_ingest(args) -> int:
    p = _pipeline()
    for path in args.files:
        res = p.ingest(args.client, Path(path).name, Path(path).read_bytes(), actor="cli")
        r = res.referral
        tag = "duplicate" if res.duplicate else r.status.value
        print(f"{r.id}  {Path(path).name:28} {tag:16} queue={r.queue or '-'}")
        for i in r.blocking_issues:
            print(f"    ! {i.field}: {i.message}")
    return 0


def cmd_list(args) -> int:
    repo = _pipeline().repo
    for r in repo.list_referrals(status=args.status, client_id=args.client):
        print(f"{r.id}  {r.client_id:12} {r.status.value:16} {r.queue or '-':22} {r.filename}")
    return 0


def cmd_retry(args) -> int:
    for o in _pipeline().process_due_deliveries():
        print(f"{o.delivery_id}  {o.referral_id}  {o.outcome}  {o.status_code}")
    return 0


def cmd_tickets(args) -> int:
    for t in _pipeline().repo.list_tickets(status=args.status):
        print(json.dumps(t.to_dict(), indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="referralflow")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("lint-configs")
    a.add_argument("dir", nargs="?")
    a.set_defaults(fn=cmd_lint)
    a = sub.add_parser("ingest")
    a.add_argument("client")
    a.add_argument("files", nargs="+")
    a.set_defaults(fn=cmd_ingest)
    a = sub.add_parser("list")
    a.add_argument("--status")
    a.add_argument("--client")
    a.set_defaults(fn=cmd_list)
    a = sub.add_parser("retry")
    a.set_defaults(fn=cmd_retry)
    a = sub.add_parser("tickets")
    a.add_argument("--status", default="open")
    a.set_defaults(fn=cmd_tickets)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
