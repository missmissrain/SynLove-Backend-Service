"""LLM模型生命周期：API常驻，llama子进程按需启动，空闲600秒或同伴请求让位时停止。

注册参数command为已构造且不记录的llama命令、cwd/log为运行目录与日志、
port为回环上游端口、coordinator为共享协调器、need_mb为冷加载预算。
模型子进程退出释放显存，正在推理时不会被空闲定时器和让位接口终止。
"""
import contextlib
import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path


class LlmRuntime:
    """将推理执行锁、软卸载及保温时钟从HTTP代理分离。"""

    def __init__(self, command, cwd, log, port, coordinator, need_mb):
        """command为固定参数，port为子进程端口；默认最多等待启动300秒。"""
        self.command = command
        self.cwd = str(cwd)
        self.log = Path(log)
        self.port = int(port)
        self.coordinator = coordinator
        self.need_mb = int(need_mb)
        self.lock = threading.Lock()
        self.process = None
        self.busy = False
        self.last_active = time.monotonic()
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.stopped = threading.Event()
        threading.Thread(target=self.idle_watchdog, daemon=True).start()

    def status(self):
        """无参数；返回模型驻留和推理状态，读取不会续期保温。"""
        loaded = self.process is not None and self.process.poll() is None
        return {"ok": True, "loaded": loaded, "busy": self.busy,
                "idleSeconds": round(max(0, time.monotonic() - self.last_active), 2),
                "keepAliveSeconds": self.coordinator.idle_seconds}

    @contextlib.contextmanager
    def request_model(self):
        """无参数；请求全局资格后加载模型，完成或异常时统一刷新空闲计时。"""
        with self.coordinator.request_slot(), self.lock:
            self.busy = True
            try:
                self.ensure_loaded()
                yield
            finally:
                self.last_active = time.monotonic()
                self.busy = False

    def ensure_loaded(self):
        """无参数；调用者持有模型锁，首次或意外退出后加载，不打印命令中的密钥。"""
        if self.process is not None and self.process.poll() is None:
            return
        self.coordinator.ensure_memory(self.need_mb)
        self.log.parent.mkdir(parents=True, exist_ok=True)
        with self.log.open("a", encoding="utf-8") as log:
            self.process = subprocess.Popen(
                self.command, cwd=self.cwd, stdout=log, stderr=log,
                creationflags=0x08000000 if os.name == "nt" else 0,
                stdin=subprocess.DEVNULL)
        deadline = time.monotonic() + 300
        try:
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise RuntimeError("LLM子进程加载失败，检查服务端模型日志")
                try:
                    with self.opener.open(f"http://127.0.0.1:{self.port}/health", timeout=2) as response:
                        if json.loads(response.read()).get("status") == "ok":
                            return
                except (OSError, ValueError):
                    pass
                time.sleep(0.25)
            raise TimeoutError("LLM模型300秒内未就绪")
        except Exception:
            self.stop_process()
            raise

    def stop_process(self):
        """无参数；调用者持有模型锁，只停止本实例创建的模型进程。"""
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=10)
        self.process = None

    def unload_idle(self):
        """无参数；非阻塞尝试卸载，忙碌时明确返回False。"""
        if not self.lock.acquire(False):
            return False
        try:
            self.stop_process()
            return True
        finally:
            self.lock.release()

    def idle_watchdog(self):
        """无参数；每秒检查TTL，锁内再次核对完成时间，避免请求刚结束就被误卸载。"""
        while not self.stopped.wait(1):
            if self.lock.acquire(False):
                try:
                    if time.monotonic() - self.last_active >= self.coordinator.idle_seconds:
                        self.stop_process()
                finally:
                    self.lock.release()

    def close_runtime(self):
        """无参数；服务退出时停止检查线程并结束所属子进程。"""
        self.stopped.set()
        with self.lock:
            self.stop_process()
