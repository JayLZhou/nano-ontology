"""nano-onto procurement demo: the same requests an agent would make, with the
ontology deciding what is allowed.  Run:  python examples/procurement/demo.py
"""

import json
from pathlib import Path

from nano_onto import ActionError, Actor, Engine, Ontology, Store

HERE = Path(__file__).parent

onto = Ontology.load(HERE / "ontology.yaml")
engine = Engine(onto, Store())  # in-memory
engine.load_seed(json.loads((HERE / "seed.json").read_text(encoding="utf-8")))

alice = Actor("alice", "employee")
bob = Actor("bob", "dept_manager")
carol = Actor("carol", "cfo")


def show(title, res):
    print(f"\n== {title}")
    print(f"   status: {res.status}")
    for m in res.messages:
        print(f"   - {m}")
    if res.request_id and res.required_approvals:
        print(f"   request {res.request_id} waiting for {res.required_approvals}")
    if res.created:
        print(f"   created: {res.created}")
    return res


def order(title, amount, supplier, actor=alice):
    return engine.execute("submit_purchase_order", {"title": title, "amount": amount, "supplier": supplier}, actor)


show("1. Alice orders 8,000 of stationery (no approval needed)", order("Stationery", 8000, "S-001"))
show("2. Alice orders from a blacklisted supplier", order("Chips", 4800, "S-003"))
big = show("3. Alice orders 60,000 of servers (department manager + CFO must approve)", order("Servers", 60000, "S-002"))

print("\n== 4. Alice tries to approve it herself")
try:
    engine.approve(big.request_id, Actor("alice", "cfo"))
except ActionError as exc:
    print(f"   blocked: {exc}")

show("5. Bob (department manager) approves", engine.approve(big.request_id, bob))
show("6. Carol (CFO) approves -> now it really happens", engine.approve(big.request_id, carol))

show("7. Alice cancels Dave's order (she cannot see it, so to her it does not exist)",
     engine.execute("cancel_purchase_order", {"po": "PO-0002"}, alice))
show("7b. Bob can see it, but the rules do not let him cancel someone else's order",
     engine.execute("cancel_purchase_order", {"po": "PO-0002"}, bob))
show("8. Alice blacklists a supplier (not her role)", engine.execute("blacklist_supplier", {"supplier": "S-002"}, alice))

print("\n== 9. The same query, different answers (row- and property-level permissions)")
for who in (alice, carol):
    orders = [po["po_id"] for po in engine.query("PurchaseOrder", actor=who)]
    account = engine.get("Supplier", "S-001", actor=who)["properties"]["bank_account"]
    print(f"   {who.id:>5} ({who.role}): orders {orders}  S-001 bank account {account}")

print("\n== Audit log")
for row in engine.store.audit_log():
    print(f"   {row['actor']:>6} ({row['role']}) {row['event']:<8} {row['action'] or '':<24} -> {row['outcome']}")
