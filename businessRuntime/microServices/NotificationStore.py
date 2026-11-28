"""远端通知发件箱。参数path为SQLite路径；默认runtime/notifications.sqlite，无消息平台账号和密钥。"""

import hashlib
import sqlite3
import time
from pathlib import Path
from contextlib import contextmanager


class NotificationStore:
    """先持久化结果，再由个人实例领取并确认；确认后保留收据防止重复写入。"""

    def __init__(self, path):
        """path为当前个人部署专属通知库，多个服务共用同一文件。"""
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS notifications "
                       "(id TEXT PRIMARY KEY,text TEXT NOT NULL,created REAL NOT NULL,acked REAL)")

    @contextmanager
    def connect(self):
        """无参数；返回独立事务连接，最多等待10秒。"""
        db = sqlite3.connect(str(self.path), timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def enqueue(self, text, key):
        """text为通知正文，key为业务稳定ID；同ID不同正文拒绝而非覆盖。"""
        if not isinstance(text, str) or not text.strip() or len(text) > 30000:
            raise ValueError("通知必须为1至30000字")
        ident = hashlib.sha256(str(key).encode()).hexdigest()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT text FROM notifications WHERE id=?", (ident,)).fetchone()
            if row and row[0] != text:
                raise ValueError("同一通知ID已有不同正文")
            db.execute("INSERT OR IGNORE INTO notifications VALUES(?,?,?,NULL)",
                       (ident, text, time.time()))
        return ident

    def pending(self):
        """无参数；按创建时间返回最早20条未确认结果，离线期间不会丢弃。"""
        with self.connect() as db:
            return [{"id": row[0], "text": row[1], "created": row[2]} for row in db.execute(
                "SELECT id,text,created FROM notifications WHERE acked IS NULL ORDER BY created,id LIMIT 20")]

    def acknowledge(self, ident):
        """ident为本机已持久入队的通知ID；允许重复确认。"""
        with self.connect() as db:
            row = db.execute("UPDATE notifications SET acked=COALESCE(acked,?) WHERE id=?",
                             (time.time(), str(ident)))
            if row.rowcount != 1:
                raise ValueError("通知不存在")
