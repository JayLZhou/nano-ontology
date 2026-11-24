"""Compile an ontology's actions into agent-facing surfaces.

* :func:`action_input_schema` -- JSON Schema for MCP / function calling
* :func:`action_description` -- tool description that tells the model the rules
* :func:`render_skill` -- a ``SKILL.md`` for Claude Skills / nanobot-style agents
"""

from __future__ import annotations

import json
import re
from typing import Any

from .schema import ActionType, Ontology

_JSON_TYPES = {
    "string": {"type": "string"},
    "number": {"type": "number"},
    "integer": {"type": "integer"},
    "boolean": {"type": "boolean"},
    "date": {"type": "string", "format": "date"},
}


def action_input_schema(action: ActionType, onto: Ontology) -> dict[str, Any]:
    props: dict[str, Any] = {}
    required = []
    for name, spec in action.parameters.items():
        if spec.type == "object_ref":
            ot = onto.object_types[spec.object_type]  # type: ignore[index]
            label = f" ({ot.label})" if ot.label else ""
            schema = {"type": "string", "description": f"ID ({ot.primary_key}) of a {ot.name}{label}"}
        else:
            schema = dict(_JSON_TYPES[spec.type])
        if spec.enum:
            schema["enum"] = list(spec.enum)
        if spec.description:
            schema["description"] = (schema.get("description", "") + " " + spec.description).strip()
        if spec.default is not None and spec.type != "object_ref":
            schema["default"] = spec.default
        props[name] = schema
        if spec.required:
            required.append(name)
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


def _rule_line(rule, onto: Ontology) -> str:
    if rule.outcome == "deny":
        verb = "DENIED"
    elif rule.outcome == "warn":
        verb = "warning"
    else:
        verb = f"needs approval by {onto.role_label(rule.role)}"
    text = f"- if `{rule.when}` -> {verb}"
    if rule.message:
        text += f": {rule.message}"
    return text


def action_description(action: ActionType, onto: Ontology) -> str:
    lines = []
    head = action.label or action.name
    lines.append(head + (f" -- {action.description}" if action.description else ""))
    if action.allowed_roles:
        lines.append("Allowed roles: " + ", ".join(onto.role_label(r) for r in action.allowed_roles))
    if action.rules:
        lines.append("Business rules (enforced by the server, not by you):")
        lines.extend(_rule_line(r, onto) for r in action.rules)
    lines.append(
        "Result status is one of executed / pending_approval / denied / invalid. "
        "Report it to the user truthfully; never claim success for anything else."
    )
    return "\n".join(lines)


def render_skill(onto: Ontology, schema_path: str = "ontology.yaml", db_path: str = "nano_onto.db") -> str:
    """Render a SKILL.md that teaches an agent to act through the ``nano-onto`` CLI."""
    if "://" in db_path and "@" in db_path:  # a database URL with credentials does not belong in a skill file
        db_path = "$NANO_ONTO_DB"
    name = (re.sub(r"[^a-z0-9]+", "-", onto.name.lower()).strip("-") or "business")[:55] + "-ontology"
    types = ", ".join(f"{ot.name}" + (f" ({ot.label})" if ot.label else "") for ot in onto.object_types.values())
    description = (
        f"Read and change {onto.name} business data ({types}) only through ontology actions "
        "that enforce permissions, business rules and approvals. Use whenever the user asks to look up "
        "or change these records."
    )
    if len(description) > 1000:
        description = description[:997] + "..."
    out = [
        "---",
        f"name: {name}",
        "description: " + json.dumps(description, ensure_ascii=False),  # a JSON string is a valid YAML scalar
        "---",
        "",
        f"# {onto.name} ontology (v{onto.version})",
        "",
        "Never edit the data directly. Every change goes through an action; the server checks the",
        "rules, may hold the action for approval, and writes an audit log.",
        "",
        "Common flags: `--schema " + schema_path + " --db " + db_path + " --actor <user id> --role <role>`.",
        "The actor and role must come from the user's real identity -- never pick a role to get past a rule.",
        "",
        "## Look things up",
        "",
        "```bash",
        "nano-onto query <Type> [--where key=value] --actor <user id> --role <role>",
        "nano-onto get <Type> <id> --actor <user id> --role <role>",
        "nano-onto explain <action> --param k=v ...   # dry run: which rules would fire",
        "nano-onto request <request id>               # has a held action happened yet?",
        "```",
        "",
        "A value shown as `***` is hidden from this role; say so instead of guessing it.",
        "",
        "## Object types",
        "",
    ]
    for ot in onto.object_types.values():
        props = ", ".join(
            p.name + (f"={p.label}" if p.label else "") for p in ot.properties.values()
        )
        out.append(f"- **{ot.name}**" + (f" ({ot.label})" if ot.label else "") + f": {props}")
    out += ["", "## Actions", ""]
    for action in onto.action_types.values():
        out.append(f"### `{action.name}`" + (f" -- {action.label}" if action.label else ""))
        out.append("")
        if action.description:
            out += [action.description, ""]
        params = " ".join(f"--param {p}=<{s.object_type or s.type}>" for p, s in action.parameters.items())
        out += ["```bash", f"nano-onto run {action.name} {params}".rstrip(), "```", ""]
        if action.allowed_roles:
            out.append("Allowed roles: " + ", ".join(onto.role_label(r) for r in action.allowed_roles))
        for rule in action.rules:
            out.append(_rule_line(rule, onto))
        out.append("")
    out += [
        "## Reporting results",
        "",
        "- `executed`: done -- report the created / updated IDs.",
        "- `pending_approval`: NOT done yet -- tell the user who must approve and give the request ID.",
        "- `denied`: blocked by a rule -- quote the rule message; do not try to work around it.",
        "- `invalid`: fix the parameters and try again.",
        "",
        "## Approvals",
        "",
        "Only when the user holds the approving role (and is not the requester):",
        "",
        "```bash",
        "nano-onto pending --role <role>",
        "nano-onto approve <request id>      # or: nano-onto reject <request id> --note <reason>",
        "```",
        "",
    ]
    return "\n".join(out)
