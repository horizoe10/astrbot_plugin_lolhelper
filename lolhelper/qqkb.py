"""QQ official bots: turn the plugin's command buttons into QQ "指令" buttons.

AstrBot's portable buttons only know callbacks and links, and its QQ adapter drops images when it answers a
button click, so a callback button cannot bring back an image card. A QQ command button (action type 2) makes the
user send the command instead: the bot then answers an ordinary message and the card arrives as usual. enter=False
only fills the input box, for commands that still need an argument such as "/tft 英雄 ".

install() wraps the adapter's keyboard builder; only buttons whose id starts with PREFIX are changed, every other
button and the rest of the sending path stay AstrBot's own.
"""

from __future__ import annotations

PREFIX = "lolhelper:cmd:"
_MARK = "_lolhelper_orig"


def command_map(chain) -> dict[str, tuple[str, bool]]:
    """button id -> (command, send at once) for the plugin's command buttons in a message chain."""
    out: dict[str, tuple[str, bool]] = {}
    for comp in getattr(chain, "chain", None) or []:
        for b in getattr(comp, "buttons", None) or []:
            bid = str(getattr(b, "id", "") or "")
            data = getattr(getattr(b, "action", None), "data", None)
            if bid.startswith(PREFIX) and isinstance(data, dict) and data.get("c"):
                out[bid] = (str(data["c"]), bool(data.get("e", True)))
    return out


def rewrite(keyboard: dict | None, cmds: dict[str, tuple[str, bool]]) -> dict | None:
    """Swap the action of the listed buttons in a QQ keyboard payload for a command action."""
    if not keyboard or not cmds:
        return keyboard
    for row in (keyboard.get("content") or {}).get("rows") or []:
        for b in row.get("buttons") or []:
            hit = cmds.get(str(b.get("id")))
            if hit:
                cmd, enter = hit
                b["action"] = {"type": 2, "permission": {"type": 2}, "data": cmd if enter else cmd.rstrip() + " ",
                               "enter": enter, "reply": False, "unsupport_tips": f"请发送 {cmd.strip()}"}
    return keyboard


def _current(cls):
    cur = cls.__dict__.get("_parse_keyboard")
    return getattr(cur, "__func__", cur)


def install(cls) -> bool:
    """Wrap cls._parse_keyboard (a staticmethod); safe to call again after a plugin reload."""
    fn = _current(cls)
    if not callable(fn):
        return False
    orig = getattr(fn, _MARK, fn)

    def parse_keyboard(message_chain):
        keyboard = orig(message_chain)
        try:
            return rewrite(keyboard, command_map(message_chain))
        except Exception:
            return keyboard

    setattr(parse_keyboard, _MARK, orig)
    cls._parse_keyboard = staticmethod(parse_keyboard)
    return True


def uninstall(cls) -> None:
    fn = _current(cls)
    orig = getattr(fn, _MARK, None)
    if orig is not None:
        cls._parse_keyboard = staticmethod(orig)


def installed(cls) -> bool:
    return getattr(_current(cls), _MARK, None) is not None
