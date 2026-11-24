"""Regression tests for holes found while auditing 0.1.0."""

import pytest

from conftest import ALICE, BOB, CAROL, make_engine, raw_schema, submit
from nano_onto import ActionError, Actor, Engine, ExprError, Ontology, SchemaError, Store, expr


def outcomes(engine):
    return [(r["actor"], r["event"], r["outcome"]) for r in engine.store.audit_log()]


# ------------------------------------------------------------------ parameters
@pytest.mark.parametrize("amount", ["nan", "NaN", "inf", "-inf", float("nan"), float("inf"), "1e999", True])
def test_non_finite_amounts_cannot_slip_past_thresholds(engine, amount):
    # NaN compares False with everything: it used to pass every rule and create the order
    res = submit(engine, amount)
    assert res.status == "invalid"
    assert engine.store.count("PurchaseOrder") == 2


def test_params_must_be_an_object(engine):
    assert engine.execute("submit_purchase_order", ["x"], ALICE).status == "invalid"
    assert engine.execute("submit_purchase_order", {"title": {"a": 1}, "amount": 1, "supplier": "S-001"},
                          ALICE).status == "invalid"


def test_unknown_role_is_denied(engine):
    res = engine.execute("blacklist_supplier", {"supplier": "S-001"}, Actor("mallory", "admin"))
    assert res.status == "denied" and "unknown role" in res.messages[0]


def test_empty_allowed_roles_means_nobody():
    eng = make_engine(lambda d: d["action_types"]["blacklist_supplier"].update(allowed_roles=[]))
    assert eng.execute("blacklist_supplier", {"supplier": "S-001"}, CAROL).status == "denied"


# ---------------------------------------------------------------------- schema
def _mutations():
    acts = lambda d: d["action_types"]  # noqa: E731
    return [
        # a typo must not silently open the action to every role
        (lambda d: acts(d)["blacklist_supplier"].update(allowed_role=["cfo"]), "unknown key 'allowed_role'"),
        (lambda d: acts(d)["submit_purchase_order"]["rules"][0].update(wen="True"), "unknown key 'wen'"),
        (lambda d: d["object_types"]["Supplier"]["properties"]["rating"].update(enumm=[1]), "unknown key 'enumm'"),
        (lambda d: d.update(actions={}), "unknown key 'actions'"),
        # used to crash with KeyError instead of reporting
        (lambda d: acts(d)["blacklist_supplier"]["parameters"]["supplier"].update(object_type="Vendor"),
         "unknown object type 'Vendor'"),
        (lambda d: d["object_types"].update(Supplier="oops"), "expected a mapping"),
        (lambda d: acts(d)["blacklist_supplier"].update(rules="oops"), "expected a list"),
        (lambda d: acts(d)["blacklist_supplier"].update(rules=["oops"]), "expected a mapping"),
        (lambda d: acts(d)["blacklist_supplier"].update(allowed_roles="cfo"), "expected a list of role names"),
        (lambda d: acts(d)["submit_purchase_order"]["rules"][2].update(then={"require_approval": ["cfo"]}),
         "'then' must be"),
        # effects that could never work
        (lambda d: acts(d)["submit_purchase_order"]["effects"][0]["set"].pop("title"),
         "does not set required property PurchaseOrder.title"),
        (lambda d: acts(d)["cancel_purchase_order"]["effects"][0]["set"].update(po_id="'X'"), "cannot be updated"),
        (lambda d: acts(d)["submit_purchase_order"]["effects"][0].update({"as": "params"}), "alias 'params'"),
        (lambda d: acts(d)["submit_purchase_order"]["effects"].append({"create": "Supplier", "link": "supplier"}),
         "exactly one of"),
        (lambda d: acts(d)["submit_purchase_order"]["effects"].append({"webhook": "nope"}), "unknown webhook"),
        # names and values
        (lambda d: acts(d).update(onto_query=acts(d)["blacklist_supplier"]), "not start with 'onto_'"),
        (lambda d: acts(d)["submit_purchase_order"]["rules"].append(dict(acts(d)["submit_purchase_order"]["rules"][0])),
         "duplicate rule id"),
        (lambda d: d["object_types"]["Supplier"]["properties"]["rating"].update(default="Z"), "not in enum"),
        (lambda d: d["object_types"]["PurchaseOrder"]["properties"]["amount"].update(default="lots"), "is not a number"),
        (lambda d: d["object_types"]["Supplier"].update(readable_by=["ceo"]), "unknown role 'ceo'"),
        (lambda d: d["object_types"]["Supplier"].update(visible_when="obj.colour == 1"), "no property or link 'colour'"),
        (lambda d: d["object_types"]["Supplier"].update(visible_when="params.x == 1"), "unknown name 'params'"),
        (lambda d: d["object_types"]["Supplier"]["properties"]["supplier_id"].update(readable_by=["cfo"]),
         "primary key cannot be restricted"),
        (lambda d: d["object_types"]["Supplier"].update(source={"table": "s; DROP TABLE x"}), "not a valid table name"),
        (lambda d: d.update(webhooks={"erp": {"url": "file:///etc/passwd"}}), "needs an http(s) 'url'"),
    ]


@pytest.mark.parametrize("mutate, message", _mutations())
def test_schema_mistakes_are_reported_not_ignored(mutate, message):
    data = raw_schema()
    mutate(data)
    with pytest.raises(SchemaError) as err:
        Ontology.from_dict(data)
    assert message in str(err.value)


def test_invalid_yaml_is_a_schema_error(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text("name: [unclosed", encoding="utf-8")
    with pytest.raises(SchemaError):
        Ontology.load(path)


def test_rule_source_may_be_a_plain_string():
    eng = make_engine(lambda d: d["action_types"]["submit_purchase_order"]["rules"][0].update(source="制度.md"))
    assert submit(eng, 1, supplier="S-003").trace[0]["source"] == {"doc": "制度.md"}


# ----------------------------------------------------------------- expressions
def test_expressions_cannot_build_huge_values():
    with pytest.raises(ExprError):
        expr.evaluate("'x' * 1000000000", {})
    with pytest.raises(ExprError):
        expr.evaluate("[1] + [2]", {})
    assert expr.evaluate("'a' + 'b'", {}) == "ab" and expr.evaluate("7 // 2 + 1.5", {}) == 4.5


# ------------------------------------------------------------------- approvals
def _with_explicit_id(data):
    action = data["action_types"]["submit_purchase_order"]
    action["parameters"]["po_id"] = {"type": "string"}
    action["effects"][0]["set"]["po_id"] = "params.po_id"


def test_approved_request_that_cannot_run_is_closed_not_stuck():
    eng = make_engine(_with_explicit_id)
    res = submit(eng, 20000, po_id="PO-0001")  # that id is taken, which only shows when the effects run
    final = eng.approve(res.request_id, BOB)
    assert final.status == "invalid" and "already exists" in final.messages[-1]
    req = eng.store.get_approval(res.request_id)
    assert req["status"] == "failed" and req["approved_by"] == {"dept_manager": "bob"}
    assert eng.pending() == []  # it used to stay pending forever, waiting for "nobody"
    assert outcomes(eng)[-1] == ("bob", "approve", "invalid")


def test_refused_approval_attempts_are_audited(engine):
    res = submit(engine, 60000)
    for actor, text in ((Actor("alice", "cfo"), "separation of duties"), (Actor("eve", "employee"), "no approval request")):
        with pytest.raises(ActionError, match=text):
            engine.approve(res.request_id, actor)
    with pytest.raises(ActionError):
        engine.reject("REQ-NOPE", BOB)
    assert outcomes(engine)[-3:] == [("alice", "approve", "refused"), ("eve", "approve", "refused"),
                                     ("bob", "reject", "refused")]
    assert engine.store.get_approval(res.request_id)["approved_by"] == {}


def test_request_status_is_visible_to_those_involved(engine):
    res = submit(engine, 20000)
    assert engine.request(res.request_id, ALICE)["status"] == "pending"
    assert engine.request(res.request_id, BOB)["required_roles"] == ["dept_manager"]
    assert engine.request(res.request_id, Actor("eve", "employee")) is None
    engine.approve(res.request_id, BOB)
    assert engine.request(res.request_id, ALICE)["status"] == "executed"


# --------------------------------------------------------------------- effects
def test_failed_effects_roll_back_everything_and_are_audited():
    def broken(data):  # second effect links to a supplier id that does not exist
        data["action_types"]["submit_purchase_order"]["effects"][1]["to"] = "'S-404'"

    eng = make_engine(broken)
    res = submit(eng, 100)
    assert res.status == "invalid" and "rolled back" in res.messages[-1] and "S-404" in res.messages[-1]
    assert res.created == {} and eng.store.count("PurchaseOrder") == 2
    assert outcomes(eng) == [("alice", "execute", "invalid")]


def test_link_cardinality_is_enforced_on_both_ends():
    def one_to_one(data):
        data["link_types"]["supplier"]["cardinality"] = "one_to_one"

    eng = make_engine(one_to_one)
    first, second = submit(eng, 1).created["po"], submit(eng, 2).created["po"]
    assert eng.store.links_to("supplier", "S-001") == [second]  # the supplier's previous order was unlinked
    assert eng.store.links_from("supplier", first) == []


def test_two_processes_on_one_database_stay_consistent(tmp_path):
    path = str(tmp_path / "shared.db")
    one = make_engine(store=Store(path))
    two = Engine(one.onto, Store(path))
    ids = [submit(eng, 10).created["po"] for eng in (one, two, one, two)]
    assert ids == ["PO-0003", "PO-0004", "PO-0005", "PO-0006"]
    assert [r["seq"] for r in two.store.audit_log()] == [1, 2, 3, 4]
    req = submit(one, 60000).request_id
    two.approve(req, BOB)
    assert one.approve(req, CAROL).status == "executed"  # sees the approval recorded by the other process
