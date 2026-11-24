"""Side effects: call an external API after an action commits, with retries.

An action queues a webhook with a ``webhook`` effect::

    webhooks:
      erp:
        url: ${ERP_HOOK_URL}                 # or a literal https://... URL
        headers: {Authorization: "Bearer ${ERP_TOKEN}"}
        secret_env: ERP_HOOK_SECRET          # optional: HMAC-SHA256 signature
        max_attempts: 6

    action_types:
      submit_purchase_order:
        effects:
          - create: PurchaseOrder
            as: po
            set: {...}
          - webhook: erp
            event: purchase_order.created
            payload: {po_id: po.po_id, amount: params.amount}

The delivery is written to an outbox table *in the action's transaction*, so it
exists exactly when the action committed. :func:`deliver` then POSTs it; a
failure is retried with exponential backoff until ``max_attempts``, after which
the delivery is marked ``dead``. Delivery is at-least-once: receivers should
de-duplicate on the ``X-Nano-Onto-Delivery`` header.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .engine import Engine

DEFAULT_MAX_ATTEMPTS = 6
DEFAULT_TIMEOUT = 10.0
_SYSTEM = {"id": "nano-onto", "role": "system"}
_ENV = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

Sender = Callable[[str, bytes, dict[str, str], float], int]


def _expand(text: str, used: list[str]) -> str:
    """Replace ``${VAR}`` with the environment variable, so secrets stay out of the YAML.

    Every value substituted is appended to ``used`` so it can be scrubbed from error messages.
    """

    def repl(match: re.Match) -> str:
        value = os.environ.get(match.group(1))
        if value is None:
            raise ValueError(f"environment variable {match.group(1)} is not set")
        used.append(value)
        return value

    return _ENV.sub(repl, str(text))


def _scrub(text: str, secrets: list[str]) -> str:
    for secret in sorted({s for v in secrets for s in (v, v.strip()) if s}, key=len, reverse=True):
        text = text.replace(secret, "***").replace(repr(secret)[1:-1], "***")
    return text


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect would re-send the credentials to another host as a body-less GET: treat 3xx as a failure."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _post(url: str, body: bytes, headers: dict[str, str], timeout: float) -> int:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with _OPENER.open(request, timeout=timeout) as response:  # scheme checked by the caller
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


def backoff(attempts: int) -> timedelta:
    """Wait before retry number ``attempts + 1``: 30s, 1m, 2m, ... capped at 1h."""
    return timedelta(seconds=min(30 * 2 ** max(attempts - 1, 0), 3600))


def deliver(engine: Engine, limit: int = 50, sender: Sender = _post,
            now: datetime | None = None) -> list[dict[str, Any]]:
    """Attempt every queued delivery that is due. Returns one ``{id, webhook, status, ...}`` per attempt."""
    now = now or datetime.now(timezone.utc)
    report = []
    stamp = now.isoformat(timespec="seconds")
    for item in engine.store.outbox("pending", stamp, limit):
        spec = engine.onto.webhooks.get(item["webhook"])
        timeout = float((spec or {}).get("timeout") or DEFAULT_TIMEOUT)
        # claim it first, so two sweeps (a server's worker and a CLI run, say) do not both send it;
        # if this process dies mid-send the claim lapses and the delivery is retried
        with engine.store.transaction():
            current = [r for r in engine.store.outbox("pending", stamp, 10_000) if r["id"] == item["id"]]
            if not current:
                continue
            lease = now + timedelta(seconds=timeout + 60)
            engine.store.update_outbox(item["id"], "pending", item["attempts"], lease.isoformat(timespec="seconds"),
                                       item["last_error"])
        attempts = item["attempts"] + 1
        error: str | None = None
        secrets: list[str] = []
        try:
            if spec is None:
                raise ValueError(f"webhook {item['webhook']!r} is no longer defined")
            url = _expand(spec["url"], secrets)
            if not url.startswith(("http://", "https://")):
                raise ValueError("webhook URL must be http(s)")
            body = json.dumps(item["body"], ensure_ascii=False, sort_keys=True).encode("utf-8")
            headers = {k: _expand(v, secrets) for k, v in (spec.get("headers") or {}).items()}
            for name, value in headers.items():
                if "\n" in value or "\r" in value:
                    raise ValueError(f"header {name} contains a line break (check the environment variable)")
            headers.update({
                "Content-Type": "application/json",
                "User-Agent": "nano-onto-webhook",
                "X-Nano-Onto-Event": str(item["body"].get("event", "")),
                "X-Nano-Onto-Delivery": item["id"],
            })
            if spec.get("secret_env"):
                secret = _expand("${" + spec["secret_env"] + "}", secrets).encode("utf-8")
                headers["X-Nano-Onto-Signature"] = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
            status = sender(url, body, headers, timeout)
            if not 200 <= status < 300:
                error = f"HTTP {status}"
        except Exception as exc:  # noqa: BLE001 - any failure means "retry later"
            error = _scrub(f"{type(exc).__name__}: {exc}", secrets)
        max_attempts = int((spec or {}).get("max_attempts") or DEFAULT_MAX_ATTEMPTS)
        if error is None:
            state, next_at = "delivered", now
        elif attempts >= max_attempts:
            state, next_at = "dead", now
        else:
            state, next_at = "pending", now + backoff(attempts)
        with engine.store.transaction():
            engine.store.update_outbox(item["id"], state, attempts, next_at.isoformat(timespec="seconds"), error)
            engine.store.log(_SYSTEM, "webhook", item["body"].get("action"),
                             "delivered" if error is None else ("dead" if state == "dead" else "retrying"),
                             {"delivery": item["id"], "webhook": item["webhook"], "attempt": attempts, "error": error})
        report.append({"id": item["id"], "webhook": item["webhook"], "status": state,
                       "attempts": attempts, "error": error})
    return report


class Worker:
    """Background delivery for long-running servers: ``wake()`` after a commit, plus a periodic retry sweep."""

    def __init__(self, engine: Engine, interval: float = 15.0, sender: Sender = _post):
        self.engine, self.interval, self.sender = engine, interval, sender
        self._wake = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="nano-onto-webhooks", daemon=True)
        self._thread.start()

    def wake(self) -> None:
        self._wake.set()

    def _loop(self) -> None:
        while True:
            self._wake.wait(self.interval)
            self._wake.clear()
            try:
                deliver(self.engine, sender=self.sender)
            except Exception:  # noqa: BLE001 - keep the worker alive; failures are retried on the next sweep
                pass


def retry(engine: Engine, delivery_id: str) -> bool:
    """Put a ``dead`` delivery back in the queue."""
    rows = [r for r in engine.store.outbox("dead", limit=10_000) if r["id"] == delivery_id]
    if not rows:
        return False
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    engine.store.update_outbox(delivery_id, "pending", 0, now, rows[0]["last_error"])
    return True
