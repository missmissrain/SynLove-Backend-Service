"""去重与推送记录：SQLite 存"见过哪些链接、推过什么"。

职责：
- `items` 表存每条新闻的 url（主键）、标题、时间、首次/最近见到的时间、是否推送过；
- `pushes` 表存每次推送的正文与结果（成功/失败原因），便于回查"为什么推了/没推"；
- 对外只暴露两个动作：`take_new(items)`（挑出没见过的，并全部登记为"见过"）、`record_push(...)`。

注册参数：
    dbPath：数据库路径，默认 runtime/newsDigest.db
"""

import sqlite3
import sys
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:  # 极老的解释器没有 reconfigure
    pass

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
  url TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  news_time TEXT,
  first_seen TEXT NOT NULL,
  last_seen TEXT NOT NULL,
  pushed INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS pushes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  channel TEXT NOT NULL,
  ok INTEGER NOT NULL,
  content TEXT NOT NULL,
  error TEXT NOT NULL DEFAULT ''
);
"""


class newsStore:
    """新闻去重库。"""

    def __init__(self, db_path):
        """db_path：SQLite 文件路径（不存在会自动建目录与表）。"""
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def take_new(self, items, record=True):
        """返回本次**新出现**的条目（按 url 判重）。record=False 时只算不写（给 --dry-run 用）。"""
        known = {row[0] for row in self.conn.execute("SELECT url FROM items")}
        fresh = [item for item in items if item["url"] not in known]
        if record:
            self.record_new(items)
        return fresh

    def record_new(self, items):
        """把条目登记为"已见过"（不标记已推送）。

        为什么不在 `take_new` 里直接登记：**推送失败时不能把新闻弄丢**。
        现在的顺序是"先算新的 → 推送 → 成功才登记并标记"，推失败下一次还能重来。
        """
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        rows = [
            (item["url"], item["title"], item.get("time", ""), stamp, stamp)
            for item in items
        ]
        self.conn.executemany(
            """INSERT INTO items (url, title, news_time, first_seen, last_seen)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(url) DO UPDATE SET last_seen = excluded.last_seen, title = excluded.title""",
            rows,
        )
        self.conn.commit()

    def mark_pushed(self, items):
        """把条目标记为已推送（按 url）。"""
        self.conn.executemany("UPDATE items SET pushed = 1 WHERE url = ?", [(item["url"],) for item in items])
        self.conn.commit()

    def record_push(self, channel, ok, content, error=""):
        """记录一次推送结果（成功与否、正文、错误）。"""
        self.conn.execute(
            "INSERT INTO pushes (created_at, channel, ok, content, error) VALUES (?, ?, ?, ?, ?)",
            (time.strftime("%Y-%m-%d %H:%M:%S"), channel, 1 if ok else 0, content, error),
        )
        self.conn.commit()

    def stats(self):
        """返回 (库里总条数, 已推送条数, 推送记录数)。"""
        total = self.conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        pushed = self.conn.execute("SELECT COUNT(*) FROM items WHERE pushed = 1").fetchone()[0]
        pushes = self.conn.execute("SELECT COUNT(*) FROM pushes").fetchone()[0]
        return total, pushed, pushes

    def close(self):
        """关闭数据库连接。"""
        self.conn.close()
