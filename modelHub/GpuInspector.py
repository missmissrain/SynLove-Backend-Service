"""显卡显存观测器。

职责：通过 nvidia-smi 读取显存总量、已用量、空闲量和进程摘要。
注册参数：query_timeout_seconds 为 nvidia-smi 查询超时，默认 5 秒。
该模块只观测，不根据 GPU 利用率擅自终止桌面程序或推理进程。
"""

from __future__ import annotations

import csv
import io
import os
import subprocess
from typing import Any


class GpuInspector:
    """读取 NVIDIA GPU 的实时显存状态。"""

    def __init__(self, query_timeout_seconds: float = 5.0):
        """query_timeout_seconds 为 nvidia-smi 单次查询的最大等待秒数。"""
        self.query_timeout_seconds = float(query_timeout_seconds)

    def _run(self, query: str) -> str:
        """query 为 nvidia-smi 查询字段；返回 CSV 文本，失败时抛出运行时错误。"""
        flags = 0x08000000 if os.name == "nt" else 0
        result = subprocess.run(
            ["nvidia-smi", f"--query-gpu={query}", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=self.query_timeout_seconds,
            creationflags=flags,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "nvidia-smi 查询失败")
        return result.stdout

    def status(self) -> dict[str, Any]:
        """无参数；返回显存状态，查询失败时返回 ok=false 和错误原因。"""
        try:
            rows = list(csv.reader(io.StringIO(self._run("index,name,memory.used,memory.free,memory.total,utilization.gpu"))))
            devices = []
            for row in rows:
                if len(row) < 6:
                    continue
                index, name, used, free, total, utilization = [item.strip() for item in row[:6]]
                devices.append({
                    "index": int(index),
                    "name": name,
                    "usedMiB": int(float(used)),
                    "freeMiB": int(float(free)),
                    "totalMiB": int(float(total)),
                    "utilizationPercent": int(float(utilization)),
                })
            return {"ok": True, "devices": devices}
        except Exception as error:  # noqa: BLE001
            return {"ok": False, "devices": [], "error": f"{type(error).__name__}: {error}"}
