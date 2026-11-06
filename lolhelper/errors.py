"""Exception types shared by the plugin."""

from __future__ import annotations


class LolHelperError(Exception):
    """Base error. str(error) is safe to show to chat users."""


class UsageError(LolHelperError):
    """The user typed something we cannot understand."""


class NotFoundError(LolHelperError):
    """The requested summoner, champion, item or match does not exist."""


class UpstreamError(LolHelperError):
    """OP.GG, Data Dragon or another remote service failed."""


class BridgeError(LolHelperError):
    """The China server bridge is unavailable or returned an error."""

