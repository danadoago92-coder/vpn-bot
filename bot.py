"""Portable Telegram entry point. No network or data mutation on import."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import suppress
import fcntl
import logging
import os
import re
from pathlib import Path
import sys

from telegram import BotCommand, Update
from telegram.error import BadRequest, NetworkError, TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters

from config import load_settings
from database import Store
from provisioning import Provisioner
from handlers import admin, customer, catalog_admin
from purchase import retry_deliveries
from ui import cancel_cleanup_tasks, delete_transient, esc, is_admin, main_rows, reply_menu, send, touch_cleanup
from stickers import show_sticker

log = logging.getLogger(__name__)


def clear_flow(ctx):
    """Drop unfinished forms while keeping the last menu sticker removable."""
    menu_sticker = ctx.user_data.get("menu_sticker")
    ctx.user_data.clear()
    if menu_sticker:
        ctx.user_data["menu_sticker"] = menu_sticker


async def startup(application):
    await application.bot_data["store"].init()
    from config import PLANS
    if PLANS:
        from tools.import_plans import import_plans
        plans = [{"id":key,**value} for key,value in PLANS.items()] if isinstance(PLANS,dict) else PLANS
        await import_plans(application.bot_data["store"],plans)
    application.bot_data["delivery_task"] = asyncio.create_task(retry_deliveries(application))
    try:
        await application.bot.set_my_commands([
            BotCommand("start", "نمایش کیبورد اصلی"),
            BotCommand("cancel", "خروج از مرحلهٔ فعلی"),
        ])
    except TelegramError as exc:
        log.warning("Command menu registration failed (%s)", type(exc).__name__)
    username = getattr(application.bot, "username", None)
    log.info("ربات%s آماده است و پیام‌های تلگرام را دریافت می‌کند.", f" @{username}" if username else "")


async def shutdown(application):
    task = application.bot_data.pop("delivery_task", None)
    if task:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
    await cancel_cleanup_tasks()
    await application.bot_data["provisioner"].close()
    await application.bot_data["store"].close()


async def handle_update(update, ctx):
    if not update.effective_user or not update.effective_chat or update.effective_chat.type != "private":
        return
    touch_cleanup(ctx.bot, update.effective_chat.id)
    if update.callback_query:
        try:
            await update.callback_query.answer()
        except BadRequest:
            return  # Do not execute expired callbacks that the user cannot acknowledge.
    uid = update.effective_user.id
    locks = ctx.bot_data.setdefault("user_locks", {})
    entry = locks.setdefault(uid, {"lock": asyncio.Lock(), "users": 0})
    entry["users"] += 1
    try:
        async with entry["lock"]:
            store = ctx.bot_data["store"]
            user = await store.upsert_user(uid, update.effective_user.username or "", update.effective_user.full_name or "")
            if user["blocked"] and not is_admin(update, ctx):
                return await send(update, "دسترسی شما محدود شده است.")
            try:
                await dispatch(update, ctx)
            except ValueError as exc:
                message = str(exc)
                if not any("\u0600" <= c <= "\u06ff" for c in message):
                    message = "این ورودی معتبر نیست؛ از دکمه‌های منو استفاده کنید."
                await update.effective_message.reply_text(
                    "⚠️ " + message,
                    reply_markup=reply_menu(is_admin(update, ctx)),
                )
    finally:
        entry["users"] -= 1
        if not entry["users"]:
            locks.pop(uid, None)


async def dispatch(update, ctx):
    if update.callback_query:
        data = update.callback_query.data or ""
        # Switching menus ends transient input; order itself remains in the database.
        ctx.user_data.pop("customer_state", None)
        ctx.user_data.pop("receipt_order", None)
        if data.startswith("cat:"):
            await catalog_admin.callback(update, ctx)
        elif data.startswith("adm:"):
            await admin.callback(update, ctx)
        else:
            ctx.user_data.pop("admin_state", None)
            await customer.callback(update, ctx)
        return
    message = update.effective_message
    replied = getattr(message, "reply_to_message", None)
    if replied and is_admin(update, ctx):
        sender = getattr(replied, "from_user", None)
        if sender and sender.id == ctx.bot.id:
            match = re.search(r"(?:لینک|کانفیگ) سفارش #(\d+)", replied.text or "")
            if match:
                order = await ctx.bot_data["store"].get_order(int(match.group(1)))
                if not order or order["status"] != "awaiting_config":
                    raise ValueError("این سفارش دیگر منتظر کانفیگ نیست.")
                ctx.user_data["admin_state"] = {"kind":"manual_config", "order_id":order["id"]}
    if message.photo:
        return await customer.photo(update, ctx)
    if message.document and is_admin(update,ctx):
        if await admin.document(update,ctx):
            return
    if not message.text:
        return await send(update, "برای تحویل، لینک اشتراک یا فایل کانفیگ خواسته‌شده را بفرست؛ برای پرداخت، عکس رسید را ارسال کن.")
    value = message.text.strip()
    if value.startswith("/"):
        command = value.split()[0].split("@")[0].lower()
        if command in ("/start", "/cancel"):
            await delete_transient(ctx.bot, update.effective_chat.id)
            result = await customer.home(update, ctx, fresh=True)
            await _delete_menu_trigger(update, ctx)
            return result
        if command == "/admin":
            clear_flow(ctx)
            await show_sticker(update, ctx, "tutorial")
            return await admin.menu(update, ctx)
        commands = {"/buy": "buy", "/services": "services", "/wallet": "wallet", "/profile": "profile", "/orders": "orders", "/help": "tutorial"}
        if command in commands:
            clear_flow(ctx)
            await delete_transient(ctx.bot, update.effective_chat.id)
            result = await customer.callback(update, ctx, commands[command])
            await _delete_menu_trigger(update, ctx)
            return result
        return await customer.home(update, ctx, fresh=True)
    menus = {b.text: b.callback_data for row in main_rows(is_admin(update, ctx)) for b in row}
    if value in menus:
        clear_flow(ctx)
        await delete_transient(ctx.bot, update.effective_chat.id)
        action = menus[value]
        if action.startswith("adm:"):
            await show_sticker(update, ctx, "tutorial")
            result = await admin.menu(update, ctx)
        else:
            result = await customer.callback(update, ctx, action)
        await _delete_menu_trigger(update, ctx)
        return result
    # Old buttons do not route to removed test/group/reseller/protocol handlers.
    if await catalog_admin.text(update, ctx):
        return
    if await admin.text(update, ctx):
        return
    if await customer.text(update, ctx):
        return
    return await customer.home(update, ctx, fresh=True)


async def _delete_menu_trigger(update, ctx):
    """Remove a reply-keyboard command bubble when Telegram permits it."""
    message = update.effective_message
    try:
        await ctx.bot.delete_message(chat_id=message.chat_id, message_id=message.message_id)
    except TelegramError:
        pass


async def error_handler(update, ctx):
    # Telegram/panel exception text can include URLs or credentials; never log it.
    error = ctx.error
    log.error("Request failed: %s", type(error).__name__)
    if isinstance(error, NetworkError):
        return
    if update and update.effective_chat and update.effective_chat.type == "private":
        with suppress(TelegramError):
            await update.effective_message.reply_text("خطای موقتی رخ داد. از /start وارد شوید و وضعیت سفارش قبلی را بررسی کنید؛ دوباره پرداخت نکنید.")


def build_application(settings):
    builder = (Application.builder().token(settings.token).concurrent_updates(8)
        .connect_timeout(15).read_timeout(30).write_timeout(30).pool_timeout(15)
        .get_updates_connect_timeout(15).get_updates_read_timeout(40)
        .post_init(startup).post_shutdown(shutdown))
    if settings.telegram_proxy_url:
        builder = builder.proxy(settings.telegram_proxy_url).get_updates_proxy(settings.telegram_proxy_url)
    application = builder.build()
    store = Store(settings.db_path)
    application.bot_data.update(settings=settings, store=store, provisioner=Provisioner(settings, store))
    application.add_handler(CallbackQueryHandler(handle_update))
    application.add_handler(MessageHandler(filters.ChatType.PRIVATE & (filters.TEXT | filters.PHOTO | filters.Document.ALL), handle_update))
    application.add_error_handler(error_handler)
    return application


def acquire_instance_lock(db_path):
    path = Path(db_path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    handle = path.with_suffix(path.suffix+".lock").open("a+")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise ValueError("یک نسخهٔ دیگر ربات با همین دیتابیس در حال اجراست.") from None
    return handle


def main(argv=None):
    parser = argparse.ArgumentParser(description="ربات فروش کانفیگ با لوکیشن و پروتکل داینامیک")
    parser.add_argument("--check", action="store_true", help="اعتبارسنجی تنظیمات و اتصال ماژول‌ها، بدون اتصال شبکه")
    args = parser.parse_args(argv)
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for name in ("httpx", "httpcore", "telegram", "aiohttp"):
        logging.getLogger(name).setLevel(logging.WARNING)
    try:
        settings = load_settings()
        app = build_application(settings)
        if args.check:
            print("تنظیمات و ماژول‌ها معتبرند. در این بررسی به تلگرام یا پنل متصل نشدیم.")
            return 0
        with acquire_instance_lock(settings.db_path):
            # Polling needs outbound HTTPS only; no inbound port or domain is required.
            asyncio.set_event_loop(asyncio.new_event_loop())
            log.info("در حال اتصال به تلگرام؛ برای توقف Ctrl+C را بزنید.")
            app.run_polling(allowed_updates=["message", "callback_query"], drop_pending_updates=False, bootstrap_retries=5)
        return 0
    except (ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except TelegramError as exc:
        print(f"خطای ارتباط با تلگرام ({type(exc).__name__}). توکن، اینترنت و پراکسی را بررسی کنید.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
