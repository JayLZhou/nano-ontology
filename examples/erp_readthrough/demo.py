"""Read-through + webhook demo: nano-onto governs tables that already exist.

Run:  python examples/erp_readthrough/demo.py
"""

import hashlib
import hmac
import json
import os
import sqlite3
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from nano_onto import Actor, Engine, Ontology, open_store
from nano_onto.webhooks import deliver

HERE = Path(__file__).parent
db = os.path.join(tempfile.mkdtemp(), "erp.db")

# 1. An "existing" ERP database that nano-onto did not create ------------------------
legacy = sqlite3.connect(db)
legacy.executescript("""
CREATE TABLE suppliers (id INTEGER PRIMARY KEY, supplier_name TEXT NOT NULL, credit_limit REAL, active INTEGER DEFAULT 1);
CREATE TABLE purchase_orders (po_no TEXT PRIMARY KEY, title TEXT NOT NULL, amount REAL NOT NULL CHECK (amount > 0),
                              state TEXT DEFAULT 'open', requested_by TEXT, supplier_id INTEGER REFERENCES suppliers(id));
CREATE TABLE po_tags (po_no TEXT, tag TEXT);
INSERT INTO suppliers VALUES (1, 'Acme Tools', 5000, 1), (2, 'Globex', 100000, 1), (3, 'Initech', 2000, 0);
""")
legacy.commit()
legacy.close()

# 2. A stand-in for the ERP's webhook endpoint ------------------------------------------
received = []


class Hook(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        body = self.rfile.read(int(self.headers["Content-Length"]))
        expected = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
        received.append((json.loads(body), self.headers["X-Nano-Onto-Signature"] == expected))
        self.send_response(204)
        self.end_headers()

    def log_message(self, *args):
        pass


hook = HTTPServer(("127.0.0.1", 0), Hook)
threading.Thread(target=hook.serve_forever, daemon=True).start()
os.environ["ERP_HOOK_URL"] = f"http://127.0.0.1:{hook.server_address[1]}/hooks/po"
os.environ["ERP_HOOK_SECRET"] = "s3cret"

# 3. Govern it ----------------------------------------------------------------------------
onto = Ontology.load(HERE / "ontology.yaml")
engine = Engine(onto, open_store(db, onto))
engine.load_seed({"objects": {"Tag": [{"tag": "urgent"}]}})
dana, frank = Actor("dana", "buyer"), Actor("frank", "finance")


def show(title, res):
    print(f"\n== {title}\n   status: {res.status}")
    for message in res.messages:
        print(f"   - {message}")
    return res


show("1. Dana orders 3,000 from Acme (limit 5,000)",
     engine.execute("create_order", {"title": "drills", "amount": 3000, "supplier": 1, "tag": "urgent"}, dana))
show("2. Dana orders from an inactive supplier",
     engine.execute("create_order", {"title": "chairs", "amount": 100, "supplier": 3}, dana))
big = show("3. Dana orders 9,000 from Acme (over the limit -> finance must approve)",
           engine.execute("create_order", {"title": "lathe", "amount": 9000, "supplier": 1}, dana))
show("4. Frank approves", engine.approve(big.request_id, frank))
show("5. The table's own CHECK constraint still applies (amount > 0): rolled back",
     engine.execute("create_order", {"title": "refund", "amount": -5, "supplier": 2}, dana))
show("6. Dana tags an order", engine.execute("tag_order", {"po": "PO-0001", "tag": "urgent"}, dana))

print("\n== The rows are in the ERP's own tables")
check = sqlite3.connect(db)
for row in check.execute("SELECT po_no, title, amount, state, requested_by, supplier_id FROM purchase_orders"):
    print("   purchase_orders:", row)
print("   po_tags:", check.execute("SELECT * FROM po_tags").fetchall())

print("\n== Webhooks queued in the action's transaction, delivered after commit")
for item in deliver(engine):
    print(f"   {item['id']} -> {item['status']}")
for body, signature_ok in received:
    print(f"   ERP received {body['event']} {body['payload']} (signature ok: {signature_ok})")
hook.shutdown()
