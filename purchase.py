"""Order fulfillment and recoverable Telegram delivery."""
from __future__ import annotations

from admins import admin_ids

import asyncio
import logging
import weakref
from io import BytesIO
from functools import wraps
from telegram import InlineKeyboardMarkup
from telegram.error import TelegramError
from ui import button, esc, money, render_template, runtime

log = logging.getLogger(__name__)
DEFAULT_DELIVERY = "✅ {name} عزیز، سرویس شما در {brand} آماده شد.\n\nپلن: {plan}\nسفارش: #{order_id}\nاعتبار ثبت‌شده تا: {expires_at}\n\nاز بخش آموزش می‌توانید راهنمای اتصال را ببینید."


def serialized_delivery(function):
    @wraps(function)
    async def wrapper(ctx, item):
        key = item.get("order_id", item["id"])
        locks = ctx.bot_data.setdefault("delivery_locks", weakref.WeakValueDictionary())
        lock = locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            locks[key] = lock
        async with lock:
            return await function(ctx, item)
    return wrapper


async def notify_admins(ctx, text, rows=None):
    for admin_id in admin_ids(ctx.bot_data["settings"]):
        try:
            await ctx.bot.send_message(admin_id, text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows) if rows else None)
        except TelegramError as exc:
            log.warning("Admin notification deferred (%s)", type(exc).__name__)


@serialized_delivery
async def notify_service(ctx, service):
    store = ctx.bot_data["store"]
    order = await store.get_order(service["order_id"])
    if order.get("delivered_at"):
        return
    values = await runtime(ctx)
    user = await store.get_user(service["user_id"])
    message = render_template(values.get("delivery_template") or DEFAULT_DELIVERY,
        brand=values["brand_name"], name=(user or {}).get("full_name", "دوست"),
        plan=service["plan_name"], order_id=order["id"], expires_at=str(service.get("expires_at") or "طبق تنظیم سرویس")[:10])
    traffic = "نامحدود" if not service["traffic_gb"] else f"{service['traffic_gb']:g} گیگابایت"
    message += f"\n\n🗓 مدت: {service['months']} ماه\n📦 حجم: {traffic}"
    config = service["config_text"]
    protocol = service.get("protocol", "v2ray")
    rows = [
        [button("🛍 مشاهده سرویس", f"service:{service['id']}")],
        [button("📱 اندروید", "tutorial:android"), button("🍎 iOS", "tutorial:ios"), button("💻 ویندوز", "tutorial:windows")],
    ]
    # Store the service before delivery; an unavailable Telegram connection never loses the sale.
    if service.get("document_file_id"):
        await ctx.bot.send_document(service["user_id"], document=service["document_file_id"],
            caption=esc(message[:900]), parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows))
        await store.mark_delivered(order["id"])
        return
    if protocol in ("wireguard", "openvpn"):
        file = BytesIO(config.encode("utf-8"))
        file.name = "wireguard.conf" if protocol == "wireguard" else "openvpn.ovpn"
        await ctx.bot.send_document(service["user_id"], document=file, caption=esc(message[:900]),
            parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows))
        await store.mark_delivered(order["id"])
        return
    link_block = "🔗 <b>لینک اشتراک</b>\n<code>" + esc(config) + "</code>"
    if len(message) + len(config) < 3600:
        await ctx.bot.send_message(service["user_id"], esc(message) + "\n\n" + link_block,
            parse_mode="HTML", disable_web_page_preview=True, reply_markup=InlineKeyboardMarkup(rows))
    else:
        await ctx.bot.send_message(service["user_id"], esc(message), parse_mode="HTML")
        await ctx.bot.send_message(service["user_id"], link_block, parse_mode="HTML",
            disable_web_page_preview=True, reply_markup=InlineKeyboardMarkup(rows))
    await store.mark_delivered(order["id"])


async def fulfill_and_notify(ctx, order_id, from_wallet=False, notify_waiting=True):
    result = await ctx.bot_data["provisioner"].fulfill(order_id, from_wallet=from_wallet)
    if result.get("awaiting_config") and notify_waiting:
        order = await ctx.bot_data["store"].get_order(order_id)
        protocol = order.get("plan_snapshot", {}).get("protocol", "v2ray")
        what = "لینک اشتراک" if protocol == "v2ray" else "فایل کانفیگ"
        await notify_admins(ctx, f"📨 سفارش #{order_id} پرداخت شده و منتظر {what} است.\nکاربر: <code>{order['user_id']}</code>",
            [[button(f"📤 ارسال {what}", f"adm:order:{order_id}")]])
    if result.get("awaiting_config"):
        return result
    try:
        await notify_service(ctx, result)
    except TelegramError as exc:
        log.warning("Service delivery pending order=%s (%s)", order_id, type(exc).__name__)
    return result


async def deliver_manual(ctx, order_id, config_text="", **document):
    service = await ctx.bot_data["store"].complete_manual_order(order_id, config_text, **document)
    try:
        await notify_service(ctx, service)
    except TelegramError as exc:
        log.warning("Manual delivery pending order=%s (%s)", order_id, type(exc).__name__)
    return service


@serialized_delivery
async def notify_topup(ctx, order):
    order = await ctx.bot_data["store"].get_order(order["id"])
    if order.get("delivered_at"):
        return
    try:
        await ctx.bot.send_message(order["user_id"], f"✅ شارژ {money(order['amount'])} تأیید شد.\nشماره سفارش: #{order['id']}",
            reply_markup=InlineKeyboardMarkup([[button("🏦 کیف پول", "wallet")]]))
        await ctx.bot_data["store"].mark_delivered(order["id"])
    except TelegramError as exc:
        log.warning("Topup notification pending order=%s (%s)", order["id"], type(exc).__name__)


async def retry_deliveries(application):
    """Retry recorded deliveries. A crash after send may duplicate a message, never a charge."""
    from types import SimpleNamespace
    ctx = SimpleNamespace(bot=application.bot, bot_data=application.bot_data)
    while True:
        try:
            for order in await ctx.bot_data["store"].undelivered_orders():
                if order["kind"] == "topup":
                    await notify_topup(ctx, order)
                else:
                    service = await ctx.bot_data["store"].service_for_order(order["id"])
                    if service:
                        try:
                            await notify_service(ctx, service)
                        except TelegramError:
                            pass
                await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("Delivery retry failed (%s)", type(exc).__name__)
        await asyncio.sleep(60)
