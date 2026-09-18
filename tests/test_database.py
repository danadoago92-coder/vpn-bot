import asyncio
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from database import Store, validate_config

CONFIG_A = "https://sub.example.test/sub/one"
CONFIG_B = "https://sub.example.test/sub/two"


class StoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "bot.sqlite3")
        await self.store.init()
        self.location_id = await self.store.save_location("🌍 تست", kind="multi")
        await self.store.set_location_protocol(self.location_id, "v2ray", True)
        await self.store.upsert_user(100, "customer", "Customer")
        self.plan = await self.store.save_plan({"name": "یک ماهه", "months": 1, "traffic_gb": 20, "price": 100, "mode": "inventory", "location_id": self.location_id})

    async def asyncTearDown(self):
        await self.store.close()
        self.temp.cleanup()

    async def credit(self, amount=1000):
        order = await self.store.create_order(100, amount=amount, kind="topup")
        await self.store.attach_receipt(order["id"], 100, "receipt")
        await self.store.approve_topup(order["id"])
        return order

    async def buy(self, method="wallet", request_key=None):
        order = await self.store.create_order(100, self.plan["id"], method=method, request_key=request_key)
        if method == "card":
            await self.store.attach_receipt(order["id"], 100, "receipt")
        return order

    async def test_new_database_has_no_customer_or_saleable_sample_data(self):
        other = Store(Path(self.temp.name) / "fresh.sqlite3")
        await other.init()
        self.assertEqual(await other.list_plans(), [])
        self.assertEqual(await other.list_locations(), [])
        self.assertEqual((await other.get_stats())["users"], 0)

    async def test_parallel_topup_approval_credits_only_once(self):
        order = await self.store.create_order(100, amount=1000, kind="topup")
        await self.store.attach_receipt(order["id"], 100, "receipt")
        approvals = await asyncio.gather(*(self.store.approve_topup(order["id"]) for _ in range(15)))
        self.assertTrue(all(order["status"] == "paid" for order in approvals))
        self.assertEqual((await self.store.get_user(100))["balance"], 1000)

    async def test_topup_requires_own_receipt(self):
        await self.store.upsert_user(200)
        order = await self.store.create_order(100, amount=1000, kind="topup")
        self.assertFalse(await self.store.attach_receipt(order["id"], 200, "stolen"))
        with self.assertRaises(ValueError):
            await self.store.approve_topup(order["id"])
        self.assertEqual((await self.store.get_user(100))["balance"], 0)

    async def test_parallel_requests_reuse_one_snapshot(self):
        orders = await asyncio.gather(*(self.buy(request_key="same") for _ in range(15)))
        self.assertEqual(len({order["id"] for order in orders}), 1)
        await self.store.save_plan({"id": self.plan["id"], "price": 999, "name": "edited", "months": 2})
        order = await self.store.get_order(orders[0]["id"])
        self.assertEqual(order["amount"], 100)
        self.assertEqual(order["plan_snapshot"]["days"], 30)
        self.assertEqual(order["plan_snapshot"]["name"], "یک ماهه")

    async def test_stock_out_does_not_charge_or_claim(self):
        await self.credit()
        order = await self.buy()
        with self.assertRaises(ValueError):
            await self.store.claim_order(order["id"], from_wallet=True)
        self.assertEqual((await self.store.get_user(100))["balance"], 1000)
        self.assertEqual((await self.store.get_order(order["id"]))["status"], "pending")

    async def test_insufficient_balance_rolls_back_inventory(self):
        await self.store.add_inventory(self.plan["id"], [CONFIG_A])
        order = await self.buy()
        with self.assertRaises(ValueError):
            await self.store.claim_order(order["id"], from_wallet=True)
        self.assertEqual(await self.store.inventory_count(self.plan["id"]), 1)
        self.assertEqual((await self.store.get_order(order["id"]))["status"], "pending")

    async def test_two_simultaneous_sales_cannot_share_last_item(self):
        await self.credit()
        await self.store.add_inventory(self.plan["id"], [CONFIG_A])
        orders = [await self.buy(), await self.buy()]
        claims = await asyncio.gather(*(self.store.claim_order(o["id"], from_wallet=True) for o in orders), return_exceptions=True)
        self.assertEqual(sum(isinstance(claim, dict) for claim in claims), 1)
        self.assertEqual((await self.store.get_user(100))["balance"], 900)
        self.assertEqual(await self.store.inventory_count(self.plan["id"]), 0)

    async def test_parallel_claims_debit_once_complete_once(self):
        await self.credit()
        await self.store.add_inventory(self.plan["id"], [CONFIG_A])
        order = await self.buy()
        claims = await asyncio.gather(*(self.store.claim_order(order["id"], from_wallet=True) for _ in range(15)))
        self.assertEqual(sum(claim is not None for claim in claims), 1)
        services = await asyncio.gather(*(self.store.complete_order(order["id"], CONFIG_A) for _ in range(15)))
        self.assertEqual(len({service["id"] for service in services}), 1)
        self.assertEqual((await self.store.get_user(100))["balance"], 900)
        self.assertEqual((await self.store.get_stats())["services"], 1)

    async def test_inventory_unique_across_plans_and_after_sale(self):
        second = await self.store.save_plan({"name": "other", "months": 2, "price": 200, "location_id": self.location_id})
        self.assertEqual(await self.store.add_inventory(self.plan["id"], [CONFIG_A, CONFIG_A]), 1)
        self.assertEqual(await self.store.add_inventory(second["id"], [CONFIG_A]), 0)
        order = await self.buy("card")
        await self.store.claim_order(order["id"])
        await self.store.complete_order(order["id"], CONFIG_A)
        self.assertEqual(await self.store.add_inventory(second["id"], [CONFIG_A]), 0)

    async def test_definite_failure_refunds_once_retry_charges_once(self):
        await self.credit()
        await self.store.add_inventory(self.plan["id"], [CONFIG_A])
        order = await self.buy()
        await self.store.claim_order(order["id"], from_wallet=True)
        await asyncio.gather(*(self.store.fail_order(order["id"], "known failure") for _ in range(5)))
        self.assertEqual((await self.store.get_user(100))["balance"], 1000)
        await self.store.claim_order(order["id"], from_wallet=True)
        await self.store.complete_order(order["id"], CONFIG_A)
        self.assertEqual((await self.store.get_user(100))["balance"], 900)

    async def test_uncertain_failure_preserves_payment_for_retry(self):
        await self.credit()
        await self.store.add_inventory(self.plan["id"], [CONFIG_A])
        order = await self.buy()
        first = await self.store.claim_order(order["id"], from_wallet=True)
        await self.store.fail_order(order["id"], "unknown", refund=False)
        self.assertEqual((await self.store.get_user(100))["balance"], 900)
        with self.assertRaises(ValueError):
            await self.store.reject_order(order["id"])
        retry = await self.store.claim_order(order["id"], from_wallet=True)
        self.assertEqual(retry["remote_key"], first["remote_key"])
        self.assertEqual((await self.store.get_user(100))["balance"], 900)

    async def test_restart_recovers_processing_without_losing_reservation_or_charge(self):
        await self.credit()
        await self.store.add_inventory(self.plan["id"], [CONFIG_A])
        order = await self.buy()
        await self.store.claim_order(order["id"], from_wallet=True)
        restarted = Store(self.store.path)
        await restarted.init()
        recovered = await restarted.get_order(order["id"])
        self.assertEqual(recovered["status"], "failed")
        self.assertTrue(recovered["wallet_debited"])
        self.assertEqual(await restarted.inventory_count(self.plan["id"]), 0)
        await restarted.claim_order(order["id"], from_wallet=True)
        self.assertEqual(await restarted.reserve_inventory(order["id"]), CONFIG_A)
        self.assertEqual((await restarted.get_user(100))["balance"], 900)

    async def test_manual_approval_delivery_and_cancel_are_idempotent(self):
        await self.credit()
        await self.store.save_plan({"id": self.plan["id"], "mode": "manual"})
        order = await self.buy()
        approvals = await asyncio.gather(*(self.store.approve_manual_order(order["id"], from_wallet=True) for _ in range(10)))
        self.assertTrue(all(o["status"] == "awaiting_config" for o in approvals))
        self.assertEqual((await self.store.get_user(100))["balance"], 900)
        self.assertIn(order["id"], [o["id"] for o in await self.store.pending_orders()])
        services = await asyncio.gather(*(self.store.complete_manual_order(order["id"], CONFIG_A) for _ in range(10)))
        self.assertEqual(len({service["id"] for service in services}), 1)
        self.assertFalse(await self.store.reject_order(order["id"]))
        self.assertFalse(await self.store.cancel_order(order["id"], 100))

    async def test_rejected_manual_wallet_refunds_but_approved_card_cannot_disappear(self):
        await self.credit()
        await self.store.save_plan({"id": self.plan["id"], "mode": "manual"})
        wallet = await self.buy()
        await self.store.approve_manual_order(wallet["id"], from_wallet=True)
        self.assertTrue(await self.store.reject_order(wallet["id"]))
        self.assertFalse(await self.store.reject_order(wallet["id"]))
        self.assertEqual((await self.store.get_user(100))["balance"], 1000)
        card = await self.buy("card")
        await self.store.approve_manual_order(card["id"])
        with self.assertRaises(ValueError):
            await self.store.reject_order(card["id"])

    async def test_delivery_outbox_and_service_owner(self):
        await self.store.add_inventory(self.plan["id"], [CONFIG_A])
        order = await self.buy("card")
        await self.store.claim_order(order["id"])
        service = await self.store.complete_order(order["id"], CONFIG_A)
        self.assertIsNone(await self.store.get_service(service["id"], 999))
        self.assertEqual((await self.store.service_for_order(order["id"]))["id"], service["id"])
        self.assertEqual([o["id"] for o in await self.store.undelivered_orders()], [order["id"]])
        await self.store.mark_delivered(order["id"])
        self.assertEqual(await self.store.undelivered_orders(), [])

    async def test_blocked_user_cannot_buy_or_claim(self):
        order = await self.buy("card")
        await self.store.set_user_blocked(100, True)
        with self.assertRaises(ValueError):
            await self.buy()
        with self.assertRaises(ValueError):
            await self.store.claim_order(order["id"])

    async def test_invalid_configuration_and_archive(self):
        for invalid in ("", "not a config", "http://", "vless://abc@example.test:443", CONFIG_A + "\n" + CONFIG_B):
            with self.assertRaises(ValueError):
                validate_config(invalid)
        self.assertEqual(validate_config("http://example.test/sub"), "http://example.test/sub")
        self.assertEqual(validate_config(CONFIG_A), CONFIG_A)
        await self.store.archive_plan(self.plan["id"])
        self.assertEqual(await self.store.list_plans(), [])
        self.assertIsNotNone(await self.store.get_plan(self.plan["id"]))
        with self.assertRaises(ValueError):
            await self.buy()


if __name__ == "__main__":
    unittest.main()
