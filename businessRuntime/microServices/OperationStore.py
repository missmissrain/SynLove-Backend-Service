"""请求去重与结果存储。OperationStore(path) 默认使用SQLite事务，保留提交中的未知结果。"""

import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager


class OperationStore:
    """按请求ID和请求摘要保存唯一操作；重启后不自动重复执行未完成的写操作。"""

    def __init__(self, path):
        """path 为服务专属数据库路径；不复用消息队列或提醒数据库。"""
        self.path = str(path)
        with self.connect() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS operations "
                         "(id TEXT PRIMARY KEY, hash TEXT NOT NULL, state TEXT NOT NULL, "
                         "result TEXT, created REAL NOT NULL)")
            conn.execute("UPDATE operations SET state='unknown' WHERE state='running'")

    @contextmanager
    def connect(self):
        """无参数；返回独立连接，等待事务锁最多10秒。"""
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def begin(self, key, payload):
        """key为调用方稳定ID；payload为请求对象；同ID不同内容拒绝，重复返回已存状态。"""
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT hash,state,result FROM operations WHERE id=?", (key,)).fetchone()
            if row:
                if row[0] != digest:
                    raise ValueError("requestId 已用于其他请求")
                return {"id": key, "state": row[1], "result": json.loads(row[2]) if row[2] else None}
            conn.execute("INSERT INTO operations VALUES (?,?, 'running',NULL,?)", (key, digest, time.time()))
        return None

    def finish(self, key, result, state="done"):
        """key为请求ID，result为响应，state为done/unknown；不确定的副作用禁止自动重放。"""
        if state not in ("done", "unknown"):
            raise ValueError("非法操作终态")
        with self.connect() as conn:
            conn.execute("UPDATE operations SET state=?,result=? WHERE id=?",
                         (state, json.dumps(result, ensure_ascii=False), key))

    def get(self, key):
        """key为请求ID；返回状态和结果，缺失返回None。"""
        with self.connect() as conn:
            row = conn.execute("SELECT state,result FROM operations WHERE id=?", (key,)).fetchone()
        return None if not row else {"id": key, "state": row[0],
                                     "result": json.loads(row[1]) if row[1] else None}

    def expire_results(self, cutoff):
        """cutoff为保留边界时间戳；只清理已完成结果正文，保留ID和哈希防止历史写操作重放。"""
        with self.connect() as conn:
            conn.execute("UPDATE operations SET state='expired',result=NULL "
                         "WHERE state='done' AND created<?", (cutoff,))
