"""Versioned sheet storage (Home Assistant .storage/bas_wiresheet.sheets)."""
from __future__ import annotations

import copy
from datetime import UTC, datetime
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .validation import has_errors, validate_sheet

STORAGE_KEY = "bas_wiresheet.sheets"
STORAGE_VERSION = 1
MAX_REVISIONS = 100
MAX_DEPLOYMENTS = 50


class RepoError(Exception):
    """Invalid repository operation."""


MAX_PROGRAM_BACKUPS = 10


def _now() -> str:
    return datetime.now(UTC).isoformat()


class SheetRepository:
    def __init__(self, hass: HomeAssistant) -> None:
        self._store: Store = Store(hass, STORAGE_VERSION, STORAGE_KEY, private=True, atomic_writes=True)
        self._data: dict[str, Any] = {"sheets": {}}

    async def async_load(self) -> None:
        self._data = await self._store.async_load() or {"sheets": {}}

    async def _save(self) -> None:
        await self._store.async_save(self._data)

    # ---- queries ----
    def list(self) -> list[dict]:
        out = []
        for s in self._data["sheets"].values():
            revs = s["revisions"]
            last_dep = s["deployments"][-1] if s["deployments"] else None
            out.append({
                "id": s["id"], "name": s["draft"].get("name", s["id"]),
                "draft_updated_at": s["draft_updated_at"], "draft_updated_by": s["draft_updated_by"],
                "latest_revision": revs[-1]["rev"] if revs else None,
                "draft_dirty": s.get("draft_dirty", True),
                "deployed": {k: last_dep[k] for k in ("target", "rev", "deployed_at", "status")} if last_dep else None,
                "editors": list(s.get("access", {}).get("editors", [])),
            })
        return sorted(out, key=lambda x: x["name"].lower())

    def get(self, sheet_id: str) -> dict:
        s = self._data["sheets"].get(sheet_id)
        if not s:
            raise RepoError(f"Sheet {sheet_id} not found")
        return s

    def get_revision(self, sheet_id: str, rev: int) -> dict:
        for r in self.get(sheet_id)["revisions"]:
            if r["rev"] == rev:
                return r
        raise RepoError(f"Revision {rev} of {sheet_id} not found")

    def describe(self, sheet_id: str) -> dict:
        s = self.get(sheet_id)
        return {
            "id": s["id"], "draft": s["draft"], "draft_updated_at": s["draft_updated_at"],
            "draft_updated_by": s["draft_updated_by"], "draft_dirty": s.get("draft_dirty", True),
            "revisions": [{k: r[k] for k in ("rev", "saved_at", "saved_by", "note")} for r in s["revisions"]],
            "deployments": [{k: d.get(k) for k in ("target", "rev", "deployed_at", "by", "status", "backup_of", "kind")} for d in s["deployments"]],
            "ha_runtime": s.get("ha_runtime"),
            "access": {"editors": list(s.get("access", {}).get("editors", [])), "updated_at": s.get("access", {}).get("updated_at"),
                       "updated_by": s.get("access", {}).get("updated_by")},
        }

    async def set_ha_runtime(self, sheet_id: str, value: dict | None) -> None:
        s = self.get(sheet_id)
        if value is None:
            s.pop("ha_runtime", None)
        else:
            s["ha_runtime"] = value
        await self._save()

    def editors_of(self, sheet_id: str) -> list[str]:
        """User ids allowed to change this sheet; empty means every editor."""
        s = self._data["sheets"].get(sheet_id)
        return list(s.get("access", {}).get("editors", [])) if s else []

    async def set_editors(self, sheet_id: str, editors: list[str], user: str) -> None:
        s = self.get(sheet_id)
        s["access"] = {"editors": sorted(set(editors)), "updated_at": _now(), "updated_by": user}
        await self._save()

    # ---- mutations ----
    async def save_draft(self, sheet: dict, user: str, *, create_only: bool = False) -> dict:
        sid = sheet.get("id")
        if not isinstance(sid, str):
            raise RepoError("Sheet id is required")
        s = self._data["sheets"].get(sid)
        if s and create_only:
            raise RepoError(f"A sheet with id {sid} already exists")
        clean = copy.deepcopy(sheet)
        clean.pop("revision", None)
        if not s:
            s = self._data["sheets"][sid] = {"id": sid, "revisions": [], "deployments": []}
        s["draft"] = clean
        s["draft_updated_at"] = _now()
        s["draft_updated_by"] = user
        s["draft_dirty"] = not s["revisions"] or _strip(s["revisions"][-1]["sheet"]) != _strip(clean)
        await self._save()
        return s

    async def commit(self, sheet_id: str, user: str, note: str, known_entities: dict | None) -> dict:
        s = self.get(sheet_id)
        issues = validate_sheet(s["draft"], known_entities=known_entities)
        if has_errors(issues):
            raise RepoError("The draft has validation errors; fix them before saving a revision")
        rev = (s["revisions"][-1]["rev"] + 1) if s["revisions"] else 1
        snap = copy.deepcopy(s["draft"])
        snap["revision"] = rev
        s["revisions"].append({"rev": rev, "saved_at": _now(), "saved_by": user, "note": note or "", "sheet": snap})
        if len(s["revisions"]) > MAX_REVISIONS:
            s["revisions"] = s["revisions"][-MAX_REVISIONS:]
        s["draft_dirty"] = False
        await self._save()
        return s["revisions"][-1]

    async def restore(self, sheet_id: str, rev: int, user: str) -> dict:
        r = self.get_revision(sheet_id, rev)
        return await self.save_draft(r["sheet"], user)

    async def record_deployment(self, sheet_id: str, entry: dict) -> None:
        s = self.get(sheet_id)
        s["deployments"].append(entry)
        if len(s["deployments"]) > MAX_DEPLOYMENTS:
            s["deployments"] = s["deployments"][-MAX_DEPLOYMENTS:]
        await self._save()

    async def add_audit(self, entry: dict) -> None:
        """Controller and access actions, newest last (kept: 2000)."""
        items = self._data.setdefault("audit", [])
        items.append({"at": _now(), **entry})
        del items[:-2000]
        await self._save()

    def audit(self) -> list[dict]:
        return list(self._data.get("audit", []))

    def all_sheets(self) -> list[dict]:
        return list(self._data["sheets"].values())

    async def add_program_backup(self, target: str, entry: dict) -> None:
        items = self._data.setdefault("program_backups", {}).setdefault(target, [])
        items.append(entry)
        del items[:-MAX_PROGRAM_BACKUPS]
        await self._save()

    def program_backups(self, target: str) -> list[dict]:
        return self._data.get("program_backups", {}).get(target, [])

    async def pop_program_backup(self, target: str) -> None:
        self._data["program_backups"][target].pop()
        await self._save()

    def active_targets(self, sheet_id: str) -> list[str]:
        """Targets where this sheet is still deployed or loaded."""
        last: dict[str, dict] = {}
        for d in self.get(sheet_id)["deployments"]:
            last[d["target"]] = d
        return sorted(t for t, d in last.items() if d.get("status") not in ("removed", "rolled_back", "program_removed", "failed"))

    async def delete(self, sheet_id: str) -> None:
        active = self.active_targets(sheet_id)
        if active:
            raise RepoError(f"Sheet is still active on: {', '.join(active)}. Roll back or remove it first")
        del self._data["sheets"][sheet_id]
        await self._save()

    def last_deployment(self, sheet_id: str, target: str) -> dict | None:
        for d in reversed(self.get(sheet_id)["deployments"]):
            if d["target"] == target:
                return d
        return None


def _strip(sheet: dict) -> dict:
    c = copy.deepcopy(sheet)
    c.pop("revision", None)
    return c
