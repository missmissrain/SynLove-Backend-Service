"""TTS驻留策略：取得全局执行资格，空闲到期或LLM让位请求时释放模型及CUDA缓存。

注册参数state为原TTS状态、lock为原请求锁、coordinator为共享显存协调器；
默认保温600秒，保留现有INT8及CPU混合推理实现，HTTP始终常驻。
"""
import contextlib
import gc
import sys
import threading
import time


class TtsResidency:
    """独立管理Breeze模型驻留，不修改量化权重及生成参数。"""

    def __init__(self, state, lock, coordinator):
        """state/lock沿用当前服务；仅成功取得请求锁后允许释放runtime。"""
        self.state, self.lock, self.coordinator = state, lock, coordinator
        self.stopped = threading.Event()
        threading.Thread(target=self.idle_watchdog, daemon=True).start()

    def needed_memory(self):
        """无参数；按原加载器预算返回MiB，CPU模式不要求显存。"""
        mode = self.state.get("device_arg")
        if mode == "cpu":
            return 0
        if mode == "int8":
            return int(float(self.state.get("int8_gpu_gb") or 3.6) * 1024 +
                       self.state.get("int8_margin_mb", 1500))
        try:
            budget = float(mode)
            if budget > 0:
                return int(budget * 1024 + 900)
        except (TypeError, ValueError):
            pass
        return 6500

    @contextlib.contextmanager
    def request_slot(self):
        """无参数；先取得全局锁再进入现有请求锁，排队请求不能阻止闲置模型让位。"""
        with self.coordinator.request_slot(), self.lock:
            if self.state["runtime"] is None and self.needed_memory() > 0:
                self.coordinator.ensure_memory(self.needed_memory())
            yield

    def clear_runtime(self):
        """无参数；调用者须持有请求锁，释放引用、循环引用及CUDA缓存，不终止HTTP。"""
        if self.state["runtime"] is None:
            return
        self.state["runtime"] = None
        self.state["device"] = ""
        gc.collect()
        torch = sys.modules.get("torch")
        if torch is not None and torch.cuda.is_available():
            torch.cuda.empty_cache()
            torch.cuda.ipc_collect()
            self.state["last_unload"] = {
                "at": time.time(),
                "allocatedMb": round(torch.cuda.memory_allocated() / 1024 / 1024, 2),
                "reservedMb": round(torch.cuda.memory_reserved() / 1024 / 1024, 2)}

    def unload_idle(self):
        """无参数；正在合成时返回False，空闲时软卸载，不按显卡利用率中断任务。"""
        if not self.lock.acquire(False):
            return False
        try:
            if self.state.get("busy"):
                return False
            self.clear_runtime()
            return True
        finally:
            self.lock.release()

    def idle_watchdog(self):
        """无参数；每秒检查空闲期限，取锁后再次核对，健康查询不续期。"""
        while not self.stopped.wait(1):
            if self.lock.acquire(False):
                try:
                    if (not self.state.get("busy") and
                        time.time() - self.state["last_active"] >= self.coordinator.idle_seconds):
                        self.clear_runtime()
                finally:
                    self.lock.release()
