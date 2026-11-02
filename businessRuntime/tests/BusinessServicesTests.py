"""后台业务隔离回归。临时SQLite、伪网络；不调用真实模型、不向消息平台发消息。"""

import concurrent.futures
import hashlib
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from contextlib import closing

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from microServices.NotificationStore import NotificationStore
from microServices.CrawlerAdapter import CrawlerAdapter
from microServices.ScheduleStore import ScheduleStore
from microServices.ServiceLease import ServiceLease
from BusinessDataImport import BusinessDataImport
from BackendSupervisor import BackendSupervisor


class BusinessServicesTests(unittest.TestCase):
    """验证通知去重、故障恢复、单实例与迁移边界，不使用正式数据库。"""

    def test_concurrent_notifications_and_receipts(self):
        """并发重复200次写入同一业务ID，确认与重新打开后仍只能产生一条通知。"""
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "queue.sqlite"
            store = NotificationStore(path)
            with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
                ids = list(pool.map(lambda _: store.enqueue("测试新闻", "news-1"), range(200)))
            self.assertEqual(len(set(ids)), 1)
            self.assertEqual(len(store.pending()), 1)
            store.acknowledge(ids[0])
            reopened = NotificationStore(path)
            self.assertEqual(reopened.enqueue("测试新闻", "news-1"), ids[0])
            self.assertEqual(reopened.pending(), [])
            with self.assertRaises(ValueError):
                reopened.enqueue("被篡改的消息", "news-1")

    def test_reminder_crash_after_enqueue(self):
        """模拟通知已落库但提醒未确认，下一轮不得生成第二条消息，最终更新提醒为done。"""
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            env = {"SYNLOVE_REMINDERS_DB": str(path / "reminders.db"),
                   "SYNLOVE_NOTIFICATION_DB": str(path / "notifications.sqlite"),
                   "SYNLOVE_REMINDERS_CONFIG": str(path / "absent.json")}
            with patch.dict(os.environ, env):
                spec = importlib.util.spec_from_file_location("isolatedReminder", ROOT / "reminders" / "reminderService.py")
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                module.add("到点测试", in_seconds=10)
                with closing(module.connect()) as db, db:
                    db.execute("UPDATE reminders SET next_at='2020-01-01 00:00:00'")
                from newsDigest.push.qqPush import qqPush
                push = qqPush.push

                def crash_after_write(pusher, text, event_key=None):
                    """先真实写入通知，再模拟进程在业务确认前失败。"""
                    push(pusher, text, event_key)
                    raise RuntimeError("simulated_crash")

                with patch.object(qqPush, "push", crash_after_write):
                    self.assertEqual(module.fire_due(), 0)
                self.assertEqual(module.fire_due(), 1)
                self.assertEqual(module.fire_due(), 0)
                self.assertEqual(len(NotificationStore(path / "notifications.sqlite").pending()), 1)

    def test_schedule_survives_restart(self):
        """每日计划写入后重新打开，确认领取只消费一次，禁用计划不会继续领取。"""
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "schedule.sqlite"
            store = ScheduleStore(Path(folder), path)
            store.configure({"action": "set", "time": "00:00"}, None)
            key = store.claim_due()
            self.assertIsNotNone(key)
            store.confirm_enqueued(key)
            self.assertIsNone(ScheduleStore(Path(folder), path).claim_due())
            store.configure({"action": "disable"}, None)
            self.assertIsNone(store.claim_due())

    def test_exclusive_service_lock(self):
        """同一状态目录第二个调度实例必须被拒绝，释放后才能重新获得锁。"""
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "lock"
            first = ServiceLease(path).acquire()
            try:
                with self.assertRaises(RuntimeError):
                    ServiceLease(path).acquire()
            finally:
                first.close()
            ServiceLease(path).acquire().close()

    def test_private_url_rejected(self):
        """通过假DNS返回私网地址，浏览器和HTTP共用的入口必须拒绝。"""
        with patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("127.0.0.1", 80))]):
            with self.assertRaises(ValueError):
                CrawlerAdapter(ROOT).validate_url("http://public.example/")
        with self.assertRaises(ValueError):
            CrawlerAdapter(ROOT).validate_url("file:///etc/passwd")

    def test_migration_checks_and_no_overwrite(self):
        """已冻结且哈希正确的库可导入，旧数据、未冻结快照、路径穿越都被拒绝。"""
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source, target = root / "source", root / "target"
            (source / "runtime").mkdir(parents=True)
            dbpath = source / "runtime" / "reminders.db"
            with closing(sqlite3.connect(dbpath)) as db, db:
                db.execute("CREATE TABLE test(value TEXT)")
                db.execute("INSERT INTO test VALUES('隔离测试')")
            files = {"runtime/reminders.db": hashlib.sha256(dbpath.read_bytes()).hexdigest()}
            manifest = source / "manifest.json"
            manifest.write_text(json.dumps({"version": 1, "frozen": False, "files": files}), encoding="utf-8")
            with self.assertRaises(ValueError):
                BusinessDataImport(target).import_data(source)
            manifest.write_text(json.dumps({"version": 1, "frozen": True, "files": files}), encoding="utf-8")
            result = BusinessDataImport(target).import_data(source)
            self.assertTrue(result["imported"])
            with self.assertRaises(ValueError):
                BusinessDataImport(target).import_data(source)
            files["../../outside.db"] = "fake"
            manifest.write_text(json.dumps({"version": 1, "frozen": True, "files": files}), encoding="utf-8")
            with self.assertRaises(ValueError):
                BusinessDataImport(root / "second").import_data(source)


if __name__ == "__main__":
    unittest.main()
