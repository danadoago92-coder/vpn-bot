"""Additive import: legacy tables and every original row remain untouched."""
from datetime import datetime, timezone
import json
import uuid


def migrate_legacy(conn):
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    columns = {r[1] for r in conn.execute("PRAGMA table_info(users)")} if "users" in tables else set()
    if "user_id" not in columns or conn.execute("SELECT 1 FROM shop_settings WHERE key='_legacy_imported'").fetchone():
        return
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("INSERT OR IGNORE INTO shop_locations(id,name,kind,active) VALUES('legacy_multi','🌍 مولتی‌لوکیشن قدیمی','multi',0)")
        conn.execute("INSERT OR IGNORE INTO shop_location_protocols(location_id,protocol,active) VALUES('legacy_multi','v2ray',1)")
        for row in conn.execute("SELECT * FROM users").fetchall():
            user = dict(row)
            conn.execute("INSERT OR IGNORE INTO shop_users(id,username,full_name,balance,blocked,created_at) VALUES(?,?,?,?,?,?)",
                (user["user_id"],user.get("username") or "",user.get("full_name") or "",user.get("balance") or 0,user.get("is_blocked") or 0,user.get("joined_at") or now))
        plans = {}
        if "service_plans" in tables:
            for row in conn.execute("SELECT * FROM service_plans").fetchall():
                plan = dict(row)
                old_id = str(plan["id"])
                protocol = "v2ray"
                days = int(plan.get("days") or 30)
                months = 2 if days==60 else 1
                active = bool(plan.get("is_active",1) and days in (30,60) and (plan.get("price") or 0)>0)
                pid=conn.execute("INSERT INTO shop_plans(name,months,days,traffic_gb,price,mode,inbound_id,active,description,location_id,protocol,legacy_id) VALUES(?,?,?,?,?,'manual',0,?,?,'legacy_multi',?,?)",
                    (plan.get("name") or old_id,months,months*30,max(0,float(plan.get("traffic_gb") or 0)),max(1,int(plan.get("price") or 0)),int(active),"پلن منتقل‌شده؛ روش تحویل و مشخصات را پیش از فروش بررسی کنید.",protocol,old_id)).lastrowid
                plans[old_id]=dict(conn.execute("SELECT * FROM shop_plans WHERE id=?",(pid,)).fetchone())
        if "orders" in tables:
            for row in conn.execute("SELECT * FROM orders").fetchall():
                order=dict(row)
                if not conn.execute("SELECT 1 FROM shop_users WHERE id=?",(order["user_id"],)).fetchone():
                    continue
                plan=plans.get(str(order.get("plan_id")))
                snapshot=dict(plan or {"name":order.get("plan_name") or str(order.get("plan_id") or "سفارش قبلی"),"months":1,"days":order.get("plan_days") or 30,"traffic_gb":max(0,order.get("plan_traffic_gb") or 0),"mode":"manual","protocol":"v2ray","location_id":"legacy_multi"})
                status=order.get("status")
                if status not in ("paid","rejected","cancelled"):
                    status="legacy_review"
                conn.execute("INSERT OR IGNORE INTO shop_orders(id,user_id,kind,method,plan_id,amount,status,receipt_file_id,plan_snapshot,remote_key,created_at,updated_at,error,delivered_at,wallet_debited,approved_at,remote_uncertain) VALUES(?,?,'purchase',?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (order["id"],order["user_id"],"wallet" if order.get("payment_method")=="wallet" else "card",plan["id"] if plan else None,max(0,int(order.get("amount") or 0)),status,order.get("receipt_file_id") or "",json.dumps(snapshot,ensure_ascii=False),uuid.uuid4().hex,order.get("created_at") or now,now,"سفارش نسخهٔ قبلی؛ پیش از هر اقدام، وضعیت پرداخت و سرویس قبلی را تطبیق دهید." if status=="legacy_review" else "",now if status=="paid" else None,int(bool(order.get("wallet_debited") and not order.get("wallet_refunded"))),order.get("paid_at"),int(status=="legacy_review")))
        if "subscriptions" in tables:
            for row in conn.execute("SELECT * FROM subscriptions").fetchall():
                sub=dict(row)
                if sub.get("is_test") or not conn.execute("SELECT 1 FROM shop_users WHERE id=?",(sub["user_id"],)).fetchone():
                    continue
                subscription_url = str(sub.get("sub_url") or "").strip()
                if not subscription_url.startswith(("https://", "http://")):
                    continue
                order=conn.execute("SELECT * FROM shop_orders WHERE id=?",(sub.get("order_id"),)).fetchone()
                # Group subscriptions each retain a separate service, even with one old order.
                used=order and conn.execute("SELECT 1 FROM shop_services WHERE order_id=?",(order["id"],)).fetchone()
                plan=plans.get(str(sub.get("plan_id")),{})
                protocol="v2ray"
                if not order or used:
                    oid=conn.execute("INSERT INTO shop_orders(user_id,kind,method,amount,status,plan_snapshot,remote_key,created_at,updated_at,delivered_at,error) VALUES(?,'purchase','card',0,'archived',?,?,?,?,?,?)",
                        (sub["user_id"],json.dumps({"name":plan.get("name","سرویس قبلی"),"mode":"manual","protocol":protocol}),uuid.uuid4().hex,sub.get("start_date") or now,now,now,f"بایگانی سرویس #{sub['id']} از سفارش قبلی {sub.get('order_id')}")).lastrowid
                else:
                    oid=order["id"]
                conn.execute("INSERT INTO shop_services(user_id,order_id,plan_name,months,traffic_gb,config_text,remote_id,created_at,expires_at,protocol) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (sub["user_id"],oid,plan.get("name") or sub.get("display_name") or "سرویس قبلی",plan.get("months",1),max(0,float(sub.get("traffic_gb") or 0)),subscription_url,sub.get("remote_id") or sub.get("xui_email") or "",sub.get("start_date") or now,sub.get("expire_date"),protocol))
        conn.execute("INSERT INTO shop_settings(key,value) VALUES('_legacy_imported','true')")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
