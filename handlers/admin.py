"""Persian administrator workflows, with authorization on every entry point."""

from __future__ import annotations

from admins import admin_ids, update_admins

import math
import re
import string

from telegram.error import TelegramError

from database import validate_config
from card_rotation import format_card_number
from ui import button, esc, is_admin, money, runtime, send


PAGE_SIZE = 8
MAX_PRICE = 1_000_000_000
MAX_CONFIG_LENGTH = 50_000
_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
_STATUSES = {
    "pending": "منتظر پرداخت",
    "awaiting_review": "منتظر بررسی فیش",
    "awaiting_config": "پرداخت تأیید شده؛ منتظر کانفیگ",
    "processing": "در حال تحویل",
    "paid": "پرداخت و تحویل ثبت شده",
    "failed": "تحویل ناموفق؛ نیازمند بررسی",
    "legacy_review": "سفارش قبلی؛ نیازمند تطبیق دستی",
    "rejected": "رد شده",
    "cancelled": "لغو شده",
}
_MODES = {"manual": "ارسال دستی لینک", "inventory": "موجودی لینک آماده", "xui": "ساخت خودکار با پنل"}
_PROTOCOLS = {"v2ray": "🔗 کانفیگ عادی", "wireguard": "🛡 WireGuard", "openvpn": "🔐 OpenVPN"}
_FIELDS = {
    "name": "نام پلن", "price": "قیمت (تومان)", "traffic_gb": "حجم (گیگابایت)",
    "description": "توضیحات", "inbound_id": "شناسه اینباند",
}
_SETTINGS = {
    "brand_name": ("نام برند", "نام برند جدید را بفرستید؛ حداکثر ۶۰ حرف."),
    "welcome_text": ("پیام خوش‌آمد", "پیام خوش‌آمد را بفرستید. متغیرهای مجاز: {brand} برای برند و {name} برای نام کاربر."),
    "card_number": ("شماره کارت", "شماره کارت ۱۶ رقمی را بفرستید."),
    "card_holder": ("نام صاحب کارت", "نام صاحب کارت را بفرستید؛ حداکثر ۱۰۰ حرف."),
    "support_username": ("آیدی پشتیبانی", "آیدی پشتیبانی را با @ یا بدون آن بفرستید. برای حذف، - بفرستید."),
    "delivery_template": (
        "پیام تحویل کانفیگ",
        "متن پیام تحویل را بفرستید. متغیرهای مجاز: {brand}، {name}، {plan}، {order_id}، {expires_at}. "
        "خود کانفیگ به‌صورت جداگانه به پیام اضافه می‌شود.",
    ),
    "tutorial_android": ("آموزش اندروید", "متن آموزش اندروید را بفرستید. می‌توانید لینک دانلود برنامه را هم اضافه کنید."),
    "tutorial_ios": ("آموزش آیفون", "متن آموزش آیفون را بفرستید. می‌توانید لینک دانلود برنامه را هم اضافه کنید."),
    "tutorial_windows": ("آموزش ویندوز", "متن آموزش ویندوز را بفرستید. می‌توانید لینک دانلود برنامه را هم اضافه کنید."),
}


def _store(ctx):
    return ctx.bot_data["store"]


def _back(data="adm:home", label="↩️ بازگشت"):
    return [button(label, data)]


def _number(value: str) -> str:
    return value.translate(_DIGITS).replace(",", "").replace("٬", "").strip()


def _integer(value: str, low=1, high=MAX_PRICE) -> int:
    normalized = _number(value)
    if not normalized.isascii() or not normalized.isdecimal():
        raise ValueError("فقط عدد صحیح وارد کنید؛ نمونه: ۱۵۰۰۰۰")
    result = int(normalized)
    if not low <= result <= high:
        raise ValueError(f"عدد باید بین {low:,} و {high:,} باشد.")
    return result


def _config_lines(value: str, *, inventory=False, protocol="v2ray") -> list[str]:
    if len(value) > (MAX_CONFIG_LENGTH if inventory else 12_000):
        raise ValueError("متن خیلی طولانی است؛ آن را در چند نوبت بفرستید.")
    if protocol != "v2ray":
        if inventory:
            raise ValueError("موجودی متنی فقط برای کانفیگ عادی است؛ برای این پروتکل تحویل دستی فایل را انتخاب کن.")
        return [validate_config(value, protocol)]
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    if not lines or (inventory and len(lines) > 100):
        raise ValueError("در هر نوبت بین ۱ تا ۱۰۰ کانفیگ بفرستید؛ هر کانفیگ در یک خط.")
    if not inventory and len(lines) != 1:
        raise ValueError("برای هر سفارش فقط یک لینک سابسکریپشن بفرستید.")
    return list(dict.fromkeys(validate_config(line, protocol) for line in lines))


async def _authorized(update, ctx) -> bool:
    if is_admin(update, ctx):
        return True
    ctx.user_data.pop("admin_state", None)
    await send(update, "⛔️ این بخش فقط برای مدیر ربات است.", [[button("🏠 خانه", "home")]])
    return False


async def menu(update, ctx):
    if not await _authorized(update, ctx):
        return
    ctx.user_data.pop("admin_state", None)
    await send(update, "👑 <b>مدیریت ربات</b>\nپلن‌ها، پرداخت‌ها و پیام‌های برند را از اینجا مدیریت کنید.", [
        [button("🛍 مدیریت پلن‌ها", "adm:plans")],
        [button("🧾 پرداخت‌ها و تحویل‌ها", "adm:orders:0"), button("📊 آمار", "adm:stats")],
        [button("👥 کاربران", "adm:users:0"), button("🛡 مدیریت ادمین‌ها", "adm:admins")],
        [button("⚙️ برند و پرداخت", "adm:settings"), button("📚 ویرایش آموزش‌ها", "adm:tutorials")],
        [button("🧭 لوکیشن‌ها و پروتکل‌ها", "cat:locations")],
        [button("⚙️ اتصال پنل", "cat:panel"),button("💳 کارت‌های چرخشی", "cat:cards")],
        [button("🏠 منوی اصلی", "home")],
    ])


async def _plans(update, ctx, months=None, offset=0):
    if months is None:
        await send(update, "🛍 <b>مدیریت پلن‌ها</b>\nپلن‌های هر لوکیشن و پروتکل می‌توانند قیمت و حجم جدا داشته باشند.", [
            [button("🗓 یک‌ماهه · ۳۰ روز", "adm:plans:1:0"), button("🗓 دوماهه · ۶۰ روز", "adm:plans:2:0")],
            _back(),
        ])
        return
    if months not in (1, 2) or offset < 0:
        raise ValueError("مدت پلن نامعتبر است.")
    plans = await _store(ctx).list_plans(months=months, active_only=False)
    shown = plans[offset:offset + PAGE_SIZE]
    rows = [[button(f"{'🟢' if p['active'] else '⚪️'} {p['name']} · {money(p['price'])}", f"adm:plan:{p['id']}")] for p in shown]
    nav = []
    if offset:
        nav.append(button("→ قبلی", f"adm:plans:{months}:{max(0, offset - PAGE_SIZE)}"))
    if offset + PAGE_SIZE < len(plans):
        nav.append(button("بعدی ←", f"adm:plans:{months}:{offset + PAGE_SIZE}"))
    if nav:
        rows.append(nav)
    rows.extend([[button("➕ ساخت پلن جدید", f"adm:new:{months}")], _back("adm:plans")])
    await send(update, f"🌍 <b>پلن‌های {months} ماهه</b>\n" + ("برای مدیریت روی نام پلن بزنید." if plans else "هنوز پلنی ساخته نشده است."), rows)


async def _plan(update, ctx, plan_id):
    plan = await _store(ctx).get_plan(plan_id)
    if not plan or plan.get("archived"):
        raise ValueError("این پلن پیدا نشد یا حذف شده است.")
    mode = plan["mode"]
    volume = f"{plan['traffic_gb']:g} گیگابایت" if plan["traffic_gb"] else "نامحدود"
    stock = await _store(ctx).inventory_count(plan_id) if mode == "inventory" else None
    text = (
        f"🌍 <b>{esc(plan['name'])}</b>\n\n"
        f"مدت: {plan['months']} ماه ({plan['days']} روز)\n"
        f"حجم: {volume}\nقیمت: {money(plan['price'])}\n"
        f"تحویل: {esc(_MODES.get(mode, mode))}\n"
        f"وضعیت: {'فعال و قابل خرید' if plan['active'] else 'غیرفعال'}"
    )
    location = next((x for x in await _store(ctx).list_locations() if x["id"] == plan.get("location_id")), None)
    text += f"\nلوکیشن: {esc((location or {}).get('name', plan.get('location_id', 'نامشخص')))}"
    text += f"\nپروتکل: {esc(_PROTOCOLS.get(plan.get('protocol'), plan.get('protocol', 'نامشخص')))}"
    if stock is not None:
        text += f"\nموجودی آماده: {stock} عدد"
    if mode == "xui":
        text += f"\nشناسه اینباند: {plan['inbound_id']}"
    if plan.get("description"):
        text += f"\n\n{esc(plan['description'])}"
    rows = [
        [button("✏️ نام", f"adm:edit:{plan_id}:name"), button("💰 قیمت", f"adm:edit:{plan_id}:price")],
        [button("📦 حجم", f"adm:edit:{plan_id}:traffic_gb"), button("📝 توضیحات", f"adm:edit:{plan_id}:description")],
        [button("📤 روش تحویل", f"adm:mode:{plan_id}"), button("🗓 تغییر مدت", f"adm:months:{plan_id}")],
        [button("⏸ غیرفعال کردن" if plan["active"] else "▶️ فعال کردن", f"adm:toggle:{plan_id}")],
    ]
    rows.append([button("🧭 تغییر لوکیشن", f"cat:planlocation:{plan_id}"), button("📡 تغییر پروتکل", f"cat:planprotocol:{plan_id}")])
    if mode == "inventory":
        rows.insert(0, [button(f"➕ افزودن کانفیگ · موجودی {stock}", f"adm:stock:{plan_id}")])
    if mode == "xui":
        rows.append([button("⚙️ تغییر اینباند", f"adm:edit:{plan_id}:inbound_id")])
    rows.extend([[button("🗑 حذف از لیست فروش", f"adm:archive:{plan_id}")], _back(f"adm:plans:{plan['months']}:0")])
    await send(update, text, rows)


def _mode_rows(ctx, prefix, protocol="v2ray"):
    rows = [[button("👤 ارسال دستی بعد از تأیید", f"{prefix}:manual")]]
    if protocol == "v2ray":
        rows.append([button("📦 موجودی آماده", f"{prefix}:inventory")])
    settings = ctx.bot_data["settings"]
    if protocol == "v2ray" and settings.xui_url and settings.xui_sub_url and (settings.xui_api_token or (settings.xui_username and settings.xui_password)):
        rows.append([button("⚡️ ساخت خودکار با پنل", f"{prefix}:xui")])
    return rows


def _require_mode(ctx, mode, protocol="v2ray"):
    if mode not in _MODES:
        raise ValueError("روش تحویل نامعتبر است.")
    settings = ctx.bot_data["settings"]
    if mode == "xui" and not (settings.xui_url and settings.xui_sub_url and (settings.xui_api_token or (settings.xui_username and settings.xui_password))):
        raise ValueError("اول اتصال پنل و آدرس اشتراک را در راه‌انداز SSH تنظیم کنید.")
    if mode == "xui" and protocol != "v2ray":
        raise ValueError("ساخت خودکار پنل فعلی فقط برای کانفیگ عادی در دسترس است.")
    if mode == "inventory" and protocol != "v2ray":
        raise ValueError("موجودی آماده فعلاً برای کانفیگ عادی است؛ WireGuard و OpenVPN را دستی با فایل تحویل بده.")


async def _ask_plan_field(update, ctx, state):
    field = state["field"]
    hint = {
        "name": "نام پلن را بفرستید؛ نمونه: مولتی لوکیشن ۳۰ گیگ. حداکثر ۸۰ حرف.",
        "price": "قیمت پلن را به تومان بفرستید؛ نمونه: ۱۵۰۰۰۰",
        "traffic_gb": "حجم را به گیگابایت بفرستید؛ نمونه: ۳۰ یا ۵۰.۵\nبرای حجم نامحدود، ۰ بفرستید.",
        "inbound_id": "شناسه عددی اینباند پنل را بفرستید؛ نمونه: ۱",
        "description": "توضیح کوتاه پلن را بفرستید؛ حداکثر ۵۰۰ حرف. برای بدون توضیح، - بفرستید.",
    }[field]
    ctx.user_data["admin_state"] = state
    target = f"adm:plan:{state['plan_id']}" if state["kind"] == "edit_plan" else f"adm:plans:{state['plan']['months']}:0"
    await send(update, f"✏️ <b>{_FIELDS[field]}</b>\n\n{hint}\n\nبرای لغو /cancel را بفرستید.", [_back(target, "✖️ انصراف")])


def _plan_value(field: str, value: str):
    if field == "price":
        return _integer(value)
    if field == "inbound_id":
        return _integer(value, 1, 2_147_483_647)
    if field == "traffic_gb":
        try:
            amount = float(_number(value).replace("٫", "."))
        except ValueError:
            raise ValueError("حجم را به‌صورت عدد وارد کنید؛ نمونه: ۳۰ یا ۵۰.۵") from None
        if not math.isfinite(amount) or not 0 <= amount <= 1_000_000:
            raise ValueError("حجم باید عددی بین صفر تا یک میلیون گیگابایت باشد.")
        return amount
    maximum = 80 if field == "name" else 500
    if not value or len(value) > maximum:
        raise ValueError(f"متن باید بین ۱ تا {maximum} حرف باشد.")
    return "" if field == "description" and value == "-" else value


async def _new_review(update, ctx, state):
    state["field"] = "review"
    ctx.user_data["admin_state"] = state
    p = state["plan"]
    volume = f"{p['traffic_gb']:g} گیگ" if p["traffic_gb"] else "نامحدود"
    location = next((x for x in await _store(ctx).list_locations() if x["id"] == p["location_id"]), None)
    await send(update,
        f"✅ <b>ثبت پلن جدید</b>\n\nنام: {esc(p['name'])}\nمدت: {p['months']} ماه\n"
        f"لوکیشن: {esc((location or {}).get('name', p['location_id']))}\nپروتکل: {esc(_PROTOCOLS[p['protocol']])}\n"
        f"حجم: {volume}\nقیمت: {money(p['price'])}\nتحویل: {_MODES[p['mode']]}\n\n"
        f"{esc(p.get('description', ''))}\n\nپلن پس از ثبت فعال می‌شود.", [
            [button("✅ ثبت پلن", "adm:create")], _back(f"adm:plans:{p['months']}:0", "✖️ انصراف"),
        ])


async def _orders(update, ctx, offset=0):
    if offset < 0:
        raise ValueError("صفحه نامعتبر است.")
    orders = await _store(ctx).pending_orders(limit=PAGE_SIZE + 1, offset=offset)
    rows = []
    for order in orders[:PAGE_SIZE]:
        kind = "شارژ کیف پول" if order["kind"] == "topup" else order.get("plan_snapshot", {}).get("name", "خرید")
        rows.append([button(f"#{order['id']} · {kind} · {_STATUSES.get(order['status'], order['status'])}", f"adm:order:{order['id']}")])
    nav = []
    if offset:
        nav.append(button("→ قبلی", f"adm:orders:{max(0, offset - PAGE_SIZE)}"))
    if len(orders) > PAGE_SIZE:
        nav.append(button("بعدی ←", f"adm:orders:{offset + PAGE_SIZE}"))
    if nav:
        rows.append(nav)
    rows.extend([[button("🔄 تازه‌سازی", f"adm:orders:{offset}")], _back()])
    await send(update, "🧾 <b>پرداخت‌ها و تحویل‌های نیازمند بررسی</b>\n\n" + ("برای بررسی فیش و تحویل روی سفارش بزنید." if orders else "موردی برای بررسی وجود ندارد."), rows)


async def _order(update, ctx, order_id, notice=""):
    order = await _store(ctx).get_order(order_id)
    if not order:
        raise ValueError("سفارش پیدا نشد.")
    user = await _store(ctx).get_user(order["user_id"])
    customer = user.get("full_name") or str(order["user_id"]) if user else str(order["user_id"])
    kind = "شارژ کیف پول" if order["kind"] == "topup" else order.get("plan_snapshot", {}).get("name", "خرید سرویس")
    text = (
        f"🧾 <b>سفارش #{order_id}</b>\n\n"
        f"کاربر: {esc(customer)} · <code>{order['user_id']}</code>\n"
        f"موضوع: {esc(kind)}\nمبلغ: {money(order['amount'])}\n"
        f"پرداخت: {'کارت به کارت' if order['method'] == 'card' else 'کیف پول'}\n"
        f"وضعیت: {esc(_STATUSES.get(order['status'], order['status']))}\n"
        f"ثبت: {esc(order.get('created_at', ''))}"
    )
    if order.get("error"):
        text += "\n\n⚠️ تحویل کامل نشده؛ اتصال پنل یا موجودی را بررسی و سپس دوباره تلاش کنید."
    if order["status"] == "paid" and not order.get("delivered_at"):
        text += "\n\n⚠️ پیام تحویل به کاربر نرسیده است."
    if notice:
        text += f"\n\n{esc(notice)}"
    rows = []
    if order.get("receipt_file_id"):
        rows.append([button("🖼 مشاهده فیش", f"adm:receipt:{order_id}")])
    if order["status"] in ("awaiting_review", "failed"):
        if order["method"] == "card" and order.get("receipt_file_id"):
            label = "✅ تأیید پرداخت و تحویل" if order["kind"] == "purchase" else "✅ تأیید شارژ کیف پول"
            actions = [button(label, f"adm:approve:{order_id}")]
            if not order.get("approved_at"):
                actions.append(button("❌ رد پرداخت", f"adm:reject:{order_id}"))
            rows.append(actions)
        elif order["status"] == "failed" and order["kind"] == "purchase" and order["method"] == "wallet":
            rows.append([button("🔄 تلاش مجدد برای تحویل", f"adm:approve:{order_id}")])
    if order["status"] == "awaiting_config":
        protocol = order.get("plan_snapshot", {}).get("protocol", "v2ray")
        rows.append([button("📤 ارسال لینک یا فایل کانفیگ", f"adm:manual:{order_id}") if protocol != "v2ray" else button("📤 ارسال لینک اشتراک", f"adm:manual:{order_id}")])
    if order["status"] in ("processing", "paid") and order["kind"] == "purchase" and not order.get("delivered_at"):
        rows.append([button("🔄 بازیابی / ارسال مجدد تحویل", f"adm:approve:{order_id}")])
    if order["status"] == "paid" and order["kind"] == "topup" and not order.get("delivered_at"):
        rows.append([button("🔄 ارسال اعلان شارژ", f"adm:approve:{order_id}")])
    rows.extend([[button("👤 اطلاعات خریدار", f"adm:user:{order['user_id']}")], _back("adm:orders:0")])
    await send(update, text, rows)


async def _manual_prompt(update, ctx, order_id):
    order = await _store(ctx).get_order(order_id)
    if not order or order["status"] != "awaiting_config":
        raise ValueError("این سفارش در انتظار کانفیگ دستی نیست.")
    ctx.user_data["admin_state"] = {"kind": "manual_config", "order_id": order_id}
    plan = order.get("plan_snapshot", {})
    plan_name = plan.get("name", "سرویس")
    protocol = plan.get("protocol", "v2ray")
    instruction = {
        "v2ray": "فقط یک لینک سابسکریپشن کامل با http یا https بفرست.",
        "wireguard": "فایل .conf وایرگارد یا متن کامل کانفیگ را بفرست.",
        "openvpn": "فایل .ovpn یا متن کامل کانفیگ OpenVPN را بفرست.",
    }.get(protocol, "کانفیگ را بفرست.")
    await send(update, f"📤 <b>تحویل سفارش #{order_id}</b>\n\n"
        f"پلن: {esc(plan_name)}\nکاربر: <code>{order['user_id']}</code>\n\n"
        f"{instruction}\nکانفیگ با پیام برند برای همین خریدار ارسال می‌شود.\nبرای انصراف /cancel را بفرستید.",
        [_back(f"adm:order:{order_id}", "✖️ فعلاً ارسال نمی‌کنم")])


async def _approve(update, ctx, order_id):
    from purchase import fulfill_and_notify, notify_topup

    order = await _store(ctx).get_order(order_id)
    if not order:
        raise ValueError("سفارش پیدا نشد.")
    if order["kind"] == "topup":
        result = await _store(ctx).approve_topup(order_id)
        await notify_topup(ctx, result)
    else:
        # The approving admin is taken straight to delivery; another notification
        # for the same action would create the duplicate shown in the chat.
        result = await fulfill_and_notify(
            ctx, order_id, from_wallet=order["method"] == "wallet", notify_waiting=False
        )
        if result.get("status") == "awaiting_config":
            await _manual_prompt(update, ctx, order_id)
            return
    updated = await _store(ctx).get_order(order_id)
    notice = "✅ عملیات با موفقیت ثبت شد."
    if updated["status"] == "paid" and not updated.get("delivered_at"):
        notice = "✅ پرداخت ثبت شد؛ اعلان به کاربر نرسیده و ارسال آن دوباره پیگیری می‌شود."
    await _order(update, ctx, order_id, notice)


async def _users(update, ctx, offset=0):
    if offset < 0:
        raise ValueError("صفحه نامعتبر است.")
    users = await _store(ctx).list_users(limit=PAGE_SIZE + 1, offset=offset)
    rows = [[button(f"{'🚫' if u['blocked'] else '👤'} {u.get('full_name') or u['id']} · {u['id']}", f"adm:user:{u['id']}")] for u in users[:PAGE_SIZE]]
    nav = []
    if offset:
        nav.append(button("→ قبلی", f"adm:users:{max(0, offset - PAGE_SIZE)}"))
    if len(users) > PAGE_SIZE:
        nav.append(button("بعدی ←", f"adm:users:{offset + PAGE_SIZE}"))
    if nav:
        rows.append(nav)
    rows.extend([[button("🔎 جست‌وجو با آیدی عددی", "adm:finduser")], _back()])
    await send(update, "👥 <b>کاربران ربات</b>\n" + ("برای مشاهده حساب روی نام کاربر بزنید." if users else "هنوز کاربری ثبت نشده است."), rows)


def _admins_file(ctx):
    path = getattr(ctx.bot_data["settings"], "admins_file", None)
    if path is None:
        raise ValueError("مسیر admins.json تنظیم نشده است؛ setup.py را یک بار اجرا کنید.")
    return path


async def _admins(update, ctx, notice=""):
    current = admin_ids(ctx.bot_data["settings"])
    actor = update.effective_user.id
    rows = []
    for admin_id in current:
        user = await _store(ctx).get_user(admin_id)
        name = (user or {}).get("full_name")
        label = f"{name} · {admin_id}" if name else str(admin_id)
        suffix = " · شما" if admin_id == actor else ""
        data = "adm:admins" if admin_id == actor else f"adm:deladmin:{admin_id}"
        rows.append([button(f"👤 {label}{suffix}", data)])
    rows.extend([[button("➕ افزودن ادمین", "adm:addadmin")], _back()])
    text = "🛡 <b>مدیریت ادمین‌ها</b>\n\n"
    if notice:
        text += esc(notice) + "\n\n"
    text += "برای افزودن، Chat ID عددی تلگرام را وارد کن. تغییرات همان لحظه اعمال می‌شوند و ری‌استارت لازم نیست."
    await send(update, text, rows)


async def _user(update, ctx, user_id):
    user = await _store(ctx).get_user(user_id)
    if not user:
        raise ValueError("این کاربر هنوز ربات را شروع نکرده است.")
    rows = []
    if user_id not in admin_ids(ctx.bot_data["settings"]):
        rows.append([button("✅ رفع مسدودی" if user["blocked"] else "🚫 مسدود کردن", f"adm:block:{user_id}:{0 if user['blocked'] else 1}")])
    rows.append(_back("adm:users:0"))
    await send(update, f"👤 <b>{esc(user.get('full_name') or 'کاربر')}</b>\n\n"
        f"آیدی: <code>{user_id}</code>\nنام کاربری: {esc('@' + user['username'] if user.get('username') else 'ندارد')}\n"
        f"موجودی: {money(user['balance'])}\nوضعیت: {'مسدود' if user['blocked'] else 'فعال'}\n"
        f"عضویت: {esc(user.get('created_at', ''))}", rows)


async def _settings(update, ctx, tutorials=False):
    current = await runtime(ctx)
    keys = [key for key in _SETTINGS if key.startswith("tutorial_") == tutorials]
    rows = [[button(f"✏️ {_SETTINGS[key][0]}", f"adm:setting:{key}")] for key in keys]
    rows.append(_back())
    text = "📚 <b>آموزش اتصال</b>\nمتن هر دستگاه را جداگانه ویرایش کنید." if tutorials else (
        "⚙️ <b>برند و اطلاعات پرداخت</b>\n\n"
        f"برند: {esc(current.get('brand_name', ''))}\n"
        f"کارت: <code>{esc(format_card_number(current.get('card_number', '')) or 'تنظیم نشده')}</code>\n"
        f"صاحب کارت: {esc(current.get('card_holder', '') or 'تنظیم نشده')}\n"
        f"پشتیبانی: {esc('@' + current['support_username'] if current.get('support_username') else 'تنظیم نشده')}\n\n"
        "تغییرات بلافاصله در ربات اعمال می‌شوند. توکن و اتصال پنل از راه‌انداز SSH قابل تنظیم‌اند."
    )
    await send(update, text, rows)


def _setting_value(key: str, value: str):
    maximum = {"brand_name": 60, "card_holder": 100, "welcome_text": 1500, "delivery_template": 1500}.get(key, 3000)
    if not value or len(value) > maximum:
        raise ValueError(f"متن باید بین ۱ تا {maximum} حرف باشد.")
    if key == "card_number":
        value = re.sub(r"[\s-]", "", value.translate(_DIGITS))
        if not re.fullmatch(r"[0-9]{16}", value):
            raise ValueError("شماره کارت باید دقیقاً ۱۶ رقم باشد.")
    if key == "support_username":
        value = value.lstrip("@")
        if value == "-":
            return ""
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}", value):
            raise ValueError("آیدی تلگرام معتبر وارد کنید؛ نمونه: @support_name")
    if key in ("welcome_text", "delivery_template"):
        allowed = {"brand", "name"} if key == "welcome_text" else {"brand", "name", "plan", "order_id", "expires_at"}
        try:
            parsed = list(string.Formatter().parse(value))
        except ValueError:
            raise ValueError("آکولادهای پیام کامل نیستند. نمونه صحیح: {brand}") from None
        if any(name is not None and (name not in allowed or spec or conversion) for _, name, spec, conversion in parsed):
            raise ValueError("فقط از متغیرهای معرفی‌شده در پیام راهنما استفاده کنید.")
    return value


async def callback(update, ctx) -> bool:
    """Handle only adm: callbacks. Root has already answered the callback."""
    query = update.callback_query
    data = query.data if query else ""
    if not isinstance(data, str) or not data.startswith("adm:"):
        return False
    if not await _authorized(update, ctx):
        return True
    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else ""
    keep_state = action in ("create", "newmode", "newloc", "newproto")
    if not keep_state:
        ctx.user_data.pop("admin_state", None)
    try:
        if action in ("home", "menu") and len(parts) == 2:
            await menu(update, ctx)
        elif action == "plans" and len(parts) in (2, 4):
            await _plans(update, ctx, int(parts[2]) if len(parts) == 4 else None, int(parts[3]) if len(parts) == 4 else 0)
        elif action == "plan" and len(parts) == 3:
            await _plan(update, ctx, _integer(parts[2]))
        elif action == "new" and len(parts) == 3:
            months = _integer(parts[2], 1, 2)
            locations = await _store(ctx).list_locations(active_only=True)
            if not locations:
                raise ValueError("اول از بخش لوکیشن‌ها یک لوکیشن بساز.")
            rows = [[button(item["name"], f"adm:newloc:{months}:{item['id']}")] for item in locations]
            await send(update, "🧭 <b>ساخت پلن جدید</b>\n\nلوکیشن پلن را انتخاب کن:", rows + [_back(f"adm:plans:{months}:0", "✖️ انصراف")])
        elif action == "newloc" and len(parts) == 4:
            months = _integer(parts[2], 1, 2)
            protocols = await _store(ctx).location_protocols(parts[3], active_only=True)
            if not protocols:
                raise ValueError("برای این لوکیشن اول حداقل یک پروتکل را روشن کن.")
            rows = [[button(_PROTOCOLS[item["protocol"]], f"adm:newproto:{months}:{parts[3]}:{item['protocol']}")]
                    for item in protocols if item["protocol"] in _PROTOCOLS]
            await send(update, "📡 پروتکل این پلن را انتخاب کن:", rows + [_back(f"adm:plans:{months}:0", "✖️ انصراف")])
        elif action == "newproto" and len(parts) == 5 and parts[4] in _PROTOCOLS:
            months = _integer(parts[2], 1, 2)
            enabled = {item["protocol"] for item in await _store(ctx).location_protocols(parts[3], active_only=True)}
            if parts[4] not in enabled:
                raise ValueError("این پروتکل برای لوکیشن انتخابی خاموش است.")
            await _ask_plan_field(update, ctx, {"kind": "new_plan", "field": "name", "plan": {
                "months": months, "days": months * 30, "mode": "manual", "inbound_id": 0, "active": True, "description": "",
                "location_id": parts[3], "protocol": parts[4],
            }})
        elif action == "newmode" and len(parts) == 3:
            state = ctx.user_data.get("admin_state", {})
            if state.get("kind") != "new_plan" or state.get("field") != "mode":
                raise ValueError("این فرم منقضی شده است؛ ساخت پلن را دوباره شروع کنید.")
            _require_mode(ctx, parts[2], state["plan"]["protocol"])
            state["plan"]["mode"] = parts[2]
            state["field"] = "inbound_id" if parts[2] == "xui" else "description"
            await _ask_plan_field(update, ctx, state)
        elif action == "create" and len(parts) == 2:
            state = ctx.user_data.get("admin_state", {})
            if state.get("kind") != "new_plan" or state.get("field") != "review":
                raise ValueError("این فرم قبلاً ثبت شده یا منقضی شده است.")
            created = await _store(ctx).save_plan(state["plan"])
            ctx.user_data.pop("admin_state", None)
            await _plan(update, ctx, created["id"])
        elif action == "edit" and len(parts) == 4 and parts[3] in _FIELDS:
            plan_id = _integer(parts[2])
            if not await _store(ctx).get_plan(plan_id):
                raise ValueError("پلن پیدا نشد.")
            await _ask_plan_field(update, ctx, {"kind": "edit_plan", "field": parts[3], "plan_id": plan_id})
        elif action in ("mode", "months", "toggle", "stock", "archive") and len(parts) == 3:
            plan_id = _integer(parts[2])
            plan = await _store(ctx).get_plan(plan_id)
            if not plan or plan.get("archived"):
                raise ValueError("پلن پیدا نشد.")
            if action == "mode":
                await send(update, "📤 روش تحویل پلن را انتخاب کنید.\nدر حالت دستی، پس از تأیید پرداخت کانفیگ را برای همان سفارش می‌فرستید.", _mode_rows(ctx, f"adm:setmode:{plan_id}", plan["protocol"]) + [_back(f"adm:plan:{plan_id}")])
            elif action == "months":
                await send(update, "🗓 پلن در کدام لیست نمایش داده شود؟\nمدت سفارش‌های قبلی تغییر نمی‌کند.", [[button("🗓 یک‌ماهه", f"adm:setmonths:{plan_id}:1"), button("🗓 دوماهه", f"adm:setmonths:{plan_id}:2")], _back(f"adm:plan:{plan_id}")])
            elif action == "toggle":
                plan["active"] = not plan["active"]
                await _store(ctx).save_plan(plan)
                await _plan(update, ctx, plan_id)
            elif action == "stock":
                if plan["mode"] != "inventory":
                    raise ValueError("افزودن موجودی فقط برای روش تحویل «موجودی آماده» است.")
                ctx.user_data["admin_state"] = {"kind": "inventory", "plan_id": plan_id}
                count = await _store(ctx).inventory_count(plan_id)
                await send(update, f"📦 <b>موجودی {esc(plan['name'])}</b>\n\nآماده فروش: {count} عدد\n\n"
                    "لینک‌های سابسکریپشن را بفرستید؛ هر خط یک سرویس مستقل و قابل فروش است. "
                    "در هر نوبت حداکثر ۱۰۰ خط. لینک تکراری اضافه نمی‌شود.\nبرای لغو /cancel را بفرستید.", [_back(f"adm:plan:{plan_id}", "✖️ انصراف")])
            else:
                await send(update, f"🗑 پلن «{esc(plan['name'])}» از فروش حذف شود؟\nسفارش‌ها و سرویس‌های قبلی حفظ می‌شوند.", [[button("🗑 بله، حذف از فروش", f"adm:archiveok:{plan_id}")], _back(f"adm:plan:{plan_id}", "↩️ انصراف")])
        elif action in ("setmode", "setmonths") and len(parts) == 4:
            plan_id = _integer(parts[2])
            plan = await _store(ctx).get_plan(plan_id)
            if not plan or plan.get("archived"):
                raise ValueError("پلن پیدا نشد.")
            if action == "setmode":
                _require_mode(ctx, parts[3], plan["protocol"])
                if parts[3] == "xui" and not plan.get("inbound_id"):
                    await _ask_plan_field(update, ctx, {"kind": "edit_plan", "field": "inbound_id", "plan_id": plan_id, "set_mode": "xui"})
                    return True
                plan["mode"] = parts[3]
            else:
                plan["months"] = _integer(parts[3], 1, 2)
                plan["days"] = plan["months"] * 30
            await _store(ctx).save_plan(plan)
            await _plan(update, ctx, plan_id)
        elif action == "archiveok" and len(parts) == 3:
            plan_id = _integer(parts[2])
            plan = await _store(ctx).get_plan(plan_id)
            if not plan:
                raise ValueError("این پلن قبلاً حذف شده است.")
            await _store(ctx).archive_plan(plan_id)
            await _plans(update, ctx, plan["months"])
        elif action in ("orders", "users") and len(parts) == 3:
            await (_orders if action == "orders" else _users)(update, ctx, _integer(parts[2], 0))
        elif action in ("order", "approve", "manual", "receipt", "reject", "rejectok") and len(parts) == 3:
            order_id = _integer(parts[2])
            if action == "order":
                await _order(update, ctx, order_id)
            elif action == "approve":
                await _approve(update, ctx, order_id)
            elif action == "manual":
                await _manual_prompt(update, ctx, order_id)
            elif action == "receipt":
                order = await _store(ctx).get_order(order_id)
                if not order or not order.get("receipt_file_id"):
                    raise ValueError("فیشی برای این سفارش ثبت نشده است.")
                await ctx.bot.send_photo(chat_id=update.effective_user.id, photo=order["receipt_file_id"], caption=f"فیش سفارش #{order_id} · مبلغ {money(order['amount'])}")
                await _order(update, ctx, order_id)
            elif action == "reject":
                order = await _store(ctx).get_order(order_id)
                if not order or order["status"] not in ("awaiting_review", "failed") or order["method"] != "card" or order.get("approved_at"):
                    raise ValueError("این سفارش قابل رد کردن نیست.")
                await send(update, f"❌ <b>رد فیش سفارش #{order_id}</b>\n\nکاربر از رد پرداخت مطلع می‌شود.", [[button("❌ بله، رد پرداخت", f"adm:rejectok:{order_id}")], _back(f"adm:order:{order_id}", "↩️ انصراف")])
            else:
                order = await _store(ctx).get_order(order_id)
                if not order or order["method"] != "card" or not await _store(ctx).reject_order(order_id):
                    raise ValueError("وضعیت سفارش تغییر کرده است و قابل رد کردن نیست.")
                try:
                    await ctx.bot.send_message(chat_id=order["user_id"], text=f"❌ فیش سفارش #{order_id} تأیید نشد. برای پیگیری از بخش پشتیبانی ربات اقدام کنید.")
                except TelegramError:
                    await _order(update, ctx, order_id, "پرداخت رد شد؛ ارسال اعلان به کاربر ناموفق بود.")
                else:
                    await _order(update, ctx, order_id, "پرداخت رد شد و به کاربر اطلاع دادیم.")
        elif action == "user" and len(parts) == 3:
            await _user(update, ctx, _integer(parts[2], 1, 9_223_372_036_854_775_807))
        elif action == "block" and len(parts) == 4:
            user_id = _integer(parts[2], 1, 9_223_372_036_854_775_807)
            blocked = _integer(parts[3], 0, 1)
            if user_id in admin_ids(ctx.bot_data["settings"]):
                raise ValueError("مدیر ربات را نمی‌توان مسدود کرد.")
            if not await _store(ctx).get_user(user_id):
                raise ValueError("کاربر پیدا نشد.")
            await _store(ctx).set_user_blocked(user_id, bool(blocked))
            await _user(update, ctx, user_id)
        elif action == "finduser" and len(parts) == 2:
            ctx.user_data["admin_state"] = {"kind": "find_user"}
            await send(update, "🔎 آیدی عددی تلگرام کاربر را بفرستید.\nبرای لغو /cancel را بفرستید.", [_back("adm:users:0")])
        elif action == "admins" and len(parts) == 2:
            await _admins(update, ctx)
        elif action == "addadmin" and len(parts) == 2:
            _admins_file(ctx)
            ctx.user_data["admin_state"] = {"kind": "add_admin"}
            await send(update, "➕ <b>افزودن ادمین</b>\n\nChat ID عددی ادمین جدید را بفرست.\n"
                "آن شخص لازم نیست از قبل کاربر ربات باشد، ولی برای استفاده باید /start را بزند.\n\nلغو: /cancel", [_back("adm:admins", "✖️ انصراف")])
        elif action == "deladmin" and len(parts) == 3:
            admin_id = _integer(parts[2], 1, 9_223_372_036_854_775_807)
            if admin_id == update.effective_user.id:
                raise ValueError("برای جلوگیری از قفل شدن پنل، نمی‌توانی خودت را حذف کنی.")
            if admin_id not in admin_ids(ctx.bot_data["settings"]):
                raise ValueError("این ادمین دیگر در فهرست نیست.")
            await send(update, f"🗑 <b>حذف ادمین</b>\n\nادمین <code>{admin_id}</code> حذف شود؟", [
                [button("🗑 بله، حذف ادمین", f"adm:deladminok:{admin_id}")], _back("adm:admins", "↩️ انصراف")])
        elif action == "deladminok" and len(parts) == 3:
            admin_id = _integer(parts[2], 1, 9_223_372_036_854_775_807)
            update_admins(_admins_file(ctx), remove=admin_id, actor=update.effective_user.id)
            await _admins(update, ctx, f"✅ ادمین {admin_id} حذف شد.")
        elif action == "stats" and len(parts) == 2:
            stats = await _store(ctx).get_stats()
            await send(update, "📊 <b>آمار ربات</b>\n\n"
                f"کاربران: {stats['users']}\nسفارش‌ها: {stats['orders']}\nنیازمند بررسی: {stats['pending_orders']}\n"
                f"سرویس‌های تحویل‌شده: {stats['services']}\nفروش: {money(stats['revenue'])}\n"
                f"مجموع موجودی کیف پول‌ها: {money(stats['balance_total'])}",
                [[button("🔄 تازه‌سازی", "adm:stats")], _back()])
        elif action in ("settings", "tutorials") and len(parts) == 2:
            await _settings(update, ctx, action == "tutorials")
        elif action == "setting" and len(parts) == 3 and parts[2] in _SETTINGS:
            key = parts[2]
            current = await runtime(ctx)
            ctx.user_data["admin_state"] = {"kind": "setting", "key": key}
            previous = str(current.get(key, ""))
            await send(update, f"✏️ <b>{_SETTINGS[key][0]}</b>\n\nمتن فعلی:\n{esc(previous[:1800]) or 'تنظیم نشده'}\n\n"
                f"{esc(_SETTINGS[key][1])}\nبرای لغو /cancel را بفرستید.", [_back("adm:tutorials" if key.startswith("tutorial_") else "adm:settings", "✖️ انصراف")])
        else:
            await send(update, "این دکمه قدیمی یا نامعتبر است. از پنل مدیریت ادامه دهید.", [_back()])
    except (ValueError, KeyError, IndexError) as error:
        # Malformed callbacks must not escape into the global error handler.
        message = str(error) if isinstance(error, ValueError) and str(error) and not str(error).startswith("invalid literal") else "این درخواست قدیمی یا نامعتبر است؛ دوباره از پنل شروع کنید."
        await send(update, f"⚠️ {esc(message)}", [_back()])
    except TelegramError:
        await send(update, "⚠️ ارتباط با تلگرام کامل نشد. وضعیت سفارش را بررسی کنید و در صورت نیاز دوباره تلاش کنید.", [_back("adm:orders:0")])
    return True


async def text(update, ctx) -> bool:
    """Consume text only while this administrator is in an input workflow."""
    state = ctx.user_data.get("admin_state")
    if not state:
        return False
    if not await _authorized(update, ctx):
        return True
    message = update.effective_message
    value = (message.text or "").strip() if message else ""
    if value.split("@", 1)[0] == "/cancel":
        ctx.user_data.pop("admin_state", None)
        await menu(update, ctx)
        return True
    if value.startswith("/"):
        return False
    try:
        kind = state["kind"]
        if kind == "new_plan":
            field = state["field"]
            if field in ("mode", "review"):
                await send(update, "👇 برای ادامه از دکمه‌های فرم استفاده کنید یا /cancel را بفرستید.", _mode_rows(ctx, "adm:newmode", state["plan"]["protocol"]) if field == "mode" else [[button("✅ ثبت پلن", "adm:create")]])
                return True
            state["plan"][field] = _plan_value(field, value)
            next_field = {"name": "price", "price": "traffic_gb", "traffic_gb": "mode", "inbound_id": "description", "description": "review"}[field]
            state["field"] = next_field
            if next_field == "mode":
                ctx.user_data["admin_state"] = state
                await send(update, "📤 روش تحویل را انتخاب کنید.\nاگر پنل ندارید، «ارسال دستی بعد از تأیید» را بزنید.", _mode_rows(ctx, "adm:newmode", state["plan"]["protocol"]) + [_back(f"adm:plans:{state['plan']['months']}:0", "✖️ انصراف")])
            elif next_field == "review":
                await _new_review(update, ctx, state)
            else:
                await _ask_plan_field(update, ctx, state)
        elif kind == "edit_plan":
            plan = await _store(ctx).get_plan(state["plan_id"])
            if not plan or plan.get("archived"):
                ctx.user_data.pop("admin_state", None)
                raise ValueError("این پلن دیگر در دسترس نیست.")
            plan[state["field"]] = _plan_value(state["field"], value)
            if state.get("set_mode"):
                _require_mode(ctx, state["set_mode"])
                plan["mode"] = state["set_mode"]
            await _store(ctx).save_plan(plan)
            ctx.user_data.pop("admin_state", None)
            await _plan(update, ctx, plan["id"])
        elif kind == "inventory":
            plan = await _store(ctx).get_plan(state["plan_id"])
            if not plan or plan.get("archived") or plan["mode"] != "inventory":
                ctx.user_data.pop("admin_state", None)
                raise ValueError("این پلن برای افزودن موجودی در دسترس نیست.")
            configs = _config_lines(value, inventory=True, protocol=plan["protocol"])
            added = await _store(ctx).add_inventory(plan["id"], configs)
            count = await _store(ctx).inventory_count(plan["id"])
            ctx.user_data.pop("admin_state", None)
            await send(update, f"✅ {added} لینک جدید اضافه شد.\nموجودی آماده فروش: {count} عدد\nلینک‌های تکراری دوباره ثبت نمی‌شوند.", [[button("➕ افزودن بیشتر", f"adm:stock:{plan['id']}")], _back(f"adm:plan:{plan['id']}")])
        elif kind == "manual_config":
            from purchase import deliver_manual
            order = await _store(ctx).get_order(state["order_id"])
            configs = _config_lines(value, protocol=order["plan_snapshot"].get("protocol", "v2ray"))
            await deliver_manual(ctx, state["order_id"], "\n".join(configs))
            ctx.user_data.pop("admin_state", None)
            order = await _store(ctx).get_order(state["order_id"])
            notice = "✅ کانفیگ این سفارش ثبت و برای خریدار ارسال شد." if order.get("delivered_at") else "✅ کانفیگ این سفارش ثبت شد؛ ارسال پیام به خریدار در صف پیگیری قرار گرفت."
            await _order(update, ctx, state["order_id"], notice)
        elif kind == "setting":
            key = state["key"]
            if key not in _SETTINGS:
                raise ValueError("تنظیم نامعتبر است.")
            await _store(ctx).set_setting(key, _setting_value(key, value))
            ctx.user_data.pop("admin_state", None)
            await _settings(update, ctx, key.startswith("tutorial_"))
        elif kind == "find_user":
            user_id = _integer(value, 1, 9_223_372_036_854_775_807)
            await _user(update, ctx, user_id)
            ctx.user_data.pop("admin_state", None)
        elif kind == "add_admin":
            admin_id = _integer(value, 1, 9_223_372_036_854_775_807)
            update_admins(_admins_file(ctx), add=admin_id)
            ctx.user_data.pop("admin_state", None)
            await _admins(update, ctx, f"✅ ادمین {admin_id} اضافه شد.")
        else:
            ctx.user_data.pop("admin_state", None)
            raise ValueError("فرم منقضی شده است؛ از پنل مدیریت دوباره شروع کنید.")
    except ValueError as error:
        await send(update, f"⚠️ {esc(str(error))}\n\nدوباره بفرستید یا با /cancel خارج شوید.", [_back()])
    except TelegramError:
        ctx.user_data.pop("admin_state", None)
        await send(update, "⚠️ ارسال پیام کامل نشد. وضعیت سفارش را بررسی کنید؛ از همان سفارش می‌توانید تحویل را دوباره ارسال کنید.", [_back("adm:orders:0")])
    return True


async def document(update, ctx):
    state = ctx.user_data.get("admin_state", {})
    if state.get("kind") != "manual_config":
        return False
    if not await _authorized(update, ctx):
        return True
    from purchase import deliver_manual
    order = await _store(ctx).get_order(state["order_id"])
    protocol = order.get("plan_snapshot", {}).get("protocol", "v2ray") if order else "v2ray"
    document = update.effective_message.document
    name = str(document.file_name or "")
    extension = ".conf" if protocol == "wireguard" else ".ovpn" if protocol == "openvpn" else ""
    if not extension:
        raise ValueError("برای کانفیگ عادی، لینک سابسکریپشن http/https را به‌صورت متن بفرست.")
    if not name.lower().endswith(extension):
        raise ValueError(f"برای این سفارش فایل {extension} بفرست.")
    if document.file_size and document.file_size > 2_000_000:
        raise ValueError("حجم فایل کانفیگ باید کمتر از ۲ مگابایت باشد.")
    await deliver_manual(ctx, state["order_id"], document_file_id=document.file_id, document_name=name)
    ctx.user_data.pop("admin_state", None)
    await _order(update, ctx, state["order_id"], "✅ فایل کانفیگ ثبت و برای خریدار ارسال شد.")
    return True
