# ASR驻留：AsrResidency让CPU语音识别也参与全局互斥，空闲保温后只卸载模型而保持端口在线。
# 注册参数state为ASR状态字典、lock为请求锁、coordinator为共享资源协调器；默认保温来自统一配置。
import contextlib
import gc
import threading
import time


class AsrResidency:
    """隔离ASR生命周期，不改变识别参数、权重、CPU INT8模式或上传协议。"""

    def __init__(self, state, lock, coordinator):
        """state/lock沿用识别服务；后台线程仅在锁内确认空闲后卸载。"""
        self.state = state
        self.lock = lock
        self.coordinator = coordinator
        self.stopped = threading.Event()
        threading.Thread(target=self.idle_watchdog, daemon=True).start()

    @contextlib.contextmanager
    def request_slot(self):
        """无参数；先取得全局资格再持有ASR请求锁，排队不阻碍旧模型让位。"""
        with self.coordinator.request_slot(), self.lock:
            if self.state["model"] is None:
                self.coordinator.ensure_memory(0, cold=True)
            self.state["last_active"] = time.time()
            try:
                yield
            finally:
                self.state["last_active"] = time.time()

    def clear_runtime(self):
        """无参数；调用方持有请求锁，释放CTranslate2模型和循环引用。"""
        if self.state["model"] is None:
            return
        self.state["model"] = None
        self.state["device"] = ""
        gc.collect()
        self.state["last_unload"] = time.time()

    def unload_idle(self):
        """无参数；执行中返回False，空闲时释放模型并保持HTTP服务在线。"""
        if not self.lock.acquire(False):
            return False
        try:
            self.clear_runtime()
            return True
        finally:
            self.lock.release()

    def idle_watchdog(self):
        """无参数；健康查询不续期，超时后在锁内再次检查最后业务完成时间。"""
        while not self.stopped.wait(1):
            if self.lock.acquire(False):
                try:
                    if time.time() - self.state["last_active"] >= self.coordinator.idle_seconds:
                        self.clear_runtime()
                finally:
                    self.lock.release()
