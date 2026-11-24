"""Regression tests for the defects an independent review confirmed before 0.2.0 was published."""

import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
import yaml

from conftest import ALICE, BOB, CAROL, EXAMPLES, make_engine, raw_schema, submit
from nano_onto import ActionError, Actor, Engine, ExprError, Ontology, SchemaError, Store, VersionError, expr, open_store
from nano_onto.compile import render_skill
from nano_onto.mcp_server import MCPServer
from nano_onto.migrate import migrate
from nano_onto.schema import coerce
from nano_onto.webhooks import deliver

DAVE, EVE, FRANK = Actor("dave", "employee"), Actor("eve", "employee"), Actor("frank", "finance")


# ---------------------------------------------------------------- parameters
def _with_quantity(data):
    data["object_types"]["PurchaseOrder"]["properties"]["quantity"] = {"type": "integer", "default": 1}
    action = data["action_types"]["submit_purchase_order"]
    action["parameters"]["quantity"] = {"type": "integer", "default": 1}
    action["rules"][2]["when"] = "params.amount * params.quantity > 10000"
    action["rules"][3]["when"] = "params.amount * params.quantity > 50000"
    action["effects"][0]["set"]["quantity"] = "params.quantity"


@pytest.mark.parametrize("extra", [{}, {"quantity": None}, {"quantity": ""}])
def test_explicit_null_takes_the_default_instead_of_skipping_rules(extra):
    eng = make_engine(_with_quantity)
    res = submit(eng, 99999, **extra)  # null * amount used to be None, and no threshold rule fired
    assert res.status == "pending_approval" and res.required_approvals == ["cfo", "dept_manager"]


def test_integers_are_parsed_exactly():
    assert coerce("integer", "9007199254740993") == 9007199254740993  # float() would give ...992
    assert coerce("number", "9007199254740993") == 9007199254740993
    assert coerce("integer", " 42 ") == 42 and coerce("integer", "1e3") == 1000 and coerce("number", "2.50") == 2.5
    for bad in ("1_0", "0x10", "9007199254740993.0", "１２a", "nan", "", "1e999"):
        with pytest.raises(ValueError):
            coerce("integer", bad)
    assert coerce("date", "2026-01-01T10:00:00") == "2026-01-01"
    with pytest.raises(ValueError):
        coerce("date", "2026-01-01 or anything")


def test_arithmetic_overflow_is_an_error_not_a_silent_nan():
    env = {"params": {"q": 1e200, "p": 1e200, "r": 1e200}}
    with pytest.raises(ExprError, match="overflow"):
        expr.evaluate("params.q * params.p - params.q * params.r > 10000", env)


def test_subscript_members_are_checked_at_load_time():
    data = raw_schema()
    data["action_types"]["submit_purchase_order"]["rules"][0]["when"] = "params.supplier['blacklistd']"
    with pytest.raises(SchemaError, match="no property or link 'blacklistd'"):
        Ontology.from_dict(data)


# ------------------------------------------------- one canonical key per row
@pytest.fixture
def erp(tmp_path):
    path = str(tmp_path / "erp.db")
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE suppliers (id INTEGER PRIMARY KEY, supplier_name TEXT NOT NULL, credit_limit REAL, active INTEGER);
        CREATE TABLE purchase_orders (po_no TEXT PRIMARY KEY, title TEXT NOT NULL, amount REAL NOT NULL,
                                      state TEXT, requested_by TEXT, supplier_id INTEGER);
        CREATE TABLE po_tags (po_no TEXT, tag TEXT);
        INSERT INTO suppliers VALUES (1, 'Acme', 5000, 1), (2, 'Globex', 9000, 1),
                                     (9007199254740992, 'Victim Ltd', 1, 1), (9007199254740993, 'Target Ltd', 1, 1);
    """)
    db.commit()
    db.close()
    data = yaml.safe_load((EXAMPLES / "erp_readthrough" / "ontology.yaml").read_text(encoding="utf-8"))
    data["link_types"]["category"] = {"from": "Supplier", "to": "Tag"}  # many_to_one, kept in nano-onto's tables
    data["action_types"]["merge_suppliers"] = {
        "parameters": {"keep": {"type": "object_ref", "object_type": "Supplier"},
                       "drop": {"type": "object_ref", "object_type": "Supplier"}},
        "rules": [{"id": "distinct", "when": "params.keep == params.drop", "then": "deny"}],
        "effects": [{"update": "params.drop", "set": {"active": False}}]}
    data["action_types"]["categorise"] = {
        "parameters": {"supplier": {"type": "object_ref", "object_type": "Supplier"},
                       "tag": {"type": "object_ref", "object_type": "Tag"}},
        "effects": [{"link": "category", "from": "params.supplier", "to": "params.tag"}]}
    data["action_types"]["add_supplier"] = {
        "parameters": {"name": "string"}, "effects": [{"create": "Supplier", "as": "s", "set": {"name": "params.name"}}]}
    onto = Ontology.from_dict(data)
    engine = Engine(onto, open_store(path, onto))
    engine.load_seed({"objects": {"Tag": [{"tag": "urgent"}, {"tag": "capex"}]}})
    return engine


def test_large_integer_keys_address_the_right_row(erp):
    res = erp.execute("deactivate_supplier", {"supplier": "9007199254740993"}, FRANK)
    assert res.updated == ["Supplier:9007199254740993"]
    assert erp.store.get("Supplier", "9007199254740993") == {
        "supplier_id": 9007199254740993, "name": "Target Ltd", "credit_limit": 1, "active": False}
    assert erp.store.get("Supplier", "9007199254740992")["active"] is True  # the neighbour is untouched


@pytest.mark.parametrize("alias", ["01", "1.0", " 1 ", 1, 1.0])
def test_key_spellings_cannot_defeat_same_object_rules(erp, alias):
    res = erp.execute("merge_suppliers", {"keep": "1", "drop": alias}, FRANK)
    assert res.status == "denied"


def test_links_are_stored_under_the_canonical_key(erp):
    assert erp.execute("categorise", {"supplier": "2", "tag": "urgent"}, FRANK).status == "executed"
    assert erp.execute("categorise", {"supplier": "02", "tag": "capex"}, FRANK).status == "executed"
    assert erp.store.links_from("category", "2") == ["capex"]  # replaced, not a second link under "02"
    assert erp.store._rows("SELECT COUNT(*) AS n FROM onto_links WHERE link = 'category'")[0]["n"] == 1
    assert erp.get("Supplier", "02")["id"] == "2"


def test_generated_ids_fit_integer_keys(erp):
    res = erp.execute("add_supplier", {"name": "New Co"}, FRANK)
    assert res.status == "executed" and res.created == {"s": "3"}  # first free integer, no 'SUP-' prefix
    assert erp.store.get("Supplier", "3")["name"] == "New Co"


def test_a_row_that_does_not_fit_its_type_is_a_clear_error(erp):
    erp.store._run("UPDATE suppliers SET active = 2 WHERE id = 2")
    with pytest.raises(Exception, match="suppliers.active holds 2, which is not a boolean"):
        erp.query("Supplier")


# ------------------------------------------- actions cannot probe hidden data
def test_actions_treat_hidden_objects_as_missing(engine):
    server = MCPServer(engine, ALICE)  # alice may not see dave's PO-0002
    hidden, _ = server.call_tool("onto_explain", {"action": "cancel_purchase_order", "params": {"po": "PO-0002"}})
    missing, _ = server.call_tool("onto_explain", {"action": "cancel_purchase_order", "params": {"po": "PO-9999"}})
    assert hidden["status"] == missing["status"] == "invalid" and hidden["trace"] == missing["trace"] == []
    assert hidden["messages"][0].replace("PO-0002", "X") == missing["messages"][0].replace("PO-9999", "X")
    assert engine.execute("cancel_purchase_order", {"po": "PO-0002"}, DAVE).status == "executed"  # its owner still can


def test_dry_runs_are_audited(engine):
    for amount in (1, 20000, 60000):
        engine.explain("submit_purchase_order", {"title": "t", "amount": amount, "supplier": "S-001"}, ALICE)
    assert [(r["actor"], r["event"], r["outcome"]) for r in engine.store.audit_log()] == [
        ("alice", "explain", "executed"), ("alice", "explain", "pending_approval"), ("alice", "explain", "pending_approval")]
    assert engine.pending() == [] and engine.store.count("PurchaseOrder") == 2


def test_uninvolved_actors_learn_nothing_about_a_request(engine):
    req = submit(engine, 60000).request_id
    messages = []
    for request_id in (req, "REQ-00000000"):
        for method in (engine.approve, engine.reject):
            with pytest.raises(ActionError) as err:
                method(request_id, EVE)
            messages.append(str(err.value).replace(request_id, "X"))
    assert len(set(messages)) == 1  # existing and missing requests answer identically
    assert engine.store.get_approval(req)["status"] == "pending"


# ----------------------------------------------------------------- approvals
def test_a_busy_database_does_not_let_anyone_close_a_request(tmp_path, monkeypatch):
    eng = make_engine(store=Store(str(tmp_path / "busy.db")))
    req = submit(eng, 60000).request_id
    eng.approve(req, BOB)
    from nano_onto.store import StoreError

    real_run, state = eng.store._run, {"failed": False}

    def busy_once(sql, params=()):  # the first BEGIN times out, as when another process holds the write lock
        if sql.startswith("BEGIN") and not state["failed"]:
            state["failed"] = True
            raise StoreError("OperationalError: database is locked")
        return real_run(sql, params)

    with monkeypatch.context() as patch:
        patch.setattr(eng.store, "_run", busy_once)
        with pytest.raises(ActionError, match="nothing was changed"):
            eng.approve(req, EVE)  # eve has no say in this request at all
    assert state["failed"]
    after = eng.store.get_approval(req)
    assert after["status"] == "pending" and after["approved_by"] == {"dept_manager": "bob"}
    assert eng.approve(req, CAROL).status == "executed"


def test_a_transient_error_during_the_final_approval_keeps_the_request_open(tmp_path, monkeypatch):
    eng = make_engine(store=Store(str(tmp_path / "t.db")))
    req = submit(eng, 20000).request_id
    from nano_onto.store import StoreError

    real_put = eng.store.put

    def flaky(*args, **kwargs):
        raise StoreError("OperationalError: disk I/O error")

    monkeypatch.setattr(eng.store, "put", flaky)
    with pytest.raises(ActionError, match="nothing was changed; try again"):
        eng.approve(req, BOB)
    assert eng.store.get_approval(req)["status"] == "pending" and eng.store.get_approval(req)["approved_by"] == {}
    monkeypatch.setattr(eng.store, "put", real_put)
    assert eng.approve(req, BOB).status == "executed"


def test_failed_commit_releases_the_database(tmp_path):
    path = str(tmp_path / "c.db")
    eng = make_engine(store=Store(path))
    real = eng.store.conn

    class FailingCommit:
        def __init__(self):
            self.failed = False

        def execute(self, sql, params=()):
            if sql == "COMMIT" and not self.failed:
                self.failed = True
                raise sqlite3.OperationalError("database is locked")
            return real.execute(sql, params)

        def __getattr__(self, name):
            return getattr(real, name)

    eng.store.conn = FailingCommit()
    try:
        submit(eng, 5)
    except Exception:  # noqa: BLE001 - raising or returning invalid are both acceptable
        pass
    assert not real.in_transaction  # it used to stay open, holding the write lock
    assert eng.store.count("PurchaseOrder") == 2
    assert submit(eng, 5).status == "executed"
    assert Engine(eng.onto, Store(path)).store.count("PurchaseOrder") == 3  # other connections are not locked out


def test_engine_refuses_to_run_inside_a_callers_transaction(engine):
    with engine.store.transaction():
        with pytest.raises(ActionError, match="own transaction"):
            submit(engine, 100)
    assert engine.store.count("PurchaseOrder") == 2


def _restricted(data):
    data["version"] = "0.2.0"
    data["action_types"]["submit_purchase_order"]["allowed_roles"] = ["dept_manager", "cfo"]


def test_role_gate_is_rechecked_when_a_request_is_approved(engine):
    req = submit(engine, 20000).request_id
    data = raw_schema()
    _restricted(data)
    new = Ontology.from_dict(data)
    engine.store.set_meta("ontology", {"name": new.name, "version": new.version, "fingerprint": new.fingerprint(),
                                       "schema": new.raw})  # as if stamped without the stale-request clean-up
    final = Engine(new, engine.store).approve(req, BOB)
    assert final.status == "invalid" and "may no longer run" in final.messages[-1]
    assert engine.store.get_approval(req)["status"] == "failed" and engine.store.count("PurchaseOrder") == 2


def test_migration_closes_requests_that_can_no_longer_be_decided(engine):
    no_longer_allowed = submit(engine, 20000).request_id
    data = raw_schema()
    _restricted(data)
    plan = migrate(engine.store, Ontology.from_dict(data))
    assert plan.applied and any("may no longer run" in s for s in plan.steps)
    assert engine.store.get_approval(no_longer_allowed)["status"] == "stale"

    eng = make_engine()
    waiting = submit(eng, 60000).request_id
    data = raw_schema()
    data["version"] = "0.2.0"
    del data["roles"]["dept_manager"]
    for action in data["action_types"].values():
        action["allowed_roles"] = [r for r in action["allowed_roles"] if r != "dept_manager"]
    data["action_types"]["submit_purchase_order"]["rules"].pop(2)
    plan = migrate(eng.store, Ontology.from_dict(data))
    assert plan.applied and any("role that no longer exists (dept_manager)" in s for s in plan.steps)
    assert eng.store.get_approval(waiting)["status"] == "stale"


# ----------------------------------------------------------------- versioning
def test_running_engine_stops_when_the_database_is_migrated(tmp_path):
    path = str(tmp_path / "live.db")
    server = make_engine(store=Store(path))  # a long-running process
    data = raw_schema()
    data["version"] = "0.2.0"
    data["action_types"]["submit_purchase_order"]["rules"].append({"id": "freeze", "when": "True", "then": "deny"})
    assert migrate(Store(path), Ontology.from_dict(data)).applied  # another process tightens the rules
    with pytest.raises(VersionError, match="restart this process"):
        submit(server, 5)
    with pytest.raises(VersionError):
        server.query("Supplier", actor=ALICE)
    with pytest.raises(VersionError):
        server.approve("REQ-X", BOB)
    assert Store(path).count("PurchaseOrder") == 2


def test_reordering_rules_can_be_migrated(engine):
    data = raw_schema()
    rules = data["action_types"]["submit_purchase_order"]["rules"]
    rules[0], rules[1] = rules[1], rules[0]
    new = Ontology.from_dict(data)
    with pytest.raises(VersionError):
        Engine(new, engine.store)
    plan = migrate(engine.store, new)
    assert plan.applied and plan.changes == ["~ reordered or reformatted only (same types, rules and effects)"]
    Engine(new, engine.store)  # used to keep raising while migrate said "up to date"


def test_renamed_from_an_existing_type_is_a_problem_not_a_data_move(engine):
    data = raw_schema()
    data["version"] = "0.2.0"
    data["object_types"]["Vendor"] = {**data["object_types"]["Supplier"], "renamed_from": "Supplier"}
    plan = migrate(engine.store, Ontology.from_dict(data))
    assert not plan.applied and "still exists" in plan.problems[0]
    assert engine.store.count("Supplier") == 3


# ------------------------------------------------------------------- webhooks
def _hooked(data, url, **spec):
    data["webhooks"] = {"erp": {"url": url, **spec}}
    data["action_types"]["submit_purchase_order"]["effects"].append(
        {"webhook": "erp", "payload": {"x-request-id": "po.po_id", "sid": "params.supplier"}})


def test_x_keys_survive_in_headers_and_payloads_and_count_as_changes(monkeypatch):
    monkeypatch.setenv("K", "key-1")
    eng = make_engine(lambda d: _hooked(d, "https://erp.example/h", headers={"x-api-key": "${K}", "X-Other": "1"}))
    submit(eng, 5)
    seen = []
    deliver(eng, sender=lambda url, body, headers, timeout: seen.append(headers) or 200)
    assert seen[0]["x-api-key"] == "key-1" and seen[0]["X-Other"] == "1"
    assert eng.store.outbox()[0]["body"]["payload"] == {"x-request-id": "PO-0003", "sid": "S-001"}
    a, b = raw_schema(), raw_schema()
    _hooked(a, "https://erp.example/h", headers={"x-api-key": "one"})
    _hooked(b, "https://erp.example/h", headers={"x-api-key": "two"})
    assert Ontology.from_dict(a).fingerprint() != Ontology.from_dict(b).fingerprint()


def test_secrets_are_scrubbed_from_delivery_errors(monkeypatch):
    monkeypatch.setenv("ERP_TOKEN", "tok-SECRET-123\n")  # a trailing newline makes the header invalid
    eng = make_engine(lambda d: _hooked(d, "https://erp.example/h", headers={"Authorization": "Bearer ${ERP_TOKEN}"}))
    submit(eng, 5)
    report = deliver(eng, sender=lambda *a: (_ for _ in ()).throw(ValueError("Invalid header b'Bearer tok-SECRET-123\\n'")))
    everything = str(report) + str(eng.store.outbox()) + str(eng.store.audit_log())
    assert "tok-SECRET-123" not in everything and "line break" in report[0]["error"]

    monkeypatch.setenv("ERP_TOKEN", "tok-SECRET-123")
    later = datetime.now(timezone.utc) + timedelta(hours=2)
    report = deliver(eng, now=later, sender=lambda *a: (_ for _ in ()).throw(OSError("refused: Bearer tok-SECRET-123")))
    assert "tok-SECRET-123" not in str(report) + str(eng.store.audit_log()) and "***" in report[0]["error"]


def test_redirects_are_failures_and_credentials_are_not_forwarded(monkeypatch):
    got = {"a": [], "b": []}

    def handler(name, status, location=None):
        class Handler(BaseHTTPRequestHandler):
            def _answer(self):
                got[name].append((self.command, self.headers.get("Authorization")))
                self.send_response(status)
                if location:
                    self.send_header("Location", location())
                self.send_header("Content-Length", "0")
                self.end_headers()

            do_GET = do_POST = _answer  # noqa: N815

            def log_message(self, *args):
                pass
        return Handler

    b = HTTPServer(("127.0.0.1", 0), handler("b", 200))
    a = HTTPServer(("127.0.0.1", 0), handler("a", 302, lambda: f"http://127.0.0.1:{b.server_address[1]}/x"))
    for server in (a, b):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("ERP_TOKEN", "tok-SECRET-123")
    eng = make_engine(lambda d: _hooked(d, f"http://127.0.0.1:{a.server_address[1]}/hook",
                                        headers={"Authorization": "Bearer ${ERP_TOKEN}"}))
    submit(eng, 5)
    try:
        report = deliver(eng)
    finally:
        a.shutdown()
        b.shutdown()
    assert report[0]["status"] == "pending" and report[0]["error"] == "HTTP 302"
    assert got["a"] == [("POST", "Bearer tok-SECRET-123")] and got["b"] == []


def test_two_sweeps_do_not_send_the_same_event_twice(tmp_path):
    path = str(tmp_path / "w.db")
    one = make_engine(lambda d: _hooked(d, "https://erp.example/h"), Store(path))
    two = Engine(one.onto, Store(path))
    submit(one, 5)
    sent, barrier = [], threading.Barrier(2, timeout=5)

    def sweep(engine):
        try:
            barrier.wait()
        except threading.BrokenBarrierError:
            pass
        deliver(engine, sender=lambda url, body, headers, timeout: sent.append(headers["X-Nano-Onto-Delivery"]) or 200)

    threads = [threading.Thread(target=sweep, args=(e,)) for e in (one, two)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(sent) == 1 and one.store.outbox()[0]["status"] == "delivered"


def test_an_abandoned_claim_is_retried_after_its_lease(engine_factory=make_engine):
    eng = engine_factory(lambda d: _hooked(d, "https://erp.example/h", timeout=5))
    submit(eng, 5)
    now = datetime.now(timezone.utc)

    def dies(*args):
        raise KeyboardInterrupt  # the process is killed mid-send: nothing after the claim runs

    with pytest.raises(KeyboardInterrupt):
        deliver(eng, sender=dies, now=now)
    assert deliver(eng, sender=lambda *a: 200, now=now + timedelta(seconds=30)) == []  # still claimed
    assert deliver(eng, sender=lambda *a: 200, now=now + timedelta(seconds=66))[0]["status"] == "delivered"


# ------------------------------------------------------------------- surfaces
def test_skill_never_contains_database_credentials():
    text = render_skill(Ontology.load(EXAMPLES / "procurement" / "ontology.yaml"), "ontology.yaml",
                        "postgresql://onto:s3cr3t@db.internal/erp")
    assert "s3cr3t" not in text and "--db $NANO_ONTO_DB" in text
    assert "--db shop.db" in render_skill(Ontology.load(EXAMPLES / "procurement" / "ontology.yaml"), "o.yaml", "shop.db")


def test_names_with_a_trailing_newline_are_rejected():
    data = raw_schema()
    data["object_types"]["Supplier"]["source"] = {"table": "suppliers\n"}
    with pytest.raises(SchemaError, match="not a valid table name"):
        Ontology.from_dict(data)
