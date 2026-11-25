"""Home Assistant side of controller backups (snapshots) and the controller alarm watch."""
from __future__ import annotations

import logging
import secrets
import time
from datetime import UTC, datetime
from typing import Any, Awaitable, Callable

from homeassistant.components import persistent_notification
from homeassistant.components.sensor import SensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store

from . import t3000_alarms, t3000_snapshot

_LOGGER = logging.getLogger(__name__)
SNAP_STORE_KEY = "bas_wiresheet.snapshots"
MAX_SNAPSHOTS = 12


class SnapshotStore:
    """Up to MAX_SNAPSHOTS full backups per controller (oldest dropped first, pinned ones kept)."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._store = Store(hass, 1, SNAP_STORE_KEY, private=True, atomic_writes=True)
        self.data: dict[str, Any] = {"devices": {}}
        self.jobs: dict[str, dict] = {}

    async def async_load(self) -> None:
        self.data = await self._store.async_load() or self.data

    def list(self, instance: int) -> list[dict]:
        out = []
        for s in self.data["devices"].get(str(instance), []):
            out.append({"id": s["id"], "taken_at": s["taken_at"], "by": s.get("by"), "note": s.get("note", ""), "pinned": s.get("pinned", False),
                        "reason": s.get("reason", "manual"), **t3000_snapshot.summary(s)})
        return out

    def get(self, instance: int, snap_id: str) -> dict:
        for s in self.data["devices"].get(str(instance), []):
            if s["id"] == snap_id:
                return s
        raise KeyError(f"Snapshot {snap_id} not found")

    async def add(self, snap: dict) -> dict:
        snap["id"] = snap.get("id") or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(2)
        items = self.data["devices"].setdefault(str(snap["instance"]), [])
        items.append(snap)
        while len(items) > MAX_SNAPSHOTS:
            drop = next((s for s in items if not s.get("pinned")), None)
            if drop is None:
                break
            items.remove(drop)
        await self._store.async_save(self.data)
        return snap

    async def update(self, instance: int, snap_id: str, **fields) -> None:
        self.get(instance, snap_id).update(fields)
        await self._store.async_save(self.data)

    async def delete(self, instance: int, snap_id: str) -> None:
        items = self.data["devices"].get(str(instance), [])
        self.data["devices"][str(instance)] = [s for s in items if s["id"] != snap_id]
        await self._store.async_save(self.data)

    def new_job(self, instance: int, kind: str, total: int) -> dict:
        job = {"id": secrets.token_urlsafe(10), "instance": instance, "kind": kind, "done": 0, "total": total, "state": "running",
               "started": time.time(), "result": None, "error": None}
        self.jobs[job["id"]] = job
        for jid in [j for j, v in self.jobs.items() if v["state"] != "running" and time.time() - v["started"] > 3600]:
            self.jobs.pop(jid, None)
        return job


class ControllerAlarmSensor(SensorEntity):
    """Active alarms of one controller (count), with the alarm list as attributes."""

    _attr_should_poll = False
    _attr_icon = "mdi:alarm-light-outline"
    _attr_native_unit_of_measurement = "alarms"

    def __init__(self, instance: int, name: str) -> None:
        self._attr_unique_id = f"bas_wiresheet_t3_alarms_{instance}"
        self.entity_id = f"sensor.bas_wiresheet_t3_{instance}_alarms"
        self._attr_name = f"{name} Controller Alarms"
        self._attr_native_value = None
        self._attr_extra_state_attributes = {"instance": instance}
        self._instance = instance

    def update_alarms(self, alarms: list[dict] | None, error: str | None = None) -> None:
        if alarms is None:
            self._attr_available = False
            self._attr_extra_state_attributes = {"instance": self._instance, "error": error}
        else:
            active = [a for a in alarms if a["active"]]
            self._attr_available = True
            self._attr_native_value = len(active)
            self._attr_extra_state_attributes = {"instance": self._instance, "unacknowledged": sum(1 for a in active if not a["acknowledged"]),
                                                 "alarms": [{k: a[k] for k in ("index", "message", "time", "acknowledged", "active", "source")} for a in alarms]}
        if self.hass is not None:
            self.async_write_ha_state()


class AlarmWatch:
    """Reads every controller's alarm table periodically; new alarms become notifications and History entries."""

    def __init__(self, hass: HomeAssistant, devices: Callable[[], list[dict]], run: Callable[[int, Callable], Awaitable[Any]],
                 audit: Callable[[dict], Awaitable[None]], interval_s: int) -> None:
        self.hass, self._devices, self._run, self._audit = hass, devices, run, audit
        self.interval_s = interval_s
        self.add_entities: Callable | None = None
        self.sensors: dict[int, ControllerAlarmSensor] = {}
        self.known: dict[int, set] = {}
        self.latest: dict[int, dict] = {}
        self._unsub = None
        self._busy = False

    def start(self) -> None:
        if self.interval_s > 0 and not self._unsub:
            from datetime import timedelta
            self._unsub = async_track_time_interval(self.hass, self._tick, timedelta(seconds=self.interval_s))
            self.hass.async_create_task(self._tick())

    def stop(self) -> None:
        if self._unsub:
            self._unsub()
            self._unsub = None

    async def _tick(self, _now=None) -> None:
        if self._busy:
            return
        self._busy = True
        try:
            for dev in self._devices():
                await self.refresh(dev["instance"], dev.get("name") or str(dev["instance"]))
        finally:
            self._busy = False

    def _sensor(self, instance: int, name: str) -> ControllerAlarmSensor | None:
        if instance not in self.sensors and self.add_entities:
            self.sensors[instance] = ControllerAlarmSensor(instance, name)
            self.add_entities([self.sensors[instance]])
        return self.sensors.get(instance)

    async def refresh(self, instance: int, name: str) -> list[dict] | None:
        try:
            alarms = await self._run(instance, lambda c: t3000_alarms.read_alarms(c, instance))
        except Exception as err:  # noqa: BLE001 - a controller that does not answer must not stop the others
            self.latest[instance] = {"at": time.time(), "error": str(err), "alarms": None}
            sensor = self._sensor(instance, name)
            if sensor:
                sensor.update_alarms(None, str(err))
            return None
        self.latest[instance] = {"at": time.time(), "error": None, "alarms": alarms}
        sensor = self._sensor(instance, name)
        if sensor:
            sensor.update_alarms(alarms)
        keys = {(a["index"], a["time_s"], a["message"]) for a in alarms if a["active"]}
        first = instance not in self.known
        new = [a for a in alarms if a["active"] and (a["index"], a["time_s"], a["message"]) not in self.known.get(instance, set())]
        self.known[instance] = keys
        if not first:
            for a in new:
                persistent_notification.async_create(
                    self.hass, f"**{name}** (controller {instance}): {a['message']}\n\nRaised {a['time'] or 'unknown time'} "
                    f"({'firmware' if a['source'] == 'firmware' else 'program'}). Acknowledge it in BAS Wire Sheet > T3000 Explorer > Alarms.",
                    title="Controller alarm", notification_id=f"bas_wiresheet_t3_alarm_{instance}_{a['index']}")
                await self._audit({"by": "Alarm watch", "action": "controller_alarm", "target": f"t3000:{instance}", "ok": True,
                                   "detail": f"Alarm {a['index']}: {a['message']}"})
        return alarms
