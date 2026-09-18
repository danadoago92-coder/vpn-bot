"""Owned subscription-link details."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from io import BytesIO
from ui import button, esc, send
from handlers.common import BACK


def expiry_label(service):
    value = service.get("expires_at")
    if not value:
        return "طبق تنظیم سرویس"
    try:
        expiry = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        remaining = (expiry-datetime.now(timezone.utc)).total_seconds()
        return f"{str(value)[:10]} · " + (f"حدود {max(1, int(remaining/86400))} روز باقی‌مانده" if remaining > 0 else "تاریخ ثبت‌شده گذشته است")
    except (ValueError, TypeError):
        return str(value)[:30]


async def services(update, ctx, page=0):
    page = max(0, page)
    rows = await ctx.bot_data["store"].list_services(update.effective_user.id, limit=9, offset=page*8)
    buttons = [[button(f"#{s['id']} · {s['plan_name']}", f"service:{s['id']}")] for s in rows[:8]]
    nav = []
    if page:
        nav.append(button("◀️ قبلی", f"services:{page-1}"))
    if len(rows) > 8:
        nav.append(button("بعدی ▶️", f"services:{page+1}"))
    if nav:
        buttons.append(nav)
    if not rows:
        return await send(update, "🛍 <b>سرویس‌های من</b>\n\nهنوز سرویسی نداری. از دکمه «خرید اشتراک» در کیبورد پایین شروع کن.")
    return await send(update, "🛍 <b>سرویس‌های من</b>\n\nسرویست رو انتخاب کن:", buttons)


async def owned_service(update, ctx, service_id):
    service = await ctx.bot_data["store"].get_service(service_id, user_id=update.effective_user.id)
    if not service:
        raise ValueError("سرویس پیدا نشد.")
    return service


async def service_detail(update, ctx, service_id):
    service = await owned_service(update, ctx, service_id)
    labels = {"v2ray": "🔗 کانفیگ عادی", "wireguard": "🛡 WireGuard", "openvpn": "🔐 OpenVPN"}
    protocol = service.get("protocol", "v2ray")
    text = (f"🌍 <b>{esc(service['plan_name'])}</b>\n\nشناسه سرویس: #{service_id}\nسفارش: #{service['order_id']}\n"
        f"نوع کانفیگ: {labels.get(protocol, protocol)}\n"
        f"حجم ثبت‌شده: {'نامحدود' if not service['traffic_gb'] else str(service['traffic_gb'])+' گیگابایت'}\n"
        f"اعتبار ثبت‌شده: {esc(expiry_label(service))}\n\n")
    if protocol == "v2ray" and service["config_text"] and len(service["config_text"]) < 2300:
        text += "🔗 <b>لینک اشتراک</b>\n<code>" + esc(service["config_text"]) + "</code>\n\n"
    text += "لینک را داخل برنامه وارد کن." if protocol == "v2ray" else "فایل کانفیگ را دانلود و داخل برنامه همان پروتکل وارد کن."
    action = [button("🔳 نمایش QR", f"qr:{service_id}")] if protocol == "v2ray" else [button("📥 دریافت فایل کانفیگ", f"download:{service_id}")]
    return await send(update, text, [
        action,
        [button("🔙 سرویس‌های من", "services")],
    ], keep=True)


async def download(update, ctx, service_id, qr=False):
    service = await owned_service(update, ctx, service_id)
    protocol = service.get("protocol", "v2ray")
    if not qr and protocol in ("wireguard", "openvpn"):
        if service.get("document_file_id"):
            return await update.effective_message.reply_document(service["document_file_id"], caption=f"📥 کانفیگ سرویس #{service_id}")
        file = BytesIO(service["config_text"].encode("utf-8"))
        file.name = "wireguard.conf" if protocol == "wireguard" else "openvpn.ovpn"
        return await update.effective_message.reply_document(file, caption=f"📥 کانفیگ سرویس #{service_id}")
    if protocol != "v2ray":
        raise ValueError("نمایش QR فقط برای کانفیگ عادی است.")
    if len(service["config_text"].encode("utf-8")) > 1800:
        raise ValueError("این لینک برای QR طولانی است؛ خود لینک را کپی کن.")
    def make_qr():
        import qrcode
        file = BytesIO()
        qrcode.make(service["config_text"]).save(file, format="PNG")
        file.seek(0)
        return file
    file = await asyncio.to_thread(make_qr)
    await update.effective_message.reply_photo(file, caption=f"QR لینک اشتراک #{service_id}")
