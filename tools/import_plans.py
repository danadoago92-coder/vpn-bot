#!/usr/bin/env python3
"""Import legacy PLANS dict/list using literal parsing, never execute old config."""
import argparse
import ast
import asyncio
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import load_settings
from database import Store
from tools.manage_data import exclusive_bot_lock, backup_database


def read_plans(path):
    source=Path(path).read_text(encoding="utf-8")
    if Path(path).suffix==".json":
        value=json.loads(source)
    else:
        value=None
        for node in ast.parse(source).body:
            if isinstance(node,ast.Assign) and any(isinstance(t,ast.Name) and t.id=="PLANS" for t in node.targets):
                value=ast.literal_eval(node.value)
    if isinstance(value,dict):
        return [{"id":key,**plan} for key,plan in value.items()]
    if not isinstance(value,list):
        raise ValueError("PLANS باید دیکشنری یا لیست literal باشد؛ فایل قدیمی اجرا نمی‌شود.")
    return value


async def import_plans(store,plans):
    existing={p.get("legacy_id"):p for p in await store.list_plans(active_only=False)}
    routes=[]
    for location in await store.list_locations(active_only=True):
        routes.extend((location["id"], item["protocol"]) for item in await store.location_protocols(location["id"], active_only=True))
    if not routes:
        raise ValueError("پیش از ورود پلن‌های قدیمی، در پنل ادمین یک لوکیشن و حداقل یک پروتکل فعال بسازید.")
    location_id, protocol = routes[0]
    added=0
    for plan in plans:
        if int(plan.get("days",30)) not in (30,60) or float(plan.get("price",0))<=0:
            continue
        legacy_id=str(plan.get("id") or plan["name"])
        if legacy_id in existing:
            continue
        await store.save_plan({"name":plan["name"],"months":int(plan.get("days",30))//30,"price":int(plan["price"]),"traffic_gb":max(0,float(plan.get("traffic_gb",0))),"mode":"manual","active":False,"legacy_id":legacy_id,"location_id":location_id,"protocol":protocol})
        existing[legacy_id]=True
        added+=1
    return added


def main():
    parser=argparse.ArgumentParser(description="ورود پلن‌های قبلی بدون اجرای کد خصوصی")
    parser.add_argument("source",type=Path)
    parser.add_argument("--db",type=Path)
    args=parser.parse_args()
    path=args.db or load_settings(require_token=False).db_path
    plans=read_plans(args.source)
    with exclusive_bot_lock(path):
        if path.exists():
            backup_database(path,path.parent/"backups")
        async def run():
            store=Store(path)
            await store.init()
            print(f"{await import_plans(store,plans)} پلن به‌صورت غیرفعال وارد شد؛ در پنل ادمین بررسی و فعال کنید.")
        asyncio.run(run())


if __name__=="__main__":
    main()
