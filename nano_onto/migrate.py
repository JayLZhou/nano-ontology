"""Ontology versioning and migrations.

Every database is stamped with the ontology it follows (name, version, content
fingerprint and a full copy). When the ontology file changes, the engine
refuses to act until the change is reviewed and applied::

    nano-onto migrate --dry-run     # what changed, and what happens to the data
    nano-onto migrate               # apply it in one transaction
    nano-onto history               # every ontology this database has followed

A migration renames, backfills, re-types and drops stored properties to fit the
new schema, and closes pending approval requests that no longer make sense.
Anything it cannot do safely is reported as a *problem* and nothing is applied.
Renames are declared in the ontology with ``renamed_from: <old name>`` on an
object type, property or link type. Types that read through to existing tables
(``source:``) are never rewritten -- that data is not nano-onto's to migrate.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

from .engine import Engine
from .schema import Ontology, coerce, strip_annotations
from .store import Store


@dataclass
class Plan:
    old: str | None  # e.g. "procurement@0.1.0"
    new: str
    changes: list[str] = field(default_factory=list)  # schema differences
    steps: list[str] = field(default_factory=list)  # what will happen to stored data
    problems: list[str] = field(default_factory=list)  # reasons the migration cannot run
    applied: bool = False
    ops: list[Callable[[], None]] = field(default_factory=list, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {"from": self.old, "to": self.new, "changes": self.changes, "steps": self.steps,
                "problems": self.problems, "applied": self.applied}


def _plain(raw: Any) -> Any:
    """Comparable form of a raw ontology: no ``x-`` keys, JSON types, rules keyed by id."""
    def keyed(value: Any, key: str | None = None) -> Any:
        if isinstance(value, dict):
            return {k: keyed(v, k) for k, v in value.items()}
        if isinstance(value, list) and key in ("rules", "effects"):
            return {(str(v.get("id")) if isinstance(v, dict) and key == "rules" and v.get("id") else f"#{i + 1}"): keyed(v)
                    for i, v in enumerate(value)}
        return value

    return keyed(json.loads(json.dumps(strip_annotations(raw), default=str)))


def _short(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= 80 else text[:77] + "..."


def diff(old: Any, new: Any, path: str = "") -> list[str]:
    """Differences between two raw ontologies, one line each: ``+`` added, ``-`` removed, ``~`` changed."""
    if not path:
        old, new = _plain(old), _plain(new)
    if isinstance(old, dict) and isinstance(new, dict):
        out = []
        for key in sorted(set(old) | set(new)):
            sub = f"{path}.{key}" if path else key
            if key not in old:
                out.append(f"+ {sub}" + ("" if isinstance(new[key], dict) else f": {_short(new[key])}"))
            elif key not in new:
                out.append(f"- {sub}")
            else:
                out.extend(diff(old[key], new[key], sub))
        return out
    return [] if old == new else [f"~ {path}: {_short(old)} -> {_short(new)}"]


def _renamed(spec: Any, name: str, old_names: dict[str, Any], new_names: dict[str, Any], out: Plan | None = None) -> str:
    """Name this type/property/link had in the old ontology."""
    previous = getattr(spec, "renamed_from", None)
    if name in old_names or previous not in old_names:
        return name
    if previous in new_names:  # both exist: moving the data would empty the one that stays
        if out is not None:
            out.problems.append(f"{name}: renamed_from {previous!r}, but {previous!r} still exists in the new ontology")
        return name
    return previous


def plan(store: Store, onto: Ontology, drop_removed: bool = False) -> Plan:
    stamp = store.get_meta("ontology")
    if stamp is None:
        return Plan(None, onto.stamp, steps=["database has no ontology yet; it will be initialised"])
    out = Plan(f"{stamp['name']}@{stamp['version']}", onto.stamp)
    if stamp["fingerprint"] == onto.fingerprint():
        return out
    old = stamp["schema"]
    out.changes = diff(old, onto.raw) or ["~ reordered or reformatted only (same types, rules and effects)"]
    if str(stamp["version"]) == onto.version:
        out.steps.append("note: the content changed but `version` did not; consider bumping it")
    _plan_objects(store, onto, old.get("object_types") or {}, drop_removed, out)
    _plan_links(store, onto, old.get("link_types") or {}, drop_removed, out)
    for req in store.list_approvals("pending"):
        action = onto.action_types.get(req["action"])
        role = req["actor"].get("role")
        gone = sorted(r for r in req["required_roles"] if r not in req["approved_by"] and r not in onto.roles)
        if action is None:
            reason = f"action {req['action']} was removed"
        elif (onto.roles and role not in onto.roles) or (action.allowed_roles is not None
                                                        and role not in action.allowed_roles):
            reason = f"role {role} may no longer run {action.name}"
        elif gone:
            reason = f"it waits for approval by a role that no longer exists ({', '.join(gone)})"
        else:
            unknown = sorted(set(req["params"]) - set(action.parameters))
            missing = sorted(p.name for p in action.parameters.values()
                             if p.required and req["params"].get(p.name) is None and p.default is None)
            if not unknown and not missing:
                continue
            reason = "its parameters no longer fit the action" + (
                f" (unknown: {', '.join(unknown)})" if unknown else "") + (
                f" (missing: {', '.join(missing)})" if missing else "")
        out.steps.append(f"close pending request {req['id']} as stale: {reason}")
        out.ops.append(lambda rid=req["id"]: store.update_approval(rid, status="stale"))
    return out


def _plan_objects(store: Store, onto: Ontology, old_types: dict[str, Any], drop_removed: bool, out: Plan) -> None:
    kept = set()
    for ot in onto.object_types.values():
        source = _renamed(ot, ot.name, old_types, onto.object_types, out)
        kept.add(source)
        if ot.table or source not in old_types or (old_types[source] or {}).get("source"):
            continue
        old_spec = old_types[source] or {}
        old_props = old_spec.get("properties") or {}
        old_pk = old_spec.get("primary_key") or next(iter(old_props), "id")
        rewritten, dropped, filled = 0, set(), set()
        for row in store.query(source, None, None):
            new: dict[str, Any] = {}
            used = set()
            for prop in ot.properties.values():
                key = _renamed(prop, prop.name, row, ot.properties)
                used.add(key)
                value = row.get(key)
                where = f"{ot.name} {row.get(old_pk)!r}: {prop.name}"
                if value is None and prop.default is not None:
                    value = prop.default
                    filled.add(prop.name)
                if value is None:
                    if prop.required:
                        out.problems.append(f"{where} is required but has no value and no default")
                else:
                    try:
                        value = coerce(prop.type, value)
                        if prop.enum and value not in prop.enum:
                            out.problems.append(f"{where}={value!r} is not one of {prop.enum}")
                    except (TypeError, ValueError):
                        out.problems.append(f"{where}={value!r} is not a valid {prop.type}")
                new[prop.name] = value
            dropped |= set(row) - used
            old_key, new_key = row.get(old_pk), new.get(ot.primary_key)
            if new_key is None or str(new_key) != str(old_key):
                out.problems.append(f"{ot.name} {old_key!r}: primary key would change to {new_key!r} (not supported)")
            elif new != row or source != ot.name:
                rewritten += 1
                if source != ot.name:
                    out.ops.append(lambda s=source, k=old_key: store.delete(s, k))
                out.ops.append(lambda t=ot.name, k=new_key, p=new: store.put(t, k, p))
        if source != ot.name:
            out.steps.append(f"rename object type {source} -> {ot.name}")
            out.ops.append(lambda s=source, t=ot.name: _move_counter(store, s, t))
        if rewritten:
            notes = [f"drop {', '.join(sorted(dropped))}" if dropped else "",
                     f"backfill {', '.join(sorted(filled))}" if filled else ""]
            out.steps.append(f"rewrite {rewritten} {ot.name} object(s)" + "".join(f"; {n}" for n in notes if n))
    for name in sorted(set(old_types) - kept):
        rows = [] if (old_types[name] or {}).get("source") else store.query(name, None, None)
        if not rows:
            continue
        pk = (old_types[name] or {}).get("primary_key") or next(iter((old_types[name] or {}).get("properties") or {}), "id")
        if drop_removed:
            out.steps.append(f"delete {len(rows)} object(s) of removed type {name}")
            for key in [row.get(pk) for row in rows]:
                out.ops.append(lambda n=name, k=key: store.delete(n, k))
        else:
            out.problems.append(f"{len(rows)} object(s) of removed type {name} are still stored "
                                "(declare `renamed_from`, or pass --drop-removed to delete them)")


def _move_counter(store: Store, old: str, new: str) -> None:
    rows = store._rows("SELECT value FROM onto_counters WHERE name = ?", (old,))
    if rows:
        store._set_counter(new, rows[0]["value"])


def _plan_links(store: Store, onto: Ontology, old_links: dict[str, Any], drop_removed: bool, out: Plan) -> None:
    def stored(name: str) -> int:
        return store._rows("SELECT COUNT(*) AS n FROM onto_links WHERE link = ?", (name,))[0]["n"]

    kept = set()
    for lt in onto.link_types.values():
        source = _renamed(lt, lt.name, old_links, onto.link_types, out)
        kept.add(source)
        if lt.column or lt.table or source not in old_links:
            continue
        if source != lt.name:
            out.steps.append(f"rename link type {source} -> {lt.name} ({stored(source)} link(s))")
            out.ops.append(lambda s=source, t=lt.name: store._run(
                "UPDATE onto_links SET link = ? WHERE link = ?", (t, s)))
        for single, column, end in ((lt.single, "from_pk", lt.from_type), (lt.unique_target, "to_pk", lt.to_type)):
            if single:
                for row in store._rows(f"SELECT {column} AS pk, COUNT(*) AS n FROM onto_links WHERE link = ? "
                                       f"GROUP BY {column} HAVING COUNT(*) > 1", (source,)):
                    out.problems.append(f"link {lt.name}: {end} {row['pk']!r} has {row['n']} links, "
                                        f"but the cardinality is now {lt.cardinality}")
    for name in sorted(set(old_links) - kept):
        count = stored(name)
        if not count:
            continue
        if drop_removed:
            out.steps.append(f"delete {count} link(s) of removed link type {name}")
            out.ops.append(lambda n=name: store.remove_link(n))
        else:
            out.problems.append(f"{count} link(s) of removed link type {name} are still stored "
                                "(declare `renamed_from`, or pass --drop-removed to delete them)")


def migrate(store: Store, onto: Ontology, actor: str = "admin", dry_run: bool = False,
            drop_removed: bool = False) -> Plan:
    """Plan the move to ``onto`` and, unless ``dry_run`` or there are problems, apply it atomically."""
    with store.transaction():
        out = plan(store, onto, drop_removed)
        if out.old is None:
            if not dry_run:
                Engine(onto, store)  # stamps a fresh database
                out.applied = True
            return out
        if dry_run or out.problems or not out.changes:
            return out
        for op in out.ops:
            op()
        engine = Engine(onto, store, check_version=False)
        engine.stamp("migrated", **{"from": out.old, "by": actor, "changes": out.changes, "steps": out.steps})
        store.log({"id": actor, "role": "admin"}, "migrate", None, "applied",
                  {"from": out.old, "to": out.new, "changes": out.changes, "steps": out.steps})
        out.applied = True
    return out
