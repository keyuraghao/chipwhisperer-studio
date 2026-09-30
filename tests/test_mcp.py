"""End-to-end test of ``cw-studio mcp`` over stdio with the official MCP client and the simulator."""
import asyncio
import json
import os
import socket
import sys

import pytest

mcp = pytest.importorskip("mcp")
from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

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
    params = StdioServerParameters(command=sys.executable, args=["-m", "cwstudio", "mcp", "--simulate", "--data-dir", str(tmp_path), "--port", str(_free_port()), "--url", f"http://127.0.0.1:{_free_port()}"], env={**os.environ, "PYTHONPATH": SRC})
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = {t.name for t in (await s.list_tools()).tools}
            call = lambda name, **a: s.call_tool(name, a)  # noqa: E731
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
            prompts = {p.name for p in (await s.list_prompts()).prompts}
            assert {"cpa_attack", "glitch_search"} <= prompts
            status = await s.read_resource("studio://status")
            assert json.loads(status.contents[0].text)["scope"]["connected"]


def test_mcp_end_to_end(tmp_path):
    asyncio.run(asyncio.wait_for(_session(tmp_path), 240))
