"""Regenerate skills/chipwhisperer-studio/references/tools.md from the MCP server's own tool list, so the agent skill always matches the tools Studio serves.

Run from the repository root: PYTHONPATH=src python tools/skill_reference.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from cwstudio import __version__  # noqa: E402
from cwstudio.mcp_server import StudioClient, build_server  # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "skills", "chipwhisperer-studio", "references", "tools.md")

# Tool name prefix -> section, in the order the sections appear.
SECTIONS = [
    ("General", ("studio_", "list_devices", "get_logs", "open_ui")),
    ("Scope", ("scope_",)),
    ("Target and serial", ("target_", "serial_", "simpleserial")),
    ("Capture", ("capture_", "job_wait")),
    ("Traces", ("trace",)),
    ("CPA", ("cpa_",)),
    ("Glitch", ("glitch_",)),
    ("Toolchains", ("toolchain",)),
    ("Firmware", ("firmware_",)),
    ("Notebooks and kernels", ("notebook_", "kernel_", "tutorials_")),
    ("Notes and calculator", ("note", "calculate", "selection_stats")),
    ("Interfaces", ("hardware_capabilities", "interfaces_", "uart_", "spi_", "gpio_", "userio_", "trigger_", "bitbang", "onewire")),
    ("OpenOCD", ("openocd_",)),
    ("Logic analyser", ("la_",)),
    ("Code map", ("code_map_",)),
]


def kind(ann):
    if ann.get("readOnlyHint"):
        return "read only"
    if ann.get("destructiveHint"):
        return "DESTRUCTIVE"
    if ann.get("openWorldHint"):
        return "network"
    return "changes state"


def signature(schema):
    props = schema.get("properties", {})
    req = set(schema.get("required", []))
    parts = []
    for name, p in props.items():
        if name in req:
            parts.append(name)
        else:
            default = p.get("default", None)
            parts.append(f"{name}={default!r}" if "default" in p else f"{name}?")
    return ", ".join(parts)


def main():
    server = build_server(StudioClient("http://127.0.0.1:1"))
    tools = server._tools_list({})["tools"]
    anns = {name: ann for name, (_c, ann) in server.tools.items()}
    groups = {title: [] for title, _ in SECTIONS}
    groups["Other"] = []
    for t in tools:
        for title, prefixes in SECTIONS:
            if t["name"].startswith(prefixes):
                groups[title].append(t)
                break
        else:
            groups["Other"].append(t)
    lines = [f"# ChipWhisperer Studio MCP tool reference", "",
             f"Generated from the MCP server of chipwhisperer-studio {__version__} by tools/skill_reference.py ({len(tools)} tools). Do not edit by hand.", "",
             "Each entry: `name(arguments)` with defaults (`?` means optional with no default), whether it changes anything, then the server's own description. Every result arrives wrapped as `{\"result\": ...}`.", ""]
    for title, items in groups.items():
        if not items:
            continue
        lines += [f"## {title}", ""]
        for t in items:
            desc = " ".join(t.get("description", "").split())
            lines.append(f"- `{t['name']}({signature(t['inputSchema'])})` [{kind(anns.get(t['name'], {}))}]: {desc}")
        lines.append("")
    prompts = server._prompts_list({})["prompts"]
    lines += ["## Prompts", ""]
    for p in prompts:
        args = ", ".join(a["name"] for a in p.get("arguments", []))
        lines.append(f"- `{p['name']}({args})`: {p.get('description', '')}")
    lines += ["", "## Resources", ""]
    for uri in server.resources:
        lines.append(f"- `{uri}`")
    lines.append("")
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"wrote {os.path.relpath(OUT)} ({len(tools)} tools)")


if __name__ == "__main__":
    main()
