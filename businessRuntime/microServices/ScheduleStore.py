"""跨平台新闻调度状态。ScheduleStore(root) 使用服务本地SQLite，不依赖Windows计划任务。"""

import json
import os
import re
import sqlite3
import time
from pathlib import Path
from contextlib import contextmanager


class ScheduleStore:
    """调度定义只有服务端一份；先事务领取日期，再提交确定ID的任务。"""

    def __init__(self, root, path=None):
        """root为项目根目录；默认不启用计划，旧计划须显式迁移。"""
        data = os.environ.get("SYNLOVE_SERVICE_DATA_DIR")
        self.path = Path(path or (Path(data) / "schedule.sqlite" if data else
                                 root / "runtime" / "microServices" / "digest" / "schedule.sqlite"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS schedule "
                         "(id INTEGER PRIMARY KEY CHECK(id=1), hhmm TEXT, enabled INTEGER, day TEXT)")
            conn.execute("INSERT OR IGNORE INTO schedule VALUES(1,'08:55',0,'')")

    @contextmanager
    def connect(self):
        """无参数；为每个请求创建独立连接。"""
        db = sqlite3.connect(str(self.path), timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def configure(self, args, module):
        """args为原digest_schedule参数；module提供画像配置读写，不再操作操作系统任务。"""
        action = args.get("action", "get")
        if action not in ("get", "set", "enable", "disable", "delete"):
            raise ValueError("未知调度操作")
        hhmm = args.get("time")
        if "enabled" in args and not isinstance(args["enabled"], bool):
            raise ValueError("enabled必须为布尔值")
        if hhmm is not None and not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", str(hhmm)):
            raise ValueError("time必须为HH:MM")
        if action in ("set", "enable") and args.get("enabled", True):
            with self.connect() as conn:
                prior = conn.execute("SELECT hhmm FROM schedule WHERE id=1").fetchone()[0]
            if not (hhmm or prior):
                raise ValueError("启用计划前必须先设置time")
        if action == "set" and any(key in args for key in ("interest", "maxItems", "maxChars")):
            config = module.read_config()
            for key, low, high in (("maxItems", 1, 8), ("maxChars", 60, 400)):
                if key in args:
                    value = int(args[key])
                    if not low <= value <= high:
                        raise ValueError(key + "超出范围")
                    config[key] = value
            if "interest" in args:
                if not isinstance(args["interest"], str) or not args["interest"].strip():
                    raise ValueError("interest必须为非空文本")
                config["interest"] = str(args["interest"])
            module.write_config(config)
        with self.connect() as conn:
            if action == "delete":
                conn.execute("UPDATE schedule SET enabled=0,day='',hhmm=NULL WHERE id=1")
            elif action in ("set", "enable", "disable"):
                conn.execute("UPDATE schedule SET hhmm=COALESCE(?,hhmm),enabled=? WHERE id=1",
                             (hhmm, int(action != "disable" and args.get("enabled", True)),))
            row = conn.execute("SELECT hhmm,enabled,day FROM schedule WHERE id=1").fetchone()
        return json.dumps({"time": row[0], "enabled": bool(row[1]), "lastClaimedDate": row[2]},
                          ensure_ascii=False)

    def claim_due(self):
        """无参数；查询今日到点计划，持久任务提交成功前不消费日期。"""
        day, hhmm = time.strftime("%Y-%m-%d"), time.strftime("%H:%M")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT hhmm,enabled,day FROM schedule WHERE id=1").fetchone()
            if not row[1] or not row[0] or hhmm < row[0] or row[2] == day:
                return None
        return "digest-schedule-" + day

    def confirm_enqueued(self, key):
        """key为成功登记到OperationStore的稳定ID；确认当日计划已入队。"""
        day = key.removeprefix("digest-schedule-")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            raise ValueError("非法调度ID")
        with self.connect() as conn:
            conn.execute("UPDATE schedule SET day=? WHERE id=1", (day,))
