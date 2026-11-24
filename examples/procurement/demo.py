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


show("1. Alice 买 8000 元文具（无需审批）",
     engine.execute("submit_purchase_order", {"title": "文具", "amount": 8000, "supplier": "S-001"}, alice))

show("2. Alice 向黑名单供应商下单",
     engine.execute("submit_purchase_order", {"title": "电容", "amount": 3000, "supplier": "S-003"}, alice))

big = show("3. Alice 买 6 万元服务器（需部门经理 + 财务总监）",
           engine.execute("submit_purchase_order", {"title": "服务器 2 台", "amount": 60000, "supplier": "S-002"}, alice))

print("\n== 4. Alice 想自己批")
try:
    engine.approve(big.request_id, Actor("alice", "cfo"))
except ActionError as exc:
    print(f"   blocked: {exc}")

show("5. Bob（部门经理）批准", engine.approve(big.request_id, bob))
show("6. Carol（财务总监）批准 -> 真正执行", engine.approve(big.request_id, carol))

show("7. Alice 取消 Dave 的采购单（她看不到这张单，对她来说等同于不存在）",
     engine.execute("cancel_purchase_order", {"po": "PO-0002"}, alice))
show("7b. Bob 看得到这张单，但规则不允许他取消别人的单",
     engine.execute("cancel_purchase_order", {"po": "PO-0002"}, bob))
show("8. Alice 拉黑供应商（无权限）", engine.execute("blacklist_supplier", {"supplier": "S-002"}, alice))

print("\n== 9. 同一个查询，不同的人看到不同的结果（行级 + 字段级权限）")
for who in (alice, carol):
    orders = [po["po_id"] for po in engine.query("PurchaseOrder", actor=who)]
    account = engine.get("Supplier", "S-001", actor=who)["properties"]["bank_account"]
    print(f"   {who.id:>5} ({who.role}): 采购单 {orders}  S-001 收款账号 {account}")

print("\n== 审计日志")
for row in engine.store.audit_log():
    print(f"   {row['actor']:>6} ({row['role']}) {row['event']:<8} {row['action'] or '':<24} -> {row['outcome']}")
