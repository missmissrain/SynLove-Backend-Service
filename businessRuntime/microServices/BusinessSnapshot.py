"""业务只读展示。BusinessSnapshot(name,root) 在服务端读取提醒/新闻库，返回有界GUI数据，不返回密钥。"""

import os
import sqlite3
from pathlib import Path


class BusinessSnapshot:
    """数据库读取留在所属业务服务内，GUI不再打开这些数据库文件。"""

    def __init__(self, name, root):
        """name为reminders或digest，root为服务代码根目录；路径可由服务环境覆盖。"""
        self.name, self.root = name, Path(root)

    def read_rows(self, path, sql):
        """path为固定服务库，sql为内部常量查询；缺库不创建，锁等待最多2秒。"""
        path = Path(path).resolve()
        if not path.is_file():
            return []
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(row) for row in conn.execute(sql)]

    def read_snapshot(self):
        """无参数；返回提醒列表/投递元数据，或新闻推送元数据与每日计划。"""
        if self.name == "reminders":
            path = Path(os.environ.get("SYNLOVE_REMINDERS_DB") or self.root / "runtime" / "reminders.db")
            rows = self.read_rows(path, "SELECT id,text,next_at,daily_at FROM reminders "
                                  "WHERE status='pending' ORDER BY next_at LIMIT 20")
            count = self.read_rows(path, "SELECT COUNT(*) AS n FROM reminders WHERE status='pending'")
            events = self.read_rows(path, "SELECT id,fired_at FROM reminders "
                                    "WHERE fired_at IS NOT NULL ORDER BY fired_at DESC LIMIT 4")
            return {"total": count[0]["n"] if count else 0, "rows": rows, "events": events}
        if self.name == "digest":
            path = Path(os.environ.get("SYNLOVE_DIGEST_DB") or self.root / "runtime" / "newsDigest.db")
            events = self.read_rows(path, "SELECT id,created_at,channel,ok FROM pushes ORDER BY id DESC LIMIT 8")
            state = Path(os.environ.get("SYNLOVE_SERVICE_DATA_DIR") or
                         self.root / "runtime" / "microServices" / "digest")
            plans = self.read_rows(state / "schedule.sqlite", "SELECT hhmm,enabled,day FROM schedule WHERE id=1")
            return {"events": events, "schedule": plans[0] if plans else None}
        raise ValueError("该服务不提供业务展示快照")
