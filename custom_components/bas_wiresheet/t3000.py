"""T3000-style Variable Explorer (phase B: read-only).

Reads TEMCO controllers through the entities that the installed BACnet/IP
integration (domain ``bacnet``) already keeps up to date. No BACnet request is
sent from here. Values come from the live State objects inside Home Assistant,
so ``last_reported`` is the real time of the last write (not a cached copy).

Port classification follows T3000's GetOutputType() / product IO tables
(temcocontrols/T3000_Building_Automation_System, T3000/global_function.cpp).
"""
from __future__ import annotations

import time

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .t3000_policy import (  # noqa: F401  (re-exported)
    GLOBAL_LOCK_PATTERNS, MODEL_PROFILES, RANGE_RE, DeviceConfig, device_configs, parse_ranges, port_kind, protection,
)

BACNET_DOMAIN = "bacnet"

CATEGORY_BY_TYPE = {
    "Analog Input": "IN", "Binary Input": "IN", "Multi-state Input": "IN",
    "Analog Output": "OUT", "Binary Output": "OUT", "Multi-state Output": "OUT",
    "Analog Value": "VAR", "Binary Value": "VAR", "Multi-state Value": "VAR",
}


def _all_devices(dev_reg: dr.DeviceRegistry) -> list[dr.DeviceEntry]:
    """Newer Home Assistant iterates device entries; older releases iterate device ids."""
    items = list(dev_reg.devices)
    if items and isinstance(items[0], str):
        return [dev_reg.async_get(i) for i in items]
    return items


def _bacnet_instance(device: dr.DeviceEntry) -> int | None:
    for dom, ident in device.identifiers:
        if dom == BACNET_DOMAIN:
            try:
                return int(ident)
            except (TypeError, ValueError):
                return None
    return None


def _entry_options(hass: HomeAssistant, device: dr.DeviceEntry) -> dict:
    entry_ids = [device.config_entry_id] if hasattr(device, "config_entry_id") else device.config_entries
    for entry_id in entry_ids:
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry and entry.domain == BACNET_DOMAIN:
            return {"title": entry.title, "state": str(entry.state.value if hasattr(entry.state, "value") else entry.state),
                    "disabled": entry.disabled_by is not None, "polling_interval": entry.options.get("polling_interval"),
                    "cov": entry.options.get("enable_cov")}
    return {}


def _heartbeat(hass: HomeAssistant, entities: list[er.RegistryEntry]) -> dict:
    """Device heartbeat: the integration's 'last successful poll' timestamp sensor."""
    for ent in entities:
        if ent.domain == "sensor" and ent.entity_id.endswith("_last_successful_poll"):
            st = hass.states.get(ent.entity_id)
            if st and st.state not in ("unknown", "unavailable"):
                from homeassistant.util import dt as dt_util
                ts = dt_util.parse_datetime(st.state)
                if ts:
                    return {"entity_id": ent.entity_id, "last_poll": ts.timestamp() * 1000}
            return {"entity_id": ent.entity_id, "last_poll": None}
    return {"entity_id": None, "last_poll": None}


def is_temco(manufacturer: str | None, model: str | None) -> bool:
    return "temco" in f"{manufacturer or ''} {model or ''}".lower()


def list_devices(hass: HomeAssistant, conf: dict, include_all: bool = False) -> list[dict]:
    """TEMCO controllers in the BACnet/IP integration (and any device configured under t3000: devices).

    include_all=True also returns other BACnet devices (used only to report what discovery found)."""
    dev_reg, ent_reg = dr.async_get(hass), er.async_get(hass)
    cfgs = device_configs(conf)
    out = []
    for device in _all_devices(dev_reg):
        inst = _bacnet_instance(device)
        if inst is None:
            continue
        if not include_all and not is_temco(device.manufacturer, device.model) and inst not in cfgs:
            continue
        entities = er.async_entries_for_device(ent_reg, device.id, include_disabled_entities=False)
        cfg = cfgs.get(inst)
        counts = {"IN": 0, "OUT": 0, "VAR": 0, "OTHER": 0}
        for ent in entities:
            st = hass.states.get(ent.entity_id)
            otype = st.attributes.get("bacnet_object_type") if st else None
            if otype:
                counts[CATEGORY_BY_TYPE.get(otype, "OTHER")] += 1
        out.append({
            "device_id": device.id, "instance": inst, "name": device.name_by_user or device.name,
            "manufacturer": device.manufacturer, "reported_model": device.model, "sw_version": device.sw_version,
            "model": cfg.model if cfg else None, "model_source": MODEL_PROFILES.get(cfg.model, {}).get("source") if cfg and cfg.model else None,
            "entry": _entry_options(hass, device), "heartbeat": _heartbeat(hass, entities), "counts": counts,
            "configured": cfg is not None,
        })
    # controllers configured by IP address that the BACnet/IP integration does not know (for example a Tstat10 that
    # answers as a routed device): reached directly by T3000 requests, no Home Assistant entities
    seen = {d["instance"] for d in out}
    for inst, d in sorted((conf.get("devices") or {}).items()):
        if int(inst) in seen or not d.get("address"):
            continue
        model = d.get("model")
        out.append({
            "device_id": f"{DIRECT_PREFIX}{int(inst)}", "instance": int(inst), "name": d.get("name") or f"Controller {inst}",
            "manufacturer": "TEMCO", "reported_model": model, "sw_version": None, "model": model,
            "model_source": MODEL_PROFILES.get(model or "", {}).get("source"), "entry": {"device_address": d.get("address")},
            "heartbeat": {"last_poll": None}, "counts": {"IN": 0, "OUT": 0, "VAR": 0, "OTHER": 0}, "configured": True, "direct": True,
        })
    return sorted(out, key=lambda d: (d["name"] or "").lower())


DIRECT_PREFIX = "t3direct:"


def list_points(hass: HomeAssistant, conf: dict, device_id: str) -> dict:
    if device_id.startswith(DIRECT_PREFIX):  # reached by IP only: points come from the controller tables, not entities
        inst = int(device_id[len(DIRECT_PREFIX):])
        d = (conf.get("devices") or {}).get(inst) or (conf.get("devices") or {}).get(str(inst))
        if not d:
            raise ValueError("Not a configured controller")
        model = d.get("model")
        return {"device": {"device_id": device_id, "instance": inst, "name": d.get("name") or f"Controller {inst}", "model": model,
                           "model_source": MODEL_PROFILES.get(model or "", {}).get("source"), "entry": {"device_address": d.get("address")},
                           "heartbeat": {"last_poll": None}, "stale_after_s": 0, "direct": True}, "points": [], "now": time.time() * 1000}
    dev_reg, ent_reg = dr.async_get(hass), er.async_get(hass)
    device = dev_reg.async_get(device_id)
    if device is None or _bacnet_instance(device) is None:
        raise ValueError("Not a BACnet device")
    inst = _bacnet_instance(device)
    cfg = device_configs(conf).get(inst)
    model = cfg.model if cfg else None
    opts = _entry_options(hass, device)
    poll_s = opts.get("polling_interval") or 30
    stale_ms = max(3 * poll_s, 60) * 1000
    entities = er.async_entries_for_device(ent_reg, device.id, include_disabled_entities=False)
    hb = _heartbeat(hass, entities)
    now = time.time() * 1000
    rows: list[dict] = []
    for ent in entities:
        st = hass.states.get(ent.entity_id)
        if st is None:
            continue
        a = st.attributes
        otype = a.get("bacnet_object_type")
        if not otype:
            continue
        category = CATEGORY_BY_TYPE.get(otype, "OTHER")
        index = a.get("bacnet_instance")
        flags = a.get("bacnet_status_flags") or [False, False, False, False]
        flags = (list(flags) + [False] * 4)[:4]
        label = a.get("friendly_name") or ent.entity_id
        dev_name = device.name_by_user or device.name or ""
        if dev_name and label.startswith(dev_name + " "):
            label = label[len(dev_name) + 1:]
        text = " ".join(str(x) for x in (ent.entity_id, label, a.get("bacnet_description", "")))
        prot, reason = protection(cfg, category, int(index) if index is not None else -1, text, bool(conf.get("unlock_protected_points")))
        reported = st.last_reported.timestamp() * 1000
        unavailable = st.state in ("unavailable", "unknown")
        # Per point: the BACnet/IP integration writes every polled object on every poll, changed or
        # not (verified on a production installation: last_reported age == last poll age), so last_reported
        # read from the live State object is the arrival time of the point's last sample.
        age = now - reported
        if unavailable:
            freshness, fresh_reason = "Unavailable", f"Entity state is {st.state}"
        elif age <= stale_ms:
            freshness, fresh_reason = "Fresh", f"New sample {round(age / 1000)} s ago"
        elif hb["last_poll"] is not None and now - hb["last_poll"] <= stale_ms and a.get("bacnet_update_method") == "cov":
            freshness, fresh_reason = "Freshness Unknown", "COV point without a recent change; device itself is polled"
        else:
            dev_note = "" if hb["last_poll"] is None else f"; device last polled {round((now - hb['last_poll']) / 1000)} s ago"
            freshness, fresh_reason = "Stale", f"No new sample for {round(age / 1000)} s (limit {stale_ms // 1000} s){dev_note}"
        rows.append({
            "category": category, "index": index, "object_type": otype, "entity_id": ent.entity_id,
            "label": label, "description": a.get("bacnet_description"), "value": st.state,
            "units": a.get("unit_of_measurement") or (None if a.get("bacnet_units") in (None, "no-units") else a.get("bacnet_units")),
            "bacnet_units": a.get("bacnet_units"),
            "auto_manual": "Manual" if flags[3] else "Auto", "status_flags": {"in_alarm": flags[0], "fault": flags[1], "overridden": flags[2], "out_of_service": flags[3]},
            "commandable": bool(a.get("bacnet_commandable")), "update_method": a.get("bacnet_update_method"),
            "port": port_kind(model, category, int(index)) if index is not None else "unknown",
            "protection": prot, "protection_reason": reason,
            "last_reported": reported, "last_changed": st.last_changed.timestamp() * 1000,
            "freshness": freshness, "freshness_reason": fresh_reason,
        })
    order = {"IN": 0, "OUT": 1, "VAR": 2, "OTHER": 3}
    rows.sort(key=lambda r: (order.get(r["category"], 9), r["index"] if r["index"] is not None else 1e9))
    return {"device": {"device_id": device.id, "instance": inst, "name": device.name_by_user or device.name, "model": model,
                       "model_source": MODEL_PROFILES.get(model or "", {}).get("source"), "entry": opts, "heartbeat": hb,
                       "stale_after_s": stale_ms // 1000},
            "points": rows, "now": now}
