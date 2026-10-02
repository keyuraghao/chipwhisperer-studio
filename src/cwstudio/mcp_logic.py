"""MCP tools for the logic analyser: sources, capture, import and export, decoders, measurements, channels and search.

They map onto the ``/api/la/*`` HTTP routes. Like the interface tools, a source or decoder the connected hardware (or the missing sigrok-cli) cannot provide is not a tool error: the tool answers ``{"ok": false, "supported": false, "reason": ...}``.
"""
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

Decoder = Literal["uart", "spi", "i2c", "onewire", "jtag", "swd", "can", "simpleserial", "sigrok"]


def register_logic_tools(mcp, client, RO: Dict[str, Any], HW: Dict[str, Any]) -> None:
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

    @mcp.tool(annotations=RO)
    def la_sources() -> Dict[str, Any]:
        """Logic analyser sources and whether each is available (with the reason when not): native (Husky scope.LA: 9-signal groups 'CW 20-pin', 'USERIO 20-pin', 'glitch'), adc (one line on the analog input of any ChipWhisperer, thresholded), sim (the simulator's demo traffic: UART, SPI, I2C, 1-Wire, CAN, JTAG, SWD, trigger, clock), sigrok (external analysers through sigrok-cli, with install steps when missing) and files (VCD, CSV, .sr). Also lists LA triggers, clock sources and the decoders with their options."""
        r = call("GET", "/api/la/sources")
        if isinstance(r, dict):
            r.pop("la_trigger_help", None)
        return r

    @mcp.tool(annotations=HW)
    def la_capture(source: Literal["native", "adc", "sim", "sigrok"] = "sim", group: Optional[Literal["CW 20-pin", "USERIO 20-pin", "glitch"]] = None, clk_source: Optional[Literal["usb", "target", "pll"]] = None, oversampling: Optional[float] = None, downsample: Optional[int] = None,
                   depth: Optional[int] = None, trigger: Optional[str] = None, with_analog: Optional[bool] = None, fire: Optional[Literal["simpleserial", "none"]] = None, segments: Optional[int] = None, samples: Optional[int] = None, level: Optional[str] = None,
                   hysteresis: Optional[float] = None, sim_signal: Optional[str] = None, samplerate: Optional[float] = None, duration_ms: Optional[float] = None, pretrigger: Optional[float] = None, channels: Optional[List[str]] = None,
                   jitter_ns: Optional[float] = None, glitches_per_ms: Optional[float] = None, device: Optional[str] = None, time_ms: Optional[float] = None, triggers: Optional[str] = None, timeout: float = 60) -> Dict[str, Any]:
        """Capture logic data and wait for it; returns the capture summary (channels, sample rate, trigger index, duration). native (Husky): group, clk_source and oversampling set the sampling clock (rate = source x oversampling / downsample), depth (max 16376 Husky, 65535 Husky Plus), trigger ('capture', 'manual', 'rising_tio1', 'falling_userio_d3', 'HS1', 'glitch', ...; no pre-trigger), fire='simpleserial' sends a command so the target raises its trigger, with_analog also keeps the ADC trace (trigger 'capture'). adc (any ChipWhisperer): segments traces of samples each, level ('auto' or volts) and hysteresis; on the simulator sim_signal picks the demo line on the measure input. sim: samplerate, duration_ms, pretrigger (%), channels, jitter_ns, glitches_per_ms. sigrok: device (e.g. 'fx2lafw' or 'demo'), samplerate, channels, samples or time_ms, triggers ('D0=r,D1=1')."""
        settings = clean(group=group, clk_source=clk_source, oversampling=oversampling, downsample=downsample, depth=depth, trigger=trigger, with_analog=with_analog, fire=fire, segments=segments, samples=samples, level=level, hysteresis=hysteresis,
                         sim_signal=sim_signal, samplerate=samplerate, duration_ms=duration_ms, pretrigger=pretrigger, channels=channels, jitter_ns=jitter_ns, glitches_per_ms=glitches_per_ms, device=device, time_ms=time_ms, triggers=triggers, timeout=None)
        return call("POST", "/api/la/capture", {"source": source, "settings": settings, "wait": True, "timeout": timeout}, timeout=timeout + 30)

    @mcp.tool(annotations=HW)
    def la_import(path: str, format: Optional[Literal["vcd", "csv", "sr"]] = None, samplerate: Optional[float] = None) -> Dict[str, Any]:
        """Import a logic capture from any analyser: a .vcd, .csv (Saleae Logic export, or a time/sample-index column plus one 0/1 column per channel) or sigrok .sr file on the Studio machine. samplerate is needed for CSV files with sample indices or no time column. It becomes the current capture."""
        return call("POST", "/api/la/import", clean(path=path, format=format, samplerate=samplerate))

    @mcp.tool(annotations=HW)
    def la_export(format: Literal["vcd", "csv", "sr"] = "vcd", path: Optional[str] = None, channels: Optional[List[str]] = None) -> Dict[str, Any]:
        """Write the current capture as VCD, CSV (Saleae style) or a sigrok .sr session; path defaults to Studio's logic/exports folder. channels exports a subset (names or indices)."""
        return call("POST", "/api/la/export", clean(format=format, path=path, channels=channels))

    @mcp.tool(annotations=RO)
    def la_decode(decoder: Decoder, channels: Optional[Dict[str, Any]] = None, options: Optional[Dict[str, Any]] = None, limit: int = 200, add_to_view: bool = False) -> Dict[str, Any]:
        """Decode the current capture and return the annotations (time from the trigger, row, channel, kind, text, value). channels maps roles to channel names or indices (uart/simpleserial: rx, tx; spi: cs, sck, mosi, miso; i2c: scl, sda; onewire: owr; jtag: tck, tms, tdi, tdo; swd: swclk, swdio; can: can); left out, they are guessed from the channel names. options: uart baud ('auto' or a number), data_bits 5-9, parity, stop_bits, bit_order, inverted; spi mode 0-3, bit_order, word_size, cs_active; i2c address_format; can bitrate, sample_point; simpleserial version auto/1/2 plus the UART options; sigrok spec (e.g. 'uart:rx=D0:baudrate=115200', channels named D0..Dn by position, needs sigrok-cli). add_to_view also shows it in the Logic tab."""
        body = clean(type=decoder, channels=channels, options=options)
        if add_to_view:
            added = call("POST", "/api/la/decoders", body)
            if isinstance(added, dict) and added.get("supported") is False:
                return added
        return call("POST", "/api/la/decode", dict(body, limit=limit))

    @mcp.tool(annotations=RO)
    def la_measure(channel: Optional[str] = None, t0: Optional[float] = None, t1: Optional[float] = None, from_sample: Optional[int] = None, to_sample: Optional[int] = None) -> Dict[str, Any]:
        """Measure a channel (name or index; all channels when left out) of the current capture between t0 and t1 seconds from the trigger (or sample indices): edge counts (rising/falling), frequency, period, duty cycle and high/low pulse width min/max/avg."""
        return call("POST", "/api/la/measure", clean(channel=channel, t0=t0, t1=t1, **{"from": from_sample, "to": to_sample}))

    @mcp.tool(annotations=HW)
    def la_channels(rename: Optional[Dict[str, str]] = None, hide: Optional[List[str]] = None, show: Optional[List[str]] = None, order: Optional[List[str]] = None, colors: Optional[Dict[str, str]] = None, buses: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        """List the current capture's channels (index, name, colour, hidden, edge count) and change them: rename {old: new}, hide/show lists, order (top to bottom), colors {name: '#rrggbb'}, buses [{name, channels (MSB first), format hex/dec/bin}] shown as a value row. With no arguments it only lists."""
        if not any(x is not None for x in (rename, hide, show, order, colors, buses)):
            return call("GET", "/api/la/channels")
        upd: Dict[str, Dict[str, Any]] = {}
        for old, new in (rename or {}).items():
            upd.setdefault(old, {"index": old})["name"] = new
        for ch in hide or []:
            upd.setdefault(ch, {"index": ch})["hidden"] = True
        for ch in show or []:
            upd.setdefault(ch, {"index": ch})["hidden"] = False
        for ch, col in (colors or {}).items():
            upd.setdefault(ch, {"index": ch})["color"] = col
        return call("PUT", "/api/la/channels", clean(channels=list(upd.values()) or None, order=order, buses=buses))

    @mcp.tool(annotations=RO)
    def la_search(kind: Literal["edge", "pattern", "decoded"] = "edge", channel: Optional[str] = None, edge: Literal["any", "rising", "falling"] = "any", pattern: Optional[str] = None, edge_channel: Optional[str] = None, text: Optional[str] = None,
                  from_sample: int = -1, direction: Literal["next", "prev"] = "next") -> Dict[str, Any]:
        """Find the next (or previous) edge on a channel, a pattern across the shown channels in display order ('1X0': first channel high, second any, third low; optionally only at an edge of edge_channel), or a decoded value (text like '0x41' or 'NACK'), starting from a sample index. Returns the sample index and time."""
        return call("POST", "/api/la/search", clean(kind=kind, channel=channel, edge=edge, pattern=pattern, edge_channel=edge_channel, text=text, direction=direction, **{"from": from_sample}))

    @mcp.tool(annotations=RO)
    def la_status() -> Dict[str, Any]:
        """The logic analyser state: a running capture, the current capture's summary, the captures kept in memory, configured decoders and buses."""
        return call("GET", "/api/la")
