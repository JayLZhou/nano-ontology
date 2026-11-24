import json
import threading
import urllib.error
import urllib.request

import pytest

from conftest import make_engine
from nano_onto.cli import main
from nano_onto.http_server import issue_token, load_tokens, make_server, revoke_tokens


@pytest.fixture
def server(tmp_path):
    tokens = tmp_path / "tokens.yaml"
    issued = {name: issue_token(tokens, name, role)
              for name, role in (("alice", "employee"), ("bob", "dept_manager"), ("carol", "cfo"))}
    engine = make_engine()
    httpd = make_server(engine, tokens, port=0, allowed_origins=("https://app.example",))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", issued, engine
    httpd.shutdown()


def post(url, message, token=None, path="/mcp", headers=None, raw=None):
    request = urllib.request.Request(url + path, data=raw if raw is not None else json.dumps(message).encode(),
                                     method="POST", headers={"Content-Type": "application/json", **(headers or {})})
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            body = response.read()
            return response.status, json.loads(body) if body else None
    except urllib.error.HTTPError as exc:
        body = exc.read()
        return exc.code, (json.loads(body) if body else None)


def call(url, token, name, **arguments):
    status, reply = post(url, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                               "params": {"name": name, "arguments": arguments}}, token)
    assert status == 200
    return reply["result"]


def test_tokens_file_stores_only_hashes(tmp_path):
    path = tmp_path / "t.yaml"
    token = issue_token(path, "alice", "employee")
    assert token.startswith("nonto_") and token not in path.read_text()
    assert oct(path.stat().st_mode)[-3:] == "600"
    assert [(a.id, a.role) for a in load_tokens(path).values()] == [("alice", "employee")]
    issue_token(path, "alice", "cfo")
    assert revoke_tokens(path, "alice") == 2 and load_tokens(path) == {}
    path.write_text("- just a string\n")
    with pytest.raises(ValueError):
        load_tokens(path)


def test_requests_need_a_valid_token(server):
    url, tokens, _ = server
    ping = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    assert post(url, ping)[0] == 401
    assert post(url, ping, "nonto_wrong")[0] == 401
    assert post(url, ping, tokens["alice"]) == (200, {"jsonrpc": "2.0", "id": 1, "result": {}})


def test_the_token_decides_who_acts(server):
    url, tokens, engine = server
    status, reply = post(url, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                               "params": {"protocolVersion": "2025-06-18"}}, tokens["alice"])
    assert status == 200 and reply["result"]["protocolVersion"] == "2025-06-18"
    assert "alice (employee)" in reply["result"]["instructions"]
    assert post(url, {"jsonrpc": "2.0", "method": "notifications/initialized"}, tokens["alice"]) == (202, None)

    denied = call(url, tokens["alice"], "blacklist_supplier", supplier="S-002")
    assert denied["structuredContent"]["status"] == "denied"
    held = call(url, tokens["alice"], "submit_purchase_order", title="服务器", amount=60000, supplier="S-001")
    request_id = held["structuredContent"]["request_id"]
    assert call(url, tokens["alice"], "onto_approve", request_id=request_id)["isError"] is True
    assert call(url, tokens["bob"], "onto_approve", request_id=request_id)["structuredContent"]["status"] == "pending_approval"
    done = call(url, tokens["carol"], "onto_approve", request_id=request_id)["structuredContent"]
    assert done["status"] == "executed" and done["created"] == {"po": "PO-0003"}
    assert call(url, tokens["alice"], "onto_request_status", request_id=request_id)["structuredContent"]["status"] == "executed"
    assert [r["actor"] for r in engine.store.audit_log()] == ["alice", "alice", "alice", "bob", "carol", "carol"]

    seen = json.loads(call(url, tokens["alice"], "onto_get", type="Supplier", id="S-001")["content"][0]["text"])
    assert seen["properties"]["bank_account"] == "***"


def test_transport_rules(server):
    url, tokens, _ = server
    token = tokens["alice"]
    ping = {"jsonrpc": "2.0", "id": 7, "method": "ping"}
    assert post(url, ping, token, path="/other")[0] == 404
    assert post(url, ping, token, headers={"Origin": "https://evil.example"})[0] == 403
    assert post(url, ping, token, headers={"Origin": "https://app.example"})[0] == 200
    assert post(url, ping, token, headers={"Origin": "http://localhost:3000"})[0] == 200
    assert post(url, ping, token, headers={"MCP-Protocol-Version": "1999-01-01"})[0] == 400
    assert post(url, ping, token, headers={"MCP-Protocol-Version": "2025-06-18"})[0] == 200
    assert post(url, None, token, raw=b"{not json")[0] == 400
    assert post(url, [ping, ping], token)[0] == 400
    status, reply = post(url, {"jsonrpc": "2.0", "id": 1, "method": "nope"}, token)
    assert status == 200 and reply["error"]["code"] == -32601
    with urllib.request.urlopen(url + "/healthz", timeout=10) as response:
        assert json.loads(response.read())["ontology"] == "procurement@0.1.0"
    with pytest.raises(urllib.error.HTTPError) as err:
        urllib.request.urlopen(url + "/mcp", timeout=10)
    assert err.value.code == 405


def test_concurrent_clients_do_not_corrupt_state(server):
    url, tokens, engine = server
    errors = []

    def worker(n):
        try:
            for i in range(5):
                result = call(url, tokens["alice"], "submit_purchase_order", title=f"t{n}-{i}", amount=10, supplier="S-001")
                assert result["structuredContent"]["status"] == "executed"
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors
    assert engine.store.count("PurchaseOrder") == 32
    assert len({po["po_id"] for po in engine.store.query("PurchaseOrder", limit=None)}) == 32
    assert [r["seq"] for r in engine.store.audit_log(100)] == list(range(1, 31))


def test_server_rejects_tokens_with_unknown_roles(tmp_path):
    tokens = tmp_path / "tokens.yaml"
    issue_token(tokens, "mallory", "admin")
    with pytest.raises(ValueError, match="unknown role"):
        make_server(make_engine(), tokens, port=0)


def test_cli_token(tmp_path, capsys):
    path = str(tmp_path / "tokens.yaml")
    assert main(["token", "--tokens", path, "--actor", "alice", "--role", "employee"]) == 0
    assert "nonto_" in capsys.readouterr().out
    assert main(["token", "--tokens", path, "--actor", "alice", "--revoke"]) == 0
    assert "revoked 1" in capsys.readouterr().out
