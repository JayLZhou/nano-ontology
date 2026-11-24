"""The same engine on SQLite, DuckDB and PostgreSQL, with and without read-through tables.

DuckDB runs if ``duckdb`` is installed. PostgreSQL runs against ``$NANO_ONTO_TEST_PG``
(a connection URL to a scratch database) or, if the ``pgserver`` package is installed,
against a throwaway embedded server.
"""

import os

import pytest

from conftest import ALICE, BOB, CAROL, EXAMPLES, make_engine, submit
from nano_onto import Actor, Engine, Ontology, Store, StoreError, open_store
from nano_onto.migrate import migrate

DANA, FRANK = Actor("dana", "buyer"), Actor("frank", "finance")


@pytest.fixture(scope="session")
def postgres_url(tmp_path_factory):
    url = os.environ.get("NANO_ONTO_TEST_PG")
    if url:
        pytest.importorskip("psycopg")
        yield url
        return
    pytest.importorskip("psycopg")
    pgserver = pytest.importorskip("pgserver")
    server = pgserver.get_server(tmp_path_factory.mktemp("pgdata"))
    yield server.get_uri()
    server.cleanup()


@pytest.fixture(params=["sqlite", "duckdb", "postgres"])
def url(request, tmp_path):
    if request.param == "sqlite":
        return str(tmp_path / "t.db")
    if request.param == "duckdb":
        pytest.importorskip("duckdb")
        return f"duckdb://{tmp_path / 't.duckdb'}"
    pg = request.getfixturevalue("postgres_url")
    reset = Store(pg)
    reset._run("DROP SCHEMA public CASCADE")
    reset._run("CREATE SCHEMA public")
    reset.close()
    return pg


def test_document_store_end_to_end(url):
    eng = make_engine(store=Store(url))
    assert submit(eng, 8000).created == {"po": "PO-0003"}
    assert submit(eng, 100, supplier="S-003").status == "denied"
    held = submit(eng, 60000)
    assert eng.approve(held.request_id, BOB).required_approvals == ["cfo"]
    assert eng.approve(held.request_id, CAROL).created == {"po": "PO-0004"}
    assert eng.store.links_from("supplier", "PO-0004") == ["S-001"]
    assert eng.store.links_to("supplier", "S-001") == ["PO-0001", "PO-0003", "PO-0004"]
    assert [po["po_id"] for po in eng.query("PurchaseOrder", actor=ALICE)] == ["PO-0001", "PO-0003", "PO-0004"]
    assert eng.execute("cancel_purchase_order", {"po": "PO-0003"}, ALICE).updated == ["PurchaseOrder:PO-0003"]
    assert eng.store.get("PurchaseOrder", "PO-0003")["status"] == "cancelled"
    assert [r["seq"] for r in eng.store.audit_log()] == list(range(1, 8))
    assert [r["outcome"] for r in eng.store.audit_log()][-1] == "executed"

    eng.store.close()
    again = Engine(eng.onto, Store(url))  # reopen: data, stamp and counters persist
    assert again.store.count("PurchaseOrder") == 4 and submit(again, 5).created == {"po": "PO-0005"}

    def add_country(data):
        data["version"] = "0.2.0"
        data["object_types"]["Supplier"]["properties"]["country"] = {"type": "string", "default": "CN"}

    from conftest import raw_schema
    data = raw_schema()
    add_country(data)
    assert migrate(again.store, Ontology.from_dict(data)).applied
    assert again.store.get("Supplier", "S-001")["country"] == "CN"


def test_failed_effects_roll_back(url):
    def broken(data):
        data["action_types"]["submit_purchase_order"]["effects"][1]["to"] = "'S-404'"

    eng = make_engine(broken, Store(url))
    assert submit(eng, 100).status == "invalid"
    assert eng.store.count("PurchaseOrder") == 2 and eng.store.outbox() == []
    assert [r["outcome"] for r in eng.store.audit_log()] == ["invalid"]
    assert submit(eng, 100, supplier="S-003").status == "denied"  # the connection is still usable


# ------------------------------------------------------------- read-through
_NATIVE = {  # column types as each database would really have them
    "sqlite": ("INTEGER", "REAL", "INTEGER", "TEXT"),
    "duckdb": ("INTEGER", "DOUBLE", "BOOLEAN", "DATE"),
    "postgres": ("INTEGER", "NUMERIC(12,2)", "BOOLEAN", "DATE"),
}


@pytest.fixture
def erp(url):
    setup = Store(url)
    integer, number, boolean, date = _NATIVE[setup.dialect]
    for statement in (
        f"CREATE TABLE suppliers (id {integer} PRIMARY KEY, supplier_name TEXT NOT NULL, "
        f"credit_limit {number}, active {boolean}, legacy_code TEXT)",
        f"CREATE TABLE purchase_orders (po_no TEXT PRIMARY KEY, title TEXT NOT NULL, amount {number} NOT NULL "
        f"CHECK (amount > 0), state TEXT, requested_by TEXT, due {date}, supplier_id {integer})",
        "CREATE TABLE po_tags (po_no TEXT, tag TEXT)",
        "INSERT INTO suppliers VALUES (1, 'Acme Tools', 5000, ?, 'A-1')",
        "INSERT INTO suppliers VALUES (2, 'Globex', 100000, ?, 'G-7')",
        "INSERT INTO suppliers VALUES (3, 'Initech', 2000, ?, 'I-3')",
    ):
        setup._run(statement, [not statement.endswith("'I-3')")] if "?" in statement else [])
    setup._run("INSERT INTO purchase_orders VALUES ('PO-0001', 'old order', 10, 'open', 'dana', ?, 2)", ("2026-03-01",))
    setup.close()

    import yaml
    data = yaml.safe_load((EXAMPLES / "erp_readthrough" / "ontology.yaml").read_text(encoding="utf-8"))
    data["object_types"]["PurchaseOrder"]["properties"]["due"] = {"type": "date"}
    data["action_types"]["create_order"]["parameters"]["due"] = {"type": "date", "required": False}
    data["action_types"]["create_order"]["effects"][0]["set"]["due"] = "params.due"
    onto = Ontology.from_dict(data)
    store = open_store(url, onto)
    engine = Engine(onto, store)
    engine.load_seed({"objects": {"Tag": [{"tag": "urgent"}, {"tag": "capex"}]}})
    yield engine
    store.close()


def table(engine, sql):
    return [tuple(row.values()) for row in engine.store._rows(sql)]


def test_reads_come_from_the_existing_tables(erp):
    assert erp.store.get("Supplier", "1") == {"supplier_id": 1, "name": "Acme Tools", "credit_limit": 5000, "active": True}
    assert erp.store.get("Supplier", "3")["active"] is False
    assert erp.store.get("Supplier", "99") is None and erp.store.get("Supplier", "abc") is None
    assert erp.store.get("PurchaseOrder", "PO-0001")["due"] == "2026-03-01"  # native DATE -> ISO string
    assert erp.store.count("Supplier") == 3
    assert [s["supplier_id"] for s in erp.query("Supplier", {"active": "true"})] == [1, 2]
    assert [s["name"] for s in erp.query("Supplier", limit=1)] == ["Acme Tools"]
    assert erp.query("Supplier", {"credit_limit": None}) == []
    assert erp.get("PurchaseOrder", "PO-0001")["links"] == {"supplier": ["2"], "tags": []}
    assert erp.store.links_to("supplier", "2") == ["PO-0001"]
    assert erp.store.count("Tag") == 2  # unmapped types still live in nano-onto's own tables


def test_actions_write_through_in_one_transaction(erp):
    res = erp.execute("create_order", {"title": "drills", "amount": "3000", "supplier": 1, "due": "2026-12-24"}, DANA)
    assert res.status == "executed" and res.created == {"po": "PO-0002"} and len(res.webhooks) == 1
    assert table(erp, "SELECT po_no, title, state, requested_by, supplier_id FROM purchase_orders ORDER BY po_no")[1] == (
        "PO-0002", "drills", "open", "dana", 1)
    assert erp.store.get("PurchaseOrder", "PO-0002") == {
        "po_no": "PO-0002", "title": "drills", "amount": 3000, "status": "open", "requester": "dana", "due": "2026-12-24"}
    assert erp.store.outbox()[0]["body"]["payload"] == {"po_no": "PO-0002", "amount": 3000, "supplier_id": 1}

    assert erp.execute("create_order", {"title": "x", "amount": 1, "supplier": 3}, DANA).status == "denied"
    held = erp.execute("create_order", {"title": "lathe", "amount": 9000, "supplier": 1}, DANA)
    assert held.status == "pending_approval" and erp.store.count("PurchaseOrder") == 2
    assert erp.approve(held.request_id, FRANK).created == {"po": "PO-0003"}

    assert erp.execute("deactivate_supplier", {"supplier": 2}, FRANK).updated == ["Supplier:2"]
    # only the changed column is written; columns the ontology does not map are left alone
    assert table(erp, "SELECT supplier_name, legacy_code FROM suppliers WHERE id = 2") == [("Globex", "G-7")]
    assert erp.store.get("Supplier", "2")["active"] is False
    assert erp.execute("create_order", {"title": "x", "amount": 1, "supplier": 2}, DANA).status == "denied"


def test_database_constraints_still_apply_and_roll_back(erp):
    res = erp.execute("create_order", {"title": "refund", "amount": -5, "supplier": 2}, DANA)
    assert res.status == "invalid" and res.created == {} and res.webhooks == []
    assert erp.store.count("PurchaseOrder") == 1 and erp.store.outbox() == []
    assert erp.store.audit_log()[-1]["outcome"] == "invalid"
    assert erp.execute("create_order", {"title": "ok", "amount": 5, "supplier": 2}, DANA).status == "executed"


def test_join_table_and_foreign_key_links(erp):
    for tag in ("urgent", "capex", "urgent"):  # linking twice is idempotent
        assert erp.execute("tag_order", {"po": "PO-0001", "tag": tag}, DANA).status == "executed"
    assert sorted(table(erp, "SELECT po_no, tag FROM po_tags")) == [("PO-0001", "capex"), ("PO-0001", "urgent")]
    assert erp.get("PurchaseOrder", "PO-0001")["links"]["tags"] == ["capex", "urgent"]
    assert erp.store.links_to("tags", "urgent") == ["PO-0001"]
    erp.store.remove_link("tags", "PO-0001", "capex")
    assert erp.store.links_from("tags", "PO-0001") == ["urgent"]
    erp.store.remove_link("supplier", "PO-0001")  # clears the foreign key, keeps the row
    assert table(erp, "SELECT supplier_id FROM purchase_orders WHERE po_no = 'PO-0001'") == [(None,)]
    assert erp.store.links_from("supplier", "PO-0001") == []


def test_missing_table_or_column_fails_at_startup(url):
    import yaml
    data = yaml.safe_load((EXAMPLES / "erp_readthrough" / "ontology.yaml").read_text(encoding="utf-8"))
    with pytest.raises(StoreError):
        open_store(url, Ontology.from_dict(data))


def test_mapped_ontology_needs_open_store():
    onto = Ontology.load(EXAMPLES / "erp_readthrough" / "ontology.yaml")
    with pytest.raises(Exception, match="open_store"):
        Engine(onto, Store())
