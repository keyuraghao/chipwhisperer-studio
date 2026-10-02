"""Simulated peripherals behind the simulator's interfaces, so every protocol in the Interfaces tab can be demonstrated and tested without hardware.

* :class:`SimSPIFlash` is a Winbond W25Q128 style serial flash (JEDEC ID EF 40 18) on the SPI pins.
* :class:`SimPins` gives the pin levels the simulated scope would read back (TIO, PDIC, PDID, nRST, SPI pins, Husky USERIO).
* :class:`SimBitBanger` is a loopback bit-banger with a DS18B20 style 1-Wire device (family 0x28) on its data pin.

The objects are plain Python and know nothing about Studio; :mod:`cwstudio.interfaces` attaches them to a ``SimScope`` on first use.
"""
from __future__ import annotations

import time
from typing import Dict, List, Optional

JEDEC_ID = bytes([0xEF, 0x40, 0x18])
FLASH_SIZE = 16 * 1024 * 1024
UNIQUE_ID = bytes.fromhex("d26a3c2b1f0e4c57")
GREETING = b"ChipWhisperer Studio simulated SPI flash (W25Q128, JEDEC EF 40 18)\n"
ONEWIRE_ROM = bytes([0x28, 0xFF, 0x4C, 0x1A, 0x62, 0x16, 0x03])  # family code 0x28 (DS18B20) + 48-bit serial, CRC appended below


def crc8_maxim(data: bytes) -> int:
    """The Dallas/Maxim CRC-8 used by 1-Wire ROM codes."""
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8C if crc & 1 else crc >> 1
    return crc & 0xFF


class SimSPIFlash:
    """Byte-level model of a SPI NOR flash. ``transfer`` clocks bytes in and returns what MISO carried, honouring chip select framing like ``naeusb.spi.SPI.transfer``."""

    def __init__(self):
        self.mem: Dict[int, int] = {i: b for i, b in enumerate(GREETING)}
        self.selected = False
        self.cmd: List[int] = []
        self.wel = False
        self.busy_until = 0.0

    # --- helpers ---------------------------------------------------------
    def _read(self, addr: int) -> int:
        return self.mem.get(addr % FLASH_SIZE, 0xFF)

    def _status(self) -> int:
        busy = 1 if time.time() < self.busy_until else 0
        return busy | (0x02 if self.wel else 0)

    def _addr(self) -> int:
        c = self.cmd
        return (c[1] << 16) | (c[2] << 8) | c[3]

    def select(self):
        self.selected = True
        self.cmd = []

    def deselect(self):
        """Chip select released: commit page programs and erases that were sent with write enable."""
        if self.selected and self.cmd:
            op = self.cmd[0]
            if op == 0x06:
                self.wel = True
            elif op == 0x04:
                self.wel = False
            elif self.wel and op == 0x02 and len(self.cmd) > 4:
                base = self._addr()
                page, off = base & ~0xFF, base & 0xFF
                for i, b in enumerate(self.cmd[4:]):
                    a = page | ((off + i) & 0xFF)
                    self.mem[a] = self._read(a) & b
                self.wel, self.busy_until = False, time.time() + 0.001
            elif self.wel and op in (0x20, 0x52, 0xD8) and len(self.cmd) >= 4:
                size = {0x20: 4096, 0x52: 32768, 0xD8: 65536}[op]
                base = self._addr() & ~(size - 1)
                for a in [a for a in self.mem if base <= a < base + size]:
                    del self.mem[a]
                self.wel, self.busy_until = False, time.time() + 0.01
            elif self.wel and op in (0xC7, 0x60):
                self.mem.clear()
                self.wel, self.busy_until = False, time.time() + 0.05
        self.selected = False
        self.cmd = []

    def _out(self, pos: int) -> int:
        """MISO for byte number ``pos`` of the current transaction (bytes 0..pos-1 are already in ``self.cmd``)."""
        if pos == 0:
            return 0xFF
        op = self.cmd[0]
        if op == 0x9F:
            return JEDEC_ID[(pos - 1) % 3]
        if op in (0x05, 0x35, 0x15):
            return self._status() if op == 0x05 else 0x00
        if op == 0xAB and pos >= 4:
            return 0x17
        if op == 0x90 and pos >= 4:
            return (0xEF, 0x17)[(pos - 4) % 2]
        if op == 0x03 and pos >= 4:
            return self._read(self._addr() + pos - 4)
        if op == 0x0B and pos >= 5:
            return self._read(self._addr() + pos - 5)
        if op == 0x4B and pos >= 5:
            return UNIQUE_ID[(pos - 5) % 8]
        return 0xFF

    def transfer(self, data, start: bool = True, stop: bool = True) -> List[int]:
        if start or not self.selected:
            self.select()
        out = []
        for b in bytes(data):
            out.append(self._out(len(self.cmd)))
            self.cmd.append(b & 0xFF)
        if stop:
            self.deselect()
        return out


class SimPins:
    """Pin levels the simulated scope reads back, derived from the simulated IO settings."""

    EXTERNAL = {"tio1": 1, "tio2": 1, "tio3": 1, "tio4": 0, "pdic": 1, "pdid": 1, "nrst": 1, "miso": 1, "mosi": 0, "sck": 0}

    def __init__(self, io):
        self.io = io
        self.userio_direction = 0
        self.userio_drive = 0
        self.userio_mode = "normal"
        self.t0 = time.time()

    def level(self, pin: str) -> int:
        mode = getattr(self.io, pin, None) if pin in ("tio1", "tio2", "tio3", "tio4", "pdic", "pdid", "nrst") else None
        if mode in ("gpio_high", "high", True, "serial_tx"):
            return 1
        if mode in ("gpio_low", "low", False):
            return 0
        return self.EXTERNAL.get(pin, 0)

    def tio_states(self):
        return tuple(self.level(p) for p in ("tio1", "tio2", "tio3", "tio4"))

    def userio_status(self) -> int:
        """Inputs show a slow counter on D0-D7 (so the live view moves); driven pins read back what is driven."""
        inputs = int((time.time() - self.t0) * 2) & 0xFF
        d = self.userio_direction
        return (self.userio_drive & d) | (inputs & ~d & 0x1FF)


class SimBitBanger:
    """A loopback bit-banger: driven bits come back as recorded, released (high-z) bits read the 1-Wire device or a 0xA5 pattern."""

    def __init__(self):
        self.data_pin = "USERIO_D0"
        self.clock_pin = "USERIO_CK"
        self.clk_div = 296
        self.rom = ONEWIRE_ROM + bytes([crc8_maxim(ONEWIRE_ROM)])
        self.presence = True

    def send(self, bits: List[int], hiz: List[int], record: List[int]) -> List[int]:
        out = []
        pattern = [(0xA5 >> (7 - i % 8)) & 1 for i in range(len(bits))]
        for i, b in enumerate(bits):
            if record[i]:
                out.append(pattern[i] if hiz[i] else b)
        return out

    def onewire_reset(self) -> bool:
        return self.presence

    def onewire_read_rom(self, command: int = 0x33) -> Optional[bytes]:
        if command != 0x33 or not self.presence:
            return None
        return self.rom


def attach(scope):
    """Give a ``SimScope`` its simulated peripherals (idempotent) and return them as a dict."""
    devs = getattr(scope, "_sim_devices", None)
    if devs is None:
        devs = {"flash": SimSPIFlash(), "pins": SimPins(scope.io), "bitbanger": SimBitBanger(), "spi_enabled": False, "spi_speed": 0}
        scope._sim_devices = devs
    return devs
