"""Idempotent delivery through manual approval, inventory, or optional 3x-ui.

3x-ui API reference: https://docs.sanaei.dev/docs/reference/api/clients/
No panel response, endpoint, credential or subscription is written to logs.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
from urllib.parse import quote, urlsplit
import uuid
import weakref

import aiohttp

from database import validate_config


class ProvisioningError(ValueError):
    """Known failure before any remote client could be created."""


class UncertainProvisioningError(ProvisioningError):
    """A client may exist: preserve payment until an idempotent retry reconciles it."""


class _PanelHTTPError(Exception):
    def __init__(self, status):
        self.status = status


class XUIAdapter:
    def __init__(self, settings):
        self.settings = settings
        self._session = None
        self._login_lock = asyncio.Lock()
        self._logged_in = False

    def _validate_settings(self):
        if not self.settings.xui_url or not self.settings.xui_sub_url or not (getattr(self.settings,"xui_api_token","") or (self.settings.xui_username and self.settings.xui_password)):
            raise ProvisioningError("اتصال پنل کامل تنظیم نشده؛ با پشتیبانی تماس بگیرید.")
        try:
            base = urlsplit(self.settings.xui_url)
            sub = urlsplit(self.settings.xui_sub_url.replace("{sub_id}", "test"))
            if base.scheme not in ("https", "http") or not base.hostname or base.username or base.password or base.query or base.fragment:
                raise ValueError
            if sub.scheme not in ("http", "https") or not sub.hostname or sub.username or sub.password:
                raise ValueError
        except ValueError:
            raise ProvisioningError("آدرس پنل یا آدرس اشتراک معتبر نیست.") from None

    async def _get_session(self):
        if self._session is None or self._session.closed:
            # CookieJar otherwise refuses cookies from an administrator's IP URL.
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=25, connect=8),
                cookie_jar=aiohttp.CookieJar(unsafe=True),
                connector=aiohttp.TCPConnector(limit=10, ssl=self.settings.xui_verify_tls),
                headers={"Accept": "application/json", **({"Authorization":"Bearer "+self.settings.xui_api_token} if getattr(self.settings,"xui_api_token","") else {})},
            )
        return self._session

    async def _request(self, method, path, *, payload=None):
        session = await self._get_session()
        async with session.request(method, self.settings.xui_url.rstrip("/") + path,
                                   json=payload, allow_redirects=False) as response:
            if response.status >= 300:
                raise _PanelHTTPError(response.status)
            if response.content_length and response.content_length > 8 * 1024 * 1024:
                raise ProvisioningError("پاسخ پنل بیش از حد بزرگ است.")
            chunks, size = [], 0
            async for chunk in response.content.iter_chunked(65536):
                size += len(chunk)
                if size > 8 * 1024 * 1024:
                    raise ProvisioningError("پاسخ پنل بیش از حد بزرگ است.")
                chunks.append(chunk)
            raw = b"".join(chunks)
            try:
                data = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                raise ProvisioningError("پاسخ پنل معتبر نیست؛ تنظیمات اتصال را بررسی کنید.") from None
            if not isinstance(data, dict) or not isinstance(data.get("success"), bool):
                raise ProvisioningError("پاسخ پنل معتبر نیست؛ تنظیمات اتصال را بررسی کنید.")
            return data

    async def _login(self):
        async with self._login_lock:
            if self._logged_in:
                return
            try:
                result = await self._request("POST", "/login", payload={"username": self.settings.xui_username, "password": self.settings.xui_password})
                if not result["success"]:
                    raise ProvisioningError("ورود به پنل ناموفق بود؛ مدیر اطلاعات اتصال را بررسی کند.")
                self._logged_in = True
            except (aiohttp.ClientError, asyncio.TimeoutError, _PanelHTTPError):
                raise ProvisioningError("اتصال امن به پنل برقرار نشد؛ مدیر تنظیمات را بررسی کند.") from None

    async def _authenticated(self, method, path, *, payload=None):
        if getattr(self.settings,"xui_api_token",""):
            return await self._request(method,path,payload=payload)
        await self._login()
        try:
            return await self._request(method, path, payload=payload)
        except _PanelHTTPError as exc:
            if exc.status not in (401, 403, 302, 307):
                raise
            self._logged_in = False
            await self._login()
            return await self._request(method, path, payload=payload)

    @staticmethod
    def identity(order):
        key = order["remote_key"]
        return {"id": str(uuid.UUID(hex=key)), "email": f"vpn-order-{order['id']}-{key[:12]}", "subId": key[8:24]}

    async def _inbound(self, inbound_id):
        try:
            response = await self._authenticated("GET", f"/panel/api/inbounds/get/{int(inbound_id)}")
            if not response["success"] or not isinstance(response.get("obj"), dict):
                raise ProvisioningError("اینباند پلن در پنل پیدا نشد یا در دسترس نیست.")
            inbound = response["obj"]
            settings = inbound.get("settings", {})
            if isinstance(settings, str):
                settings = json.loads(settings)
            if not isinstance(settings, dict) or not isinstance(settings.get("clients", []), list):
                raise ValueError
            inbound["settings"] = settings
            return inbound
        except ProvisioningError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError, _PanelHTTPError, TypeError, ValueError):
            raise ProvisioningError("خواندن اطلاعات پنل ممکن نشد؛ دوباره تلاش کنید.") from None

    @staticmethod
    def _find(inbound, identity):
        for client in inbound["settings"].get("clients", []):
            if not isinstance(client, dict):
                continue
            if client.get("email") == identity["email"]:
                if client.get("subId") != identity["subId"]:
                    raise UncertainProvisioningError("شناسه سرویس در پنل تطابق ندارد؛ مدیر باید سفارش را بررسی کند.")
                actual_id = client.get("id") or client.get("uuid")
                if actual_id and inbound.get("protocol") in ("vless", "vmess") and actual_id != identity["id"]:
                    raise UncertainProvisioningError("شناسه سرویس در پنل تطابق ندارد؛ مدیر باید سفارش را بررسی کند.")
                return client
        return None

    def _result(self, client, identity):
        token = quote(identity["subId"], safe="")
        url = self.settings.xui_sub_url
        url = url.replace("{sub_id}", token) if "{sub_id}" in url else url.rstrip("/") + "/" + token
        try:
            expiry_ms = int(client.get("expiryTime", 0))
            expiry = datetime.fromtimestamp(expiry_ms / 1000, timezone.utc).isoformat(timespec="seconds") if expiry_ms > 0 else None
        except (ValueError, TypeError, OverflowError, OSError):
            raise UncertainProvisioningError("تاریخ سرویس ساخته‌شده معتبر نیست؛ مدیر پنل را بررسی کند.") from None
        return {"config_text": validate_config(url), "remote_id": identity["email"], "expires_at": expiry}

    async def ensure_client(self, order):
        self._validate_settings()
        plan, identity = order["plan_snapshot"], self.identity(order)
        inbound = await self._inbound(plan["inbound_id"])
        existing = self._find(inbound, identity)
        if existing:
            return self._result(existing, identity)
        if inbound.get("protocol") not in ("vless", "vmess", "trojan"):
            raise ProvisioningError("ساخت خودکار فقط برای VLESS، VMess و Trojan فعال است؛ برای این اینباند از تحویل دستی استفاده کنید.")
        if inbound.get("enable") is False:
            raise ProvisioningError("اینباند انتخاب‌شده غیرفعال است.")
        created = datetime.fromisoformat(order["approved_at"] or order["created_at"])
        expiry = int((created + timedelta(days=plan["days"])).timestamp() * 1000)
        client = dict(identity, password=order["remote_key"], flow="", alterId=0,
                      totalGB=int(plan["traffic_gb"] * 1024**3), expiryTime=expiry,
                      enable=True, tgId=str(order["user_id"]), limitIp=0, reset=0)
        # Modern 3x-ui API; old releases return 404/405, permitting a safe legacy fallback.
        rejected = False
        try:
            try:
                result = await self._authenticated("POST", "/panel/api/clients/add", payload={"client": {**{k:v for k,v in client.items() if k != "id"},"uuid":identity["id"]}, "inboundIds": [plan["inbound_id"]]})
            except _PanelHTTPError as exc:
                if exc.status not in (404, 405):
                    raise
                result = await self._authenticated("POST", "/panel/api/inbounds/addClient", payload={"id": plan["inbound_id"], "settings": json.dumps({"clients": [client]})})
            rejected = not result["success"]
        except (aiohttp.ClientError, asyncio.TimeoutError, _PanelHTTPError, ProvisioningError):
            # A timeout/error after POST cannot prove the server did not commit.
            pass
        # Read back even after success, recovering a completed POST whose reply was lost.
        try:
            confirmed = self._find(await self._inbound(plan["inbound_id"]), identity)
        except (ProvisioningError, ValueError):
            raise UncertainProvisioningError("نتیجه ساخت سرویس هنوز مشخص نیست؛ وجه محفوظ است و مدیر باید دوباره تلاش کند.") from None
        if confirmed:
            return self._result(confirmed, identity)
        if rejected:
            raise ProvisioningError("پنل ساخت سرویس را نپذیرفت؛ تنظیمات پلن را بررسی کنید.")
        raise UncertainProvisioningError("نتیجه ساخت سرویس هنوز مشخص نیست؛ وجه محفوظ است و مدیر باید دوباره تلاش کند.")

    async def close(self):
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._logged_in = False


class Provisioner:
    def __init__(self, settings, store):
        self.settings = settings
        self.store = store
        self.xui = XUIAdapter(settings)
        self._locks = weakref.WeakValueDictionary()

    def _lock_for(self, order_id):
        lock = self._locks.get(order_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[order_id] = lock
        return lock

    async def fulfill(self, order_id, from_wallet=False):
        async with self._lock_for(order_id):
            order = await self.store.get_order(order_id)
            if order is None or order["kind"] != "purchase":
                raise ValueError("سفارش خرید پیدا نشد.")
            if from_wallet and order["method"] != "wallet":
                raise ValueError("روش پرداخت سفارش کیف پول نیست.")
            if order["status"] == "paid":
                service = await self.store.service_for_order(order_id)
                if service:
                    return service
                raise ValueError("سرویس سفارش پیدا نشد؛ مدیر پایگاه داده را بررسی کند.")
            values = await self.store.get_settings()
            default_panel = getattr(self.settings,"panel_connected",None)
            if default_panel is None:
                default_panel = bool(self.settings.xui_url)
            connected = values.get("panel_connected", str(bool(default_panel)).lower()) == "true"
            order = await self.store.prepare_delivery(order_id, connected)
            # Admin retries infer the method from durable state, not the callback.
            from_wallet = order["method"] == "wallet"
            if order["plan_snapshot"]["mode"] == "manual":
                order = await self.store.approve_manual_order(order_id, from_wallet=from_wallet)
                return {"awaiting_config": True, "order_id": order_id, "status": order["status"]}
            order = await self.store.claim_order(order_id, from_wallet=from_wallet)
            if order is None:
                raise ValueError("این سفارش در حال پردازش است؛ کمی بعد دوباره بررسی کنید.")
            remote_started = False
            try:
                if order["plan_snapshot"]["mode"] == "inventory":
                    config_text = await self.store.reserve_inventory(order_id)
                    if not config_text:
                        raise ProvisioningError("موجودی رزروشده سفارش پیدا نشد.")
                    return await self.store.complete_order(order_id, config_text)
                remote_started = True
                result = await self.xui.ensure_client(order)
                # If storing a confirmed remote client fails, keep funds reserved.
                remote_started = True
                return await self.store.complete_order(order_id, **result)
            except asyncio.CancelledError:
                await asyncio.shield(self.store.fail_order(order_id, "عملیات نیمه‌تمام ماند؛ مدیر باید دوباره تلاش کند.", refund=not remote_started and not order["remote_uncertain"]))
                raise
            except ProvisioningError as exc:
                uncertain = isinstance(exc, UncertainProvisioningError) or bool(order["remote_uncertain"])
                await self.store.fail_order(order_id, str(exc), refund=not uncertain)
                raise
            except Exception:
                message = "تحویل کامل نشد؛ سفارش ذخیره شده و مدیر باید دوباره تلاش کند."
                await self.store.fail_order(order_id, message, refund=not remote_started and not order["remote_uncertain"])
                raise ValueError(message) from None

    async def close(self):
        await self.xui.close()
