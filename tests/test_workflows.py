"""Cross-module business workflows with real SQLite and a fake Telegram transport."""
import asyncio
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from config import Settings
from database import Store
from admins import write_admins, admin_ids
from bot import acquire_instance_lock, dispatch
from handlers import customer, admin, catalog_admin, start, buy, my_subs, wallet
from provisioning import Provisioner
from purchase import notify_service
from tools.import_plans import read_plans, import_plans
import ui
from ui import cancel_cleanup_tasks, keep_message, main_rows, reply_menu, touch_cleanup, track_transient


class WorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.store=Store(self.root/'bot.sqlite3')
        await self.store.init()
        self.location_id=await self.store.save_location('🌍 مولتی آزمایشی',kind='multi')
        await self.store.set_location_protocol(self.location_id,'v2ray',True)
        self.settings=Settings(admin_ids=(10,),card_number='1234567890123456',card_holder='آزمایشی',panel_connected=False)
        self.provisioner=Provisioner(self.settings,self.store)
        self.bot=SimpleNamespace(send_message=AsyncMock(),send_document=AsyncMock(),send_photo=AsyncMock(),delete_message=AsyncMock())
        self.shared={'settings':self.settings,'store':self.store,'provisioner':self.provisioner}
        self.ctx=SimpleNamespace(bot_data=self.shared,user_data={},bot=self.bot)
        self.admin_ctx=SimpleNamespace(bot_data=self.shared,user_data={},bot=self.bot)
        self.output=AsyncMock()
        self.patches=[patch.object(mod,'send',self.output) for mod in (customer,admin,catalog_admin,start,buy,my_subs,wallet)]
        for p in self.patches:p.start()
        await self.store.upsert_user(20,'buyer','خریدار')
        await self.store.upsert_user(21,'second','خریدار دوم')
        self.plan=await self.store.save_plan({'name':'مولتی یک‌ماهه','months':1,'price':100000,'traffic_gb':20,'location_id':self.location_id})

    async def asyncTearDown(self):
        for p in self.patches:p.stop()
        await cancel_cleanup_tasks()
        await self.provisioner.close()
        self.temp.cleanup()

    def update(self, data=None, text=None, uid=20, document=None, photo=None):
        msg=SimpleNamespace(text=text,chat_id=uid,message_id=50,document=document,photo=photo,reply_to_message=None,
            reply_text=AsyncMock(),reply_photo=AsyncMock(),reply_document=AsyncMock())
        query=SimpleNamespace(data=data,message=msg,answer=AsyncMock(),delete_message=AsyncMock(),edit_message_reply_markup=AsyncMock()) if data else None
        return SimpleNamespace(callback_query=query,effective_user=SimpleNamespace(id=uid,username='user',full_name='خریدار',first_name='دوست'),effective_chat=SimpleNamespace(id=uid,type='private'),effective_message=msg,message=msg)

    def buttons(self):
        return [b for row in self.output.await_args.args[2] for b in row]

    async def test_admin_defined_location_protocol_and_two_durations(self):
        await customer.callback(self.update('buy'),self.ctx)
        self.assertEqual([b.text for b in self.buttons() if b.callback_data == f'location:{self.location_id}'],['🌍 مولتی آزمایشی'])
        await customer.callback(self.update(f'location:{self.location_id}'),self.ctx)
        self.assertTrue(any(b.callback_data == f'protocol:{self.location_id}:v2ray' for b in self.buttons()))
        await customer.callback(self.update(f'protocol:{self.location_id}:v2ray'),self.ctx)
        self.assertEqual([b.callback_data for b in self.buttons() if b.callback_data.startswith('duration:')],[f'duration:{self.location_id}:v2ray:1:0',f'duration:{self.location_id}:v2ray:2:0'])

    async def test_main_keyboard_is_small_and_uses_requested_labels(self):
        labels=[button.text for row in main_rows(False) for button in row]
        self.assertEqual(labels,[
            '🔐 خرید اشتراک','♻️ تمدید سرویس','🛍 سرویس‌های من',
            '🏦 کیف پول','📚 آموزش اتصال','☎️ پشتیبانی',
        ])
        markup=reply_menu(False)
        self.assertTrue(markup.is_persistent)
        self.assertEqual(len(markup.keyboard),3)
        self.assertFalse(markup.keyboard[0][0].api_kwargs)
        self.assertFalse(markup.keyboard[2][1].api_kwargs)

    async def test_back_home_deletes_previous_inline_section_before_sticker(self):
        update=self.update('home')
        await customer.callback(update,self.ctx)
        self.bot.delete_message.assert_awaited_once_with(chat_id=20,message_id=50)
        update.callback_query.delete_message.assert_not_awaited()
        self.bot.send_message.assert_awaited_once()

    async def test_back_from_android_to_tutorial_menu_does_not_send_new_sticker(self):
        with patch.object(start,'show_sticker',new_callable=AsyncMock) as sticker:
            await start.tutorial(self.update('tutorial'),self.ctx)
            sticker.assert_not_awaited()
            await start.tutorial(self.update(),self.ctx)
            sticker.assert_awaited_once()

    async def test_reply_keyboard_switch_removes_old_screen_and_trigger_bubble(self):
        track_transient(self.bot,SimpleNamespace(chat_id=20,message_id=40))
        track_transient(self.bot,SimpleNamespace(chat_id=20,message_id=41))
        await dispatch(self.update(text='🔐 خرید اشتراک'),self.ctx)
        deleted={(call.kwargs['chat_id'],call.kwargs['message_id']) for call in self.bot.delete_message.await_args_list}
        self.assertEqual(deleted,{(20,40),(20,41),(20,50)})

    async def test_card_checkout_accepts_photo_directly_and_delivers_subscription(self):
        await customer.plan_detail(self.update('plan:1'),self.ctx,self.plan['id'])
        callback=next(b.callback_data for b in self.buttons() if b.callback_data.startswith('checkout:card'))
        await customer.callback(self.update(callback),self.ctx)
        order=(await self.store.user_orders(20))[0]
        self.assertEqual(self.ctx.user_data['receipt_order'],order['id'])
        self.assertIn('1234-5678-9012-3456',self.output.await_args.args[1])
        copy_button=next(b for b in self.buttons() if b.text=='📋 کپی شماره کارت')
        self.assertEqual(copy_button.copy_text.text,'1234567890123456')
        await customer.photo(self.update(photo=[SimpleNamespace(file_id='receipt-id')]),self.ctx)
        self.bot.send_photo.assert_awaited()
        approval_buttons=self.bot.send_photo.await_args.kwargs['reply_markup'].inline_keyboard[0]
        self.assertTrue(all(not b.api_kwargs for b in approval_buttons))
        await admin.callback(self.update(f"adm:approve:{order['id']}",uid=10),self.admin_ctx)
        self.assertEqual(self.admin_ctx.user_data['admin_state']['order_id'],order['id'])
        await admin.text(self.update(text='https://sub.example.test/user/abc',uid=10),self.admin_ctx)
        service=await self.store.service_for_order(order['id'])
        self.assertEqual(service['config_text'],'https://sub.example.test/user/abc')
        self.assertTrue((await self.store.get_order(order['id']))['delivered_at'])
        await customer.service_detail(self.update('service:1'),self.ctx,service['id'])
        self.assertIn('لینک اشتراک',self.output.await_args.args[1])
        with self.assertRaises(ValueError):
            await customer.owned_service(self.update(uid=21),self.ctx,service['id'])

    async def test_temporary_messages_are_deleted_after_seven_minute_timer(self):
        bot=SimpleNamespace(delete_message=AsyncMock())
        message=SimpleNamespace(chat_id=20,message_id=77)
        preserved=SimpleNamespace(chat_id=20,message_id=78)
        with patch.object(ui,'AUTO_DELETE_SECONDS',0.01):
            touch_cleanup(bot,20)
            track_transient(bot,message)
            track_transient(bot,preserved)
            keep_message(bot,preserved)
            await asyncio.sleep(0.03)
        bot.delete_message.assert_awaited_once_with(chat_id=20,message_id=77)

    async def test_duplicate_checkout_idempotent_but_new_visit_can_buy_again(self):
        await customer.plan_detail(self.update('plan:1'),self.ctx,self.plan['id'])
        callback=next(b.callback_data for b in self.buttons() if b.callback_data.startswith('checkout:card'))
        await customer.callback(self.update(callback),self.ctx)
        await customer.callback(self.update(callback),self.ctx)
        self.assertEqual(len(await self.store.user_orders(20)),1)
        await customer.plan_detail(self.update('plan:1'),self.ctx,self.plan['id'])
        callback=next(b.callback_data for b in self.buttons() if b.callback_data.startswith('checkout:card'))
        await customer.callback(self.update(callback),self.ctx)
        self.assertEqual(len(await self.store.user_orders(20)),2)

    async def test_price_changed_after_quote_does_not_charge(self):
        await customer.plan_detail(self.update('plan:1'),self.ctx,self.plan['id'])
        callback=next(b.callback_data for b in self.buttons() if b.callback_data.startswith('checkout:wallet'))
        await self.store.save_plan({'id':self.plan['id'],'price':200000})
        with self.assertRaisesRegex(ValueError,'قیمت'):
            await customer.callback(self.update(callback),self.ctx)
        self.assertEqual(await self.store.user_orders(20),[])

    async def test_rotation_atomic_and_card_stable_for_existing_order(self):
        cards=[{'number':'1111111111111111','holder':'الف'},{'number':'2222222222222222','holder':'ب'}]
        orders=[await self.store.create_order(20,plan_id=self.plan['id']) for _ in range(4)]
        assigned=await asyncio.gather(*(self.store.assign_card(o['id'],cards) for o in orders))
        self.assertEqual(sorted(c['number'] for c in assigned),[cards[0]['number']]*2+[cards[1]['number']]*2)
        self.assertEqual(await self.store.assign_card(orders[0]['id'],[]),assigned[0])

    async def test_manual_delivery_rejects_files_and_raw_vless(self):
        order=await self.store.create_order(20,plan_id=self.plan['id'])
        await self.store.attach_receipt(order['id'],20,'receipt')
        await admin.callback(self.update(f"adm:approve:{order['id']}",uid=10),self.admin_ctx)
        file=SimpleNamespace(file_name='my-vpn.conf',file_id='telegram-file-id',file_size=500)
        with self.assertRaisesRegex(ValueError,'لینک سابسکریپشن'):
            await admin.document(self.update(uid=10,document=file),self.admin_ctx)
        await admin.text(self.update(uid=10,text='vless://abc@example.test:443'),self.admin_ctx)
        self.assertIsNone(await self.store.service_for_order(order['id']))
        self.assertIn('admin_state',self.admin_ctx.user_data)
        await admin.text(self.update(uid=10,text='https://sub.example.test/user/ok'),self.admin_ctx)
        self.assertEqual((await self.store.service_for_order(order['id']))['config_text'],'https://sub.example.test/user/ok')

    async def test_wireguard_location_accepts_conf_file_and_delivers_it(self):
        location=await self.store.save_location('🇳🇱 هلند',kind='country')
        await self.store.set_location_protocol(location,'wireguard',True)
        plan=await self.store.save_plan({'name':'هلند وایرگارد','months':1,'price':120000,'traffic_gb':30,
            'location_id':location,'protocol':'wireguard','mode':'manual'})
        order=await self.store.create_order(20,plan_id=plan['id'])
        await self.store.attach_receipt(order['id'],20,'receipt')
        await self.provisioner.fulfill(order['id'])
        self.admin_ctx.user_data['admin_state']={'kind':'manual_config','order_id':order['id']}
        file=SimpleNamespace(file_name='netherlands.conf',file_id='wg-file-id',file_size=500)
        await admin.document(self.update(uid=10,document=file),self.admin_ctx)
        service=await self.store.service_for_order(order['id'])
        self.assertEqual((service['protocol'],service['document_file_id']),('wireguard','wg-file-id'))
        self.bot.send_document.assert_awaited()

    async def test_protocol_is_per_location_and_location_toggle_blocks_sale(self):
        await self.store.set_location_protocol(self.location_id,'openvpn',True)
        plan=await self.store.save_plan({'id':self.plan['id'],'protocol':'openvpn'})
        self.assertEqual(plan['protocol'],'openvpn')
        await customer.callback(self.update(f'protocol:{self.location_id}:openvpn'),self.ctx)
        self.assertIn('مدت سرویس',self.output.await_args.args[1])
        await self.store.save_location('مولتی',self.location_id,False,kind='multi')
        with self.assertRaises(ValueError):
            await self.store.create_order(20,plan_id=self.plan['id'])

    async def test_panel_off_never_calls_api_even_for_existing_automatic_plan(self):
        await self.store.save_plan({'id':self.plan['id'],'mode':'xui','inbound_id':1})
        order=await self.store.create_order(20,plan_id=self.plan['id'])
        await self.store.attach_receipt(order['id'],20,'receipt')
        self.provisioner.xui.ensure_client=AsyncMock(side_effect=AssertionError('network forbidden'))
        result=await self.provisioner.fulfill(order['id'])
        self.assertTrue(result['awaiting_config'])
        self.provisioner.xui.ensure_client.assert_not_awaited()

    async def test_dynamic_admin_file_revokes_access_without_restart(self):
        from dataclasses import replace
        path=self.root/'admins.json'
        write_admins(path,[10])
        self.shared['settings']=replace(self.settings,admins_file=path)
        self.assertEqual(admin_ids(self.shared['settings']),(10,))
        write_admins(path,[11])
        await admin.callback(self.update('adm:stats',uid=10),self.admin_ctx)
        self.assertIn('فقط برای مدیر',self.output.await_args.args[1])
        path.write_text('bad-json')
        self.assertEqual(admin_ids(self.shared['settings']),())

    async def test_dynamic_location_and_protocol_routes(self):
        await catalog_admin.callback(self.update('cat:addlocation',uid=10),self.admin_ctx)
        await catalog_admin.callback(self.update('cat:addkind:country',uid=10),self.admin_ctx)
        await catalog_admin.text(self.update(text='لوکیشن جدید',uid=10),self.admin_ctx)
        locations=await self.store.list_locations()
        new=next(x for x in locations if x['id']!=self.location_id)
        await catalog_admin.callback(self.update(f"cat:toggleprotocol:{new['id']}:v2ray",uid=10),self.admin_ctx)
        await catalog_admin.callback(self.update(f"cat:setroute:{self.plan['id']}:{new['id']}:v2ray",uid=10),self.admin_ctx)
        await customer.callback(self.update('location:'+new['id']),self.ctx)
        self.assertIn('نوع کانفیگ',self.output.await_args.args[1])
        await customer.catalog(self.update(),self.ctx,months=1,location_id=new['id'])
        self.assertTrue(any(b.callback_data==f"plan:{self.plan['id']}" for b in self.buttons()))

    async def test_new_plan_works_after_default_location_is_deleted(self):
        location=await self.store.save_location('لوکیشن جایگزین',kind='country')
        await self.store.set_location_protocol(location,'v2ray',True)
        await self.store.archive_location(self.location_id)
        await admin.callback(self.update('adm:new:2',uid=10),self.admin_ctx)
        await admin.callback(self.update(f'adm:newloc:2:{location}',uid=10),self.admin_ctx)
        await admin.callback(self.update(f'adm:newproto:2:{location}:v2ray',uid=10),self.admin_ctx)
        self.assertEqual(self.admin_ctx.user_data['admin_state']['plan']['location_id'],location)

    async def test_literal_plan_import_never_executes_old_file(self):
        file=self.root/'old.py'
        file.write_text("raise RuntimeError('must not execute')\nPLANS={'old':{'name':'دوماهه','days':60,'price':100,'traffic_gb':-1}}")
        self.assertEqual(await import_plans(self.store,read_plans(file)),1)
        self.assertEqual(await import_plans(self.store,read_plans(file)),0)
        imported=(await self.store.list_plans(months=2,active_only=False))[0]
        self.assertEqual(imported['traffic_gb'],0)
        self.assertFalse(imported['active'])

    async def test_lock_rejects_duplicate_instance(self):
        with acquire_instance_lock(self.store.path):
            with self.assertRaises(ValueError):
                acquire_instance_lock(self.store.path)

    async def test_dispatch_rejects_manual_document(self):
        order=await self.store.create_order(20,plan_id=self.plan['id'])
        await self.store.attach_receipt(order['id'],20,'receipt')
        await self.provisioner.fulfill(order['id'])
        self.admin_ctx.user_data['admin_state']={'kind':'manual_config','order_id':order['id']}
        file=SimpleNamespace(file_name='config.txt',file_id='file-id',file_size=600)
        with self.assertRaisesRegex(ValueError,'لینک سابسکریپشن'):
            await dispatch(self.update(uid=10,document=file),self.admin_ctx)
        self.assertEqual((await self.store.get_order(order['id']))['status'],'awaiting_config')

    async def test_reply_targets_its_order_instead_of_other_active_order(self):
        orders=[]
        for uid in (20,21):
            order=await self.store.create_order(uid,plan_id=self.plan['id'])
            await self.store.attach_receipt(order['id'],uid,'receipt')
            await self.provisioner.fulfill(order['id'])
            orders.append(order)
        self.admin_ctx.user_data['admin_state']={'kind':'manual_config','order_id':orders[1]['id']}
        update=self.update(uid=10,text='https://sub.example.test/replied-order')
        self.bot.id=999
        update.effective_message.reply_to_message=SimpleNamespace(from_user=SimpleNamespace(id=999),text=f"📤 کانفیگ سفارش #{orders[0]['id']}")
        await dispatch(update,self.admin_ctx)
        self.assertEqual((await self.store.get_order(orders[0]['id']))['status'],'paid')
        self.assertEqual((await self.store.get_order(orders[1]['id']))['status'],'awaiting_config')

    async def test_two_delivery_workers_do_not_send_same_order_twice(self):
        order=await self.store.create_order(20,plan_id=self.plan['id'])
        await self.store.attach_receipt(order['id'],20,'receipt')
        await self.provisioner.fulfill(order['id'])
        service=await self.store.complete_manual_order(order['id'],'https://example.test/sub')
        self.bot.send_message.reset_mock()
        await asyncio.gather(*(notify_service(self.ctx,service) for _ in range(5)))
        self.assertEqual(self.bot.send_message.await_count,1)

    async def test_dispatch_cancel_clears_pending_input_but_preserves_order(self):
        order=await self.store.create_order(20,plan_id=self.plan['id'])
        self.ctx.user_data.update(receipt_order=order['id'],customer_state='topup')
        await dispatch(self.update(text='/cancel'),self.ctx)
        self.assertEqual(self.ctx.user_data,{})
        self.assertEqual((await self.store.get_order(order['id']))['status'],'pending')

    async def test_legacy_import_retains_original_schema_balance_and_services(self):
        path=self.root/'old.sqlite3'
        with sqlite3.connect(path) as conn:
            conn.executescript("""
                CREATE TABLE users(user_id INTEGER PRIMARY KEY,username TEXT,full_name TEXT,balance INTEGER,joined_at TEXT);
                INSERT INTO users VALUES(55,'old','قدیمی',456000,'2026-01-01');
                CREATE TABLE service_plans(id TEXT PRIMARY KEY,name TEXT,price INTEGER,days INTEGER,traffic_gb INTEGER,is_active INTEGER);
                INSERT INTO service_plans VALUES('old','قدیمی',50000,30,-1,1);
                CREATE TABLE orders(id INTEGER PRIMARY KEY,user_id INTEGER,plan_id TEXT,amount INTEGER,status TEXT,created_at TEXT);
                INSERT INTO orders VALUES(3,55,'old',50000,'paid','2026-01-01');
                INSERT INTO orders VALUES(4,55,'old',50000,'waiting_confirm','2026-01-01');
                CREATE TABLE subscriptions(id INTEGER PRIMARY KEY,user_id INTEGER,order_id INTEGER,plan_id TEXT,sub_url TEXT,traffic_gb INTEGER,is_test INTEGER);
                INSERT INTO subscriptions VALUES(8,55,3,'old','https://example.test/sub',-1,0);
            """)
            original_schema=conn.execute("SELECT sql FROM sqlite_master WHERE name='users'").fetchone()[0]
        store=Store(path)
        await store.init()
        self.assertEqual((await store.get_user(55))['balance'],456000)
        self.assertEqual(len(await store.list_services(55)),1)
        self.assertEqual((await store.get_order(4))['status'],'legacy_review')
        await store.init()
        self.assertEqual(len(await store.list_services(55)),1)
        with sqlite3.connect(path) as conn:
            self.assertEqual(conn.execute("SELECT sql FROM sqlite_master WHERE name='users'").fetchone()[0],original_schema)
            self.assertEqual(conn.execute('SELECT balance FROM users').fetchone()[0],456000)
        self.assertTrue(path.with_suffix('.sqlite3.before-upgrade.bak').exists())


if __name__=='__main__':
    unittest.main()
