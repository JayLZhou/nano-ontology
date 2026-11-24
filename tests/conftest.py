import copy
import json
from pathlib import Path

import pytest
import yaml

from nano_onto import Actor, Engine, Ontology, Store

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
PROCUREMENT = EXAMPLES / "procurement"
ALICE = Actor("alice", "employee")
BOB = Actor("bob", "dept_manager")
CAROL = Actor("carol", "cfo")
_RAW = yaml.safe_load((PROCUREMENT / "ontology.yaml").read_text(encoding="utf-8"))
_SEED = json.loads((PROCUREMENT / "seed.json").read_text(encoding="utf-8"))


def raw_schema() -> dict:
    return copy.deepcopy(_RAW)


def seed() -> dict:
    return copy.deepcopy(_SEED)


def make_engine(mutate=None, store=None) -> Engine:
    """Procurement engine on a fresh store; ``mutate(raw)`` edits the ontology first."""
    data = raw_schema()
    if mutate:
        mutate(data)
    eng = Engine(Ontology.from_dict(data), store or Store())
    eng.load_seed(seed())
    return eng


def submit(engine, amount, supplier="S-001", actor=ALICE, **extra):
    return engine.execute(
        "submit_purchase_order", {"title": "t", "amount": amount, "supplier": supplier, **extra}, actor
    )


@pytest.fixture
def engine():
    return make_engine()
