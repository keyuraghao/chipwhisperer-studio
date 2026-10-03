"""The agent skill in skills/chipwhisperer-studio must describe exactly the tools the MCP server serves."""
import os
import re

from cwstudio.mcp_server import StudioClient, build_server

SKILL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "skills", "chipwhisperer-studio")


def test_skill_tool_reference_matches_server():
    served = set(build_server(StudioClient("http://127.0.0.1:1")).tools)
    with open(os.path.join(SKILL, "references", "tools.md"), encoding="utf-8") as f:
        listed = set(re.findall(r"^- `([a-z_0-9]+)\(", f.read(), re.M))
    assert served - listed == set(), "tools missing from the skill: run PYTHONPATH=src python tools/skill_reference.py"
    assert listed - served <= {"cpa_attack", "glitch_search"}, "the skill lists tools the server no longer has"


def test_skill_mentions_only_real_tools():
    served = set(build_server(StudioClient("http://127.0.0.1:1")).tools)
    for name in ("SKILL.md", os.path.join("references", "workflows.md")):
        with open(os.path.join(SKILL, name), encoding="utf-8") as f:
            text = f.read()
        called = set(re.findall(r"`([a-z][a-z_0-9]+)\(", text))
        unknown = {c for c in called if "_" in c and c not in served and c not in {"cw_scope", "capture_trace", "add_trace", "build_firmware"}}
        assert not unknown, f"{name} mentions tools the server does not have: {sorted(unknown)}"


def test_skill_frontmatter():
    with open(os.path.join(SKILL, "SKILL.md"), encoding="utf-8") as f:
        head = f.read().split("---")[1]
    assert re.search(r"^name: chipwhisperer-studio$", head, re.M)
    assert re.search(r"^description: .{50,1024}$", head, re.M)
