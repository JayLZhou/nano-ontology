"""Ontology schema: object types, link types, roles and *action types*.

An ontology is written in YAML (see ``examples/procurement/ontology.yaml``).
Loading it validates everything up front -- types, references inside rule
expressions, roles, effects -- so a broken ontology fails at load time rather
than in the middle of an agent's action. Unknown keys are errors: a typo such
as ``allowed_role:`` must not silently leave an action open to everyone.

Keys starting with ``x-`` are ignored everywhere, so other tools can attach their own
annotations (review notes, provenance) without breaking the loader.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml

from . import expr

PRIMITIVE_TYPES = {"string", "number", "integer", "boolean", "date"}
CARDINALITIES = {"many_to_one", "one_to_one", "one_to_many", "many_to_many"}
RULE_OUTCOMES = {"deny", "warn", "require_approval"}
EFFECT_KINDS = ("create", "update", "link", "unlink", "webhook")
ACTOR_FIELDS = {"id", "role"}
RESERVED_NAMES = {"params", "actor", "obj", *expr.FUNCTIONS}

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")  # \Z, not $: a trailing newline is not part of a name
_ACTION_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_-]{0,63}\Z")
_SQL_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?\Z")
_INT = re.compile(r"[+-]?[0-9]+")
_FLOAT = re.compile(r"[+-]?([0-9]+\.?[0-9]*|\.[0-9]+)([eE][+-]?[0-9]+)?")

_TOP_KEYS = {"name", "version", "description", "roles", "object_types", "link_types", "action_types", "webhooks"}
_ROLE_KEYS = {"label", "description"}
_OBJECT_KEYS = {"label", "description", "primary_key", "id_prefix", "properties", "readable_by",
                "visible_when", "source", "renamed_from"}
_PROPERTY_KEYS = {"type", "required", "enum", "default", "label", "description", "readable_by", "renamed_from"}
_LINK_KEYS = {"from", "to", "cardinality", "label", "description", "source", "renamed_from"}
_ACTION_KEYS = {"label", "description", "allowed_roles", "parameters", "rules", "effects"}
_PARAM_KEYS = {"type", "object_type", "required", "enum", "default", "label", "description"}
_RULE_KEYS = {"id", "when", "then", "message", "source", "description"}
_EFFECT_KEYS = {*EFFECT_KINDS, "as", "set", "from", "to", "event", "payload"}
_WEBHOOK_KEYS = {"url", "headers", "secret_env", "max_attempts", "timeout", "description"}

_TRUE = {"true", "yes", "y", "1", "是", "真"}
_FALSE = {"false", "no", "n", "0", "否", "假"}


class SchemaError(ValueError):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("invalid ontology:\n  - " + "\n  - ".join(errors))


def coerce(type_name: str, value: Any) -> Any:
    """Coerce ``value`` to a primitive ontology type; raises ``ValueError`` if it does not fit."""
    if type_name == "string":
        if isinstance(value, (dict, list)):
            raise ValueError("not a string")
        return str(value)
    if type_name in ("integer", "number"):
        if isinstance(value, bool):
            raise ValueError("booleans are not numbers")
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            text = value.strip()
            if _INT.fullmatch(text):
                return int(text)  # exact: never through float, which loses digits above 2**53
            if not _FLOAT.fullmatch(text):
                raise ValueError("not a number")
            value = text
        try:
            number = float(value)
        except OverflowError:
            raise ValueError("number out of range") from None
        if not math.isfinite(number):  # NaN compares False with everything and would slip past every rule
            raise ValueError("not a finite number")
        if not number.is_integer():
            if type_name == "integer":
                raise ValueError("not an integer")
            return number
        if abs(number) >= 2**53 and type_name == "integer":
            raise ValueError("write large integers without a decimal point or exponent")
        return int(number)
    if type_name == "boolean":
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in _TRUE:
            return True
        if text in _FALSE:
            return False
        raise ValueError("not a boolean")
    if type_name == "date":
        text = str(value).strip()
        return (date.fromisoformat(text) if len(text) == 10 else datetime.fromisoformat(text).date()).isoformat()
    raise ValueError(f"unknown type {type_name}")


@dataclass
class PropertySpec:
    name: str
    type: str = "string"
    required: bool = False
    enum: list[Any] | None = None
    default: Any = None
    label: str | None = None
    description: str | None = None
    readable_by: list[str] | None = None  # None = every role may read it
    renamed_from: str | None = None


@dataclass
class ObjectType:
    name: str
    primary_key: str
    properties: dict[str, PropertySpec]
    label: str | None = None
    description: str | None = None
    id_prefix: str | None = None
    readable_by: list[str] | None = None  # roles that may read objects of this type at all
    visible_when: str | None = None  # row filter over ``obj`` and ``actor``
    table: str | None = None  # read-through: rows live in this existing table
    columns: dict[str, str] = field(default_factory=dict)  # property -> column
    renamed_from: str | None = None

    @property
    def restricted(self) -> bool:
        return bool(self.readable_by or self.visible_when or any(p.readable_by for p in self.properties.values()))

    def column(self, prop: str) -> str:
        return self.columns.get(prop, prop)


@dataclass
class LinkType:
    name: str
    from_type: str
    to_type: str
    cardinality: str = "many_to_one"
    label: str | None = None
    description: str | None = None
    column: str | None = None  # read-through: foreign-key column on the from-type's table
    table: str | None = None  # read-through: join table ...
    from_column: str | None = None  # ... and its two columns
    to_column: str | None = None
    renamed_from: str | None = None

    @property
    def single(self) -> bool:
        return self.cardinality in ("many_to_one", "one_to_one")

    @property
    def unique_target(self) -> bool:
        return self.cardinality in ("one_to_one", "one_to_many")


@dataclass
class ParamSpec:
    name: str
    type: str = "string"
    required: bool = True
    object_type: str | None = None
    enum: list[Any] | None = None
    default: Any = None
    description: str | None = None


@dataclass
class Rule:
    id: str
    when: str
    outcome: str  # deny | warn | require_approval
    role: str | None = None
    message: str | None = None
    source: dict[str, Any] | None = None


@dataclass
class Effect:
    kind: str  # create | update | link | unlink | webhook
    target: str  # object type (create), expression (update), link type (link/unlink) or webhook name
    alias: str | None = None
    values: dict[str, Any] = field(default_factory=dict)  # ``set`` or webhook ``payload``
    from_expr: str | None = None
    to_expr: str | None = None
    event: str | None = None


@dataclass
class ActionType:
    name: str
    parameters: dict[str, ParamSpec]
    rules: list[Rule]
    effects: list[Effect]
    label: str | None = None
    description: str | None = None
    allowed_roles: list[str] | None = None


@dataclass
class Ontology:
    name: str
    version: str
    roles: dict[str, dict[str, Any]]
    object_types: dict[str, ObjectType]
    link_types: dict[str, LinkType]
    action_types: dict[str, ActionType]
    webhooks: dict[str, dict[str, Any]] = field(default_factory=dict)
    description: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    # ------------------------------------------------------------------ loading
    @classmethod
    def load(cls, path: str | Path) -> "Ontology":
        with open(path, encoding="utf-8") as fh:
            try:
                data = yaml.safe_load(fh)
            except yaml.YAMLError as exc:
                raise SchemaError([f"{path}: not valid YAML: {exc}"]) from None
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Ontology":
        errors: list[str] = []
        if not isinstance(data, dict):
            raise SchemaError(["ontology must be a mapping"])
        top = _mapping(data, "ontology", errors, _TOP_KEYS)
        onto = cls(
            name=str(top.get("name", "ontology")),
            version=str(top.get("version", "0.1.0")),
            roles=_parse_roles(top.get("roles"), errors),
            object_types={},
            link_types={},
            action_types={},
            description=top.get("description"),
            raw=data,
        )
        for name, spec in _mapping(top.get("object_types"), "object_types", errors).items():
            onto.object_types[name] = _parse_object_type(name, spec, errors)
        for name, spec in _mapping(top.get("link_types"), "link_types", errors).items():
            onto.link_types[name] = _parse_link_type(name, spec, errors)
        for name, spec in _mapping(top.get("webhooks"), "webhooks", errors).items():
            onto.webhooks[name] = _parse_webhook(name, spec, errors)
        for name, spec in _mapping(top.get("action_types"), "action_types", errors).items():
            onto.action_types[name] = _parse_action(name, spec, errors)
        if not errors:
            errors.extend(onto.check())
        if errors:
            raise SchemaError(errors)
        return onto

    # --------------------------------------------------------------- helpers
    def links_from(self, type_name: str) -> dict[str, LinkType]:
        return {n: lt for n, lt in self.link_types.items() if lt.from_type == type_name}

    def links_to(self, type_name: str) -> dict[str, LinkType]:
        return {n: lt for n, lt in self.link_types.items() if lt.to_type == type_name}

    def role_label(self, role: str) -> str:
        label = self.roles.get(role, {}).get("label")
        return f"{role} ({label})" if label else role

    def fingerprint(self) -> str:
        """Stable hash of the ontology's content (``x-`` annotations excluded)."""
        text = json.dumps(strip_annotations(self.raw), sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]

    @property
    def mapped(self) -> bool:
        """True if any type or link reads through to an existing table (needs ``open_store``)."""
        return any(ot.table for ot in self.object_types.values()) or any(
            lt.column or lt.table for lt in self.link_types.values())

    @property
    def stamp(self) -> str:
        return f"{self.name}@{self.version}"

    # ------------------------------------------------------------ validation
    def check(self) -> list[str]:
        errors: list[str] = []
        for ot in self.object_types.values():
            errors.extend(self._check_object_type(ot))
        for lt in self.link_types.values():
            errors.extend(self._check_link_type(lt))
        if errors:  # expressions below walk types and links, which must be sound first
            return errors
        for ot in self.object_types.values():
            if ot.visible_when is not None:
                errors.extend(self._check_expr(ot.visible_when, None, f"object type {ot.name} visible_when",
                                               {"obj": ot.name}))
        for action in self.action_types.values():
            errors.extend(self._check_action(action))
        return errors

    def _check_roles(self, roles: list[str] | None, where: str) -> list[str]:
        return [f"{where}: unknown role {r!r}" for r in roles or [] if r not in self.roles]

    def _check_object_type(self, ot: ObjectType) -> list[str]:
        errors: list[str] = []
        where = f"object type {ot.name}"
        if not _IDENT.fullmatch(ot.name):
            errors.append(f"{where}: name must be an identifier")
        if ot.primary_key not in ot.properties:
            errors.append(f"{where}: primary_key {ot.primary_key!r} is not a property")
        elif ot.properties[ot.primary_key].readable_by:
            errors.append(f"{where}: the primary key cannot be restricted with readable_by")
        errors.extend(self._check_roles(ot.readable_by, f"{where} readable_by"))
        for prop in ot.properties.values():
            label = f"{ot.name}.{prop.name}"
            if not _IDENT.fullmatch(prop.name):
                errors.append(f"{label}: property names must be identifiers")
            if prop.type not in PRIMITIVE_TYPES:
                errors.append(f"{label}: unknown type {prop.type!r}")
                continue
            errors.extend(_check_values(prop, label))
            errors.extend(self._check_roles(prop.readable_by, f"{label} readable_by"))
        if ot.table is not None and not _SQL_NAME.fullmatch(ot.table):
            errors.append(f"{where}: source table {ot.table!r} is not a valid table name")
        for prop, column in ot.columns.items():
            if ot.table is None:
                errors.append(f"{where}: source columns need a source table")
            if prop not in ot.properties:
                errors.append(f"{where}: source column mapping for unknown property {prop!r}")
            if not _IDENT.fullmatch(str(column)):
                errors.append(f"{where}: source column {column!r} is not a valid column name")
        return errors

    def _check_link_type(self, lt: LinkType) -> list[str]:
        errors: list[str] = []
        where = f"link type {lt.name}"
        if not _IDENT.fullmatch(lt.name):
            errors.append(f"{where}: name must be an identifier")
        for end in (lt.from_type, lt.to_type):
            if end not in self.object_types:
                errors.append(f"{where}: unknown object type {end!r}")
        if lt.cardinality not in CARDINALITIES:
            errors.append(f"{where}: cardinality must be one of {sorted(CARDINALITIES)}")
        src = self.object_types.get(lt.from_type)
        if src and (lt.name in src.properties or lt.name == "id"):
            errors.append(f"{where}: clashes with property {lt.from_type}.{lt.name}")
        if lt.column is not None:
            if lt.table or lt.from_column or lt.to_column:
                errors.append(f"{where}: source takes either 'column' or 'table' + 'from_column' + 'to_column'")
            if not lt.single:
                errors.append(f"{where}: a foreign-key column can only hold a to-one link")
            if src and src.table is None:
                errors.append(f"{where}: source column needs {lt.from_type} to have a source table")
        elif lt.table or lt.from_column or lt.to_column:
            if not (lt.table and lt.from_column and lt.to_column):
                errors.append(f"{where}: a join-table source needs 'table', 'from_column' and 'to_column'")
        for name, pattern in ((lt.column, _IDENT), (lt.from_column, _IDENT), (lt.to_column, _IDENT), (lt.table, _SQL_NAME)):
            if name is not None and not pattern.fullmatch(str(name)):
                errors.append(f"{where}: source name {name!r} is not valid")
        return errors

    def _check_action(self, action: ActionType) -> list[str]:
        errors: list[str] = []
        where = f"action {action.name}"
        if not _ACTION_NAME.fullmatch(action.name) or action.name.startswith("onto_"):
            errors.append(f"{where}: name must match [A-Za-z_][A-Za-z0-9_-]* and not start with 'onto_'")
        errors.extend(self._check_roles(action.allowed_roles, f"{where} allowed_roles"))
        for param in action.parameters.values():
            label = f"{where}: parameter {param.name}"
            if not _IDENT.fullmatch(param.name):
                errors.append(f"{label}: parameter names must be identifiers")
            if param.type == "object_ref":
                if param.object_type not in self.object_types:
                    errors.append(f"{label} references unknown object type {param.object_type!r}")
            elif param.type not in PRIMITIVE_TYPES:
                errors.append(f"{label} has unknown type {param.type!r}")
            else:
                errors.extend(_check_values(param, label))
        if errors:
            return errors
        seen: set[str] = set()
        for rule in action.rules:
            if rule.id in seen:
                errors.append(f"{where}: duplicate rule id {rule.id!r}")
            seen.add(rule.id)
            if rule.outcome == "require_approval" and rule.role not in self.roles:
                errors.append(f"{where}: rule {rule.id} requires approval from unknown role {rule.role!r}")
            errors.extend(self._check_expr(rule.when, action, f"{where} rule {rule.id}"))
        aliases: dict[str, str] = {}
        for i, effect in enumerate(action.effects):
            label = f"{where} effect #{i + 1} ({effect.kind})"
            if effect.kind == "create":
                ot = self.object_types.get(effect.target)
                if ot is None:
                    errors.append(f"{label}: unknown object type {effect.target!r}")
                    continue
                errors.extend(self._check_set(effect, ot, action, label, aliases))
                for prop in ot.properties.values():
                    if prop.required and prop.default is None and prop.name != ot.primary_key \
                            and prop.name not in effect.values:
                        errors.append(f"{label}: does not set required property {ot.name}.{prop.name}")
                if effect.alias:
                    if not _IDENT.fullmatch(effect.alias) or effect.alias in RESERVED_NAMES or effect.alias in aliases:
                        errors.append(f"{label}: alias {effect.alias!r} is reserved, repeated or not an identifier")
                    aliases[effect.alias] = ot.name
            elif effect.kind == "update":
                errors.extend(self._check_expr(effect.target, action, label, aliases))
                target_type = self._expr_type(effect.target, action, aliases)
                if target_type is None:
                    errors.append(f"{label}: update target {effect.target!r} must resolve to an object")
                    continue
                ot = self.object_types[target_type]
                if ot.primary_key in effect.values:
                    errors.append(f"{label}: the primary key {ot.name}.{ot.primary_key} cannot be updated")
                errors.extend(self._check_set(effect, ot, action, label, aliases))
            elif effect.kind == "webhook":
                if effect.target not in self.webhooks:
                    errors.append(f"{label}: unknown webhook {effect.target!r}")
                for value in effect.values.values():
                    if isinstance(value, str):
                        errors.extend(self._check_expr(value, action, label, aliases))
            else:
                lt = self.link_types.get(effect.target)
                if lt is None:
                    errors.append(f"{label}: unknown link type {effect.target!r}")
                    continue
                for end_expr, expected in ((effect.from_expr, lt.from_type), (effect.to_expr, lt.to_type)):
                    if not end_expr:
                        errors.append(f"{label}: needs both 'from' and 'to'")
                        continue
                    errors.extend(self._check_expr(end_expr, action, label, aliases))
                    got = self._expr_type(end_expr, action, aliases)
                    if got is not None and got != expected:
                        errors.append(f"{label}: {end_expr!r} is a {got}, expected {expected}")
        return errors

    def _check_set(self, effect, ot, action, label, aliases) -> list[str]:
        errors = []
        for key, value in effect.values.items():
            if key not in ot.properties:
                errors.append(f"{label}: {ot.name} has no property {key!r}")
            if isinstance(value, str):
                errors.extend(self._check_expr(value, action, label, aliases))
        return errors

    def _check_expr(self, source, action, label, aliases=None) -> list[str]:
        try:
            paths = expr.references(source)
        except expr.ExprError as exc:
            return [f"{label}: {exc}"]
        errors = []
        for path in sorted(paths):
            problem = self._check_path(path, action, aliases or {})
            if problem:
                errors.append(f"{label}: {'.'.join(path)}: {problem}")
        return errors

    def _check_path(self, path, action: ActionType | None, aliases: dict[str, str]) -> str | None:
        root = path[0]
        if root in expr.FUNCTIONS:
            return None
        if root == "actor":
            if len(path) == 1 or (len(path) == 2 and path[1] in ACTOR_FIELDS):
                return None
            return f"actor only has {sorted(ACTOR_FIELDS)}"
        if root in aliases:
            return self._walk_object(aliases[root], path[1:])
        if root == "params" and action is not None:
            if len(path) == 1:
                return None
            param = action.parameters.get(path[1])
            if param is None:
                return f"unknown parameter {path[1]!r}"
            if param.type != "object_ref":
                return None if len(path) == 2 else f"parameter {path[1]!r} is a {param.type}, not an object"
            return self._walk_object(param.object_type, path[2:])
        allowed = ["actor.*", *(["params.*"] if action is not None else []), *sorted(aliases)]
        return f"unknown name {root!r} (available here: {', '.join(allowed)})"

    def _walk_object(self, type_name, rest) -> str | None:
        current = type_name
        for i, member in enumerate(rest):
            ot = self.object_types[current]
            if member == "id" or member in ot.properties:
                return None if i == len(rest) - 1 else f"{member!r} is a value, not an object"
            link = self.links_from(current).get(member)
            if link is None:
                return f"{current} has no property or link {member!r}"
            if not link.single and i != len(rest) - 1:
                return f"link {member!r} is to-many; only len() or membership tests are supported"
            current = link.to_type
        return None

    def _expr_type(self, source, action, aliases) -> str | None:
        """Object type an expression like ``params.po`` or ``po`` resolves to."""
        try:
            node = expr.parse(source).body
        except expr.ExprError:
            return None
        parts: list[str] = []
        while isinstance(node, expr.ast.Attribute):
            parts.append(node.attr)
            node = node.value
        if not isinstance(node, expr.ast.Name):
            return None
        parts.append(node.id)
        parts.reverse()
        if parts[0] in aliases:
            current, rest = aliases[parts[0]], parts[1:]
        elif parts[0] == "params" and len(parts) >= 2:
            param = action.parameters.get(parts[1])
            if param is None or param.type != "object_ref":
                return None
            current, rest = param.object_type, parts[2:]
        else:
            return None
        for member in rest:
            link = self.links_from(current).get(member) if current else None
            if link is None or not link.single:
                return None
            current = link.to_type
        return current


# ---------------------------------------------------------------- parsing


def strip_annotations(value: Any) -> Any:
    """Drop ``x-`` annotation keys recursively. Header and payload maps are data: their keys all stay."""
    if isinstance(value, dict):
        return {str(k): (v if k in ("headers", "payload") else strip_annotations(v))
                for k, v in value.items() if not str(k).startswith("x-")}
    if isinstance(value, list):
        return [strip_annotations(v) for v in value]
    return value


def _mapping(value: Any, where: str, errors: list[str], allowed: set[str] | None = None,
             data: bool = False) -> dict[str, Any]:
    """``value`` as a ``{str: ...}`` mapping; reports wrong shapes and unknown keys.

    ``x-`` keys are annotations and are dropped, except in ``data`` maps (HTTP headers, webhook
    payloads), where ``x-api-key`` is a perfectly good key.
    """
    if value is None:
        return {}
    if not isinstance(value, dict):
        errors.append(f"{where}: expected a mapping, got {type(value).__name__}")
        return {}
    out = {str(k): v for k, v in value.items() if data or not str(k).startswith("x-")}
    if allowed is not None:
        for key in out:
            if key not in allowed:
                errors.append(f"{where}: unknown key {key!r} (allowed: {', '.join(sorted(allowed))})")
    return out


def _roles(value: Any, where: str, errors: list[str]) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(r, str) for r in value):
        errors.append(f"{where}: expected a list of role names")
        return None
    return list(value)


def _enum(value: Any, where: str, errors: list[str]) -> list[Any] | None:
    if value is None:
        return None
    if not isinstance(value, list) or not value:
        errors.append(f"{where}: enum must be a non-empty list")
        return None
    return list(value)


def _text(value: Any, where: str, errors: list[str]) -> str | None:
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, bool):  # YAML turns a bare `true` into a boolean; as an expression it is fine
        return str(value)
    errors.append(f"{where}: expected text, got {type(value).__name__}")
    return None


def _check_values(spec: PropertySpec | ParamSpec, label: str) -> list[str]:
    """``enum`` members and ``default`` must fit the declared type."""
    errors = []
    for value in spec.enum or []:
        try:
            if coerce(spec.type, value) != value:
                raise ValueError
        except (TypeError, ValueError):
            errors.append(f"{label}: enum value {value!r} is not a {spec.type}")
    if spec.default is not None:
        try:
            value = coerce(spec.type, spec.default)
            if spec.enum and value not in spec.enum:
                errors.append(f"{label}: default {spec.default!r} is not in enum {spec.enum}")
        except (TypeError, ValueError):
            errors.append(f"{label}: default {spec.default!r} is not a {spec.type}")
    return errors


def _parse_roles(raw: Any, errors: list[str]) -> dict[str, dict[str, Any]]:
    if raw is None:
        return {}
    if isinstance(raw, list):
        out = {}
        for item in raw:
            if isinstance(item, str):
                out[item] = {}
            elif isinstance(item, dict) and "id" in item:
                out[str(item["id"])] = {k: v for k, v in item.items() if k != "id"}
            else:
                errors.append(f"bad role entry {item!r}")
        return out
    return {name: _mapping(spec, f"role {name}", errors, _ROLE_KEYS)
            for name, spec in _mapping(raw, "roles", errors).items()}


def _parse_object_type(name: str, raw: Any, errors: list[str]) -> ObjectType:
    where = f"object type {name}"
    spec = _mapping(raw, where, errors, _OBJECT_KEYS)
    props = {}
    for pname, praw in _mapping(spec.get("properties"), f"{where} properties", errors).items():
        label = f"{name}.{pname}"
        pspec = _mapping({"type": praw} if isinstance(praw, str) else praw, label, errors, _PROPERTY_KEYS)
        props[pname] = PropertySpec(
            name=pname,
            type=str(pspec.get("type", "string")),
            required=bool(pspec.get("required", False)),
            enum=_enum(pspec.get("enum"), label, errors),
            default=pspec.get("default"),
            label=pspec.get("label"),
            description=pspec.get("description"),
            readable_by=_roles(pspec.get("readable_by"), f"{label} readable_by", errors),
            renamed_from=pspec.get("renamed_from"),
        )
    source = _mapping(spec.get("source"), f"{where} source", errors, {"table", "columns"})
    if "source" in spec and not source.get("table"):
        errors.append(f"{where}: source needs a 'table'")
    return ObjectType(
        name=name,
        primary_key=str(spec.get("primary_key") or next(iter(props), "id")),
        properties=props,
        label=spec.get("label"),
        description=spec.get("description"),
        id_prefix=spec.get("id_prefix"),
        readable_by=_roles(spec.get("readable_by"), f"{where} readable_by", errors),
        visible_when=_text(spec.get("visible_when"), f"{where} visible_when", errors),
        table=source.get("table"),
        columns=_mapping(source.get("columns"), f"{where} source columns", errors),
        renamed_from=spec.get("renamed_from"),
    )


def _parse_link_type(name: str, raw: Any, errors: list[str]) -> LinkType:
    where = f"link type {name}"
    spec = _mapping(raw, where, errors, _LINK_KEYS)
    if "from" not in spec or "to" not in spec:
        errors.append(f"{where}: needs 'from' and 'to'")
    source = _mapping(spec.get("source"), f"{where} source", errors, {"column", "table", "from_column", "to_column"})
    if "source" in spec and not source:
        errors.append(f"{where}: source needs 'column' or 'table' + 'from_column' + 'to_column'")
    return LinkType(
        name=name,
        from_type=str(spec.get("from", "")),
        to_type=str(spec.get("to", "")),
        cardinality=str(spec.get("cardinality", "many_to_one")),
        label=spec.get("label"),
        description=spec.get("description"),
        column=source.get("column"),
        table=source.get("table"),
        from_column=source.get("from_column"),
        to_column=source.get("to_column"),
        renamed_from=spec.get("renamed_from"),
    )


def _parse_webhook(name: str, raw: Any, errors: list[str]) -> dict[str, Any]:
    where = f"webhook {name}"
    spec = _mapping(raw, where, errors, _WEBHOOK_KEYS)
    url = spec.get("url")
    if not isinstance(url, str) or not (url.startswith(("http://", "https://")) or url.startswith("${")):
        errors.append(f"{where}: needs an http(s) 'url' (or a ${{ENV_VAR}} that holds one)")
    spec["headers"] = _mapping(spec.get("headers"), f"{where} headers", errors, data=True)
    for key, low in (("max_attempts", 1), ("timeout", 0)):
        value = spec.get(key)
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or value < low):
            errors.append(f"{where}: {key} must be a number >= {low}")
    return spec


def _parse_action(name: str, raw: Any, errors: list[str]) -> ActionType:
    where = f"action {name}"
    spec = _mapping(raw, where, errors, _ACTION_KEYS)
    params = {}
    for pname, praw in _mapping(spec.get("parameters"), f"{where} parameters", errors).items():
        label = f"{where}: parameter {pname}"
        pspec = _mapping({"type": praw} if isinstance(praw, str) else praw, label, errors, _PARAM_KEYS)
        ptype = str(pspec.get("type", "string"))
        object_type = pspec.get("object_type")
        if ptype not in PRIMITIVE_TYPES and ptype != "object_ref" and object_type is None:
            object_type, ptype = ptype, "object_ref"  # shorthand: `supplier: Supplier`
        params[pname] = ParamSpec(
            name=pname,
            type=ptype,
            required=bool(pspec.get("required", "default" not in pspec)),
            object_type=object_type,
            enum=_enum(pspec.get("enum"), label, errors),
            default=pspec.get("default"),
            description=pspec.get("description") or pspec.get("label"),
        )
    rules = []
    for i, rraw in enumerate(_sequence(spec.get("rules"), f"{where} rules", errors)):
        rid = str(rraw.get("id") or f"{name}_rule_{i + 1}") if isinstance(rraw, dict) else f"{name}_rule_{i + 1}"
        label = f"{where} rule {rid}"
        rspec = _mapping(rraw, label, errors, _RULE_KEYS)
        then = rspec.get("then", "deny")
        role = None
        if isinstance(then, dict) and set(then) == {"require_approval"} and isinstance(then["require_approval"], str):
            outcome, role = "require_approval", then["require_approval"]
        elif then in ("deny", "warn"):
            outcome = then
        else:
            outcome = "deny"
            errors.append(f"{label}: 'then' must be deny, warn or {{require_approval: <role>}}")
        if "when" not in rspec:
            errors.append(f"{label}: missing 'when'")
        src = rspec.get("source")
        rules.append(
            Rule(
                id=rid,
                when=_text(rspec.get("when", "False"), f"{label} when", errors) or "False",
                outcome=outcome,
                role=role,
                message=_text(rspec.get("message"), f"{label} message", errors),
                source=_mapping({"doc": src} if isinstance(src, str) else src, f"{label} source", errors) or None,
            )
        )
    effects = []
    for i, eraw in enumerate(_sequence(spec.get("effects"), f"{where} effects", errors)):
        label = f"{where} effect #{i + 1}"
        espec = _mapping(eraw, label, errors, _EFFECT_KEYS)
        kinds = [k for k in EFFECT_KINDS if k in espec]
        if len(kinds) != 1:
            errors.append(f"{label}: must have exactly one of {', '.join(EFFECT_KINDS)}")
            continue
        kind = kinds[0]
        values = espec.get("payload") if kind == "webhook" else espec.get("set")
        effects.append(
            Effect(
                kind=kind,
                target=str(espec[kind]),
                alias=espec.get("as"),
                values=_mapping(values, f"{label} {'payload' if kind == 'webhook' else 'set'}", errors,
                                data=kind == "webhook"),
                from_expr=_text(espec.get("from"), f"{label} from", errors),
                to_expr=_text(espec.get("to"), f"{label} to", errors),
                event=espec.get("event"),
            )
        )
    return ActionType(
        name=name,
        parameters=params,
        rules=rules,
        effects=effects,
        label=spec.get("label"),
        description=spec.get("description"),
        allowed_roles=_roles(spec.get("allowed_roles"), f"{where} allowed_roles", errors),
    )


def _sequence(value: Any, where: str, errors: list[str]) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        errors.append(f"{where}: expected a list, got {type(value).__name__}")
        return []
    return value
