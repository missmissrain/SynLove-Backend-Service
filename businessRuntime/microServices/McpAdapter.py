"""隔离执行适配器。McpAdapter(root,name,timeout) 运行固定Worker，超时结束整个进程树。"""

import json
import os
import signal
import subprocess
import sys
import threading
import time


class McpAdapter:
    """每次调用独立子进程，服务HTTP入口不执行模型或长任务。"""

    def __init__(self, root, name, timeout, env=None):
        """root为代码根目录，name为服务名，timeout为执行秒数，env为服务状态目录等覆盖。"""
        self.root = root
        self.name = name
        self.timeout = timeout
        self.child = None
        self.env = env or {}
        self.stopped = threading.Event()
        self.lock = threading.Lock()

    def call(self, message):
        """message为JSON-RPC请求；返回完整响应，超时与进程失败不伪装成功。"""
        options = {"start_new_session": True} if os.name != "nt" else {
            "creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP}
        with self.lock:
            if self.stopped.is_set():
                raise RuntimeError("业务已取消")
            self.child = subprocess.Popen(
                [sys.executable, str(self.root / "microServices" / "McpWorker.py"), self.name],
                cwd=str(self.root), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env={**os.environ, **self.env, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
                     "SYNLOVE_SERVICE_WORKER": "1"}, **options)
        chunks, size = [], [0]
        overflow = threading.Event()
        reader = threading.Thread(target=self.read_output, args=(chunks, size, overflow), daemon=True)
        reader.start()
        try:
            self.child.stdin.write(json.dumps(message).encode() + b"\n")
            self.child.stdin.close()
            deadline = time.monotonic() + self.timeout
            while self.child.poll() is None:
                if overflow.is_set():
                    raise RuntimeError("业务结果超过8MiB")
                if time.monotonic() >= deadline:
                    raise TimeoutError("业务执行超时，已结束进程树")
                if self.stopped.wait(0.05):
                    raise RuntimeError("业务已取消")
            reader.join(5)
            if reader.is_alive():
                raise RuntimeError("业务输出流未关闭")
            if overflow.is_set():
                raise RuntimeError("业务结果超过8MiB")
            if self.child.returncode != 0:
                raise RuntimeError(f"{self.name}业务进程失败，退出码{self.child.returncode}")
            result = json.loads(b"".join(chunks))
            if not isinstance(result, dict):
                raise ValueError("业务返回了非法协议对象")
            return result
        finally:
            self.close()
            reader.join(5)
            self.child.stdout.close()

    def read_output(self, chunks, size, overflow):
        """chunks为受限缓冲，size为累计字节，overflow为超限标志；不缓存超限内容。"""
        while True:
            data = self.child.stdout.read(65536)
            if not data:
                return
            size[0] += len(data)
            if size[0] > 8 * 1024 * 1024:
                overflow.set()
                return
            chunks.append(data)

    def close(self):
        """无参数；结束仍存活的Worker及子进程并等待退出。"""
        self.stopped.set()
        with self.lock:
            child = self.child
            if child and child.poll() is None:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(child.pid)],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
                else:
                    try:
                        os.killpg(child.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                child.wait(timeout=15)
