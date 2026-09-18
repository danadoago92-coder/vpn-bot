"""Administrator permissions and real SQLite workflow integration; no network calls."""
from __future__ import annotations

from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import Settings
from database import Store
from handlers import admin
from admins import read_admins, write_admins
from provisioning import Provisioner


class AdminTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "test.sqlite3")
        await self.store.init()
        self.location_id = await self.store.save_location("🌍 تست", kind="multi")
        await self.store.set_location_protocol(self.location_id, "v2ray", True)
        self.admin_file = Path(self.temp.name) / "admins.json"
        write_admins(self.admin_file, (10, 11))
        self.settings = Settings(admin_ids=(10, 11), brand_name="برند آزمایشی", admins_file=self.admin_file)
        self.provisioner = Provisioner(self.settings, self.store)
        self.ctx = SimpleNamespace(
            bot_data={"settings": self.settings, "store": self.store, "provisioner": self.provisioner},
            user_data={}, bot=SimpleNamespace(send_message=AsyncMock(), send_photo=AsyncMock(), send_document=AsyncMock()),
        )
        self.send = AsyncMock()
        self.send_patch = patch.object(admin, "send", self.send)
        self.send_patch.start()
        await self.store.upsert_user(20, "customer_a", "خریدار اول")
        await self.store.upsert_user(21, "customer_b", "خریدار دوم")

    async def asyncTearDown(self):
        self.send_patch.stop()
        await self.provisioner.close()
        await self.store.close()
        self.temp.cleanup()

    def update(self, *, data=None, text=None, user_id=10):
        message = SimpleNamespace(text=text, reply_text=AsyncMock())
        query = SimpleNamespace(data=data, message=message, answer=AsyncMock()) if data is not None else None
        return SimpleNamespace(effective_user=SimpleNamespace(id=user_id), effective_message=message, message=message, callback_query=query)

    async def click(self, data, user_id=10):
        return await admin.callback(self.update(data=data, user_id=user_id), self.ctx)

    async def write(self, value, user_id=10):
        return await admin.text(self.update(text=value, user_id=user_id), self.ctx)

    def buttons(self):
        return [b for row in self.send.await_args.args[2] for b in row]

    async def plan(self, *, months=1, mode="manual"):
        return await self.store.save_plan({"name": f"پلن {months} ماهه", "months": months, "traffic_gb": 30, "price": 150000, "mode": mode, "inbound_id": 0, "active": True, "location_id": self.location_id, "protocol": "v2ray"})

    async def order(self, plan, user_id=20):
        order = await self.store.create_order(user_id, plan_id=plan["id"])
        await self.store.attach_receipt(order["id"], user_id, f"receipt-{order['id']}")
        return order

    async def test_every_admin_callback_rejects_non_admin_before_store_access(self):
        callbacks = [
            "adm:home", "adm:menu", "adm:plans", "adm:plan:1", "adm:new:1", "adm:create", "adm:newmode:manual",
            "adm:edit:1:price", "adm:mode:1", "adm:setmode:1:xui", "adm:months:1", "adm:setmonths:1:2",
            "adm:toggle:1", "adm:stock:1", "adm:archive:1", "adm:archiveok:1", "adm:orders:0",
            "adm:order:1", "adm:receipt:1", "adm:approve:1", "adm:reject:1", "adm:rejectok:1", "adm:manual:1",
            "adm:users:0", "adm:user:20", "adm:block:20:1", "adm:finduser", "adm:admins", "adm:addadmin",
            "adm:deladmin:11", "adm:deladminok:11", "adm:stats", "adm:settings",
            "adm:tutorials", "adm:setting:brand_name", "adm:unknown",
        ]
        with patch.object(admin, "_store", side_effect=AssertionError("unauthorized storage access")):
            for route in callbacks:
                self.ctx.user_data["admin_state"] = {"kind": "setting", "key": "brand_name"}
                self.assertTrue(await self.click(route, user_id=20), route)
                self.assertNotIn("admin_state", self.ctx.user_data)
            await admin.menu(self.update(user_id=20), self.ctx)
        self.ctx.bot.send_message.assert_not_awaited()
        self.ctx.bot.send_photo.assert_not_awaited()

    async def test_forged_admin_text_states_cannot_mutate_or_send_config(self):
        for kind in ("new_plan", "edit_plan", "inventory", "manual_config", "setting", "find_user"):
            self.ctx.user_data["admin_state"] = {"kind": kind, "key": "brand_name", "order_id": 1}
            with patch.object(admin, "_store", side_effect=AssertionError("unauthorized storage access")):
                self.assertTrue(await self.write("forged", user_id=20))
            self.assertNotIn("admin_state", self.ctx.user_data)
        self.assertEqual({}, await self.store.get_settings())

    async def test_create_manual_plan_in_correct_duration_with_persian_numbers(self):
        await self.click("adm:new:2")
        await self.click(f"adm:newloc:2:{self.location_id}")
        await self.click(f"adm:newproto:2:{self.location_id}:v2ray")
        await self.write("مولتی لوکیشن ویژه")
        await self.write("۲۵۰٬۰۰۰")
        await self.write("۵۰٫۵")
        self.assertEqual("mode", self.ctx.user_data["admin_state"]["field"])
        self.assertFalse(any(b.callback_data.endswith(":xui") for b in self.buttons()))
        await self.click("adm:newmode:manual")
        await self.write("برای استفاده روزانه")
        await self.click("adm:create")
        plans = await self.store.list_plans(months=2)
        self.assertEqual(1, len(plans))
        self.assertEqual((250000, 50.5, 60, "manual"), (plans[0]["price"], plans[0]["traffic_gb"], plans[0]["days"], plans[0]["mode"]))
        self.assertEqual([], await self.store.list_plans(months=1))
        await self.click("adm:create")
        self.assertEqual(1, len(await self.store.list_plans()))
        self.assertIn("منقضی", self.send.await_args.args[1])

    async def test_invalid_inputs_keep_wizard_for_correction(self):
        await self.click("adm:new:1")
        await self.click(f"adm:newloc:1:{self.location_id}")
        await self.click(f"adm:newproto:1:{self.location_id}:v2ray")
        await self.write("پلن")
        await self.write("-1")
        self.assertEqual("price", self.ctx.user_data["admin_state"]["field"])
        await self.write("۱۲۰۰۰۰")
        await self.write("nan")
        self.assertEqual("traffic_gb", self.ctx.user_data["admin_state"]["field"])
        await self.write("۰")
        await self.click("adm:newmode:xui")
        self.assertEqual("mode", self.ctx.user_data["admin_state"]["field"])
        self.assertIn("SSH", self.send.await_args.args[1])

    async def test_edit_plan_price_does_not_change_existing_order(self):
        plan = await self.plan()
        order = await self.order(plan)
        await self.click(f"adm:edit:{plan['id']}:price")
        await self.write("۲۰۰۰۰۰")
        await self.click(f"adm:setmonths:{plan['id']}:2")
        modified = await self.store.get_plan(plan["id"])
        previous = await self.store.get_order(order["id"])
        self.assertEqual((200000, 2), (modified["price"], modified["months"]))
        self.assertEqual((150000, 1), (previous["amount"], previous["plan_snapshot"]["months"]))

    async def test_inventory_validation_and_deduplication(self):
        plan = await self.plan(mode="inventory")
        await self.click(f"adm:stock:{plan['id']}")
        await self.write("not a config")
        self.assertEqual(0, await self.store.inventory_count(plan["id"]))
        self.assertIn("admin_state", self.ctx.user_data)
        await self.write("https://example.com/sub/one\nhttps://example.com/sub/two\nhttps://example.com/sub/one")
        self.assertEqual(2, await self.store.inventory_count(plan["id"]))
        await self.click(f"adm:stock:{plan['id']}")
        await self.write("https://example.com/sub/one")
        self.assertEqual(2, await self.store.inventory_count(plan["id"]))

    async def test_manual_approval_and_delivery_stay_bound_to_selected_order(self):
        plan = await self.plan()
        first = await self.order(plan, 20)
        second = await self.order(plan, 21)
        self.ctx.bot.send_message.reset_mock()
        await self.click(f"adm:approve:{first['id']}")
        self.assertEqual("awaiting_config", (await self.store.get_order(first["id"]))["status"])
        self.ctx.bot.send_message.assert_not_awaited()
        await self.click(f"adm:approve:{second['id']}")
        self.assertEqual(second["id"], self.ctx.user_data["admin_state"]["order_id"])
        await self.write("https://example.com/sub/second")
        service = (await self.store.list_services(21))[0]
        self.assertEqual(second["id"], service["order_id"])
        self.assertEqual("https://example.com/sub/second", service["config_text"])
        self.assertEqual([], await self.store.list_services(20))
        self.assertEqual("awaiting_config", (await self.store.get_order(first["id"]))["status"])
        await self.click(f"adm:manual:{first['id']}")
        await self.write("https://example.com/sub/first")
        self.assertEqual(first["id"], (await self.store.list_services(20))[0]["order_id"])

    async def test_receipt_photo_only_sent_on_explicit_view(self):
        order = await self.order(await self.plan())
        await self.click(f"adm:order:{order['id']}")
        self.ctx.bot.send_photo.assert_not_awaited()
        await self.click(f"adm:receipt:{order['id']}")
        self.ctx.bot.send_photo.assert_awaited_once()
        self.assertEqual(10, self.ctx.bot.send_photo.await_args.kwargs["chat_id"])

    async def test_topup_approval_is_idempotent(self):
        order = await self.store.create_order(20, amount=500000, kind="topup")
        await self.store.attach_receipt(order["id"], 20, "topup-receipt")
        await self.click(f"adm:approve:{order['id']}")
        await self.click(f"adm:approve:{order['id']}")
        self.assertEqual(500000, (await self.store.get_user(20))["balance"])
        self.ctx.bot.send_message.assert_awaited_once()

    async def test_rejection_and_user_blocking(self):
        order = await self.order(await self.plan())
        await self.click(f"adm:reject:{order['id']}")
        self.assertEqual("awaiting_review", (await self.store.get_order(order["id"]))["status"])
        await self.click(f"adm:rejectok:{order['id']}")
        self.assertEqual("rejected", (await self.store.get_order(order["id"]))["status"])
        await self.click("adm:block:20:1")
        self.assertTrue((await self.store.get_user(20))["blocked"])
        await self.click("adm:block:20:0")
        self.assertFalse((await self.store.get_user(20))["blocked"])
        await self.click("adm:block:10:1")
        self.assertIn("نمی‌توان", self.send.await_args.args[1])

    async def test_admins_can_be_added_and_removed_without_restart(self):
        await self.click("adm:addadmin")
        self.assertEqual("add_admin", self.ctx.user_data["admin_state"]["kind"])
        await self.write("۱۲۳۴۵۶۷۸۹")
        self.assertEqual((10, 11, 123456789), read_admins(self.admin_file))
        self.assertNotIn("admin_state", self.ctx.user_data)
        self.assertTrue(await self.click("adm:admins", user_id=123456789))
        await self.click("adm:deladminok:11")
        self.assertEqual((10, 123456789), read_admins(self.admin_file))

    async def test_admin_cannot_remove_self_or_last_admin(self):
        await self.click("adm:deladminok:10")
        self.assertEqual((10, 11), read_admins(self.admin_file))
        self.assertIn("خودت", self.send.await_args.args[1])
        write_admins(self.admin_file, (10,))
        with self.assertRaisesRegex(ValueError, "حداقل"):
            admin.update_admins(self.admin_file, remove=10, actor=999)

    async def test_settings_validation_and_plain_text_storage(self):
        await self.click("adm:setting:card_number")
        await self.write("۱۲۳۴")
        self.assertNotIn("card_number", await self.store.get_settings())
        await self.write("۶۰۳۷-۹۹۷۵-۱۲۳۴-۵۶۷۸")
        self.assertEqual("6037997512345678", (await self.store.get_settings())["card_number"])
        await self.click("adm:setting:delivery_template")
        await self.write("{name.__class__}")
        self.assertNotIn("delivery_template", await self.store.get_settings())
        await self.write("{name} عزیز، سرویس {plan} از {brand} آماده است.")
        await self.click("adm:setting:welcome_text")
        await self.write("سلام {name}، به {brand} خوش آمدی")
        await self.click("adm:setting:tutorial_android")
        await self.write("آموزش <متن> https://example.com")
        self.assertEqual("آموزش <متن> https://example.com", (await self.store.get_settings())["tutorial_android"])

    async def test_stale_callbacks_and_cancel_recover_cleanly(self):
        for route in ("adm:plans:9:0", "adm:order:no", "adm:plan:999", "adm:unknown", "adm:edit:1:bad", "adm:newmode:manual"):
            self.assertTrue(await self.click(route), route)
        self.assertFalse(await self.click("services"))
        await self.click("adm:setting:brand_name")
        await self.write("/cancel")
        self.assertNotIn("admin_state", self.ctx.user_data)
        await self.click("adm:menu")
        self.assertIn("مدیریت", self.send.await_args.args[1])


if __name__ == "__main__":
    unittest.main()
