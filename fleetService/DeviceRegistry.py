"""功能：设备清单的唯一维护类。设备事实只从 DeviceStore 取，导出文件只由本类写。

注册参数store为DeviceStore、path为导出文件、minInterval为最小写盘间隔秒数。
本类不发命令、不校验签名；外部要设备清单只调 list()/get()，不得直接读 devices.json。
"""
import hashlib
import json
import threading
import time
from pathlib import Path


class DeviceRegistry:
    """设备清单的唯一出口，内部维护导出快照与修订号。"""

    def __init__(self, store, path, min_interval=5.0):
        """store为DeviceStore；path为导出文件；minInterval为最小写盘间隔。"""
        self.store = store
        self.path = Path(path)
        self.min_interval = float(min_interval)
        self._lock = threading.RLock()
        self._last_write = 0.0
        self._revision = ""

    def list(self):
        """无参数；返回当前设备清单，并按需刷新导出文件。"""
        rows = self.store.list_devices()
        self._publish(rows)
        return rows

    def get(self, ident):
        """ident为设备ID；返回单台设备，不存在时抛 ValueError。"""
        return self.store.get_device(ident)

    def refresh(self, force=False):
        """force为是否忽略最小间隔；立即刷新导出并返回设备清单。"""
        rows = self.store.list_devices()
        self._publish(rows, force=force)
        return rows

    def snapshot(self):
        """无参数；返回导出快照内容，文件缺失时返回空结构。"""
        try:
            value = json.loads(self.path.read_text("utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def _publish(self, rows, force=False):
        """rows为设备清单；按间隔与修订号决定是否落盘。"""
        with self._lock:
            now = time.time()
            if not force and now - self._last_write < self.min_interval:
                return
            value = {"devices": rows, "checkedAt": now,
                     "revision": self._revision_of(rows)}
            if value["revision"] == self._revision and not force:
                self._last_write = now
                return
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                temp = self.path.with_suffix(".tmp")
                temp.write_text(json.dumps(value, ensure_ascii=False), "utf-8")
                temp.replace(self.path)
                self._revision = value["revision"]
                self._last_write = now
            except OSError:
                pass

    def _revision_of(self, rows):
        """rows为设备清单；返回与设备身份相关的稳定摘要。"""
        parts = []
        for row in sorted(rows, key=lambda item: item.get("id") or ""):
            # 心跳按 30 秒取整，既能反映在线变化，又不会每次心跳都重写文件。
            bucket = int(float(row.get("lastSeen") or 0) // 30)
            parts.append("%s:%s:%s:%s:%s" % (row.get("id"), row.get("bootId"),
                                            bool(row.get("online")), bucket,
                                            row.get("capabilities")))
        return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]
