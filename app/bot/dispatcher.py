"""aiogram dispatcher wiring."""

from __future__ import annotations

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.redis import RedisStorage

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)

_bot: Bot | None = None
_dispatcher: Dispatcher | None = None


def get_bot() -> Bot:
    global _bot
    if _bot is None:
        if not settings.telegram_bot_token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")
        _bot = Bot(
            token=settings.telegram_bot_token,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
    return _bot


def _storage():
    """FSM state in Redis so wizard progress survives a restart or a second worker."""
    try:
        return RedisStorage.from_url(settings.redis_url)
    except Exception as exc:  # pragma: no cover - local development
        log.warning("redis_fsm_unavailable", error=str(exc))
        return MemoryStorage()


def get_dispatcher() -> Dispatcher:
    global _dispatcher
    if _dispatcher is None:
        dispatcher = Dispatcher(storage=_storage())
        from app.bot.handlers import admin, advertiser, common, publisher

        # Order matters: specific flows before the catch-all menu handlers.
        dispatcher.include_router(advertiser.router)
        dispatcher.include_router(publisher.router)
        dispatcher.include_router(admin.router)
        dispatcher.include_router(common.router)
        _dispatcher = dispatcher
    return _dispatcher


async def set_webhook() -> str:
    """Register the webhook with Telegram, including the verification secret."""
    bot = get_bot()
    url = settings.base_url.rstrip("/") + settings.telegram_webhook_path
    await bot.set_webhook(
        url=url,
        secret_token=settings.telegram_webhook_secret or None,
        drop_pending_updates=False,
        allowed_updates=["message", "callback_query", "my_chat_member", "chat_member"],
    )
    log.info("webhook_registered", url=url)
    return url


async def delete_webhook() -> None:
    await get_bot().delete_webhook(drop_pending_updates=False)


COMMANDS = [
    ("start", "Main menu"),
    ("wallet", "Your balance"),
    ("deposit", "Add funds"),
    ("campaign", "Create a campaign"),
    ("campaigns", "Your campaigns"),
    ("stats", "Statistics"),
    ("publisher", "Publisher menu"),
    ("channel", "Add a channel"),
    ("earnings", "Your earnings"),
    ("withdraw", "Request a payout"),
    ("transactions", "Transaction history"),
    ("profile", "Your account"),
    ("help", "Help"),
    ("support", "Contact support"),
    ("report", "Report an advertisement"),
]


async def set_commands() -> None:
    from aiogram.types import BotCommand

    await get_bot().set_my_commands([BotCommand(command=c, description=d) for c, d in COMMANDS])
