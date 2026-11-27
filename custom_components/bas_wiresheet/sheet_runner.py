"""Runs monitoring sheets continuously inside Home Assistant (no Node-RED, no open browser needed).

Every 2 s each running sheet reads its entities from the state machine (with last_reported), ticks the Python runtime
and publishes the result to Live Monitoring. One sensor per running sheet records the worst status in history; a
notification is raised when a point stays Stale, Unavailable or without telemetry for the alert delay, and cleared
when it recovers. Sheets that contain Commissioning Pending equipment never raise notifications (their invalid
readings are data-quality states, not equipment alarms). Read only: no entity of the sheet is ever written.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import timedelta
from typing import Any, Callable

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store

from .runtime_py import FRESH, STALE, UNAVAILABLE, UNKNOWN, Runtime

_LOGGER = logging.getLogger(__name__)
TICK = timedelta(seconds=2)
PUBLISH_EVERY_MS = 5000
SAVE_EVERY_MS = 60000
STATUS_ORDER = ["OK", "Freshness Unknown", "Invalid", "Stale", "No Telemetry", "Unavailable", "Alarm"]


def display_status(v: dict) -> str:
    if v.get("alarm") is True:
        return "Alarm"
    if v.get("q") == "unavailable":
        return "Unavailable"
    if v.get("q") == "no_telemetry":
        return "No Telemetry"
    if v.get("q") != "good":
        return "Invalid"
    return {FRESH: "OK", STALE: "Stale", UNAVAILABLE: "Unavailable", UNKNOWN: "Freshness Unknown"}.get(v.get("v"), "OK") if isinstance(v.get("v"), str) else "OK"


def sheet_status(snap: dict) -> tuple[str, dict[str, str]]:
    per = {d["label"]: display_status(d["value"]) for d in snap["displays"].values()}
    worst = max(per.values(), key=STATUS_ORDER.index, default="OK")
    return worst, per


class RunningSheet:
    def __init__(self, sheet: dict, rev: int, started_by: str) -> None:
        self.sheet = sheet
        self.rev = rev
        self.rt = Runtime(sheet)
        self.started_at = time.time() * 1000
        self.started_by = started_by
        self.last_publish = 0.0
        self.last_digest = None
        self.status = "OK"
        self.per: dict[str, str] = {}
        self.bad_since: float | None = None
        self.alerted = False
        self.commissioning = bool(sheet.get("equipment"))


class SheetRunner:
    def __init__(self, hass: HomeAssistant, signal: str, live: dict, audit: Callable) -> None:
        self.hass = hass
        self.signal = signal
        self.live = live
        self.audit = audit
        self.sheets: dict[str, RunningSheet] = {}
        self.entities: dict[str, Any] = {}
        self.display_entities: dict[str, dict[str, Any]] = {}
        self.add_entities: Callable | None = None
        self._store = Store(hass, 1, "bas_wiresheet.runtime_state")
        self._saved: dict[str, Any] = {}
        self._last_save = 0.0
        self._unsub = None
        self.alert_delay_s = 60

    async def async_load(self) -> None:
        self._saved = await self._store.async_load() or {}

    def start_timer(self) -> None:
        if self._unsub is None:
            self._unsub = async_track_time_interval(self.hass, self._tick, TICK)

    async def start(self, sheet_id: str, sheet: dict, rev: int, by: str) -> int:
        """Start (or restart with a new revision). Returns how many timer states were restored."""
        run = RunningSheet({**sheet, "revision": rev}, rev, by)
        saved = self._saved.get(sheet_id)
        restored = run.rt.import_state(saved["state"], saved["at"], time.time() * 1000) if saved and saved.get("revision") == rev else 0
        self.sheets[sheet_id] = run
        self._ensure_entity(sheet_id, sheet.get("name", sheet_id))
        self.start_timer()
        await self._tick_one(sheet_id, run, force=True)
        return restored

    async def stop(self, sheet_id: str) -> None:
        run = self.sheets.pop(sheet_id, None)
        for entity in self.display_entities.get(sheet_id, {}).values():
            entity.set_stopped()
        if run:
            self._saved[sheet_id] = {"revision": run.rev, "at": time.time() * 1000, "state": run.rt.export_state()}
        ent = self.entities.get(sheet_id)
        if ent:
            ent.set_stopped()
        self._clear_alert(sheet_id)
        if run:
            await self._store.async_save(self._saved)

    def status(self) -> dict:
        return {sid: {"revision": r.rev, "started_at": r.started_at, "started_by": r.started_by, "status": r.status, "points": r.per,
                      "notifications": not r.commissioning} for sid, r in self.sheets.items()}

    def _ensure_entity(self, sheet_id: str, name: str) -> None:
        from .sensor import SheetStatusSensor
        ent = self.entities.get(sheet_id)
        if ent is None and self.add_entities is not None:
            ent = self.entities[sheet_id] = SheetStatusSensor(sheet_id, name)
            self.add_entities([ent])
        elif ent is not None:
            ent.set_name(name)

    def _publish_displays(self, sid: str, run: RunningSheet, displays: dict) -> None:
        from .sensor import DisplayValueSensor

        if self.add_entities is None:
            return
        entities = self.display_entities.setdefault(sid, {})
        published = set()
        for bid, display in displays.items():
            params = run.rt.by_id[bid].get("params") or {}
            if params.get("ha_publish") != "Numeric sensor":
                continue
            published.add(bid)
            name = f"{run.sheet.get('name') or sid} {display['label']}"
            signal = display["value"] or {}
            unit = params.get("ha_unit") or signal.get("unit")
            entity = entities.get(bid)
            if entity is None:
                entity = entities[bid] = DisplayValueSensor(sid, bid, name)
                self.add_entities([entity])
            entity.update_display(name, unit, signal)
        for bid in entities.keys() - published:
            entities[bid].set_stopped()

    async def _tick(self, _now=None) -> None:
        for sid, run in list(self.sheets.items()):
            try:
                await self._tick_one(sid, run)
            except Exception:  # noqa: BLE001 - one broken sheet must not stop the others
                _LOGGER.exception("BAS Wire Sheet: runtime error in sheet %s", sid)
        now = time.time() * 1000
        if now - self._last_save >= SAVE_EVERY_MS and self.sheets:
            self._last_save = now
            for sid, run in self.sheets.items():
                self._saved[sid] = {"revision": run.rev, "at": now, "state": run.rt.export_state()}
            await self._store.async_save(self._saved)

    async def _tick_one(self, sid: str, run: RunningSheet, force: bool = False) -> None:
        now = time.time() * 1000
        for eid in run.rt.entity_ids():
            st = self.hass.states.get(eid)
            if st is not None:
                run.rt.ingest(eid, st.state, dict(st.attributes), st.last_reported.timestamp() * 1000, st.last_changed.timestamp() * 1000, now)
            elif eid in run.rt.ctx["samples"]:
                del run.rt.ctx["samples"][eid]
        snap = run.rt.tick(now)
        self._publish_displays(sid, run, snap["displays"])
        run.status, run.per = sheet_status(snap)
        ent = self.entities.get(sid)
        if ent:
            ent.update_status(run.status, run.per, run.rev)
        self._alerts(sid, run, now)
        digest = json.dumps([[k, {p: [o.get("v"), o.get("q")] for p, o in b["outputs"].items() if isinstance(o, dict)}] for k, b in snap["blocks"].items()],
                            default=str)
        if force or digest != run.last_digest or now - run.last_publish >= PUBLISH_EVERY_MS:
            run.last_digest, run.last_publish = digest, now
            snap["runtime"] = {"host": "home-assistant", "deployed_at": run.started_at, "started_by": run.started_by}
            item = {"received_at": now, "data": snap}
            self.live[sid] = item
            async_dispatcher_send(self.hass, self.signal.format(sid), item)

    def _alerts(self, sid: str, run: RunningSheet, now: float) -> None:
        alarms = [k for k, v in run.per.items() if v == "Alarm"]
        quality = [] if run.commissioning else [k for k, v in run.per.items() if v in ("Stale", "Unavailable", "No Telemetry")]
        bad = alarms + quality
        if not bad:
            run.bad_since = None
            if run.alerted:
                run.alerted = False
                self._clear_alert(sid)
                self.hass.async_create_task(self.audit({"by": "sheet runtime", "action": "sheet_alert_cleared", "target": None, "sheet": sid, "ok": True,
                                                        "detail": "All points back to normal"}))
            return
        run.bad_since = run.bad_since or now
        # Alarm blocks have their own delay; data-quality states wait the alert delay.
        if run.alerted or (not alarms and now - run.bad_since < self.alert_delay_s * 1000):
            return
        run.alerted = True
        detail = ", ".join(f"{k}: {run.per[k]}" for k in bad)
        from homeassistant.components import persistent_notification
        persistent_notification.async_create(self.hass, f"{detail}\n\nSheet: {run.sheet.get('name', sid)} (revision r{run.rev}). See BAS Wire Sheet > Live Monitoring.",
                                             title=f"BAS Wire Sheet: {run.sheet.get('name', sid)}", notification_id=f"bws_sheet_{sid}")
        self.hass.async_create_task(self.audit({"by": "sheet runtime", "action": "sheet_alert", "target": None, "sheet": sid, "ok": False, "detail": detail}))

    @callback
    def _clear_alert(self, sid: str) -> None:
        from homeassistant.components import persistent_notification
        persistent_notification.async_dismiss(self.hass, f"bws_sheet_{sid}")
