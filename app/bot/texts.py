"""User-facing bot copy. Kept together so wording stays consistent."""

from __future__ import annotations

from decimal import Decimal

from app.core.money import fmt


def welcome(platform: str, name: str) -> str:
    return (
        f"👋 Welcome to <b>{platform}</b>, {name}.\n\n"
        "This is an independent Telegram advertising network.\n\n"
        "• <b>Advertiser</b> — run ads across verified Telegram channels\n"
        "• <b>Publisher</b> — earn from your own channel or group\n\n"
        "You can be both. Choose where to start:"
    )


def help_text(platform: str, support: str | None) -> str:
    lines = [
        f"<b>{platform} — help</b>\n",
        "<b>Commands</b>",
        "/start — main menu",
        "/wallet — balance",
        "/campaign — create a campaign",
        "/campaigns — your campaigns",
        "/stats — statistics",
        "/publisher — publisher menu",
        "/channel — add a channel",
        "/earnings — your earnings",
        "/withdraw — request a payout",
        "/transactions — transaction history",
        "/profile — your account",
        "/support — contact support",
        "",
        "<b>How advertising is measured</b>",
        "You are charged only for impressions we can independently measure, "
        "never for an estimate. Estimated reach shown before launch is a "
        "projection, not a guarantee.",
        "",
        "<b>How earnings work</b>",
        "Earnings are <i>pending</i> while we validate the traffic, then become "
        "<i>confirmed</i> and withdrawable.",
    ]
    if support:
        lines += ["", f"Questions: @{support.lstrip('@')}"]
    return "\n".join(lines)


def wallet_advertiser(view, currency: str) -> str:
    return (
        "<b>💰 Advertiser wallet</b>\n\n"
        f"Available: <b>{fmt(view.available, currency)}</b>\n"
        f"Reserved in campaigns: {fmt(view.reserved, currency)}\n"
        f"Spent to date: {fmt(view.spent, currency)}\n"
        f"Deposited to date: {fmt(view.deposited, currency)}\n"
        f"Refunded: {fmt(view.refunded, currency)}"
    )


def wallet_publisher(view, currency: str) -> str:
    return (
        "<b>📈 Publisher earnings</b>\n\n"
        f"Pending validation: {fmt(view.pending, currency)}\n"
        f"Confirmed (withdrawable): <b>{fmt(view.confirmed, currency)}</b>\n"
        f"Earned to date: {fmt(view.earned, currency)}\n"
        f"Withdrawn to date: {fmt(view.withdrawn, currency)}\n\n"
        "<i>Pending earnings become confirmed once traffic validation completes.</i>"
    )


def campaign_preview(preview: dict) -> str:
    currency = preview["currency"]
    return (
        "<b>Campaign preview</b>\n\n"
        f"Name: <b>{preview['name']}</b>\n"
        f"Type: {preview['type']}\n"
        f"Total budget: {fmt(preview['total_budget'], currency)}\n"
        f"Daily budget: {fmt(preview['daily_budget'], currency)}\n"
        f"CPM bid: {fmt(preview['bid_cpm'], currency)}\n"
        f"Estimated impressions: ~{preview['estimated_impressions']:,}\n"
        f"Countries: {', '.join(preview['countries'])}\n"
        f"Categories: {', '.join(preview['categories'])}\n"
        f"Duration: {preview['duration_days']} days\n\n"
        "<i>The impression figure is an estimate from historical channel "
        "performance. You are billed only for measured impressions.</i>"
    )


def campaign_stats(stats: dict) -> str:
    currency = stats["currency"]
    return (
        "<b>📊 Campaign statistics</b>\n\n"
        f"Status: {stats['status']}\n"
        f"Spent: {fmt(stats['spend'], currency)}\n"
        f"Remaining budget: {fmt(stats['remaining_budget'], currency)}\n"
        f"Billable impressions: {stats['billable_impressions']:,}\n"
        f"Clicks: {stats['clicks']:,}\n"
        f"CTR: {stats['ctr_percent']}%\n"
        f"Effective CPM: {fmt(stats['effective_cpm'], currency)}\n"
        f"Cost per click: {fmt(stats['cost_per_click'], currency)}\n"
        f"Channels reached: {stats['channels_reached']}"
    )


def publisher_stats(overview: dict, currency: str) -> str:
    return (
        "<b>📊 Publisher statistics</b>\n\n"
        f"Channels: {overview['channels_active']} active of {overview['channels_total']}\n"
        f"Ads served: {overview['ads_served']:,}\n"
        f"Billable impressions: {overview['billable_impressions']:,}\n"
        f"Clicks: {overview['clicks']:,}\n"
        f"CTR: {overview['ctr_percent']}%\n"
        f"Effective CPM: {fmt(overview['effective_cpm'], currency)}\n\n"
        f"Pending: {fmt(overview['pending_balance'], currency)}\n"
        f"Confirmed: <b>{fmt(overview['confirmed_balance'], currency)}</b>"
    )


def channel_verification_prompt(bot_username: str) -> str:
    return (
        "<b>➕ Add a channel or group</b>\n\n"
        "Two steps:\n\n"
        f"<b>1.</b> Add <code>@{bot_username}</code> to your channel as an "
        "administrator with permission to <b>post messages</b>.\n"
        "<b>2.</b> Send me the channel's username, for example "
        "<code>@mychannel</code>, or its t.me link.\n\n"
        "I will confirm with Telegram that I am an admin there and that you own "
        "or administer it. Submitting a channel you do not administer will be "
        "rejected."
    )


def ask_amount(minimum: Decimal, currency: str, what: str = "amount") -> str:
    return f"Send the {what} you want (minimum {fmt(minimum, currency)}):"


def not_registered(role: str) -> str:
    return (
        f"You are not registered as {role} yet.\n"
        "Use the main menu to enable it — it takes one tap."
    )


SUPPORT_FALLBACK = (
    "Describe your issue in one message and our team will pick it up. "
    "Include a campaign or channel name if it is about one."
)
