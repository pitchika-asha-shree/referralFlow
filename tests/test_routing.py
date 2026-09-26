from app.routing import Route, RoutingRule, evaluate, route

VALUES = {"urgency": "Urgent", "diagnosis_codes": ["M54.16", "M51.26"],
          "insurance_payer": "Medicare Part B", "reason_for_referral": "CPAP machine"}


def test_scalar_ops_are_case_insensitive():
    assert evaluate({"field": "urgency", "op": "equals", "value": "urgent"}, VALUES)
    assert evaluate({"field": "insurance_payer", "op": "contains", "value": "MEDICARE"}, VALUES)
    assert evaluate({"field": "urgency", "op": "in", "value": ["urgent", "stat"]}, VALUES)
    assert evaluate({"field": "urgency", "op": "not_equals", "value": "routine"}, VALUES)


def test_list_fields_match_any_element():
    assert evaluate({"field": "diagnosis_codes", "op": "starts_with", "value": ["M17", "M51"]}, VALUES)
    assert not evaluate({"field": "diagnosis_codes", "op": "starts_with", "value": "G47"}, VALUES)
    assert evaluate({"field": "diagnosis_codes", "op": "not_equals", "value": "G47.33"}, VALUES)


def test_exists_missing_and_absent_fields():
    assert evaluate({"field": "urgency", "op": "exists"}, VALUES)
    assert evaluate({"field": "patient_phone", "op": "missing"}, VALUES)
    assert not evaluate({"field": "patient_phone", "op": "equals", "value": "x"}, VALUES)


def test_combinators():
    cond = {"all": [{"field": "insurance_payer", "op": "contains", "value": "medicare"},
                    {"any": [{"field": "reason_for_referral", "op": "contains", "value": "cpap"},
                             {"field": "diagnosis_codes", "op": "starts_with", "value": "G47"}]},
                    {"not": {"field": "urgency", "op": "equals", "value": "routine"}}]}
    assert evaluate(cond, VALUES)


def test_first_match_wins_then_default():
    rules = [
        RoutingRule("spine", {"field": "diagnosis_codes", "op": "starts_with", "value": "M5"},
                    Route("spine")),
        RoutingRule("urgent", {"field": "urgency", "op": "equals", "value": "urgent"},
                    Route("urgent", "high")),
    ]
    assert route(VALUES, rules, Route("general")).rule == "spine"
    d = route({"urgency": "routine"}, rules, Route("general", "low"))
    assert (d.queue, d.priority, d.rule) == ("general", "low", "default")
