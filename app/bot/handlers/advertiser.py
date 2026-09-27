"""Advertiser flows: the campaign creation wizard (spec §3) and campaign control."""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from app.bot import texts
from app.bot.handlers.common import _edit_or_send
from app.bot.keyboards import menus
from app.bot.session import bot_session, resolve_user
from app.core.errors import AdNetError
from app.core.money import D, fmt, q
from app.db.base import utcnow
from app.models.enums import CampaignType, PricingModel
from app.services.audit import Actor
from app.services.campaigns import CampaignDraft, CampaignService
from app.services.payments import DepositService
from app.services.refunds import RefundService
from app.services.settings_service import SettingsService
from app.services.wallet import WalletService

router = Router(name="advertiser")


class NewCampaign(StatesGroup):
    """Mirrors the seven steps of spec §3."""

    name = State()
    creative = State()
    destination = State()
    cta = State()
    total_budget = State()
    daily_budget = State()
    pricing_model = State()
    bid = State()
    countries = State()
    categories = State()
    duration = State()
    confirm = State()


class Deposit(StatesGroup):
    amount = State()


def _parse_money(raw: str) -> Decimal:
    """Accept '5,000', '৳5000', '5000.50'. Reject anything else."""
    cleaned = (raw or "").strip().replace(",", "").replace("৳", "").replace(" ", "")
    if not cleaned:
        raise ValueError("empty")
    try:
        value = D(cleaned)
    except Exception as exc:
        raise ValueError("not a number") from exc
    if value <= 0:
        raise ValueError("must be positive")
    return q(value)


def _parse_list(raw: str) -> list[str]:
    if (raw or "").strip().lower() in {"any", "all", "-", "skip"}:
        return []
    return [part.strip() for part in raw.replace(";", ",").split(",") if part.strip()]


# --------------------------------------------------------------------------
# Wizard
# --------------------------------------------------------------------------


@router.callback_query(F.data == "campaign:new")
@router.message(Command("campaign"))
async def start_wizard(event, state: FSMContext) -> None:
    with bot_session() as db:
        user = resolve_user(db, event.from_user)
        if not user.is_advertiser:
            await _edit_or_send(event, texts.not_registered("an advertiser"), menus.back_to("menu"))
            return
        wallets = WalletService(db)
        view = wallets.view(wallets.for_advertiser(user.advertiser.id))
        currency = user.advertiser.currency
        minimum = SettingsService(db).money("min_campaign_budget")

    if view.available < minimum:
        await _edit_or_send(
            event,
            f"Your available balance is {fmt(view.available, currency)}, but the "
            f"minimum campaign budget is {fmt(minimum, currency)}.\n\n"
            "Add funds first.",
            menus.back_to("role:advertiser"),
        )
        return

    await state.set_state(NewCampaign.name)
    await _edit_or_send(
        event,
        "<b>Step 1 of 7 — name</b>\n\nWhat should this campaign be called?\n"
        "<i>Example: Exam Preparation 2026</i>",
        menus.cancel_only("campaign:cancel"),
    )


@router.message(NewCampaign.name)
async def wizard_name(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if len(name) < 2:
        await message.answer("That name is too short. Send at least 2 characters.")
        return
    await state.update_data(name=name[:200])
    await state.set_state(NewCampaign.creative)
    await message.answer(
        "<b>Step 2 of 7 — advertisement</b>\n\n"
        "Send the ad itself: text, or a photo/video with a caption.",
        reply_markup=menus.cancel_only("campaign:cancel"),
    )


@router.message(NewCampaign.creative, F.photo)
async def wizard_photo(message: Message, state: FSMContext) -> None:
    await state.update_data(
        media_file_id=message.photo[-1].file_id,
        campaign_type=CampaignType.IMAGE.value,
        body_text=(message.caption or "").strip() or None,
    )
    await _ask_destination(message, state)


@router.message(NewCampaign.creative, F.video)
async def wizard_video(message: Message, state: FSMContext) -> None:
    await state.update_data(
        media_file_id=message.video.file_id,
        campaign_type=CampaignType.VIDEO.value,
        body_text=(message.caption or "").strip() or None,
    )
    await _ask_destination(message, state)


@router.message(NewCampaign.creative, F.text)
async def wizard_text(message: Message, state: FSMContext) -> None:
    body = (message.text or "").strip()
    if len(body) < 10:
        await message.answer("That is very short for an ad. Send at least 10 characters.")
        return
    if len(body) > 3500:
        await message.answer("That is too long. Keep the ad under 3500 characters.")
        return
    await state.update_data(body_text=body, campaign_type=CampaignType.TEXT.value)
    await _ask_destination(message, state)


async def _ask_destination(message: Message, state: FSMContext) -> None:
    await state.set_state(NewCampaign.destination)
    await message.answer(
        "<b>Step 2b — destination link</b>\n\n"
        "Send the URL the button should open, or <code>skip</code> for no button.",
        reply_markup=menus.cancel_only("campaign:cancel"),
    )


@router.message(NewCampaign.destination)
async def wizard_destination(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    if raw.lower() in {"skip", "-", "none"}:
        await state.update_data(destination_url=None, cta_text=None)
        await _ask_total_budget(message, state)
        return
    try:
        CampaignService._validate_url(raw)
    except AdNetError as exc:
        await message.answer(f"{exc.message}\n\nSend a valid link, or <code>skip</code>.")
        return
    await state.update_data(destination_url=raw)
    await state.set_state(NewCampaign.cta)
    await message.answer(
        "<b>Step 2c — button text</b>\n\n"
        "What should the button say? <i>Example: Enrol now</i>\n"
        "Send <code>skip</code> to use “Learn more”.",
        reply_markup=menus.cancel_only("campaign:cancel"),
    )


@router.message(NewCampaign.cta)
async def wizard_cta(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    await state.update_data(cta_text=None if raw.lower() in {"skip", "-"} else raw[:64])
    await _ask_total_budget(message, state)


async def _ask_total_budget(message: Message, state: FSMContext) -> None:
    with bot_session() as db:
        settings = SettingsService(db)
        minimum = settings.money("min_campaign_budget")
        user = resolve_user(db, message.from_user)
        currency = user.advertiser.currency if user.advertiser else "BDT"
    await state.set_state(NewCampaign.total_budget)
    await message.answer(
        f"<b>Step 3 of 7 — total budget</b>\n\n"
        f"How much do you want to spend in total?\n"
        f"Minimum {fmt(minimum, currency)}.",
        reply_markup=menus.cancel_only("campaign:cancel"),
    )


@router.message(NewCampaign.total_budget)
async def wizard_total_budget(message: Message, state: FSMContext) -> None:
    try:
        total = _parse_money(message.text or "")
    except ValueError:
        await message.answer("Send a number, for example <code>10000</code>.")
        return
    with bot_session() as db:
        settings = SettingsService(db)
        minimum = settings.money("min_campaign_budget")
        user = resolve_user(db, message.from_user)
        currency = user.advertiser.currency if user.advertiser else "BDT"
        wallets = WalletService(db)
        available = wallets.view(wallets.for_advertiser(user.advertiser.id)).available
    if total < minimum:
        await message.answer(f"The minimum campaign budget is {fmt(minimum, currency)}.")
        return
    if total > available:
        await message.answer(
            f"Your available balance is {fmt(available, currency)}. "
            "Choose a smaller budget or add funds."
        )
        return
    await state.update_data(total_budget=str(total))
    await state.set_state(NewCampaign.daily_budget)
    await message.answer(
        "<b>Step 3b — daily budget</b>\n\n"
        "How much per day at most? This paces your spending so the budget is not "
        "consumed in the first hour.",
        reply_markup=menus.cancel_only("campaign:cancel"),
    )


@router.message(NewCampaign.daily_budget)
async def wizard_daily_budget(message: Message, state: FSMContext) -> None:
    try:
        daily = _parse_money(message.text or "")
    except ValueError:
        await message.answer("Send a number, for example <code>2000</code>.")
        return
    data = await state.get_data()
    total = D(data["total_budget"])
    if daily > total:
        await message.answer("The daily budget cannot exceed the total budget.")
        return
    with bot_session() as db:
        minimum = SettingsService(db).money("min_daily_budget")
    if daily < minimum:
        await message.answer(f"The minimum daily budget is {fmt(minimum, 'BDT')}.")
        return
    await state.update_data(daily_budget=str(daily))
    await state.set_state(NewCampaign.pricing_model)
    await message.answer("<b>Step 4 of 7 — pricing model</b>", reply_markup=menus.pricing_models())


@router.callback_query(NewCampaign.pricing_model, F.data.startswith("pmodel:"))
async def wizard_pricing_model(callback: CallbackQuery, state: FSMContext) -> None:
    model = callback.data.split(":", 1)[1]
    await state.update_data(pricing_model=model)
    with bot_session() as db:
        settings = SettingsService(db)
        base = settings.money("base_cpm")
        low = settings.money("min_cpm")
        high = settings.money("max_cpm")
    await state.set_state(NewCampaign.bid)
    await callback.message.edit_text(
        f"<b>Step 4b — your bid</b>\n\n"
        f"What will you pay per 1,000 impressions?\n"
        f"Typical: {fmt(base, 'BDT')} · allowed range "
        f"{fmt(low, 'BDT')}–{fmt(high, 'BDT')}",
        reply_markup=menus.cancel_only("campaign:cancel"),
    )
    await callback.answer()


@router.message(NewCampaign.bid)
async def wizard_bid(message: Message, state: FSMContext) -> None:
    try:
        bid = _parse_money(message.text or "")
    except ValueError:
        await message.answer("Send a number, for example <code>50</code>.")
        return
    with bot_session() as db:
        settings = SettingsService(db)
        low, high = settings.money("min_cpm"), settings.money("max_cpm")
    if not (low <= bid <= high):
        await message.answer(f"The bid must be between {fmt(low, 'BDT')} and {fmt(high, 'BDT')}.")
        return
    await state.update_data(bid_cpm=str(bid))
    await state.set_state(NewCampaign.countries)
    await message.answer(
        "<b>Step 5 of 7 — targeting</b>\n\n"
        "Which countries? Send 2-letter codes separated by commas, "
        "for example <code>BD, IN</code>.\n"
        "Send <code>any</code> for no country restriction.",
        reply_markup=menus.cancel_only("campaign:cancel"),
    )


@router.message(NewCampaign.countries)
async def wizard_countries(message: Message, state: FSMContext) -> None:
    codes = [c.upper() for c in _parse_list(message.text or "")]
    for code in codes:
        if len(code) != 2 or not code.isalpha():
            await message.answer(
                f"<code>{code}</code> is not a 2-letter country code. Example: <code>BD</code>."
            )
            return
    await state.update_data(countries=codes)
    await state.set_state(NewCampaign.categories)
    await message.answer(
        "<b>Step 5b — channel categories</b>\n\n"
        "Which categories should carry this ad? For example "
        "<code>education, technology</code>.\nSend <code>any</code> for all.",
        reply_markup=menus.cancel_only("campaign:cancel"),
    )


@router.message(NewCampaign.categories)
async def wizard_categories(message: Message, state: FSMContext) -> None:
    await state.update_data(categories=[c.lower() for c in _parse_list(message.text or "")])
    await state.set_state(NewCampaign.duration)
    await message.answer(
        "<b>Step 6 of 7 — schedule</b>\n\n"
        "How many days should it run? Send a number, for example <code>7</code>.",
        reply_markup=menus.cancel_only("campaign:cancel"),
    )


@router.message(NewCampaign.duration)
async def wizard_duration(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    if not raw.isdigit() or not (1 <= int(raw) <= 365):
        await message.answer("Send a whole number of days between 1 and 365.")
        return
    await state.update_data(duration_days=int(raw))

    data = await state.get_data()
    try:
        with bot_session() as db:
            user = resolve_user(db, message.from_user)
            service = CampaignService(db)
            campaign = service.create(user.advertiser, _draft_from(data))
            preview = service.preview(campaign)
            await state.update_data(campaign_id=str(campaign.id))
            text = texts.campaign_preview(preview)
    except AdNetError as exc:
        await message.answer(f"❌ {exc.message}\n\nStart again with /campaign.")
        await state.clear()
        return

    await state.set_state(NewCampaign.confirm)
    await message.answer(text, reply_markup=menus.confirm_campaign())


def _draft_from(data: dict) -> CampaignDraft:
    now = utcnow()
    return CampaignDraft(
        name=data["name"],
        campaign_type=CampaignType(data.get("campaign_type", "text")),
        pricing_model=PricingModel(data.get("pricing_model", "cpm")),
        total_budget=D(data["total_budget"]),
        daily_budget=D(data["daily_budget"]),
        bid_cpm=D(data["bid_cpm"]),
        starts_at=now,
        ends_at=now + timedelta(days=int(data.get("duration_days", 7))),
        body_text=data.get("body_text"),
        media_file_id=data.get("media_file_id"),
        destination_url=data.get("destination_url"),
        cta_text=data.get("cta_text"),
        countries=data.get("countries") or [],
        categories=data.get("categories") or [],
    )


@router.callback_query(NewCampaign.confirm, F.data == "campaign:confirm")
async def wizard_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    campaign_id = data.get("campaign_id")
    if not campaign_id:
        await callback.answer("This draft expired. Start again with /campaign.", show_alert=True)
        await state.clear()
        return
    try:
        with bot_session() as db:
            user = resolve_user(db, callback.from_user)
            service = CampaignService(db)
            campaign = service.get_owned(uuid.UUID(campaign_id), user.advertiser.id)
            service.submit(campaign, None)
            status = campaign.status.value
    except AdNetError as exc:
        await callback.answer(exc.message[:190], show_alert=True)
        return
    await state.clear()
    message = (
        "✅ <b>Campaign submitted for review.</b>\n\n"
        "Our moderators check the text, media, link and targeting. You will get a "
        "message as soon as it is approved."
        if status == "submitted"
        else "✅ <b>Campaign is live.</b>"
    )
    await callback.message.edit_text(message, reply_markup=menus.back_to("role:advertiser"))
    await callback.answer()


@router.callback_query(F.data == "campaign:cancel")
async def wizard_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text(
        "Cancelled. Nothing was charged.", reply_markup=menus.back_to("role:advertiser")
    )
    await callback.answer()


@router.callback_query(NewCampaign.confirm, F.data == "campaign:edit")
async def wizard_edit(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text(
        "The draft was discarded. Start a fresh one with /campaign.",
        reply_markup=menus.back_to("role:advertiser"),
    )
    await callback.answer()


# --------------------------------------------------------------------------
# Campaign management
# --------------------------------------------------------------------------


@router.callback_query(F.data == "campaigns")
@router.message(Command("campaigns"))
async def list_campaigns(event, state: FSMContext) -> None:
    await state.clear()
    with bot_session() as db:
        user = resolve_user(db, event.from_user)
        if not user.is_advertiser or not user.advertiser:
            await _edit_or_send(event, texts.not_registered("an advertiser"), menus.back_to("menu"))
            return
        campaigns = CampaignService(db).list_for_advertiser(user.advertiser.id, limit=15)
        currency = user.advertiser.currency
        if not campaigns:
            text = "You have no campaigns yet. Tap “Create Campaign” to start."
            rows = []
        else:
            lines = ["<b>🎯 Your campaigns</b>\n"]
            rows = []
            for c in campaigns:
                lines.append(
                    f"• <b>{c.name}</b> — {c.status.value}\n"
                    f"  spent {fmt(c.spent_amount, currency)} of "
                    f"{fmt(c.total_budget, currency)} · "
                    f"{c.billable_impressions:,} impressions"
                )
                rows.append([(f"{c.name[:28]} ({c.status.value})", f"campaign:view:{c.id}")])
            text = "\n".join(lines)
    keyboard = (
        menus._kb([*rows, [("⬅️ Back", "role:advertiser")]])
        if rows
        else menus.back_to("role:advertiser")
    )
    await _edit_or_send(event, text, keyboard)


@router.callback_query(F.data.startswith("campaign:view:"))
async def view_campaign(callback: CallbackQuery, state: FSMContext) -> None:
    campaign_id = callback.data.rsplit(":", 1)[1]
    try:
        with bot_session() as db:
            user = resolve_user(db, callback.from_user)
            service = CampaignService(db)
            campaign = service.get_owned(uuid.UUID(campaign_id), user.advertiser.id)
            text = texts.campaign_stats(service.stats(campaign))
            status = campaign.status.value
    except (AdNetError, ValueError) as exc:
        await callback.answer(getattr(exc, "message", "not found")[:190], show_alert=True)
        return
    await callback.message.edit_text(text, reply_markup=menus.campaign_actions(campaign_id, status))
    await callback.answer()


@router.callback_query(F.data.startswith("campaign:stats:"))
async def campaign_stats(callback: CallbackQuery, state: FSMContext) -> None:
    await view_campaign(callback, state)


async def _transition(callback: CallbackQuery, action: str) -> None:
    campaign_id = callback.data.rsplit(":", 1)[1]
    try:
        with bot_session() as db:
            user = resolve_user(db, callback.from_user)
            service = CampaignService(db)
            campaign = service.get_owned(uuid.UUID(campaign_id), user.advertiser.id)
            if action == "pause":
                service.pause(campaign)
                note = "⏸ Campaign paused."
            elif action == "resume":
                service.resume(campaign)
                note = "▶️ Campaign resumed."
            else:
                campaign, refund = RefundService(db).cancel_campaign(campaign, Actor.user(user))
                amount = fmt(refund.approved_amount, campaign.currency) if refund else "৳0"
                note = f"✖️ Campaign cancelled. {amount} returned to your wallet."
            status = campaign.status.value
    except AdNetError as exc:
        await callback.answer(exc.message[:190], show_alert=True)
        return
    await callback.message.edit_text(note, reply_markup=menus.campaign_actions(campaign_id, status))
    await callback.answer()


@router.callback_query(F.data.startswith("campaign:pause:"))
async def pause_campaign(callback: CallbackQuery, state: FSMContext) -> None:
    await _transition(callback, "pause")


@router.callback_query(F.data.startswith("campaign:resume:"))
async def resume_campaign(callback: CallbackQuery, state: FSMContext) -> None:
    await _transition(callback, "resume")


@router.callback_query(F.data.startswith("campaign:kill:"))
async def cancel_campaign(callback: CallbackQuery, state: FSMContext) -> None:
    await _transition(callback, "kill")


@router.callback_query(F.data.startswith("campaign:submit:"))
async def submit_campaign(callback: CallbackQuery, state: FSMContext) -> None:
    campaign_id = callback.data.rsplit(":", 1)[1]
    try:
        with bot_session() as db:
            user = resolve_user(db, callback.from_user)
            service = CampaignService(db)
            campaign = service.get_owned(uuid.UUID(campaign_id), user.advertiser.id)
            service.submit(campaign)
            status = campaign.status.value
    except AdNetError as exc:
        await callback.answer(exc.message[:190], show_alert=True)
        return
    await callback.message.edit_text(
        "📤 Submitted for review.", reply_markup=menus.campaign_actions(campaign_id, status)
    )
    await callback.answer()


# --------------------------------------------------------------------------
# Deposits
# --------------------------------------------------------------------------


@router.callback_query(F.data == "deposit")
@router.message(Command("deposit"))
async def start_deposit(event, state: FSMContext) -> None:
    with bot_session() as db:
        user = resolve_user(db, event.from_user)
        if not user.is_advertiser:
            await _edit_or_send(event, texts.not_registered("an advertiser"), menus.back_to("menu"))
            return
        currency = user.advertiser.currency
    await state.set_state(Deposit.amount)
    await _edit_or_send(
        event,
        f"<b>➕ Add funds</b>\n\nHow much would you like to add? Send an amount in {currency}.",
        menus.cancel_only("role:advertiser"),
    )


@router.message(Deposit.amount)
async def finish_deposit(message: Message, state: FSMContext) -> None:
    try:
        amount = _parse_money(message.text or "")
    except ValueError:
        await message.answer("Send a number, for example <code>5000</code>.")
        return
    with bot_session() as db:
        user = resolve_user(db, message.from_user)
        deposit, instructions = DepositService(db).initiate(user.advertiser.id, amount)
        currency = deposit.currency
        reference = str(deposit.id)
    await state.clear()
    await message.answer(
        "<b>Deposit created</b>\n\n"
        f"Amount: <b>{fmt(amount, currency)}</b>\n"
        f"Reference: <code>{reference}</code>\n\n"
        f"{instructions.get('instructions', '')}\n\n"
        "<i>Your balance updates once the payment is confirmed. Nothing is "
        "credited until then.</i>",
        reply_markup=menus.back_to("role:advertiser"),
    )
