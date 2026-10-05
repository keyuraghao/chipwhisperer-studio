"""Connect flow without hardware: a stuck USB call, scope reads only on the hardware thread, device listing problems, wrong device kind, failure logging, outdated firmware, unplug and stale state."""
import tempfile
import threading
import time

import pytest
from starlette.testclient import TestClient

from cwstudio import hardware
from cwstudio import worker as wmod
from cwstudio.app import create_app
from cwstudio.session import Session
from cwstudio.worker import HardwareBusy, HardwareTimeout, HardwareWorker


@pytest.fixture
def studio():
    s = Session(simulate=False, data_dir=tempfile.mkdtemp())
    with TestClient(create_app(s), raise_server_exceptions=False) as c:
        c.session = s
        yield c


class FakeNAE:
    def __init__(self, up2date=None):
        if up2date is not None:
            self.fw_up2date = up2date

    def hw_location(self):
        return (3, 9)


class FakeScope:
    """Records which thread reads each attribute that is a USB access on a real scope."""

    _is_husky = False

    def __init__(self, sn="LITE01", up2date=None, adc=True, setup_error=None):
        self.reads = []
        self.alive = True
        self.dis_called = 0
        self._nae = FakeNAE(up2date)
        self._sn = sn
        self.setup_error = setup_error
        if adc:
            self.adc = object()
        else:
            self.adc = None

    def _rec(self, what):
        self.reads.append((what, threading.current_thread().name))

    def _getNAEUSB(self):
        return self._nae

    def _getCWType(self):
        self._rec("_getCWType")
        return "cwlite"

    def get_name(self):
        self._rec("get_name")
        return "ChipWhisperer Lite"

    @property
    def sn(self):
        self._rec("sn")
        return self._sn

    @property
    def fw_version(self):
        self._rec("fw_version")
        if not self.alive:
            import usb1
            raise usb1.USBErrorNoDevice()
        return {"major": 0, "minor": 50, "debug": 0}

    def default_setup(self):
        if self.setup_error:
            raise RuntimeError(self.setup_error)

    def dis(self):
        self.dis_called += 1


def _cap_timeouts(monkeypatch, cap):
    orig = wmod.HardwareWorker.call

    def call(self, fn, *a, timeout=120.0, **k):
        return orig(self, fn, *a, timeout=min(timeout or 1e9, cap), **k)
    monkeypatch.setattr(wmod.HardwareWorker, "call", call)


# --- 2: a stuck hardware call ------------------------------------------------------------
def test_worker_stuck_call_fails_fast_and_recovers():
    w = HardwareWorker()
    w.start()
    try:
        release = threading.Event()
        with pytest.raises(HardwareTimeout) as ei:
            w.call(lambda: release.wait(10), timeout=0.3, job_name="opening the scope")
        assert "did not respond within 0.3 s (opening the scope)" in str(ei.value) and "restart Studio" in str(ei.value)
        assert w.stuck()["name"] == "opening the scope"
        t = time.monotonic()
        ran = []
        with pytest.raises(HardwareBusy) as ei:
            w.call(lambda: ran.append(1), timeout=5, job_name="listing")
        assert time.monotonic() - t < 0.5 and not ran  # fails at once instead of queueing behind the stuck call
        assert "busy with opening the scope since" in str(ei.value)
        release.set()
        end = time.time() + 5
        while w.stuck() is not None and time.time() < end:
            time.sleep(0.01)
        assert w.stuck() is None
        assert w.call(lambda: 42, timeout=5) == 42  # the thread recovers once the stuck call returns
    finally:
        w.stop()


def test_worker_queued_call_is_not_run_after_its_caller_gave_up():
    w = HardwareWorker()
    w.start()
    try:
        release = threading.Event()
        fut = w.submit(lambda: release.wait(10))
        ran = []
        with pytest.raises(HardwareBusy) as ei:
            w.call(lambda: ran.append(1), timeout=0.3, job_name="reading settings")
        assert "reading settings waited 0.3 s and was not run" in str(ei.value)
        assert w.stuck() is None  # a long job a caller did not give up on is busy, not stuck
        release.set()
        fut.result(5)
        assert w.call(lambda: 1) == 1 and not ran
        def boom():
            raise TimeoutError("own timeout")
        with pytest.raises(TimeoutError, match="own timeout"):  # a TimeoutError raised by the job itself is passed through, not taken for a hang
            w.call(boom, timeout=5)
    finally:
        w.stop()


def test_stuck_connect_gives_clear_errors_and_a_late_scope_is_released(studio, monkeypatch):
    _cap_timeouts(monkeypatch, 0.5)
    release = threading.Event()
    late = FakeScope()

    orig = hardware.connect_scope

    def slow(kind="auto", sn=None, **kw):
        if kind == "sim":
            return orig(kind, sn=sn, **kw)
        release.wait(10)
        return late
    monkeypatch.setattr(hardware, "connect_scope", slow)
    monkeypatch.setattr(hardware, "list_devices", lambda connected=None: [{"name": "ChipWhisperer-Lite", "sn": "LITE01", "hw_loc": [1, 2], "kind": "lite"}])
    r = studio.post("/api/scope/connect", json={"kind": "lite"})
    assert r.status_code == 504, r.text
    assert "did not respond within" in r.json()["detail"] and "unplug it, plug it back in and restart Studio" in r.json()["detail"]
    st = studio.get("/api/status").json()
    assert st["scope"]["connected"] is False and st["scope_kind"] is None and st["hardware_stuck"]["name"] == "connecting to the ChipWhisperer-Lite"
    d = studio.get("/api/devices")
    assert d.status_code == 200 and d.json()[0]["sn"] == "LITE01"  # listing does not wait for the stuck hardware thread
    for path, body in (("/api/scope/disconnect", None), ("/api/target/disconnect", None), ("/api/scope/connect", {"kind": "sim"})):
        t = time.monotonic()
        r = studio.post(path, json=body or {})
        assert r.status_code == 503, (path, r.text)
        assert "busy with connecting to the ChipWhisperer-Lite" in r.json()["detail"] and time.monotonic() - t < 0.4
    release.set()  # the stuck cw.scope() returns after Studio told the user it failed
    end = time.time() + 5
    while late.dis_called == 0 and time.time() < end:
        time.sleep(0.05)
    assert late.dis_called == 1
    st = studio.get("/api/status").json()
    assert st["scope"]["connected"] is False and st["hardware_stuck"] is None
    assert any("answered after Studio had given up" in (e.get("msg") or "") for e in studio.get("/api/logs").json())
    r = studio.post("/api/scope/connect", json={"kind": "sim"})
    assert r.status_code == 200, r.text


# --- 3: scope info is read on the hardware thread only -------------------------------------
def test_status_and_hello_never_read_the_scope_off_the_hardware_thread(studio, monkeypatch):
    sc = FakeScope()
    monkeypatch.setattr(hardware, "connect_scope", lambda kind="auto", sn=None, **kw: sc)
    r = studio.post("/api/scope/connect", json={"kind": "lite"})
    assert r.status_code == 200 and r.json()["name"] == "ChipWhisperer Lite" and r.json()["hw_loc"] == [3, 9]
    for _ in range(3):
        st = studio.get("/api/status").json()
        assert st["scope"]["sn"] == "LITE01" and st["scope"]["fw_version"]["minor"] == 50
    with studio.websocket_connect("/ws") as ws:
        assert '"LITE01"' in ws.receive_text()
    studio.session._push_status()
    time.sleep(2.3)  # the periodic status push and liveness check run meanwhile
    assert sc.reads and {t for _, t in sc.reads} == {"cw-hardware"}, sc.reads


# --- 4b: device listing problems -------------------------------------------------------------
class FakeDev:
    def __init__(self, vid, pid, sn=None, err=None, loc=(1, 5)):
        self.vid, self.pid, self.sn, self.err, self.loc = vid, pid, sn, err, loc

    def getVendorID(self):
        return self.vid

    def getProductID(self):
        return self.pid

    def getSerialNumber(self):
        if self.err:
            raise self.err
        return self.sn

    def getBusNumber(self):
        return self.loc[0]

    def getDeviceAddress(self):
        return self.loc[1]


@pytest.fixture
def fake_usb(monkeypatch):
    import usb1
    devs = []
    monkeypatch.setattr(usb1.USBContext, "getDeviceIterator", lambda self, skip_on_error=False: iter(devs))
    return devs


def test_list_devices_reports_each_problem(fake_usb, monkeypatch):
    import usb1
    monkeypatch.setattr(hardware.platform, "system", lambda: "Linux")
    fake_usb[:] = [FakeDev(0x2B3E, 0xACE2, "LITE01"), FakeDev(0x2B3E, 0xACE0, err=usb1.USBErrorAccess(), loc=(1, 6)),
                   FakeDev(0x2B3E, 0xACE5, err=usb1.USBErrorBusy(), loc=(1, 7)), FakeDev(0x03EB, 0x6124, loc=(1, 8)), FakeDev(0x046D, 0xC077)]
    out = hardware.list_devices()
    by = {d["name"]: d for d in out}
    assert by["ChipWhisperer-Lite"] == {"name": "ChipWhisperer-Lite", "sn": "LITE01", "hw_loc": [1, 5], "kind": "lite"}
    nano = by["ChipWhisperer-Nano"]
    assert nano["error"] and nano["problem"] == "access" and "udev rule" in nano["hint"] and "udevadm" in nano["command"]
    assert by["ChipWhisperer-Husky"]["problem"] == "busy" and "another program" in by["ChipWhisperer-Husky"]["hint"]
    boot = by["ChipWhisperer in bootloader mode"]
    assert boot["bootloader"] and "program_sam_firmware" in boot["hint"] and "firmware.html" in boot["hint"]
    assert len(out) == 4  # other vendors are left out
    monkeypatch.setattr(hardware.platform, "system", lambda: "Windows")
    fake_usb[:] = [FakeDev(0x2B3E, 0xACE2, err=usb1.USBErrorNotSupported())]
    d = hardware.list_devices()[0]
    assert d["problem"] == "driver" and "libusb-win32" in d["hint"] and d["url"] == hardware.DRIVERS_URL
    fake_usb[:] = [FakeDev(0x2B3E, 0xACE2, err=usb1.USBErrorAccess())]
    assert "another program has it open" in hardware.list_devices()[0]["hint"]


def test_list_devices_reports_the_open_scope_without_opening_it(fake_usb):
    import usb1
    fake_usb[:] = [FakeDev(0x2B3E, 0xACE2, err=AssertionError("opened the connected scope"), loc=(3, 9)), FakeDev(0x2B3E, 0xACE0, "NANO01", loc=(3, 10))]
    out = hardware.list_devices({"connected": True, "sn": "LITE01", "hw_loc": [3, 9], "name": "ChipWhisperer Lite"})
    assert out[0]["sn"] == "LITE01" and out[0]["in_use"] and not out[0].get("error")
    assert out[1]["sn"] == "NANO01" and not out[1].get("in_use")
    # location unknown: the same model that Windows refuses to open a second time is the connected one
    fake_usb[:] = [FakeDev(0x2B3E, 0xACE2, err=usb1.USBErrorAccess(), loc=(3, 9))]
    out = hardware.list_devices({"connected": True, "sn": "LITE01", "name": "ChipWhisperer Lite"})
    assert out[0]["sn"] == "LITE01" and out[0]["in_use"] and not out[0].get("error")


def test_libusb_missing_names_the_cause_and_fits_the_install(fake_usb, monkeypatch):
    import usb1

    def bad_open(self):
        raise OSError("[WinError 126] The specified module could not be found")
    monkeypatch.setattr(usb1.USBContext, "open", bad_open)
    monkeypatch.setattr(hardware.platform, "system", lambda: "Windows")
    d = hardware.list_devices()[0]
    assert d["error"] and d["problem"] == "libusb" and "WinError 126" in d["hint"] and "pip install" in d["hint"]
    monkeypatch.setattr(hardware.sys, "frozen", True, raising=False)
    d = hardware.list_devices()[0]
    assert "WinError 126" in d["hint"] and "pip" not in d["hint"] and "reinstall Studio" in d["hint"]
    # the same from connect: chipwhisperer's "Try pip install libusb1" text is replaced by the cause
    import chipwhisperer as cw

    def cw_scope(**kw):
        try:
            raise OSError("libusb-1.0.dll not found")
        except OSError as e:
            raise OSError("Could not import libusb dll. Try \npip uninstall libusb1\npip install libusb1") from e
    monkeypatch.setattr(cw, "scope", cw_scope)
    with pytest.raises(hardware.ScopeConnectError) as ei:
        hardware.connect_scope("auto")
    assert "libusb-1.0.dll not found" in str(ei.value) and "pip" not in str(ei.value)


def test_platform_help_driver_and_libusb_notes(monkeypatch):
    monkeypatch.setattr(hardware.platform, "system", lambda: "Windows")
    ph = hardware.platform_help()
    assert ph["driver_url"] == "https://chipwhisperer.readthedocs.io/en/latest/drivers.html"
    assert "WinUSB" in ph["note"] and "libusb-win32" in ph["note"] and "Delete the driver software" in ph["note"] and "NewAE driver package" in ph["note"]
    assert ph["empty_hints"] and "Device Manager" in ph["empty_hints"][-1]
    monkeypatch.setattr(hardware.platform, "system", lambda: "Darwin")
    assert "brew install libusb" in hardware.platform_help()["note"]
    monkeypatch.setattr(hardware.sys, "frozen", True, raising=False)
    assert "brew" not in hardware.platform_help()["note"]


# --- 5: wrong kind selected ---------------------------------------------------------------------
def test_wrong_kind_lists_what_is_attached(studio, monkeypatch):
    import chipwhisperer as cw

    def not_found(**kw):
        raise OSError("Could not find ChipWhisperer. Is it connected?")
    monkeypatch.setattr(cw, "scope", not_found)
    monkeypatch.setattr(hardware, "list_devices", lambda connected=None: [{"name": "ChipWhisperer-Lite", "sn": "LITE01", "hw_loc": [1, 2], "kind": "lite"}])
    r = studio.post("/api/scope/connect", json={"kind": "nano"})
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "Could not find ChipWhisperer" in detail and "No ChipWhisperer-Nano found; attached: ChipWhisperer-Lite (sn LITE01). Choose Auto-detect or ChipWhisperer-Lite." in detail
    monkeypatch.setattr(hardware, "list_devices", lambda connected=None: [])
    detail = studio.post("/api/scope/connect", json={"kind": "auto"}).json()["detail"]
    assert "No NewAE USB device is attached" in detail and "data-capable" in detail


# --- 6: failures reach the log at WARNING --------------------------------------------------------
def test_connect_target_and_program_failures_are_logged(studio, monkeypatch, tmp_path):
    import chipwhisperer as cw

    def boom(**kw):
        raise OSError("Could not find ChipWhisperer. Is it connected?")
    monkeypatch.setattr(cw, "scope", boom)
    monkeypatch.setattr(hardware, "list_devices", lambda connected=None: [])
    assert studio.post("/api/scope/connect", json={"kind": "lite"}).status_code == 400
    assert studio.post("/api/target/connect", json={"kind": "SimpleSerial2"}).status_code == 400
    assert studio.post("/api/scope/connect", json={"kind": "sim"}).status_code == 200
    fw = tmp_path / "fw.hex"
    fw.write_text(":00000001FF\n")
    monkeypatch.setattr(hardware, "program_target", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no ack from the bootloader")))
    assert studio.post("/api/target/program", json={"programmer": "STM32F", "path": str(fw)}).status_code == 400
    logs = [(e["level"], e["msg"]) for e in studio.get("/api/logs").json() if e.get("type") == "log"]
    assert any(lv == "WARNING" and "Scope connect failed" in m and "Could not find ChipWhisperer" in m for lv, m in logs), logs
    assert any(lv == "WARNING" and "Target connect failed" in m and "connect a scope first" in m for lv, m in logs), logs
    assert any(lv == "WARNING" and "Programming failed" in m and "no ack" in m for lv, m in logs), logs


# --- 7: outdated firmware and default_setup errors ------------------------------------------------
def test_outdated_firmware_fails_the_connect(studio, monkeypatch):
    half = FakeScope(up2date=False, adc=False)
    monkeypatch.setattr(hardware, "connect_scope", lambda kind="auto", sn=None, **kw: half)
    r = studio.post("/api/scope/connect", json={"kind": "lite"})
    assert r.status_code == 400
    d = r.json()["detail"]
    assert "firmware 0.50.0 is older than this ChipWhisperer library needs; upgrade it" in d and "upgrade_firmware()" in d and "firmware.html" in d
    assert half.dis_called == 1
    st = studio.get("/api/status").json()
    assert st["scope"]["connected"] is False and st["scope_kind"] is None
    # chipwhisperer 6.0.0 has no fw_up2date and always sets adc: not flagged
    ok = FakeScope(adc=True, setup_error="adc clock not locked")
    monkeypatch.setattr(hardware, "connect_scope", lambda kind="auto", sn=None, **kw: ok)
    r = studio.post("/api/scope/connect", json={"kind": "lite"})
    assert r.status_code == 200, r.text
    assert r.json()["warnings"] == ["default_setup() failed: RuntimeError: adc clock not locked"]
    assert hardware.firmware_outdated(FakeScope(up2date=True)) is None


# --- 8: unplug ----------------------------------------------------------------------------------------
def test_unplugged_scope_is_marked_lost(studio, monkeypatch):
    sc = FakeScope()
    monkeypatch.setattr(hardware, "connect_scope", lambda kind="auto", sn=None, **kw: sc)
    assert studio.post("/api/scope/connect", json={"kind": "lite"}).status_code == 200
    sc.alive = False
    end = time.time() + 8
    st = None
    while time.time() < end:
        st = studio.get("/api/status").json()
        if not st["scope"]["connected"]:
            break
        time.sleep(0.1)
    assert st["scope"]["connected"] is False and "unplugged" in st["scope"]["lost"] and st["scope_kind"] is None
    assert studio.session.scope is None
    assert hardware.is_device_lost(OSError("[Errno 19] No such device"))
    assert not hardware.is_device_lost(OSError("Could not find ChipWhisperer"))


# --- 9: no stale scope_kind ---------------------------------------------------------------------------
def test_failed_connect_resets_scope_kind(studio, monkeypatch):
    assert studio.post("/api/scope/connect", json={"kind": "sim"}).status_code == 200
    assert studio.post("/api/target/connect", json={"kind": "sim"}).status_code == 200
    import chipwhisperer as cw
    monkeypatch.setattr(cw, "scope", lambda **kw: (_ for _ in ()).throw(OSError("Could not find ChipWhisperer. Is it connected?")))
    monkeypatch.setattr(hardware, "list_devices", lambda connected=None: [])
    assert studio.post("/api/scope/connect", json={"kind": "husky"}).status_code == 400
    st = studio.get("/api/status").json()
    assert st["scope"]["connected"] is False and st["scope_kind"] is None and st["target_kind"] is None and not st["target"]["connected"]
