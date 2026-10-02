"""MCP tools for the code map: emulate the firmware for a captured trace, map its code onto the samples, and ask which code a range of samples is (or where a function or source line ran).

They map onto the ``/api/codemap/*`` HTTP routes. What the code map cannot do here (an architecture it cannot emulate, a missing engine, firmware without getch/putch, exact mode without a Husky) is not a tool error: the tool answers ``{"ok": false, "supported": false, "reason": ...}``.
"""
from __future__ import annotations

from typing import Any, Dict, Literal, Optional


def register_codemap_tools(mcp, client, RO: Dict[str, Any], HW: Dict[str, Any]) -> None:
    from cwstudio.mcp_server import StudioError

    def call(method: str, path: str, body: Optional[Dict[str, Any]] = None, timeout: Optional[float] = None, **params) -> Any:
        try:
            if method == "GET":
                return client.get(path, **params)
            if method == "PUT":
                return client.put(path, body or {})
            return client.post(path, body or {}, timeout=timeout)
        except StudioError as e:
            msg = str(e)
            if "Unsupported:" in msg:
                return {"ok": False, "supported": False, "reason": msg.split("Unsupported:", 1)[1].strip()}
            raise

    def clean(**kw) -> Dict[str, Any]:
        return {k: v for k, v in kw.items() if v is not None}

    @mcp.tool(annotations=HW)
    def code_map_build(elf: Optional[str] = None, sources: Optional[str] = None, trace: Optional[int] = None, key: Optional[str] = None, text: Optional[str] = None, core: Optional[str] = None,
                       protocol: Optional[Literal["2.1", "1.1", "1.0"]] = None, wait_states: Optional[int] = None, align: Optional[bool] = None, target_freq: Optional[float] = None, adc_freq: Optional[float] = None, top: int = 25,
                       cmd: Optional[str] = None, raw: Optional[str] = None, max_instructions: Optional[int] = None) -> Dict[str, Any]:
        """Work out which firmware code runs when during a captured trace. Emulates the ELF (default: the firmware programmed in this session, else the newest build) for the key and plaintext of stored trace `trace` (default the newest; or give key/text in hex), maps clock cycles to samples from the scope's clocks and ADC settings, and aligns the emulated power model with the stored traces. Supports Arm Cortex-M (Unicorn), RISC-V RV32 (Unicorn) and AVR/XMEGA (Studio's own cycle-accurate emulator). core overrides the timing model (cortex-m0, cortex-m0+, cortex-m3, cortex-m4, cortex-m7, cortex-m33, rv32-generic, ibex, neorv32, avr, avrxmega). sources is a folder to find the source files in. cmd is the SimpleSerial command to emulate (default 'p'; e.g. 'g' for simpleserial-glitch, with text "" for no data); raw is hex serial input fed as it is, for firmware that is not SimpleSerial (the response is then its output); max_instructions raises the per command limit (default 4000000) for long computations. A firmware that ends in an endless loop after answering reports where in `halted`. Returns whether the emulated ciphertext is the correct AES and matches the stored one, the mapping and alignment, and the `top` functions that run in the trigger window with their sample ranges."""
        body = clean(elf=elf, sources=sources, trace=trace, key=key, text=text, core=core, protocol=protocol, wait_states=wait_states, align=align, cmd=cmd, raw=raw)
        if max_instructions:
            body["options"] = {"max_instructions": int(max_instructions)}
        if target_freq or adc_freq:
            body["mapping"] = clean(target_freq=target_freq, adc_freq=adc_freq)
        st = call("POST", "/api/codemap/build", body, timeout=300)
        if not isinstance(st, dict) or st.get("supported") is False:
            return st
        band = st.pop("band", None) or {}
        t1 = band.get("t1") if band.get("t1") is not None else band.get("total")
        m = st.get("mapping") or {}
        if t1:
            a = m.get("samples_per_cycle", 4.0)
            b = m["intercept"] if m.get("intercept") is not None else -m.get("adc_offset", 0) / max(1, m.get("decimate") or 1) + m.get("presamples", 0) + m.get("shift", 0)
            reg = call("POST", "/api/codemap/region", {"start": b, "end": t1 * a + b, "limit": 50})
            if isinstance(reg, dict):
                st["trigger_window"] = {"samples": reg.get("samples"), "functions": [{k: f.get(k) for k in ("name", "file", "line", "self_cycles", "share", "samples", "depth")} for f in reg.get("functions", [])[:top]]}
        st.pop("engines", None)
        st.pop("cores", None)
        return st

    @mcp.tool(annotations=RO)
    def code_map_region(start: float, end: float, limit: int = 100) -> Dict[str, Any]:
        """Which code a range of samples represents (after code_map_build): the functions with their self cycles, share of the range and inclusive spans, and the source lines (file, line, source text, cycles, how often they ran), in the order they first run."""
        return call("POST", "/api/codemap/region", {"start": start, "end": end, "limit": limit})

    @mcp.tool(annotations=RO)
    def code_map_lookup(function: Optional[str] = None, file: Optional[str] = None, line: Optional[int] = None) -> Dict[str, Any]:
        """Where a function (by name) or a source line (file name or path, and line number) ran during the trace: its cycle and sample ranges."""
        return call("POST", "/api/codemap/lookup", clean(function=function, file=file, line=line))

    @mcp.tool(annotations=HW)
    def code_map_align(source: Literal["mean", "trace"] = "mean", trace: Optional[int] = None, scale_min: float = 0.9, scale_max: float = 1.1, max_shift: Optional[int] = None,
                       shift: Optional[float] = None, scale: Optional[float] = None) -> Dict[str, Any]:
        """Fit the cycle to sample mapping by cross-correlating the emulated power model with the mean of the stored traces (or one trace): returns the shift (samples), scale, correlation, a confidence (0..1, label high/medium/low: high means a strong correlation with no rival match within the search window) and whether the fit was applied (a low confidence fit is not: the nominal mapping is kept). Giving shift and/or scale instead sets the mapping by hand."""
        if shift is not None or scale is not None:
            return call("PUT", "/api/codemap/mapping", clean(shift=shift, scale=scale))
        return call("POST", "/api/codemap/align", clean(source=source, trace=trace, scale_min=scale_min, scale_max=scale_max, max_shift=max_shift))

    @mcp.tool(annotations=RO)
    def code_map_disassemble(function: Optional[str] = None, file: Optional[str] = None, line: Optional[int] = None) -> Dict[str, Any]:
        """Disassembly of a function or of a source line (file and line) of the mapped firmware, with how often each instruction ran in the emulated trace."""
        return call("GET", "/api/codemap/disasm", **clean(function=function, file=file, line=line))

    @mcp.tool(annotations=HW)
    def code_map_pc_trace(interval: int = 64) -> Dict[str, Any]:
        """Exact mode: record real program counter samples through a ChipWhisperer-Husky's Arm trace port (SWO, every `interval` cycles, rounded to the nearest the Arm DWT supports: 64 or 1024 times 1 to 16, the result's `interval`; needs simpleserial-trace firmware) and compare them with the emulation: agreement (share of samples in the predicted function) and the fitted cycle scale. On the simulator the samples come from the emulated run through the same decoder."""
        return call("POST", "/api/codemap/pctrace", {"interval": interval}, timeout=120)
