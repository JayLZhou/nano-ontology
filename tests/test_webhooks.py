import hashlib
import hmac
import json
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from conftest import BOB, make_engine, submit
from nano_onto.cli import main
from nano_onto.webhooks import backoff, deliver, retry


def with_webhook(data, url="https://erp.example/hook", **spec):
    data["webhooks"] = {"erp": {"url": url, **spec}}
    data["action_types"]["submit_purchase_order"]["effects"].append(
        {"webhook": "erp", "event": "po.created",
         "payload": {"po": "po.po_id", "amount": "params.amount", "supplier": "params.supplier", "by": "actor.id"}})


class Recorder:
    """A fake sender that returns the given statuses in turn (an exception class is raised)."""

    def __init__(self, *statuses):
        self.statuses, self.calls = list(statuses), []

    def __call__(self, url, body, headers, timeout):
        self.calls.append((url, json.loads(body), headers, timeout))
        status = self.statuses.pop(0) if self.statuses else 200
        if isinstance(status, type):
            raise status("boom")
        return status


def test_webhook_is_queued_with_the_action_and_delivered(monkeypatch):
    monkeypatch.setenv("HOOK_SECRET", "s3cret")
    monkeypatch.setenv("ERP_TOKEN", "t0ken")
    eng = make_engine(lambda d: with_webhook(d, secret_env="HOOK_SECRET", timeout=3,
                                             headers={"Authorization": "Bearer ${ERP_TOKEN}"}))
    res = submit(eng, 100)
    assert res.status == "executed" and len(res.webhooks) == 1
    queued = eng.store.outbox("pending")
    assert [q["id"] for q in queued] == res.webhooks

    sender = Recorder(204)
    assert [r["status"] for r in deliver(eng, sender=sender)] == ["delivered"]
    url, body, headers, timeout = sender.calls[0]
    assert url == "https://erp.example/hook" and timeout == 3
    assert body["event"] == "po.created" and body["action"] == "submit_purchase_order"
    assert body["payload"] == {"po": "PO-0003", "amount": 100, "supplier": "S-001", "by": "alice"}
    assert body["actor"] == {"id": "alice", "role": "employee"} and body["ontology"] == "procurement@0.1.0"
    assert headers["Authorization"] == "Bearer t0ken" and headers["X-Nano-Onto-Delivery"] == res.webhooks[0]
    raw = json.dumps(body, ensure_ascii=False, sort_keys=True).encode()
    assert headers["X-Nano-Onto-Signature"] == "sha256=" + hmac.new(b"s3cret", raw, hashlib.sha256).hexdigest()

    assert deliver(eng, sender=sender) == [] and len(sender.calls) == 1  # delivered once
    assert eng.store.audit_log()[-1]["outcome"] == "delivered"


def test_nothing_is_queued_unless_the_action_commits():
    def failing(data):
        with_webhook(data)
        data["action_types"]["submit_purchase_order"]["effects"].append(
            {"link": "supplier", "from": "po", "to": "'S-404'"})

    eng = make_engine(failing)
    assert submit(eng, 100).status == "invalid"
    assert submit(eng, 100, supplier="S-003").status == "denied"
    held = submit(eng, 20000)
    assert held.status == "pending_approval" and eng.store.outbox() == []

    ok = make_engine(with_webhook)
    held = submit(ok, 20000)
    assert ok.store.outbox() == []
    done = ok.approve(held.request_id, BOB)  # queued when the approval executes it
    assert done.status == "executed" and len(ok.store.outbox("pending")) == 1
    assert ok.store.outbox()[0]["body"]["actor"]["id"] == "alice"  # the requester, not the approver


def test_failures_retry_with_backoff_then_die():
    eng = make_engine(lambda d: with_webhook(d, max_attempts=3))
    submit(eng, 100)
    t0 = datetime.now(timezone.utc)
    sender = Recorder(500, ConnectionError, 503)

    first = deliver(eng, sender=sender, now=t0)[0]
    assert first["status"] == "pending" and first["error"] == "HTTP 500"
    assert deliver(eng, sender=sender, now=t0 + timedelta(seconds=5)) == []  # not due yet
    second = deliver(eng, sender=sender, now=t0 + backoff(1))[0]
    assert second["status"] == "pending" and "ConnectionError" in second["error"]
    third = deliver(eng, sender=sender, now=t0 + backoff(1) + backoff(2))[0]
    assert third["status"] == "dead" and third["attempts"] == 3
    assert deliver(eng, sender=sender, now=t0 + timedelta(days=1)) == []
    assert [r["outcome"] for r in eng.store.audit_log()[-3:]] == ["retrying", "retrying", "dead"]

    assert retry(eng, third["id"]) and not retry(eng, "EVT-NOPE")
    assert deliver(eng, sender=sender)[0]["status"] == "delivered"
    assert [backoff(n).total_seconds() for n in (1, 2, 3, 20)] == [30, 60, 120, 3600]


def test_unset_env_var_and_bad_scheme_are_delivery_errors(monkeypatch):
    eng = make_engine(lambda d: with_webhook(d, url="${HOOK_URL}"))
    submit(eng, 100)
    sender = Recorder()
    assert "HOOK_URL is not set" in deliver(eng, sender=sender)[0]["error"]
    monkeypatch.setenv("HOOK_URL", "file:///etc/passwd")
    later = datetime.now(timezone.utc) + timedelta(hours=2)
    assert "must be http(s)" in deliver(eng, sender=sender, now=later)[0]["error"]
    assert sender.calls == []


@pytest.fixture
def receiver():
    received = []

    class Hook(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200 if len(received) > 1 else 500)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Hook)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/hook", received
    server.shutdown()


def test_real_http_delivery(receiver):
    url, received = receiver
    eng = make_engine(lambda d: with_webhook(d, url=url))
    submit(eng, 100)
    now = datetime.now(timezone.utc)
    assert deliver(eng, now=now)[0]["error"] == "HTTP 500"  # the receiver fails the first call
    assert deliver(eng, now=now + timedelta(minutes=1))[0]["status"] == "delivered"
    assert [r["payload"]["po"] for r in received] == ["PO-0003", "PO-0003"]


def test_cli_delivers_after_run(receiver, tmp_path, capsys, monkeypatch):
    import yaml

    from conftest import PROCUREMENT, raw_schema

    url, received = receiver
    data = raw_schema()
    with_webhook(data, url=url)
    schema = tmp_path / "ontology.yaml"
    schema.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    common = ["--schema", str(schema), "--db", str(tmp_path / "w.db")]
    main(["init", *common, "--seed", str(PROCUREMENT / "seed.json")])
    args = ["run", "submit_purchase_order", *common, "--actor", "alice", "--role", "employee",
            "-p", "title=t", "-p", "amount=5", "-p", "supplier=S-001"]
    assert main(args) == 0
    assert "HTTP 500; will retry" in capsys.readouterr().out and len(received) == 1
    assert main(["deliver", *common]) == 0  # nothing is due yet
    assert main(["outbox", *common, "--status", "pending", "--json"]) == 0
    assert json.loads(capsys.readouterr().out.split("\n", 1)[1])[0]["attempts"] == 1
