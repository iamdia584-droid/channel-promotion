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


async def run_polling() -> None:
    """Run the bot by long-polling Telegram instead of receiving webhooks.

    Webhooks need a public HTTPS URL with a valid certificate, which means a server
    and a domain. Polling needs neither, so this is the way to run the bot from a
    laptop — for development, for a first trial, or as a fallback if the webhook
    endpoint is ever unreachable.

    Telegram refuses to deliver by both mechanisms at once, so any registered
    webhook is removed first.
    """
    bot = get_bot()
    dispatcher = get_dispatcher()

    try:
        await _polling_loop(bot, dispatcher)
    finally:
        # aiohttp warns about an unclosed session otherwise, which buries the real
        # error behind noise.
        await bot.session.close()


async def _polling_loop(bot: Bot, dispatcher: Dispatcher) -> None:
    await bot.delete_webhook(drop_pending_updates=False)
    await set_commands()

    me = await bot.get_me()
    log.info("polling_started", bot=f"@{me.username}", bot_id=me.id)
    print(f"Bot @{me.username} is live. Open https://t.me/{me.username} and send /start.")
    print("Press Ctrl+C to stop.")

    await dispatcher.start_polling(
        bot, allowed_updates=["message", "callback_query", "my_chat_member", "chat_member"]
    )


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
