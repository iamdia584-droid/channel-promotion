"""Inline keyboards. The bot is button-driven, not command-driven (spec §36)."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


def _kb(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for row in rows:
        builder.row(*(InlineKeyboardButton(text=t, callback_data=d) for t, d in row))
    return builder.as_markup()


def main_menu(
    is_advertiser: bool = False, is_publisher: bool = False, is_staff: bool = False
) -> InlineKeyboardMarkup:
    rows = [[("📣 Advertiser", "role:advertiser"), ("📢 Publisher", "role:publisher")]]
    if is_advertiser or is_publisher:
        rows.append([("💰 Wallet", "wallet"), ("📊 Statistics", "stats")])
    if is_advertiser:
        rows.append([("🎯 Campaigns", "campaigns"), ("➕ Add Funds", "deposit")])
    if is_publisher:
        rows.append([("📈 Earnings", "earnings"), ("🏧 Withdraw", "withdraw")])
    if is_advertiser or is_publisher:
        rows.append([("🧾 Transactions", "transactions")])
    if is_staff:
        rows.append([("🛠 Admin", "admin")])
    rows.append([("❓ Help", "help"), ("💬 Support", "support")])
    return _kb(rows)


def advertiser_menu() -> InlineKeyboardMarkup:
    return _kb(
        [
            [("➕ Create Campaign", "campaign:new")],
            [("🎯 My Campaigns", "campaigns"), ("📊 Statistics", "stats:advertiser")],
            [("💰 Wallet", "wallet"), ("➕ Add Funds", "deposit")],
            [("🧾 Transactions", "transactions")],
            [("⬅️ Back", "menu")],
        ]
    )


def publisher_menu() -> InlineKeyboardMarkup:
    return _kb(
        [
            [("➕ Add Channel", "channel:add")],
            [("📋 My Channels", "channels"), ("📈 Earnings", "earnings")],
            [("🏧 Withdraw", "withdraw"), ("📊 Statistics", "stats:publisher")],
            [("🧾 Transactions", "transactions")],
            [("⬅️ Back", "menu")],
        ]
    )


def admin_menu() -> InlineKeyboardMarkup:
    return _kb(
        [
            [("📋 Review Queue", "admin:queue"), ("💵 Revenue", "admin:revenue")],
            [("🏧 Withdrawals", "admin:withdrawals"), ("🚨 Fraud", "admin:fraud")],
            [("👥 Users", "admin:users"), ("⚙️ Settings", "admin:settings")],
            [("⬅️ Back", "menu")],
        ]
    )


def campaign_types() -> InlineKeyboardMarkup:
    return _kb(
        [
            [("📝 Text", "ctype:text")],
            [("🖼 Image", "ctype:image"), ("🎬 Video", "ctype:video")],
            [("🔗 Button / Link", "ctype:button_link")],
            [("📰 Channel Post", "ctype:channel_post")],
            [("✖️ Cancel", "campaign:cancel")],
        ]
    )


def pricing_models() -> InlineKeyboardMarkup:
    return _kb(
        [
            [("CPM — per 1,000 impressions", "pmodel:cpm")],
            [("CPV — per view", "pmodel:cpv")],
            [("✖️ Cancel", "campaign:cancel")],
        ]
    )


def confirm_campaign() -> InlineKeyboardMarkup:
    return _kb(
        [
            [("✅ Confirm Campaign", "campaign:confirm")],
            [("✏️ Edit", "campaign:edit"), ("✖️ Cancel", "campaign:cancel")],
        ]
    )


def campaign_actions(campaign_id: str, status: str) -> InlineKeyboardMarkup:
    rows: list[list[tuple[str, str]]] = [[("📊 Statistics", f"campaign:stats:{campaign_id}")]]
    if status == "draft":
        rows.append([("📤 Submit for review", f"campaign:submit:{campaign_id}")])
    if status == "running":
        rows.append([("⏸ Pause", f"campaign:pause:{campaign_id}")])
    if status == "paused":
        rows.append([("▶️ Resume", f"campaign:resume:{campaign_id}")])
    if status in {"draft", "paused", "running", "approved"}:
        rows.append([("✖️ Cancel & refund", f"campaign:kill:{campaign_id}")])
    rows.append([("⬅️ Back", "campaigns")])
    return _kb(rows)


def channel_actions(channel_id: str, auto_on: bool) -> InlineKeyboardMarkup:
    toggle = (
        ("⏸ Pause ads", f"channel:off:{channel_id}")
        if auto_on
        else ("▶️ Resume ads", f"channel:on:{channel_id}")
    )
    return _kb(
        [
            [("📊 Statistics", f"channel:stats:{channel_id}")],
            [toggle],
            [("🔄 Re-check permissions", f"channel:recheck:{channel_id}")],
            [("⬅️ Back", "channels")],
        ]
    )


def payout_methods() -> InlineKeyboardMarkup:
    return _kb(
        [
            [("bKash", "payout:bkash"), ("Nagad", "payout:nagad")],
            [("Rocket", "payout:rocket"), ("Bank", "payout:bank")],
            [("✖️ Cancel", "menu")],
        ]
    )


def back_to(target: str = "menu", label: str = "⬅️ Back") -> InlineKeyboardMarkup:
    return _kb([[(label, target)]])


def cancel_only(target: str = "menu") -> InlineKeyboardMarkup:
    return _kb([[("✖️ Cancel", target)]])
