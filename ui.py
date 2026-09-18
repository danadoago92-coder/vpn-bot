"""Shared Persian menus and safe Telegram rendering."""
from __future__ import annotations

import asyncio
import html
from telegram import CopyTextButton, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup
from telegram.error import BadRequest, TelegramError
from admins import admin_ids


AUTO_DELETE_SECONDS = 7 * 60
_transient_messages = {}
_cleanup_tasks = {}


def esc(value):
    return html.escape(str(value if value is not None else ""))


def money(value):
    return f"{int(value):,} تومان"


def button(label, data=None, url=None, copy_text=None):
    # Telegram's own theme is easier on the eyes than forced red/green/blue.
    copied = CopyTextButton(str(copy_text)) if copy_text is not None else None
    return InlineKeyboardButton(label, callback_data=data, url=url, copy_text=copied)


def is_admin(update, ctx):
    return bool(update.effective_user and update.effective_user.id in admin_ids(ctx.bot_data["settings"]))


async def runtime(ctx):
    settings = ctx.bot_data["settings"]
    keys = ("brand_name", "welcome_text", "card_number", "card_holder", "support_username", "channel_username", "delivery_template")
    result = {key: getattr(settings, key, "") for key in keys}
    result.update(await ctx.bot_data["store"].get_settings())
    return result


def render_template(template, **values):
    # Replace only literal documented placeholders, never evaluate format expressions.
    import re
    return re.sub(r"\{([a-z_]+)\}", lambda m: str(values.get(m.group(1), m.group(0))), template)


def main_rows(admin=False):
    rows = [
        [button("🔐 خرید اشتراک", "buy"), button("♻️ تمدید سرویس", "renew")],
        [button("🛍 سرویس‌های من", "services"), button("🏦 کیف پول", "wallet")],
        [button("📚 آموزش اتصال", "tutorial"), button("☎️ پشتیبانی", "support")],
    ]
    if admin:
        rows.append([button("⚙️ پنل ادمین", "adm:menu")])
    return rows


def reply_menu(admin=False):
    return ReplyKeyboardMarkup([
        [KeyboardButton(b.text) for b in row]
        for row in main_rows(admin)
    ], resize_keyboard=True, is_persistent=True, input_field_placeholder="یکی رو انتخاب کن 👇")


async def remove_inline_menu(update, delete_message=False, bot=None):
    query = getattr(update, "callback_query", None)
    if not query or not query.message:
        return
    if delete_message:
        try:
            active_bot = bot or _bot_for(query.message)
            if active_bot:
                await active_bot.delete_message(
                    chat_id=query.message.chat_id,
                    message_id=query.message.message_id,
                )
            else:
                await query.delete_message()
            keep_message(active_bot, query.message)
            return
        except TelegramError:
            pass
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except BadRequest:
        pass


def _key(bot, chat_id):
    return id(bot), int(chat_id)


def track_transient(bot, message):
    """Remember a temporary bot message without ever scheduling user messages."""
    if not bot or not callable(getattr(bot, "delete_message", None)) or not message:
        return
    chat_id = getattr(message, "chat_id", None)
    message_id = getattr(message, "message_id", None)
    if chat_id is None or message_id is None:
        return
    _transient_messages.setdefault(_key(bot, chat_id), set()).add(int(message_id))


def keep_message(bot, message):
    if not bot or not message:
        return
    chat_id = getattr(message, "chat_id", None)
    message_id = getattr(message, "message_id", None)
    if chat_id is None or message_id is None:
        return
    messages = _transient_messages.get(_key(bot, chat_id))
    if messages:
        messages.discard(int(message_id))


async def delete_transient(bot, chat_id):
    """Delete bot menus/prompts; receipts and delivered services are kept."""
    message_ids = _transient_messages.pop(_key(bot, chat_id), set())
    for message_id in sorted(message_ids):
        try:
            await bot.delete_message(chat_id=chat_id, message_id=message_id)
        except Exception:
            pass


async def _delete_after_inactivity(bot, chat_id):
    key = _key(bot, chat_id)
    try:
        await asyncio.sleep(AUTO_DELETE_SECONDS)
        await delete_transient(bot, chat_id)
    except asyncio.CancelledError:
        raise
    finally:
        if _cleanup_tasks.get(key) is asyncio.current_task():
            _cleanup_tasks.pop(key, None)


def touch_cleanup(bot, chat_id):
    """Restart the seven-minute inactivity window for this private chat."""
    if not callable(getattr(bot, "delete_message", None)):
        return
    key = _key(bot, chat_id)
    previous = _cleanup_tasks.get(key)
    if previous and not previous.done():
        previous.cancel()
    _cleanup_tasks[key] = asyncio.create_task(_delete_after_inactivity(bot, chat_id))


async def cancel_cleanup_tasks():
    tasks = list(_cleanup_tasks.values())
    _cleanup_tasks.clear()
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _transient_messages.clear()


def _bot_for(message):
    getter = getattr(message, "get_bot", None)
    if not callable(getter):
        return None
    try:
        return getter()
    except RuntimeError:
        return None


def _remember(message, keep=False):
    bot = _bot_for(message)
    if keep:
        keep_message(bot, message)
    else:
        track_transient(bot, message)
    return message


async def send(update, text, rows=None, keep=False):
    markup = InlineKeyboardMarkup(rows) if rows is not None else None
    kwargs = {"reply_markup": markup, "parse_mode": "HTML", "disable_web_page_preview": True}
    query = update.callback_query
    if query and query.message and query.message.text:
        try:
            return _remember(await query.edit_message_text(text, **kwargs), keep)
        except BadRequest as exc:
            if "message is not modified" in str(exc).lower():
                return None
            if not any(s in str(exc).lower() for s in ("message to edit not found", "message can't be edited")):
                raise
    return _remember(await update.effective_message.reply_text(text, **kwargs), keep)
