"""Publisher flows: channel onboarding (spec §4), earnings and withdrawals (§13)."""

from __future__ import annotations

import uuid
from decimal import Decimal

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from app.bot import texts
from app.bot.handlers.advertiser import _parse_money
from app.bot.handlers.common import _edit_or_send
from app.bot.keyboards import menus
from app.bot.session import bot_session, resolve_user
from app.core.config import settings as app_settings
from app.core.errors import AdNetError
from app.core.money import fmt
from app.models.enums import PayoutMethod
from app.services.analytics import AnalyticsService
from app.services.audit import Actor
from app.services.channels import ChannelService
from app.services.earnings import EarningsService
from app.services.settings_service import SettingsService
from app.services.withdrawals import WithdrawalService

router = Router(name="publisher")


class AddChannel(StatesGroup):
    identifier = State()
    category = State()


class Withdraw(StatesGroup):
    amount = State()
    method = State()
    destination = State()


# --------------------------------------------------------------------------
# Channel onboarding (spec §4)
# --------------------------------------------------------------------------


@router.callback_query(F.data == "channel:add")
@router.message(Command("channel"))
async def start_add_channel(event, state: FSMContext) -> None:
    with bot_session() as db:
        user = resolve_user(db, event.from_user)
        if not user.is_publisher:
            await _edit_or_send(event, texts.not_registered("a publisher"), menus.back_to("menu"))
            return
        bot_username = app_settings.telegram_bot_username or "this_bot"
    await state.set_state(AddChannel.identifier)
    await _edit_or_send(
        event,
        texts.channel_verification_prompt(bot_username),
        menus.cancel_only("role:publisher"),
    )


@router.message(AddChannel.identifier)
async def receive_identifier(message: Message, state: FSMContext) -> None:
    identifier = (message.text or "").strip()
    if not identifier:
        await message.answer("Send the channel username, for example <code>@mychannel</code>.")
        return
    try:
        with bot_session() as db:
            user = resolve_user(db, message.from_user)
            result = ChannelService(db).register(user.publisher, identifier)
            ok = result.ok
            note = result.user_message()
            channel_id = str(result.channel.id) if result.channel else None
            title = (
                result.channel.chat.title if result.channel and result.channel.chat else identifier
            )
    except AdNetError as exc:
        await message.answer(f"❌ {exc.message}", reply_markup=menus.back_to("role:publisher"))
        await state.clear()
        return

    if not ok:
        # Keep the state so the publisher can fix the permission and retry.
        await message.answer(
            f"❌ {note}\n\nFix that and send the channel again.",
            reply_markup=menus.cancel_only("role:publisher"),
        )
        return

    await state.update_data(channel_id=channel_id)
    await state.set_state(AddChannel.category)
    await message.answer(
        f"✅ Verified <b>{title}</b>.\n\n"
        "What category is it? For example <code>education</code>, "
        "<code>technology</code>, <code>news</code>.\n"
        "This decides which ads you are offered.",
        reply_markup=menus.cancel_only("role:publisher"),
    )


@router.message(AddChannel.category)
async def receive_category(message: Message, state: FSMContext) -> None:
    category = (message.text or "").strip().lower()[:48]
    data = await state.get_data()
    channel_id = data.get("channel_id")
    if not channel_id:
        await state.clear()
        await message.answer("That session expired. Start again with /channel.")
        return
    with bot_session() as db:
        from app.models.telegram import PublisherChannel

        user = resolve_user(db, message.from_user)
        channel = db.get(PublisherChannel, uuid.UUID(channel_id))
        if channel is None or channel.publisher_id != user.publisher.id:
            await state.clear()
            await message.answer("Channel not found.")
            return
        channel.category = category or None
        db.flush()
        status = channel.status.value
    await state.clear()
    await message.answer(
        f"✅ Saved. Your channel is <b>{status}</b> and will start receiving "
        "matching ads automatically.\n\n"
        "<i>You are paid per measured impression. Earnings are pending while we "
        "validate traffic, then become withdrawable.</i>",
        reply_markup=menus.publisher_menu(),
    )


@router.callback_query(F.data == "channels")
async def list_channels(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    with bot_session() as db:
        user = resolve_user(db, callback.from_user)
        if not user.is_publisher:
            await _edit_or_send(
                callback, texts.not_registered("a publisher"), menus.back_to("menu")
            )
            return
        channels = ChannelService(db).list_for_publisher(user.publisher.id)
        currency = user.publisher.currency
        if not channels:
            text = "You have no channels yet. Tap “Add Channel” to register one."
            rows = []
        else:
            lines = ["<b>📋 Your channels</b>\n"]
            rows = []
            for c in channels:
                title = c.chat.title if c.chat else str(c.telegram_chat_id)
                lines.append(
                    f"• <b>{title}</b> — {c.status.value}\n"
                    f"  {c.avg_views:,} avg views · {c.total_impressions:,} impressions · "
                    f"earned {fmt(c.lifetime_earned, currency)}"
                )
                rows.append([(f"{title[:28]} ({c.status.value})", f"channel:view:{c.id}")])
            text = "\n".join(lines)
    keyboard = (
        menus._kb([*rows, [("⬅️ Back", "role:publisher")]])
        if rows
        else menus.back_to("role:publisher")
    )
    await _edit_or_send(callback, text, keyboard)


@router.callback_query(F.data.startswith("channel:view:"))
async def view_channel(callback: CallbackQuery, state: FSMContext) -> None:
    channel_id = callback.data.rsplit(":", 1)[1]
    with bot_session() as db:
        from app.models.telegram import PublisherChannel

        user = resolve_user(db, callback.from_user)
        channel = db.get(PublisherChannel, uuid.UUID(channel_id))
        if channel is None or channel.publisher_id != user.publisher.id:
            await callback.answer("Channel not found.", show_alert=True)
            return
        currency = user.publisher.currency
        title = channel.chat.title if channel.chat else str(channel.telegram_chat_id)
        members = channel.chat.member_count if channel.chat else 0
        text = (
            f"<b>{title}</b>\n\n"
            f"Status: {channel.status.value}\n"
            f"Verification: {channel.verification_status.value}\n"
            f"Members: {members:,}\n"
            f"Average views: {channel.avg_views:,}\n"
            f"Average ad reach: {channel.avg_ad_views:,}\n"
            f"Ads served: {channel.total_ads_served:,}\n"
            f"Impressions: {channel.total_impressions:,}\n"
            f"Clicks: {channel.total_clicks:,}\n"
            f"Earned: <b>{fmt(channel.lifetime_earned, currency)}</b>\n"
            f"Ads enabled: {'yes' if channel.auto_advertising else 'no'}\n\n"
            "<i>Payment is based on measured ad reach, not member count.</i>"
        )
        auto_on = channel.auto_advertising
    await callback.message.edit_text(text, reply_markup=menus.channel_actions(channel_id, auto_on))
    await callback.answer()


@router.callback_query(F.data.startswith("channel:stats:"))
async def channel_stats(callback: CallbackQuery, state: FSMContext) -> None:
    await view_channel(callback, state)


@router.callback_query(F.data.regexp(r"^channel:(on|off):"))
async def toggle_channel(callback: CallbackQuery, state: FSMContext) -> None:
    _, action, channel_id = callback.data.split(":", 2)
    with bot_session() as db:
        from app.models.telegram import PublisherChannel

        user = resolve_user(db, callback.from_user)
        channel = db.get(PublisherChannel, uuid.UUID(channel_id))
        if channel is None or channel.publisher_id != user.publisher.id:
            await callback.answer("Channel not found.", show_alert=True)
            return
        channel.auto_advertising = action == "on"
        db.flush()
        auto_on = channel.auto_advertising
    await callback.answer("Ads enabled." if auto_on else "Ads paused for this channel.")
    await view_channel(callback, state)


@router.callback_query(F.data.startswith("channel:recheck:"))
async def recheck_channel(callback: CallbackQuery, state: FSMContext) -> None:
    channel_id = callback.data.rsplit(":", 1)[1]
    with bot_session() as db:
        from app.models.telegram import PublisherChannel

        user = resolve_user(db, callback.from_user)
        channel = db.get(PublisherChannel, uuid.UUID(channel_id))
        if channel is None or channel.publisher_id != user.publisher.id:
            await callback.answer("Channel not found.", show_alert=True)
            return
        result = ChannelService(db).revalidate(channel)
        note = result.user_message()
    await callback.answer(note[:190], show_alert=True)
    await view_channel(callback, state)


# --------------------------------------------------------------------------
# Earnings
# --------------------------------------------------------------------------


@router.callback_query(F.data == "earnings")
@router.message(Command("earnings"))
async def earnings(event, state: FSMContext) -> None:
    await state.clear()
    with bot_session() as db:
        user = resolve_user(db, event.from_user)
        if not user.is_publisher or not user.publisher:
            await _edit_or_send(event, texts.not_registered("a publisher"), menus.back_to("menu"))
            return
        summary = EarningsService(db).summary(user.publisher.id)
        overview = AnalyticsService(db).publisher_overview(user.publisher.id)
        currency = user.publisher.currency
        minimum = SettingsService(db).money("min_withdrawal")
        hours = SettingsService(db).int_("earnings_validation_hours")
    text = (
        "<b>📈 Earnings</b>\n\n"
        f"Pending validation: {fmt(summary['pending'], currency)}\n"
        f"Confirmed (withdrawable): <b>{fmt(summary['confirmed'], currency)}</b>\n"
        f"Paid out: {fmt(summary['paid'], currency)}\n"
        f"Total earned: {fmt(summary['total_earned'], currency)}\n\n"
        f"Billable impressions: {summary['billable_impressions']:,}\n"
        f"Your effective CPM: {fmt(summary['effective_cpm'], currency)}\n"
        f"Clicks: {overview['clicks']:,} (CTR {overview['ctr_percent']}%)\n\n"
        f"<i>Earnings are held for {hours} hours of traffic validation before "
        f"becoming withdrawable. Minimum withdrawal is {fmt(minimum, currency)}.</i>"
    )
    await _edit_or_send(event, text, menus.publisher_menu())


# --------------------------------------------------------------------------
# Withdrawals (spec §13)
# --------------------------------------------------------------------------


@router.callback_query(F.data == "withdraw")
@router.message(Command("withdraw"))
async def start_withdraw(event, state: FSMContext) -> None:
    with bot_session() as db:
        user = resolve_user(db, event.from_user)
        if not user.is_publisher or not user.publisher:
            await _edit_or_send(event, texts.not_registered("a publisher"), menus.back_to("menu"))
            return
        summary = EarningsService(db).summary(user.publisher.id)
        currency = user.publisher.currency
        settings = SettingsService(db)
        minimum = settings.money("min_withdrawal")
        confirmed = summary["confirmed"]

    if confirmed < minimum:
        await _edit_or_send(
            event,
            f"<b>🏧 Withdraw</b>\n\n"
            f"Confirmed balance: {fmt(confirmed, currency)}\n"
            f"Minimum withdrawal: {fmt(minimum, currency)}\n\n"
            f"Pending earnings ({fmt(summary['pending'], currency)}) are not yet "
            "withdrawable — they become available once traffic validation finishes.",
            menus.back_to("role:publisher"),
        )
        return

    await state.set_state(Withdraw.amount)
    await _edit_or_send(
        event,
        f"<b>🏧 Withdraw</b>\n\n"
        f"Withdrawable now: <b>{fmt(confirmed, currency)}</b>\n\n"
        f"{texts.ask_amount(minimum, currency)}",
        menus.cancel_only("role:publisher"),
    )


@router.message(Withdraw.amount)
async def withdraw_amount(message: Message, state: FSMContext) -> None:
    try:
        amount = _parse_money(message.text or "")
    except ValueError:
        await message.answer("Send a number, for example <code>1000</code>.")
        return
    with bot_session() as db:
        user = resolve_user(db, message.from_user)
        service = WithdrawalService(db)
        summary = EarningsService(db).summary(user.publisher.id)
        currency = user.publisher.currency
        if amount > summary["confirmed"]:
            await message.answer(
                f"You can withdraw up to {fmt(summary['confirmed'], currency)} right now."
            )
            return
        try:
            breakdown = service.quote_fee(amount)
        except AdNetError as exc:
            await message.answer(f"❌ {exc.message}")
            return
        methods = service.list_payout_methods(user.publisher.id)
        saved = [
            (f"{m.method.value} · {m.destination_masked}", f"withdraw:use:{m.id}") for m in methods
        ]

    await state.update_data(amount=str(amount))
    text = (
        f"Amount: <b>{fmt(breakdown.amount, currency)}</b>\n"
        f"Fee: {fmt(breakdown.fee, currency)}\n"
        f"You will receive: <b>{fmt(breakdown.net, currency)}</b>\n\n"
        "Choose a payout method:"
    )
    if saved:
        keyboard = menus._kb(
            [[row] for row in saved]
            + [[("➕ New method", "withdraw:new")], [("✖️ Cancel", "role:publisher")]]
        )
    else:
        await state.set_state(Withdraw.method)
        keyboard = menus.payout_methods()
    await message.answer(text, reply_markup=keyboard)


@router.callback_query(F.data == "withdraw:new")
async def withdraw_new_method(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Withdraw.method)
    await callback.message.edit_text("Choose a payout method:", reply_markup=menus.payout_methods())
    await callback.answer()


@router.callback_query(Withdraw.method, F.data.startswith("payout:"))
async def withdraw_pick_method(callback: CallbackQuery, state: FSMContext) -> None:
    method = callback.data.split(":", 1)[1]
    await state.update_data(method=method)
    await state.set_state(Withdraw.destination)
    hint = (
        "Send your 11-digit number starting 01, for example <code>01712345678</code>."
        if method in {"bkash", "nagad", "rocket"}
        else "Send your account number."
    )
    await callback.message.edit_text(
        f"<b>{method}</b>\n\n{hint}\n\n<i>Stored encrypted and shown only in masked form.</i>",
        reply_markup=menus.cancel_only("role:publisher"),
    )
    await callback.answer()


@router.message(Withdraw.destination)
async def withdraw_destination(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    destination = (message.text or "").strip()
    try:
        with bot_session() as db:
            user = resolve_user(db, message.from_user)
            service = WithdrawalService(db)
            record = service.add_payout_method(
                user.publisher.id, PayoutMethod(data["method"]), destination
            )
            withdrawal = service.request(
                user.publisher.id,
                Decimal(data["amount"]),
                record.id,
                actor=Actor.user(user),
            )
            text = _withdrawal_receipt(withdrawal)
    except AdNetError as exc:
        await message.answer(f"❌ {exc.message}\n\nSend a valid number, or /withdraw again.")
        return
    await state.clear()
    await message.answer(text, reply_markup=menus.back_to("role:publisher"))


@router.callback_query(F.data.startswith("withdraw:use:"))
async def withdraw_use_saved(callback: CallbackQuery, state: FSMContext) -> None:
    method_id = callback.data.rsplit(":", 1)[1]
    data = await state.get_data()
    amount = data.get("amount")
    if not amount:
        await callback.answer("That session expired. Start again with /withdraw.", show_alert=True)
        await state.clear()
        return
    try:
        with bot_session() as db:
            user = resolve_user(db, callback.from_user)
            withdrawal = WithdrawalService(db).request(
                user.publisher.id,
                Decimal(amount),
                uuid.UUID(method_id),
                actor=Actor.user(user),
            )
            text = _withdrawal_receipt(withdrawal)
    except AdNetError as exc:
        await callback.answer(exc.message[:190], show_alert=True)
        return
    await state.clear()
    await callback.message.edit_text(text, reply_markup=menus.back_to("role:publisher"))
    await callback.answer()


def _withdrawal_receipt(withdrawal) -> str:
    return (
        "✅ <b>Withdrawal requested</b>\n\n"
        f"Amount: {fmt(withdrawal.amount, withdrawal.currency)}\n"
        f"Fee: {fmt(withdrawal.fee, withdrawal.currency)}\n"
        f"You will receive: <b>{fmt(withdrawal.net_amount, withdrawal.currency)}</b>\n"
        f"Method: {withdrawal.method.value} · {withdrawal.destination_masked}\n"
        f"Status: {withdrawal.status.value}\n"
        f"Reference: <code>{withdrawal.id}</code>\n\n"
        "<i>You will be notified when it is paid.</i>"
    )


@router.callback_query(F.data == "publisher")
@router.message(Command("publisher"))
async def publisher_menu(event, state: FSMContext) -> None:
    from app.bot.handlers.common import become_publisher

    if isinstance(event, CallbackQuery):
        await become_publisher(event, state)
    else:
        await state.clear()
        with bot_session() as db:
            resolve_user(db, event.from_user)
        await event.answer("<b>📢 Publisher</b>", reply_markup=menus.publisher_menu())
