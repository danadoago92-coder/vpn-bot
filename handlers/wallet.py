"""Wallet balance and topup requests."""
from __future__ import annotations

from card_rotation import configured_cards
from ui import button, money, send
from handlers.common import BACK
from handlers.buy import request_key, order_detail


async def wallet(update, ctx):
    user = await ctx.bot_data["store"].get_user(update.effective_user.id)
    return await send(update, f"🏦 <b>کیف پول</b>\n\nموجودی: <b>{money(user['balance'])}</b>\nمبلغ شارژ را انتخاب کنید:", [
        [button("💰 ۱۰۰٬۰۰۰ تومان", "topup:100000"), button("💰 ۲۰۰٬۰۰۰ تومان", "topup:200000")],
        [button("💰 ۵۰۰٬۰۰۰ تومان", "topup:500000"), button("✏️ مبلغ دلخواه", "topup_custom")],
    ])


async def topup(update, ctx, amount):
    if not 10000 <= amount <= 100000000:
        raise ValueError("مبلغ شارژ باید بین ۱۰٬۰۰۰ و ۱۰۰٬۰۰۰٬۰۰۰ تومان باشد.")
    if not await configured_cards(ctx):
        raise ValueError("اطلاعات پرداخت هنوز تنظیم نشده است.")
    order = await ctx.bot_data["store"].create_order(update.effective_user.id, amount=amount, kind="topup",
        request_key=request_key(update, f"topup:{amount}"))
    ctx.user_data.pop("customer_state", None)
    ctx.user_data["receipt_order"] = order["id"]
    return await order_detail(update, ctx, order["id"])
