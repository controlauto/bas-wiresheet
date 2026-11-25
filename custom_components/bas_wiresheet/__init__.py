"""BAS Wire Sheet: visual monitoring logic editor for Home Assistant, executed by Node-RED."""
from __future__ import annotations

import asyncio
import base64
import binascii
import copy
import difflib
import hashlib
import json
import logging
import re
import secrets
import struct
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import voluptuous as vol

from homeassistant.components import panel_custom, websocket_api
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from homeassistant.helpers.dispatcher import async_dispatcher_connect, async_dispatcher_send
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers import discovery
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import CoreState

from .compiler import compile_sheet, diff_flows, load_runtime, tab_id, to_flow_payload
from .const import (
    CONF_EDITORS, CONF_NODERED_TEST, DOMAIN, PANEL_ICON, PANEL_TITLE, PANEL_URL_PATH,
    PREVIEW_TTL_S, STATIC_URL, TARGET_PRODUCTION, TARGET_TEST, VERSION,
)
from . import t3000, t3000_alarms, t3000_bacnet, t3000_basic, t3000_import, t3000_points, t3000_policy, t3000_settings, t3000_snapshot, t3000_tables, t3000_toolchain
from .controller_tools import AlarmWatch, SnapshotStore
from .t3000_policy import make_guard
from .program_watch import ProgramWatch
from .sheet_runner import SheetRunner
from .nodered import NodeRedClient, NodeRedError
from .repository import RepoError, SheetRepository
from .validation import ID_RE, has_errors, load_catalog, validate_sheet

_LOGGER = logging.getLogger(__name__)

NODERED_SCHEMA = vol.Schema({
    vol.Required("url"): cv.url,
    vol.Required("ha_server_id"): cv.string,
    vol.Optional("auth", default="none"): vol.In(["none", "token", "basic"]),
    vol.Optional("username"): cv.string,
    vol.Optional("password"): cv.string,
    vol.Optional("verify_ssl", default=True): cv.boolean,
    vol.Optional("name", default="Test Node-RED"): cv.string,
})

T3_RANGE = vol.Match(r"^(IN|OUT|VAR)\d+(-\d+)?$")
T3_DEVICE_SCHEMA = vol.Schema({
    vol.Optional("model"): vol.In(list(t3000.MODEL_PROFILES)),
    vol.Optional("locked", default=[]): vol.All(cv.ensure_list, [vol.All(cv.string, vol.Upper, T3_RANGE)]),
    vol.Optional("sandbox", default=[]): vol.All(cv.ensure_list, [vol.All(cv.string, vol.Upper, T3_RANGE)]),
    # Controller program access is off unless enabled per device and per program slot.
    vol.Optional("allow_program_read", default=False): cv.boolean,
    vol.Optional("program_write_slots", default=[]): vol.All(cv.ensure_list, [vol.All(vol.Coerce(int), vol.Range(min=1, max=16))]),
    # Editing a program that already writes physical or locked points needs this second, explicit switch.
    vol.Optional("allow_editing_physical_outputs", default=False): cv.boolean,
    # IP controller: "192.0.2.10[:47808]". MS/TP controller behind a router: "router_ip[:port]/network:mac".
    vol.Optional("address"): vol.Match(r"^[0-9.]+(:\d+)?(/\d+:(\d+|0x[0-9a-fA-F]+))?$"),
    # Explorer may write a value only to sandbox points on virtual ports of a device that enables this.
    vol.Optional("allow_point_writes", default=False): cv.boolean,
    # "full": every program slot and every point may be changed, physical outputs included. Safety points
    # (interlocks, limits, freeze, fire, smoke, emergency stop) and Commissioning Pending equipment stay locked.
    vol.Optional("access", default="restricted"): vol.In(["restricted", "full"]),
})
T3000_SCHEMA = vol.Schema({vol.Optional("devices", default={}): {vol.Coerce(int): T3_DEVICE_SCHEMA},
                           # Applies "full" access to every BACnet controller in Home Assistant, including ones added later.
                           vol.Optional("full_access_all_devices", default=False): cv.boolean,
                           # BACnet network number -> router IP[:port], for TEMCO controllers on MS/TP (address "network:mac").
                           vol.Optional("mstp_routers", default={}): {vol.Coerce(int): vol.Match(r"^[0-9.]+(:\d+)?$")},
                           # Operator decision: safety-pattern points and Commissioning Pending equipment are no longer locked.
                           vol.Optional("unlock_protected_points", default=False): cv.boolean,
                           # Site equipment to lock (for example Commissioning Pending): name, regex pattern on labels/entity ids/programs.
                           vol.Optional("locked_equipment", default=[]): [vol.Schema({vol.Required("name"): cv.string, vol.Required("pattern"): cv.string,
                                                                                      vol.Optional("tag"): cv.string,
                                                                                      vol.Optional("exclude_from_sheets", default=False): cv.boolean})],
                           # Nightly read of every readable controller's programs; changes made outside this panel are reported.
                           vol.Optional("program_watch", default=True): cv.boolean,
                           vol.Optional("alarm_poll_s", default=60): vol.All(vol.Coerce(int), vol.Range(min=0, max=3600)),
                           vol.Optional("program_watch_time", default="02:30:00"): cv.time})

CONFIG_SCHEMA = vol.Schema({
    DOMAIN: vol.Schema({
        vol.Optional(CONF_EDITORS, default=[]): vol.All(cv.ensure_list, [cv.string]),
        vol.Optional("admins_are_editors", default=True): cv.boolean,
        vol.Optional(CONF_NODERED_TEST): NODERED_SCHEMA,
        vol.Optional("t3000", default={}): T3000_SCHEMA,
    })
}, extra=vol.ALLOW_EXTRA)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    conf = config.get(DOMAIN)
    if conf is None:
        return True  # set up from the UI (config entry) instead
    hass.data.setdefault(f"{DOMAIN}_yaml", True)
    return await _async_setup_core(hass, conf, config)


async def async_setup_entry(hass: HomeAssistant, entry) -> bool:
    """UI configuration. When YAML configuration exists it wins and the entry only says so."""
    if hass.data.get(f"{DOMAIN}_yaml"):
        _LOGGER.warning("BAS Wire Sheet is configured in YAML; the settings of the UI entry are not used")
        return True
    from .config_flow import options_to_conf
    conf = CONFIG_SCHEMA({DOMAIN: options_to_conf(dict(entry.options))})[DOMAIN]
    if DOMAIN in hass.data:  # already running (options changed): apply the new settings in place
        _apply_conf(hass, conf)
    else:
        await _async_setup_core(hass, conf, {})
    entry.async_on_unload(entry.add_update_listener(_entry_updated))
    return True


async def _entry_updated(hass: HomeAssistant, entry) -> None:
    from .config_flow import options_to_conf
    _apply_conf(hass, CONFIG_SCHEMA({DOMAIN: options_to_conf(dict(entry.options))})[DOMAIN])


async def async_unload_entry(hass: HomeAssistant, entry) -> bool:
    return True  # the panel stays registered until Home Assistant restarts; new settings apply on reload


def _apply_conf(hass: HomeAssistant, conf: dict) -> None:
    """Access and controller settings take effect at once (they are read on every command)."""
    data = hass.data[DOMAIN]
    data["conf"] = conf
    t3000_policy.set_site_patterns(conf.get("t3000", {}).get("locked_equipment", []))
    from .validation import set_excluded_equipment
    set_excluded_equipment(conf.get("t3000", {}).get("locked_equipment", []))
    watch = data.get("alarm_watch")
    if watch is not None:
        watch.stop()
        watch.interval_s = conf.get("t3000", {}).get("alarm_poll_s", 60)
        watch.start()
    data.pop("t3_settings", None)


async def _async_setup_core(hass: HomeAssistant, conf: dict, config: ConfigType) -> bool:
    await hass.async_add_executor_job(load_catalog)
    await hass.async_add_executor_job(load_runtime)
    repo = SheetRepository(hass)
    await repo.async_load()
    data: dict[str, Any] = {
        "repo": repo, "conf": conf, "previews": {}, "live": {},
        "nodered": {TARGET_TEST: NodeRedClient(async_get_clientsession(hass), {**conf[CONF_NODERED_TEST]})} if CONF_NODERED_TEST in conf else {},
    }
    hass.data[DOMAIN] = data
    data["templates"] = await hass.async_add_executor_job(_load_templates)
    t3000_policy.set_site_patterns(conf.get("t3000", {}).get("locked_equipment", []))
    from .validation import set_excluded_equipment
    set_excluded_equipment(conf.get("t3000", {}).get("locked_equipment", []))
    runner = SheetRunner(hass, SIGNAL_LIVE, data["live"], repo.add_audit)
    await runner.async_load()
    data["runner"] = runner
    hass.async_create_task(discovery.async_load_platform(hass, "sensor", DOMAIN, {}, config))

    async def _start_runtimes(_event=None) -> None:
        for s in repo.all_sheets():
            hr = s.get("ha_runtime") or {}
            if hr.get("enabled"):
                try:
                    rev = repo.get_revision(s["id"], hr["rev"])["sheet"]
                    await runner.start(s["id"], rev, hr["rev"], hr.get("by", "?"))
                except (RepoError, ValueError, KeyError) as err:
                    _LOGGER.warning("BAS Wire Sheet: sheet %s could not start in Home Assistant: %s", s["id"], err)

    async def _save_runtimes(_event=None) -> None:
        for sid in list(runner.sheets):
            await runner.stop(sid)

    if hass.state is CoreState.running:
        hass.async_create_task(_start_runtimes())
    else:
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _start_runtimes)
    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _save_runtimes)
    watch = ProgramWatch(hass)
    await watch.async_load()
    data["watch"] = watch
    snapshots = SnapshotStore(hass)
    await snapshots.async_load()
    data["snapshots"] = snapshots

    def _alarm_devices() -> list[dict]:
        out = []
        for d in t3000.list_devices(hass, conf.get("t3000", {})):
            if (_t3_effective(hass, d["instance"]) or {}).get("allow_program_read"):
                out.append(d)
        return out

    async def _alarm_run(instance: int, fn):
        return await _t3_run(hass, instance, _t3_require(hass, instance), fn)

    alarm_watch = AlarmWatch(hass, _alarm_devices, _alarm_run, repo.add_audit, conf.get("t3000", {}).get("alarm_poll_s", 60))
    data["alarm_watch"] = alarm_watch
    @callback
    def _alarms_start(_event=None) -> None:
        alarm_watch.start()

    @callback
    def _alarms_stop(_event=None) -> None:
        alarm_watch.stop()

    if hass.state is CoreState.running:
        _alarms_start()
    else:
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _alarms_start)
    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _alarms_stop)
    t3conf = conf.get("t3000", {})
    if t3conf.get("program_watch", True):
        at = t3conf.get("program_watch_time")

        async def _nightly(now) -> None:
            await _watch_run(hass, "nightly")

        async_track_time_change(hass, _nightly, hour=at.hour, minute=at.minute, second=at.second)

    frontend_dir = Path(__file__).parent / "frontend"
    runtime_dir = Path(__file__).parent / "runtime"
    await hass.http.async_register_static_paths([
        StaticPathConfig(f"{STATIC_URL}/frontend", str(frontend_dir), True),
        StaticPathConfig(f"{STATIC_URL}/runtime", str(runtime_dir), True),
    ])
    asset_v = await hass.async_add_executor_job(_asset_hash, frontend_dir / "bas-wiresheet-panel.js")
    await panel_custom.async_register_panel(
        hass,
        frontend_url_path=PANEL_URL_PATH,
        webcomponent_name="bas-wiresheet-panel",
        sidebar_title=PANEL_TITLE,
        sidebar_icon=PANEL_ICON,
        module_url=f"{STATIC_URL}/frontend/bas-wiresheet-panel.js?v={asset_v}",
        require_admin=False,
        config={"version": VERSION},
    )
    for handler in (ws_catalog, ws_list, ws_get, ws_validate, ws_save_draft, ws_import, ws_commit, ws_restore,
                    ws_deploy_preview, ws_deploy, ws_rollback, ws_target_status, ws_subscribe_live, ws_push_result,
                    ws_t3_devices, ws_t3_points, ws_t3_compile, ws_delete_sheet, ws_t3_program_list, ws_t3_program_read,
                    ws_t3_upload_plan, ws_t3_upload_apply, ws_t3_program_control, ws_t3_program_to_sheet, ws_t3_prog_file,
                    ws_t3_code_check, ws_t3_code_apply, ws_t3_code_restore, ws_t3_program_state, ws_t3_point_write, ws_t3_point_table,
                    ws_t3_discover, ws_sheet_access, ws_live_states, ws_history, ws_point_history,
                    ws_program_watch_status, ws_program_watch_run, ws_sheet_run, ws_sheet_run_status, ws_templates, ws_program_export, ws_t3_point_config, ws_t3_point_config_write,
                    ws_t3_schedules, ws_t3_schedule_get, ws_t3_schedule_set, ws_t3_pids, ws_t3_pid_set, ws_t3_settings, ws_t3_port_set, ws_t3_send_check, ws_t3_send_apply, ws_t3_snapshot_take, ws_t3_job, ws_t3_snapshots,
                    ws_t3_snapshot_compare, ws_t3_snapshot_download, ws_t3_snapshot_update, ws_t3_snapshot_restore, ws_t3_alarms, ws_t3_alarm_set,
                    ws_t3_monitors, ws_t3_point_values):
        websocket_api.async_register_command(hass, handler)
    return True


def _asset_hash(p: Path) -> str:
    try:
        return hashlib.sha1(p.read_bytes()).hexdigest()[:10]
    except OSError:
        return VERSION


# ---------------- permissions ----------------

def _role(hass: HomeAssistant, user) -> str:
    conf = hass.data[DOMAIN]["conf"]
    if user is None:
        return "operator"
    if user.id in conf[CONF_EDITORS] or (conf["admins_are_editors"] and user.is_admin):
        return "editor"
    return "operator"


def _require_editor(hass, connection, msg) -> bool:
    if _role(hass, connection.user) != "editor":
        connection.send_error(msg["id"], "unauthorized", "Editor permission is required for this action")
        return False
    return True


def _sheet_allowed(hass, connection, msg, sheet_id) -> bool:
    """Per-sheet permission: a sheet with an editor list can only be changed by those users (and administrators)."""
    if not isinstance(sheet_id, str):
        return True
    allowed = _repo(hass).editors_of(sheet_id)
    u = connection.user
    if allowed and not (u and (u.is_admin or u.id in allowed)):
        connection.send_error(msg["id"], "unauthorized", "This sheet can only be changed by the users listed in its access settings")
        return False
    return True


def _user_label(connection) -> str:
    u = connection.user
    return u.name if u and u.name else (u.id if u else "unknown")


def _known_entities(hass: HomeAssistant) -> dict[str, dict]:
    return {s.entity_id: {"unit": s.attributes.get("unit_of_measurement", "") or ""} for s in hass.states.async_all()}


def _repo(hass) -> SheetRepository:
    return hass.data[DOMAIN]["repo"]


# ---------------- read commands (operator + editor) ----------------

@websocket_api.websocket_command({vol.Required("type"): "bas_wiresheet/catalog"})
@websocket_api.async_response
async def ws_catalog(hass, connection, msg):
    catalog = await hass.async_add_executor_job(load_catalog)
    targets = [{"id": k, "name": c.name} for k, c in hass.data[DOMAIN]["nodered"].items()]
    connection.send_result(msg["id"], {
        "catalog": catalog, "role": _role(hass, connection.user), "version": VERSION,
        "runtime_url": f"{STATIC_URL}/runtime/runtime.js", "targets": targets,
        "production": {"available": False, "reason": "Production publish is locked pending explicit approval"},
    })


@websocket_api.websocket_command({vol.Required("type"): "bas_wiresheet/list"})
@callback
def ws_list(hass, connection, msg):
    connection.send_result(msg["id"], {"sheets": _repo(hass).list()})


@websocket_api.websocket_command({vol.Required("type"): "bas_wiresheet/get", vol.Required("sheet_id"): str, vol.Optional("revision"): int})
@callback
def ws_get(hass, connection, msg):
    repo = _repo(hass)
    try:
        d = repo.describe(msg["sheet_id"])
        if "revision" in msg:
            d["revision_sheet"] = repo.get_revision(msg["sheet_id"], msg["revision"])["sheet"]
    except RepoError as err:
        connection.send_error(msg["id"], "not_found", str(err))
        return
    allowed = d["access"]["editors"]
    u = connection.user
    d["can_edit"] = _role(hass, u) == "editor" and (not allowed or bool(u and (u.is_admin or u.id in allowed)))
    connection.send_result(msg["id"], d)


@websocket_api.websocket_command({vol.Required("type"): "bas_wiresheet/validate", vol.Required("sheet"): dict})
@callback
def ws_validate(hass, connection, msg):
    issues = validate_sheet(msg["sheet"], known_entities=_known_entities(hass))
    connection.send_result(msg["id"], {"valid": not has_errors(issues), "issues": issues})


# ---------------- T3000 Variable Explorer (read-only) ----------------

@websocket_api.websocket_command({vol.Required("type"): "bas_wiresheet/t3_devices"})
@callback
def ws_t3_devices(hass, connection, msg):
    conf = hass.data[DOMAIN]["conf"].get("t3000", {})
    devices = t3000.list_devices(hass, conf)
    for d in devices:
        d["programs"] = _t3_caps(_t3_effective(hass, d["instance"]))
    connection.send_result(msg["id"], {"devices": devices, "models": {k: v["source"] for k, v in t3000.MODEL_PROFILES.items()},
                                       "read_only": True})


@websocket_api.websocket_command({vol.Required("type"): "bas_wiresheet/t3_points", vol.Required("device_id"): str})
@callback
def ws_t3_points(hass, connection, msg):
    conf = hass.data[DOMAIN]["conf"].get("t3000", {})
    try:
        res = t3000.list_points(hass, conf, msg["device_id"])
    except ValueError as err:
        connection.send_error(msg["id"], "not_found", str(err))
        return
    cfg = _t3_effective(hass, res["device"]["instance"]) or {}
    full = cfg.get("access") == "full"
    res["device"]["access"] = "full" if full else "restricted"
    for r in res["points"]:
        domain = r["entity_id"].split(".")[0]
        r["writable"] = bool(cfg.get("allow_point_writes")) and r["protection"] != "locked" and domain in ("number", "switch") and (
            full or (r["protection"] == "sandbox" and r["port"] == "virtual"))
    connection.send_result(msg["id"], res)


SIGNAL_LIVE = "bas_wiresheet_live_{}"
MAX_RESULT_BYTES = 256_000


def _t3_compile(sheet: dict, devices: dict, guard=None) -> dict:
    gen = t3000_basic.generate(sheet, guard or make_guard(devices, sheet["t3000"]["device_instance"]))
    out = {k: gen[k] for k in ("source", "issues", "reads", "writes", "work_vars", "state_map", "modbus_reads", "panel")}
    if gen["source"]:
        try:
            built = t3000_toolchain.build(gen["source"])
            out.update({k: built[k] for k in ("length", "sha256", "listing", "code_hex")})
            out["logic"] = t3000_import.logic_digest(bytes.fromhex(built["code_hex"]))
        except t3000_toolchain.ToolchainError as err:
            out["issues"] = [*out["issues"], {"severity": "error", "code": "t3_compile", "message": str(err)}]
    return out


@websocket_api.websocket_command({vol.Required("type"): "bas_wiresheet/t3_compile", vol.Required("sheet"): dict})
@websocket_api.async_response
async def ws_t3_compile(hass, connection, msg):
    """Compile only. Nothing is sent to a controller from Home Assistant."""
    sheet = msg["sheet"]
    issues = validate_sheet(sheet, known_entities=_known_entities(hass))
    if sheet.get("mode") != "t3000":
        issues = [*issues, {"severity": "error", "code": "mode", "message": "This is not a T3000 program sheet"}]
    if has_errors(issues):
        connection.send_result(msg["id"], {"issues": issues})
        return
    devices = hass.data[DOMAIN]["conf"].get("t3000", {}).get("devices", {})
    guard = _t3_guard(hass, sheet["t3000"]["device_instance"]) if isinstance(sheet.get("t3000"), dict) else None
    panel_issue = await _t3_fill_panel(hass, sheet)
    result = await hass.async_add_executor_job(_t3_compile, sheet, devices, guard)
    if panel_issue:
        result["issues"] = [*result.get("issues", []), panel_issue]
    connection.send_result(msg["id"], result)


def _uses_modbus(sheet: dict) -> bool:
    return any(b.get("type") in ("modbus_read", "modbus_write") for b in sheet.get("blocks", []))


async def _t3_settings(hass, instance: int, refresh: bool = False) -> dict:
    """Controller settings (panel number, ports), cached for 10 minutes."""
    cache = hass.data[DOMAIN].setdefault("t3_settings", {})
    hit = cache.get(instance)
    if hit and not refresh and time.monotonic() - hit[0] < 600:
        return hit[1]
    cfg = _t3_require(hass, instance)
    data = await _t3_run(hass, instance, cfg, lambda c: t3000_settings.read_settings(c, instance))
    cache[instance] = (time.monotonic(), data)
    return data


async def _t3_fill_panel(hass, sheet: dict) -> dict | None:
    """Modbus points name the controller's own panel number: take it from the controller when the sheet has none."""
    t3 = sheet.get("t3000")
    if not isinstance(t3, dict) or t3.get("panel") or not _uses_modbus(sheet):
        return None
    try:
        t3["panel"] = (await _t3_settings(hass, int(t3["device_instance"])))["panel"]
    except (*T3_ERRORS, KeyError, ValueError, TypeError) as err:
        return {"severity": "warning", "code": "t3_panel", "message": f"Could not read the controller's panel number: {err}"}
    return None


# ---------------- T3000 controller programs ----------------
# Off by default. A device needs allow_program_read; each writable slot must be in program_write_slots.

class T3AccessError(Exception):
    """The operation is not enabled for this device or slot."""


def _t3_caps(cfg: dict | None) -> dict:
    cfg = cfg or {}
    return {"read": bool(cfg.get("allow_program_read")), "write_slots": sorted(cfg.get("program_write_slots", [])),
            "point_writes": bool(cfg.get("allow_point_writes")), "full": cfg.get("access") == "full"}


def _t3_effective(hass, instance: int) -> dict | None:
    """Device access after applying full access (per device, or to every controller with full_access_all_devices)."""
    t3 = hass.data[DOMAIN]["conf"].get("t3000", {})
    cfg = t3.get("devices", {}).get(instance)
    all_full = bool(t3.get("full_access_all_devices")) and _is_temco(hass, instance)
    if cfg is None and not all_full:
        return None
    cfg = dict(cfg or {})
    if all_full or cfg.get("access") == "full":
        cfg.update({"access": "full", "allow_program_read": True, "program_write_slots": list(range(1, 17)),
                    "allow_editing_physical_outputs": True, "allow_point_writes": True})
    return cfg


def _is_temco(hass, instance: int) -> bool:
    """T3000 access only applies to TEMCO controllers (not UPS units or other BACnet devices)."""
    for d in t3000.list_devices(hass, {}, include_all=True):
        if d["instance"] == instance:
            return t3000.is_temco(d.get("manufacturer"), d.get("reported_model"))
    return False


def _watch_devices(hass) -> list[int]:
    out = [d["instance"] for d in t3000.list_devices(hass, {}) if (_t3_effective(hass, d["instance"]) or {}).get("allow_program_read")]
    return sorted(set(out))


async def _watch_run(hass, reason: str) -> dict:
    async def read_fn(inst, fn):
        cfg = _t3_require(hass, inst)
        return await _t3_run(hass, inst, cfg, fn)
    return await hass.data[DOMAIN]["watch"].run(_watch_devices(hass), read_fn, _repo(hass).add_audit, reason)


async def _panel_changed(hass, instance: int, slot: int) -> None:
    await hass.data[DOMAIN]["watch"].note_panel_change(instance, slot)


@websocket_api.websocket_command({vol.Required("type"): "bas_wiresheet/sheet_run", vol.Required("sheet_id"): str,
                                  vol.Required("enabled"): bool, vol.Optional("revision"): int})
@websocket_api.async_response
async def ws_sheet_run(hass, connection, msg):
    """Run a monitoring sheet revision continuously inside Home Assistant, or stop it. Monitoring only."""
    if not _require_editor(hass, connection, msg) or not _sheet_allowed(hass, connection, msg, msg["sheet_id"]):
        return
    repo, runner = _repo(hass), hass.data[DOMAIN]["runner"]
    try:
        s = repo.get(msg["sheet_id"])
        if not msg["enabled"]:
            await runner.stop(msg["sheet_id"])
            await repo.set_ha_runtime(msg["sheet_id"], None)
            await repo.add_audit({"by": _user_label(connection), "action": "sheet_run_stop", "target": None, "sheet": msg["sheet_id"], "ok": True,
                                  "detail": "Stopped running in Home Assistant"})
            connection.send_result(msg["id"], {"running": False})
            return
        if not s["revisions"]:
            raise RepoError("Save a revision first")
        rev = msg.get("revision") or s["revisions"][-1]["rev"]
        sheet = repo.get_revision(msg["sheet_id"], rev)["sheet"]
    except RepoError as err:
        connection.send_error(msg["id"], "not_found", str(err))
        return
    if sheet.get("mode") != "monitoring":
        connection.send_error(msg["id"], "invalid", "Only monitoring sheets run in Home Assistant; T3000 programs run on the controller")
        return
    if has_errors(validate_sheet(sheet, known_entities=_known_entities(hass))):
        connection.send_error(msg["id"], "invalid", f"Revision r{rev} has validation errors")
        return
    try:
        restored = await runner.start(msg["sheet_id"], sheet, rev, _user_label(connection))
    except ValueError as err:
        connection.send_error(msg["id"], "invalid", str(err))
        return
    await repo.set_ha_runtime(msg["sheet_id"], {"enabled": True, "rev": rev, "since": datetime.now(UTC).isoformat(), "by": _user_label(connection)})
    await repo.add_audit({"by": _user_label(connection), "action": "sheet_run_start", "target": None, "sheet": msg["sheet_id"], "ok": True,
                          "detail": f"Running r{rev} in Home Assistant" + (f" ({restored} timer state(s) restored)" if restored else "")})
    connection.send_result(msg["id"], {"running": True, "revision": rev, "restored": restored,
                                       "entity_id": f"sensor.bas_wiresheet_{msg['sheet_id'].replace('-', '_')}"})


@websocket_api.websocket_command({vol.Required("type"): "bas_wiresheet/sheet_run_status"})
@callback
def ws_sheet_run_status(hass, connection, msg):
    connection.send_result(msg["id"], {"sheets": hass.data[DOMAIN]["runner"].status()})


def _load_templates() -> list[dict]:
    out = []
    for f in sorted((Path(__file__).parent / "templates").glob("*.json")):
        try:
            out.append(json.loads(f.read_text()))
        except (OSError, ValueError) as err:
            _LOGGER.warning("BAS Wire Sheet: template %s not loaded: %s", f.name, err)
    return out


@websocket_api.websocket_command({vol.Required("type"): "bas_wiresheet/templates"})
@callback
def ws_templates(hass, connection, msg):
    connection.send_result(msg["id"], {"templates": hass.data[DOMAIN]["templates"]})


def _program_export_zip(instance: int, name: str, programs: list[dict], source: str) -> tuple[bytes, dict]:
    """Zip of T3000 program text files (T3000 program editor > Load File) plus exact byte code. Runs in the executor."""
    import io
    import zipfile
    mem, meta = io.BytesIO(), {"controller": instance, "name": name, "source": source, "exported_at": datetime.now(UTC).isoformat(), "programs": []}
    with zipfile.ZipFile(mem, "w", zipfile.ZIP_DEFLATED) as z:
        for p in programs:
            code = p["code"]
            if not code:
                meta["programs"].append({"program": p["slot"], "empty": True})
                continue
            image = code.ljust(t3000_bacnet.CODE_SIZE, b"\0")
            listing = t3000_toolchain.decompile(image)
            safe = _listing_is_safe(image, listing)
            z.writestr(f"PRG{p['slot']:02d}.txt", listing.replace("\r\n", "\n").replace("\n", "\r\n").encode("latin-1", "replace"))
            meta["programs"].append({"program": p["slot"], "description": p.get("description", ""), "label": p.get("label", ""), "on": p.get("on"),
                                     "bytes": len(code), "sha256": hashlib.sha256(code).hexdigest(), "logic": t3000_import.logic_digest(image),
                                     "listing_compiles_to_same_logic": safe, "code_hex": code.hex()})
        unsafe = [x["program"] for x in meta["programs"] if x.get("listing_compiles_to_same_logic") is False]
        z.writestr("programs.json", json.dumps(meta, indent=1))
        z.writestr("README.txt", (
            f"T3000 programs of controller {instance} ({name}), {source}, exported {meta['exported_at']} by BAS Wire Sheet.\r\n\r\n"
            "PRGnn.txt: program text for the T3000 program editor (open the program in T3000, Load File, then Send).\r\n"
            "Loading a text file changes only that program; it never touches inputs, outputs, variables, schedules or users.\r\n"
            "programs.json: description, label, ON/OFF, size, SHA-256 and the exact byte code of each program.\r\n\r\n"
            + (f"WARNING: programs {', '.join(map(str, unsafe))}: T3000's listing does not compile back to the same logic "
               "(it drops brackets around AND/OR groups or divisors). Do not load those .txt files into a controller; "
               "use BAS Wire Sheet's Restore or the byte code in programs.json instead.\r\n" if unsafe else
               "Every listing compiles back to the same logic as the controller code.\r\n")
            + "\r\nThis is not a .prog file: a complete .prog (all tables, including user logins) is made by T3000 itself (File > Save).\r\n").encode())
    return mem.getvalue(), meta


@websocket_api.websocket_command({vol.Required("type"): "bas_wiresheet/program_export", vol.Required("instance"): int,
                                  vol.Optional("source", default="stored"): vol.In(["stored", "controller"])})
@websocket_api.async_response
async def ws_program_export(hass, connection, msg):
    """Download a controller's programs as T3000 program text files. 'controller' reads them now (read only)."""
    import base64
    from .program_watch import _read_all
    instance = msg["instance"]
    names = {d["instance"]: d["name"] for d in t3000.list_devices(hass, {})}
    try:
        cfg = _t3_require(hass, instance)
        if msg["source"] == "controller":
            raw = await _t3_run(hass, instance, cfg, _read_all(instance))
            programs = [{"slot": i + 1, "on": st["on"], "description": st["description"], "label": st["label"],
                         "code": code[:t3000_bacnet.code_used_length(code)]} for i, (st, code) in enumerate(raw)]
            source = "read from the controller"
        else:
            programs = hass.data[DOMAIN]["watch"].latest(instance)
            if not programs:
                raise T3AccessError("No stored copy yet: use Check Programs Now, or export from the controller")
            source = f"Program Watch copy of {datetime.fromtimestamp(programs[0]['read_at'] / 1000, UTC).isoformat()}"
        data, meta = await hass.async_add_executor_job(_program_export_zip, instance, names.get(instance, str(instance)), programs, source)
    except (*T3_ERRORS, IndexError, struct.error, t3000_import.Unsupported) as err:
        connection.send_error(msg["id"], "t3000", str(err))
        return
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M")
    connection.send_result(msg["id"], {"filename": f"{instance}-programs-{stamp}.zip", "zip_b64": base64.b64encode(data).decode(),
                                       "programs": [{k: v for k, v in x.items() if k != "code_hex"} for x in meta["programs"]]})


@websocket_api.websocket_command({vol.Required("type"): "bas_wiresheet/t3_point_config", vol.Required("instance"): int,
                                  vol.Required("category"): vol.In(["IN", "OUT", "VAR"]), vol.Required("index"): vol.All(int, vol.Range(min=1, max=128))})
@websocket_api.async_response
async def ws_t3_point_config(hass, connection, msg):
    """Read one point's configuration from the controller (description, label, auto/manual, units). Read only."""
    instance, kind, index = msg["instance"], msg["category"], msg["index"]

    def read(client):
        if client.identify() != instance:
            raise t3000_bacnet.UploadRefused("Wrong controller at this address")
        return t3000_points.decode(kind, t3000_points.read_struct(client, kind, index))

    try:
        cfg = _t3_require(hass, instance)
        result = await _t3_run(hass, instance, cfg, read)
    except (*T3_ERRORS, IndexError, struct.error) as err:
        connection.send_error(msg["id"], "t3000", str(err))
        return
    connection.send_result(msg["id"], {**result, "units_analog": t3000_points.VARIABLE_ANALOG_UNITS if kind == "VAR" else None,
                                       "units_digital": t3000_points.DIGITAL_UNITS if kind == "VAR" else None,
                                       "description_max": t3000_points.KINDS[kind][3] - 1, "label_max": t3000_points.LABEL_LEN - 1})


@websocket_api.websocket_command({vol.Required("type"): "bas_wiresheet/t3_point_config_write", vol.Required("instance"): int,
                                  vol.Required("category"): vol.In(["IN", "OUT", "VAR"]), vol.Required("index"): vol.All(int, vol.Range(min=1, max=128)),
                                  vol.Required("changes"): dict, vol.Required("confirm"): str})
@websocket_api.async_response
async def ws_t3_point_config_write(hass, connection, msg):
    """Change a point's description, label, auto/manual, digital/analog or units on the controller (read back to verify)."""
    if not _require_editor(hass, connection, msg):
        return
    instance, kind, index = msg["instance"], msg["category"], msg["index"]
    name = f"{kind}{index}"
    cfg = _t3_effective(hass, instance) or {}
    if cfg.get("access") != "full":
        connection.send_error(msg["id"], "not_allowed", f"Point configuration can be changed only with full access (device {instance})")
        return
    if msg["confirm"].strip().upper() != name:
        connection.send_error(msg["id"], "not_allowed", f"Type {name} to confirm")
        return
    t3conf = hass.data[DOMAIN]["conf"].get("t3000", {})
    texts = _t3_point_texts(hass, instance)
    status, reason = t3000_policy.protection(t3000_policy.device_configs(t3conf).get(instance), kind, index, f"{name} {texts.get(name, '')}",
                                             bool(t3conf.get("unlock_protected_points")))
    if status == "locked":
        connection.send_error(msg["id"], "not_allowed", f"{name} is locked ({reason})")
        return
    try:
        result = await _t3_run(hass, instance, cfg, lambda c: t3000_points.edit_point(c, instance, kind, index, msg["changes"]))
    except (*T3_ERRORS, IndexError, struct.error, ValueError) as err:
        await _repo(hass).add_audit({"by": _user_label(connection), "action": "point_config", "target": f"t3000:{instance}:{name}", "ok": False,
                                     "detail": f"{name} configuration change failed: {err}"})
        connection.send_error(msg["id"], "t3000", str(err))
        return
    diff = ", ".join(f"{k}: {result['before'].get(k)!r} -> {result['after'].get(k)!r}" for k in msg["changes"] if result["before"].get(k) != result["after"].get(k))
    await _repo(hass).add_audit({"by": _user_label(connection), "action": "point_config", "target": f"t3000:{instance}:{name}", "ok": True,
                                 "detail": f"{name}: {diff or 'no change'}"})
    connection.send_result(msg["id"], result)


@websocket_api.websocket_command({vol.Required("type"): "bas_wiresheet/t3_point_table", vol.Required("instance"): int,
                                  vol.Required("category"): vol.In(["IN", "OUT", "VAR"])})
@websocket_api.async_response
async def ws_t3_point_table(hass, connection, msg):
    """Read only: every IN, OUT or VAR of a controller straight from its point table (no Home Assistant entity needed)."""
    instance, kind = msg["instance"], msg["category"]
    t3conf = hass.data[DOMAIN]["conf"].get("t3000", {})
    texts = _t3_point_texts(hass, instance)
    devcfg = t3000_policy.device_configs(t3conf).get(instance)

    def read(client):
        if client.identify() != instance:
            raise t3000_bacnet.UploadRefused("Wrong controller at this address")
        rows = []
        for i, raw in enumerate(t3000_points.read_all(client, kind), start=1):
            d = t3000_points.decode(kind, raw)
            d.pop("value_raw", None)
            rows.append({"category": kind, "index": i, **d})
        return rows

    try:
        cfg = _t3_require(hass, instance)
        rows = await _t3_run(hass, instance, cfg, read)
    except (*T3_ERRORS, IndexError, struct.error) as err:
        connection.send_error(msg["id"], "t3000", str(err))
        return
    for r in rows:
        name = f"{kind}{r['index']}"
        r["protection"], r["protection_reason"] = t3000_policy.protection(devcfg, kind, r["index"], f"{name} {r['label']} {r['description']} {texts.get(name, '')}",
                                                                          bool(t3conf.get("unlock_protected_points")))
    connection.send_result(msg["id"], {"points": rows, "access": (_t3_effective(hass, instance) or {}).get("access")})


@websocket_api.websocket_command({vol.Required("type"): "bas_wiresheet/t3_settings", vol.Required("instance"): int})
@websocket_api.async_response
async def ws_t3_settings(hass, connection, msg):
    """Read only: panel number and RS-485 port modes / baud rates of a controller."""
    try:
        data = await _t3_settings(hass, msg["instance"], refresh=True)
    except (*T3_ERRORS, IndexError, struct.error) as err:
        connection.send_error(msg["id"], "t3000", str(err))
        return
    connection.send_result(msg["id"], {**data, "modes": t3000_settings.EDITABLE_MODES, "bauds": sorted(t3000_settings.BAUD_RATES)})


@websocket_api.websocket_command({vol.Required("type"): "bas_wiresheet/t3_port_set", vol.Required("instance"): int,
                                  vol.Required("port"): vol.In(t3000_settings.PORTS), vol.Optional("mode"): int, vol.Optional("baud"): int,
                                  vol.Required("confirm"): str})
@websocket_api.async_response
async def ws_t3_port_set(hass, connection, msg):
    """Change one RS-485 port's mode (Unused, MS/TP, Modbus Slave, Modbus Master) or baud rate. Full access, typed confirmation."""
    if not _require_editor(hass, connection, msg):
        return
    instance, port = msg["instance"], msg["port"]
    cfg = _t3_effective(hass, instance) or {}
    if cfg.get("access") != "full":
        connection.send_error(msg["id"], "not_allowed", f"Ports can be changed only with full access (device {instance})")
        return
    if msg["confirm"].strip().upper() != port:
        connection.send_error(msg["id"], "not_allowed", f"Type {port} to confirm")
        return
    try:
        result = await _t3_run(hass, instance, cfg, lambda c: t3000_settings.set_port(c, instance, port, msg.get("mode"), msg.get("baud")))
    except (*T3_ERRORS, IndexError, struct.error, ValueError) as err:
        await _repo(hass).add_audit({"by": _user_label(connection), "action": "port", "target": f"t3000:{instance}:{port}", "ok": False,
                                     "detail": f"{port} change failed: {err}"})
        connection.send_error(msg["id"], "t3000", str(err))
        return
    i = t3000_settings.PORTS.index(port)
    b, a = result["before"]["ports"][i], result["after"]["ports"][i]
    await _repo(hass).add_audit({"by": _user_label(connection), "action": "port", "target": f"t3000:{instance}:{port}", "ok": True,
                                 "detail": f"{port}: {b['mode']} {b['baud']} -> {a['mode']} {a['baud']}"})
    hass.data[DOMAIN].setdefault("t3_settings", {}).pop(instance, None)
    connection.send_result(msg["id"], result)


# ---------------- controller backups (snapshots) ----------------

def _snap(hass) -> SnapshotStore:
    return hass.data[DOMAIN]["snapshots"]


async def _run_snapshot_job(hass, job: dict, instance: int, by: str, note: str, reason: str) -> dict:
    def progress(n):
        job["done"] = n

    cfg = _t3_require(hass, instance)
    snap = await _t3_run(hass, instance, cfg, lambda c: t3000_snapshot.take(c, instance, progress))
    snap.update({"by": by, "note": note, "reason": reason})
    return await _snap(hass).add(snap)


@websocket_api.websocket_command({vol.Required("type"): "bas_wiresheet/t3_snapshot_take", vol.Required("instance"): int,
                                  vol.Optional("note", default=""): vol.All(str, vol.Length(max=200))})
@websocket_api.async_response
async def ws_t3_snapshot_take(hass, connection, msg):
    """Start a full read of the controller in the background. Read only on the controller."""
    if not _require_editor(hass, connection, msg):
        return
    instance = msg["instance"]
    try:
        _t3_require(hass, instance)
    except T3_ERRORS as err:
        connection.send_error(msg["id"], "t3000", str(err))
        return
    store = _snap(hass)
    if any(j["instance"] == instance and j["state"] == "running" for j in store.jobs.values()):
        connection.send_error(msg["id"], "busy", "A backup or restore of this controller is already running")
        return
    job = store.new_job(instance, "snapshot", t3000_snapshot.count_steps())
    by = _user_label(connection)

    async def run():
        try:
            snap = await _run_snapshot_job(hass, job, instance, by, msg["note"], "manual")
            job.update(state="done", result={"id": snap["id"]})
            await _repo(hass).add_audit({"by": by, "action": "snapshot", "target": f"t3000:{instance}", "ok": True, "detail": f"Full backup {snap['id']}"})
        except Exception as err:  # noqa: BLE001 - reported to the panel through the job
            job.update(state="failed", error=str(err))
            await _repo(hass).add_audit({"by": by, "action": "snapshot", "target": f"t3000:{instance}", "ok": False, "detail": f"Full backup failed: {err}"})

    hass.async_create_task(run())
    connection.send_result(msg["id"], {"job": job["id"], "total": job["total"]})


@websocket_api.websocket_command({vol.Required("type"): "bas_wiresheet/t3_job", vol.Required("job"): str})
@callback
def ws_t3_job(hass, connection, msg):
    job = _snap(hass).jobs.get(msg["job"])
    if not job:
        connection.send_error(msg["id"], "not_found", "Unknown job")
        return
    connection.send_result(msg["id"], {k: job[k] for k in ("id", "instance", "kind", "done", "total", "state", "result", "error")})


@websocket_api.websocket_command({vol.Required("type"): "bas_wiresheet/t3_snapshots", vol.Required("instance"): int})
@callback
def ws_t3_snapshots(hass, connection, msg):
    store = _snap(hass)
    running = [{k: j[k] for k in ("id", "kind", "done", "total", "state")} for j in store.jobs.values() if j["instance"] == msg["instance"] and j["state"] == "running"]
    connection.send_result(msg["id"], {"snapshots": list(reversed(store.list(msg["instance"]))), "jobs": running})


@websocket_api.websocket_command({vol.Required("type"): "bas_wiresheet/t3_snapshot_compare", vol.Required("instance"): int,
                                  vol.Required("before"): str, vol.Required("after"): str})
@websocket_api.async_response
async def ws_t3_snapshot_compare(hass, connection, msg):
    try:
        a, b = _snap(hass).get(msg["instance"], msg["before"]), _snap(hass).get(msg["instance"], msg["after"])
    except KeyError as err:
        connection.send_error(msg["id"], "not_found", str(err))
        return
    changes = await hass.async_add_executor_job(t3000_snapshot.compare, a, b, t3000_toolchain.decompile)
    connection.send_result(msg["id"], {"changes": changes, "before": {"id": a["id"], "taken_at": a["taken_at"]}, "after": {"id": b["id"], "taken_at": b["taken_at"]}})


@websocket_api.websocket_command({vol.Required("type"): "bas_wiresheet/t3_snapshot_download", vol.Required("instance"): int, vol.Required("snapshot"): str})
@callback
def ws_t3_snapshot_download(hass, connection, msg):
    try:
        connection.send_result(msg["id"], _snap(hass).get(msg["instance"], msg["snapshot"]))
    except KeyError as err:
        connection.send_error(msg["id"], "not_found", str(err))


@websocket_api.websocket_command({vol.Required("type"): "bas_wiresheet/t3_snapshot_update", vol.Required("instance"): int, vol.Required("snapshot"): str,
                                  vol.Optional("note"): vol.All(str, vol.Length(max=200)), vol.Optional("pinned"): bool,
                                  vol.Optional("delete", default=False): bool})
@websocket_api.async_response
async def ws_t3_snapshot_update(hass, connection, msg):
    if not _require_editor(hass, connection, msg):
        return
    store = _snap(hass)
    try:
        store.get(msg["instance"], msg["snapshot"])
        if msg["delete"]:
            await store.delete(msg["instance"], msg["snapshot"])
        else:
            await store.update(msg["instance"], msg["snapshot"], **{k: msg[k] for k in ("note", "pinned") if k in msg})
    except KeyError as err:
        connection.send_error(msg["id"], "not_found", str(err))
        return
    connection.send_result(msg["id"], {"ok": True})


@websocket_api.websocket_command({vol.Required("type"): "bas_wiresheet/t3_snapshot_restore", vol.Required("instance"): int, vol.Required("snapshot"): str,
                                  vol.Required("items"): vol.All([str], vol.Length(min=1, max=400)), vol.Required("confirm"): str})
@websocket_api.async_response
async def ws_t3_snapshot_restore(hass, connection, msg):
    """Write selected items of a backup back to the controller. A fresh full backup is taken first."""
    if not _require_editor(hass, connection, msg):
        return
    instance = msg["instance"]
    cfg = _t3_effective(hass, instance) or {}
    if cfg.get("access") != "full":
        connection.send_error(msg["id"], "not_allowed", f"Restoring needs full access (device {instance})")
        return
    if msg["confirm"].strip().upper() != "RESTORE":
        connection.send_error(msg["id"], "not_allowed", "Type RESTORE to confirm")
        return
    store = _snap(hass)
    try:
        snap = store.get(instance, msg["snapshot"])
    except KeyError as err:
        connection.send_error(msg["id"], "not_found", str(err))
        return
    t3conf = hass.data[DOMAIN]["conf"].get("t3000", {})
    devcfg = t3000_policy.device_configs(t3conf).get(instance)
    texts = _t3_point_texts(hass, instance)
    for item in msg["items"]:
        kind, _, num = item.partition(":")
        if kind not in t3000_snapshot.RESTORABLE or not num.isdigit():
            connection.send_error(msg["id"], "invalid", f"Cannot restore {item}")
            return
        if kind in ("OUT", "IN", "VAR"):
            status, reason = t3000_policy.protection(devcfg, kind, int(num), f"{kind}{num} {texts.get(f'{kind}{num}', '')}", bool(t3conf.get("unlock_protected_points")))
            if status == "locked":
                connection.send_error(msg["id"], "not_allowed", f"{kind}{num} is locked ({reason})")
                return
    if any(j["instance"] == instance and j["state"] == "running" for j in store.jobs.values()):
        connection.send_error(msg["id"], "busy", "A backup or restore of this controller is already running")
        return
    job = store.new_job(instance, "restore", t3000_snapshot.count_steps() + len(msg["items"]))
    by = _user_label(connection)

    async def run():
        results = []
        try:
            pre = await _run_snapshot_job(hass, job, instance, by, f"Before restoring {snap['id']}", "pre-restore")
            for item in msg["items"]:
                try:
                    r = await _t3_run(hass, instance, cfg, lambda c, item=item: t3000_snapshot.restore_item(
                        c, instance, snap, item, cfg["program_write_slots"], t3000_import.logic_digest))
                except T3_ERRORS as err:
                    r = {"ok": False, "item": item, "error": str(err)}
                results.append(r)
                job["done"] += 1
                if item.startswith("program:") and r.get("ok"):
                    await _panel_changed(hass, instance, int(item.split(":")[1]))
            ok = all(r.get("ok") for r in results)
            job.update(state="done", result={"results": results, "pre_restore": pre["id"], "ok": ok})
            await _repo(hass).add_audit({"by": by, "action": "snapshot_restore", "target": f"t3000:{instance}", "ok": ok,
                                         "detail": f"Restored from {snap['id']}: " + ", ".join(f"{r['item']}{'' if r.get('ok') else ' FAILED'}" for r in results)
                                         + f" (backup before restore: {pre['id']})"})
        except Exception as err:  # noqa: BLE001
            job.update(state="failed", error=str(err), result={"results": results})
            await _repo(hass).add_audit({"by": by, "action": "snapshot_restore", "target": f"t3000:{instance}", "ok": False, "detail": f"Restore failed: {err}"})

    hass.async_create_task(run())
    connection.send_result(msg["id"], {"job": job["id"], "total": job["total"]})


# ---------------- controller alarms and monitors ----------------

def _t3_device_id(hass, instance: int) -> str | None:
    for d in t3000.list_devices(hass, hass.data[DOMAIN]["conf"].get("t3000", {})):
        if d["instance"] == instance:
            return d["device_id"]
    return None


@websocket_api.websocket_command({vol.Required("type"): "bas_wiresheet/t3_alarms", vol.Required("instance"): int})
@websocket_api.async_response
async def ws_t3_alarms(hass, connection, msg):
    instance = msg["instance"]
    watch = hass.data[DOMAIN]["alarm_watch"]
    try:
        _t3_require(hass, instance)
    except T3_ERRORS as err:
        connection.send_error(msg["id"], "t3000", str(err))
        return
    name = next((d["name"] for d in t3000.list_devices(hass, hass.data[DOMAIN]["conf"].get("t3000", {})) if d["instance"] == instance), str(instance))
    alarms = await watch.refresh(instance, name)
    latest = watch.latest.get(instance, {})
    if alarms is None:
        connection.send_error(msg["id"], "t3000", latest.get("error") or "No reply")
        return
    connection.send_result(msg["id"], {"alarms": alarms, "poll_s": watch.interval_s, "sensor": f"sensor.bas_wiresheet_t3_{instance}_alarms"})


@websocket_api.websocket_command({vol.Required("type"): "bas_wiresheet/t3_alarm_set", vol.Required("instance"): int,
                                  vol.Required("index"): vol.All(int, vol.Range(min=1, max=16)), vol.Required("action"): vol.In(["acknowledge", "delete"])})
@websocket_api.async_response
async def ws_t3_alarm_set(hass, connection, msg):
    if not _require_editor(hass, connection, msg):
        return
    instance = msg["instance"]
    cfg = _t3_effective(hass, instance) or {}
    if cfg.get("access") != "full":
        connection.send_error(msg["id"], "not_allowed", f"Alarms can be acknowledged only with full access (device {instance})")
        return
    try:
        result = await _t3_run(hass, instance, cfg, lambda c: t3000_alarms.set_alarm(c, instance, msg["index"], msg["action"]))
    except T3_ERRORS as err:
        connection.send_error(msg["id"], "t3000", str(err))
        return
    from homeassistant.components import persistent_notification
    persistent_notification.async_dismiss(hass, f"bas_wiresheet_t3_alarm_{instance}_{msg['index']}")
    await _repo(hass).add_audit({"by": _user_label(connection), "action": "alarm", "target": f"t3000:{instance}", "ok": True,
                                 "detail": f"Alarm {msg['index']} {msg['action']}d"})
    connection.send_result(msg["id"], result)


@websocket_api.websocket_command({vol.Required("type"): "bas_wiresheet/t3_monitors", vol.Required("instance"): int})
@websocket_api.async_response
async def ws_t3_monitors(hass, connection, msg):
    instance = msg["instance"]
    try:
        cfg = _t3_require(hass, instance)
        monitors = await _t3_run(hass, instance, cfg, lambda c: t3000_alarms.read_monitors(c, instance))
    except T3_ERRORS as err:
        connection.send_error(msg["id"], "t3000", str(err))
        return
    points = {f"{r['category']}{r['index']}": r for r in (t3000.list_points(hass, hass.data[DOMAIN]["conf"].get("t3000", {}),
                                                                          _t3_device_id(hass, instance))["points"] if _t3_device_id(hass, instance) else [])}
    for m in monitors:
        m["entities"] = {p: points[p]["entity_id"] for p in m["inputs"] if p in points}
    connection.send_result(msg["id"], {"monitors": monitors})


@websocket_api.websocket_command({vol.Required("type"): "bas_wiresheet/t3_point_values", vol.Required("instance"): int,
                                  vol.Required("points"): vol.All([vol.All(str, vol.Upper, vol.Match(r"^(IN|OUT|VAR)[1-9]\d{0,2}$"))], vol.Length(max=60))})
@websocket_api.async_response
async def ws_t3_point_values(hass, connection, msg):
    """Read only: current value, auto/manual and label of the listed points, straight from the controller."""
    instance = msg["instance"]

    def read(client):
        if client.identify() != instance:
            raise t3000_bacnet.UploadRefused("Wrong controller at this address")
        out = {}
        for name in dict.fromkeys(msg["points"]):
            kind = re.match(r"^(IN|OUT|VAR)", name).group(1)
            d = t3000_points.decode(kind, t3000_points.read_struct(client, kind, int(name[len(kind):])))
            out[name] = {"value": d["value"], "digital": d["digital_analog"] == "digital", "manual": d["auto_manual"] == "manual", "label": d["label"]}
        return out

    try:
        cfg = _t3_require(hass, instance)
        values = await _t3_run(hass, instance, cfg, read)
    except (*T3_ERRORS, ValueError) as err:
        connection.send_error(msg["id"], "t3000", str(err))
        return
    connection.send_result(msg["id"], {"values": values, "at": time.time()})


def _local_year(hass) -> int:
    from homeassistant.util import dt as dt_util
    return dt_util.now().year


@websocket_api.websocket_command({vol.Required("type"): "bas_wiresheet/t3_schedules", vol.Required("instance"): int})
@websocket_api.async_response
async def ws_t3_schedules(hass, connection, msg):
    """Read only: weekly and annual routines (names and state) of a controller."""
    instance = msg["instance"]

    def read(client):
        if client.identify() != instance:
            raise t3000_bacnet.UploadRefused("Wrong controller at this address")
        weekly = [{"index": i, **t3000_tables.decode_weekly(t3000_tables.read_table(client, "weekly", i))} for i in range(1, 9)]
        annual = [{"index": i, **t3000_tables.decode_annual(t3000_tables.read_table(client, "annual", i))} for i in range(1, 5)]
        return {"weekly": weekly, "annual": annual}

    try:
        cfg = _t3_require(hass, instance)
        result = await _t3_run(hass, instance, cfg, read)
    except (*T3_ERRORS, IndexError, struct.error) as err:
        connection.send_error(msg["id"], "t3000", str(err))
        return
    connection.send_result(msg["id"], {**result, "days": t3000_tables.DAYS})


@websocket_api.websocket_command({vol.Required("type"): "bas_wiresheet/t3_schedule_get", vol.Required("instance"): int,
                                  vol.Required("kind"): vol.In(["weekly", "annual"]), vol.Required("index"): vol.All(int, vol.Range(min=1, max=8))})
@websocket_api.async_response
async def ws_t3_schedule_get(hass, connection, msg):
    instance, kind, index = msg["instance"], msg["kind"], msg["index"]
    year = _local_year(hass)
    fn = (lambda c: t3000_tables.read_weekly(c, index)) if kind == "weekly" else (lambda c: t3000_tables.read_annual(c, index, year))
    try:
        cfg = _t3_require(hass, instance)
        result = await _t3_run(hass, instance, cfg, fn)
    except (*T3_ERRORS, IndexError, struct.error) as err:
        connection.send_error(msg["id"], "t3000", str(err))
        return
    connection.send_result(msg["id"], {**result, "kind": kind, "days_names": t3000_tables.DAYS})


@websocket_api.websocket_command({vol.Required("type"): "bas_wiresheet/t3_schedule_set", vol.Required("instance"): int,
                                  vol.Required("kind"): vol.In(["weekly", "annual"]), vol.Required("index"): vol.All(int, vol.Range(min=1, max=8)),
                                  vol.Required("changes"): dict, vol.Required("confirm"): str})
@websocket_api.async_response
async def ws_t3_schedule_set(hass, connection, msg):
    """Change a weekly or annual routine on the controller (read back to verify). Full access only."""
    if not _require_editor(hass, connection, msg):
        return
    instance, kind, index = msg["instance"], msg["kind"], msg["index"]
    name = f"{'WR' if kind == 'weekly' else 'AR'}{index}"
    cfg = _t3_effective(hass, instance) or {}
    if cfg.get("access") != "full":
        connection.send_error(msg["id"], "not_allowed", f"Schedules can be changed only with full access (device {instance})")
        return
    if msg["confirm"].strip().upper() != name:
        connection.send_error(msg["id"], "not_allowed", f"Type {name} to confirm")
        return
    year = _local_year(hass)
    fn = (lambda c: t3000_tables.edit_weekly(c, instance, index, msg["changes"])) if kind == "weekly" else \
        (lambda c: t3000_tables.edit_annual(c, instance, index, msg["changes"], year))
    try:
        result = await _t3_run(hass, instance, cfg, fn)
    except (*T3_ERRORS, IndexError, struct.error, ValueError) as err:
        await _repo(hass).add_audit({"by": _user_label(connection), "action": "schedule", "target": f"t3000:{instance}:{name}", "ok": False,
                                     "detail": f"{name} change failed: {err}"})
        connection.send_error(msg["id"], "t3000", str(err))
        return
    what = ", ".join(sorted(msg["changes"]))
    await _repo(hass).add_audit({"by": _user_label(connection), "action": "schedule", "target": f"t3000:{instance}:{name}", "ok": True,
                                 "detail": f"{name} ({result['after'].get('label') or result['after'].get('description')}): {what} changed"})
    connection.send_result(msg["id"], result)


@websocket_api.websocket_command({vol.Required("type"): "bas_wiresheet/t3_pids", vol.Required("instance"): int})
@websocket_api.async_response
async def ws_t3_pids(hass, connection, msg):
    """Read only: the 16 PID controllers of a controller."""
    instance = msg["instance"]

    def read(client):
        if client.identify() != instance:
            raise t3000_bacnet.UploadRefused("Wrong controller at this address")
        return t3000_tables.read_pids(client)

    try:
        cfg = _t3_require(hass, instance)
        loops = await _t3_run(hass, instance, cfg, read)
    except (*T3_ERRORS, IndexError, struct.error) as err:
        connection.send_error(msg["id"], "t3000", str(err))
        return
    connection.send_result(msg["id"], {"loops": loops})


@websocket_api.websocket_command({vol.Required("type"): "bas_wiresheet/t3_pid_set", vol.Required("instance"): int,
                                  vol.Required("index"): vol.All(int, vol.Range(min=1, max=16)), vol.Required("changes"): dict,
                                  vol.Required("confirm"): str})
@websocket_api.async_response
async def ws_t3_pid_set(hass, connection, msg):
    """Tune a PID controller on the controller (read back to verify). Full access only."""
    if not _require_editor(hass, connection, msg):
        return
    instance, index = msg["instance"], msg["index"]
    name = f"PID{index}"
    cfg = _t3_effective(hass, instance) or {}
    if cfg.get("access") != "full":
        connection.send_error(msg["id"], "not_allowed", f"PID controllers can be changed only with full access (device {instance})")
        return
    if msg["confirm"].strip().upper() != name:
        connection.send_error(msg["id"], "not_allowed", f"Type {name} to confirm")
        return
    try:
        result = await _t3_run(hass, instance, cfg, lambda c: t3000_tables.edit_pid(c, instance, index, msg["changes"]))
    except (*T3_ERRORS, IndexError, struct.error, ValueError) as err:
        await _repo(hass).add_audit({"by": _user_label(connection), "action": "pid", "target": f"t3000:{instance}:{name}", "ok": False,
                                     "detail": f"{name} change failed: {err}"})
        connection.send_error(msg["id"], "t3000", str(err))
        return
    diff = ", ".join(f"{k}: {result['before'].get(k)} -> {result['after'].get(k)}" for k in msg["changes"])
    await _repo(hass).add_audit({"by": _user_label(connection), "action": "pid", "target": f"t3000:{instance}:{name}", "ok": True, "detail": f"{name}: {diff}"})
    connection.send_result(msg["id"], result)


@websocket_api.websocket_command({vol.Required("type"): "bas_wiresheet/program_watch_status"})
@callback
def ws_program_watch_status(hass, connection, msg):
    """Read only. Last program reads, stored versions and runs."""
    st = hass.data[DOMAIN]["watch"].status()
    t3 = hass.data[DOMAIN]["conf"].get("t3000", {})
    at = t3.get("program_watch_time")
    connection.send_result(msg["id"], {**st, "enabled": t3.get("program_watch", True), "time": at.isoformat() if at else None,
                                       "devices": _watch_devices(hass)})


@websocket_api.websocket_command({vol.Required("type"): "bas_wiresheet/program_watch_run"})
@websocket_api.async_response
async def ws_program_watch_run(hass, connection, msg):
    """Read every program now (read only on the controllers). Runs in the background."""
    if not _require_editor(hass, connection, msg):
        return
    if hass.data[DOMAIN]["watch"].running:
        connection.send_error(msg["id"], "busy", "A program check is already running")
        return
    hass.async_create_task(_watch_run(hass, f"requested by {_user_label(connection)}"))
    connection.send_result(msg["id"], {"started": True, "devices": _watch_devices(hass)})


def _t3_guard(hass, instance: int):
    cfg = _t3_effective(hass, instance) or {}
    devices = hass.data[DOMAIN]["conf"].get("t3000", {}).get("devices", {})
    if cfg.get("access") == "full":
        unlocked = bool(hass.data[DOMAIN]["conf"].get("t3000", {}).get("unlock_protected_points"))
        return make_guard(devices, instance, full=True, labels=_t3_point_texts(hass, instance), unlocked=unlocked)
    return make_guard(devices, instance)


def _t3_cfg(hass, instance: int) -> dict:
    cfg = _t3_effective(hass, instance)
    if not cfg:
        raise T3AccessError(f"Device {instance} is not configured for T3000 access")
    return cfg


def _t3_require(hass, instance: int, slot: int | None = None) -> dict:
    cfg = _t3_cfg(hass, instance)
    if slot is None:
        if not cfg.get("allow_program_read"):
            raise T3AccessError(f"Program access is not enabled for device {instance}")
    elif slot not in cfg.get("program_write_slots", []):
        raise T3AccessError(f"Program {slot} of device {instance} is not enabled for writing")
    return cfg


def _t3_address(hass, instance: int, cfg: dict) -> tuple[str, int, int | None, bytes]:
    """Where to send T3000 requests: the configured address, else the BACnet/IP integration's device address.

    MS/TP controllers appear as "network:mac"; the router for that network comes from t3000: mstp_routers.
    """
    raw = cfg.get("address")
    if not raw:
        for entry in hass.config_entries.async_entries(t3000.BACNET_DOMAIN):
            if entry.unique_id == str(instance):
                raw = entry.data.get("device_address")
                break
    if not raw:
        raise T3AccessError(f"No network address known for device {instance}")
    m = re.fullmatch(r"(\d+):(\d+|0x[0-9a-fA-F]+)", str(raw))
    if m and int(m.group(1)) > 0 and "." not in str(raw):
        routers = hass.data[DOMAIN]["conf"].get("t3000", {}).get("mstp_routers", {})
        router = routers.get(int(m.group(1)))
        if not router:
            raise T3AccessError(f"Device {instance} is on BACnet network {m.group(1)} (MS/TP); add its router under t3000: mstp_routers")
        raw = f"{router}/{raw}"
    try:
        return t3000_bacnet.parse_route(str(raw))
    except ValueError as err:
        raise T3AccessError(str(err)) from err


async def _t3_run(hass, instance: int, cfg: dict, fn):
    host, port, dnet, dadr = _t3_address(hass, instance, cfg)
    locks = hass.data[DOMAIN].setdefault("t3_locks", {})
    lock = locks.setdefault(instance, asyncio.Lock())

    def job():
        with t3000_bacnet.Client(host, port, gap=0.3, dnet=dnet, dadr=dadr) as client:
            return fn(client)

    async with lock:
        return await hass.async_add_executor_job(job)


T3_ERRORS = (T3AccessError, t3000_bacnet.BacnetError, t3000_bacnet.UploadRefused, t3000_toolchain.ToolchainError, OSError)


PROGRAM_STATE_TTL_S = 10.0


@websocket_api.websocket_command({vol.Required("type"): "bas_wiresheet/t3_program_state", vol.Required("instance"): int,
                                  vol.Required("slot"): vol.All(int, vol.Range(min=1, max=16))})
@websocket_api.async_response
async def ws_t3_program_state(hass, connection, msg):
    """Read only. Timer and local-variable state of one program, for Live Monitoring; at most one read per 10 s."""
    instance, slot = msg["instance"], msg["slot"]
    cache = hass.data[DOMAIN].setdefault("t3_state_cache", {})
    hit = cache.get((instance, slot))
    if hit and time.time() - hit["read_at"] / 1000 < PROGRAM_STATE_TTL_S:
        connection.send_result(msg["id"], hit)
        return

    def read(client):
        if client.identify() != instance:
            raise t3000_bacnet.UploadRefused("Wrong controller at this address")
        status = t3000_bacnet.describe_status(client.read_point(slot - 1))
        code = client.read_code(slot - 1)
        used = t3000_bacnet.code_used_length(code)
        return {"program": slot, "on": status["on"], "used": used, "logic": t3000_import.logic_digest(code) if used else None,
                **t3000_import.program_state(code), "read_at": time.time() * 1000}

    try:
        cfg = _t3_require(hass, instance)
        result = await _t3_run(hass, instance, cfg, read)
    except (*T3_ERRORS, IndexError, struct.error, t3000_import.Unsupported) as err:
        connection.send_error(msg["id"], "t3000", str(err))
        return
    cache[(instance, slot)] = result
    connection.send_result(msg["id"], result)


@websocket_api.websocket_command({vol.Required("type"): "bas_wiresheet/t3_point_write", vol.Required("device_id"): str,
                                  vol.Required("entity_id"): str, vol.Required("value"): vol.Coerce(float),
                                  vol.Required("confirm"): str})
@websocket_api.async_response
async def ws_t3_point_write(hass, connection, msg):
    """Write one value to a controller point through the BACnet integration's own entity.

    Restricted access: Test Sandbox points on virtual ports only. Full access: any writable point, physical outputs
    included, except points locked by the safety and commissioning patterns or the device's locked list.
    """
    if not _require_editor(hass, connection, msg):
        return
    conf = hass.data[DOMAIN]["conf"].get("t3000", {})
    try:
        data = t3000.list_points(hass, conf, msg["device_id"])
    except ValueError as err:
        connection.send_error(msg["id"], "not_found", str(err))
        return
    instance = data["device"]["instance"]
    row = next((r for r in data["points"] if r["entity_id"] == msg["entity_id"]), None)
    cfg = _t3_effective(hass, instance) or {}
    full = cfg.get("access") == "full"
    name = f"{row['category']}{row['index']}" if row else "?"
    domain = msg["entity_id"].split(".")[0]
    refuse = None
    if not cfg.get("allow_point_writes"):
        refuse = f"Point writes are not enabled for device {instance} (allow_point_writes)"
    elif row is None:
        refuse = "This entity is not a point of the selected controller"
    elif row["protection"] == "locked":
        refuse = f"{name} is locked ({row['protection_reason']})"
    elif not full and row["protection"] != "sandbox":
        refuse = f"{name} is not a sandbox point"
    elif not full and row["port"] != "virtual":
        refuse = f"{name} is a {row['port']} port; only virtual points can be written"
    elif domain not in ("number", "switch"):
        refuse = f"{name} is read only in Home Assistant ({domain})"
    elif domain == "switch" and msg["value"] not in (0, 1):
        refuse = f"{name} is on/off: write 1 or 0"
    elif not -1000000 <= msg["value"] <= 1000000:
        refuse = "Value out of range"
    elif msg["confirm"].strip().upper() != name:
        refuse = f"Type {name} to confirm"
    if refuse:
        connection.send_error(msg["id"], "not_allowed", refuse)
        return
    before = hass.states.get(msg["entity_id"])
    try:
        if domain == "switch":
            await hass.services.async_call("switch", "turn_on" if msg["value"] == 1 else "turn_off", {"entity_id": msg["entity_id"]}, blocking=True)
        else:
            await hass.services.async_call("number", "set_value", {"entity_id": msg["entity_id"], "value": msg["value"]}, blocking=True)
    except Exception as err:  # noqa: BLE001 - report the integration's own error to the operator
        await _repo(hass).add_audit({"by": _user_label(connection), "action": "point_write", "target": f"t3000:{instance}:{name}", "ok": False,
                                     "detail": f"{name} = {msg['value']:g} failed: {err}", "entity_id": msg["entity_id"]})
        connection.send_error(msg["id"], "write_failed", str(err))
        return
    _LOGGER.warning("BAS Wire Sheet: %s wrote %s = %s on device %s (was %s; %s port)", _user_label(connection), name, msg["value"],
                    instance, before.state if before else None, row["port"])
    await _repo(hass).add_audit({"by": _user_label(connection), "action": "point_write", "target": f"t3000:{instance}:{name}", "ok": True,
                                 "detail": f"{name} = {msg['value']:g} (was {before.state if before else '?'}; {row['port']} port)", "entity_id": msg["entity_id"]})
    after = hass.states.get(msg["entity_id"])
    connection.send_result(msg["id"], {"ok": True, "point": name, "requested": msg["value"], "port": row["port"],
                                       "state": after.state if after else None, "previous": before.state if before else None})


DISCOVERY_TTL_S = 300.0


@websocket_api.websocket_command({vol.Required("type"): "bas_wiresheet/t3_discover", vol.Optional("refresh", default=False): bool})
@websocket_api.async_response
async def ws_t3_discover(hass, connection, msg):
    """Who-Is on the BACnet network through the BACnet integration's client. Read only; cached for 5 minutes."""
    if not _require_editor(hass, connection, msg):
        return
    cache = hass.data[DOMAIN].get("t3_discovery")
    if not cache or msg["refresh"] and time.time() - cache["at"] / 1000 > 30 or time.time() - cache["at"] / 1000 > DISCOVERY_TTL_S:
        clients = (hass.data.get(t3000.BACNET_DOMAIN) or {}).get("_port_clients") or {}
        client = next((v.get("client") for v in clients.values() if isinstance(v, dict) and v.get("client")), None)
        if client is None or not hasattr(client, "discover_devices"):
            connection.send_error(msg["id"], "unavailable", "The BACnet integration does not offer discovery")
            return
        try:
            found = await client.discover_devices(timeout=5)
        except Exception as err:  # noqa: BLE001
            connection.send_error(msg["id"], "discovery_failed", str(err))
            return
        cache = {"at": time.time() * 1000, "devices": found}
        hass.data[DOMAIN]["t3_discovery"] = cache
    known = {d["instance"]: d for d in t3000.list_devices(hass, hass.data[DOMAIN]["conf"].get("t3000", {}), include_all=True)}
    routers = hass.data[DOMAIN]["conf"].get("t3000", {}).get("mstp_routers", {})
    cfg = hass.data[DOMAIN]["conf"].get("t3000", {}).get("devices", {})
    rows = []
    for d in cache["devices"]:
        inst = int(d.get("device_id"))
        k = known.get(inst)
        addr = str(d.get("address") or "")
        mm = re.fullmatch(r"(\d+):(\d+|0x[0-9a-fA-F]+)", addr)
        rows.append({"instance": inst, "name": d.get("device_name"), "address": addr or None, "vendor": d.get("vendor_name") or "",
                     "mstp_network": int(mm.group(1)) if mm else None, "mstp_mac": mm.group(2) if mm else None,
                     "router_configured": bool(mm and int(mm.group(1)) in routers),
                     "model": d.get("model_name") or "", "firmware": d.get("firmware_version") or "",
                     "temco": t3000.is_temco(d.get("vendor_name"), d.get("model_name")), "in_home_assistant": k is not None,
                     "ha_name": k["name"] if k else None, "configured": inst in cfg})
    for inst, k in known.items():
        if all(r["instance"] != inst for r in rows):
            rows.append({"instance": inst, "name": k["name"], "address": None, "vendor": k.get("manufacturer") or "", "model": k.get("reported_model") or "",
                         "firmware": k.get("sw_version") or "", "temco": "temco" in (k.get("manufacturer") or "").lower(), "in_home_assistant": True,
                         "ha_name": k["name"], "configured": inst in cfg, "not_answering": True})
    rows.sort(key=lambda r: r["instance"])
    connection.send_result(msg["id"], {"at": cache["at"], "devices": rows})


def _t3_target(instance: int, slot: int) -> str:
    return f"t3000:{instance}:{slot}"


@websocket_api.websocket_command({vol.Required("type"): "bas_wiresheet/sheet_access", vol.Required("sheet_id"): str,
                                  vol.Optional("editors"): [str]})
@websocket_api.async_response
async def ws_sheet_access(hass, connection, msg):
    """Read or set which users may change a sheet. Empty list: every editor."""
    repo = _repo(hass)
    try:
        repo.get(msg["sheet_id"])
    except RepoError as err:
        connection.send_error(msg["id"], "not_found", str(err))
        return
    users = [{"id": u.id, "name": u.name or u.id, "admin": u.is_admin} for u in await hass.auth.async_get_users()
             if u.is_active and not u.system_generated and _role(hass, u) == "editor"]
    if "editors" in msg:
        if not _require_editor(hass, connection, msg) or not _sheet_allowed(hass, connection, msg, msg["sheet_id"]):
            return
        known = {u["id"] for u in users}
        bad = [e for e in msg["editors"] if e not in known]
        if bad:
            connection.send_error(msg["id"], "invalid", "Only users with editor permission can be listed")
            return
        me = connection.user
        if msg["editors"] and me and not me.is_admin and me.id not in msg["editors"]:
            connection.send_error(msg["id"], "invalid", "Keep yourself in the list, or ask an administrator")
            return
        await repo.set_editors(msg["sheet_id"], msg["editors"], _user_label(connection))
        await repo.add_audit({"by": _user_label(connection), "action": "sheet_access", "target": None, "sheet": msg["sheet_id"], "ok": True,
                              "detail": f"Editors: {', '.join(msg['editors']) or 'every editor'}"})
    connection.send_result(msg["id"], {"editors": repo.editors_of(msg["sheet_id"]), "users": users})


@websocket_api.websocket_command({vol.Required("type"): "bas_wiresheet/delete_sheet", vol.Required("sheet_id"): str})
@websocket_api.async_response
async def ws_delete_sheet(hass, connection, msg):
    if not _require_editor(hass, connection, msg):
        return
    if not _sheet_allowed(hass, connection, msg, msg["sheet_id"]):
        return
    try:
        await _repo(hass).delete(msg["sheet_id"])
    except RepoError as err:
        connection.send_error(msg["id"], "not_allowed", str(err))
        return
    connection.send_result(msg["id"], {"deleted": msg["sheet_id"]})


@websocket_api.websocket_command({vol.Required("type"): "bas_wiresheet/t3_program_list", vol.Required("instance"): int})
@websocket_api.async_response
async def ws_t3_program_list(hass, connection, msg):
    instance = msg["instance"]

    def read(client):
        if client.identify() != instance:
            raise t3000_bacnet.UploadRefused("Wrong controller at this address")
        return [{"program": i + 1, **t3000_bacnet.describe_status(client.read_point(i))} for i in range(16)]

    try:
        cfg = _t3_require(hass, instance)
        programs = await _t3_run(hass, instance, cfg, read)
    except T3_ERRORS as err:
        connection.send_error(msg["id"], "t3000", str(err))
        return
    connection.send_result(msg["id"], {"programs": programs, "caps": _t3_caps(cfg)})


@websocket_api.websocket_command({vol.Required("type"): "bas_wiresheet/t3_program_read", vol.Required("instance"): int,
                                  vol.Required("slot"): vol.All(int, vol.Range(min=1, max=16))})
@websocket_api.async_response
async def ws_t3_program_read(hass, connection, msg):
    instance, slot = msg["instance"], msg["slot"]

    def read(client):
        if client.identify() != instance:
            raise t3000_bacnet.UploadRefused("Wrong controller at this address")
        status = t3000_bacnet.describe_status(client.read_point(slot - 1))
        code = client.read_code(slot - 1)
        used = t3000_bacnet.code_used_length(code)
        listing = t3000_toolchain.decompile(code) if used else ""
        return {"program": slot, "status": status, "used": used, "empty": not any(code),
                "sha256": hashlib.sha256(code[:used]).hexdigest() if used else None, "listing": listing,
                "listing_safe": _listing_is_safe(code, listing) if used else True}

    try:
        cfg = _t3_require(hass, instance)
        result = await _t3_run(hass, instance, cfg, read)
    except T3_ERRORS as err:
        connection.send_error(msg["id"], "t3000", str(err))
        return
    connection.send_result(msg["id"], result)


def _t3_point_texts(hass, instance: int) -> dict[str, str]:
    """Label, description and entity id of every point, so the safety and commissioning patterns can see what it is."""
    device = next((d for d in t3000.list_devices(hass, {}) if d["instance"] == instance), None)
    if not device:
        return {}
    conf = hass.data[DOMAIN]["conf"].get("t3000", {})
    return {f"{p['category']}{p['index']}": f"{p.get('label') or ''} {p.get('description') or ''} {p['entity_id']}"
            for p in t3000.list_points(hass, conf, device["device_id"])["points"]}


def _t3_labels(hass, instance: int) -> dict[str, str]:
    """Point labels Home Assistant already knows for this controller."""
    conf = hass.data[DOMAIN]["conf"].get("t3000", {})
    device = next((d for d in t3000.list_devices(hass, conf) if d["instance"] == instance), None)
    if not device:
        return {}
    out: dict[str, str] = {}
    for p in t3000.list_points(hass, conf, device["device_id"])["points"]:
        label = str(p.get("label") or "")
        name = f"{p['category']}{p['index']}"
        if label and not re.fullmatch(r"(IN|OUT|VAR|Object)\s*\d+", label, re.I):
            out[name] = label
    return out


async def _t3_store_import(hass, connection, code: bytes, instance: int, slot: int, title: str) -> dict:
    labels = _t3_labels(hass, instance)
    repo = _repo(hass)
    existing = {s["id"] for s in repo.list()}
    base = f"t3-{instance}-prg{slot}"
    sheet_id, n = base, 2
    while sheet_id in existing:
        sheet_id, n = f"{base}-{n}", n + 1

    def build():
        listing = t3000_toolchain.decompile(code)
        return t3000_import.code_to_sheet(code, listing, sheet_id=sheet_id, name=title, instance=instance, slot=slot, labels=labels), listing

    result, listing = await hass.async_add_executor_job(build)
    if result["report"]["too_large"]:
        raise t3000_bacnet.UploadRefused("This program is too large to show as a wire sheet")
    await repo.save_draft(result["sheet"], _user_label(connection), create_only=True)
    return {"sheet_id": sheet_id, "report": result["report"], "listing": listing}


@websocket_api.websocket_command({vol.Required("type"): "bas_wiresheet/t3_program_to_sheet", vol.Required("instance"): int,
                                  vol.Required("slot"): vol.All(int, vol.Range(min=1, max=16))})
@websocket_api.async_response
async def ws_t3_program_to_sheet(hass, connection, msg):
    """Read a program from the controller and create a documentation sheet. Nothing is written to the controller."""
    if not _require_editor(hass, connection, msg):
        return
    instance, slot = msg["instance"], msg["slot"]

    def read(client):
        if client.identify() != instance:
            raise t3000_bacnet.UploadRefused("Wrong controller at this address")
        return client.read_point(slot - 1), client.read_code(slot - 1)

    try:
        cfg = _t3_require(hass, instance)
        point, code = await _t3_run(hass, instance, cfg, read)
        if not any(code):
            raise t3000_bacnet.UploadRefused(f"Program {slot} is empty")
        status = t3000_bacnet.describe_status(point)
        title = f"{instance} PRG{slot}" + (f" {status['description']}" if status["description"] else "")
        connection.send_result(msg["id"], await _t3_store_import(hass, connection, code, instance, slot, title))
    except (*T3_ERRORS, ValueError, RepoError) as err:
        connection.send_error(msg["id"], "t3000", str(err))


@websocket_api.websocket_command({vol.Required("type"): "bas_wiresheet/t3_prog_file", vol.Required("data"): str, vol.Required("instance"): int,
                                  vol.Optional("slot"): vol.All(int, vol.Range(min=1, max=16))})
@websocket_api.async_response
async def ws_t3_prog_file(hass, connection, msg):
    """A saved T3000 .prog file: list its programs, or create a documentation sheet from one. No controller traffic."""
    if not _require_editor(hass, connection, msg):
        return
    try:
        data = base64.b64decode(msg["data"], validate=True)
        programs = t3000_import.parse_prog_file(data)
        if "slot" not in msg:
            connection.send_result(msg["id"], {"programs": [{k: v for k, v in p.items() if k != "code"} for p in programs]})
            return
        p = programs[msg["slot"] - 1]
        if p["empty"]:
            raise ValueError(f"Program {msg['slot']} is empty")
        title = f"{msg['instance']} PRG{msg['slot']}" + (f" {p['description']}" if p["description"] else "") + " (file)"
        connection.send_result(msg["id"], await _t3_store_import(hass, connection, p["code"], msg["instance"], msg["slot"], title))
    except (binascii.Error, ValueError, RepoError, t3000_toolchain.ToolchainError, t3000_bacnet.UploadRefused) as err:
        connection.send_error(msg["id"], "t3000", str(err))


def _listing_is_safe(code: bytes, listing: str) -> bool:
    """T3000's listing can lose grouping: editing is only safe when it compiles back to the same logic."""
    try:
        return t3000_import.logic_digest(t3000_toolchain.compile_source(listing)) == t3000_import.logic_digest(code)
    except t3000_toolchain.ToolchainError:
        return False


def _site_locked(text: str) -> str | None:
    """Name of the configured locked equipment (t3000: locked_equipment) this text refers to, if any."""
    return t3000_policy.site_match(text)


def _t3_review_change(old: bytes, source: str, guard, status: dict | None = None, allow_physical: bool = False, unlocked: bool = False) -> dict:
    """Compile an edited program and decide whether it may replace the current one. Runs in the executor."""
    old_listing = t3000_toolchain.decompile(old)
    identity = " ".join(str((status or {}).get(k, "")) for k in ("description", "label"))
    hit = _site_locked(identity) or _site_locked(old_listing) or _site_locked(source)
    if not unlocked and hit:
        raise t3000_bacnet.UploadRefused(f"This program belongs to locked equipment ({hit}): its logic is locked")
    if not _listing_is_safe(old, old_listing):
        raise t3000_bacnet.UploadRefused("T3000's listing of this program does not compile back to the same logic, so it cannot be edited safely")
    text = source.replace("\r\n", "\n").strip().upper().replace("\n", "\r\n") + "\r\n"
    try:
        text.encode("ascii")
    except UnicodeEncodeError as err:
        raise t3000_bacnet.UploadRefused("The program can only contain plain ASCII characters") from err
    code = t3000_toolchain.compile_source(text)
    listing = t3000_toolchain.decompile(code)
    if t3000_import.logic_digest(t3000_toolchain.compile_source(listing)) != t3000_import.logic_digest(code):
        raise t3000_bacnet.UploadRefused("T3000's listing of the new code changes its logic (grouping in AND/OR or a divisor). Use an intermediate variable")
    before, after = t3000_import.program_targets(old, old_listing), t3000_import.program_targets(code, listing)
    known = {re.sub(r"^\d+\s*", "", l).strip() for l in old_listing.splitlines()}
    unknown = [l for l in after["unknown"] if l not in known]
    if unknown:
        raise t3000_bacnet.UploadRefused("Cannot verify what this new line writes: " + unknown[0])
    added = sorted(set(after["targets"]) - set(before["targets"]))
    for point in added:
        parsed = t3000_basic.parse_point(point)
        allowed, reason = guard(*parsed) if parsed else (False, "unknown point")
        if not allowed:
            raise t3000_bacnet.UploadRefused(f"The new code would write {point}: {reason}")
    if not allow_physical:
        for point in after["targets"]:
            parsed = t3000_basic.parse_point(point)
            allowed, reason = guard(*parsed) if parsed else (False, "unknown point")
            if not allowed:
                raise t3000_bacnet.UploadRefused(f"This program writes {point} ({reason}). Editing it needs allow_editing_physical_outputs for this controller")
    diff = [l for l in difflib.unified_diff(old_listing.splitlines(), listing.splitlines(), "current", "new", lineterm="", n=1)][2:]
    return {"code": code, "listing": listing, "diff": diff, "added_writes": added,
            "removed_writes": sorted(set(before["targets"]) - set(after["targets"])), "writes": after["targets"], "changed": t3000_import.logic_digest(code) != t3000_import.logic_digest(old)}


@websocket_api.websocket_command({vol.Required("type"): "bas_wiresheet/t3_code_check", vol.Required("instance"): int,
                                  vol.Required("slot"): vol.All(int, vol.Range(min=1, max=16)), vol.Required("source"): vol.All(str, vol.Length(min=1, max=20000))})
@websocket_api.async_response
async def ws_t3_code_check(hass, connection, msg):
    """Compile an edited program and compare it with the controller. Reads only."""
    if not _require_editor(hass, connection, msg):
        return
    instance, slot = msg["instance"], msg["slot"]
    try:
        cfg = _t3_require(hass, instance, slot)
        guard = _t3_guard(hass, instance)

        def check(client):
            if client.identify() != instance:
                raise t3000_bacnet.UploadRefused("Wrong controller at this address")
            current = client.read_code(slot - 1)
            if not t3000_bacnet.code_used_length(current):
                raise t3000_bacnet.UploadRefused(f"Program {slot} is empty; use a T3000 sheet and Load Program")
            status = t3000_bacnet.describe_status(client.read_point(slot - 1))
            review = _t3_review_change(current, msg["source"], guard, status, cfg.get("allow_editing_physical_outputs", False),
                                       bool(hass.data[DOMAIN]["conf"].get("t3000", {}).get("unlock_protected_points")))
            if not review["changed"]:
                return review, None, None
            plan, backup = t3000_bacnet.plan_replace(client, instance, slot, cfg["program_write_slots"], review["code"], t3000_import.logic_digest)
            return review, plan, backup

        review, plan, backup = await _t3_run(hass, instance, cfg, check)
    except T3_ERRORS as err:
        connection.send_error(msg["id"], "t3000", str(err))
        return
    out = {k: review[k] for k in ("diff", "added_writes", "removed_writes", "writes", "changed", "listing")}
    out["new_bytes"] = len(review["code"])
    if plan:
        token = secrets.token_urlsafe(18)
        hass.data[DOMAIN].setdefault("t3_replace", {})[token] = {
            "user": connection.user.id if connection.user else None, "expires": time.time() + PREVIEW_TTL_S, "plan": plan,
            "code": review["code"], "backup": backup, "instance": instance, "slot": slot}
        out.update({"token": token, "running": plan.was_on, "expires_in_s": PREVIEW_TTL_S})
    connection.send_result(msg["id"], out)


@websocket_api.websocket_command({vol.Required("type"): "bas_wiresheet/t3_code_apply", vol.Required("token"): str, vol.Required("confirm"): str})
@websocket_api.async_response
async def ws_t3_code_apply(hass, connection, msg):
    """Replace a program on the controller. The previous version is stored first."""
    if not _require_editor(hass, connection, msg):
        return
    plans = hass.data[DOMAIN].setdefault("t3_replace", {})
    p = plans.get(msg["token"])
    if not p or p["expires"] < time.time() or p["user"] != (connection.user.id if connection.user else None):
        plans.pop(msg["token"], None)
        connection.send_error(msg["id"], "plan_required", "Check expired, already used or owned by another user; check again")
        return
    expected = f"PROGRAM {p['slot']}"
    if msg["confirm"].strip().upper() != expected:
        connection.send_error(msg["id"], "confirm", f"Type {expected} to confirm")
        return
    plans.pop(msg["token"], None)
    target = _t3_target(p["instance"], p["slot"])
    repo = _repo(hass)
    entry = {"saved_at": datetime.now(UTC).isoformat(), "by": _user_label(connection), "code_hex": p["backup"].hex(), "status_hex": p["plan"].old_status}
    await repo.add_program_backup(target, entry)
    try:
        cfg = _t3_require(hass, p["instance"], p["slot"])
        result = await _t3_run(hass, p["instance"], cfg, lambda c: t3000_bacnet.apply_replace(c, p["plan"], p["code"], p["backup"], p["plan"].token, t3000_import.logic_digest))
    except T3_ERRORS as err:
        await repo.pop_program_backup(target)
        connection.send_error(msg["id"], "t3000", str(err))
        return
    if not result["ok"] and result.get("restored"):
        await repo.pop_program_backup(target)
    if result["ok"]:
        await _panel_changed(hass, p["instance"], p["slot"])
    await repo.add_audit({"by": _user_label(connection), "action": "code_update", "target": target, "ok": bool(result["ok"]),
                          "detail": "Program code updated" if result["ok"] else f"Update failed: {result.get('error')}", "backup_at": entry["saved_at"]})
    connection.send_result(msg["id"], {**result, "backups": len(repo.program_backups(target))})


@websocket_api.websocket_command({vol.Required("type"): "bas_wiresheet/t3_code_restore", vol.Required("instance"): int,
                                  vol.Required("slot"): vol.All(int, vol.Range(min=1, max=16)), vol.Required("confirm"): str})
@websocket_api.async_response
async def ws_t3_code_restore(hass, connection, msg):
    """Put back the version stored before the last update."""
    if not _require_editor(hass, connection, msg):
        return
    instance, slot = msg["instance"], msg["slot"]
    if msg["confirm"].strip().upper() != f"PROGRAM {slot}":
        connection.send_error(msg["id"], "confirm", f"Type PROGRAM {slot} to confirm")
        return
    target = _t3_target(instance, slot)
    repo = _repo(hass)
    saved = repo.program_backups(target)
    if not saved:
        connection.send_error(msg["id"], "no_backup", "No stored version for this program")
        return
    image = bytes.fromhex(saved[-1]["code_hex"])
    code = image[:t3000_bacnet.code_used_length(image)]
    try:
        cfg = _t3_require(hass, instance, slot)

        def restore(client):
            plan, backup = t3000_bacnet.plan_replace(client, instance, slot, cfg["program_write_slots"], code, t3000_import.logic_digest)
            return t3000_bacnet.apply_replace(client, plan, code, backup, plan.token, t3000_import.logic_digest)

        result = await _t3_run(hass, instance, cfg, restore)
    except T3_ERRORS as err:
        connection.send_error(msg["id"], "t3000", str(err))
        return
    if result["ok"]:
        await repo.pop_program_backup(target)
        await _panel_changed(hass, instance, slot)
    await repo.add_audit({"by": _user_label(connection), "action": "code_restore", "target": target, "ok": bool(result["ok"]),
                          "detail": f"Restored the version stored {saved[-1]['saved_at']}" if result["ok"] else f"Restore failed: {result.get('error')}"})
    connection.send_result(msg["id"], {**result, "backups": len(repo.program_backups(target))})


@websocket_api.websocket_command({vol.Required("type"): "bas_wiresheet/t3_upload_plan", vol.Required("sheet_id"): str,
                                  vol.Required("revision"): int, vol.Optional("allow_replace", default=False): bool})
@websocket_api.async_response
async def ws_t3_upload_plan(hass, connection, msg):
    """Read-only on the controller. Uses an immutable revision, never the draft."""
    if not _require_editor(hass, connection, msg):
        return
    if not _sheet_allowed(hass, connection, msg, msg["sheet_id"]):
        return
    try:
        sheet = _repo(hass).get_revision(msg["sheet_id"], msg["revision"])["sheet"]
    except RepoError as err:
        connection.send_error(msg["id"], "not_found", str(err))
        return
    if sheet.get("mode") != "t3000" or has_errors(validate_sheet(sheet, known_entities=_known_entities(hass))):
        connection.send_error(msg["id"], "invalid", "Revision is not a valid T3000 program sheet")
        return
    if sheet["t3000"].get("imported"):
        connection.send_error(msg["id"], "invalid", "This sheet documents an existing controller program and cannot be loaded")
        return
    instance, slot = sheet["t3000"]["device_instance"], sheet["t3000"]["program_slot"]
    try:
        cfg = _t3_require(hass, instance, slot)
        devices = hass.data[DOMAIN]["conf"].get("t3000", {}).get("devices", {})
        await _t3_fill_panel(hass, sheet)
        built = await hass.async_add_executor_job(_t3_compile, sheet, devices, _t3_guard(hass, instance))
        if "code_hex" not in built:
            raise t3000_bacnet.UploadRefused("; ".join(i["message"] for i in built["issues"] if i["severity"] == "error"))
        code = bytes.fromhex(built["code_hex"])
        plan, backup = await _t3_run(hass, instance, cfg, lambda c: t3000_bacnet.plan_upload(
            c, instance, slot, cfg["program_write_slots"], code, allow_replace=msg["allow_replace"]))
    except T3_ERRORS as err:
        connection.send_error(msg["id"], "t3000", str(err))
        return
    token = secrets.token_urlsafe(18)
    hass.data[DOMAIN].setdefault("t3_plans", {})[token] = {
        "user": connection.user.id if connection.user else None, "expires": time.time() + PREVIEW_TTL_S, "plan": plan, "code": code,
        "backup": backup, "sheet_id": msg["sheet_id"], "rev": msg["revision"], "instance": instance, "slot": slot}
    connection.send_result(msg["id"], {
        "token": token, "instance": instance, "program": slot, "revision": msg["revision"], "new_bytes": plan.new_length, "existing_bytes": plan.old_used,
        "currently_on": bool(bytes.fromhex(plan.old_status)[t3000_bacnet.ON_OFF_OFFSET]), "sha256": plan.new_sha,
        "writes": built["writes"], "reads": built["reads"], "work_vars": built["work_vars"], "listing": built["listing"], "expires_in_s": PREVIEW_TTL_S})


# ---------------- Send to Controller: one check, one confirmation ----------------
# Saves a revision when the draft has changes, compiles it, compares with what the controller holds and prepares either a
# first load (empty program) or a replacement (existing program, running or not). Apply writes, verifies, keeps a backup
# of the previous program (Restore in Programs) and starts the program when asked.

@websocket_api.websocket_command({vol.Required("type"): "bas_wiresheet/t3_send_check", vol.Required("sheet_id"): str})
@websocket_api.async_response
async def ws_t3_send_check(hass, connection, msg):
    if not _require_editor(hass, connection, msg):
        return
    if not _sheet_allowed(hass, connection, msg, msg["sheet_id"]):
        return
    repo = _repo(hass)
    try:
        rec = repo.get(msg["sheet_id"])
        committed = False
        if rec.get("draft_dirty") or not rec["revisions"]:
            await repo.commit(msg["sheet_id"], _user_label(connection), "Sent to controller", _known_entities(hass))
            committed = True
        rev = repo.get(msg["sheet_id"])["revisions"][-1]
        sheet = copy.deepcopy(rev["sheet"])
    except RepoError as err:
        connection.send_error(msg["id"], "invalid", str(err))
        return
    if sheet.get("mode") != "t3000":
        connection.send_error(msg["id"], "invalid", "Only T3000 program sheets can be sent to a controller")
        return
    if (sheet.get("t3000") or {}).get("imported"):
        connection.send_error(msg["id"], "invalid", "This sheet documents an existing controller program and cannot be sent")
        return
    instance, slot = sheet["t3000"]["device_instance"], sheet["t3000"]["program_slot"]
    try:
        cfg = _t3_require(hass, instance, slot)
        devices = hass.data[DOMAIN]["conf"].get("t3000", {}).get("devices", {})
        await _t3_fill_panel(hass, sheet)
        built = await hass.async_add_executor_job(_t3_compile, sheet, devices, _t3_guard(hass, instance))
        if "code_hex" not in built:
            raise t3000_bacnet.UploadRefused("; ".join(i["message"] for i in built["issues"] if i["severity"] == "error"))
        code = bytes.fromhex(built["code_hex"])

        def check(client):
            if client.identify() != instance:
                raise t3000_bacnet.UploadRefused("Wrong controller at this address")
            current = client.read_code(slot - 1)
            used = t3000_bacnet.code_used_length(current)
            old_listing = t3000_toolchain.decompile(current[:used]) if used else ""
            if used:
                plan, backup = t3000_bacnet.plan_replace(client, instance, slot, cfg["program_write_slots"], code, t3000_import.logic_digest)
                return "replace", plan, backup, old_listing, plan.was_on
            plan, backup = t3000_bacnet.plan_upload(client, instance, slot, cfg["program_write_slots"], code, allow_replace=True)
            return "load", plan, backup, old_listing, False

        kind, plan, backup, old_listing, running = await _t3_run(hass, instance, cfg, check)
    except (*T3_ERRORS, t3000_toolchain.ToolchainError) as err:
        connection.send_error(msg["id"], "t3000", str(err))
        return
    same = kind == "replace" and plan.old_digest == t3000_import.logic_digest(code)
    diff = list(difflib.unified_diff(old_listing.splitlines(), built["listing"].splitlines(), "controller", "sheet", lineterm="", n=1))
    token = secrets.token_urlsafe(18)
    hass.data[DOMAIN].setdefault("t3_send", {})[token] = {
        "user": connection.user.id if connection.user else None, "expires": time.time() + PREVIEW_TTL_S, "kind": kind, "plan": plan, "code": code,
        "backup": backup, "sheet_id": msg["sheet_id"], "rev": rev["rev"], "instance": instance, "slot": slot}
    connection.send_result(msg["id"], {
        "token": token, "kind": kind, "same": same, "running": running, "committed": committed, "revision": rev["rev"], "instance": instance,
        "program": slot, "new_bytes": len(code), "writes": built["writes"], "reads": built["reads"], "modbus_reads": built.get("modbus_reads", []),
        "panel": built.get("panel"), "listing": built["listing"], "diff": diff, "expires_in_s": PREVIEW_TTL_S})


@websocket_api.websocket_command({vol.Required("type"): "bas_wiresheet/t3_send_apply", vol.Required("token"): str,
                                  vol.Optional("start", default=True): bool})
@websocket_api.async_response
async def ws_t3_send_apply(hass, connection, msg):
    if not _require_editor(hass, connection, msg):
        return
    plans = hass.data[DOMAIN].setdefault("t3_send", {})
    p = plans.pop(msg["token"], None)
    if not p or p["expires"] < time.time() or p["user"] != (connection.user.id if connection.user else None):
        connection.send_error(msg["id"], "plan_required", "Check expired, already used or owned by another user; send again")
        return
    repo, target = _repo(hass), _t3_target(p["instance"], p["slot"])
    logic = t3000_import.logic_digest(p["code"])
    sha = hashlib.sha256(p["code"]).hexdigest()
    entry = None
    if p["kind"] == "replace":
        entry = {"saved_at": datetime.now(UTC).isoformat(), "by": _user_label(connection), "code_hex": p["backup"].hex(), "status_hex": p["plan"].old_status}
        await repo.add_program_backup(target, entry)

    def send(client):
        if p["kind"] == "replace":
            result = t3000_bacnet.apply_replace(client, p["plan"], p["code"], p["backup"], p["plan"].token, t3000_import.logic_digest)
        else:
            result = t3000_bacnet.apply_upload(client, p["plan"], p["code"], p["backup"], p["plan"].token)
        if result.get("ok") and msg["start"]:
            result["start"] = t3000_bacnet.start_program(client, p["instance"], p["slot"], sha, logic, t3000_import.logic_digest)
        return result

    try:
        cfg = _t3_require(hass, p["instance"], p["slot"])
        result = await _t3_run(hass, p["instance"], cfg, send)
    except T3_ERRORS as err:
        if entry:
            await repo.pop_program_backup(target)
        await repo.add_audit({"by": _user_label(connection), "action": "send", "target": target, "ok": False, "detail": f"Send r{p['rev']} failed: {err}"})
        connection.send_error(msg["id"], "t3000", str(err))
        return
    if not result.get("ok") and entry and result.get("restored"):
        await repo.pop_program_backup(target)
    running = bool(result.get("ok") and (msg["start"] or result.get("running")))
    status = ("running" if running else "loaded") if result.get("ok") else "failed"
    if result.get("ok"):
        await _panel_changed(hass, p["instance"], p["slot"])
    await repo.record_deployment(p["sheet_id"], {
        "target": target, "rev": p["rev"], "deployed_at": datetime.now(UTC).isoformat(), "by": _user_label(connection), "status": status,
        "kind": "t3000", "sha256": sha, "logic": logic,
        "backup": {"code_hex": p["backup"].hex(), "status_hex": p["plan"].old_status}, "backup_of": None})
    await repo.add_audit({"by": _user_label(connection), "action": "send", "target": target, "ok": bool(result.get("ok")),
                          "detail": (f"r{p['rev']} sent to program {p['slot']} ({'replaced' if p['kind'] == 'replace' else 'loaded'}), {status}"
                                     if result.get("ok") else f"Send r{p['rev']} failed: {result.get('error')}")})
    connection.send_result(msg["id"], {**result, "status": status, "revision": p["rev"]})


@websocket_api.websocket_command({vol.Required("type"): "bas_wiresheet/t3_upload_apply", vol.Required("token"): str})
@websocket_api.async_response
async def ws_t3_upload_apply(hass, connection, msg):
    """Writes the planned program and leaves it OFF. Starting it is a separate action."""
    if not _require_editor(hass, connection, msg):
        return
    plans = hass.data[DOMAIN].setdefault("t3_plans", {})
    p = plans.pop(msg["token"], None)
    if not p or p["expires"] < time.time() or p["user"] != (connection.user.id if connection.user else None):
        connection.send_error(msg["id"], "plan_required", "Plan expired, already used or owned by another user; plan again")
        return
    try:
        cfg = _t3_require(hass, p["instance"], p["slot"])
        result = await _t3_run(hass, p["instance"], cfg, lambda c: t3000_bacnet.apply_upload(c, p["plan"], p["code"], p["backup"], p["plan"].token))
    except T3_ERRORS as err:
        connection.send_error(msg["id"], "t3000", str(err))
        return
    if result.get("ok"):
        await _panel_changed(hass, p["instance"], p["slot"])
    await _repo(hass).record_deployment(p["sheet_id"], {
        "target": _t3_target(p["instance"], p["slot"]), "rev": p["rev"], "deployed_at": datetime.now(UTC).isoformat(), "by": _user_label(connection),
        "status": "loaded" if result["ok"] else "failed", "kind": "t3000", "sha256": p["plan"].new_sha,
        "logic": t3000_import.logic_digest(p["code"]),
        "backup": {"code_hex": p["backup"].hex(), "status_hex": p["plan"].old_status}, "backup_of": None})
    connection.send_result(msg["id"], result)


@websocket_api.websocket_command({vol.Required("type"): "bas_wiresheet/t3_program_control", vol.Required("sheet_id"): str,
                                  vol.Required("action"): vol.In(["start", "stop", "remove"])})
@websocket_api.async_response
async def ws_t3_program_control(hass, connection, msg):
    """Start, stop or remove the program this sheet loaded. Only that program, only on an enabled slot."""
    if not _require_editor(hass, connection, msg):
        return
    if not _sheet_allowed(hass, connection, msg, msg["sheet_id"]):
        return
    repo = _repo(hass)
    try:
        last = next((d for d in reversed(repo.get(msg["sheet_id"])["deployments"]) if d.get("kind") == "t3000"), None)
    except RepoError as err:
        connection.send_error(msg["id"], "not_found", str(err))
        return
    if not last or last["status"] not in ("loaded", "running", "stopped"):
        connection.send_error(msg["id"], "not_loaded", "This sheet has no program loaded on a controller")
        return
    _, inst, sl = last["target"].split(":")
    instance, slot, action = int(inst), int(sl), msg["action"]
    logic = last.get("logic")
    if action == "start" and not logic:
        logic = await hass.async_add_executor_job(_t3_loaded_logic, hass, msg["sheet_id"], last)
    fn = {"start": lambda c: t3000_bacnet.start_program(c, instance, slot, last["sha256"], logic, t3000_import.logic_digest),
          "stop": lambda c: t3000_bacnet.stop_program(c, instance, slot),
          "remove": lambda c: t3000_bacnet.remove_program(c, instance, slot)}[action]
    try:
        cfg = _t3_require(hass, instance, slot)
        result = await _t3_run(hass, instance, cfg, fn)
    except T3_ERRORS as err:
        connection.send_error(msg["id"], "t3000", str(err))
        return
    if action == "remove":
        await _panel_changed(hass, instance, slot)
    status = {"start": "running", "stop": "stopped", "remove": "program_removed"}[action]
    await repo.record_deployment(msg["sheet_id"], {
        "target": last["target"], "rev": last["rev"], "deployed_at": datetime.now(UTC).isoformat(), "by": _user_label(connection),
        "status": status, "kind": "t3000", "sha256": last["sha256"], "logic": logic, "backup": last.get("backup"), "backup_of": None})
    connection.send_result(msg["id"], {**result, "status": status})


def _t3_loaded_logic(hass, sheet_id: str, last: dict) -> str | None:
    """Logic digest of a load recorded before 0.6.2: rebuilt from its revision, used only if it reproduces the recorded hash."""
    try:
        sheet = _repo(hass).get_revision(sheet_id, last["rev"])["sheet"]
        built = _t3_compile(sheet, hass.data[DOMAIN]["conf"].get("t3000", {}).get("devices", {}))
        code = bytes.fromhex(built["code_hex"])
    except (RepoError, KeyError, ValueError):
        return None
    return t3000_import.logic_digest(code) if hashlib.sha256(code).hexdigest() == last["sha256"] else None


@websocket_api.websocket_command({vol.Required("type"): "bas_wiresheet/push_result", vol.Required("result"): dict})
@callback
def ws_push_result(hass, connection, msg):
    """Runtime -> integration channel used by the generated Node-RED tab.

    Admin users only (the Node-RED server node's token). Not the HA event bus, so
    nothing is written to the recorder. Results are kept in memory (last per sheet).
    """
    if not connection.user or not connection.user.is_admin:
        connection.send_error(msg["id"], "unauthorized", "Only the runtime (admin token) may push results")
        return
    res = msg["result"]
    sid = res.get("sheet_id")
    if res.get("schema") != "bas-wiresheet/1" or not isinstance(sid, str) or not ID_RE.match(sid):
        connection.send_error(msg["id"], "invalid", "Not a bas-wiresheet/1 result")
        return
    if len(json.dumps(res)) > MAX_RESULT_BYTES:
        connection.send_error(msg["id"], "invalid", "Result too large")
        return
    try:
        _repo(hass).get(sid)
    except RepoError:
        connection.send_error(msg["id"], "not_found", f"Unknown sheet {sid}")
        return
    item = {"received_at": time.time() * 1000, "data": res}
    hass.data[DOMAIN]["live"][sid] = item
    async_dispatcher_send(hass, SIGNAL_LIVE.format(sid), item)
    connection.send_result(msg["id"])


@websocket_api.websocket_command({vol.Required("type"): "bas_wiresheet/subscribe_live", vol.Required("sheet_id"): str})
@callback
def ws_subscribe_live(hass, connection, msg):
    sid = msg["sheet_id"]

    @callback
    def forward(item: dict) -> None:
        connection.send_message(websocket_api.event_message(msg["id"], item))

    connection.subscriptions[msg["id"]] = async_dispatcher_connect(hass, SIGNAL_LIVE.format(sid), forward)
    connection.send_result(msg["id"])
    last = hass.data[DOMAIN]["live"].get(sid)
    if last:
        connection.send_message(websocket_api.event_message(msg["id"], last))


@websocket_api.websocket_command({vol.Required("type"): "bas_wiresheet/live_states",
                                  vol.Required("entities"): vol.All([str], vol.Length(max=300))})
@callback
def ws_live_states(hass, connection, msg):
    """Read only. Current states with last_reported, for Live Monitoring calculated in the browser (no Node-RED needed)."""
    out = {}
    for eid in msg["entities"]:
        st = hass.states.get(eid)
        out[eid] = None if st is None else [st.state, dict(st.attributes), st.last_reported.timestamp() * 1000,
                                            st.last_changed.timestamp() * 1000]
    connection.send_result(msg["id"], {"now": time.time() * 1000, "states": out})


HISTORY_ACTIONS = {"loaded": "program_load", "running": "program_start", "stopped": "program_stop", "program_removed": "program_remove",
                   "failed": "program_load"}


@websocket_api.websocket_command({vol.Required("type"): "bas_wiresheet/history", vol.Optional("limit", default=500): vol.All(int, vol.Range(min=1, max=2000))})
@callback
def ws_history(hass, connection, msg):
    """Read only. One list of who changed what: controller programs, point writes, deployments, revisions and access."""
    repo = _repo(hass)
    names = {d["instance"]: d["name"] for d in t3000.list_devices(hass, hass.data[DOMAIN]["conf"].get("t3000", {}))}
    rows: list[dict] = []
    for a in repo.audit():
        rows.append({k: a.get(k) for k in ("at", "by", "action", "target", "ok", "detail", "sheet")})
    audited = {a.get("backup_at") for a in repo.audit() if a.get("backup_at")}
    for target, items in (repo._data.get("program_backups") or {}).items():
        for b in items:
            if b.get("saved_at") not in audited:
                rows.append({"at": b.get("saved_at"), "by": b.get("by"), "action": "code_update", "target": target, "ok": True,
                             "detail": "Program code updated (previous version stored)", "sheet": None})
    for s in repo.all_sheets():
        name = s["draft"].get("name", s["id"])
        for r in s["revisions"]:
            rows.append({"at": r["saved_at"], "by": r["saved_by"], "action": "revision", "target": None, "ok": True,
                         "detail": f"Revision r{r['rev']}" + (f": {r['note']}" if r.get("note") else ""), "sheet": s["id"], "sheet_name": name})
        for d in s["deployments"]:
            kind = HISTORY_ACTIONS.get(d.get("status"), "deploy") if d.get("kind") == "t3000" else ("rollback" if d.get("status") == "rolled_back" else "deploy")
            rows.append({"at": d.get("deployed_at"), "by": d.get("by"), "action": kind, "target": d.get("target"),
                         "ok": d.get("status") != "failed", "detail": f"r{d.get('rev')} · {d.get('status')}", "sheet": s["id"], "sheet_name": name})
    sheet_names = {s["id"]: s["draft"].get("name", s["id"]) for s in repo.all_sheets()}
    for r in rows:
        r.setdefault("sheet_name", sheet_names.get(r.get("sheet")))
        t = str(r.get("target") or "")
        if t.startswith("t3000:"):
            parts = t.split(":")
            inst = int(parts[1]) if parts[1].isdigit() else None
            label = f"{names.get(inst, 'Controller')} ({parts[1]})"
            sub = parts[2] if len(parts) > 2 else ""
            r["target_label"] = f"{label} · program {sub}" if sub.isdigit() else f"{label} · {sub}" if sub else label
        else:
            r["target_label"] = t or None
    rows.sort(key=lambda r: r.get("at") or "", reverse=True)
    connection.send_result(msg["id"], {"items": rows[:msg["limit"]], "total": len(rows)})


@websocket_api.websocket_command({vol.Required("type"): "bas_wiresheet/point_history", vol.Required("entity_id"): str,
                                  vol.Optional("hours", default=6): vol.All(vol.Coerce(float), vol.Range(min=0.25, max=168))})
@websocket_api.async_response
async def ws_point_history(hass, connection, msg):
    """Read only. Recorded values of one entity for a trend chart (at most 1500 points)."""
    from homeassistant.components.recorder import get_instance, history
    eid = msg["entity_id"]
    st = hass.states.get(eid)
    if st is None:
        connection.send_error(msg["id"], "not_found", f"{eid} is not a Home Assistant entity")
        return
    end = datetime.now(UTC)
    start = end - timedelta(hours=msg["hours"])

    def load():
        return history.state_changes_during_period(hass, start, end, eid, True, False, None, True).get(eid, [])

    try:
        states = await get_instance(hass).async_add_executor_job(load)
    except Exception as err:  # noqa: BLE001 - recorder not available
        connection.send_error(msg["id"], "history_unavailable", str(err))
        return
    pts = [[x.last_changed.timestamp() * 1000, x.state] for x in states]
    if len(pts) > 1500:
        step = len(pts) / 1500
        pts = [pts[int(i * step)] for i in range(1500)] + [pts[-1]]
    connection.send_result(msg["id"], {"entity_id": eid, "unit": st.attributes.get("unit_of_measurement"), "name": st.attributes.get("friendly_name", eid),
                                       "start": start.timestamp() * 1000, "end": end.timestamp() * 1000, "points": pts, "current": st.state})


@websocket_api.websocket_command({vol.Required("type"): "bas_wiresheet/target_status", vol.Optional("target", default=TARGET_TEST): str})
@websocket_api.async_response
async def ws_target_status(hass, connection, msg):
    client = hass.data[DOMAIN]["nodered"].get(msg["target"])
    if not client:
        connection.send_error(msg["id"], "no_target", f"Deploy target {msg['target']} is not configured")
        return
    try:
        info = await client.info()
    except NodeRedError as err:
        connection.send_result(msg["id"], {"reachable": False, "error": str(err)})
        return
    connection.send_result(msg["id"], {"reachable": True, **info})


# ---------------- editor commands ----------------

def _check_sheet_shape(sheet: dict) -> str | None:
    if not isinstance(sheet.get("id"), str) or not ID_RE.match(sheet["id"]):
        return "Sheet id must be 1-64 characters: letters, digits, '-' or '_'"
    if len(json.dumps(sheet)) > 512_000:
        return "Sheet is too large"
    return None


@websocket_api.websocket_command({vol.Required("type"): "bas_wiresheet/save_draft", vol.Required("sheet"): dict, vol.Optional("create", default=False): bool})
@websocket_api.async_response
async def ws_save_draft(hass, connection, msg):
    if not _require_editor(hass, connection, msg):
        return
    if not _sheet_allowed(hass, connection, msg, msg["sheet"].get("id")):
        return
    err = _check_sheet_shape(msg["sheet"])
    if err:
        connection.send_error(msg["id"], "invalid", err)
        return
    try:
        await _repo(hass).save_draft(msg["sheet"], _user_label(connection), create_only=msg["create"])
    except RepoError as e:
        connection.send_error(msg["id"], "invalid", str(e))
        return
    issues = validate_sheet(msg["sheet"], known_entities=_known_entities(hass))
    connection.send_result(msg["id"], {"saved": True, "valid": not has_errors(issues), "issues": issues})


@websocket_api.websocket_command({vol.Required("type"): "bas_wiresheet/import", vol.Required("sheet"): dict})
@websocket_api.async_response
async def ws_import(hass, connection, msg):
    if not _require_editor(hass, connection, msg):
        return
    if not _sheet_allowed(hass, connection, msg, msg["sheet"].get("id")):
        return
    sheet = msg["sheet"]
    issues = validate_sheet(sheet, known_entities=_known_entities(hass))
    hard = [i for i in issues if i["code"] in ("schema", "sheet_type", "sheet_id", "blocks", "wires")]
    if hard:
        connection.send_error(msg["id"], "invalid", hard[0]["message"])
        return
    await _repo(hass).save_draft(sheet, _user_label(connection))
    connection.send_result(msg["id"], {"imported": sheet["id"], "valid": not has_errors(issues), "issues": issues})


@websocket_api.websocket_command({vol.Required("type"): "bas_wiresheet/commit", vol.Required("sheet_id"): str, vol.Optional("note", default=""): str})
@websocket_api.async_response
async def ws_commit(hass, connection, msg):
    if not _require_editor(hass, connection, msg):
        return
    if not _sheet_allowed(hass, connection, msg, msg["sheet_id"]):
        return
    try:
        r = await _repo(hass).commit(msg["sheet_id"], _user_label(connection), msg["note"], _known_entities(hass))
    except RepoError as e:
        connection.send_error(msg["id"], "invalid", str(e))
        return
    connection.send_result(msg["id"], {"rev": r["rev"], "saved_at": r["saved_at"]})


@websocket_api.websocket_command({vol.Required("type"): "bas_wiresheet/restore", vol.Required("sheet_id"): str, vol.Required("revision"): int})
@websocket_api.async_response
async def ws_restore(hass, connection, msg):
    if not _require_editor(hass, connection, msg):
        return
    if not _sheet_allowed(hass, connection, msg, msg["sheet_id"]):
        return
    try:
        await _repo(hass).restore(msg["sheet_id"], msg["revision"], _user_label(connection))
    except RepoError as e:
        connection.send_error(msg["id"], "not_found", str(e))
        return
    connection.send_result(msg["id"], {"restored": msg["revision"]})


def _target_client(hass, connection, msg) -> NodeRedClient | None:
    target = msg.get("target", TARGET_TEST)
    if target == TARGET_PRODUCTION:
        connection.send_error(msg["id"], "locked", "Production publish is locked pending explicit approval")
        return None
    client = hass.data[DOMAIN]["nodered"].get(target)
    if not client:
        connection.send_error(msg["id"], "no_target", f"Deploy target {target} is not configured")
    return client


def _digest(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True).encode()).hexdigest()


@websocket_api.websocket_command({vol.Required("type"): "bas_wiresheet/deploy_preview", vol.Required("sheet_id"): str,
                                  vol.Required("revision"): int, vol.Optional("target", default=TARGET_TEST): str})
@websocket_api.async_response
async def ws_deploy_preview(hass, connection, msg):
    if not _require_editor(hass, connection, msg):
        return
    if not _sheet_allowed(hass, connection, msg, msg["sheet_id"]):
        return
    client = _target_client(hass, connection, msg)
    if not client:
        return
    repo = _repo(hass)
    try:
        sheet = repo.get_revision(msg["sheet_id"], msg["revision"])["sheet"]
    except RepoError as e:
        connection.send_error(msg["id"], "not_found", str(e))
        return
    issues = validate_sheet(sheet, known_entities=_known_entities(hass))
    if has_errors(issues):
        connection.send_error(msg["id"], "invalid", "Revision does not pass validation")
        return
    if sheet.get("mode", "monitoring") != "monitoring":
        connection.send_error(msg["id"], "invalid", "T3000 program sheets run on the controller and cannot be deployed to Node-RED")
        return
    payload = to_flow_payload(compile_sheet(sheet, server_id=client.server_id, revision=msg["revision"]))
    try:
        current = await client.get_flow(tab_id(msg["sheet_id"]))
    except NodeRedError as e:
        connection.send_error(msg["id"], "nodered", str(e))
        return
    token = _digest({"p": payload, "c": current, "t": time.time()})
    previews = hass.data[DOMAIN]["previews"]
    for k in [k for k, v in previews.items() if v["expires"] < time.time()]:
        previews.pop(k)
    previews[token] = {"sheet_id": msg["sheet_id"], "rev": msg["revision"], "target": msg["target"], "payload": payload,
                       "current_digest": _digest(current), "current": current, "expires": time.time() + PREVIEW_TTL_S,
                       "user": connection.user.id if connection.user else None}
    connection.send_result(msg["id"], {
        "token": token, "target": msg["target"], "target_name": client.name, "tab_id": payload["id"], "label": payload["label"],
        "diff": diff_flows(current, payload), "backup": "Current tab will be backed up before deploy" if current else "No existing tab; rollback removes the tab",
        "currently_deployed_rev": _deployed_rev(current), "expires_in_s": PREVIEW_TTL_S, "warnings": [i for i in issues if i["severity"] == "warning"],
    })


def _deployed_rev(flow: dict | None) -> int | None:
    if not flow:
        return None
    for e in flow.get("env", []):
        if e.get("name") == "BAS_WS_REVISION":
            try:
                return int(e.get("value"))
            except (TypeError, ValueError):
                return None
    return None


@websocket_api.websocket_command({vol.Required("type"): "bas_wiresheet/deploy", vol.Required("token"): str})
@websocket_api.async_response
async def ws_deploy(hass, connection, msg):
    if not _require_editor(hass, connection, msg):
        return
    p = hass.data[DOMAIN]["previews"].pop(msg["token"], None)
    if not p or p["expires"] < time.time():
        connection.send_error(msg["id"], "preview_required", "Preview expired or unknown; preview the deployment again")
        return
    if p["user"] != (connection.user.id if connection.user else None):
        connection.send_error(msg["id"], "preview_required", "The preview belongs to another user")
        return
    client = _target_client(hass, connection, {**msg, "target": p["target"]})
    if not client:
        return
    tid = p["payload"]["id"]
    try:
        current = await client.get_flow(tid)
        if _digest(current) != p["current_digest"]:
            connection.send_error(msg["id"], "conflict", "The deployed tab changed after the preview; preview again")
            return
        await client.put_flow(tid, p["payload"])
        check = await client.get_flow(tid)
    except NodeRedError as e:
        connection.send_error(msg["id"], "nodered", str(e))
        return
    ok = bool(check) and {n["id"] for n in check.get("nodes", [])} == {n["id"] for n in p["payload"]["nodes"]}
    entry = {"target": p["target"], "rev": p["rev"], "deployed_at": datetime.now(UTC).isoformat(), "by": _user_label(connection),
             "status": "deployed" if ok else "verify_failed", "kind": "deploy",
             "backup": {"existed": current is not None, "flow": current}, "backup_of": _deployed_rev(current)}
    await _repo(hass).record_deployment(p["sheet_id"], entry)
    connection.send_result(msg["id"], {"status": entry["status"], "rev": p["rev"], "tab_id": tid, "previous_rev": entry["backup_of"]})


@websocket_api.websocket_command({vol.Required("type"): "bas_wiresheet/rollback", vol.Required("sheet_id"): str, vol.Optional("target", default=TARGET_TEST): str})
@websocket_api.async_response
async def ws_rollback(hass, connection, msg):
    if not _require_editor(hass, connection, msg):
        return
    if not _sheet_allowed(hass, connection, msg, msg["sheet_id"]):
        return
    client = _target_client(hass, connection, msg)
    if not client:
        return
    repo = _repo(hass)
    try:
        last = next((d for d in reversed(repo.get(msg["sheet_id"])["deployments"]) if d["target"] == msg["target"] and d["kind"] == "deploy" and d["status"] != "rolled_back"), None)
    except RepoError as e:
        connection.send_error(msg["id"], "not_found", str(e))
        return
    if not last:
        connection.send_error(msg["id"], "nothing_to_rollback", "No deployment to roll back on this target")
        return
    tid = tab_id(msg["sheet_id"])
    try:
        current = await client.get_flow(tid)
        if last["backup"]["existed"]:
            await client.put_flow(tid, last["backup"]["flow"])
        else:
            await client.delete_flow(tid)
    except NodeRedError as e:
        connection.send_error(msg["id"], "nodered", str(e))
        return
    last["status"] = "rolled_back"
    restored = last.get("backup_of")
    await repo.record_deployment(msg["sheet_id"], {
        "target": msg["target"], "rev": restored, "deployed_at": datetime.now(UTC).isoformat(), "by": _user_label(connection),
        "status": "deployed" if last["backup"]["existed"] else "removed", "kind": "rollback",
        "backup": {"existed": current is not None, "flow": current}, "backup_of": last["rev"]})
    connection.send_result(msg["id"], {"restored_rev": restored, "removed": not last["backup"]["existed"]})
