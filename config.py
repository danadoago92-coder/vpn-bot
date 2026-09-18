"""Portable configuration. Reading this module never creates files or contacts a server."""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import string
from typing import Mapping
from urllib.parse import urlsplit

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"
DEFAULT_WELCOME = "سلام {name} 👋\nبه {brand} خوش آمدید!"
DEFAULT_DELIVERY = (
    "{name} عزیز، سرویس شما در {brand} آماده است ✅\n"
    "📦 سرویس: {plan}\n🧾 سفارش: {order_id}\n📅 اعتبار: {expires_at}\n"
    "کانفیگ و راهنمای اتصال در ادامه آمده است."
)
DEFAULTS = {
    "BOT_TOKEN": "", "ADMIN_IDS": "", "BRAND_NAME": "فروشگاه کانفیگ",
    "WELCOME_TEXT": DEFAULT_WELCOME, "DELIVERY_TEMPLATE": DEFAULT_DELIVERY,
    "CARD_NUMBER": "", "CARD_HOLDER": "", "SUPPORT_USERNAME": "",
    "CHANNEL_USERNAME": "", "TELEGRAM_PROXY_URL": "", "DB_PATH": "data/bot.sqlite3",
    "XUI_URL": "", "XUI_API_TOKEN": "", "XUI_USERNAME": "", "XUI_PASSWORD": "", "XUI_SUB_URL": "",
    "XUI_VERIFY_TLS": "true", "PANEL_CONNECTED": "false", "CARDS_JSON": "[]",
}


def read_env(path: Path = ENV_FILE) -> dict[str, str]:
    """Read our JSON-quoted .env format; also accept simple unquoted/single quotes.

    Values are never evaluated, interpolated, or executed. Setup always emits JSON
    strings, allowing literal dollar signs, apostrophes and line breaks safely.
    """
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ValueError(f"خط {number} فایل تنظیمات معتبر نیست.")
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", key):
            raise ValueError(f"کلید تنظیمات در خط {number} معتبر نیست.")
        if value.startswith('"'):
            try:
                value = json.loads(value)
            except (ValueError, TypeError) as exc:
                raise ValueError(f"کوتیشن تنظیمات در خط {number} معتبر نیست؛ setup.py را اجرا کنید.") from exc
            if not isinstance(value, str):
                raise ValueError(f"مقدار تنظیمات در خط {number} باید متن باشد.")
        elif value.startswith("'"):
            if len(value) < 2 or not value.endswith("'"):
                raise ValueError(f"کوتیشن تنظیمات در خط {number} بسته نشده است.")
            value = value[1:-1]
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
        values[key] = value
    return values


def validate_template(value: str, allowed: set[str], label: str = "متن") -> str:
    if not value.strip() or len(value) > 2000:
        raise ValueError(f"{label} باید بین ۱ تا ۲۰۰۰ نویسه باشد.")
    try:
        for _, field, spec, conversion in string.Formatter().parse(value):
            if field is not None and (field not in allowed or spec or conversion):
                raise ValueError()
    except ValueError as exc:
        raise ValueError(f"متغیرهای مجاز {label}: " + ", ".join("{" + x + "}" for x in sorted(allowed))) from exc
    return value


def _username(value: str, channel: bool = False) -> str:
    value = value.strip().lstrip("@")
    for prefix in ("https://t.me/", "http://t.me/", "t.me/"):
        if value.startswith(prefix):
            value = value[len(prefix):].rstrip("/")
    if value and not re.fullmatch(r"\+[A-Za-z0-9_-]{5,}" if channel and value.startswith("+") else r"[A-Za-z][A-Za-z0-9_]{3,31}", value):
        raise ValueError("آیدی تلگرام یا لینک t.me معتبر وارد کنید.")
    return value


def _url(value: str, label: str, proxy: bool = False) -> str:
    value = value.strip().rstrip("/")
    if not value:
        return ""
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError as exc:
        raise ValueError(f"{label} معتبر نیست.") from exc
    schemes = {"http", "https", "socks5", "socks5h"} if proxy else {"http", "https"}
    if parsed.scheme not in schemes or not parsed.hostname or parsed.fragment or any(c.isspace() for c in value):
        raise ValueError(f"{label} باید آدرس کامل با پروتکل باشد.")
    if not proxy and (parsed.username or parsed.password or parsed.query):
        raise ValueError(f"{label} نباید شامل نام کاربری، رمز یا query باشد.")
    return value


@dataclass(frozen=True)
class Settings:
    token: str = ""
    admin_ids: tuple[int, ...] = ()
    brand_name: str = DEFAULTS["BRAND_NAME"]
    welcome_text: str = DEFAULT_WELCOME
    delivery_template: str = DEFAULT_DELIVERY
    card_number: str = ""
    card_holder: str = ""
    support_username: str = ""
    channel_username: str = ""
    telegram_proxy_url: str = ""
    db_path: Path = BASE_DIR / "data/bot.sqlite3"
    xui_url: str = ""
    xui_api_token: str = ""
    xui_username: str = ""
    xui_password: str = ""
    xui_sub_url: str = ""
    xui_verify_tls: bool = True
    panel_connected: bool | None = None
    cards: tuple = ()
    admins_file: Path | None = None


def settings_from_values(values: Mapping[str, str], require_token: bool = True, base_dir: Path = BASE_DIR) -> Settings:
    data = {**DEFAULTS, **values}
    token = data["BOT_TOKEN"].strip()
    if (require_token or token) and not re.fullmatch(r"[0-9]{5,}:[A-Za-z0-9_-]{20,}", token):
        raise ValueError("توکن ربات معتبر نیست؛ توکن BotFather را در setup.py وارد کنید.")
    try:
        admins = tuple(dict.fromkeys(int(x) for x in re.split(r"[\s,،]+", data["ADMIN_IDS"].strip()) if x))
    except ValueError as exc:
        raise ValueError("شناسه ادمین باید عدد باشد؛ چند شناسه را با ویرگول جدا کنید.") from exc
    if any(x <= 0 for x in admins) or (require_token and not admins):
        raise ValueError("حداقل یک شناسه عددی مثبت برای ادمین لازم است.")
    brand = data["BRAND_NAME"].strip()
    if not brand or len(brand) > 80:
        raise ValueError("نام برند باید بین ۱ تا ۸۰ نویسه باشد.")
    card = re.sub(r"[\s-]", "", data["CARD_NUMBER"]).translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))
    if card and not re.fullmatch(r"[0-9]{16}", card):
        raise ValueError("شماره کارت باید ۱۶ رقم باشد.")
    if len(data["CARD_HOLDER"]) > 100:
        raise ValueError("نام صاحب کارت حداکثر ۱۰۰ نویسه است.")
    tls = data["XUI_VERIFY_TLS"].strip().lower()
    if tls not in {"true", "false", "1", "0", "yes", "no"}:
        raise ValueError("XUI_VERIFY_TLS باید true یا false باشد.")
    db = Path(data["DB_PATH"].strip() or DEFAULTS["DB_PATH"]).expanduser()
    db = (base_dir / db).resolve() if not db.is_absolute() else db.resolve()
    if db == ENV_FILE.resolve() or db.is_dir():
        raise ValueError("DB_PATH باید مسیر فایل دیتابیس باشد.")
    xui_url = _url(data["XUI_URL"], "آدرس پنل")
    xui_sub_url = _url(data["XUI_SUB_URL"], "آدرس سابسکریپشن")
    if xui_url and (not xui_sub_url or not (data["XUI_API_TOKEN"].strip() or (data["XUI_USERNAME"].strip() and data["XUI_PASSWORD"]))):
        raise ValueError("اتصال پنل به نام کاربری، رمز و آدرس پایه سابسکریپشن نیاز دارد.")
    from card_rotation import normalize_cards
    cards = normalize_cards(data["CARDS_JSON"])
    panel_connected = data["PANEL_CONNECTED"].strip().lower()
    if panel_connected not in {"true", "false", "1", "0", "yes", "no"}:
        raise ValueError("PANEL_CONNECTED باید true یا false باشد.")
    return Settings(
        token=token, admin_ids=admins, brand_name=brand,
        welcome_text=validate_template(data["WELCOME_TEXT"], {"brand", "name"}, "خوش‌آمدگویی"),
        delivery_template=validate_template(data["DELIVERY_TEMPLATE"], {"brand", "name", "plan", "order_id", "expires_at"}, "تحویل سرویس"),
        card_number=card, card_holder=data["CARD_HOLDER"].strip(),
        support_username=_username(data["SUPPORT_USERNAME"]), channel_username=_username(data["CHANNEL_USERNAME"], True),
        telegram_proxy_url=_url(data["TELEGRAM_PROXY_URL"], "پروکسی", True), db_path=db,
        xui_url=xui_url, xui_api_token=data["XUI_API_TOKEN"].strip(), xui_username=data["XUI_USERNAME"].strip(), xui_password=data["XUI_PASSWORD"],
        xui_sub_url=xui_sub_url, xui_verify_tls=tls in {"true", "1", "yes"},
        cards=tuple(cards), panel_connected=panel_connected in {"true", "1", "yes"},
    )


def load_settings(require_token: bool = True, *, env_file: Path | None = None, environ: Mapping[str, str] | None = None) -> Settings:
    from dataclasses import replace
    from admins import read_admins
    actual_env = Path(env_file) if env_file is not None else ENV_FILE
    admin_file = actual_env.parent / "admins.json"
    values = read_env(actual_env)
    source = os.environ if environ is None else environ
    values.update({key: source[key] for key in DEFAULTS if key in source})
    if admin_file.exists():
        values["ADMIN_IDS"] = ",".join(map(str, read_admins(admin_file)))
    elif require_token:
        raise ValueError("admins.json پیدا نشد؛ setup.py را اجرا کنید.")
    return replace(settings_from_values(values, require_token), admins_file=admin_file)

# Optional legacy-compatible catalog, safely empty in a fresh installation.
# Use tools/import_plans.py to import a literal PLANS dict/list from old config.py.
PLANS = []
