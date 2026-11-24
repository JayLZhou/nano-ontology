"""Regenerate the console screenshots in ``images/`` (needs ``pip install playwright`` and a Chromium).

    python scripts/console_screenshots.py

Starts a real console in demo mode on a throwaway database, plays a short scenario through the
engine, then drives the page in a headless browser. Nothing is mocked.
"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

from playwright.sync_api import sync_playwright

from nano_onto import Actor, Engine, Ontology, Store
from nano_onto.console import make_console

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "images"
ALICE, BOB, CAROL = Actor("alice", "employee"), Actor("bob", "dept_manager"), Actor("carol", "cfo")


def scenario() -> Engine:
    onto = Ontology.load(ROOT / "examples/procurement/ontology.yaml")
    engine = Engine(onto, Store())
    engine.load_seed(json.loads((ROOT / "examples/procurement/seed.json").read_text(encoding="utf-8")))

    def order(title, amount, supplier, actor=ALICE):
        return engine.execute("submit_purchase_order", {"title": title, "amount": amount, "supplier": supplier}, actor)

    order("Stationery", 8000, "S-001")
    order("Chips", 4800, "S-003")                                   # denied: blacklisted supplier
    done = order("Laptops", 42000, "S-002")                         # needs the department manager
    engine.approve(done.request_id, BOB)
    order("Servers", 60000, "S-002")                                # still waiting for both sign-offs
    engine.execute("cancel_purchase_order", {"po": "PO-0002"}, ALICE)   # dave's order: hidden from alice
    return engine


def main() -> None:
    OUT.mkdir(exist_ok=True)
    server = make_console(scenario(), demo_users=[ALICE, BOB, CAROL], port=0, auditors=("cfo",))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        for theme in ("light", "dark"):
            page = browser.new_page(viewport={"width": 1360, "height": 860}, device_scale_factor=1.5)

            def shot(name: str, user: str, route: str, act=None, height: int = 860) -> None:
                page.set_viewport_size({"width": 1360, "height": height})
                page.goto("about:blank")
                page.goto(f"{base}/?user={user}&theme={theme}#/{route}")
                page.wait_for_load_state("networkidle")
                if act:
                    act()
                    page.wait_for_timeout(250)
                page.screenshot(path=str(OUT / f"console-{name}-{theme}.png"))

            def dry_run() -> None:
                fields = page.locator(".field")
                fields.nth(0).locator("input").fill("Servers")
                fields.nth(1).locator("input").fill("86000")
                fields.nth(2).locator("select").select_option("S-001")
                page.get_by_role("button", name="Dry run").click()
                page.wait_for_selector(".trace")

            shot("ontology", "alice", "ontology", height=1180)
            shot("action", "alice", "actions/submit_purchase_order", dry_run, height=980)
            shot("approvals", "carol", "approvals", height=600)
            shot("data", "alice", "data/Supplier:S-001", height=620)
            shot("audit", "carol", "audit", height=700)
            page.close()
        browser.close()
    server.shutdown()
    print("wrote", ", ".join(sorted(p.name for p in OUT.glob("console-*.png"))))


if __name__ == "__main__":
    sys.exit(main())
