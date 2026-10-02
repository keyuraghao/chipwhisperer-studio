"""MCP tools for hardware protocols and interfaces (UART, SimpleSerial, SPI, GPIO, USERIO, triggers, bit-banger, 1-Wire, JTAG/SWD through OpenOCD).

They are registered on the server built by :func:`cwstudio.mcp_server.build_server` and map onto the ``/api/interfaces/*`` HTTP routes. A tool whose feature the connected ChipWhisperer (or the simulator's model) does not support does not fail: it answers ``{"ok": false, "supported": false, "reason": ...}`` so the agent can explain why and pick something else. ``hardware_capabilities`` lists what is available up front.
"""
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

Pin = Literal["tio1", "tio2", "tio3", "tio4"]
TriggerPin = Literal["tio1", "tio2", "tio3", "tio4", "nrst", "sma", "userio_d0", "userio_d1", "userio_d2", "userio_d3", "userio_d4", "userio_d5", "userio_d6", "userio_d7"]
BBPin = Literal["USERIO_D0", "USERIO_D1", "USERIO_D2", "USERIO_D3", "USERIO_D4", "USERIO_D5", "USERIO_D6", "USERIO_D7", "USERIO_CK", "TIO1", "TIO2", "TIO3", "TIO4", "target_pwr", "nrst", "disabled"]


def register_interface_tools(mcp, client, RO: Dict[str, Any], HW: Dict[str, Any], DESTRUCTIVE: Dict[str, Any]) -> None:
    from cwstudio.mcp_server import StudioError

    def call(method: str, path: str, body: Optional[Dict[str, Any]] = None, timeout: Optional[float] = None) -> Any:
        """Call the API; an unsupported feature becomes a polite refusal instead of a tool error."""
        try:
            if method == "GET":
                return client.get(path)
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
    def hardware_capabilities() -> Dict[str, Any]:
        """What the connected ChipWhisperer (or the simulator's model) supports: UART pin modes, SimpleSerial versions, SPI, JTAG/SWD, GPIO, USERIO, bit-banger, 1-Wire, each trigger type and each programmer, as {available, reason, details}. Check this before using the other interface tools."""
        return client.get("/api/capabilities")

    @mcp.tool(annotations=RO)
    def interfaces_status() -> Dict[str, Any]:
        """State of every interface: capabilities, UART configuration and pins, SimpleSerial version, SPI master, the last trigger configured here, OpenOCD server and MPSSE mode."""
        return client.get("/api/interfaces")

    @mcp.tool(annotations=HW)
    def uart_configure(baud: Optional[int] = None, parity: Optional[Literal["none", "odd", "even", "mark", "space"]] = None, stop_bits: Optional[float] = None, rx: Optional[Pin] = None, tx: Optional[Pin] = None) -> Dict[str, Any]:
        """Configure the target UART: baud (500 to 2000000), parity, stop bits (1, 1.5 or 2; data bits are always 8) and which TIO pins are RX and TX. Only pin modes valid for the connected model are accepted (the CW-Nano is fixed to RX on TIO1 and TX on TIO2; TIO4 can only transmit)."""
        return call("PUT", "/api/interfaces/uart", clean(baud=baud, parity=parity, stop_bits=stop_bits, rx=rx, tx=tx))

    @mcp.tool(annotations=HW)
    def simpleserial_connect(version: Literal["1.0", "1.1", "2.1", "cdc"] = "2.1") -> Dict[str, Any]:
        """Connect the target with a SimpleSerial protocol version: 1.0 (no acks), 1.1, 2.1 (current firmware) or cdc (v2 over the scope's USB-CDC port, when the firmware has it). Then use simpleserial or serial_write."""
        return call("POST", "/api/interfaces/simpleserial/connect", {"version": version})

    @mcp.tool(annotations=HW)
    def spi_enable(speed: float = 1e6, cs: Literal["pdid", "pdic", "tio3", "tio4"] = "pdid") -> Dict[str, Any]:
        """Turn on the SPI master on the 20-pin header (SCK, MOSI, MISO; chip select on the chosen pin). Needs the TARGET_SPI firmware feature; not on the CW-Nano. nRST is held high while SPI is on. On the simulator a W25Q128-style SPI flash (JEDEC ID EF 40 18) answers."""
        return call("POST", "/api/interfaces/spi/enable", {"speed": speed, "cs": cs})

    @mcp.tool(annotations=HW)
    def spi_disable() -> Dict[str, Any]:
        """Turn off the SPI master and release its pins."""
        return call("POST", "/api/interfaces/spi/disable")

    @mcp.tool(annotations=HW)
    def spi_transfer(data: str, start: bool = True, stop: bool = True) -> Dict[str, Any]:
        """Send hex bytes on MOSI (e.g. '9f 00 00 00' reads a flash JEDEC ID) and return the bytes read on MISO. start/stop control chip select at the beginning and end, so a long transaction can be split."""
        return call("POST", "/api/interfaces/spi/transfer", {"data": data, "start": start, "stop": stop})

    @mcp.tool(annotations=RO)
    def gpio_read() -> Dict[str, Any]:
        """Drive mode and logic level of each target pin (TIO1-4, nRST, PDIC, PDID, and on the Husky MISO/MOSI/SCK). The CW-Nano can drive pins but not read them back."""
        return call("GET", "/api/interfaces/gpio")

    @mcp.tool(annotations=HW)
    def gpio_set(pin: Literal["tio1", "tio2", "tio3", "tio4", "nrst", "pdic", "pdid"], state: Literal["high", "low", "high_z"]) -> Dict[str, Any]:
        """Drive a target pin high or low, or release it (high_z). Pins the model cannot drive are refused with the reason."""
        return call("PUT", "/api/interfaces/gpio", {"pin": pin, "state": state})

    @mcp.tool(annotations=HW)
    def gpio_pulse(pin: Literal["nrst", "pdic"] = "nrst", ms: float = 50) -> Dict[str, Any]:
        """Pull a pin low for ms milliseconds and release it; on nRST this resets the target."""
        return call("POST", "/api/interfaces/gpio/pulse", {"pin": pin, "ms": ms})

    @mcp.tool(annotations=HW)
    def userio_set(direction: Optional[int] = None, drive: Optional[int] = None) -> Dict[str, Any]:
        """Husky USERIO header: direction is a bit mask (bit n = 1 drives Dn, bit 8 is CK where the library supports it) and drive the levels to drive. Returns the pins read back. Husky only."""
        return call("PUT", "/api/interfaces/userio", clean(direction=direction, drive=drive, mode="normal" if direction is not None or drive is not None else None))

    @mcp.tool(annotations=HW)
    def trigger_configure(kind: Literal["basic", "uart_decode", "uart_pattern", "edge_counter", "adc_level", "sequencer", "sad"] = "basic", pins: Optional[List[TriggerPin]] = None, op: Literal["OR", "AND", "NAND"] = "OR", edge: Literal["rising_edge", "falling_edge", "high", "low"] = "rising_edge",
                          pin: Optional[TriggerPin] = None, baud: Optional[float] = None, pattern: Optional[str] = None, rule: int = 0, data_bits: int = 8, stop_bits: int = 1, parity: Literal["none", "odd", "even"] = "none",
                          edges: int = 1, level: float = 0.1, window_start: int = 0, window_end: int = 0, threshold: int = 10, start: int = 0, enabled: bool = True) -> Dict[str, Any]:
        """Configure the capture trigger; captures use it right away. basic: pins combined with op, edge or level mode (CW-Nano: TIO4 rising edge only). uart_decode (Pro): UART byte pattern on pin at baud, pattern as quoted text "'r'" or hex '72 XX' (XX = any byte, up to 8). uart_pattern (Husky): pattern match rule (2 rules on Husky, 8 on Husky Plus) with data_bits/stop_bits/parity. edge_counter (Husky): trigger after edges edges on pin. adc_level (Husky): trigger when the signal crosses level (-0.5..0.5). sequencer (Husky): two pins in order within window_start..window_end ADC cycles, enabled=false turns it off. sad (Pro, Husky): sum of absolute differences against the newest trace from sample start, threshold."""
        body = {"kind": kind, "op": op, "edge": edge, "rule": rule, "data_bits": data_bits, "stop_bits": stop_bits, "parity": parity, "edges": edges, "level": level, "window_start": window_start, "window_end": window_end, "threshold": threshold, "start": start, "enabled": enabled}
        body.update(clean(pins=pins, pin=pin, baud=baud, pattern=pattern))
        return call("PUT", "/api/interfaces/trigger", body)

    @mcp.tool(annotations=HW)
    def bitbang(bits: str, record: Optional[str] = None, data_pin: BBPin = "USERIO_D0", clock_pin: BBPin = "USERIO_CK", clk_div: Optional[int] = None) -> Dict[str, Any]:
        """Husky bit-banger: send bits ('0' and '1') on data_pin with a clock on clock_pin ('disabled' for none); record has one 0/1 per bit, 1 = release the line and record it. clk_div (even, from the ADC clock) sets the time slot. Returns the recorded bits. Needs a Husky and a chipwhisperer library newer than 6.0.0."""
        return call("POST", "/api/interfaces/bitbang", clean(bits=bits, record=record, data_pin=data_pin, clock_pin=clock_pin, clk_div=clk_div))

    @mcp.tool(annotations=HW)
    def onewire(action: Literal["reset", "read_rom"] = "read_rom", data_pin: BBPin = "USERIO_D0", clk_div: Optional[int] = None) -> Dict[str, Any]:
        """1-Wire on the Husky bit-banger: reset (reset pulse and presence detect) or read_rom (command 0x33: family code, 48-bit serial, CRC check). The simulator has a DS18B20-style device (family 0x28)."""
        return call("POST", "/api/interfaces/onewire", clean(action=action, data_pin=data_pin, clk_div=clk_div))

    @mcp.tool(annotations=RO)
    def openocd_status(list_targets: bool = False, filter: Optional[str] = None) -> Dict[str, Any]:
        """OpenOCD state: whether it is installed (toolchain id 'openocd', install with toolchain_install), the server and its GDB/telnet/TCL ports, MPSSE mode, recent log lines; list_targets adds the target configs from OpenOCD's scripts folder (filter by text, e.g. 'stm32')."""
        st = client.get("/api/interfaces/openocd")
        if list_targets:
            t = client.get("/api/interfaces/openocd/targets")
            st["targets"] = [x for x in t if not filter or filter.lower() in x.lower()]
        return st

    @mcp.tool(annotations=HW)
    def openocd_mpsse(enable: bool = True, transport: Literal["jtag", "swd"] = "jtag", header: Literal["target", "userio"] = "target") -> Dict[str, Any]:
        """Switch the scope into MPSSE mode for JTAG or SWD (header 'userio' routes it to the Husky USERIO header) or back to normal mode (enable=false, which reconnects the scope). While MPSSE is on, Studio has no scope connection, USB-CDC serial and the native programmers are unavailable. Not on the simulator."""
        return call("POST", "/api/interfaces/openocd/mpsse", {"enable": enable, "transport": transport, "header": header}, timeout=120)

    @mcp.tool(annotations=HW)
    def openocd_start(target_cfg: str = "target/stm32f3x.cfg", transport: Optional[Literal["jtag", "swd"]] = None, gdb_port: int = 3333, telnet_port: int = 4444, tcl_port: int = 6666) -> Dict[str, Any]:
        """Start an OpenOCD server with the ChipWhisperer interface config and a target config from OpenOCD's scripts folder (see openocd_status list_targets). Needs MPSSE mode (openocd_mpsse) and real hardware. GDB connects to gdb_port."""
        return call("POST", "/api/interfaces/openocd/start", {"target_cfg": target_cfg, "transport": transport, "ports": {"gdb": gdb_port, "telnet": telnet_port, "tcl": tcl_port}})

    @mcp.tool(annotations=HW)
    def openocd_stop() -> Dict[str, Any]:
        """Stop the OpenOCD server."""
        return call("POST", "/api/interfaces/openocd/stop")

    @mcp.tool(annotations=HW)
    def openocd_command(command: str) -> Dict[str, Any]:
        """Run an OpenOCD command on the running server (e.g. 'targets', 'halt', 'reg', 'mdw 0x08000000 4', 'reset run') and return ok and its output."""
        return call("POST", "/api/interfaces/openocd/command", {"command": command}, timeout=60)

    @mcp.tool(annotations=DESTRUCTIVE)
    def openocd_program(path: str, verify: bool = True, reset: bool = True, address: Optional[str] = None) -> Dict[str, Any]:
        """Flash a .hex/.elf (or .bin with address, e.g. '0x08000000') on the Studio machine through OpenOCD's program command, then verify and reset."""
        return call("POST", "/api/interfaces/openocd/program", clean(path=path, verify=verify, reset=reset, address=address), timeout=600)
