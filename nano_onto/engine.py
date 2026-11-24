"""The action engine: the part of the ontology that *does* things.

``Engine.execute`` runs an action the way an agent should be allowed to run it:

1. check the actor's role may call the action at all;
2. validate and coerce parameters, resolving object references;
3. evaluate every rule deterministically (no LLM in the loop);
4. deny, hold for approval, or apply the effects;
5. write an audit entry either way.

Steps 2-5 run in one serialized transaction, so the state the rules saw is the
state the effects change, and an executed action always has its audit entry.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from . import expr
from .schema import ActionType, ObjectType, Ontology, ParamSpec, PropertySpec, coerce
from .store import Store, StoreError

MASK = "***"  # shown instead of a property the actor's role may not read


class ActionError(Exception):
    """Raised for misuse that is not an ordinary rule outcome (e.g. bad approver)."""


class VersionError(ActionError):
    """The database was last used with a different ontology; run ``nano-onto migrate``."""


@dataclass(frozen=True)
class Actor:
    id: str
    role: str

    def as_env(self) -> dict[str, str]:
        return {"id": self.id, "role": self.role}


@dataclass
class ActionResult:
    status: str  # executed | pending_approval | denied | invalid | rejected
    action: str
    messages: list[str] = field(default_factory=list)
    trace: list[dict[str, Any]] = field(default_factory=list)
    request_id: str | None = None
    required_approvals: list[str] = field(default_factory=list)
    created: dict[str, str] = field(default_factory=dict)
    updated: list[str] = field(default_factory=list)
    webhooks: list[str] = field(default_factory=list)
    dry_run: bool = False

    @property
    def ok(self) -> bool:
        return self.status == "executed"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ObjectView:
    """Read-only view of a stored object, used inside expressions.

    Members resolve to properties first, then to outgoing links, so rules can
    say ``params.po.supplier.rating`` and follow the graph.
    """

    def __init__(self, engine: "Engine", type_name: str, pk: str, props: dict[str, Any]):
        self._engine = engine
        self.type_name = type_name
        self.pk = pk
        self.props = props

    def onto_get(self, name: str) -> Any:
        if name in self.props:
            return self.props[name]
        if name == "id":
            return self.pk
        link = self._engine.onto.links_from(self.type_name).get(name)
        if link is None:
            return None
        targets = self._engine.store.links_from(link.name, self.pk)
        views = [v for v in (self._engine.view(link.to_type, t) for t in targets) if v is not None]
        if link.single:
            return views[0] if views else None
        return views

    def __len__(self) -> int:  # so len(view) never explodes
        return len(self.props)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, ObjectView):
            return (self.type_name, self.pk) == (other.type_name, other.pk)
        return self.pk == other

    def __hash__(self) -> int:
        return hash((self.type_name, self.pk))

    def __repr__(self) -> str:
        return f"<{self.type_name} {self.pk}>"


class Engine:
    def __init__(self, onto: Ontology, store: Store | None = None, check_version: bool = True):
        self.onto = onto
        self.store = store or Store()
        if onto.mapped and not hasattr(self.store, "onto"):
            raise ActionError("this ontology maps types onto existing tables: open the store with open_store(path, onto)")
        self._fingerprint = onto.fingerprint()
        if self.store.get_meta("ontology") is None:
            self.stamp("initialised")
        elif check_version:
            self.check_version()

    def check_version(self) -> None:
        """Refuse to work if the database follows a different ontology than this engine holds.

        Checked on every action and read, so a long-running server stops enforcing an old
        ontology the moment another process migrates the database.
        """
        stamp = self.store.get_meta("ontology")
        if stamp is not None and stamp["fingerprint"] != self._fingerprint:
            raise VersionError(
                f"this database follows ontology {stamp['name']}@{stamp['version']}, but this process holds "
                f"{self.onto.stamp} with different content. If the ontology file changed, review and apply it "
                "with `nano-onto migrate`; if the database was migrated, restart this process."
            )

    def stamp(self, event: str, **detail: Any) -> None:
        """Record that the database now follows this ontology (see ``nano_onto.migrate``)."""
        with self.store.transaction():
            self.store.set_meta("ontology", {"name": self.onto.name, "version": self.onto.version,
                                             "fingerprint": self._fingerprint, "schema": self.onto.raw})
            self.store.add_history({"event": event, "ontology": self.onto.stamp,
                                    "fingerprint": self._fingerprint, **detail})

    # ------------------------------------------------------------- loading
    def load_seed(self, seed: dict[str, Any]) -> dict[str, int]:
        """Load ``{"objects": {Type: [props...]}, "links": [{type, from, to}]}``."""
        counts: dict[str, int] = {}
        with self.store.transaction():
            for type_name, rows in (seed.get("objects") or {}).items():
                ot = self._object_type(type_name)
                for row in rows:
                    props = self._coerce_props(ot, row, partial=False)
                    self.store.put(type_name, props[ot.primary_key], props)
                    counts[type_name] = counts.get(type_name, 0) + 1
            for link in seed.get("links") or []:
                lt = self.onto.link_types.get(link["type"])
                if lt is None:
                    raise ActionError(f"unknown link type {link['type']!r}")
                self._link(lt, str(link["from"]), str(link["to"]))
                counts["links"] = counts.get("links", 0) + 1
        return counts

    def view(self, type_name: str, pk: str) -> ObjectView | None:
        props = self.store.get(type_name, pk)
        if props is None:
            return None
        # the stored key, not the caller's spelling: "01" and "1" may name the same row of an integer-keyed table
        key = props.get(self.onto.object_types[type_name].primary_key)
        return ObjectView(self, type_name, str(pk if key is None else key), props)

    # ------------------------------------------------------------- reading
    def query(self, type_name: str, where: dict[str, Any] | None = None, limit: int = 100,
              actor: Actor | None = None) -> list[dict[str, Any]]:
        """Objects of a type as ``actor`` may see them (``actor=None``: trusted caller, sees everything).

        Rows hidden by ``visible_when`` are left out; properties hidden by
        ``readable_by`` come back as ``"***"`` and cannot be used as filters.
        """
        ot = self._object_type(type_name)
        self._check_read(ot, actor)
        filters = {}
        for key, value in (where or {}).items():
            spec = ot.properties.get(key)
            if spec is None:
                raise ActionError(f"{ot.name} has no property {key!r}")
            if not self._readable(spec, actor):
                raise ActionError(f"role {actor.role!r} may not filter on {ot.name}.{key}")  # type: ignore[union-attr]
            filters[key] = None if value is None else _coerce(spec.type, value, f"filter {key}")
        if actor is None or not ot.visible_when:
            rows = self.store.query(ot.name, filters or None, limit)
        else:
            rows = [r for r in self.store.query(ot.name, filters or None, None) if self._visible(ot, r, actor)]
        return [self._mask(ot, r, actor) for r in rows[:limit]]

    def get(self, type_name: str, pk: str, actor: Actor | None = None) -> dict[str, Any] | None:
        """One object with its outgoing links, or ``None`` if it is missing *or* hidden from ``actor``."""
        ot = self._object_type(type_name)
        self._check_read(ot, actor)
        props = self.store.get(ot.name, pk)
        if props is None or not self._visible(ot, props, actor):
            return None
        pk = str(props.get(ot.primary_key, pk))
        links = {}
        for name, lt in self.onto.links_from(ot.name).items():
            target = self.onto.object_types[lt.to_type]
            links[name] = [t for t in self.store.links_from(name, pk) if self._can_see(target, t, actor)]
        return {"type": ot.name, "id": str(pk), "properties": self._mask(ot, props, actor), "links": links}

    def can_read(self, ot: ObjectType, actor: Actor | None) -> bool:
        return actor is None or not ot.readable_by or actor.role in ot.readable_by

    def _check_read(self, ot: ObjectType, actor: Actor | None) -> None:
        self.check_version()
        self._check_role(actor)
        if not self.can_read(ot, actor):
            raise ActionError(f"role {actor.role!r} may not read {ot.name}")  # type: ignore[union-attr]

    def _check_role(self, actor: Actor | None) -> None:
        if actor is not None and self.onto.roles and actor.role not in self.onto.roles:
            raise ActionError(f"unknown role {actor.role!r}; this ontology defines: {', '.join(self.onto.roles)}")

    @staticmethod
    def _readable(spec: PropertySpec, actor: Actor | None) -> bool:
        return actor is None or not spec.readable_by or actor.role in spec.readable_by

    def _visible(self, ot: ObjectType, props: dict[str, Any], actor: Actor | None) -> bool:
        if actor is None or not ot.visible_when:
            return True
        view = ObjectView(self, ot.name, str(props[ot.primary_key]), props)
        try:
            return expr.truthy(ot.visible_when, {"obj": view, "actor": actor.as_env()})
        except expr.ExprError:
            return False  # fail closed

    def _can_see(self, ot: ObjectType, pk: str, actor: Actor | None) -> bool:
        if actor is None or not (ot.readable_by or ot.visible_when):
            return True
        props = self.store.get(ot.name, pk)
        return self.can_read(ot, actor) and props is not None and self._visible(ot, props, actor)

    def _mask(self, ot: ObjectType, props: dict[str, Any], actor: Actor | None) -> dict[str, Any]:
        if actor is None:
            return props
        return {k: (v if k not in ot.properties or self._readable(ot.properties[k], actor) else MASK)
                for k, v in props.items()}

    # ------------------------------------------------------------- actions
    def explain(self, action_name: str, params: dict[str, Any], actor: Actor) -> ActionResult:
        """Evaluate an action without changing anything."""
        return self.execute(action_name, params, actor, dry_run=True)

    def execute(
        self, action_name: str, params: dict[str, Any], actor: Actor, dry_run: bool = False
    ) -> ActionResult:
        action = self.onto.action_types.get(action_name)
        result = ActionResult(status="invalid", action=action_name, dry_run=dry_run)
        event = "explain" if dry_run else "execute"
        logged: dict[str, Any] = dict(params) if isinstance(params, dict) else {}
        self._standalone()
        try:
            with self.store.transaction():
                self.check_version()
                if action is None:
                    raise ActionError(f"unknown action {action_name!r}")
                if not isinstance(params, dict):
                    raise ActionError("parameters must be an object")
                if self.onto.roles and actor.role not in self.onto.roles:
                    result.status = "denied"
                    result.messages.append(f"unknown role {actor.role!r}; this ontology defines: {', '.join(self.onto.roles)}")
                elif action.allowed_roles is not None and actor.role not in action.allowed_roles:
                    result.status = "denied"
                    result.messages.append(
                        f"role {actor.role!r} may not run {action_name}; allowed: {', '.join(action.allowed_roles)}"
                    )
                else:
                    resolved, logged = self._resolve_params(action, params, actor)
                    self._decide(action, resolved, logged, actor, result)
                self._audit(result, actor, logged, event)
        except VersionError:
            raise
        except _ROLLED_BACK as exc:  # nothing was changed
            self._failed(result, exc)
            self._audit(result, actor, logged, event)
        return result

    def _standalone(self) -> None:
        """Actions own their transaction: inside a caller's, a failed action could not be rolled back."""
        if self.store.in_transaction:
            raise ActionError("do not call the engine inside store.transaction(): an action is its own transaction")

    def _decide(self, action: ActionType, resolved: dict[str, Any], raw: dict[str, Any], requester: Actor,
                result: ActionResult, request: dict[str, Any] | None = None) -> None:
        """Evaluate the rules, then deny, hold for approval or apply the effects."""
        satisfied = set(request["approved_by"]) if request else set()
        denies, approvals = self._evaluate_rules(action, resolved, requester, result, satisfied)
        if denies:
            result.status = "denied"
            if request:
                self.store.update_approval(request["id"], status="denied")
        elif approvals:
            result.status = "pending_approval"
            result.required_approvals = sorted(approvals)
            if request:  # data changed since the request; new approvals now apply
                self.store.update_approval(
                    request["id"], required_roles=sorted(set(request["required_roles"]) | approvals)
                )
            elif not result.dry_run:
                result.request_id = self.store.create_approval(
                    action.name, raw, requester.as_env(), sorted(approvals), result.messages
                )
        elif result.dry_run:
            result.status = "executed"
            result.messages.append("dry run: all rules pass, effects were not applied")
        else:
            self._apply(action, resolved, requester, result)
            result.status = "executed"
            if request:
                self.store.update_approval(request["id"], status="executed")

    @staticmethod
    def _failed(result: ActionResult, exc: Exception) -> None:
        text = str(exc.args[0]) if isinstance(exc, KeyError) and exc.args else str(exc)
        partial = result.created or result.updated or result.webhooks
        result.status, result.created, result.updated, result.webhooks = "invalid", {}, [], []
        result.messages.append(f"effects failed and were rolled back: {text}" if partial else text)

    # ------------------------------------------------------------ approvals
    def pending(self, role: str | None = None) -> list[dict[str, Any]]:
        items = self.store.list_approvals("pending")
        if role:
            items = [a for a in items if role in self._remaining(a)]
        return items

    def request(self, request_id: str, actor: Actor | None = None) -> dict[str, Any] | None:
        """An approval request, visible to its requester and to roles that decide (or decided) it."""
        req = self.store.get_approval(request_id)
        if req is None or actor is None:
            return req
        involved = actor.id == req["actor"]["id"] or actor.role in req["required_roles"]
        return req if involved else None

    def approve(self, request_id: str, approver: Actor, note: str | None = None) -> ActionResult:
        result = ActionResult(status="invalid", action="", request_id=request_id)
        approved: dict[str, str] = {}
        executing = False  # True once every approval is in and the action itself is being run
        self._standalone()
        try:
            with self.store.transaction():
                self.check_version()
                req = self._open_request(request_id, approver)
                result.action = req["action"]
                approved = {**req["approved_by"], approver.role: approver.id}
                remaining = [r for r in req["required_roles"] if r not in approved]
                self.store.update_approval(request_id, approved_by=approved)
                self.store.log(approver.as_env(), "approve", req["action"], "recorded",
                               {"request_id": request_id, "note": note, "remaining": remaining})
                if remaining:
                    result.status = "pending_approval"
                    result.required_approvals = remaining
                    result.messages.append(f"approval recorded; still waiting for: {', '.join(remaining)}")
                    return result
                executing = True
                requester = Actor(**req["actor"])
                action = self.onto.action_types.get(req["action"])
                if action is None:
                    raise ActionError(f"action {req['action']!r} no longer exists")
                if (self.onto.roles and requester.role not in self.onto.roles) or (
                        action.allowed_roles is not None and requester.role not in action.allowed_roles):
                    raise ActionError(f"role {requester.role!r} may no longer run {action.name}")
                resolved, raw = self._resolve_params(action, req["params"], requester)
                self._decide(action, resolved, raw, requester, result, {**req, "approved_by": approved})
                self._audit(result, approver, raw, "approve")
        except _Refused as exc:
            self.store.log(approver.as_env(), "approve", exc.action, "refused",
                           {"request_id": request_id, "reason": str(exc)})
            raise ActionError(str(exc)) from None
        except VersionError:
            raise
        except _ROLLED_BACK as exc:
            if not executing or isinstance(exc, StoreError):
                # nothing about the request was decided (e.g. the database was busy): leave it as it was
                raise ActionError(f"could not process {request_id}, nothing was changed; try again ({exc})") from None
            # every approval was given but the action itself can no longer run: close the request as failed
            self._failed(result, exc)
            result.messages[-1] = f"could not execute approved request: {result.messages[-1]}"
            with self.store.transaction():
                self.store.update_approval(request_id, status="failed", approved_by=approved)
                self._audit(result, approver, {}, "approve")
        return result

    def reject(self, request_id: str, approver: Actor, reason: str = "") -> ActionResult:
        self._standalone()
        try:
            with self.store.transaction():
                self.check_version()
                req = self._open_request(request_id, approver)
                self.store.update_approval(request_id, status="rejected")
                result = ActionResult(status="rejected", action=req["action"], request_id=request_id,
                                      messages=[reason or "rejected"])
                self._audit(result, approver, req["params"], "reject")
        except _Refused as exc:
            self.store.log(approver.as_env(), "reject", exc.action, "refused",
                           {"request_id": request_id, "reason": str(exc)})
            raise ActionError(str(exc)) from None
        return result

    def _open_request(self, request_id: str, approver: Actor) -> dict[str, Any]:
        req = self.store.get_approval(request_id)
        involved = req is not None and (approver.role in req["required_roles"] or approver.id == req["actor"]["id"])
        if not involved:  # same answer whether it is missing or simply none of this actor's business
            raise _Refused(f"no approval request {request_id!r} for you to decide", req["action"] if req else None)
        if req["status"] != "pending":
            raise _Refused(f"request {request_id} is already {req['status']}", req["action"])
        if approver.role not in self._remaining(req):
            raise _Refused(
                f"role {approver.role!r} cannot decide {request_id}; waiting for: "
                f"{', '.join(self._remaining(req)) or 'nobody'}", req["action"]
            )
        if approver.id == req["actor"]["id"]:
            raise _Refused("requesters cannot approve their own requests (separation of duties)", req["action"])
        return req

    @staticmethod
    def _remaining(req: dict[str, Any]) -> list[str]:
        return [r for r in req["required_roles"] if r not in req["approved_by"]]

    # --------------------------------------------------------------- internals
    def _evaluate_rules(self, action, resolved, actor, result, satisfied=frozenset()):
        env = {"params": resolved, "actor": actor.as_env()}
        denies, approvals = [], set()
        for rule in action.rules:
            fired = expr.truthy(rule.when, env)
            entry = {"rule": rule.id, "when": rule.when, "fired": fired, "outcome": None}
            if fired:
                message = rule.message or f"rule {rule.id} fired"
                if rule.outcome == "deny":
                    entry["outcome"] = "deny"
                    denies.append(rule.id)
                    result.messages.append(f"denied: {message}")
                elif rule.outcome == "warn":
                    entry["outcome"] = "warn"
                    result.messages.append(f"warning: {message}")
                elif rule.role == actor.role or rule.role in satisfied:
                    entry["outcome"] = f"approval by {rule.role} satisfied"
                else:
                    entry["outcome"] = f"needs approval by {rule.role}"
                    approvals.add(rule.role)
                    result.messages.append(f"needs approval by {self.onto.role_label(rule.role)}: {message}")
                if rule.source:
                    entry["source"] = rule.source
            result.trace.append(entry)
        return denies, approvals

    def _resolve_params(self, action: ActionType, params: dict[str, Any], actor: Actor | None = None):
        """Coerce parameters and resolve object references -- only to objects ``actor`` may read."""
        unknown = set(params) - set(action.parameters)
        if unknown:
            raise ActionError(f"unknown parameter(s): {', '.join(sorted(map(str, unknown)))}")
        resolved: dict[str, Any] = {}
        raw: dict[str, Any] = {}
        for name, spec in action.parameters.items():
            value = params.get(name)
            if value is None or value == "":  # an explicit null means "not given", exactly like leaving it out
                value = spec.default
            if value is None or value == "":
                if spec.required:
                    raise ActionError(f"missing required parameter {name!r}")
                resolved[name] = raw[name] = None
                continue
            if spec.type == "object_ref":
                pk = value.pk if isinstance(value, ObjectView) else str(value)
                ot = self.onto.object_types[spec.object_type]  # type: ignore[index]
                view = self.view(ot.name, pk)
                # a hidden object answers exactly like a missing one, so actions cannot be used to probe for it
                if view is None or not (self.can_read(ot, actor) and self._visible(ot, view.props, actor)):
                    raise ActionError(f"{name}: {spec.object_type} {pk!r} not found")
                resolved[name], raw[name] = view, view.pk
            else:
                resolved[name] = raw[name] = _coerce_prop(spec, value, f"parameter {name}")
        return resolved, raw

    def _apply(self, action: ActionType, resolved, actor: Actor, result: ActionResult) -> None:
        env: dict[str, Any] = {"params": resolved, "actor": actor.as_env()}
        for effect in action.effects:
            if effect.kind == "create":
                ot = self._object_type(effect.target)
                values = {k: _value(v, env) for k, v in effect.values.items()}
                numeric = ot.properties[ot.primary_key].type in ("integer", "number")
                pk = values.get(ot.primary_key) or self.store.next_id(
                    ot.name, "" if numeric else ot.id_prefix or (ot.name[:3].upper() + "-")
                )
                values[ot.primary_key] = pk
                if self.store.exists(ot.name, pk):
                    raise ActionError(f"{ot.name} {pk!r} already exists")
                props = self._coerce_props(ot, values, partial=False)
                pk = props[ot.primary_key]  # in its declared type: 3, not the generated "0003"
                self.store.put(ot.name, pk, props)
                view = ObjectView(self, ot.name, str(pk), props)
                if effect.alias:
                    env[effect.alias] = view
                result.created[effect.alias or f"{ot.name}#{len(result.created) + 1}"] = str(pk)
            elif effect.kind == "update":
                target = expr.evaluate(effect.target, env)
                if not isinstance(target, ObjectView):
                    raise ActionError(f"update target {effect.target!r} is not an object")
                ot = self._object_type(target.type_name)
                changes = self._coerce_props(
                    ot, {k: _value(v, env) for k, v in effect.values.items()}, partial=True
                )
                target.props = self.store.update(ot.name, target.pk, changes)
                result.updated.append(f"{ot.name}:{target.pk}")
            elif effect.kind == "webhook":
                result.webhooks.append(self.store.enqueue(effect.target, {
                    "event": effect.event or action.name,
                    "action": action.name,
                    "actor": actor.as_env(),
                    "ontology": self.onto.stamp,
                    "payload": {k: _value(v, env) for k, v in effect.values.items()},
                }))
            else:
                lt = self.onto.link_types[effect.target]
                src = _pk(expr.evaluate(effect.from_expr, env))  # type: ignore[arg-type]
                dst = _pk(expr.evaluate(effect.to_expr, env))  # type: ignore[arg-type]
                if effect.kind == "link":
                    self._link(lt, src, dst)
                else:
                    self.store.remove_link(lt.name, src, dst)

    def _link(self, lt, src: str, dst: str) -> None:
        """Add a link between existing objects, replacing whatever the cardinality no longer allows."""
        ends = []
        for pk, type_name in ((src, lt.from_type), (dst, lt.to_type)):
            view = self.view(type_name, pk)
            if view is None:
                raise ActionError(f"link {lt.name}: {type_name} {pk!r} not found")
            ends.append(view.pk)
        src, dst = ends
        if lt.single:
            self.store.remove_link(lt.name, from_pk=src)
        if lt.unique_target:
            self.store.remove_link(lt.name, to_pk=dst)
        self.store.add_link(lt.name, src, dst)

    def _object_type(self, name: str) -> ObjectType:
        ot = self.onto.object_types.get(name)
        if ot is None:
            raise ActionError(f"unknown object type {name!r}")
        return ot

    def _coerce_props(self, ot: ObjectType, values: dict[str, Any], partial: bool) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in values.items():
            spec = ot.properties.get(key)
            if spec is None:
                raise ActionError(f"{ot.name} has no property {key!r}")
            out[key] = _coerce_prop(spec, value, f"{ot.name}.{key}")
        for key, spec in ot.properties.items():
            if partial and key not in out:
                continue
            if out.get(key) is None:
                if spec.default is not None and not partial:
                    out[key] = _coerce_prop(spec, spec.default, f"{ot.name}.{key}")
                elif spec.required:
                    raise ActionError(f"{ot.name}.{key} is required")
                else:
                    out.setdefault(key, None)
        return out

    def _audit(self, result: ActionResult, actor: Actor, params: dict[str, Any], event: str) -> None:
        # dry runs are audited too (event "explain"): they evaluate rules over data the actor may not see
        self.store.log(
            actor.as_env(), event, result.action, result.status,
            {"params": _jsonable(params), "messages": result.messages,
             "request_id": result.request_id, "created": result.created,
             "updated": result.updated, "webhooks": result.webhooks, "ontology": self.onto.stamp},
        )


class _Refused(ActionError):
    """An approver may not decide this request; the attempt is audited and nothing changes."""

    def __init__(self, message: str, action: str | None = None):
        super().__init__(message)
        self.action = action


_ROLLED_BACK = (ActionError, expr.ExprError, KeyError, ValueError, StoreError)


# ------------------------------------------------------------------ helpers


def _value(value: Any, env: dict[str, Any]) -> Any:
    """Effect values: strings are expressions, anything else is a literal."""
    if isinstance(value, str):
        out = expr.evaluate(value, env)
        return out.pk if isinstance(out, ObjectView) else out
    return value


def _pk(value: Any) -> str:
    if isinstance(value, ObjectView):
        return value.pk
    if value is None:
        raise ActionError("link endpoint is empty")
    return str(value)


def _jsonable(params: dict[str, Any]) -> dict[str, Any]:
    return {str(k): (v.pk if isinstance(v, ObjectView) else v) for k, v in (params or {}).items()}


def _coerce(type_name: str, value: Any, where: str) -> Any:
    try:
        return coerce(type_name, value)
    except (TypeError, ValueError):
        raise ActionError(f"{where}: {value!r} is not a valid {type_name}") from None


def _coerce_prop(spec: PropertySpec | ParamSpec, value: Any, where: str) -> Any:
    if value is None:
        return None
    coerced = _coerce(spec.type, value, where)
    if spec.enum and coerced not in spec.enum:
        raise ActionError(f"{where}: {coerced!r} is not one of {spec.enum}")
    return coerced
