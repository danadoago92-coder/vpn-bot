"""Admin-managed locations, per-location protocols, panel state and cards."""
import json

from card_rotation import normalize_cards
from ui import button, esc, is_admin, send

PROTOCOLS = {
    "v2ray": "🔗 کانفیگ عادی",
    "wireguard": "🛡 WireGuard",
    "openvpn": "🔐 OpenVPN",
}
KINDS = {"country": "🏳️ کشور", "multi": "🌍 مولتی‌لوکیشن"}
BACK = [[button("🔙 پنل ادمین", "adm:home")]]


async def _find_location(store, location_id):
    return next((item for item in await store.list_locations() if item["id"] == location_id), None)


async def _location_screen(update, ctx, location_id, notice=""):
    store = ctx.bot_data["store"]
    loc = await _find_location(store, location_id)
    if not loc:
        raise ValueError("لوکیشن پیدا نشد.")
    enabled = {item["protocol"]: bool(item["active"]) for item in await store.location_protocols(location_id)}
    rows = [
        [button(("🟢 " if enabled.get(key) else "⚪️ ") + label, f"cat:toggleprotocol:{location_id}:{key}")]
        for key, label in PROTOCOLS.items()
    ]
    rows.extend([
        [button("⏸ خاموش" if loc["active"] else "▶️ روشن", f"cat:togglelocation:{loc['id']}"),
         button("✏️ تغییر نام", f"cat:renamelocation:{loc['id']}")],
        [button("🗑 حذف از فروش", f"cat:deletelocation:{loc['id']}")],
        [button("🔙 لوکیشن‌ها", "cat:locations")],
    ])
    prefix = f"{notice}\n\n" if notice else ""
    await send(update, prefix + f"{KINDS.get(loc.get('kind'), '🏳️ کشور')} · <b>{esc(loc['name'])}</b>\n\n"
        "پروتکل‌هایی را که برای این لوکیشن می‌فروشی روشن کن. هر پروتکل پلن‌های مستقل خودش را دارد.", rows)


async def callback(update, ctx):
    data = update.callback_query.data or ""
    if not data.startswith("cat:"):
        return False
    if not is_admin(update, ctx):
        await send(update, "⛔️ این بخش برای ادمین است.")
        return True
    store = ctx.bot_data["store"]
    bits = data.split(":")
    action = bits[1]
    ctx.user_data.pop("admin_state", None)

    if action == "locations":
        locations = await store.list_locations()
        rows = [[button(("🟢 " if item["active"] else "⚪️ ") + item["name"], f"cat:location:{item['id']}")]
                for item in locations]
        text = "🧭 <b>لوکیشن‌ها و پروتکل‌ها</b>\n\nکشور یا مولتی‌لوکیشن را با نام دلخواه بساز و خروجی‌های هرکدام را جدا روشن کن."
        if not locations:
            text += "\n\nهنوز هیچ لوکیشنی ساخته نشده است."
        await send(update, text, rows + [[button("➕ افزودن لوکیشن", "cat:addlocation")]] + BACK)
    elif action == "addlocation":
        await send(update, "➕ <b>نوع لوکیشن</b>\n\nچه چیزی می‌خواهی به منوی خرید اضافه کنی؟", [
            [button("🏳️ کشور", "cat:addkind:country"), button("🌍 مولتی‌لوکیشن", "cat:addkind:multi")],
            [button("🔙 لوکیشن‌ها", "cat:locations")],
        ])
    elif action == "addkind" and len(bits) == 3 and bits[2] in KINDS:
        ctx.user_data["admin_state"] = {"kind": "catalog_location", "location_kind": bits[2]}
        example = "🇳🇱 هلند" if bits[2] == "country" else "🌍 مولتی‌لوکیشن ویژه"
        await send(update, f"{KINDS[bits[2]]} <b>نام دلخواه</b>\n\nنام و ایموجی را بفرست؛ نمونه: {example}\nانصراف: /cancel", BACK)
    elif action in ("location", "togglelocation", "deletelocation", "confirmdelete", "renamelocation") and len(bits) == 3:
        loc = await _find_location(store, bits[2])
        if not loc:
            raise ValueError("لوکیشن پیدا نشد.")
        if action == "togglelocation":
            await store.save_location(loc["name"], loc["id"], not loc["active"], loc.get("kind", "country"))
        elif action == "deletelocation":
            await send(update, "🗑 <b>حذف از فروش</b>\n\nاین لوکیشن حذف شود؟ سرویس‌ها و سفارش‌های قبلی حفظ می‌شوند.", [
                [button("🗑 بله، حذف شود", f"cat:confirmdelete:{loc['id']}")], [button("↩️ انصراف", f"cat:location:{loc['id']}")]])
            return True
        elif action == "confirmdelete":
            await store.archive_location(loc["id"])
            await send(update, "✅ لوکیشن از منوی خرید برداشته شد.", [[button("🧭 لوکیشن‌ها", "cat:locations")]])
            return True
        elif action == "renamelocation":
            ctx.user_data["admin_state"] = {"kind": "catalog_location", "location_id": loc["id"],
                "active": loc["active"], "location_kind": loc.get("kind", "country")}
            await send(update, "✏️ <b>تغییر نام لوکیشن</b>\n\nنام و ایموجی جدید را بفرست.", BACK)
            return True
        await _location_screen(update, ctx, loc["id"])
    elif action == "toggleprotocol" and len(bits) == 4:
        location_id, protocol = bits[2], bits[3]
        if protocol not in PROTOCOLS:
            raise ValueError("پروتکل نامعتبر است.")
        current = {item["protocol"]: bool(item["active"]) for item in await store.location_protocols(location_id)}
        await store.set_location_protocol(location_id, protocol, not current.get(protocol, False))
        await _location_screen(update, ctx, location_id, "✅ تنظیم پروتکل ذخیره شد.")
    elif action in ("protocols", "protocol"):
        await send(update, "🧭 پروتکل‌ها برای هر لوکیشن جدا تنظیم می‌شوند. از «مدیریت لوکیشن‌ها» وارد همان لوکیشن شو.", BACK)
    elif action in ("panel", "togglepanel"):
        settings = ctx.bot_data["settings"]
        values = await store.get_settings()
        enabled = values.get("panel_connected", "true" if settings.panel_connected else "false") == "true"
        if action == "togglepanel":
            if not enabled and not (settings.xui_url and settings.xui_sub_url and (settings.xui_api_token or (settings.xui_username and settings.xui_password))):
                raise ValueError("اول مشخصات پنل را از setup.py در SSH وارد کن.")
            enabled = not enabled
            await store.set_setting("panel_connected", str(enabled).lower())
        await send(update, "⚙️ <b>اتصال پنل</b>\n\n" + ("روشن؛ پلن‌های خودکار کانفیگ عادی را از API می‌سازند." if enabled else "خاموش؛ تحویل دستی و موجودی آماده بدون API کار می‌کند."),
            [[button("⏸ خاموش کردن" if enabled else "▶️ روشن کردن", "cat:togglepanel")]] + BACK)
    elif action in ("planlocation", "planprotocol") and len(bits) == 3:
        plan = await store.get_plan(int(bits[2]))
        if not plan:
            raise ValueError("پلن پیدا نشد.")
        if action == "planlocation":
            rows = [[button(item["name"], f"cat:chooseplanlocation:{plan['id']}:{item['id']}")]
                    for item in await store.list_locations(active_only=True)]
            title = "🧭 لوکیشن پلن را انتخاب کن:"
        else:
            protocols = await store.location_protocols(plan["location_id"], active_only=True)
            rows = [[button(PROTOCOLS[item["protocol"]], f"cat:setprotocol:{plan['id']}:{item['protocol']}")]
                    for item in protocols]
            title = "📡 پروتکل پلن را انتخاب کن:"
        await send(update, title, rows + [[button("🔙 پلن", f"adm:plan:{plan['id']}")]])
    elif action == "chooseplanlocation" and len(bits) == 4:
        plan = await store.get_plan(int(bits[2]))
        protocols = await store.location_protocols(bits[3], active_only=True)
        if not plan or not protocols:
            raise ValueError("برای این لوکیشن اول حداقل یک پروتکل را روشن کن.")
        rows = [[button(PROTOCOLS[item["protocol"]], f"cat:setroute:{plan['id']}:{bits[3]}:{item['protocol']}")]
                for item in protocols]
        await send(update, "📡 پروتکل این پلن را برای لوکیشن جدید انتخاب کن:", rows + [[button("↩️ انصراف", f"adm:plan:{plan['id']}")]])
    elif action == "setroute" and len(bits) == 5:
        plan = await store.get_plan(int(bits[2]))
        if not plan:
            raise ValueError("پلن پیدا نشد.")
        plan.update(location_id=bits[3], protocol=bits[4])
        await store.save_plan(plan)
        await send(update, "✅ لوکیشن و پروتکل پلن ذخیره شد.", [[button("🔙 پلن", f"adm:plan:{plan['id']}")]])
    elif action == "setprotocol" and len(bits) == 4:
        plan = await store.get_plan(int(bits[2]))
        if not plan:
            raise ValueError("پلن پیدا نشد.")
        plan["protocol"] = bits[3]
        await store.save_plan(plan)
        await send(update, "✅ پروتکل پلن ذخیره شد.", [[button("🔙 پلن", f"adm:plan:{plan['id']}")]])
    elif action == "cards":
        ctx.user_data["admin_state"] = {"kind": "catalog_cards"}
        await send(update, "💳 کارت‌های چرخشی را هرکدام در یک خط بفرست:\n<code>شماره کارت | نام صاحب کارت</code>\n\nبرای استفاده از کارت اصلی، - بفرست.", BACK)
    else:
        raise ValueError("این دکمه قدیمی است؛ پنل را دوباره باز کن.")
    return True


async def text(update, ctx):
    state = ctx.user_data.get("admin_state", {})
    if state.get("kind") not in ("catalog_location", "catalog_cards"):
        return False
    if not is_admin(update, ctx):
        ctx.user_data.pop("admin_state", None)
        return True
    value = update.effective_message.text.strip()
    if state["kind"] == "catalog_location":
        location_id = await ctx.bot_data["store"].save_location(value, state.get("location_id"), state.get("active", True), state["location_kind"])
        ctx.user_data.pop("admin_state", None)
        await _location_screen(update, ctx, location_id, "✅ لوکیشن ذخیره شد؛ حالا پروتکل‌هایش را روشن کن.")
    else:
        cards = []
        if value != "-":
            for line in value.splitlines():
                if "|" not in line:
                    raise ValueError("هر خط: شماره کارت | نام صاحب کارت")
                number, holder = line.split("|", 1)
                cards.append({"number": number, "holder": holder})
        await ctx.bot_data["store"].set_setting("cards", json.dumps(normalize_cards(cards), ensure_ascii=False))
        ctx.user_data.pop("admin_state", None)
        await send(update, "✅ کارت‌ها ذخیره شدند.", BACK)
    return True
