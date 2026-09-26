"""Per-client configuration.

Onboarding a new clinic should be a YAML file, not a code change. The loader is
strict: a typo in a routing rule fails loudly at load time (and in CI via
`python -m app.cli lint-configs`) instead of silently misrouting referrals.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from app.extraction.base import CANONICAL_FIELDS
from app.routing import VALID_OPS, RoutingRule, Route


class ConfigError(Exception):
    pass


@dataclass
class WebhookConfig:
    url: str
    secret: str
    max_attempts: int = 5
    timeout_seconds: float = 10.0


@dataclass
class ClientConfig:
    client_id: str
    name: str
    required_fields: list[str]
    min_confidence: float
    webhook: WebhookConfig
    routing_rules: list[RoutingRule] = field(default_factory=list)
    default_route: Route = field(default_factory=lambda: Route("general-intake", "normal"))
    field_map: dict[str, str] = field(default_factory=dict)   # canonical -> client's EHR key

    def summary(self) -> dict:
        return {
            "client_id": self.client_id,
            "name": self.name,
            "required_fields": self.required_fields,
            "min_confidence": self.min_confidence,
            "webhook_url": self.webhook.url,
            "routing_rules": [r.name for r in self.routing_rules],
            "default_route": {"queue": self.default_route.queue,
                              "priority": self.default_route.priority},
        }


def _require(d: dict, key: str, ctx: str) -> Any:
    if key not in d:
        raise ConfigError(f"{ctx}: missing required key '{key}'")
    return d[key]


def _check_condition(cond: Any, ctx: str) -> None:
    if not isinstance(cond, dict):
        raise ConfigError(f"{ctx}: condition must be a mapping, got {type(cond).__name__}")
    combinators = {"all", "any", "not"} & cond.keys()
    if combinators:
        key = combinators.pop()
        children = cond[key] if key != "not" else [cond[key]]
        if not isinstance(children, list) or not children:
            raise ConfigError(f"{ctx}: '{key}' needs a non-empty list")
        for i, c in enumerate(children):
            _check_condition(c, f"{ctx}.{key}[{i}]")
        return
    fld = _require(cond, "field", ctx)
    op = _require(cond, "op", ctx)
    if fld not in CANONICAL_FIELDS:
        raise ConfigError(f"{ctx}: unknown field '{fld}' (valid: {', '.join(CANONICAL_FIELDS)})")
    if op not in VALID_OPS:
        raise ConfigError(f"{ctx}: unknown op '{op}' (valid: {', '.join(sorted(VALID_OPS))})")
    if op not in {"exists", "missing"} and "value" not in cond:
        raise ConfigError(f"{ctx}: op '{op}' needs a 'value'")


def parse_client_config(raw: dict, source: str = "<config>") -> ClientConfig:
    if not isinstance(raw, dict):
        raise ConfigError(f"{source}: top level must be a mapping")
    client_id = _require(raw, "client_id", source)

    required = raw.get("required_fields", [])
    unknown = [f for f in required if f not in CANONICAL_FIELDS]
    if unknown:
        raise ConfigError(f"{source}: unknown required_fields {unknown}")

    min_conf = float(raw.get("min_confidence", 0.8))
    if not 0 <= min_conf <= 1:
        raise ConfigError(f"{source}: min_confidence must be between 0 and 1")

    wh = _require(raw, "webhook", source)
    secret_env = _require(wh, "secret_env", f"{source}.webhook")
    # Secrets never live in the YAML; the file only names the env var. A dev default
    # keeps the demo runnable, and the /v1/clients endpoint never exposes it.
    secret = os.getenv(secret_env, f"dev-secret-{client_id}")
    webhook = WebhookConfig(
        url=os.path.expandvars(_require(wh, "url", f"{source}.webhook")),
        secret=secret,
        max_attempts=int(wh.get("max_attempts", 5)),
        timeout_seconds=float(wh.get("timeout_seconds", 10)),
    )

    routing = raw.get("routing", {}) or {}
    rules = []
    for i, r in enumerate(routing.get("rules", []) or []):
        ctx = f"{source}.routing.rules[{i}]"
        name = _require(r, "name", ctx)
        when = _require(r, "when", ctx)
        _check_condition(when, f"{ctx}.when")
        rules.append(RoutingRule(name=name, when=when, route=Route(
            queue=_require(r, "queue", ctx), priority=r.get("priority", "normal"))))
    default = routing.get("default", {}) or {}
    default_route = Route(default.get("queue", "general-intake"), default.get("priority", "normal"))

    field_map = raw.get("field_map", {}) or {}
    bad_map = [k for k in field_map if k not in CANONICAL_FIELDS]
    if bad_map:
        raise ConfigError(f"{source}: field_map has unknown fields {bad_map}")

    return ClientConfig(
        client_id=client_id, name=raw.get("name", client_id), required_fields=list(required),
        min_confidence=min_conf, webhook=webhook, routing_rules=rules,
        default_route=default_route, field_map=field_map,
    )


def load_client_configs(directory: str | Path) -> dict[str, ClientConfig]:
    configs: dict[str, ClientConfig] = {}
    for path in sorted(Path(directory).glob("*.y*ml")):
        with open(path) as fh:
            try:
                raw = yaml.safe_load(fh)
            except yaml.YAMLError as exc:
                raise ConfigError(f"{path.name}: invalid YAML: {exc}") from exc
        cfg = parse_client_config(raw, path.name)
        if cfg.client_id in configs:
            raise ConfigError(f"{path.name}: duplicate client_id '{cfg.client_id}'")
        configs[cfg.client_id] = cfg
    return configs
