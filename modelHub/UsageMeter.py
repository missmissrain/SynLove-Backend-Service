"""ModelHub 云端模型用量统计。

职责：按 provider / model 记录请求数、token、耗时与失败，落盘 JSONL 并汇总。
注册参数：path 为用量文件路径；retention_days 为汇总窗口（默认 30 天）。
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Any


class UsageMeter:
    """线程安全的用量账本：逐条落盘，按天与按模型汇总。"""

    def __init__(self, path: str | os.PathLike[str], retention_days: int = 30):
        """path 为 JSONL 账本；retention_days 为汇总保留天数（默认 30）。"""
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.retention_days = max(1, int(retention_days))
        self._lock = Lock()

    def record(self, provider: str, model: str, status: int, ms: float,
               prompt_tokens: int = 0, completion_tokens: int = 0, stream: bool = False,
               error: str = "") -> None:
        """记录一次调用；token 未知时记 0，error 只保留前 200 字。"""
        row = {
            "at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "ts": time.time(),
            "provider": provider,
            "model": model,
            "status": int(status),
            "ms": round(float(ms), 1),
            "prompt": int(prompt_tokens or 0),
            "completion": int(completion_tokens or 0),
            "stream": bool(stream),
            "ok": 200 <= int(status) < 400,
            **({"error": error[:200]} if error else {}),
        }
        line = json.dumps(row, ensure_ascii=False)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")

    def _rows(self) -> list[dict[str, Any]]:
        """读取保留期内的记录；坏行跳过。"""
        if not self.path.exists():
            return []
        floor = time.time() - self.retention_days * 86400
        rows: list[dict[str, Any]] = []
        with self.path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if float(row.get("ts") or 0) >= floor:
                    rows.append(row)
        return rows

    def summary(self) -> dict[str, Any]:
        """返回按模型与按天的汇总，供 /v1/usage 与管理接口读取。"""
        rows = self._rows()
        models: dict[str, dict[str, Any]] = {}
        days: dict[str, dict[str, Any]] = {}
        today = datetime.now().astimezone().date().isoformat()
        for row in rows:
            key = "%s/%s" % (row.get("provider", ""), row.get("model", ""))
            bucket = models.setdefault(key, {
                "provider": row.get("provider", ""), "model": row.get("model", ""),
                "requests": 0, "failed": 0, "promptTokens": 0, "completionTokens": 0,
                "totalMs": 0.0, "streamRequests": 0, "lastAt": "",
            })
            bucket["requests"] += 1
            bucket["failed"] += 0 if row.get("ok") else 1
            bucket["promptTokens"] += int(row.get("prompt") or 0)
            bucket["completionTokens"] += int(row.get("completion") or 0)
            bucket["totalMs"] += float(row.get("ms") or 0)
            bucket["streamRequests"] += 1 if row.get("stream") else 0
            bucket["lastAt"] = max(bucket["lastAt"], str(row.get("at") or ""))
            day = str(row.get("at") or "")[:10]
            day_bucket = days.setdefault(day, {"date": day, "requests": 0, "failed": 0,
                                               "promptTokens": 0, "completionTokens": 0})
            day_bucket["requests"] += 1
            day_bucket["failed"] += 0 if row.get("ok") else 1
            day_bucket["promptTokens"] += int(row.get("prompt") or 0)
            day_bucket["completionTokens"] += int(row.get("completion") or 0)
        for bucket in models.values():
            bucket["averageMs"] = round(bucket["totalMs"] / max(1, bucket["requests"]), 1)
            bucket.pop("totalMs", None)
        today_bucket = days.get(today, {"date": today, "requests": 0, "failed": 0,
                                        "promptTokens": 0, "completionTokens": 0})
        return {
            "ok": True,
            "retentionDays": self.retention_days,
            "today": today_bucket,
            "models": sorted(models.values(), key=lambda item: -item["requests"]),
            "days": sorted(days.values(), key=lambda item: item["date"])[-self.retention_days:],
        }
