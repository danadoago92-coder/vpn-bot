"""Decorative stickers reused from the original bot."""
from __future__ import annotations

import asyncio
import logging
from ui import keep_message, track_transient

log = logging.getLogger(__name__)

STICKERS = {
    "start": "CAACAgIAAxUAAWqImwABHPc-btdLOblE6qj5kjRa8wACnhYAAqmhoUl3vDZi5x3Vyz0E",
    "buy": "CAACAgIAAxUAAWqImwABsmHCtsTLrbedfxMiERmUQwAC2hkAAl60sUocVED83-8UGz0E",
    "service": "CAACAgIAAxUAAWqImwAB11jpVWbzEDgevK-ZTQye0QACfhcAAiMGoUkUavOSzNsnlz0E",
    "wallet": "CAACAgIAAxUAAWqImwABdXHZLlIsDLKz8w0d6u5I8gACZxMAAtdGqUkL_0Rqpmqx2z0E",
    "tutorial": "CAACAgIAAxUAAWqImwABwNBAGQIpCxvqcb84Sp2xKwACwiQAAt-2eEuBossapGngyD0E",
    "support": "CAACAgIAAxUAAWqImwABXMEY4PTTBmozb7jP-LdmIQAClxQAAjVKIUoiKRGTJezBSD0E",
}


async def show_sticker(update, ctx, context):
    """Replace the previous menu sticker so decorative messages do not pile up."""
    previous = ctx.user_data.pop("menu_sticker", None)
    if previous:
        keep_message(ctx.bot, type("MessageRef", (), {"chat_id": previous[0], "message_id": previous[1]})())
        try:
            await ctx.bot.delete_message(chat_id=previous[0], message_id=previous[1])
        except Exception:
            pass
    try:
        sent = await ctx.bot.send_sticker(
            chat_id=update.effective_chat.id,
            sticker=STICKERS.get(context, STICKERS["start"]),
        )
        ctx.user_data["menu_sticker"] = (sent.chat_id, sent.message_id)
        track_transient(ctx.bot, sent)
        await asyncio.sleep(0.25)
    except Exception:
        log.info("Sticker unavailable for context=%s", context)
