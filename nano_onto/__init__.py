"""nano-onto: an executable ontology for AI agents.

Objects and links say what exists. Actions say what may happen, under which
rules, with whose approval -- and nano-onto enforces that outside the model.
"""

__version__ = "0.2.0"

from .engine import ActionError, ActionResult, Actor, Engine, ObjectView, VersionError  # noqa: E402
from .expr import ExprError  # noqa: E402
from .schema import Ontology, SchemaError  # noqa: E402
from .store import Store, StoreError, open_store  # noqa: E402

__all__ = [
    "__version__",
    "ActionError",
    "ActionResult",
    "Actor",
    "Engine",
    "ExprError",
    "ObjectView",
    "Ontology",
    "SchemaError",
    "Store",
    "StoreError",
    "VersionError",
    "open_store",
]
