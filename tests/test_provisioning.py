import asyncio
import copy
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from database import Store
from provisioning import Provisioner, ProvisioningError, UncertainProvisioningError, XUIAdapter, _PanelHTTPError

CONFIG = "https://sub.example.test/sub/manual-test"


def settings():
    return SimpleNamespace(xui_url="https://panel.example.test/secret-path", xui_username="test-user",
                           xui_password="test-password", xui_sub_url="https://sub.example.test/sub/{sub_id}",
                           xui_verify_tls=True)


class FakePanel(XUIAdapter):
    """In-memory panel transport: never opens a socket."""
    def __init__(self, *, behavior="success", legacy=False, protocol="vless"):
        super().__init__(settings())
        self.clients = []
        self.calls = []
        self.behavior = behavior
        self.legacy = legacy
        self.protocol = protocol
        self.post_count = 0

    async def _authenticated(self, method, path, *, payload=None):
        self.calls.append((method, path, copy.deepcopy(payload)))
        await asyncio.sleep(0)
        if method == "GET":
            return {"success": True, "obj": {"id": 1, "enable": True, "protocol": self.protocol,
                    "settings": json.dumps({"clients": self.clients})}}
        if self.legacy and path == "/panel/api/clients/add":
            raise _PanelHTTPError(404)
        self.post_count += 1
        client = payload["client"] if "client" in payload else json.loads(payload["settings"])["clients"][0]
        if self.behavior == "reject":
            return {"success": False, "msg": "private panel details should not be propagated"}
        if self.behavior == "timeout_absent":
            raise asyncio.TimeoutError("private address must not escape")
        self.clients.append({**copy.deepcopy(client),"id":client.get("uuid",client.get("id"))})
        if self.behavior == "timeout_committed":
            raise asyncio.TimeoutError("private address must not escape")
        return {"success": True}


class ProvisioningTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "bot.sqlite3")
        await self.store.init()
        self.location_id = await self.store.save_location("🌍 تست", kind="multi")
        await self.store.set_location_protocol(self.location_id, "v2ray", True)
        await self.store.upsert_user(100)
        self.plan = await self.store.save_plan({"name": "Monthly", "months": 1, "price": 100, "traffic_gb": 20, "mode": "xui", "inbound_id": 1, "location_id": self.location_id})
        topup = await self.store.create_order(100, amount=1000, kind="topup")
        await self.store.attach_receipt(topup["id"], 100, "receipt")
        await self.store.approve_topup(topup["id"])
        await self.store.mark_delivered(topup["id"])
        self.provisioner = Provisioner(settings(), self.store)
        self.panel = FakePanel()
        self.provisioner.xui = self.panel

    async def asyncTearDown(self):
        await self.provisioner.close()
        await self.store.close()
        self.temp.cleanup()

    async def order(self, method="wallet"):
        order = await self.store.create_order(100, self.plan["id"], method=method)
        if method == "card":
            await self.store.attach_receipt(order["id"], 100, "receipt")
        return order

    async def test_simultaneous_approval_creates_one_remote_client_and_service(self):
        order = await self.order()
        services = await asyncio.gather(*(self.provisioner.fulfill(order["id"], from_wallet=True) for _ in range(12)))
        self.assertEqual(len({s["id"] for s in services}), 1)
        self.assertEqual(self.panel.post_count, 1)
        self.assertEqual((await self.store.get_user(100))["balance"], 900)
        self.assertTrue(services[0]["config_text"].startswith("https://sub.example.test/sub/"))

    async def test_committed_timeout_is_recovered_by_readback(self):
        self.panel.behavior = "timeout_committed"
        order = await self.order()
        service = await self.provisioner.fulfill(order["id"])
        self.assertEqual(service["order_id"], order["id"])
        self.assertEqual(self.panel.post_count, 1)
        self.assertEqual((await self.store.get_order(order["id"]))["status"], "paid")
        self.assertEqual((await self.store.get_user(100))["balance"], 900)

    async def test_unresolved_timeout_keeps_funds_then_retry_uses_same_identity(self):
        self.panel.behavior = "timeout_absent"
        order = await self.order()
        with self.assertRaises(UncertainProvisioningError):
            await self.provisioner.fulfill(order["id"])
        failed = await self.store.get_order(order["id"])
        self.assertEqual(failed["status"], "failed")
        self.assertTrue(failed["wallet_debited"])
        self.assertEqual((await self.store.get_user(100))["balance"], 900)
        first_payload = next(c[2]["client"] for c in self.panel.calls if c[0] == "POST")
        self.panel.behavior = "success"
        service = await self.provisioner.fulfill(order["id"])
        self.assertEqual(service["remote_id"], first_payload["email"])
        self.assertEqual(self.panel.clients[0]["id"], first_payload.get("uuid",first_payload.get("id")))
        self.assertEqual(self.panel.clients[0]["expiryTime"], first_payload["expiryTime"])
        self.assertEqual((await self.store.get_user(100))["balance"], 900)

    async def test_definite_panel_rejection_refunds_wallet_and_redacts_response(self):
        self.panel.behavior = "reject"
        order = await self.order()
        with self.assertRaises(ProvisioningError) as raised:
            await self.provisioner.fulfill(order["id"])
        self.assertNotIn("private", str(raised.exception))
        self.assertEqual((await self.store.get_user(100))["balance"], 1000)
        self.assertFalse((await self.store.get_order(order["id"]))["remote_uncertain"])
        self.panel.behavior = "success"
        await self.provisioner.fulfill(order["id"])
        self.assertEqual((await self.store.get_user(100))["balance"], 900)

    async def test_prior_ambiguity_is_not_refunded_on_later_read_error(self):
        self.panel.behavior = "timeout_absent"
        order = await self.order()
        with self.assertRaises(ProvisioningError):
            await self.provisioner.fulfill(order["id"])
        self.panel.ensure_client = AsyncMock(side_effect=ProvisioningError("پنل در دسترس نیست"))
        with self.assertRaises(ProvisioningError):
            await self.provisioner.fulfill(order["id"])
        self.assertEqual((await self.store.get_user(100))["balance"], 900)
        with self.assertRaises(ValueError):
            await self.store.reject_order(order["id"])

    async def test_database_failure_after_remote_creation_does_not_refund_or_duplicate(self):
        order = await self.order()
        original = self.store.complete_order
        self.store.complete_order = AsyncMock(side_effect=RuntimeError("database temporarily unavailable"))
        with self.assertRaises(ValueError):
            await self.provisioner.fulfill(order["id"])
        self.assertEqual((await self.store.get_user(100))["balance"], 900)
        self.assertTrue((await self.store.get_order(order["id"]))["remote_uncertain"])
        self.store.complete_order = original
        service = await self.provisioner.fulfill(order["id"])
        self.assertEqual(service["order_id"], order["id"])
        self.assertEqual(self.panel.post_count, 1)
        self.assertEqual((await self.store.get_user(100))["balance"], 900)

    async def test_modern_api_falls_back_only_when_endpoint_missing(self):
        self.panel.legacy = True
        order = await self.order()
        await self.provisioner.fulfill(order["id"])
        paths = [call[1] for call in self.panel.calls if call[0] == "POST"]
        self.assertEqual(paths, ["/panel/api/clients/add", "/panel/api/inbounds/addClient"])
        self.assertEqual(self.panel.post_count, 1)

    async def test_modern_rejection_does_not_retry_a_different_write_endpoint(self):
        self.panel.behavior = "reject"
        order = await self.order()
        with self.assertRaises(ProvisioningError):
            await self.provisioner.fulfill(order["id"])
        self.assertNotIn("/panel/api/inbounds/addClient", [c[1] for c in self.panel.calls])

    async def test_manual_mode_needs_no_panel_and_waits_for_admin_config(self):
        await self.store.save_plan({"id": self.plan["id"], "mode": "manual"})
        order = await self.order()
        self.panel.ensure_client = AsyncMock(side_effect=AssertionError("manual should never call panel"))
        result = await self.provisioner.fulfill(order["id"])
        self.assertTrue(result["awaiting_config"])
        self.assertEqual((await self.store.get_order(order["id"]))["status"], "awaiting_config")
        self.assertEqual(await self.store.list_services(100), [])
        await self.store.complete_manual_order(order["id"], CONFIG)
        delivered = await self.provisioner.fulfill(order["id"])
        self.assertEqual(delivered["config_text"], CONFIG)
        self.assertEqual((await self.store.get_user(100))["balance"], 900)

    async def test_inventory_mode_does_not_contact_panel(self):
        await self.store.save_plan({"id": self.plan["id"], "mode": "inventory"})
        await self.store.add_inventory(self.plan["id"], [CONFIG])
        order = await self.order()
        service = await self.provisioner.fulfill(order["id"])
        self.assertEqual(service["config_text"], CONFIG)
        self.assertEqual(self.panel.calls, [])

    async def test_card_timeout_cannot_be_rejected_before_reconciliation(self):
        self.panel.behavior = "timeout_absent"
        order = await self.order("card")
        with self.assertRaises(ProvisioningError):
            await self.provisioner.fulfill(order["id"])
        with self.assertRaises(ValueError):
            await self.store.reject_order(order["id"])
        self.assertEqual((await self.store.get_user(100))["balance"], 1000)

    async def test_unconfigured_panel_refunds_without_network(self):
        self.panel.settings.xui_password = ""
        order = await self.order()
        with self.assertRaises(ProvisioningError):
            await self.provisioner.fulfill(order["id"])
        self.assertEqual(self.panel.calls, [])
        self.assertEqual((await self.store.get_user(100))["balance"], 1000)

    async def test_two_fresh_databases_do_not_reuse_remote_identity(self):
        first = await self.order()
        other = Store(Path(self.temp.name) / "other.sqlite3")
        await other.init()
        location = await other.save_location("🌍 تست", kind="multi")
        await other.set_location_protocol(location, "v2ray", True)
        await other.upsert_user(100)
        plan = await other.save_plan({"name": "Monthly", "months": 1, "price": 100, "location_id": location})
        second = await other.create_order(100, plan["id"])
        self.assertNotEqual(self.panel.identity(first)["id"], self.panel.identity(second)["id"])

    async def test_api_token_skips_password_login(self):
        config=settings()
        config.xui_api_token='synthetic-api-token'
        config.xui_username=config.xui_password=''
        adapter=XUIAdapter(config)
        adapter._validate_settings()
        adapter._login=AsyncMock(side_effect=AssertionError('token auth must not login'))
        adapter._request=AsyncMock(return_value={'success':True,'obj':{}})
        await adapter._authenticated('GET','/panel/api/inbounds/get/1')
        adapter._login.assert_not_awaited()
        adapter._request.assert_awaited_once()

    async def test_expired_cookie_redirect_reauthenticates_once(self):
        adapter=XUIAdapter(settings())
        adapter._login=AsyncMock()
        adapter._request=AsyncMock(side_effect=[_PanelHTTPError(302),{'success':True}])
        self.assertEqual(await adapter._authenticated('GET','/panel/api/inbounds/get/1'),{'success':True})
        self.assertEqual(adapter._login.await_count,2)


if __name__ == "__main__":
    unittest.main()
