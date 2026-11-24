import json
import threading
import urllib.error
import urllib.request

import pytest

from conftest import ALICE, BOB, CAROL, make_engine, submit
from nano_onto import Actor
from nano_onto.console import make_console
from nano_onto.http_server import issue_token, revoke_tokens


def start(engine, **kwargs):
    server = make_console(engine, port=0, **kwargs)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


@pytest.fixture
def demo():
    engine = make_engine()
    server, url = start(engine, demo_users=[ALICE, BOB, CAROL], auditors=("cfo",))
    yield url, engine
    server.shutdown()


def call(url, path, user=None, body=None, headers=None, raw=None):
    request = urllib.request.Request(url + path, headers=dict(headers or {}))
    if user:
        request.add_header("X-Demo-User", user)
    if body is not None or raw is not None:
        request.data = raw if raw is not None else json.dumps(body).encode()
        request.add_header("Content-Type", (headers or {}).get("Content-Type", "application/json"))
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            data = response.read()
            return response.status, json.loads(data) if response.headers.get_content_type() == "application/json" else data
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def test_page_and_session(demo):
    url, _ = demo
    status, page = call(url, "/")
    assert status == 200 and b"<title>nano-onto console</title>" in page
    assert b"http://" not in page.replace(b"http://www.w3.org/2000/svg", b"") and b"https://" not in page  # no external resources
    status, session = call(url, "/api/session")
    assert session["mode"] == "demo" and [u["id"] for u in session["users"]] == ["alice", "bob", "carol"]
    assert call(url, "/api/me")[0] == 401 and call(url, "/api/me", "mallory")[0] == 401
    assert call(url, "/api/me", "carol")[1] == {"id": "carol", "role": "cfo", "label": "cfo (CFO)", "auditor": True}
    assert call(url, "/api/nope", "alice")[0] == 404


def test_reads_follow_the_users_permissions(demo):
    url, _ = demo
    as_alice = call(url, "/api/ontology", "alice")[1]
    assert as_alice["object_types"]["Supplier"]["properties"]["bank_account"]["hidden"] is True
    assert "count" not in as_alice["object_types"]["PurchaseOrder"]
    assert as_alice["actions"]["blacklist_supplier"]["can_run"] is False
    assert as_alice["actions"]["submit_purchase_order"]["rules"][3]["source"]["doc"] == "procurement-policy.md"
    assert as_alice["link_types"] == [{"name": "supplier", "from": "PurchaseOrder", "to": "Supplier",
                                       "cardinality": "many_to_one", "label": "Supplier"}]
    assert [o["po_id"] for o in call(url, "/api/objects?type=PurchaseOrder", "alice")[1]] == ["PO-0001"]
    assert len(call(url, "/api/objects?type=PurchaseOrder", "carol")[1]) == 2
    assert call(url, "/api/object?type=Supplier&id=S-001", "alice")[1]["properties"]["bank_account"] == "***"
    assert call(url, "/api/object?type=Supplier&id=S-001", "carol")[1]["properties"]["bank_account"].startswith("6222")
    assert call(url, "/api/object?type=PurchaseOrder&id=PO-0002", "alice")[0] == 400  # dave's order: hidden
    assert call(url, "/api/objects?type=Nope", "alice")[0] == 400


def test_actions_approvals_and_audit_through_the_console(demo):
    url, engine = demo
    order = {"action": "submit_purchase_order", "params": {"title": "Servers", "amount": "60000", "supplier": "S-002"}}
    dry = call(url, "/api/explain", "alice", order)[1]
    assert dry["status"] == "pending_approval" and dry["dry_run"] and engine.pending() == []
    held = call(url, "/api/run", "alice", order)[1]
    assert held["status"] == "pending_approval" and engine.store.count("PurchaseOrder") == 2
    request_id = held["request_id"]

    mine = call(url, "/api/approvals", "alice")[1]
    assert mine["inbox"] == [] and [r["id"] for r in mine["mine"]] == [request_id]
    assert [r["id"] for r in call(url, "/api/approvals", "bob")[1]["inbox"]] == [request_id]

    status, refused = call(url, "/api/approve", "alice", {"request_id": request_id})
    assert status == 400 and "cannot decide" in refused["error"]  # her own request, and not her role
    assert call(url, "/api/approve", "bob", {"request_id": request_id})[1]["required_approvals"] == ["cfo"]
    done = call(url, "/api/approve", "carol", {"request_id": request_id})[1]
    assert done["status"] == "executed" and done["created"] == {"po": "PO-0003"}
    assert call(url, "/api/approvals", "alice")[1]["mine"][0]["status"] == "executed"

    denied = call(url, "/api/run", "alice", {"action": "blacklist_supplier", "params": {"supplier": "S-002"}})[1]
    assert denied["status"] == "denied"
    other = call(url, "/api/run", "alice", {"action": "submit_purchase_order",
                                           "params": {"title": "x", "amount": 20000, "supplier": "S-001"}})[1]
    assert call(url, "/api/reject", "bob", {"request_id": other["request_id"], "reason": "no budget"})[1]["status"] == "rejected"

    everything = call(url, "/api/audit", "carol")[1]
    assert everything["scope"] == "all" and {r["actor"] for r in everything["rows"]} == {"alice", "bob", "carol"}
    own = call(url, "/api/audit", "bob")[1]
    assert own["scope"] == "own" and {r["actor"] for r in own["rows"]} == {"bob"}
    assert [r["seq"] for r in everything["rows"]] == sorted((r["seq"] for r in everything["rows"]), reverse=True)


def test_own_requests_are_not_in_ones_own_inbox(demo):
    url, engine = demo
    held = submit(engine, 60000, actor=Actor("carol", "cfo"))  # carol's own order still needs the manager... and not her
    assert held.required_approvals == ["dept_manager"]
    submit(engine, 20000, actor=BOB)  # executes: bob holds the required role
    request_id = submit(engine, 60000, actor=BOB).request_id  # needs the CFO
    assert [r["id"] for r in call(url, "/api/approvals", "carol")[1]["inbox"]] == [request_id]
    inbox = call(url, "/api/approvals", "bob")[1]
    assert [r["id"] for r in inbox["inbox"]] == [held.request_id] and inbox["own_waiting"] == []


def test_request_hygiene(demo):
    url, _ = demo
    body = {"action": "blacklist_supplier", "params": {"supplier": "S-001"}}
    assert call(url, "/api/run", "carol", body, headers={"Origin": "https://evil.example"})[0] == 403
    assert call(url, "/api/run", "carol", raw=b"action=blacklist_supplier",
                headers={"Content-Type": "application/x-www-form-urlencoded"})[0] == 415  # what a cross-site form can send
    assert call(url, "/api/run", "carol", raw=b"{not json")[0] == 400
    assert call(url, "/api/run", "carol", raw=b"[]")[0] == 400
    assert call(url, "/api/run", "carol", {"action": "nope"})[1]["status"] == "invalid"
    assert call(url, "/api/objects", "carol")[0] == 400  # missing ?type=


def test_token_mode(tmp_path):
    tokens = tmp_path / "tokens.yaml"
    token = issue_token(tokens, "carol", "cfo")
    server, url = start(make_engine(), tokens_path=tokens)
    try:
        assert call(url, "/api/session")[1] == {"ontology": "procurement", "version": "0.1.0", "mode": "token", "users": []}
        assert call(url, "/api/me")[0] == 401
        assert call(url, "/api/me", "carol")[0] == 401  # the demo header means nothing here
        auth = {"Authorization": f"Bearer {token}"}
        assert call(url, "/api/me", headers=auth)[1]["id"] == "carol"
        done = call(url, "/api/run", body={"action": "blacklist_supplier", "params": {"supplier": "S-001"}}, headers=auth)[1]
        assert done["status"] == "executed"
        revoke_tokens(tokens, "carol")
        assert call(url, "/api/me", headers=auth)[0] == 401
    finally:
        server.shutdown()


def test_configuration_is_checked(tmp_path):
    engine = make_engine()
    with pytest.raises(ValueError, match="either a tokens file or demo users"):
        make_console(engine, port=0)
    with pytest.raises(ValueError, match="localhost"):
        make_console(engine, demo_users=[ALICE], host="0.0.0.0", port=0)
    with pytest.raises(ValueError, match="unknown role"):
        make_console(engine, demo_users=[Actor("mallory", "admin")], port=0)
    with pytest.raises(ValueError, match="unknown role"):
        make_console(engine, demo_users=[ALICE], auditors=("auditor",), port=0)


def test_stale_engine_reports_a_conflict(demo):
    url, engine = demo
    stamp = engine.store.get_meta("ontology")
    engine.store.set_meta("ontology", {**stamp, "fingerprint": "changed-elsewhere"})  # another process migrated
    status, body = call(url, "/api/objects?type=Supplier", "carol")
    assert status == 409 and "restart this process" in body["error"]
