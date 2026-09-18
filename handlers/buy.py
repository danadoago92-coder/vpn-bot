"""Catalog, payment quotes and order history."""
from __future__ import annotations

import uuid
from card_rotation import configured_cards, format_card_number, get_next_card
from ui import button, esc, money, send
from purchase import fulfill_and_notify, notify_admins
from handlers.common import BACK, STATUSES

PROTOCOL_LABELS = {
    "v2ray": "🔗 کانفیگ عادی",
    "wireguard": "🛡 WireGuard",
    "openvpn": "🔐 OpenVPN",
}


def volume(plan):
    return "نامحدود" if not plan["traffic_gb"] else f"{plan['traffic_gb']:g} گیگابایت"


async def locations(update, ctx):
    store = ctx.bot_data["store"]
    available = []
    for item in await store.list_locations(active_only=True):
        if await store.location_protocols(item["id"], active_only=True):
            available.append(item)
    if not available:
        return await send(update, "🛒 <b>خرید اشتراک</b>\n\nهنوز سرویسی برای فروش تنظیم نشده؛ کمی بعد دوباره سر بزن.", BACK)
    rows = [[button(item["name"], f"location:{item['id']}")] for item in available]
    rows.extend(BACK)
    return await send(update, "🛒 <b>خرید اشتراک</b>\n\nلوکیشن سرویس رو انتخاب کن:", rows)


async def protocols(update, ctx, location_id):
    store = ctx.bot_data["store"]
    location = next((item for item in await store.list_locations(active_only=True) if item["id"] == location_id), None)
    if not location:
        raise ValueError("این لوکیشن فعلاً فعال نیست.")
    choices = await store.location_protocols(location_id, active_only=True)
    if not choices:
        raise ValueError("برای این لوکیشن هنوز پروتکلی فعال نشده است.")
    rows = [[button(PROTOCOL_LABELS[item["protocol"]], f"protocol:{location_id}:{item['protocol']}")]
            for item in choices if item["protocol"] in PROTOCOL_LABELS]
    rows.append([button("🔙 انتخاب لوکیشن", "locations")])
    return await send(update, f"<b>{esc(location['name'])}</b>\n\nنوع کانفیگ رو انتخاب کن:", rows)


async def catalog(update, ctx, months=None, page=0, location_id=None, protocol=None):
    location_id = location_id or ctx.user_data.get("location_id")
    protocol = protocol or ctx.user_data.get("protocol", "v2ray")
    if not location_id or protocol not in PROTOCOL_LABELS:
        raise ValueError("لوکیشن یا پروتکل معتبر نیست.")
    locations = await ctx.bot_data["store"].list_locations(active_only=True)
    location = next((x for x in locations if x["id"]==location_id),None)
    if not location:
        raise ValueError("این لوکیشن فعلاً فعال نیست.")
    enabled = {item["protocol"] for item in await ctx.bot_data["store"].location_protocols(location_id, active_only=True)}
    if protocol not in enabled:
        raise ValueError("این پروتکل برای لوکیشن انتخابی فعال نیست.")
    ctx.user_data.update(location_id=location_id,protocol=protocol)
    if months is None:
        return await send(update, f"<b>{esc(location['name'])}</b>\n\nمدت سرویس را انتخاب کن:", [
            [button("🗓 یک‌ماهه · ۳۰ روز", f"duration:{location_id}:{protocol}:1:0"), button("🗓 دوماهه · ۶۰ روز", f"duration:{location_id}:{protocol}:2:0")],
            [button("🔙 نوع کانفیگ", f"location:{location_id}")]])
    all_plans = await ctx.bot_data["store"].list_plans(months=months,location_id=location_id,protocol=protocol)
    page = max(0, min(page, max(0, (len(all_plans)-1)//8)))
    plans = all_plans[page*8:page*8+8]
    rows = [[button(f"{p['name']} · {money(p['price'])}", f"plan:{p['id']}")] for p in plans]
    nav = []
    if page:
        nav.append(button("◀️ قبلی", f"duration:{location_id}:{protocol}:{months}:{page-1}"))
    if (page+1)*8 < len(all_plans):
        nav.append(button("بعدی ▶️", f"duration:{location_id}:{protocol}:{months}:{page+1}"))
    if nav:
        rows.append(nav)
    rows.append([button("🔙 انتخاب مدت", f"protocol:{location_id}:{protocol}")])
    return await send(update, f"🌍 <b>پلن‌های {'یک' if months == 1 else 'دو'}‌ماهه</b>\n\n" + ("پلن دلخواهتان را انتخاب کنید." if plans else "در حال حاضر پلن فعالی در این بخش نیست. به‌زودی سر بزنید."), rows)


async def plan_detail(update, ctx, plan_id):
    store = ctx.bot_data["store"]
    plan = await store.get_plan(plan_id)
    if not plan or not plan["active"]:
        raise ValueError("این پلن فعلاً قابل خرید نیست.")
    ctx.user_data.pop("receipt_order", None)
    text = (f"🌍 <b>{esc(plan['name'])}</b>\n\n"
        f"مدت: {plan['months']} ماه ({plan['days']} روز)\nحجم: {volume(plan)}\n"
        f"مبلغ: <b>{money(plan['price'])}</b>\n")
    if plan.get("description"):
        text += "\n" + esc(plan["description"]) + "\n"
    ctx.user_data.update(location_id=plan.get("location_id", ""), protocol=plan.get("protocol", "v2ray"))
    nonce = uuid.uuid4().hex[:12]
    quotes = ctx.user_data.setdefault("checkout_quotes", {})
    if len(quotes) >= 20:
        quotes.pop(next(iter(quotes)))
    quotes[nonce] = {"plan_id":plan_id,"price":plan["price"]}
    rows = []
    if plan["mode"] == "inventory" and not await store.inventory_count(plan_id):
        text += "\n⏳ موجودی این پلن تمام شده است."
    else:
        text += "\nپس از تأیید پرداخت، کانفیگ در ربات تحویل داده می‌شود."
        rows = [[button("💳 پرداخت کارت‌به‌کارت", f"checkout:card:{plan_id}:{nonce}")],
                [button("🏦 پرداخت از کیف پول", f"checkout:wallet:{plan_id}:{nonce}")]]
    rows.append([button("🔙 پلن‌ها", f"duration:{plan.get('location_id', '')}:{plan.get('protocol', 'v2ray')}:{plan['months']}:0")])
    return await send(update, text, rows)


def request_key(update, purpose):
    msg = update.callback_query.message if update.callback_query else update.effective_message
    return f"{update.effective_user.id}:{msg.chat_id}:{msg.message_id}:{purpose}"


async def checkout(update, ctx, method, plan_id, nonce):
    store = ctx.bot_data["store"]
    quote = ctx.user_data.get("checkout_quotes",{}).get(nonce)
    if not quote or quote["plan_id"] != plan_id:
        raise ValueError("این صفحهٔ پرداخت قدیمی است؛ پلن را دوباره باز کن.")
    if method == "card":
        if not await configured_cards(ctx):
            raise ValueError("اطلاعات پرداخت هنوز تنظیم نشده است؛ به پشتیبانی پیام بدهید.")
    order = await store.create_order(update.effective_user.id, plan_id=plan_id, method=method,
        request_key=f"purchase:{update.effective_user.id}:{nonce}",expected_price=quote["price"])
    if method == "card":
        ctx.user_data["receipt_order"] = order["id"]
    if method == "wallet" and order["method"] == "wallet" and order["status"] == "pending":
        try:
            await fulfill_and_notify(ctx, order["id"], from_wallet=True)
        except ValueError:
            await notify_admins(ctx, f"⚠️ سفارش #{order['id']} نیازمند بررسی است.", [[button("🔍 بررسی سفارش", f"adm:order:{order['id']}")]])
            raise
    return await order_detail(update, ctx, order["id"])


async def order_detail(update, ctx, order_id):
    store = ctx.bot_data["store"]
    order = await store.get_order(order_id)
    if not order or order["user_id"] != update.effective_user.id:
        raise ValueError("سفارش پیدا نشد.")
    rows = []
    text = f"🧾 <b>سفارش #{order_id}</b>\n\nمبلغ: {money(order['amount'])}\nوضعیت: {STATUSES.get(order['status'], 'در حال بررسی')}"
    if order["kind"] == "purchase":
        text += "\nپلن: " + esc(order["plan_snapshot"]["name"])
    else:
        text += "\nنوع: شارژ کیف پول"
    if order["status"] == "pending" and order["method"] == "card":
        ctx.user_data["receipt_order"] = order_id
        values = await get_next_card(store, order_id, await configured_cards(ctx))
        text = (f"💳 <b>پرداخت سفارش #{order_id}</b>\n\n"
            f"💰 مبلغ: <b>{money(order['amount'])}</b>\n\n"
            f"🏦 شماره کارت:\n<code>{esc(format_card_number(values['number']))}</code>\n"
            f"👤 به نام: {esc(values['holder'])}\n\n"
            "📷 بعد از واریز، عکس رسید رو همین‌جا بفرست.")
        rows.append([button("📋 کپی شماره کارت", copy_text=values["number"])])
        rows.append([button("❌ لغو سفارش", f"cancel:{order_id}")])
    elif order["status"] == "pending" and order["method"] == "wallet":
        rows.append([button("🏦 تکمیل پرداخت کیف پول", f"walletpay:{order_id}")])
        rows.append([button("❌ لغو سفارش", f"cancel:{order_id}")])
    elif order["status"] == "paid" and order["kind"] == "purchase":
        service = await store.service_for_order(order_id)
        if service:
            rows.append([button("🔗 مشاهده کانفیگ", f"service:{service['id']}")])
    elif order["status"] in ("failed", "awaiting_config"):
        text += "\n\nسفارش شما ثبت شده؛ ادمین آن را پیگیری می‌کند. برای این سفارش دوباره پرداخت نکنید."
        rows.append([button("☎️ پشتیبانی", "support")])
    if order["status"] not in ("pending", "paid", "cancelled", "rejected"):
        rows.append([button("🔄 به‌روزرسانی وضعیت", f"order:{order_id}")])
    return await send(update, text, rows, keep=True)


async def orders(update, ctx):
    rows = await ctx.bot_data["store"].user_orders(update.effective_user.id, limit=30)
    buttons = [[button(f"#{o['id']} · {STATUSES.get(o['status'], '')} · {money(o['amount'])}", f"order:{o['id']}")] for o in rows]
    return await send(update, "🧾 <b>پیگیری سفارش‌ها</b>\n\n" + ("سفارش موردنظر رو انتخاب کن." if rows else "سفارشی در انتظار نداری."), buttons or None)
