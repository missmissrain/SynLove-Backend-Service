"""
提醒服务：独立于会话的定时提醒（SQLite + 30 秒轮询 + QQ 主动推送）。

为什么不用 harness 自带的 schedule：
- schedule 是**会话内定时器**，只有该会话的 Agent 活着才走表；
- 换/停会话、重启后没人发消息，Agent 不加载，提醒就永远不会触发（今天的吃药提醒就是这么丢的）。

本服务单独跑在后台，和 harness 解耦：
- 存 `runtime/reminders.db`，支持一次性/每天固定时刻；
- 到点直接用 QQ 主动消息推送（不依赖微信窗口）；
- 提供 CLI：`--add` / `--list` / `--delete` / `--loop`，供 MCP 工具调用。

参数：
    --add TEXT            提醒内容
    --at "YYYY-MM-DD HH:MM" 一次性时间
    --daily "HH:MM"       每天这个时刻
    --in 600              多少秒后（一次性，便于测试）
    --list / --delete ID
    --loop                常驻循环（每 30 秒检查）
"""

import argparse
import io
import os
import sqlite3
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
DB = Path(os.environ.get("SYNLOVE_REMINDERS_DB") or ROOT / "runtime" / "reminders.db")
sys.path.insert(0, str(ROOT))

SCHEMA = """
CREATE TABLE IF NOT EXISTS reminders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  text TEXT NOT NULL,
  next_at TEXT NOT NULL,
  daily_at TEXT,
  status TEXT NOT NULL DEFAULT 'pending',
  created_at TEXT NOT NULL,
  fired_at TEXT
);
"""


def connect():
    """打开数据库并建表。"""
    DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB))
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def add(text, at=None, daily=None, in_seconds=None):
    """新增提醒，返回 id 与下一次触发时间。"""
    if not isinstance(text, str) or not text.strip() or len(text) > 4000:
        raise ValueError("提醒内容必须为1至4000个字符")
    if sum(value is not None for value in (at, daily, in_seconds)) != 1:
        raise ValueError("at、daily、in_seconds必须且只能选择一个")
    now = time.time()
    if in_seconds is not None:
        if isinstance(in_seconds, bool) or int(in_seconds) != in_seconds or not 1 <= in_seconds <= 31536000:
            raise ValueError("相对提醒时间必须为1至31536000秒的整数")
        next_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now + int(in_seconds)))
    elif at is not None:
        try:
            next_at = time.strftime("%Y-%m-%d %H:%M:%S", time.strptime(at, "%Y-%m-%d %H:%M"))
        except ValueError:
            raise ValueError("一次性时间必须为YYYY-MM-DD HH:MM") from None
        if time.mktime(time.strptime(next_at, "%Y-%m-%d %H:%M:%S")) <= now:
            raise ValueError("提醒时间必须在未来")
    elif daily is not None:
        import re
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", daily):
            raise ValueError("每日时间必须为HH:MM")
        hour, minute = [int(part) for part in daily.split(":")]
        candidate = time.strftime("%Y-%m-%d") + f" {hour:02d}:{minute:02d}:00"
        if time.mktime(time.strptime(candidate, "%Y-%m-%d %H:%M:%S")) <= now:
            candidate = time.strftime("%Y-%m-%d", time.localtime(now + 86400)) + f" {hour:02d}:{minute:02d}:00"
        next_at = candidate
    else:
        raise ValueError("必须给 --at / --daily / --in 之一")
    conn = connect()
    cursor = conn.execute(
        "INSERT INTO reminders (text, next_at, daily_at, status, created_at) VALUES (?, ?, ?, 'pending', ?)",
        (text, next_at, daily, time.strftime("%Y-%m-%d %H:%M:%S")),
    )
    conn.commit()
    reminder_id = cursor.lastrowid
    conn.close()
    return reminder_id, next_at


def pending():
    """列出待触发提醒。"""
    conn = connect()
    rows = conn.execute(
        "SELECT id, text, next_at, daily_at FROM reminders WHERE status = 'pending' ORDER BY next_at"
    ).fetchall()
    conn.close()
    return rows


def remove(reminder_id):
    """删除提醒，返回是否删到。"""
    if isinstance(reminder_id, bool) or not isinstance(reminder_id, int) or reminder_id < 1:
        raise ValueError("提醒ID必须为正整数")
    conn = connect()
    cursor = conn.execute("DELETE FROM reminders WHERE id = ?", (int(reminder_id),))
    conn.commit()
    deleted = cursor.rowcount
    conn.close()
    return deleted > 0


def fire_due():
    """触发所有到点提醒，返回本次推送条数。"""
    from newsDigest.push.qqPush import qqPush
    import json

    conn = connect()
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    due = conn.execute(
        "SELECT id, text, daily_at, next_at FROM reminders WHERE status = 'pending' AND next_at <= ?", (now,)
    ).fetchall()
    if not due:
        conn.close()
        return 0

    path = Path(os.environ.get("SYNLOVE_REMINDERS_CONFIG") or ROOT / "reminders" / "config" / "reminderConfig.json")
    if not path.is_file():
        # 旧安装仅兼容读取；迁移命令会生成独立提醒配置，不再与新闻服务共享可变设置。
        path = ROOT / "newsDigest" / "config" / "newsDigest.json"
    config = json.loads(path.read_text(encoding="utf-8-sig")) if path.is_file() else {}
    enabled = (config.get("push") or {}).get("enabled", True)
    pusher = qqPush() if enabled else None

    sent = 0
    for reminder_id, text, daily_at, due_at in due:
        ok = False
        detail = ""
        if pusher is not None:
            try:
                result = pusher.push(text, event_key=f"reminder:{reminder_id}:{due_at}")
                ok = bool(result.get("ok"))
                detail = str(result.get("detail", ""))[:120]
            except Exception as error:  # noqa: BLE001
                detail = f"{type(error).__name__}: {error}"
        print(f"[提醒] #{reminder_id} {now} 推送{'成功' if ok else '失败'}：{text}（{detail}）", flush=True)
        if ok:
            sent += 1
        else:
            # 失败保留pending，不能把未投递的提醒当成已完成。
            continue
        if daily_at:
            next_day = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() + 86400))
            hour, minute = daily_at.split(":")
            next_at = next_day.split(" ")[0] + f" {int(hour):02d}:{int(minute):02d}:00"
            conn.execute("UPDATE reminders SET next_at = ?, fired_at = ? WHERE id = ?", (next_at, now, reminder_id))
        else:
            conn.execute("UPDATE reminders SET status = 'done', fired_at = ? WHERE id = ?", (now, reminder_id))
    conn.commit()
    conn.close()
    return sent


def main(argv):
    """CLI 入口。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--add")
    parser.add_argument("--at")
    parser.add_argument("--daily")
    parser.add_argument("--in", dest="in_seconds", type=int)
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--delete", type=int)
    parser.add_argument("--loop", action="store_true")
    args = parser.parse_args(argv)

    if args.add:
        reminder_id, next_at = add(args.add, args.at, args.daily, args.in_seconds)
        print(f"已创建提醒 #{reminder_id}，下次触发 {next_at}")
        return 0
    if args.list:
        rows = pending()
        if not rows:
            print("（没有待触发的提醒）")
        for row in rows:
            print(f"#{row[0]}  {row[2]}  {'每天 ' + row[3] if row[3] else '一次性'}  {row[1]}")
        return 0
    if args.delete is not None:
        print("已删除" if remove(args.delete) else "没找到该提醒")
        return 0
    if args.loop:
        from microServices.ServiceManager import ServiceManager
        ServiceManager(ROOT).ensure_service("reminders")
        print("提醒已由独立API服务接管；此入口不再启动第二个轮询器。")
        return 0
    parser.print_help()
    return 0


if __name__ == "__main__":
    if os.environ.get("SYNLOVE_SERVICE_WORKER") == "1":
        sys.exit(main(sys.argv[1:]))
    from microServices.ServiceCli import ServiceCli
    sys.exit(ServiceCli("reminders", ROOT).run(sys.argv[1:]))
