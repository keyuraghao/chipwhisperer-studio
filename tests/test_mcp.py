"""Tests of Studio's MCP server: protocol unit tests, a raw stdio session, and an end-to-end run with the official MCP client (when installed) against the simulator."""
import asyncio
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Literal, Optional

import pytest

from cwstudio.mcplite import Server

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
KEY = "2b7e151628aed2a6abf7158809cf4f3c"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _result(res):
    assert not res.is_error, res.content[0].text if res.content else res
    if res.structured_content is not None:
        return res.structured_content.get("result", res.structured_content)
    return json.loads(res.content[0].text)


async def _session(tmp_path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(command=sys.executable, args=["-m", "cwstudio", "mcp", "--simulate", "--data-dir", str(tmp_path), "--port", str(_free_port()), "--url", f"http://127.0.0.1:{_free_port()}"], env={**os.environ, "PYTHONPATH": SRC})
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = {t.name for t in (await s.list_tools()).tools}
            call = lambda tool_name, **a: s.call_tool(tool_name, a)  # noqa: E731
            assert {"scope_connect", "capture_start", "cpa_start", "glitch_start", "firmware_build", "toolchain_install", "firmware_check_updates"} <= tools
            assert len(tools) >= 50
            _result(await call("scope_connect", kind="sim"))
            _result(await call("target_connect", kind="sim"))
            settings = _result(await call("scope_get_settings", filter="adc.samples"))
            assert settings and settings[0]["path"] == "adc.samples"
            assert _result(await call("scope_set_setting", path="adc.samples", value=2000))["value"] == 2000
            cap = _result(await call("capture_start", count=60, clear=True, key=KEY))
            assert cap["traces"]["count"] == 60
            tr = _result(await call("trace_get", index=0, max_points=100))
            assert tr["n"] == 2000 and len(tr["samples"]) <= 100 and tr["key"] == KEY
            cpa = _result(await call("cpa_start", model="sbox_hw"))
            assert cpa["done"] and cpa["best_key"] == KEY
            _result(await call("scope_set_setting", path="glitch.trigger_src", value="ext_single"))
            _result(await call("scope_set_setting", path="glitch.repeat", value=1))
            gl = _result(await call("glitch_start", parameters=[{"path": "glitch.ext_offset", "values": [30, 40]}, {"path": "glitch.width", "values": [10, 20]}], wait=True))
            assert len(gl["results"]) == 4
            cat = _result(await call("firmware_catalogue"))
            assert "sources" in cat and "platforms" in cat
            nb = _result(await call("notebook_run_code", code="import chipwhisperer as cw\ns2 = cw.scope()\nprint(len(studio.traces))\n6 * 7"))
            assert nb["ok"] and "42" in nb["text"] and "60" in nb["text"], nb
            assert _result(await call("calculate", expression="hw(0xff) ^ 1"))["value"] == 9
            assert _result(await call("selection_stats", values=[1, 2, 3]))["mean"] == 2
            _result(await call("note_write", name="agent", text="key found"))
            assert "key found" in _result(await call("note_read", name="agent.md"))["text"]
            _result(await call("notebook_write", path="agent/demo.ipynb", cells=[{"type": "markdown", "source": "# Demo"}, {"type": "code", "source": "print('hi from agent')"}]))
            ran = _result(await call("notebook_run", path="agent/demo.ipynb"))
            assert ran["ok"] and ran["cells"][0]["text"] == "hi from agent\n", ran
            # each notebook has its own kernel; without notebook_path the tools use the shared default kernel
            assert _result(await call("notebook_run_code", code="only_here = 7", notebook_path="agent/demo.ipynb"))["kernel"] == "agent/demo.ipynb"
            assert [v["name"] for v in _result(await call("kernel_variables", notebook_path="agent/demo.ipynb"))] == ["only_here"]
            assert "only_here" not in {v["name"] for v in _result(await call("kernel_variables"))}
            assert "agent/demo.ipynb" in {k["kernel"] for k in _result(await call("kernel_list"))}
            assert _result(await call("kernel_interrupt", notebook_path="agent/demo.ipynb"))["kernel"] == "agent/demo.ipynb"
            assert _result(await call("kernel_restart", notebook_path="agent/demo.ipynb"))["execution_count"] == 0
            assert _result(await call("kernel_variables", notebook_path="agent/demo.ipynb")) == []
            assert _result(await call("kernel_shutdown", notebook_path="agent/demo.ipynb"))["shutdown"]
            assert _result(await call("kernel_variables")), "the default kernel keeps s2 from the first notebook_run_code"
            prompts = {p.name for p in (await s.list_prompts()).prompts}
            assert {"cpa_attack", "glitch_search"} <= prompts
            status = await s.read_resource("studio://status")
            assert json.loads(status.contents[0].text)["scope"]["connected"]


def test_mcp_end_to_end(tmp_path):
    pytest.importorskip("mcp")
    asyncio.run(asyncio.wait_for(_session(tmp_path), 240))


def _demo_server() -> Server:
    srv = Server(name="demo", version="1.0", instructions="hi")

    @srv.tool(annotations={"readOnlyHint": True})
    def add(a: int, b: int = 2, mode: Literal["sum", "diff"] = "sum", tags: Optional[List[str]] = None) -> Dict[str, Any]:
        """Add or subtract."""
        return {"value": a + b if mode == "sum" else a - b, "tags": tags}

    @srv.tool()
    def boom() -> List[int]:
        """Always fails."""
        raise RuntimeError("bad")

    @srv.tool()
    def nan() -> Dict[str, Any]:
        """Returns a NaN."""
        return {"x": float("nan"), "y": [1.0, float("inf")]}

    @srv.prompt()
    def plan(n: int = 3) -> str:
        """A plan."""
        return f"do {n} things"

    @srv.resource("demo://x", mime_type="application/json")
    def res() -> str:
        """A resource."""
        return json.dumps({"x": 1})
    return srv


def test_mcplite_protocol():
    srv = _demo_server()
    rpc = lambda method, params=None, i=1: srv.handle({"jsonrpc": "2.0", "id": i, "method": method, "params": params or {}})  # noqa: E731
    init = rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}})["result"]
    assert init["protocolVersion"] == "2025-06-18" and init["serverInfo"]["name"] == "demo" and init["instructions"] == "hi"
    assert rpc("initialize", {"protocolVersion": "1999-01-01"})["result"]["protocolVersion"] == "2025-11-25"
    assert srv.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    tool = rpc("tools/list")["result"]["tools"][0]
    sch = tool["inputSchema"]
    assert tool["annotations"] == {"readOnlyHint": True} and tool["description"] == "Add or subtract."
    assert sch["required"] == ["a"] and sch["properties"]["mode"]["enum"] == ["sum", "diff"] and sch["properties"]["tags"]["anyOf"][0]["items"] == {"type": "string"}
    assert tool["outputSchema"]["properties"]["result"]["type"] == "object" and tool["outputSchema"]["required"] == ["result"]
    res = rpc("tools/call", {"name": "add", "arguments": {"a": "5", "mode": "diff"}})["result"]
    assert not res["isError"] and res["structuredContent"] == {"result": {"value": 3, "tags": None}} and json.loads(res["content"][0]["text"])["value"] == 3
    # null is only accepted for Optional parameters; list items are validated; bools and numbers convert like pydantic's lax mode
    assert "must not be null" in rpc("tools/call", {"name": "add", "arguments": {"a": None}})["result"]["content"][0]["text"]
    assert rpc("tools/call", {"name": "add", "arguments": {"a": 1, "tags": None}})["result"]["structuredContent"]["result"]["tags"] is None
    assert rpc("tools/call", {"name": "add", "arguments": {"a": 1, "tags": ["x", 2]}})["result"]["isError"]
    assert rpc("tools/call", {"name": "add", "arguments": {"a": "3.0", "b": True}})["result"]["structuredContent"]["result"]["value"] == 4
    assert rpc("tools/call", {"name": "add", "arguments": [1]})["result"]["isError"]
    assert rpc("tools/list", "nope")["error"]["code"] == -32602
    assert srv.handle({"jsonrpc": "2.0", "id": None, "method": "ping"})["error"]["code"] == -32600
    assert rpc("tools/call", {"name": "add", "arguments": {"b": 1}})["result"]["isError"]
    assert "must be one of" in rpc("tools/call", {"name": "add", "arguments": {"a": 1, "mode": "x"}})["result"]["content"][0]["text"]
    bad = rpc("tools/call", {"name": "boom"})["result"]
    assert bad["isError"] and "bad" in bad["content"][0]["text"]
    assert rpc("tools/call", {"name": "nope"})["error"]["code"] == -32602
    assert rpc("nope/nope")["error"]["code"] == -32601
    assert rpc("prompts/get", {"name": "plan", "arguments": {"n": "5"}})["result"]["messages"][0]["content"]["text"] == "do 5 things"
    assert json.loads(rpc("resources/read", {"uri": "demo://x"})["result"]["contents"][0]["text"]) == {"x": 1}
    nan = rpc("tools/call", {"name": "nan"})["result"]
    assert nan["structuredContent"] == {"result": {"x": None, "y": [1.0, None]}} and "NaN" not in json.dumps(nan)
    batch = srv.handle_raw(json.dumps([{"jsonrpc": "2.0", "id": 1, "method": "ping"}, {"jsonrpc": "2.0", "method": "notifications/cancelled"}]).encode())
    assert batch == [{"jsonrpc": "2.0", "id": 1, "result": {}}]
    assert srv.handle_raw(b"{oops")["error"]["code"] == -32700


def test_mcplite_streamable_http():
    srv, port = _demo_server(), _free_port()
    threading.Thread(target=srv.run, args=("streamable-http", "127.0.0.1", port), daemon=True).start()

    sid = {}

    def post(msg, **extra):
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream", **sid, **extra}
        req = urllib.request.Request(f"http://127.0.0.1:{port}/mcp", data=json.dumps(msg).encode(), headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read()
    for _ in range(50):
        try:
            status, headers, raw = post({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
            break
        except OSError:
            time.sleep(0.1)
    assert status == 200 and headers["Mcp-Session-Id"] and json.loads(raw)["result"]["serverInfo"]["name"] == "demo"
    assert post({"jsonrpc": "2.0", "id": 9, "method": "ping"})[0] == 400  # no session id yet
    sid["Mcp-Session-Id"] = headers["Mcp-Session-Id"]
    assert post({"jsonrpc": "2.0", "id": 9, "method": "ping"}, Origin="http://evil.example")[0] == 403
    assert post({"jsonrpc": "2.0", "id": 9, "method": "ping"}, Host="evil.example")[0] == 421
    assert post({"jsonrpc": "2.0", "id": 9, "method": "ping"}, **{"Mcp-Session-Id": "bogus"})[0] == 404
    assert post({"jsonrpc": "2.0", "method": "notifications/initialized"})[0] == 202
    out = json.loads(post({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "add", "arguments": {"a": 1, "b": 2}}})[2])
    assert out["result"]["structuredContent"]["result"]["value"] == 3


def test_mcp_stdio_raw(tmp_path):
    """A full stdio session without the MCP SDK: handshake, tool calls on the simulator, and stdout kept clean."""
    env = {**os.environ, "PYTHONPATH": SRC}
    p = subprocess.Popen([sys.executable, "-m", "cwstudio", "mcp", "--simulate", "--data-dir", str(tmp_path), "--port", str(_free_port()), "--url", f"http://127.0.0.1:{_free_port()}"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env)
    try:
        def ask(i, method, params=None):
            p.stdin.write((json.dumps({"jsonrpc": "2.0", "id": i, "method": method, "params": params or {}}) + "\n").encode())
            p.stdin.flush()
            while True:  # replies to earlier background calls may arrive first
                msg = json.loads(p.stdout.readline())
                if msg.get("id") == i:
                    return msg
        assert ask(1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}})["result"]["serverInfo"]["name"] == "chipwhisperer-studio"
        p.stdin.write(b'{"jsonrpc": "2.0", "method": "notifications/initialized"}\n')
        assert len(ask(2, "tools/list")["result"]["tools"]) >= 60
        r = ask(3, "tools/call", {"name": "scope_connect", "arguments": {"kind": "sim"}})["result"]
        assert not r["isError"], r
        r = ask(4, "tools/call", {"name": "notebook_run_code", "arguments": {"code": "print('noise on stdout')\n1 + 1"}})["result"]
        assert not r["isError"] and "noise on stdout" in r["structuredContent"]["result"]["text"]
        r = ask(11, "tools/call", {"name": "notebook_run_code", "arguments": {"code": "mine = 1", "notebook_path": "lab/a.ipynb"}})["result"]
        assert not r["isError"] and r["structuredContent"]["result"]["kernel"] == "lab/a.ipynb", r
        names = lambda res: [v["name"] for v in res["structuredContent"]["result"]]  # noqa: E731
        assert names(ask(12, "tools/call", {"name": "kernel_variables", "arguments": {"notebook_path": "lab/a.ipynb"}})["result"]) == ["mine"]
        assert "mine" not in names(ask(13, "tools/call", {"name": "kernel_variables", "arguments": {}})["result"])
        # a long call must not delay others: during a continuous capture, twelve 3 s job waits, then a validation error and ping answer at once
        assert not ask(8, "tools/call", {"name": "target_connect", "arguments": {"kind": "sim"}})["result"]["isError"]
        assert not ask(9, "tools/call", {"name": "capture_start", "arguments": {"count": 0, "wait": False}})["result"]["isError"]
        for i in range(12):
            p.stdin.write((json.dumps({"jsonrpc": "2.0", "id": 100 + i, "method": "tools/call", "params": {"name": "job_wait", "arguments": {"timeout_s": 3}}}) + "\n").encode())
        p.stdin.flush()
        t0 = time.time()
        r = ask(6, "tools/call", {"name": "capture_start", "arguments": {"count": None}})["result"]
        assert r["isError"] and "must not be null" in r["content"][0]["text"]
        assert ask(7, "ping")["id"] == 7 and time.time() - t0 < 2
        assert not ask(10, "tools/call", {"name": "capture_stop", "arguments": {}})["result"]["isError"]
        assert ask(5, "ping") == {"jsonrpc": "2.0", "id": 5, "result": {}}
    finally:
        p.stdin.close()
        p.terminate()
        p.wait(10)
