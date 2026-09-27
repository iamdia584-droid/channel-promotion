"""Admin bot commands (spec §22).

These are read-and-triage only. Anything that moves money — paying a withdrawal,
adjusting a balance — is deliberately available on the web dashboard alone, where
the actor is authenticated with a password and TOTP rather than a Telegram id that
could be hijacked.
"""

from __future__ import annotations

import uuid

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select

from app.bot.handlers.common import _edit_or_send
from app.bot.keyboards import menus
from app.bot.session import bot_session, resolve_user
from app.core.money import fmt
from app.models.enums import FraudBand
from app.models.identity import StaffUser
from app.models.ops import FraudEvent
from app.services.analytics import AnalyticsService
from app.services.audit import Actor
from app.services.campaigns import CampaignService
from app.services.settings_service import SettingsService
from app.services.withdrawals import WithdrawalService

router = Router(name="admin")


def _staff_for(db, telegram_user_id: int) -> StaffUser | None:
    """A Telegram id only grants staff access if it is linked to a staff record."""
    return db.scalars(
        select(StaffUser).where(
            StaffUser.telegram_user_id == telegram_user_id,
            StaffUser.is_active.is_(True),
        )
    ).one_or_none()


async def _require_staff(event) -> bool:
    with bot_session() as db:
        user = resolve_user(db, event.from_user)
        staff = _staff_for(db, user.telegram_user_id)
        allowed = staff is not None or user.is_admin or user.is_moderator
    if not allowed:
        await _edit_or_send(event, "That area is for platform staff.", menus.back_to("menu"))
    return allowed


@router.callback_query(F.data == "admin")
@router.message(Command("admin"))
async def admin_home(event, state: FSMContext) -> None:
    await state.clear()
    if not await _require_staff(event):
        return
    with bot_session() as db:
        overview = AnalyticsService(db).platform_overview()
        currency = overview["currency"]
    text = (
        "<b>🛠 Admin</b>\n\n"
        f"Ad spend: {fmt(overview['gross_ad_spend'], currency)}\n"
        f"Platform revenue: <b>{fmt(overview['platform_revenue'], currency)}</b>\n"
        f"Publisher revenue: {fmt(overview['publisher_revenue'], currency)}\n\n"
        f"Active campaigns: {overview['active_campaigns']}\n"
        f"Awaiting review: {overview['campaigns_awaiting_review']}\n"
        f"Active channels: {overview['active_channels']}\n"
        f"Pending withdrawals: {overview['pending_withdrawals_count']} "
        f"({fmt(overview['pending_withdrawals_amount'], currency)})\n\n"
        f"Impressions today: {overview['impressions_today']:,}\n"
        f"Clicks today: {overview['clicks_today']:,}"
    )
    difference = overview["trial_balance"]["difference"]
    if difference != 0:
        # A non-zero trial balance is a bug, and must be impossible to miss.
        text += f"\n\n⚠️ <b>Ledger imbalance: {difference}</b>"
    await _edit_or_send(event, text, menus.admin_menu())


@router.callback_query(F.data == "admin:queue")
@router.message(Command("campaigns_queue"))
async def review_queue(event, state: FSMContext) -> None:
    if not await _require_staff(event):
        return
    with bot_session() as db:
        campaigns = CampaignService(db).moderation_queue(limit=10)
        if not campaigns:
            text = "✅ Nothing awaiting review."
            rows = []
        else:
            lines = ["<b>📋 Awaiting review</b>\n"]
            rows = []
            for c in campaigns:
                ad = c.ads[0] if c.ads else None
                lines.append(
                    f"• <b>{c.name}</b>\n"
                    f"  budget {fmt(c.total_budget, c.currency)} · "
                    f"CPM {fmt(c.bid_cpm, c.currency)}\n"
                    f"  {(ad.body_text or '')[:90] if ad else '(no creative)'}"
                )
                rows.append([(f"Review: {c.name[:26]}", f"admin:review:{c.id}")])
            text = "\n".join(lines)
    keyboard = menus._kb([*rows, [("⬅️ Back", "admin")]]) if rows else menus.back_to("admin")
    await _edit_or_send(event, text, keyboard)


@router.callback_query(F.data.startswith("admin:review:"))
async def review_campaign(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _require_staff(callback):
        return
    campaign_id = callback.data.rsplit(":", 1)[1]
    with bot_session() as db:
        from app.models.campaigns import Campaign

        campaign = db.get(Campaign, uuid.UUID(campaign_id))
        if campaign is None:
            await callback.answer("Campaign not found.", show_alert=True)
            return
        ad = campaign.ads[0] if campaign.ads else None
        target = campaign.target
        text = (
            f"<b>{campaign.name}</b>\n\n"
            f"Budget: {fmt(campaign.total_budget, campaign.currency)} "
            f"(daily {fmt(campaign.daily_budget, campaign.currency)})\n"
            f"CPM bid: {fmt(campaign.bid_cpm, campaign.currency)}\n"
            f"Countries: {', '.join(target.countries) if target and target.countries else 'any'}\n"
            f"Categories: "
            f"{', '.join(target.categories) if target and target.categories else 'any'}\n\n"
            f"<b>Creative</b>\n{(ad.body_text or '(media only)') if ad else '-'}\n\n"
            f"<b>Link</b>\n<code>{ad.destination_url if ad else '-'}</code>\n\n"
            "Check the text, media, link, landing page, category and targeting."
        )
    await callback.message.edit_text(
        text,
        reply_markup=menus._kb(
            [
                [("✅ Approve", f"admin:approve:{campaign_id}")],
                [("❌ Reject", f"admin:reject:{campaign_id}")],
                [("⬅️ Back", "admin:queue")],
            ]
        ),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin:approve:"))
async def approve(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _require_staff(callback):
        return
    campaign_id = callback.data.rsplit(":", 1)[1]
    with bot_session() as db:
        from app.core.errors import AdNetError
        from app.models.campaigns import Campaign

        user = resolve_user(db, callback.from_user)
        staff = _staff_for(db, user.telegram_user_id)
        campaign = db.get(Campaign, uuid.UUID(campaign_id))
        if campaign is None:
            await callback.answer("Campaign not found.", show_alert=True)
            return
        actor = Actor.staff(staff) if staff else Actor.user(user)
        try:
            CampaignService(db).approve(campaign, actor, note="approved from bot")
            note = f"✅ Approved: {campaign.name}"
        except AdNetError as exc:
            note = f"❌ {exc.message}"
    await callback.answer(note[:190], show_alert=True)
    await review_queue(callback, state)


@router.callback_query(F.data.startswith("admin:reject:"))
async def reject(callback: CallbackQuery, state: FSMContext) -> None:
    """Rejection needs a written reason, so it happens on the dashboard."""
    if not await _require_staff(callback):
        return
    await callback.answer(
        "Rejections need a written reason — use the web dashboard so the "
        "advertiser gets a useful explanation.",
        show_alert=True,
    )


@router.callback_query(F.data == "admin:revenue")
@router.message(Command("revenue"))
async def revenue(event, state: FSMContext) -> None:
    if not await _require_staff(event):
        return
    with bot_session() as db:
        overview = AnalyticsService(db).platform_overview()
        currency = overview["currency"]
        snapshots = AnalyticsService(db).snapshots(7)
    lines = [
        "<b>💵 Revenue</b>\n",
        f"Gross ad spend: {fmt(overview['gross_ad_spend'], currency)}",
        f"Publisher revenue: {fmt(overview['publisher_revenue'], currency)}",
        f"Platform revenue: <b>{fmt(overview['platform_revenue'], currency)}</b>",
        f"Withdrawal fees: {fmt(overview['platform_fees'], currency)}",
        f"Deposits: {fmt(overview['advertiser_deposits'], currency)}",
        f"Refunds: {fmt(overview['refunds'], currency)}",
        f"Paid out: {fmt(overview['withdrawals_paid'], currency)}",
        f"Reversed for fraud: {fmt(overview['fraud_reversed'], currency)}",
    ]
    if snapshots:
        lines.append("\n<b>Last 7 days</b>")
        for s in snapshots:
            lines.append(
                f"{s.snapshot_date}: spend {fmt(s.gross_ad_spend, currency)} · "
                f"margin {fmt(s.platform_revenue, currency)}"
            )
    await _edit_or_send(event, "\n".join(lines), menus.back_to("admin"))


@router.callback_query(F.data == "admin:withdrawals")
@router.message(Command("withdrawals"))
async def withdrawals(event, state: FSMContext) -> None:
    if not await _require_staff(event):
        return
    with bot_session() as db:
        queue = WithdrawalService(db).pending_queue(limit=15)
        if not queue:
            text = "✅ No pending withdrawals."
        else:
            lines = ["<b>🏧 Pending withdrawals</b>\n"]
            for w in queue:
                flag = " 🚩" if w.fraud_hold else ""
                lines.append(
                    f"• {fmt(w.net_amount, w.currency)} via {w.method.value} "
                    f"({w.destination_masked}){flag}\n"
                    f"  fraud score {w.fraud_score} · {w.status.value} · "
                    f"<code>{str(w.id)[:8]}</code>"
                )
            lines.append(
                "\n<i>Payouts are approved on the web dashboard, which requires "
                "password and 2FA.</i>"
            )
            text = "\n".join(lines)
    await _edit_or_send(event, text, menus.back_to("admin"))


@router.callback_query(F.data == "admin:fraud")
@router.message(Command("fraud"))
async def fraud(event, state: FSMContext) -> None:
    if not await _require_staff(event):
        return
    with bot_session() as db:
        rows = db.scalars(
            select(FraudEvent)
            .where(FraudEvent.band != FraudBand.NORMAL)
            .order_by(FraudEvent.occurred_at.desc())
            .limit(12)
        ).all()
        if not rows:
            text = "✅ No fraud events above the review threshold."
        else:
            lines = ["<b>🚨 Fraud events</b>\n"]
            for e in rows:
                lines.append(
                    f"• [{e.band.value}] {e.score} — {e.signal}\n"
                    f"  {e.subject_type.value} "
                    f"<code>{str(e.subject_id)[:8] if e.subject_id else '-'}</code>"
                    f" · at risk {fmt(e.amount_at_risk, 'BDT')}"
                    f" · at risk {fmt(e.amount_at_risk, 'BDT')}"
                )
            lines.append("\n<i>Full evidence is on the web dashboard.</i>")
            text = "\n".join(lines)
    await _edit_or_send(event, text, menus.back_to("admin"))


@router.callback_query(F.data == "admin:users")
@router.message(Command("users"))
async def users(event, state: FSMContext) -> None:
    if not await _require_staff(event):
        return
    with bot_session() as db:
        overview = AnalyticsService(db).platform_overview()
    text = (
        "<b>👥 Users</b>\n\n"
        f"Advertisers: {overview['advertisers_total']}\n"
        f"Publishers: {overview['publishers_total']}\n"
        f"Active publishers: {overview['active_publishers']}\n"
        f"Active channels: {overview['active_channels']}\n"
        f"Channels awaiting review: {overview['channels_awaiting_review']}"
    )
    await _edit_or_send(event, text, menus.back_to("admin"))


@router.callback_query(F.data == "admin:settings")
@router.message(Command("settings"))
async def settings_view(event, state: FSMContext) -> None:
    if not await _require_staff(event):
        return
    with bot_session() as db:
        service = SettingsService(db)
        keys = [
            "platform_commission_rate",
            "base_cpm",
            "min_cpm",
            "max_cpm",
            "min_withdrawal",
            "withdrawal_fee_flat",
            "earnings_validation_hours",
            "selection_mode",
            "impression_cap_multiplier",
            "fraud_block_threshold",
        ]
        lines = ["<b>⚙️ Key settings</b>\n"]
        for key in keys:
            lines.append(f"<code>{key}</code> = {service.raw(key)}")
        lines.append("\n<i>Settings are changed on the web dashboard, with an audit log.</i>")
    await _edit_or_send(event, "\n".join(lines), menus.back_to("admin"))


@router.message(Command("publishers"))
async def publishers_command(message: Message, state: FSMContext) -> None:
    await users(message, state)
