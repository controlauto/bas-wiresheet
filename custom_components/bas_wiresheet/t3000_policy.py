"""T3000 point policy: port classification and write protection (no Home Assistant imports)."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# Physical output ranges per model (1-based port index), from T3000 GetOutputType().
# Inputs: physical analog inputs per model (product IO table). None = unknown.
MODEL_PROFILES: dict[str, dict[str, Any]] = {
    "T3-BB": {"out_digital": (1, 12), "out_analog": (13, 24), "inputs": 32, "source": "BIG_MINIPANEL/MINIPANELARM: 12DO, 12AO"},
    "T3-LB": {"out_digital": (1, 6), "out_analog": (7, 10), "inputs": 16, "source": "MINIPANELARM_LB: 6DO, 4AO"},
    "T3-TB": {"out_digital": (1, 8), "out_analog": (9, 14), "inputs": 8, "source": "MINIPANELARM_TB: 8DO, 6AO"},
    "T3-TB-11I": {"out_digital": (1, 6), "out_analog": (7, 11), "inputs": 11, "source": "T3_TB_11I: 6DO, 5AO"},
    "TSTAT10": {"out_digital": (1, 5), "out_analog": (6, 7), "inputs": None, "source": "T3_TSTAT10/11/HUB: 5DO, 2AO"},
}

# Generic safety points: never writable unless the operator sets unlock_protected_points.
SAFETY_PATTERNS = [
    ("Protected safety point", re.compile(r"interlock|freeze|frost|safety|lockout|e[-_ ]?stop|emergency|high[-_ ]?limit|low[-_ ]?limit|low[-_ ]?water|fire|smoke")),
]
# Site equipment that is locked (for example Commissioning Pending), from the t3000: locked_equipment setting.
SITE_PATTERNS: list[tuple[str, re.Pattern]] = []
GLOBAL_LOCK_PATTERNS = SAFETY_PATTERNS  # kept name for callers; site patterns are checked as well


def set_site_patterns(items: list[dict]) -> None:
    SITE_PATTERNS[:] = [(i["name"], re.compile(i["pattern"], re.I)) for i in items or []]


def site_match(text: str) -> str | None:
    for name, rx in SITE_PATTERNS:
        if rx.search(text or ""):
            return name
    return None


RANGE_RE = re.compile(r"^(IN|OUT|VAR)(\d+)(?:-(\d+))?$")


def parse_ranges(items: list[str]) -> dict[str, set[int]]:
    """['OUT1-10', 'IN1-16', 'VAR120-128'] -> {'OUT': {1..10}, ...}."""
    out: dict[str, set[int]] = {"IN": set(), "OUT": set(), "VAR": set()}
    for raw in items or []:
        m = RANGE_RE.match(str(raw).strip().upper().replace(" ", ""))
        if not m:
            raise ValueError(f"Invalid point range {raw!r}; use e.g. OUT1-10, VAR120-128, IN5")
        lo = int(m.group(2))
        hi = int(m.group(3) or lo)
        if hi < lo:
            raise ValueError(f"Invalid point range {raw!r}")
        out[m.group(1)].update(range(lo, hi + 1))
    return out


def port_kind(model: str | None, category: str, index: int) -> str:
    """'physical-digital' | 'physical-analog' | 'physical' | 'virtual' | 'unknown' for a T3 point."""
    prof = MODEL_PROFILES.get(model or "")
    if category == "VAR":
        return "virtual"
    if not prof:
        return "unknown"
    if category == "OUT":
        d0, d1 = prof["out_digital"]
        a0, a1 = prof["out_analog"]
        if d0 <= index <= d1:
            return "physical-digital"
        if a0 <= index <= a1:
            return "physical-analog"
        return "virtual"
    if category == "IN":
        n = prof["inputs"]
        if n is None:
            return "unknown"
        return "physical" if index <= n else "virtual"
    return "unknown"


@dataclass
class DeviceConfig:
    model: str | None
    locked: dict[str, set[int]]
    sandbox: dict[str, set[int]]


def device_configs(conf: dict) -> dict[int, DeviceConfig]:
    out: dict[int, DeviceConfig] = {}
    for inst, d in (conf.get("devices") or {}).items():
        out[int(inst)] = DeviceConfig(
            model=d.get("model"),
            locked=parse_ranges(d.get("locked", [])),
            sandbox=parse_ranges(d.get("sandbox", [])),
        )
    return out


def protection(cfg: DeviceConfig | None, category: str, index: int, text: str, unlocked: bool = False) -> tuple[str, str | None]:
    """('locked'|'sandbox'|'normal', reason). Lock always wins over sandbox.

    unlocked=True (operator setting unlock_protected_points): safety and Commissioning Pending patterns no longer lock;
    they are reported as 'normal' with the reason kept for display. A device's own `locked` list still applies.
    """
    low = text.lower()
    for reason, rx in [*SAFETY_PATTERNS, *SITE_PATTERNS]:
        if rx.search(low):
            if unlocked:
                break
            return "locked", reason
    if cfg and index in cfg.locked.get(category, set()):
        return "locked", "Locked in configuration"
    if cfg and index in cfg.sandbox.get(category, set()):
        return "sandbox", "Test sandbox"
    return "normal", None


def make_guard(devices: dict, instance: int, full: bool = False, labels: dict[str, str] | None = None, unlocked: bool = False):
    """Write guard for a T3000 program.

    Restricted (default): only sandbox points on virtual ports of a configured device.
    Full access: any point, physical or virtual, except points locked by the safety and commissioning patterns or by
    the device's own `locked` list. Labels let the safety patterns see what a point is.
    """
    cfg = device_configs({"devices": devices}).get(int(instance))
    labels = labels or {}

    def full_guard(category: str, index: int) -> tuple[bool, str]:
        name = f"{category}{index}"
        status, reason = protection(cfg, category, index, f"{name} {labels.get(name, '')}", unlocked)
        if status == "locked":
            return False, reason or "locked"
        return True, "full access"

    if full:
        return full_guard

    def guard(category: str, index: int) -> tuple[bool, str]:
        if cfg is None:
            return False, f"device {instance} is not configured"
        kind = port_kind(cfg.model, category, index)
        if kind != "virtual":
            return False, f"port is {kind}; programs may only write virtual points"
        status, reason = protection(cfg, category, index, f"{category}{index}")
        if status != "sandbox":
            return False, reason or "not in the device sandbox"
        return True, "sandbox"

    return guard
