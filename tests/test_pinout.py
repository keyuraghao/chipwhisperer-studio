"""Quick Info pin diagrams: every model has a complete 20-pin table, the Husky models add USERIO, the API follows the scope Studio has open and says so when none is."""
import tempfile

import pytest
from starlette.testclient import TestClient

from cwstudio import pinout
from cwstudio.app import create_app
from cwstudio.capabilities import LABELS, SIM_MODELS
from cwstudio.session import Session


@pytest.mark.parametrize("model", SIM_MODELS)
def test_every_model_has_a_full_target_header(model):
    po = pinout.pinout_for_model(model)
    assert po["model"] == model and po["label"] == LABELS[model]
    hdr = po["connectors"][0]
    assert hdr["name"] == "20-pin target header"
    pins = hdr["pins"]
    assert [p["pin"] for p in pins] == list(range(1, 21))
    for p in pins:
        assert p["name"] and p["function"] and p["role"] in ("power", "ground", "io", "spi", "clock", "reset", "nc")
    by = {p["pin"]: p for p in pins}
    # the fixed electrical pinout NewAE documents for every model
    assert by[2]["name"] == by[17]["name"] == by[19]["name"] == "GND"
    assert by[3]["name"] == by[18]["name"] == "+3.3V" and by[3]["alias"] == "target_pwr"
    assert by[4]["alias"] == "hs1" and by[6]["alias"] == "hs2" and by[5]["alias"] == "nrst"
    assert [by[n]["alias"] for n in (10, 12, 14, 16)] == ["tio1", "tio2", "tio3", "tio4"]
    assert by[13]["alias"] == "pdic" and by[15]["alias"] == "pdid"
    assert po["ports"] and any(p["name"].startswith("MEASURE") for p in po["ports"])


def test_model_differences():
    nano, lite, pro, husky, plus = (pinout.pinout_for_model(m) for m in ("nano", "lite", "pro", "husky", "huskyplus"))
    pins = lambda po: {p["pin"]: p for p in po["connectors"][0]["pins"]}  # noqa: E731
    # the Nano has no SPI lines; the others do
    assert all(pins(nano)[n]["role"] == "nc" for n in (7, 9, 11))
    assert all(pins(po)[n]["role"] == "spi" for po in (lite, pro, husky, plus) for n in (7, 9, 11))
    # +VUSB is wired on the Pro only
    assert pins(pro)[1]["role"] == "power" and pins(pro)[20]["role"] == "power"
    assert all(pins(po)[1]["role"] == "nc" for po in (nano, lite, husky, plus))
    # pin 8: VTarget on the Nano and Lite, unconnected on the Pro and Husky
    assert pins(nano)[8]["name"] == pins(lite)[8]["name"] == "VTarget"
    assert pins(pro)[8]["role"] == pins(husky)[8]["role"] == "nc"
    # the USERIO header exists on the Husky models only
    assert [c["name"] for c in husky["connectors"]] == ["20-pin target header", "USERIO 20-pin header"]
    assert [c["name"] for c in plus["connectors"]] == ["20-pin target header", "USERIO 20-pin header"]
    assert all(len(po["connectors"]) == 1 for po in (nano, lite, pro))
    userio = {p["pin"]: p for p in husky["connectors"][1]["pins"]}
    assert [p for p in userio] == list(range(1, 21))
    assert userio[3]["alias"] == "userio_ck" and userio[5]["alias"] == "userio_d7" and userio[19]["alias"] == "userio_d0"
    assert all(userio[n]["name"] == "GND" for n in range(4, 21, 2))
    assert any("bit-banger" in n for n in plus["notes"]) and not any("bit-banger" in n for n in husky["notes"])


def test_unknown_model_and_no_scope():
    assert pinout.pinout_for_model("cw305")["model"] is None
    po = pinout.pinout(None)
    assert po == {"connected": False, "message": "No ChipWhisperer connected", "model": None, "label": None, "connectors": [], "ports": [], "notes": []}
    assert pinout.pinout({"connected": False})["connected"] is False
    other = pinout.pinout({"connected": True, "type": "cw305", "name": "CW305"})
    assert other["connected"] and other["model"] is None and "CW305" in other["message"]
    # a real scope reports its type string; the diagram is the model's
    husky = pinout.pinout({"connected": True, "type": "cwhusky", "name": "ChipWhisperer-Husky", "sn": "50203120", "hw_loc": [1, 7]})
    assert husky["model"] == "husky" and husky["device"] == {"name": "ChipWhisperer-Husky", "sn": "50203120", "simulated": False, "hw_loc": [1, 7]}


def test_api_follows_the_open_scope():
    s = Session(simulate=True, data_dir=tempfile.mkdtemp())
    with TestClient(create_app(s)) as c:
        po = c.get("/api/pinout").json()
        assert po["connected"] is False and po["message"] == "No ChipWhisperer connected"
        c.post("/api/scope/connect", json={"kind": "sim", "sim_model": "lite"}).raise_for_status()
        po = c.get("/api/pinout").json()
        assert po["connected"] and po["model"] == "lite" and po["device"]["simulated"] and len(po["connectors"]) == 1
        # the active device changes: the diagram follows it
        c.post("/api/scope/connect", json={"kind": "sim", "sim_model": "huskyplus"}).raise_for_status()
        po = c.get("/api/pinout").json()
        assert po["model"] == "huskyplus" and len(po["connectors"]) == 2
        c.post("/api/scope/disconnect").raise_for_status()
        assert c.get("/api/pinout").json()["connected"] is False
