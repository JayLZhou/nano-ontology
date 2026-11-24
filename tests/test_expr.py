import pytest

from nano_onto import expr
from nano_onto.expr import ExprError


ENV = {
    "params": {"amount": 60000, "supplier": {"rating": "C", "blacklisted": False}, "tags": ["a", "b"]},
    "actor": {"id": "alice", "role": "employee"},
}


@pytest.mark.parametrize(
    "source, expected",
    [
        ("params.amount > 50000", True),
        ("params.amount > 50000 and actor.role == 'cfo'", False),
        ("params.supplier.rating in ['A', 'B']", False),
        ("not params.supplier.blacklisted", True),
        ("params.amount * 2 >= 120000", True),
        ("10000 < params.amount <= 60000", True),
        ("len(params.tags) == 2", True),
        ("params.missing > 5", False),  # missing -> None, ordering with None is False
        ("params.missing == None", True),
        ("lower('ABC') == 'abc'", True),
        ("'x' if params.amount > 1 else 'y'", "x"),
        ("params.tags[0]", "a"),
        ("params.tags[9]", None),
    ],
)
def test_evaluate(source, expected):
    assert expr.evaluate(source, ENV) == expected


@pytest.mark.parametrize(
    "source",
    [
        "__import__('os').system('echo hi')",
        "params.__class__",
        "open('x')",
        "[x for x in params]",
        "lambda: 1",
        "params.amount = 1",
        "f(x=1)",
        "",
    ],
)
def test_rejects_unsafe_or_invalid(source):
    with pytest.raises(ExprError):
        expr.evaluate(source, ENV)


def test_unknown_root_name():
    with pytest.raises(ExprError):
        expr.evaluate("secret > 1", ENV)


def test_today_can_be_pinned(monkeypatch):
    monkeypatch.setenv("NANO_ONTO_TODAY", "2026-12-02")
    assert expr.evaluate("today() >= '2026-12-01'", ENV) is True


def test_references():
    refs = expr.references("params.po.requester != actor.id and len(params.items) > 0")
    assert refs == {("params", "po", "requester"), ("actor", "id"), ("params", "items")}
