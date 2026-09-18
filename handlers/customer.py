"""Customer routing and receipt collection."""
from __future__ import annotations

from telegram import InlineKeyboardMarkup
from telegram.error import TelegramError
from admins import admin_ids
from ui import button, esc, money, is_admin, runtime, send
from purchase import fulfill_and_notify
from handlers.common import BACK, TUTORIALS
from handlers.start import home, tutorial
from handlers.buy import volume, locations, protocols, catalog, plan_detail, request_key, checkout, order_detail, orders
from handlers.my_subs import expiry_label, services, owned_service, service_detail, download
from handlers.wallet import wallet, topup
from stickers import show_sticker


async def callback(update, ctx, data=None):
    data = data or update.callback_query.data
    bits = data.split(":")
    action = bits[0]
    if action in ("home", "main_menu"):
        return await home(update, ctx)
    if action == "buy":
        await show_sticker(update, ctx, "buy")
        return await locations(update, ctx)
    if action == "renew":
        await show_sticker(update, ctx, "service")
        return await locations(update, ctx)
    if action == "locations":
        return await locations(update, ctx)
    if action == "location" and len(bits) == 2:
        return await protocols(update, ctx, bits[1])
    if action == "multi":
        return await protocols(update, ctx, "multi")
    if action == "protocol" and len(bits)==3:
        return await catalog(update,ctx,location_id=bits[1],protocol=bits[2])
    if action == "duration" and len(bits)==5 and bits[3] in ("1","2"):
        return await catalog(update,ctx,int(bits[3]),int(bits[4]),bits[1],bits[2])
    if action == "months" and len(bits) in (2,3) and bits[1] in ("1", "2"):
        return await catalog(update, ctx, int(bits[1]), int(bits[2]) if len(bits)==3 else 0)
    if action == "plan" and len(bits)==2:
        return await plan_detail(update, ctx, int(bits[1]))
    if action == "checkout" and len(bits)==4 and bits[1] in ("card", "wallet"):
        return await checkout(update, ctx, bits[1], int(bits[2]),bits[3])
    if action == "walletpay" and len(bits)==2:
        order = await ctx.bot_data["store"].get_order(int(bits[1]))
        if not order or order["user_id"] != update.effective_user.id or order["method"] != "wallet" or order["status"] != "pending":
            raise ValueError("این سفارش قابل پرداخت نیست؛ وضعیت سفارش را بررسی کنید.")
        await fulfill_and_notify(ctx, order["id"], from_wallet=True)
        return await order_detail(update, ctx, order["id"])
    if action == "order" and len(bits)==2:
        return await order_detail(update, ctx, int(bits[1]))
    if action == "orders":
        return await orders(update, ctx)
    if action == "receipt" and len(bits)==2:
        order = await ctx.bot_data["store"].get_order(int(bits[1]))
        if not order or order["user_id"] != update.effective_user.id or order["status"] != "pending" or order["method"] != "card":
            raise ValueError("این سفارش در مرحلهٔ دریافت رسید نیست.")
        ctx.user_data["receipt_order"] = order["id"]
        return await send(update, f"📷 عکس رسید سفارش #{order['id']} رو همین‌جا بفرست.\nلغو: /cancel")
    if action == "cancel" and len(bits)==2:
        order_id = int(bits[1])
        if not await ctx.bot_data["store"].cancel_order(order_id, update.effective_user.id):
            raise ValueError("این سفارش دیگر قابل لغو نیست.")
        ctx.user_data.pop("receipt_order", None)
        return await order_detail(update, ctx, order_id)
    if action == "wallet":
        await show_sticker(update, ctx, "wallet")
        return await wallet(update, ctx)
    if action == "topup" and len(bits)==2:
        return await topup(update, ctx, int(bits[1]))
    if action == "topup_custom":
        ctx.user_data["customer_state"] = "topup"
        return await send(update, "✏️ مبلغ شارژ دلخواه را به تومان و فقط با عدد بفرستید.\nانصراف: /cancel", BACK)
    if action == "services" and len(bits) in (1,2):
        if len(bits) == 1:
            await show_sticker(update, ctx, "service")
        return await services(update, ctx, int(bits[1]) if len(bits)==2 else 0)
    if action in ("service", "download", "qr") and len(bits)==2:
        return await service_detail(update, ctx, int(bits[1])) if action == "service" else await download(update, ctx, int(bits[1]), action == "qr")
    if action == "tutorial" and len(bits) in (1,2):
        return await tutorial(update, ctx, bits[1] if len(bits)==2 else None)
    if action == "support":
        await show_sticker(update, ctx, "support")
        values = await runtime(ctx)
        rows = []
        if values.get("support_username"):
            rows.append([button("💬 گفتگو با پشتیبانی", url="https://t.me/"+values["support_username"].lstrip("@"))])
        if values.get("channel_username"):
            rows.append([button("📣 کانال اطلاع‌رسانی", url="https://t.me/"+values["channel_username"].lstrip("@"))])
        text = "☎️ <b>پشتیبانی</b>\n\nبرای پیگیری، شماره سفارش و توضیح مشکل را ارسال کنید." if rows else "اطلاعات پشتیبانی هنوز توسط ادمین تنظیم نشده است."
        return await send(update, text, rows or None)
    if action == "profile":
        user = await ctx.bot_data["store"].get_user(update.effective_user.id)
        return await send(update, f"👤 <b>{esc(user['full_name'])}</b>\n\nشناسه: <code>{user['id']}</code>\nموجودی: {money(user['balance'])}\nعضویت: {esc(user['created_at'][:10])}", [[button("🛍 سرویس‌های من", "services"), button("🧾 سفارش‌ها", "orders")], *BACK])
    return await home(update, ctx)


async def text(update, ctx):
    value = update.effective_message.text.strip()
    if ctx.user_data.get("customer_state") == "topup":
        try:
            amount = int(value.replace(",", "").replace("٬", ""))
        except ValueError:
            raise ValueError("مبلغ را فقط با عدد به تومان وارد کنید.") from None
        await topup(update, ctx, amount)
        return True
    if ctx.user_data.get("receipt_order"):
        await send(update, "📷 فقط عکس رسید رو بفرست؛ لغو: /cancel")
        return True
    return False


async def photo(update, ctx):
    order_id = ctx.user_data.get("receipt_order")
    if not order_id:
        return await send(update, "💳 اول یک خرید کارت‌به‌کارت شروع کن، بعد عکس رسید رو بفرست.")
    store = ctx.bot_data["store"]
    file_id = update.effective_message.photo[-1].file_id
    if not await store.attach_receipt(order_id, update.effective_user.id, file_id):
        ctx.user_data.pop("receipt_order", None)
        raise ValueError("این سفارش دیگر در مرحلهٔ دریافت رسید نیست.")
    ctx.user_data.pop("receipt_order", None)
    order = await store.get_order(order_id)
    caption = f"🧾 رسید سفارش #{order_id}\nکاربر: <code>{order['user_id']}</code>\nمبلغ: {money(order['amount'])}"
    rows = InlineKeyboardMarkup([[
        button("✅ تأیید", f"adm:approve:{order_id}"),
        button("❌ رد", f"adm:reject:{order_id}"),
    ]])
    for admin_id in admin_ids(ctx.bot_data["settings"]):
        try:
            await ctx.bot.send_photo(admin_id, photo=file_id, caption=caption, parse_mode="HTML", reply_markup=rows)
        except TelegramError:
            pass
    await send(update, f"✅ رسید #{order_id} ثبت شد؛ بعد از تأیید، لینک اشتراکت همین‌جا میاد.")
