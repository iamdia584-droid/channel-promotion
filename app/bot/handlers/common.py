"""Core bot flow: /start, main menu, role selection, wallet, help (spec §2, §3, §22)."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot import texts
from app.bot.keyboards import menus
from app.bot.session import bot_session, resolve_user
from app.core.money import fmt
from app.services.analytics import AnalyticsService
from app.services.earnings import EarningsService
from app.services.settings_service import SettingsService
from app.services.wallet import WalletService

router = Router(name="common")


async def _edit_or_send(event, text: str, keyboard=None) -> None:
    """Works for both a message and a callback, so handlers stay short."""
    if isinstance(event, CallbackQuery):
        try:
            await event.message.edit_text(text, reply_markup=keyboard)
        except Exception:
            await event.message.answer(text, reply_markup=keyboard)
        await event.answer()
    else:
        await event.answer(text, reply_markup=keyboard)


@router.message(CommandStart())
async def start(message: Message, state: FSMContext) -> None:
    await state.clear()
    with bot_session() as db:
        user = resolve_user(db, message.from_user)
        settings = SettingsService(db)
        platform = settings.str_("platform_name")
        is_staff = user.is_admin or user.is_moderator
        text = texts.welcome(platform, user.first_name or "there")
        keyboard = menus.main_menu(user.is_advertiser, user.is_publisher, is_staff)
    await message.answer(text, reply_markup=keyboard)


@router.callback_query(F.data == "menu")
async def main_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    with bot_session() as db:
        user = resolve_user(db, callback.from_user)
        platform = SettingsService(db).str_("platform_name")
        text = texts.welcome(platform, user.first_name or "there")
        keyboard = menus.main_menu(
            user.is_advertiser, user.is_publisher, user.is_admin or user.is_moderator
        )
    await _edit_or_send(callback, text, keyboard)


@router.callback_query(F.data == "role:advertiser")
async def become_advertiser(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    with bot_session() as db:
        from app.api.v1.auth import ensure_advertiser

        user = resolve_user(db, callback.from_user)
        settings = SettingsService(db)
        if not user.is_advertiser and not settings.bool_("registration_open"):
            await callback.answer("Registration is currently closed.", show_alert=True)
            return
        advertiser = ensure_advertiser(db, user)
        wallets = WalletService(db)
        view = wallets.view(wallets.for_advertiser(advertiser.id))
        currency = advertiser.currency
    text = (
        "<b>📣 Advertiser</b>\n\n"
        f"Available balance: <b>{fmt(view.available, currency)}</b>\n"
        f"Reserved in campaigns: {fmt(view.reserved, currency)}\n\n"
        "What would you like to do?"
    )
    await _edit_or_send(callback, text, menus.advertiser_menu())


@router.callback_query(F.data == "role:publisher")
async def become_publisher(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    with bot_session() as db:
        from app.api.v1.auth import ensure_publisher

        user = resolve_user(db, callback.from_user)
        settings = SettingsService(db)
        if not user.is_publisher and not settings.bool_("registration_open"):
            await callback.answer("Registration is currently closed.", show_alert=True)
            return
        publisher = ensure_publisher(db, user)
        summary = EarningsService(db).summary(publisher.id)
        currency = publisher.currency
        channels = len(publisher.channels)
    text = (
        "<b>📢 Publisher</b>\n\n"
        f"Channels registered: {channels}\n"
        f"Pending: {fmt(summary['pending'], currency)}\n"
        f"Confirmed (withdrawable): <b>{fmt(summary['confirmed'], currency)}</b>\n\n"
        "What would you like to do?"
    )
    await _edit_or_send(callback, text, menus.publisher_menu())


@router.callback_query(F.data == "wallet")
@router.message(Command("wallet"))
async def wallet(event, state: FSMContext) -> None:
    from_user = event.from_user
    with bot_session() as db:
        user = resolve_user(db, from_user)
        wallets = WalletService(db)
        blocks = []
        if user.is_advertiser and user.advertiser:
            view = wallets.view(wallets.for_advertiser(user.advertiser.id))
            blocks.append(texts.wallet_advertiser(view, user.advertiser.currency))
        if user.is_publisher and user.publisher:
            view = wallets.view(wallets.for_publisher(user.publisher.id))
            blocks.append(texts.wallet_publisher(view, user.publisher.currency))
        if not blocks:
            blocks.append(texts.not_registered("an advertiser or publisher"))
    await _edit_or_send(event, "\n\n".join(blocks), menus.back_to("menu"))


@router.callback_query(F.data == "transactions")
@router.message(Command("transactions"))
async def transactions(event, state: FSMContext) -> None:
    with bot_session() as db:
        user = resolve_user(db, event.from_user)
        wallets = WalletService(db)
        lines = ["<b>🧾 Recent transactions</b>\n"]
        rows = []
        if user.is_advertiser and user.advertiser:
            rows += wallets.statement(wallets.for_advertiser(user.advertiser.id).id, 10)
        if user.is_publisher and user.publisher:
            rows += wallets.statement(wallets.for_publisher(user.publisher.id).id, 10)
        rows.sort(key=lambda r: r.created_at, reverse=True)
        if not rows:
            lines.append("No transactions yet.")
        for row in rows[:15]:
            sign = "+" if row.signed_amount >= 0 else ""
            lines.append(
                f"{row.created_at:%d %b %H:%M} · {row.transaction_type} · "
                f"<b>{sign}{fmt(row.signed_amount, row.currency)}</b>"
            )
    await _edit_or_send(event, "\n".join(lines), menus.back_to("menu"))


@router.callback_query(F.data == "stats")
@router.message(Command("stats"))
async def stats(event, state: FSMContext) -> None:
    with bot_session() as db:
        user = resolve_user(db, event.from_user)
        analytics = AnalyticsService(db)
        blocks = []
        if user.is_advertiser and user.advertiser:
            overview = analytics.advertiser_overview(user.advertiser.id)
            currency = user.advertiser.currency
            blocks.append(
                "<b>📊 Advertiser statistics</b>\n\n"
                f"Campaigns: {overview['campaigns_active']} active of "
                f"{overview['campaigns_total']}\n"
                f"Spent: {fmt(overview['spend'], currency)}\n"
                f"Billable impressions: {overview['billable_impressions']:,}\n"
                f"Clicks: {overview['clicks']:,}\n"
                f"CTR: {overview['ctr_percent']}%\n"
                f"Effective CPM: {fmt(overview['effective_cpm'], currency)}"
            )
        if user.is_publisher and user.publisher:
            overview = analytics.publisher_overview(user.publisher.id)
            blocks.append(texts.publisher_stats(overview, user.publisher.currency))
        if not blocks:
            blocks.append(texts.not_registered("an advertiser or publisher"))
    await _edit_or_send(event, "\n\n".join(blocks), menus.back_to("menu"))


@router.callback_query(F.data == "stats:advertiser")
async def stats_advertiser(callback: CallbackQuery, state: FSMContext) -> None:
    await stats(callback, state)


@router.callback_query(F.data == "stats:publisher")
async def stats_publisher(callback: CallbackQuery, state: FSMContext) -> None:
    await stats(callback, state)


@router.callback_query(F.data == "help")
@router.message(Command("help"))
async def help_handler(event, state: FSMContext) -> None:
    with bot_session() as db:
        settings = SettingsService(db)
        text = texts.help_text(
            settings.str_("platform_name"), settings.str_("support_username") or None
        )
    await _edit_or_send(event, text, menus.back_to("menu"))


@router.callback_query(F.data == "support")
@router.message(Command("support"))
async def support(event, state: FSMContext) -> None:
    with bot_session() as db:
        handle = SettingsService(db).str_("support_username")
    text = (
        "<b>💬 Support</b>\n\n"
        + (f"Message @{handle.lstrip('@')}.\n\n" if handle else "")
        + texts.SUPPORT_FALLBACK
    )
    await _edit_or_send(event, text, menus.back_to("menu"))


@router.message(Command("profile"))
async def profile(message: Message, state: FSMContext) -> None:
    with bot_session() as db:
        user = resolve_user(db, message.from_user)
        roles = sorted(r.value for r in user.roles) or ["none yet"]
        text = (
            "<b>👤 Your account</b>\n\n"
            f"Name: {user.display_name}\n"
            # The numeric id is the permanent identity; a username is not (spec §2).
            f"Telegram ID: <code>{user.telegram_user_id}</code>\n"
            f"Roles: {', '.join(roles)}\n"
            f"Status: {user.status.value}\n"
            f"Joined: {user.created_at:%d %b %Y}"
        )
    await message.answer(text, reply_markup=menus.back_to("menu"))
