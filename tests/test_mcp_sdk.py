"""Interoperability with the official MCP Python SDK client (skipped if ``mcp`` is not installed)."""

import asyncio
import json
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("mcp")

from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

from conftest import PROCUREMENT  # noqa: E402
from nano_onto.cli import main  # noqa: E402
from nano_onto.http_server import issue_token  # noqa: E402

SCHEMA, SEED = str(PROCUREMENT / "ontology.yaml"), str(PROCUREMENT / "seed.json")
CLI = [sys.executable, "-m", "nano_onto.cli"]
ROOT = str(Path(__file__).resolve().parents[1])


async def drive(read, write):
    async with ClientSession(read, write) as session:
        init = await session.initialize()
        tools = {t.name for t in (await session.list_tools()).tools}
        held = await session.call_tool("submit_purchase_order", {"title": "服务器", "amount": 60000, "supplier": "S-002"})
        seen = await session.call_tool("onto_get", {"type": "Supplier", "id": "S-001"})
        refused = await session.call_tool("blacklist_supplier", {"supplier": "S-001"})
        return (init, tools, *(json.loads(r.content[0].text) for r in (held, seen, refused)))


def check(result):
    init, tools, held, seen, refused = result
    assert getattr(init, "protocol_version", None) or getattr(init, "protocolVersion", None)
    assert {"submit_purchase_order", "onto_query", "onto_explain", "onto_request_status"} <= tools
    assert held["status"] == "pending_approval" and held["required_approvals"] == ["cfo", "dept_manager"]
    assert seen["properties"]["bank_account"] == "***"
    assert refused["status"] == "denied"


def test_stdio_server_with_the_official_client(tmp_path):
    db = str(tmp_path / "s.db")
    assert main(["init", "--schema", SCHEMA, "--db", db, "--seed", SEED]) == 0

    async def run():
        params = StdioServerParameters(command=sys.executable, env={"PYTHONPATH": ROOT}, args=[
            "-m", "nano_onto.cli", "serve-mcp", "--schema", SCHEMA, "--db", db, "--actor", "alice", "--role", "employee"])
        async with stdio_client(params) as streams:
            return await drive(streams[0], streams[1])

    check(asyncio.run(run()))


def test_http_server_with_the_official_client(tmp_path):
    http = pytest.importorskip("mcp.client.streamable_http")
    if not hasattr(http, "streamable_http_client") or not hasattr(http, "create_mcp_http_client"):
        pytest.skip("this MCP SDK version has a different HTTP client API")
    db, tokens = str(tmp_path / "h.db"), tmp_path / "tokens.yaml"
    assert main(["init", "--schema", SCHEMA, "--db", db, "--seed", SEED]) == 0
    token = issue_token(tokens, "alice", "employee")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = subprocess.Popen([*CLI, "serve-http", "--schema", SCHEMA, "--db", db, "--tokens", str(tokens), "--port", str(port)],
                              env={"PYTHONPATH": ROOT, "PATH": "/usr/bin:/bin"}, stderr=subprocess.DEVNULL)

    async def run():
        async with http.create_mcp_http_client(headers={"Authorization": f"Bearer {token}"}) as client:
            async with http.streamable_http_client(f"http://127.0.0.1:{port}/mcp", http_client=client) as streams:
                return await drive(streams[0], streams[1])

    try:
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.05)
        check(asyncio.run(run()))
    finally:
        server.terminate()
