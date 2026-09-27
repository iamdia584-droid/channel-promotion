"""Measurement adapters (spec §8, §37).

Telegram's Bot API cannot read a post's view counter, so a view-derived
impression can only come from an external source the operator supplies. That
source is abstracted here and is **NullViewSource by default**: a fresh
deployment bills only impressions it measured itself.

This module deliberately contains no billing logic. It reports observations;
:mod:`app.services.impressions` decides what is billable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class ViewObservation:
    """A post's cumulative view count as reported by an external source."""

    telegram_chat_id: int
    telegram_message_id: int
    #: Cumulative, monotonic-in-principle. Callers must still ratchet it, because
    #: a source may under-report transiently.
    views: int
    source_name: str


class ViewSource(Protocol):
    """A source of Telegram-reported post view counts."""

    name: str
    available: bool

    def fetch(self, telegram_chat_id: int, message_ids: list[int]) -> list[ViewObservation]: ...


class NullViewSource:
    """The default. Reports nothing, so nothing view-derived can be billed.

    This is the honest default: claiming view numbers we cannot observe would be
    exactly the faked precision spec §37 forbids.
    """

    name = "null"
    available = False

    def fetch(self, telegram_chat_id: int, message_ids: list[int]) -> list[ViewObservation]:
        return []


class MTProtoViewSource:
    """Reads post views via an MTProto user session (phase 2).

    Requires ``MTPROTO_*`` configuration and a user account that can see the
    channel. Left unimplemented rather than faked: with no session configured,
    ``available`` is False and the delivery engine falls back to click-only
    measurement instead of inventing numbers.
    """

    name = "mtproto"

    def __init__(self) -> None:
        self._configured = bool(
            settings.mtproto_enabled
            and settings.mtproto_api_id
            and settings.mtproto_api_hash
            and settings.mtproto_session
        )
        if settings.mtproto_enabled and not self._configured:
            log.warning("mtproto_enabled_but_unconfigured")

    @property
    def available(self) -> bool:
        return False  # flipped on when the reader client is wired up

    def fetch(self, telegram_chat_id: int, message_ids: list[int]) -> list[ViewObservation]:
        raise NotImplementedError(
            "MTProto view reading is not wired up. Keep measurement_mode at "
            "CLICK_ONLY, or supply a ViewSource implementation via set_view_source()."
        )


class StaticViewSource:
    """Test/backfill source driven by an explicit table of counts."""

    name = "static"

    def __init__(self, counts: dict[tuple[int, int], int] | None = None) -> None:
        self.counts: dict[tuple[int, int], int] = counts or {}
        self.available = True

    def set(self, chat_id: int, message_id: int, views: int) -> None:
        self.counts[(chat_id, message_id)] = views

    def fetch(self, telegram_chat_id: int, message_ids: list[int]) -> list[ViewObservation]:
        out = []
        for message_id in message_ids:
            views = self.counts.get((telegram_chat_id, message_id))
            if views is not None:
                out.append(
                    ViewObservation(telegram_chat_id, message_id, int(views), self.name)
                )
        return out


_view_source: ViewSource | None = None


def get_view_source() -> ViewSource:
    global _view_source
    if _view_source is None:
        _view_source = MTProtoViewSource() if settings.mtproto_enabled else NullViewSource()
    return _view_source


def set_view_source(source: ViewSource | None) -> None:
    global _view_source
    _view_source = source
