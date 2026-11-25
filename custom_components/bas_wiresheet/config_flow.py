"""UI configuration for BAS Wire Sheet (Settings > Devices & services). YAML configuration, when present, takes precedence."""
from __future__ import annotations

import re
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import DOMAIN

DEFAULTS: dict[str, Any] = {
    "admins_are_editors": True, "editors": [], "full_access_all_devices": False, "unlock_protected_points": False,
    "program_watch": True, "program_watch_time": "02:30:00", "alarm_poll_s": 60, "mstp_routers": "", "locked_equipment": "", "devices": {},
}
MODELS = ["", "T3-BB", "T3-LB", "T3-TB", "T3-TB-11I", "TSTAT10"]
ROUTER_RE = re.compile(r"^\s*(\d+)\s*[=:]\s*([0-9.]+(?::\d+)?)\s*$")


def _ranges(text: str) -> list[str]:
    return [x.strip().upper() for x in re.split(r"[,\s]+", text or "") if x.strip()]


def options_to_conf(options: dict) -> dict:
    """Options saved by the UI -> the same structure as the YAML configuration (validated by CONFIG_SCHEMA)."""
    o = {**DEFAULTS, **options}
    routers = {}
    for part in re.split(r"[,\n]+", o.get("mstp_routers") or ""):
        m = ROUTER_RE.match(part)
        if m:
            routers[int(m.group(1))] = m.group(2)
    locked = []
    for line in (o.get("locked_equipment") or "").splitlines():
        if ":" in line:
            name, pattern = line.split(":", 1)
            if name.strip() and pattern.strip():
                locked.append({"name": name.strip(), "pattern": pattern.strip()})
    devices = {}
    for inst, d in (o.get("devices") or {}).items():
        dev = {"access": d.get("access", "restricted"), "allow_program_read": bool(d.get("allow_program_read", True)),
               "allow_point_writes": bool(d.get("allow_point_writes", False)),
               "program_write_slots": [int(x) for x in re.split(r"[,\s]+", str(d.get("program_write_slots") or "")) if x.strip().isdigit()],
               "sandbox": _ranges(d.get("sandbox", "")), "locked": _ranges(d.get("locked", ""))}
        if d.get("model"):
            dev["model"] = d["model"]
        if d.get("address"):
            dev["address"] = d["address"].strip()
        devices[int(inst)] = dev
    conf: dict[str, Any] = {
        "admins_are_editors": bool(o["admins_are_editors"]), "editors": list(o.get("editors") or []),
        "t3000": {"full_access_all_devices": bool(o["full_access_all_devices"]), "unlock_protected_points": bool(o["unlock_protected_points"]),
                  "program_watch": bool(o["program_watch"]), "program_watch_time": o["program_watch_time"], "alarm_poll_s": int(o["alarm_poll_s"]),
                  "mstp_routers": routers, "locked_equipment": locked, "devices": devices},
    }
    nr = o.get("nodered") or {}
    if nr.get("url") and nr.get("ha_server_id"):
        conf["nodered_test"] = {k: v for k, v in nr.items() if v not in (None, "")}
    return conf


class BasWireSheetConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict | None = None):
        if self.hass.data.get(f"{DOMAIN}_yaml"):
            return self.async_abort(reason="configured_in_yaml")
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        if user_input is not None:
            return self.async_create_entry(title="BAS Wire Sheet", data={}, options={**DEFAULTS, **user_input})
        return self.async_show_form(step_id="user", data_schema=vol.Schema({
            vol.Required("full_access_all_devices", default=False): bool,
            vol.Required("admins_are_editors", default=True): bool,
        }))

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return BasWireSheetOptionsFlow()


class BasWireSheetOptionsFlow(config_entries.OptionsFlow):
    def __init__(self) -> None:
        self._device: int | None = None

    @property
    def _opts(self) -> dict:
        return {**DEFAULTS, **dict(self.config_entry.options)}

    def _save(self, **changes):
        return self.async_create_entry(data={**self._opts, **changes})

    async def async_step_init(self, user_input=None):
        return self.async_show_menu(step_id="init", menu_options=["general", "controller", "locked", "nodered"])

    async def async_step_general(self, user_input=None):
        o = self._opts
        if user_input is not None:
            routers = user_input.get("mstp_routers", "")
            bad = [p for p in re.split(r"[,\n]+", routers) if p.strip() and not ROUTER_RE.match(p)]
            if bad:
                return self.async_show_form(step_id="general", data_schema=self._general_schema(user_input), errors={"mstp_routers": "bad_router"})
            return self._save(**user_input)
        return self.async_show_form(step_id="general", data_schema=self._general_schema(o))

    def _general_schema(self, o: dict) -> vol.Schema:
        return vol.Schema({
            vol.Required("admins_are_editors", default=o["admins_are_editors"]): bool,
            vol.Optional("editors", default=o.get("editors") or []): selector.SelectSelector(selector.SelectSelectorConfig(options=[], multiple=True, custom_value=True)),
            vol.Required("full_access_all_devices", default=o["full_access_all_devices"]): bool,
            vol.Required("unlock_protected_points", default=o["unlock_protected_points"]): bool,
            vol.Required("program_watch", default=o["program_watch"]): bool,
            vol.Required("program_watch_time", default=o["program_watch_time"]): selector.TimeSelector(),
            vol.Required("alarm_poll_s", default=o["alarm_poll_s"]): selector.NumberSelector(selector.NumberSelectorConfig(min=0, max=3600, step=10, unit_of_measurement="s", mode="box")),
            vol.Optional("mstp_routers", default=o.get("mstp_routers") or ""): selector.TextSelector(selector.TextSelectorConfig(multiline=True)),
        })

    async def async_step_controller(self, user_input=None):
        if user_input is not None:
            self._device = int(user_input["instance"])
            return await self.async_step_controller_settings()
        found = []
        if DOMAIN in self.hass.data:
            from . import t3000
            for d in t3000.list_devices(self.hass, self.hass.data[DOMAIN]["conf"].get("t3000", {})):
                found.append(selector.SelectOptionDict(value=str(d["instance"]), label=f"{d['name']} ({d['instance']})"))
        return self.async_show_form(step_id="controller", data_schema=vol.Schema({
            vol.Required("instance"): selector.SelectSelector(selector.SelectSelectorConfig(options=found, custom_value=True, mode="dropdown")),
        }))

    async def async_step_controller_settings(self, user_input=None):
        devices = dict(self._opts.get("devices") or {})
        cur = {"access": "restricted", "allow_program_read": True, "allow_point_writes": False, "program_write_slots": "", "sandbox": "",
               "locked": "", "model": "", "address": "", **devices.get(str(self._device), {})}
        if user_input is not None:
            if user_input.pop("remove", False):
                devices.pop(str(self._device), None)
            else:
                devices[str(self._device)] = {k: v for k, v in user_input.items()}
            return self._save(devices=devices)
        return self.async_show_form(step_id="controller_settings", description_placeholders={"instance": str(self._device)}, data_schema=vol.Schema({
            vol.Required("access", default=cur["access"]): selector.SelectSelector(selector.SelectSelectorConfig(options=["restricted", "full"])),
            vol.Required("allow_program_read", default=cur["allow_program_read"]): bool,
            vol.Required("allow_point_writes", default=cur["allow_point_writes"]): bool,
            vol.Optional("program_write_slots", default=cur["program_write_slots"]): str,
            vol.Optional("sandbox", default=cur["sandbox"]): str,
            vol.Optional("locked", default=cur["locked"]): str,
            vol.Optional("model", default=cur["model"]): selector.SelectSelector(selector.SelectSelectorConfig(options=MODELS, mode="dropdown")),
            vol.Optional("address", default=cur["address"]): str,
            vol.Optional("remove", default=False): bool,
        }))

    async def async_step_locked(self, user_input=None):
        if user_input is not None:
            for line in (user_input.get("locked_equipment") or "").splitlines():
                if line.strip() and ":" in line:
                    try:
                        re.compile(line.split(":", 1)[1].strip())
                    except re.error:
                        return self.async_show_form(step_id="locked", data_schema=self._locked_schema(user_input), errors={"locked_equipment": "bad_pattern"})
            return self._save(**user_input)
        return self.async_show_form(step_id="locked", data_schema=self._locked_schema(self._opts))

    def _locked_schema(self, o: dict) -> vol.Schema:
        return vol.Schema({vol.Optional("locked_equipment", default=o.get("locked_equipment") or ""): selector.TextSelector(selector.TextSelectorConfig(multiline=True))})

    async def async_step_nodered(self, user_input=None):
        nr = self._opts.get("nodered") or {}
        if user_input is not None:
            return self._save(nodered=user_input if user_input.get("url") else {})
        return self.async_show_form(step_id="nodered", data_schema=vol.Schema({
            vol.Optional("url", default=nr.get("url", "")): str,
            vol.Optional("ha_server_id", default=nr.get("ha_server_id", "")): str,
            vol.Optional("auth", default=nr.get("auth", "none")): selector.SelectSelector(selector.SelectSelectorConfig(options=["none", "token", "basic"])),
            vol.Optional("username", default=nr.get("username", "")): str,
            vol.Optional("password", default=nr.get("password", "")): selector.TextSelector(selector.TextSelectorConfig(type="password")),
            vol.Optional("verify_ssl", default=nr.get("verify_ssl", True)): bool,
        }))
