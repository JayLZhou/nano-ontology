import json

import pytest
import yaml

from conftest import BOB, PROCUREMENT, make_engine, raw_schema, submit
from nano_onto import Engine, Ontology, Store, VersionError
from nano_onto.cli import main
from nano_onto.migrate import diff, migrate


def evolved(mutate, version="0.2.0"):
    data = raw_schema()
    data["version"] = version
    mutate(data)
    return Ontology.from_dict(data)


def test_database_is_stamped_and_refuses_a_changed_ontology(tmp_path):
    path = str(tmp_path / "v.db")
    old = make_engine(store=Store(path))
    assert old.store.get_meta("ontology")["version"] == "0.1.0"
    Engine(old.onto, Store(path))  # same ontology: fine
    new = evolved(lambda d: d["action_types"]["submit_purchase_order"]["rules"][2].update(when="params.amount > 5000"))
    with pytest.raises(VersionError, match="nano-onto migrate"):
        Engine(new, Store(path))
    # annotations do not count as a change
    data = raw_schema()
    data["x-review"] = {"approved_by": "legal"}
    data["object_types"]["Supplier"]["x-note"] = "anything"
    Engine(Ontology.from_dict(data), Store(path))


def test_rule_change_is_recorded_and_takes_effect(engine):
    new = evolved(lambda d: d["action_types"]["submit_purchase_order"]["rules"][2].update(when="params.amount > 5000"))
    dry = migrate(engine.store, new, dry_run=True)
    assert not dry.applied and not dry.problems
    assert dry.changes == [
        "~ action_types.submit_purchase_order.rules.dept_manager_over_10k.when: "
        '"params.amount > 10000" -> "params.amount > 5000"',
        '~ version: "0.1.0" -> "0.2.0"',
    ]
    assert engine.store.get_meta("ontology")["version"] == "0.1.0"

    done = migrate(engine.store, new, actor="carol")
    assert done.applied
    assert submit(Engine(new, engine.store), 8000).status == "pending_approval"
    last = engine.store.history()[-1]
    assert last["event"] == "migrated" and last["from"] == "procurement@0.1.0" and last["by"] == "carol"
    assert last["ontology"] == "procurement@0.2.0" and last["changes"] == dry.changes
    assert engine.store.audit_log()[-2]["event"] == "migrate"
    assert not migrate(engine.store, new).changes  # idempotent


def test_properties_are_backfilled_renamed_and_dropped(engine):
    def change(data):
        props = data["object_types"]["Supplier"]["properties"]
        props["tier"] = {"type": "string", "renamed_from": "rating", "enum": ["A", "B", "C"]}
        del props["rating"]
        del props["bank_account"]
        props["country"] = {"type": "string", "default": "CN", "required": True}
        data["action_types"]["submit_purchase_order"]["rules"][4]["when"] = "params.supplier.tier == 'C'"

    plan = migrate(engine.store, evolved(change))
    assert plan.applied and plan.steps == ["rewrite 3 Supplier object(s); drop bank_account; backfill country"]
    assert engine.store.get("Supplier", "S-003") == {
        "supplier_id": "S-003", "name": "华达电子", "tier": "C", "blacklisted": True, "country": "CN"}
    assert engine.store.count("PurchaseOrder") == 2  # untouched


def test_unsafe_changes_are_problems_and_nothing_is_applied(engine):
    def change(data):
        data["object_types"]["PurchaseOrder"]["properties"]["title"]["type"] = "integer"
        data["object_types"]["Supplier"]["properties"]["rating"]["enum"] = ["A", "B"]
        data["object_types"]["Supplier"]["properties"]["tax_id"] = {"type": "string", "required": True}

    plan = migrate(engine.store, evolved(change))
    assert not plan.applied
    text = "\n".join(plan.problems)
    assert "PurchaseOrder 'PO-0001': title='办公椅 20 把' is not a valid integer" in text
    assert "Supplier 'S-003': rating='C' is not one of ['A', 'B']" in text
    assert "Supplier 'S-001': tax_id is required but has no value and no default" in text
    assert engine.store.get_meta("ontology")["version"] == "0.1.0"
    assert engine.store.get("Supplier", "S-003")["rating"] == "C"


def _without_orders(data):
    del data["object_types"]["PurchaseOrder"]
    del data["link_types"]["supplier"]
    del data["action_types"]["submit_purchase_order"]
    del data["action_types"]["cancel_purchase_order"]


def test_removed_types_need_an_explicit_drop(engine):
    held = submit(engine, 20000).request_id
    new = evolved(_without_orders)
    blocked = migrate(engine.store, new)
    assert not blocked.applied and len(blocked.problems) == 2  # 2 objects, 2 links

    plan = migrate(engine.store, new, drop_removed=True)
    assert plan.applied
    assert "delete 2 object(s) of removed type PurchaseOrder" in plan.steps
    assert "delete 2 link(s) of removed link type supplier" in plan.steps
    assert f"close pending request {held} as stale: action submit_purchase_order was removed" in plan.steps
    assert engine.store.count("PurchaseOrder") == 0 and engine.store.links_to("supplier", "S-001") == []
    assert engine.store.get_approval(held)["status"] == "stale"


def test_renamed_type_and_link_keep_their_data(engine):
    def change(data):
        data["object_types"]["Vendor"] = {**data["object_types"].pop("Supplier"), "renamed_from": "Supplier"}
        data["link_types"]["vendor"] = {**data["link_types"].pop("supplier"), "to": "Vendor",
                                        "renamed_from": "supplier"}
        text = yaml.safe_dump(data["action_types"], allow_unicode=True)
        text = text.replace("object_type: Supplier", "object_type: Vendor").replace("link: supplier", "link: vendor")
        data["action_types"] = yaml.safe_load(text.replace("create: Vendor", "create: PurchaseOrder"))

    new = evolved(change)
    plan = migrate(engine.store, new)
    assert plan.applied, plan.problems
    assert "rename object type Supplier -> Vendor" in plan.steps
    assert "rename link type supplier -> vendor (2 link(s))" in plan.steps
    eng = Engine(new, engine.store)
    assert eng.store.count("Supplier") == 0 and eng.store.count("Vendor") == 3
    assert eng.get("PurchaseOrder", "PO-0001")["links"] == {"vendor": ["S-001"]}
    assert submit(eng, 10, supplier="S-003").status == "denied"  # rules still follow the renamed link target


def test_tightened_cardinality_is_checked(engine):
    submit(engine, 1)  # S-001 now has two orders
    plan = migrate(engine.store, evolved(lambda d: d["link_types"]["supplier"].update(cardinality="one_to_one")))
    assert not plan.applied and "Supplier 'S-001' has 2 links" in plan.problems[0]


def test_stale_requests_when_parameters_change(engine):
    keep = submit(engine, 20000).request_id

    def change(data):
        data["action_types"]["submit_purchase_order"]["parameters"]["cost_center"] = {"type": "string"}

    plan = migrate(engine.store, evolved(change))
    assert plan.applied and any("missing: cost_center" in s for s in plan.steps)
    assert engine.store.get_approval(keep)["status"] == "stale"


def test_pending_requests_survive_rule_changes_and_are_rechecked(engine):
    held = submit(engine, 20000).request_id
    new = evolved(lambda d: d["action_types"]["submit_purchase_order"]["rules"][3].update(when="params.amount > 15000"))
    assert migrate(engine.store, new).applied
    step = Engine(new, engine.store).approve(held, BOB)  # the stricter rule now asks for the CFO as well
    assert step.status == "pending_approval" and step.required_approvals == ["cfo"]


def test_diff_ignores_annotations_and_reports_additions():
    old, new = raw_schema(), raw_schema()
    new["x-meta"] = 1
    new["roles"]["auditor"] = {"label": "审计"}
    new["object_types"]["Supplier"]["properties"]["rating"]["enum"].append("D")
    del new["action_types"]["blacklist_supplier"]
    assert diff(old, new) == [
        "- action_types.blacklist_supplier",
        '~ object_types.Supplier.properties.rating.enum: ["A", "B", "C"] -> ["A", "B", "C", "D"]',
        "+ roles.auditor",
    ]


def test_cli_migrate_and_history(tmp_path, capsys):
    schema = tmp_path / "ontology.yaml"
    schema.write_text((PROCUREMENT / "ontology.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    common = ["--schema", str(schema), "--db", str(tmp_path / "m.db")]
    assert main(["init", *common, "--seed", str(PROCUREMENT / "seed.json")]) == 0

    data = raw_schema()
    data["version"] = "0.2.0"
    data["object_types"]["Supplier"]["properties"]["rating"]["enum"] = ["A", "B"]
    schema.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    assert main(["query", "Supplier", *common]) == 2  # refuses until migrated
    assert "nano-onto migrate" in capsys.readouterr().err
    assert main(["migrate", *common]) == 1
    assert "PROBLEMS" in capsys.readouterr().out

    data["object_types"]["Supplier"]["properties"]["rating"]["enum"] = ["A", "B", "C", "D"]
    schema.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    assert main(["migrate", *common, "--dry-run"]) == 0 and "dry run" in capsys.readouterr().out
    assert main(["migrate", *common, "--by", "carol"]) == 0 and "applied." in capsys.readouterr().out
    assert main(["query", "Supplier", *common]) == 0
    capsys.readouterr()
    assert main(["history", *common, "--json"]) == 0
    assert [h["event"] for h in json.loads(capsys.readouterr().out)] == ["initialised", "migrated"]
