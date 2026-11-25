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


def locked_items(value: Any) -> list[dict]:
    """Locked equipment as stored in the options: a list of {name, pattern, tag?, exclude_from_sheets} (current) or
    'name: pattern' lines (0.9.4-0.9.6)."""
    if isinstance(value, list):
        out = []
        for i in value:
            item = {"name": str(i["name"]), "pattern": str(i["pattern"]), "exclude_from_sheets": bool(i.get("exclude_from_sheets", False))}
            if i.get("tag"):
                item["tag"] = str(i["tag"])
            out.append(item)
        return out
    out = []
    for line in (value or "").splitlines():
        if ":" in line:
            name, pattern = line.split(":", 1)
            if name.strip() and pattern.strip():
                out.append({"name": name.strip(), "pattern": pattern.strip(), "exclude_from_sheets": False})
    return out


def options_to_conf(options: dict) -> dict:
    """Options saved by the UI -> the same structure as the YAML configuration (validated by CONFIG_SCHEMA)."""
    o = {**DEFAULTS, **options}
    routers = {}
    for part in re.split(r"[,\n]+", o.get("mstp_routers") or ""):
        m = ROUTER_RE.match(part)
        if m:
            routers[int(m.group(1))] = m.group(2)
    locked = locked_items(o.get("locked_equipment"))
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
        if d.get("allow_editing_physical_outputs"):
            dev["allow_editing_physical_outputs"] = True
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


def conf_to_options(conf: dict) -> dict:
    """Validated YAML configuration -> UI options that give back exactly the same configuration (YAML import)."""
    t3 = conf.get("t3000") or {}
    devices = {}
    for inst, d in (t3.get("devices") or {}).items():
        devices[str(inst)] = {"access": d.get("access", "restricted"), "allow_program_read": bool(d.get("allow_program_read", False)),
                              "allow_point_writes": bool(d.get("allow_point_writes", False)),
                              "program_write_slots": ", ".join(str(x) for x in d.get("program_write_slots") or []),
                              "sandbox": ", ".join(d.get("sandbox") or []), "locked": ", ".join(d.get("locked") or []),
                              "model": d.get("model") or "", "address": d.get("address") or "",
                              "allow_editing_physical_outputs": bool(d.get("allow_editing_physical_outputs", False))}
    wt = t3.get("program_watch_time", "02:30:00")
    out = {"admins_are_editors": bool(conf.get("admins_are_editors", True)), "editors": list(conf.get("editors") or []),
           "full_access_all_devices": bool(t3.get("full_access_all_devices", False)), "unlock_protected_points": bool(t3.get("unlock_protected_points", False)),
           "program_watch": bool(t3.get("program_watch", True)), "program_watch_time": wt.strftime("%H:%M:%S") if hasattr(wt, "strftime") else str(wt),
           "alarm_poll_s": int(t3.get("alarm_poll_s", 60)),
           "mstp_routers": "\n".join(f"{k}={v}" for k, v in (t3.get("mstp_routers") or {}).items()),
           "locked_equipment": locked_items(t3.get("locked_equipment") or []), "devices": devices}
    nr = conf.get("nodered_test")
    if nr:
        out["nodered"] = {k: v for k, v in nr.items() if v not in (None, "")}
    return out


class BasWireSheetConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_import(self, import_data: dict):
        """YAML configuration copied into a UI entry. It stays inactive while the YAML is present; once the YAML file
        is removed and Home Assistant restarts, the entry carries exactly the same settings."""
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        return self.async_create_entry(title="BAS Wire Sheet", data={"imported_from_yaml": True}, options={**DEFAULTS, **import_data})

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
                devices[str(self._device)] = {**devices.get(str(self._device), {}), **user_input}  # keeps settings the form does not show
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
        items = locked_items(self._opts.get("locked_equipment"))
        listing = "\n".join(f"- {i['name']}" + (f" ({i['tag']})" if i.get("tag") else "") + f": `{i['pattern']}`"
                            + (" · kept out of monitoring sheets" if i.get("exclude_from_sheets") else "") for i in items) or "None."
        return self.async_show_menu(step_id="locked", menu_options=["locked_add", "locked_remove"] if items else ["locked_add"],
                                    description_placeholders={"items": listing})

    async def async_step_locked_add(self, user_input=None):
        errors = {}
        if user_input is not None:
            try:
                re.compile(user_input["pattern"])
            except re.error:
                errors["pattern"] = "bad_pattern"
            if not errors:
                item = {"name": user_input["name"].strip(), "pattern": user_input["pattern"].strip(),
                        "exclude_from_sheets": bool(user_input.get("exclude_from_sheets"))}
                if (user_input.get("tag") or "").strip():
                    item["tag"] = user_input["tag"].strip()
                return self._save(locked_equipment=locked_items(self._opts.get("locked_equipment")) + [item])
        return self.async_show_form(step_id="locked_add", errors=errors, data_schema=vol.Schema({
            vol.Required("name"): str, vol.Required("pattern"): str, vol.Optional("tag", default=""): str,
            vol.Optional("exclude_from_sheets", default=False): bool}))

    async def async_step_locked_remove(self, user_input=None):
        items = locked_items(self._opts.get("locked_equipment"))
        if user_input is not None:
            drop = set(user_input.get("remove") or [])
            return self._save(locked_equipment=[i for n, i in enumerate(items) if str(n) not in drop])
        return self.async_show_form(step_id="locked_remove", data_schema=vol.Schema({
            vol.Optional("remove", default=[]): selector.SelectSelector(selector.SelectSelectorConfig(
                options=[selector.SelectOptionDict(value=str(n), label=i["name"]) for n, i in enumerate(items)], multiple=True))}))

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
