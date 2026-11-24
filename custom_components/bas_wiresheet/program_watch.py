"""Nightly read of controller programs: dated copies and detection of changes made outside BAS Wire Sheet.

Read only on the controllers. A change made from this panel (load, update, restore, remove) is marked first, so the
next read records it as a panel change instead of raising an alert. A change made elsewhere (for example in T3000) is
recorded in History and raised as a Home Assistant notification with the listing difference.
"""
from __future__ import annotations

import difflib
import hashlib
import logging
import time
from datetime import UTC, datetime
from typing import Any, Awaitable, Callable

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from . import t3000_bacnet, t3000_import, t3000_toolchain

_LOGGER = logging.getLogger(__name__)
STORE_KEY = "bas_wiresheet.programs"
MAX_VERSIONS = 20


class ProgramWatch:
    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._store = Store(hass, 1, STORE_KEY)
        self.data: dict[str, Any] = {"devices": {}, "panel_changes": {}, "runs": []}
        self.running = False

    async def async_load(self) -> None:
        self.data = await self._store.async_load() or self.data

    async def _save(self) -> None:
        await self._store.async_save(self.data)

    async def note_panel_change(self, instance: int, slot: int) -> None:
        self.data["panel_changes"][f"{instance}:{slot}"] = time.time() * 1000
        await self._save()

    def latest(self, instance: int) -> list[dict]:
        """Last stored copy of each program of a controller (from the most recent read)."""
        out = []
        for slot, s in sorted((self.data["devices"].get(str(instance)) or {}).items(), key=lambda x: int(x[0])):
            code = bytes.fromhex(s["versions"][-1]["code_hex"]) if s["used"] and s["versions"] else b""
            out.append({"slot": int(slot), "on": s["on"], "description": s.get("description", ""), "label": s.get("label", ""),
                        "code": code, "read_at": s["read_at"]})
        return out

    def status(self) -> dict:
        out = []
        for inst, slots in self.data["devices"].items():
            for slot, s in sorted(slots.items(), key=lambda x: int(x[0])):
                out.append({"instance": int(inst), "slot": int(slot), "used": s["used"], "on": s["on"], "read_at": s["read_at"],
                            "logic": s["logic"], "versions": [{k: v[k] for k in ("at", "used", "logic", "source")} for v in s["versions"]]})
        return {"programs": out, "runs": self.data["runs"][-20:], "running": self.running}

    async def run(self, devices: list[int], read_fn: Callable[[int, Callable], Awaitable[Any]], audit, reason: str) -> dict:
        """Read every program of each device; read_fn(instance, fn) runs fn(client) for that controller."""
        if self.running:
            return {"ok": False, "error": "A program check is already running"}
        self.running = True
        started = time.time() * 1000
        summary = {"at": datetime.now(UTC).isoformat(), "reason": reason, "devices": {}, "changes": 0, "external": 0}
        try:
            for inst in devices:
                try:
                    progs = await read_fn(inst, _read_all(inst))
                except Exception as err:  # noqa: BLE001 - one unreachable controller must not stop the others
                    summary["devices"][str(inst)] = f"read failed: {err}"
                    _LOGGER.warning("BAS Wire Sheet program watch: device %s read failed: %s", inst, err)
                    continue
                summary["devices"][str(inst)] = "ok"
                known = self.data["devices"].setdefault(str(inst), {})
                for slot, (status, code) in enumerate(progs, start=1):
                    used = t3000_bacnet.code_used_length(code)
                    logic = t3000_import.logic_digest(code) if used else ""
                    prev = known.get(str(slot))
                    entry = {"at": datetime.now(UTC).isoformat(), "used": used, "logic": logic,
                             "code_hex": code[:used].hex() if used else "", "source": "first read"}
                    if prev is None:
                        known[str(slot)] = {"used": used, "on": status["on"], "read_at": started, "logic": logic, "versions": [entry] if used else [],
                                            "description": status.get("description", ""), "label": status.get("label", "")}
                        continue
                    changed = prev["logic"] != logic
                    if changed:
                        summary["changes"] += 1
                        panel = self.data["panel_changes"].get(f"{inst}:{slot}", 0) > prev["read_at"]
                        entry["source"] = "panel" if panel else "outside the panel"
                        old = bytes.fromhex(prev["versions"][-1]["code_hex"]) if prev["versions"] else b""
                        diff = _listing_diff(old, code[:used])
                        if not panel:
                            summary["external"] += 1
                            detail = f"Program {slot}: {_size_text(prev['used'], used)} changed outside BAS Wire Sheet"
                            await audit({"by": "program watch", "action": "program_changed_outside", "target": f"t3000:{inst}:{slot}",
                                         "ok": False, "detail": detail + (" · " + " | ".join(diff[:12]) if diff else "")})
                            _notify(self.hass, inst, slot, detail, diff)
                        else:
                            await audit({"by": "program watch", "action": "program_checked", "target": f"t3000:{inst}:{slot}", "ok": True,
                                         "detail": f"Program {slot}: panel change confirmed on the controller ({_size_text(prev['used'], used)})"})
                        prev["versions"].append(entry)
                        del prev["versions"][:-MAX_VERSIONS]
                    prev.update({"used": used, "on": status["on"], "read_at": started, "logic": logic,
                                 "description": status.get("description", ""), "label": status.get("label", "")})
        finally:
            self.running = False
            summary["duration_s"] = round(time.time() - started / 1000, 1)
            self.data["runs"].append(summary)
            del self.data["runs"][:-60]
            await self._save()
        return {"ok": True, **summary}


def _notify(hass: HomeAssistant, inst: int, slot: int, detail: str, diff: list[str]) -> None:
    from homeassistant.components import persistent_notification
    persistent_notification.async_create(
        hass, f"{detail}.\n\nDifference:\n```\n" + "\n".join(diff[:40]) + "\n```\n\nSee BAS Wire Sheet > History.",
        title=f"Controller {inst} program {slot} changed", notification_id=f"bws_program_{inst}_{slot}")


def _size_text(a: int, b: int) -> str:
    if not a:
        return f"new program ({b} bytes)"
    if not b:
        return "program erased"
    return f"{a} to {b} bytes"


def _listing(code: bytes) -> list[str]:
    if not code or not t3000_bacnet.code_used_length(code.ljust(2000, b"\0")):
        return []
    try:
        return t3000_toolchain.decompile(code.ljust(2000, b"\0")).strip().splitlines()
    except Exception:  # noqa: BLE001 - a listing that cannot be produced is shown as a digest
        return [f"(no listing; SHA-256 {hashlib.sha256(code).hexdigest()[:16]})"]


def _listing_diff(old: bytes, new: bytes) -> list[str]:
    return [l for l in difflib.unified_diff(_listing(old), _listing(new), lineterm="", n=0) if not l.startswith(("---", "+++", "@@"))]


def _read_all(instance: int):
    def read(client):
        if client.identify() != instance:
            raise t3000_bacnet.UploadRefused("Wrong controller at this address")
        out = []
        for i in range(16):
            status = t3000_bacnet.describe_status(client.read_point(i))
            out.append((status, client.read_code(i)))
        return out
    return read
