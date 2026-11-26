"""TEMCO T3 controller alarm log and monitor (trend log) definitions.

Alarm_point (READALARM_T3000 = 13, write 113), 16 entries of 102 bytes, layout from T3000's own decoder
(global_function.cpp): point 0-4, modem 5, printer 6, alarm 7, restored 8, acknowledged 9, delete 10, type 11,
cond_type 12, level 13, alarm_time 14-17 (Unix seconds), byte 18 = message length (named alarm_count in T3000), message 19-77 (58 chars + NUL),
panel_type 83, alarm_id 85-86, prg 87 (255 = raised by the firmware, not a program), alarm_panel 88 (originating panel).
Verified against a T3-BB holding real alarms ("Short circuit on IN1,2,…", "Open circuit on IN6,7,"): lengths 40/22,
originating panel = the controller's panel number.

Str_monitor_point (READMONITOR_T3000 = 10), 12 entries of 104 bytes: label 0-8, inputs 9-78 (14 x Point_Net),
range 79-92, seconds 93, minutes 94, hours 95, max_time_length 96, num_inputs 97, an_inputs 98, status 99,
next_sample_time 100-103.
"""
from __future__ import annotations

import struct
from datetime import UTC, datetime
from typing import Any

from . import t3000_bacnet as tb

ALARM_CMD, ALARM_SIZE, ALARM_COUNT = 13, 102, 16
MONITOR_CMD, MONITOR_SIZE, MONITOR_COUNT = 10, 104, 12
POINT_TYPES = {1: "OUT", 2: "IN", 3: "VAR"}


def _read(client, cmd: int, index: int, size: int) -> bytes:
    data = tb.read_checked(client, tb.header(tb.PRIVATE_LEN, cmd, index, size),
                           lambda d: len(d) == tb.PRIVATE_LEN + size and d[2] == cmd and d[3] == index, f"reading record {index + 1} of table {cmd}")
    return data[tb.PRIVATE_LEN:]


def _point(raw: bytes) -> str | None:
    number, ptype = raw[0], raw[1] & 0x1F
    if ptype in POINT_TYPES:
        return f"{POINT_TYPES[ptype]}{(number | ((raw[1] >> 5) << 8)) + 1}"
    return None


def decode_alarm(index: int, raw: bytes) -> dict[str, Any] | None:
    if not any(raw):
        return None
    when = struct.unpack_from("<I", raw, 14)[0]
    return {"index": index, "active": bool(raw[7]), "restored": bool(raw[8]), "acknowledged": bool(raw[9]), "deleted": bool(raw[10]),
            "level": raw[13], "message": raw[19:78].split(b"\0")[0].decode("latin-1").strip().rstrip(","),
            "time": datetime.fromtimestamp(when, UTC).isoformat() if when else None, "time_s": when, "id": struct.unpack_from("<H", raw, 85)[0],
            "program": raw[87] if 0 < raw[87] < 255 else None, "source": "firmware" if raw[87] == 255 else "program" if raw[87] else "unknown",
            "panel": raw[88] or None, "point": _point(raw[0:5])}


def read_alarms(client, instance: int) -> list[dict]:
    if client.identify() != instance:
        raise tb.UploadRefused("Wrong controller at this address")
    out = []
    for i, raw in enumerate(tb.read_range(client, ALARM_CMD, 0, ALARM_COUNT, ALARM_SIZE, 4)):
        a = decode_alarm(i + 1, raw)
        if a and not a["deleted"]:
            out.append(a)
    return out


def set_alarm(client, instance: int, index: int, action: str) -> dict:
    """action 'acknowledge' or 'delete'. Read, set the flag, write back, read back."""
    if action not in ("acknowledge", "delete"):
        raise tb.UploadRefused("Unknown alarm action")
    if not 1 <= index <= ALARM_COUNT:
        raise tb.UploadRefused("Alarm 1-16")
    if client.identify() != instance:
        raise tb.UploadRefused("Wrong controller at this address")
    raw = bytearray(_read(client, ALARM_CMD, index - 1, ALARM_SIZE))
    if not any(raw):
        raise tb.UploadRefused(f"Alarm {index} is empty")
    off = 9 if action == "acknowledge" else 10
    raw[off] = 1
    client._private_write(tb.header(tb.PRIVATE_LEN + ALARM_SIZE, ALARM_CMD + 100, index - 1, ALARM_SIZE) + bytes(raw))
    after = _read(client, ALARM_CMD, index - 1, ALARM_SIZE)
    if after[off] != 1 and any(after):
        raise tb.BacnetError(f"Alarm {index}: the controller did not keep the {action}")
    return {"ok": True, "index": index, "action": action}


def decode_monitor(index: int, raw: bytes) -> dict[str, Any]:
    inputs = [p for p in (_point(raw[9 + 5 * k: 14 + 5 * k]) for k in range(14)) if p]
    interval = raw[93] + raw[94] * 60 + raw[95] * 3600
    return {"index": index, "label": raw[0:9].split(b"\0")[0].decode("latin-1"), "inputs": inputs[: raw[97] or len(inputs)],
            "interval_s": interval, "on": bool(raw[99]), "configured": bool(inputs)}


def read_monitors(client, instance: int) -> list[dict]:
    if client.identify() != instance:
        raise tb.UploadRefused("Wrong controller at this address")
    return [decode_monitor(i + 1, raw) for i, raw in enumerate(tb.read_range(client, MONITOR_CMD, 0, MONITOR_COUNT, MONITOR_SIZE, 4))]
