"""T3000 schedules (weekly and annual routines) and PID controllers: read and edit.

Layouts and commands from T3000 (CM5/ud_str.h, global_function.cpp, BacnetScheduleTime.cpp, AnnualRout_InsertDia.cpp):
  PID controllers   read 4  / write 104, Str_controller_point 28 bytes, 16 per controller
  weekly routines   read 5  / write 105, Str_weekly_routine_point 42 bytes, 8 per controller
  annual routines   read 6  / write 106, Str_annual_routine_point 33 bytes, 4 per controller
  weekly times      read 17 / write 117, 144 bytes: 9 days (Mon..Sun, Holiday 1, Holiday 2) x 8 entries x (minutes, hours)
  weekly flags      read 41 / write 141, 72 bytes in the same order: 1 = ON entry, 0 = OFF entry, 255 = empty
  annual days       read 18 / write 118, 46 bytes: bit n (byte n/8, bit n%8) = day n of the year, January 1 = 1
Entries alternate ON (rows 0, 2, 4, 6) and OFF (rows 1, 3, 5, 7), as in T3000's schedule screen.
Every edit reads back and verifies. Firmware without the flag table falls back to T3000's rule (00:00 = empty).
"""
from __future__ import annotations

import calendar
import datetime as dt
import struct
from typing import Any

from .t3000_bacnet import PRIVATE_LEN, BacnetError, UploadRefused, header, read_checked

TABLES = {
    # name: (read command, size, count)
    "pid": (4, 28, 16),
    "weekly": (5, 42, 8),
    "annual": (6, 33, 4),
    "weekly_times": (17, 144, 8),
    "weekly_flags": (41, 72, 8),
    "annual_days": (18, 46, 4),
}
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday", "Holiday 1", "Holiday 2"]
POINT_TYPES = {1: "OUT", 2: "IN", 3: "VAR"}


def read_table(client, name: str, index: int) -> bytes:
    cmd, size, count = TABLES[name]
    if not 1 <= index <= count:
        raise UploadRefused(f"{name} {index} does not exist (1-{count})")
    i = index - 1
    data = read_checked(client, header(PRIVATE_LEN, cmd, i, size), lambda d: len(d) == PRIVATE_LEN + size and d[2] == cmd and d[3] == i,
                        f"reading {name} {index}")
    return data[PRIVATE_LEN:]


def write_table(client, name: str, index: int, raw: bytes) -> None:
    cmd, size, _ = TABLES[name]
    if len(raw) != size:
        raise ValueError("Bad record size")
    client._private_write(header(PRIVATE_LEN + size, cmd + 100, index - 1, size) + raw)


def _text(raw: bytes) -> str:
    return raw.split(b"\0")[0].decode("latin-1")


def _put_text(buf: bytearray, start: int, length: int, text: str, field: str, upper: bool = False) -> None:
    data = (text.upper() if upper else text).encode("latin-1", "strict")
    if len(data) > length - 1:
        raise UploadRefused(f"{field} is limited to {length - 1} characters")
    buf[start:start + length] = data.ljust(length, b"\0")


def _point(raw: bytes) -> str | None:
    number, ptype, panel = raw[0], raw[1], raw[2]
    kind = POINT_TYPES.get(ptype)
    return f"{kind}{number + 1}" if kind else None


# ---------------- weekly routines ----------------

def decode_weekly(raw: bytes) -> dict:
    return {"description": _text(raw[0:21]), "label": _text(raw[21:30]), "value": "on" if raw[30] else "off",
            "auto_manual": "manual" if raw[31] else "auto", "override_1": _point(raw[36:39]), "override_2": _point(raw[39:42])}


def decode_times(times: bytes, flags: bytes | None) -> list[list[dict]]:
    """Per day: up to 4 {on, off} pairs ('HH:MM' or None)."""
    out = []
    for j in range(9):
        pairs = []
        for k in range(4):
            pair = {}
            for which, row in (("on", 2 * k), ("off", 2 * k + 1)):
                m, h = times[j * 16 + row * 2], times[j * 16 + row * 2 + 1]
                if flags is not None:
                    used = flags[j * 8 + row] in (0, 1)
                else:
                    used = (h, m) != (0, 0)            # T3000's rule without flags: 00:00 is an empty entry
                pair[which] = f"{h:02d}:{m:02d}" if used and h < 24 and m < 60 else None
            pairs.append(pair)
        out.append(pairs)
    return out


def _parse_hhmm(text: str | None) -> tuple[int, int] | None:
    if text in (None, ""):
        return None
    try:
        h, m = (int(x) for x in str(text).split(":"))
    except ValueError as err:
        raise UploadRefused(f"Bad time {text!r}; use HH:MM") from err
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise UploadRefused(f"Bad time {text!r}; use 00:00 to 23:59")
    return h, m


def encode_times(days: list[list[dict]]) -> tuple[bytes, bytes]:
    if len(days) != 9:
        raise UploadRefused("A schedule has 9 days (Monday to Sunday, Holiday 1, Holiday 2)")
    times, flags = bytearray(144), bytearray([255] * 72)
    for j, pairs in enumerate(days):
        if len(pairs) > 4:
            raise UploadRefused(f"{DAYS[j]}: at most 4 ON/OFF pairs")
        for k, pair in enumerate(pairs):
            for which, row, flag in (("on", 2 * k, 1), ("off", 2 * k + 1, 0)):
                t = _parse_hhmm(pair.get(which))
                if t is None:
                    continue
                times[j * 16 + row * 2], times[j * 16 + row * 2 + 1] = t[1], t[0]
                flags[j * 8 + row] = flag
    return bytes(times), bytes(flags)


def read_weekly(client, index: int) -> dict:
    rec = decode_weekly(read_table(client, "weekly", index))
    times = read_table(client, "weekly_times", index)
    try:
        flags = read_table(client, "weekly_flags", index)
    except BacnetError:
        flags = None            # older firmware: no flag table
    return {**rec, "index": index, "days": decode_times(times, flags), "flags_supported": flags is not None}


def edit_weekly(client, expected_device: int, index: int, changes: dict[str, Any]) -> dict:
    if client.identify() != expected_device:
        raise UploadRefused("Wrong controller at this address")
    before = read_weekly(client, index)
    raw = bytearray(read_table(client, "weekly", index))
    rec_changes = {k: v for k, v in changes.items() if k in ("description", "label", "auto_manual", "value")}
    unknown = set(changes) - {"description", "label", "auto_manual", "value", "days"}
    if unknown:
        raise UploadRefused(f"Not editable: {', '.join(sorted(unknown))}")
    if "description" in rec_changes:
        _put_text(raw, 0, 21, str(rec_changes["description"]), "Description")
    if "label" in rec_changes:
        _put_text(raw, 21, 9, str(rec_changes["label"]), "Label", upper=True)
    if "auto_manual" in rec_changes:
        raw[31] = 1 if rec_changes["auto_manual"] == "manual" else 0
    if "value" in rec_changes:
        raw[30] = 1 if rec_changes["value"] == "on" else 0
    if rec_changes:
        write_table(client, "weekly", index, bytes(raw))
    if "days" in changes:
        times, flags = encode_times(changes["days"])
        if before["flags_supported"]:
            write_table(client, "weekly_flags", index, flags)
        write_table(client, "weekly_times", index, times)
    after = read_weekly(client, index)
    for k in rec_changes:
        if str(after.get(k)) != str(rec_changes[k] if k != "label" else str(rec_changes[k]).upper()):
            raise BacnetError(f"Weekly routine {index}: the controller did not keep {k}")
    if "days" in changes and _norm(after["days"]) != _norm(changes["days"]) and after["flags_supported"]:
        raise BacnetError(f"Weekly routine {index}: the schedule read back differs")
    return {"ok": True, "before": before, "after": after}


def _norm(days: list[list[dict]]) -> list:
    out = []
    for pairs in days:
        p = [(x.get("on") or None, x.get("off") or None) for x in pairs] + [(None, None)] * (4 - len(pairs))
        out.append(p[:4])
    return out


# ---------------- annual routines ----------------

def decode_annual(raw: bytes) -> dict:
    return {"description": _text(raw[0:21]), "label": _text(raw[21:30]), "value": "on" if raw[30] else "off",
            "auto_manual": "manual" if raw[31] else "auto"}


def decode_days(bits: bytes, year: int) -> list[str]:
    out = []
    start = dt.date(year, 1, 1)
    for n in range(1, 367 if calendar.isleap(year) else 366):
        if bits[n // 8] & (1 << (n % 8)):
            d = start + dt.timedelta(days=n - 1)
            out.append(f"{d.month:02d}-{d.day:02d}")
    return out


def encode_days(days: list[str], year: int) -> bytes:
    bits = bytearray(46)
    for text in days:
        try:
            m, d = (int(x) for x in str(text).split("-"))
            n = (dt.date(year, m, d) - dt.date(year, 1, 1)).days + 1
        except ValueError as err:
            raise UploadRefused(f"Bad day {text!r}; use MM-DD valid in {year}") from err
        bits[n // 8] |= 1 << (n % 8)
    return bytes(bits)


def read_annual(client, index: int, year: int) -> dict:
    rec = decode_annual(read_table(client, "annual", index))
    return {**rec, "index": index, "year": year, "days": decode_days(read_table(client, "annual_days", index), year)}


def edit_annual(client, expected_device: int, index: int, changes: dict[str, Any], year: int) -> dict:
    if client.identify() != expected_device:
        raise UploadRefused("Wrong controller at this address")
    unknown = set(changes) - {"description", "label", "auto_manual", "value", "days"}
    if unknown:
        raise UploadRefused(f"Not editable: {', '.join(sorted(unknown))}")
    before = read_annual(client, index, year)
    raw = bytearray(read_table(client, "annual", index))
    if "description" in changes:
        _put_text(raw, 0, 21, str(changes["description"]), "Description")
    if "label" in changes:
        _put_text(raw, 21, 9, str(changes["label"]), "Label", upper=True)
    if "auto_manual" in changes:
        raw[31] = 1 if changes["auto_manual"] == "manual" else 0
    if "value" in changes:
        raw[30] = 1 if changes["value"] == "on" else 0
    if set(changes) - {"days"}:
        write_table(client, "annual", index, bytes(raw))
    if "days" in changes:
        write_table(client, "annual_days", index, encode_days(changes["days"], year))
    after = read_annual(client, index, year)
    if "days" in changes and sorted(after["days"]) != sorted(set(changes["days"])):
        raise BacnetError(f"Annual routine {index}: the days read back differ")
    return {"ok": True, "before": before, "after": after}


# ---------------- PID controllers ----------------

def decode_pid(raw: bytes) -> dict:
    return {"input": _point(raw[0:3]), "input_value": struct.unpack_from("<i", raw, 3)[0] / 1000,
            "output": struct.unpack_from("<i", raw, 7)[0] / 1000, "setpoint": _point(raw[11:14]),
            "setpoint_value": struct.unpack_from("<i", raw, 14)[0] / 1000, "units": raw[18],
            "auto_manual": "manual" if raw[19] else "auto", "action": "+" if raw[20] else "-",
            "i_time_unit": "min" if raw[21] else "hour", "sample_time": raw[22], "proportional": raw[24], "reset": raw[25],
            "bias": raw[26], "rate": raw[27] / 100, "used": raw[1] in POINT_TYPES}


def encode_pid(raw: bytes, changes: dict[str, Any]) -> bytes:
    allowed = {"auto_manual", "action", "proportional", "reset", "i_time_unit", "rate", "bias", "setpoint_value", "output", "sample_time"}
    unknown = set(changes) - allowed
    if unknown:
        raise UploadRefused(f"Not editable: {', '.join(sorted(unknown))}")
    buf = bytearray(raw)

    def byte(key, lo, hi):
        v = int(changes[key])
        if not lo <= v <= hi:
            raise UploadRefused(f"{key} must be {lo}-{hi}")
        return v
    if "auto_manual" in changes:
        buf[19] = 1 if changes["auto_manual"] == "manual" else 0
    if "action" in changes:
        if changes["action"] not in ("+", "-"):
            raise UploadRefused("action must be + or -")
        buf[20] = 1 if changes["action"] == "+" else 0
    if "i_time_unit" in changes:
        buf[21] = 1 if changes["i_time_unit"] == "min" else 0
    if "sample_time" in changes:
        buf[22] = byte("sample_time", 0, 255)
    if "proportional" in changes:
        buf[24] = byte("proportional", 0, 255)
    if "reset" in changes:
        buf[25] = byte("reset", 0, 255)
    if "bias" in changes:
        buf[26] = byte("bias", 0, 100)
    if "rate" in changes:
        r = round(float(changes["rate"]) * 100)
        if not 0 <= r <= 200:
            raise UploadRefused("rate must be 0.00-2.00")
        buf[27] = r
    if "setpoint_value" in changes:
        struct.pack_into("<i", buf, 14, round(float(changes["setpoint_value"]) * 1000))
    if "output" in changes:
        o = float(changes["output"])
        if not 0 <= o <= 100:
            raise UploadRefused("output must be 0-100 %")
        struct.pack_into("<i", buf, 7, round(o * 1000))
    return bytes(buf)


def read_pids(client) -> list[dict]:
    return [{"index": i, **decode_pid(read_table(client, "pid", i))} for i in range(1, 17)]


def edit_pid(client, expected_device: int, index: int, changes: dict[str, Any]) -> dict:
    if client.identify() != expected_device:
        raise UploadRefused("Wrong controller at this address")
    before = read_table(client, "pid", index)
    if "output" in changes and not (changes.get("auto_manual") == "manual" or (before[19] and "auto_manual" not in changes)):
        raise UploadRefused("The output can be set only in manual")
    target = encode_pid(before, changes)
    write_table(client, "pid", index, target)
    after = read_table(client, "pid", index)
    want, got = decode_pid(target), decode_pid(after)
    bad = [k for k in changes if k not in ("output", "setpoint_value") and want[k] != got[k]]
    if bad:
        raise BacnetError(f"PID {index}: the controller did not keep {', '.join(bad)}")
    return {"ok": True, "before": decode_pid(before), "after": got}
