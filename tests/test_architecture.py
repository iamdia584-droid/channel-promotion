"""Structural rules, enforced by the build rather than by review memory.

These tests fail if the codebase drifts away from its own documented design —
which is the only way a layering rule survives contact with a deadline.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app"


def _modules(*subdirs: str) -> list[Path]:
    out: list[Path] = []
    for subdir in subdirs:
        out.extend(sorted((APP / subdir).rglob("*.py")))
    return out


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_services_do_not_import_transport_layers():
    """A service must be callable from the API, the bot and a worker alike.

    If a service reaches into FastAPI or aiogram it can only be used from one of
    them, and the business rule it holds gets duplicated for the others.
    """
    offenders = []
    for path in _modules("services"):
        for name in _imports(path):
            if name.startswith(
                ("app.api", "app.bot", "app.admin", "fastapi", "aiogram", "starlette")
            ):
                offenders.append(f"{path.relative_to(ROOT)} imports {name}")
    assert offenders == [], "\n".join(offenders)


def test_models_do_not_import_services():
    """Models are data plus constraints; business logic lives above them."""
    offenders = []
    for path in _modules("models"):
        for name in _imports(path):
            if name.startswith(("app.services", "app.api", "app.bot", "app.admin")):
                offenders.append(f"{path.relative_to(ROOT)} imports {name}")
    assert offenders == [], "\n".join(offenders)


def test_core_does_not_import_models_or_services():
    """``app.core`` is the base of the dependency graph and must stay there."""
    offenders = []
    for path in _modules("core"):
        for name in _imports(path):
            if name.startswith(("app.services", "app.api", "app.bot", "app.admin")):
                offenders.append(f"{path.relative_to(ROOT)} imports {name}")
    assert offenders == [], "\n".join(offenders)


def test_only_the_ledger_service_posts_ledger_entries():
    """Spec §15: no balance may change outside the ledger.

    Anything that constructs a LedgerEntry itself bypasses the balance check, the
    idempotency key and the deadlock-free lock ordering.
    """
    allowed = {
        "app/services/ledger.py",  # the only writer
        "app/models/money.py",  # where the class is declared
    }
    offenders = []
    for path in sorted(APP.rglob("*.py")):
        relative = path.relative_to(ROOT).as_posix()
        if relative in allowed:
            continue
        if re.search(r"\bLedgerEntry\s*\(", path.read_text()):
            offenders.append(relative)
    assert offenders == [], (
        "these modules construct LedgerEntry directly instead of using "
        "LedgerService.post(): " + ", ".join(offenders)
    )


def test_no_float_arithmetic_on_money_in_services():
    """A float literal in a money calculation is the bug this project avoids.

    Catching ``float(`` and bare decimal literals used with money keeps the
    fixed-point guarantee from eroding one convenience call at a time.
    """
    offenders = []
    for path in _modules("services") + _modules("api"):
        text = path.read_text()
        for lineno, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#") or "noqa: float-ok" in stripped:
                continue
            if re.search(r"\bfloat\s*\(", stripped):
                offenders.append(f"{path.relative_to(ROOT)}:{lineno} {stripped[:70]}")
    # The delivery engine converts an already-computed score to float purely to
    # feed random.choices, which never touches money.
    allowed_substrings = ("random.choices", "weights = [float(")
    offenders = [o for o in offenders if not any(a in o for a in allowed_substrings)]
    assert offenders == [], "\n".join(offenders)


def test_every_setting_used_in_code_is_declared():
    """A typo'd setting key would silently fall back to a default, or raise."""
    from app.services.settings_service import SPEC_BY_KEY

    used: set[str] = set()
    # Anchored on the settings accessors specifically. A looser pattern matches
    # any dict .get() and produces noise instead of signal.
    pattern = re.compile(
        r"(?:self\.settings|\bsettings|SettingsService\([^)]*\))"
        r"\.(?:raw|decimal|money|int_|bool_|str_)\(\s*[\"']([a-z0-9_]+)[\"']"
    )
    for path in sorted(APP.rglob("*.py")):
        used.update(pattern.findall(path.read_text()))
    unknown = sorted(used - set(SPEC_BY_KEY))
    assert unknown == [], f"undeclared settings referenced in code: {unknown}"


def test_every_notification_template_referenced_in_code_exists():
    """A missing template raises at send time, which is far too late."""
    from app.services.notifications import TEMPLATES

    pattern = re.compile(
        r"queue(?:_for_publisher|_for_advertiser|_for_admins)?\(\s*\n?\s*"
        r"(?:[a-z_.]+,\s*)?[\"']([a-z0-9_]+)[\"']"
    )
    used: set[str] = set()
    for path in sorted(APP.rglob("*.py")):
        used.update(pattern.findall(path.read_text()))
    unknown = sorted(used - set(TEMPLATES))
    assert unknown == [], f"unknown notification templates referenced: {unknown}"


def test_every_event_name_emitted_is_declared():
    """Spec §30 names the events; an ad-hoc string would not be subscribable."""
    from app.core.events import Event

    pattern = re.compile(r"emit\(\s*\n?\s*[a-z_.]+,\s*[\"']([a-z0-9_.]+)[\"']")
    used: set[str] = set()
    for path in sorted(APP.rglob("*.py")):
        used.update(pattern.findall(path.read_text()))
    unknown = sorted(used - Event.ALL)
    assert unknown == [], f"undeclared event names emitted: {unknown}"


def test_no_secrets_are_hard_coded():
    """Spec §28: no token, key or password in source."""
    suspicious = re.compile(
        r"(?:bot_?token|api_?key|secret_?key|password)\s*=\s*[\"'][A-Za-z0-9_\-:]{16,}[\"']",
        re.IGNORECASE,
    )
    offenders = []
    for path in sorted(APP.rglob("*.py")):
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            if suspicious.search(line) and "dev-only-insecure-key" not in line:
                offenders.append(f"{path.relative_to(ROOT)}:{lineno}")
    assert offenders == [], "\n".join(offenders)


def test_telegram_token_is_never_logged():
    """A token in a log sink is a token in a breach."""
    offenders = []
    for path in sorted(APP.rglob("*.py")):
        text = path.read_text()
        if "app/core/logging.py" in str(path):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if re.search(r"log\.[a-z]+\(.*telegram_bot_token", line):
                offenders.append(f"{path.relative_to(ROOT)}:{lineno}")
    assert offenders == [], "\n".join(offenders)


def test_documented_tables_all_exist():
    """Spec §24 lists the schema; each name must map to a real table."""
    from app.models import Base

    required = {
        "users",
        "advertisers",
        "publishers",
        "telegram_chats",
        "publisher_channels",
        "campaigns",
        "advertisements",
        "campaign_targets",
        "campaign_publishers",
        "ad_deliveries",
        "impressions",
        "clicks",
        "conversion_events",
        "wallets",
        "wallet_transactions",
        "ledger_entries",
        "publisher_earnings",
        "withdrawals",
        "deposits",
        "fraud_events",
        "fraud_scores",
        "moderation_reviews",
        "reports",
        "audit_logs",
        "notifications",
        "system_settings",
    }
    missing = sorted(required - set(Base.metadata.tables))
    assert missing == [], f"tables named in the spec are missing: {missing}"


@pytest.mark.parametrize(
    "table,columns",
    [
        ("campaigns", ["status", "starts_at", "ends_at"]),
        ("publisher_channels", ["status"]),
        ("telegram_chats", ["telegram_chat_id"]),
        ("ad_deliveries", ["campaign_id"]),
        ("ad_deliveries", ["publisher_id"]),
        ("impressions", ["occurred_at"]),
        ("ledger_transactions", ["created_at"]),
        ("withdrawals", ["status"]),
    ],
)
def test_spec_required_indexes_exist(table, columns):
    """Spec §24's index list. Without these, the delivery query table-scans."""
    from app.models import Base

    target = Base.metadata.tables[table]
    indexed: set[str] = set()
    for index in target.indexes:
        indexed.update(c.name for c in index.columns)
    for column in target.columns:
        if column.index or column.primary_key or column.unique:
            indexed.add(column.name)
    for constraint in target.constraints:
        indexed.update(c.name for c in getattr(constraint, "columns", []))
    missing = [c for c in columns if c not in indexed]
    assert missing == [], f"{table} is missing an index covering {missing}"


def test_readme_and_design_docs_exist():
    for name in (
        "README.md",
        "docs/ARCHITECTURE.md",
        "docs/TELEGRAM_CONSTRAINTS.md",
        "docs/ROADMAP.md",
    ):
        path = ROOT / name
        assert path.exists(), f"{name} is missing"
        assert len(path.read_text()) > 400, f"{name} is a stub"


def test_env_example_lists_every_required_setting():
    """A deployment fails confusingly if .env.example omits a required variable."""
    example = (ROOT / ".env.example").read_text()
    for key in (
        "DATABASE_URL",
        "REDIS_URL",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_WEBHOOK_SECRET",
        "SECRET_KEY",
        "BASE_URL",
        "DEFAULT_CURRENCY",
        "BOOTSTRAP_ADMIN_EMAIL",
    ):
        assert key in example, f"{key} is not documented in .env.example"


def test_env_example_contains_no_real_values():
    """A committed .env.example with a real token is a leaked token."""
    example = (ROOT / ".env.example").read_text()
    for line in example.splitlines():
        if line.startswith("TELEGRAM_BOT_TOKEN=") or line.startswith("MTPROTO_API_HASH="):
            assert line.split("=", 1)[1].strip() == "", f"{line} carries a value"
