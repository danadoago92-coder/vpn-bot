"""Main menu and connection guides."""
from __future__ import annotations

from ui import button, delete_transient, esc, is_admin, reply_menu, remove_inline_menu, render_template, runtime, send, track_transient
from handlers.common import BACK, TUTORIALS
from stickers import show_sticker


async def home(update, ctx, fresh=False):
    previous_sticker = ctx.user_data.get("menu_sticker")
    ctx.user_data.clear()
    if previous_sticker:
        ctx.user_data["menu_sticker"] = previous_sticker
    values = await runtime(ctx)
    text = "👋 " + render_template(values["welcome_text"], brand=values["brand_name"], name=update.effective_user.first_name or "دوست")
    text += "\n\nاز کیبورد پایین یکی رو انتخاب کن 👇"
    # Returning from an inline section must remove its old text; otherwise the
    # new home sticker and message accumulate underneath it.
    await remove_inline_menu(update, delete_message=True, bot=ctx.bot)
    await delete_transient(ctx.bot, update.effective_chat.id)
    await show_sticker(update, ctx, "start")
    sent = await ctx.bot.send_message(
        chat_id=update.effective_chat.id,
        text=text,
        reply_markup=reply_menu(is_admin(update, ctx)),
    )
    track_transient(ctx.bot, sent)
    return sent


async def tutorial(update, ctx, key=None):
    if key is None:
        # Show the decoration only when entering from the reply keyboard or a
        # command. The inline "آموزش‌ها" back button edits the current text and
        # must not append a fresh sticker underneath the menu.
        if not update.callback_query:
            await show_sticker(update, ctx, "tutorial")
        return await send(update, "📚 <b>آموزش اتصال لینک اشتراک</b>\n\nدستگاه خود را انتخاب کنید:", [
            [button("📱 اندروید", "tutorial:android"), button("🍎 آیفون", "tutorial:ios")],
            [button("💻 ویندوز و دسکتاپ", "tutorial:windows")], [button("🛠 رفع مشکل اتصال", "tutorial:help")], *BACK])
    if key not in TUTORIALS:
        raise ValueError("آموزش پیدا نشد.")
    values = await runtime(ctx)
    text = values.get("tutorial_"+key) or TUTORIALS[key]
    buttons = []
    if key in ("android","ios","windows"):
        buttons.append([button("🌐 صفحهٔ رسمی Hiddify", url="https://github.com/hiddify/hiddify-app#readme")])
    buttons.extend([[button("🔙 آموزش‌ها", "tutorial"), button("☎️ پشتیبانی", "support")]])
    return await send(update, esc(text), buttons)
