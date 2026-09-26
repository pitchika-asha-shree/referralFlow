"""A tiny declarative rules engine. First matching rule wins; otherwise the default route.

Condition grammar (YAML):
    {field: urgency, op: equals, value: urgent}
    {all: [cond, cond]}   {any: [cond, cond]}   {not: cond}

Ops work on scalars and on lists (e.g. diagnosis_codes): for a list, the
condition matches if ANY element matches.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

VALID_OPS = {"equals", "not_equals", "in", "contains", "starts_with", "exists", "missing"}


@dataclass(frozen=True)
class Route:
    queue: str
    priority: str = "normal"


@dataclass
class RoutingRule:
    name: str
    when: dict
    route: Route


@dataclass(frozen=True)
class RouteDecision:
    queue: str
    priority: str
    rule: str   # name of the matching rule, or "default"


def _norm(v: Any) -> str:
    return str(v).strip().lower()


def _match_scalar(op: str, actual: Any, expected: Any) -> bool:
    a = _norm(actual)
    if op == "equals":
        return a == _norm(expected)
    if op == "not_equals":
        return a != _norm(expected)
    if op == "in":
        return a in {_norm(e) for e in expected}
    if op == "contains":
        return _norm(expected) in a
    if op == "starts_with":
        prefixes = expected if isinstance(expected, list) else [expected]
        return any(a.startswith(_norm(p)) for p in prefixes)
    raise ValueError(f"unknown op {op}")


def evaluate(cond: dict, values: dict[str, Any]) -> bool:
    if "all" in cond:
        return all(evaluate(c, values) for c in cond["all"])
    if "any" in cond:
        return any(evaluate(c, values) for c in cond["any"])
    if "not" in cond:
        return not evaluate(cond["not"], values)

    op, present = cond["op"], cond["field"] in values and values[cond["field"]] not in (None, "", [])
    if op == "exists":
        return present
    if op == "missing":
        return not present
    if not present:
        return op == "not_equals"
    actual = values[cond["field"]]
    if isinstance(actual, list):
        if op == "not_equals":
            return all(_match_scalar(op, a, cond["value"]) for a in actual)
        return any(_match_scalar(op, a, cond["value"]) for a in actual)
    return _match_scalar(op, actual, cond["value"])


def route(values: dict[str, Any], rules: list[RoutingRule], default: Route) -> RouteDecision:
    for rule in rules:
        if evaluate(rule.when, values):
            return RouteDecision(rule.route.queue, rule.route.priority, rule.name)
    return RouteDecision(default.queue, default.priority, "default")
