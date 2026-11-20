"""ModelHub 的跨进程模型租约。

职责：对同一台服务器建立一个共享文件锁，记录当前请求者、模型和超时，
避免多个 ModelHub 实例同时做出互相矛盾的卸载决定。
注册参数：lock_path 为锁文件；lease_path 为可读状态文件。
"""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class ModelLease:
    """使用 Windows msvcrt 或 POSIX flock 实现单一调度器租约。"""

    def __init__(self, lock_path: str | os.PathLike[str], lease_path: str | os.PathLike[str]):
        """lock_path 为跨进程锁；lease_path 为脱敏后的当前租约状态文件。"""
        self.lock_path = Path(lock_path)
        self.lease_path = Path(lease_path)
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self.lease_path.parent.mkdir(parents=True, exist_ok=True)

    def _lock(self, file) -> None:
        """file 为已打开的锁文件；阻塞取得一个字节的独占锁。"""
        if os.name == "nt":
            import msvcrt
            file.seek(0)
            msvcrt.locking(file.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(file.fileno(), fcntl.LOCK_EX)

    def _unlock(self, file) -> None:
        """file 为当前租约文件；释放已取得的独占锁。"""
        if os.name == "nt":
            import msvcrt
            file.seek(0)
            msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(file.fileno(), fcntl.LOCK_UN)

    @contextmanager
    def acquire(self, model_name: str, owner: str, timeout_seconds: float = 1800) -> Iterator[dict]:
        """model_name 为模型名，owner 为调用方标识，timeout_seconds 为最长租约时间。"""
        started = time.time()
        with self.lock_path.open("a+b", buffering=0) as file:
            if self.lock_path.stat().st_size == 0:
                file.write(b"\0")
            self._lock(file)
            state = {
                "model": model_name,
                "owner": owner,
                "startedAt": started,
                "expiresAt": started + float(timeout_seconds),
            }
            self.lease_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
            try:
                yield state
            finally:
                try:
                    self.lease_path.unlink()
                except FileNotFoundError:
                    pass
                self._unlock(file)
