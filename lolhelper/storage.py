"""Persistent plugin state: account bindings, session preferences, subscriptions."""

from __future__ import annotations

import asyncio
import secrets
import time
from pathlib import Path
from typing import Any

from .cache import JsonStore
from .constants import OPGG_REGIONS, cn_server_name

EMPTY = {"version": 1, "bindings": {}, "prefs": {}, "subs": [], "push_state": {}}


def account_key(acc: dict) -> str:
    if acc.get("kind") == "cn":
        return f"cn:{acc.get('server', '')}:{acc.get('puuid', '')}"
    return f"g:{acc.get('region', '')}:{str(acc.get('game_name', '')).lower()}#{str(acc.get('tag_line', '')).lower()}"


def sub_league_list(sub: dict) -> list[str]:
    """Leagues of an esports subscription; [] means every league. Supports the old single-league form."""
    t = sub.get("target") or {}
    if t.get("leagues"):
        return [str(x) for x in t["leagues"]]
    return [str(t["league"])] if t.get("league") else []


def account_label(acc: dict) -> str:
    rid = f"{acc.get('game_name')}#{acc.get('tag_line')}"
    if acc.get("kind") == "cn":
        return f"{rid}（国服·{cn_server_name(acc.get('server', ''))}）"
    return f"{rid}（{OPGG_REGIONS.get(acc.get('region', ''), acc.get('region', ''))}）"


class StateStore:
    def __init__(self, path: Path):
        self.store = JsonStore(path)
        self.data: dict[str, Any] = {k: (v.copy() if isinstance(v, (dict, list)) else v) for k, v in EMPTY.items()}
        self._dirty = False
        self._save_task: asyncio.Task | None = None

    async def load(self) -> None:
        data = await self.store.load(None)
        if isinstance(data, dict):
            for k, v in EMPTY.items():
                data.setdefault(k, v.copy() if isinstance(v, (dict, list)) else v)
            self.data = data

    async def save(self) -> None:
        await self.store.save(self.data)

    def _schedule_save(self) -> None:
        # coalesce frequent push-state updates into one write
        if self._save_task and not self._save_task.done():
            return

        async def later():
            await asyncio.sleep(2)
            await self.save()

        try:
            self._save_task = asyncio.get_running_loop().create_task(later())
        except RuntimeError:
            self.store.save_sync(self.data)

    # ----------------------------------------------------------------- bindings
    def accounts(self, user: str) -> list[dict]:
        return list((self.data["bindings"].get(user) or {}).get("accounts") or [])

    def default_account(self, user: str) -> dict | None:
        b = self.data["bindings"].get(user) or {}
        accs = b.get("accounts") or []
        if not accs:
            return None
        idx = int(b.get("default") or 0)
        return accs[idx] if 0 <= idx < len(accs) else accs[0]

    async def bind(self, user: str, account: dict) -> int:
        b = self.data["bindings"].setdefault(user, {"accounts": [], "default": 0})
        key = account_key(account)
        for i, acc in enumerate(b["accounts"]):
            if account_key(acc) == key:
                b["accounts"][i] = {**acc, **account}
                b["default"] = i
                await self.save()
                return i
        b["accounts"].append({**account, "bound_at": int(time.time())})
        b["default"] = len(b["accounts"]) - 1
        await self.save()
        return b["default"]

    async def unbind(self, user: str, index: int | None = None) -> list[dict]:
        b = self.data["bindings"].get(user)
        if not b:
            return []
        if index is None:
            removed = b["accounts"]
            self.data["bindings"].pop(user, None)
        else:
            if not 0 <= index < len(b["accounts"]):
                return []
            removed = [b["accounts"].pop(index)]
            if b["default"] >= len(b["accounts"]):
                b["default"] = max(0, len(b["accounts"]) - 1)
        await self.save()
        return removed

    async def set_default(self, user: str, index: int) -> dict | None:
        b = self.data["bindings"].get(user)
        if not b or not 0 <= index < len(b["accounts"]):
            return None
        b["default"] = index
        await self.save()
        return b["accounts"][index]

    # -------------------------------------------------------------------- prefs
    def mode(self, umo: str) -> str | None:
        return (self.data["prefs"].get(umo) or {}).get("mode")

    async def set_mode(self, umo: str, mode: str | None) -> None:
        p = self.data["prefs"].setdefault(umo, {})
        if mode:
            p["mode"] = mode
        else:
            p.pop("mode", None)
        await self.save()

    # ----------------------------------------------------------- subscriptions
    def subs(self, umo: str | None = None, kind: str | None = None) -> list[dict]:
        return [
            s for s in self.data["subs"]
            if (umo is None or s.get("umo") == umo) and (kind is None or s.get("type") == kind)
        ]

    async def add_sub(self, umo: str, kind: str, target: dict | None = None, by: str = "") -> tuple[dict, bool]:
        target = target or {}
        for s in self.data["subs"]:
            if s["umo"] == umo and s["type"] == kind and s.get("target") == target:
                return s, False
        sub = {"id": secrets.token_hex(3), "umo": umo, "type": kind, "target": target, "by": by, "created_at": int(time.time())}
        self.data["subs"].append(sub)
        await self.save()
        return sub, True

    async def remove_sub(self, umo: str, sub_id: str) -> int:
        before = len(self.data["subs"])
        self.data["subs"] = [
            s for s in self.data["subs"] if not (s["umo"] == umo and (sub_id == "all" or s["id"] == sub_id))
        ]
        removed = before - len(self.data["subs"])
        if removed:
            await self.save()
        return removed

    # --------------------------------------------------------------- push state
    def state(self, key: str, default: Any = None) -> Any:
        return self.data["push_state"].get(key, default)

    def set_state(self, key: str, value: Any) -> None:
        self.data["push_state"][key] = value
        self._schedule_save()

