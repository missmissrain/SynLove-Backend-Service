# 调度状态：SchedulerState记录全局执行者、等待者和资源准入事件，不保存提示词或密钥。
# 注册参数root为后台根目录；状态位于runtime/gpuResidency，实际互斥仍由操作系统文件锁承担。
import ctypes
import json
import os
import time
import uuid
from pathlib import Path


class SchedulerState:
    """提供原子状态文件和可观测排队记录；状态文件不代替执行锁。"""

    def __init__(self, root):
        """root为后台根目录；创建唯一调度状态目录。"""
        self.base = Path(root) / "runtime/gpuResidency"
        self.base.mkdir(parents=True, exist_ok=True)
        self.waiting = self.base / "waiting"
        self.waiting.mkdir(exist_ok=True)

    def write_json(self, path, value):
        """path为目标文件、value为无凭据对象；使用唯一临时文件进行原子替换。"""
        temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
        temp.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        os.replace(temp, path)

    def mark_waiting(self, model):
        """model为协调器标识；返回当前请求唯一编号并记录进入队列的时间。"""
        key = uuid.uuid4().hex
        self.write_json(self.waiting / (key + ".json"), {
            "requestId": key, "model": model, "pid": os.getpid(),
            "queuedAt": time.time()})
        return key

    def remove_waiting(self, key):
        """key为请求编号；只移除该请求自己的等待记录。"""
        try:
            (self.waiting / (key + ".json")).unlink()
        except FileNotFoundError:
            pass

    def mark_active(self, key, model, phase, **extra):
        """key/model/phase为执行请求信息；extra只允许加入资源和错误等审计数据。"""
        self.write_json(self.base / "state.json", {
            "requestId": key, "model": model, "pid": os.getpid(),
            "active": phase != "idle", "phase": phase, "updatedAt": time.time(),
            **extra})

    def process_alive(self, pid):
        """pid为记录中的进程编号；只查询进程是否仍运行，不发送终止信号。"""
        if os.name != "nt":
            return Path("/proc", str(pid)).exists()
        lib = ctypes.WinDLL("kernel32", use_last_error=True)
        lib.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        lib.OpenProcess.restype = ctypes.c_void_p
        lib.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        lib.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = lib.OpenProcess(0x1000, False, int(pid))
        if not handle:
            return False
        try:
            code = ctypes.c_uint32()
            return bool(lib.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            lib.CloseHandle(handle)

    def read_status(self):
        """无参数；返回存活执行者与等待者，忽略进程崩溃遗留的排队文件。"""
        state = {}
        try:
            state = json.loads((self.base / "state.json").read_text(encoding="utf-8"))
            if state.get("active") and not self.process_alive(state.get("pid", 0)):
                state.update(active=False, phase="ownerExited")
        except (OSError, ValueError):
            pass
        queue = []
        for path in self.waiting.glob("*.json"):
            try:
                item = json.loads(path.read_text(encoding="utf-8"))
                if self.process_alive(item.get("pid", 0)):
                    queue.append(item)
            except (OSError, ValueError):
                continue
        return {"active": state, "waiting": sorted(queue, key=lambda item: item["queuedAt"]),
                "waitingCount": len(queue)}
