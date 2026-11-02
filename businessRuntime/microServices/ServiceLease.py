"""服务独占锁。ServiceLease(path) 在服务生命周期内锁住状态目录，默认非阻塞拒绝第二实例。"""

import os
from pathlib import Path


class ServiceLease:
    """用操作系统文件锁保护单个服务状态目录；进程崩溃后自动释放。"""

    def __init__(self, path):
        """path 为锁文件路径；只创建锁文件，不修改业务数据。"""
        self.path = Path(path)
        self.handle = None

    def acquire(self):
        """无参数；独占锁失败立即报错，禁止第二个调度消费者启动。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            if handle.seek(0, 2) == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise RuntimeError("该服务状态目录已被另一个实例占用") from None
        self.handle = handle
        return self

    def close(self):
        """无参数；关闭句柄释放文件锁。"""
        if self.handle:
            self.handle.close()
            self.handle = None
