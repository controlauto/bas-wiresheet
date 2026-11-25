"""Read and edit T3000 input, output and variable configuration (description, label, auto/manual, units).

Layouts and commands from T3000 (CM5/ud_str.h): READOUTPUT_T3000 = 1, READINPUT_T3000 = 2, READVARIABLE_T3000 = 3,
writes are 100 + read. Str_out_point 45 bytes, Str_in_point 46 bytes, Str_variable_point 39 bytes, packed.
An edit reads the point, changes only the requested fields, writes it back and reads it again to verify.
Outputs and variables can also be controlled directly (no Home Assistant entity needed): auto/manual plus the value.
T3000 stores the value as engineering value x 1000 (digital: 0 = OFF, 1000 = ON; verified on a T3-LB).
In auto the controller program owns the value, so a value is verified only when the point is left in manual.
"""
from __future__ import annotations

import struct
from typing import Any

from .t3000_bacnet import PRIVATE_LEN, BacnetError, UploadRefused, header, read_range

KINDS = {
    # category: (read command, size, max index, description length)
    "OUT": (1, 45, 64, 19),
    "IN": (2, 46, 64, 21),
    "VAR": (3, 39, 128, 21),
}
LABEL_LEN = 9

VARIABLE_ANALOG_UNITS = ["Unused", "°C", "°F", "FPM", "Pa", "KPa", "PSI", "inWC", "W", "kW", "kWH", "V", "kV", "A", "mA", "CFM", "s", "min",
                         "h", "d", "Time", "Ω", "%", "%RH", "PPM", "Counts", "%Open", "kg", "L/h", "GPH", "GAL", "CF", "BTU", "m³/h"]
DIGITAL_UNITS = ["Unused", "Off/On", "Close/Open", "Stop/Start", "Disable/Enable", "Normal/Alarm", "Normal/High", "Normal/Low", "No/Yes",
                 "Cool/Heat", "Unoccupy/Occupy", "Low/High", "On/Off", "Open/Close", "Start/Stop", "Enable/Disable", "Alarm/Normal",
                 "High/Normal", "Low/Normal", "Yes/No", "Heat/Cool", "Occupy/Unoccupy", "High/Low"]

# field offsets inside each struct (after description and label)
OFFSETS = {
    "OUT": {"description": (0, 19), "label": (21, 9), "value": 30, "auto_manual": 34, "digital_analog": 35, "range": 40, "decom": 39},
    "IN": {"description": (0, 21), "label": (21, 9), "value": 30, "decom": 35, "auto_manual": 39, "digital_analog": 40, "range": 45},
    "VAR": {"description": (0, 21), "label": (21, 9), "value": 30, "auto_manual": 34, "digital_analog": 35, "range": 38},
}
EDITABLE = {"description", "label", "auto_manual", "digital_analog", "range", "value"}
VALUE_KINDS = {"OUT", "VAR"}


def _text(raw: bytes) -> str:
    return raw.split(b"\0")[0].decode("latin-1")


def decode(kind: str, raw: bytes) -> dict[str, Any]:
    o = OFFSETS[kind]
    d0, dl = o["description"]
    l0, ll = o["label"]
    out = {"description": _text(raw[d0:d0 + dl]), "label": _text(raw[l0:l0 + ll]),
           "value_raw": struct.unpack_from("<i", raw, o["value"])[0], "auto_manual": "manual" if raw[o["auto_manual"]] else "auto",
           "digital_analog": "analog" if raw[o["digital_analog"]] else "digital", "range": raw[o["range"]]}
    out["value"] = out["value_raw"] / 1000.0
    if kind == "VAR":
        names = VARIABLE_ANALOG_UNITS if out["digital_analog"] == "analog" else DIGITAL_UNITS
        out["range_name"] = names[out["range"]] if out["range"] < len(names) else f"custom {out['range']}"
    return out


def _put_text(buf: bytearray, start: int, length: int, text: str, field: str) -> None:
    data = text.encode("latin-1", "strict")
    if len(data) > length - 1:
        raise UploadRefused(f"{field} is limited to {length - 1} characters")
    if any(c < 32 for c in data):
        raise UploadRefused(f"{field} contains control characters")
    buf[start:start + length] = data.ljust(length, b"\0")


def encode_changes(kind: str, raw: bytes, changes: dict[str, Any]) -> bytes:
    unknown = set(changes) - EDITABLE
    if unknown:
        raise UploadRefused(f"Not editable: {', '.join(sorted(unknown))}")
    o, buf = OFFSETS[kind], bytearray(raw)
    if "description" in changes:
        _put_text(buf, *o["description"], str(changes["description"]), "Description")
    if "label" in changes:
        if not str(changes["label"]).strip():
            raise UploadRefused("Label cannot be empty")
        _put_text(buf, *o["label"], str(changes["label"]).upper(), "Label")
    if "auto_manual" in changes:
        if changes["auto_manual"] not in ("auto", "manual"):
            raise UploadRefused("auto_manual must be auto or manual")
        buf[o["auto_manual"]] = 1 if changes["auto_manual"] == "manual" else 0
    if "digital_analog" in changes:
        if changes["digital_analog"] not in ("digital", "analog"):
            raise UploadRefused("digital_analog must be digital or analog")
        buf[o["digital_analog"]] = 1 if changes["digital_analog"] == "analog" else 0
    if "range" in changes:
        r = int(changes["range"])
        if not 0 <= r <= 255:
            raise UploadRefused("range must be 0-255")
        buf[o["range"]] = r
    if "value" in changes:
        if kind not in VALUE_KINDS:
            raise UploadRefused("Inputs are measured by the controller; only outputs and variables take a value")
        try:
            v = float(changes["value"])
        except (TypeError, ValueError):
            raise UploadRefused("value must be a number") from None
        digital = not buf[o["digital_analog"]]
        if digital:
            if v not in (0, 1):
                raise UploadRefused("Digital point: value must be 1 (ON) or 0 (OFF)")
            raw_v = 1000 if v else 0
        else:
            if not -2000000 <= v <= 2000000:
                raise UploadRefused("value out of range")
            raw_v = int(round(v * 1000))
        struct.pack_into("<i", buf, o["value"], raw_v)
    return bytes(buf)


def read_struct(client, kind: str, index: int) -> bytes:
    cmd, size, top, _ = KINDS[kind]
    if not 1 <= index <= top:
        raise UploadRefused(f"{kind}{index} does not exist (1-{top})")
    i = index - 1
    data = client._private(header(PRIVATE_LEN, cmd, i, size))
    if len(data) != PRIVATE_LEN + size or data[2] != cmd or data[3] != i:
        raise BacnetError(f"Unexpected reply reading {kind}{index}")
    return data[PRIVATE_LEN:]


def read_all(client, kind: str) -> list[bytes]:
    """Every record of a kind, 10 per request (450-460 bytes, like T3000)."""
    cmd, size, top, _ = KINDS[kind]
    return read_range(client, cmd, 0, top, size, 10)


def write_struct(client, kind: str, index: int, raw: bytes) -> None:
    cmd, size, _, _ = KINDS[kind]
    if len(raw) != size:
        raise ValueError("Bad point size")
    client._private_write(header(PRIVATE_LEN + size, cmd + 100, index - 1, size) + raw)


def edit_point(client, expected_device: int, kind: str, index: int, changes: dict[str, Any]) -> dict:
    """Read, change only the given fields, write, read back and verify. Returns before/after."""
    if client.identify() != expected_device:
        raise UploadRefused("Wrong controller at this address")
    before = read_struct(client, kind, index)
    target = encode_changes(kind, before, changes)
    if target == before:
        return {"ok": True, "changed": False, "before": decode(kind, before), "after": decode(kind, before)}
    write_struct(client, kind, index, target)
    after = read_struct(client, kind, index)
    want, got = decode(kind, target), decode(kind, after)
    check = [k for k in changes if not (k == "value" and got["auto_manual"] == "auto")]  # in auto the program owns the value
    bad = [k for k in check if want.get(k) != got.get(k)]
    if bad:
        raise BacnetError(f"{kind}{index}: the controller did not keep {', '.join(bad)}")
    return {"ok": True, "changed": True, "before": decode(kind, before), "after": got}
