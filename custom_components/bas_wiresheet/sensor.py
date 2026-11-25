"""One status sensor per monitoring sheet that runs inside Home Assistant (recorded in history)."""
from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
from homeassistant.core import HomeAssistant

from .const import DOMAIN


async def async_setup_platform(hass: HomeAssistant, config, async_add_entities, discovery_info=None) -> None:
    runner = hass.data[DOMAIN]["runner"]
    runner.add_entities = async_add_entities
    hass.data[DOMAIN]["sensor_add"] = async_add_entities  # the alarm watch may be created after this platform
    watch = hass.data[DOMAIN].get("alarm_watch")
    if watch is not None:
        watch.add_entities = async_add_entities
    for sid, run in runner.sheets.items():
        runner._ensure_entity(sid, run.sheet.get("name", sid))


class SheetStatusSensor(SensorEntity):
    _attr_should_poll = False
    _attr_icon = "mdi:sitemap-outline"

    def __init__(self, sheet_id: str, name: str) -> None:
        self._sheet_id = sheet_id
        self._attr_unique_id = f"bas_wiresheet_sheet_{sheet_id}"
        self.entity_id = f"sensor.bas_wiresheet_{sheet_id.replace('-', '_')}"
        self._attr_name = f"Wire Sheet {name}"
        self._attr_native_value = None
        self._attr_extra_state_attributes = {"sheet_id": sheet_id}
        self._attr_available = True

    def set_name(self, name: str) -> None:
        self._attr_name = f"Wire Sheet {name}"

    def update_status(self, status: str, per: dict[str, str], rev: int) -> None:
        changed = status != self._attr_native_value or per != self._attr_extra_state_attributes.get("points") or not self._attr_available
        self._attr_native_value = status
        self._attr_available = True
        self._attr_extra_state_attributes = {"sheet_id": self._sheet_id, "revision": rev, "points": per}
        if changed and self.hass is not None:
            self.async_write_ha_state()

    def set_stopped(self) -> None:
        self._attr_available = False
        if self.hass is not None:
            self.async_write_ha_state()
