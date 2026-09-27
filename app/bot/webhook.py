"""Telegram webhook endpoint (spec §28: verified, not merely obscure)."""

from __future__ import annotations

from aiogram.types import Update
from fastapi import APIRouter, Header, Request, Response

from app.core.config import settings
from app.core.errors import RateLimited, SuspendedAccount
from app.core.logging import get_logger
from app.core.security import verify_webhook_secret

log = get_logger(__name__)

router = APIRouter(tags=["telegram"])


@router.post(settings.telegram_webhook_path)
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(None),
) -> Response:
    """Validate, feed the dispatcher, and always return 200.

    Telegram retries any non-2xx response, so a bug in one handler must not turn
    into an infinite redelivery loop. Errors are logged and swallowed here.
    """
    verify_webhook_secret(x_telegram_bot_api_secret_token)

    from app.bot.dispatcher import get_bot, get_dispatcher

    try:
        payload = await request.json()
        # Parsing is inside the try on purpose: a malformed update must not
        # produce a 500, because Telegram would then redeliver it forever.
        update = Update.model_validate(payload, context={"bot": get_bot()})
        await get_dispatcher().feed_update(get_bot(), update)
    except SuspendedAccount as exc:
        log.info("suspended_account_update", reason=exc.message)
    except RateLimited:
        log.info("bot_rate_limited")
    except Exception as exc:
        log.exception("bot_update_failed", error=str(exc))
    return Response(status_code=200)
