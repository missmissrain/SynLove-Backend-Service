"""本机业务服务生命周期。ServiceManager(root,registry) 只启动managed端点，external只探活。"""

import json
import concurrent.futures
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from microServices.ServiceTransport import ServiceTransport
from services.ServiceRegistry import ServiceRegistry
from services.ProcessLaunch import console_python, spawn_hidden


class ServiceManager:
    """服务持有自己的锁与状态；启动器不接管远程进程、不根据失败创建本机替身。"""

    def __init__(self, root=None, registry=None):
        """root为代码根目录；registry可指定独立配置文件。"""
        self.root = Path(root or Path(__file__).resolve().parents[1])
        self.registry = registry or ServiceRegistry()
        self.children = {}

    def is_ready(self, name):
        """name为业务服务名；验证健康协议和服务身份，防止端口被其他程序占用。"""
        cfg = self.registry.get_service(name)
        try:
            data = json.loads(ServiceTransport({**cfg, "timeoutSeconds": 1}).request("GET", "/health"))
            return data.get("ok") is True and data.get("service") == name and data.get("protocol") == 1
        except Exception:
            return False

    def ensure_service(self, name):
        """name为业务服务名；托管服务独占启动，远端不可用直接报错。"""
        cfg = self.registry.get_service(name)
        if cfg["management"] == "disabled":
            raise RuntimeError(name + "服务已禁用")
        if self.is_ready(name):
            return
        if cfg["management"] == "external":
            raise RuntimeError(name + "远程服务不可用；没有回退本机执行")
        from microServices.ServiceLease import ServiceLease
        state = (self.root / cfg["dataDir"] if cfg.get("dataDir")
                 else self.root / "runtime" / "microServices" / name).resolve()
        state.mkdir(parents=True, exist_ok=True)
        lock = ServiceLease(state / "startup.lock")
        deadline = time.monotonic() + 30
        while True:
            try:
                lock.acquire()
                break
            except RuntimeError:
                if self.is_ready(name):
                    return
                if time.monotonic() >= deadline:
                    raise TimeoutError(name + "启动锁等待超时")
                time.sleep(0.2)
        try:
            if self.is_ready(name):
                return
            try:
                with socket.create_connection((cfg["host"], cfg["port"]), timeout=0.5):
                    raise RuntimeError(name + "端口已有不匹配的服务；请检查身份或鉴权，不创建第二个实例")
            except OSError:
                pass
            if name in ("reminders", "digest"):
                from microServices.LegacyServiceMigration import LegacyServiceMigration
                LegacyServiceMigration(self.root, registry=self.registry).check()
            deadline = time.monotonic() + 30
            env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
                   "SYNLOVE_SERVICE_CONFIG": str(self.registry.path.resolve())}
            # 只脱离窗口、不脱离控制台：CREATE_NO_WINDOW 让本进程拥有一个不可见控制台，
            # 它再拉起的子进程（MCP、git、tasklist 等）会继承这个控制台，因此不会再弹黑框。
            # 之前用的 DETACHED_PROCESS 会让整棵树失去控制台，子孙进程各自新建可见窗口。
            options = {"start_new_session": True} if os.name != "nt" else {}
            if name == "session":
                # 原生 Harness session 必须由 Node 服务端持有，不能让 Python 业务
                # ServiceHost 伪装成 session 权威存储。
                harness_root = self.root / "backend"
                env.update({
                    "SYNLOVE_SESSION_PORT": str(cfg["port"]),
                    "SYNLOVE_SESSION_HOST": str(cfg["host"]),
                    "SYNLOVE_SESSION_ROOT": str((self.root / cfg.get("rootDir", "runtime/sessions")).resolve()),
                    "SYNLOVE_SESSION_COMPRESSION": str(cfg.get("compression", "zstd")),
                    "SYNLOVE_SESSION_MODE": "local",
                })
                command = ["node", "tools/sessionServiceServer.mjs"]
                cwd = harness_root
            else:
                command = [console_python(), str(self.root / "microServices" / "ServiceHost.py"), name,
                           "--host", urlsplit(cfg["baseURL"]).hostname, "--port", str(cfg["port"])]
                cwd = self.root
            with (state / "service.log").open("ab") as output:
                child = spawn_hidden(
                    command, cwd=str(cwd), env=env,
                    stdout=output, stderr=output, **options)
            self.children[name] = child
            # 启动器可先退出；后台等待线程持有进程句柄，最终回收而不是遗失Popen所有权。
            threading.Thread(target=child.wait, daemon=True).start()
            (state / "service.pid").write_text(str(child.pid), encoding="utf-8")
            while time.monotonic() < deadline:
                if self.is_ready(name):
                    return
                if child.poll() is not None:
                    raise RuntimeError(name + "服务启动失败，请检查对应service.log")
                time.sleep(0.2)
            # 只结束刚刚启动且尚未就绪的子进程，禁止清理任意端口持有者。
            child.terminate()
            child.wait(timeout=10)
            raise TimeoutError(name + "服务启动超时")
        finally:
            lock.close()

    def stop_service(self, name):
        """name为服务名；只关闭配置为managed且身份匹配的端点，不接管external。"""
        cfg = self.registry.get_service(name)
        if cfg["management"] != "managed":
            return False

        def port_closed():
            """无参数；只有配置端口实际释放，才能确认进程不再提供服务。"""
            try:
                with socket.create_connection((cfg["host"], cfg["port"]), timeout=0.5):
                    return False
            except OSError:
                return True

        def wait_port_closed(seconds=8):
            """seconds为等待上限；服务主动断开连接后等待端口真正释放。"""
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                if port_closed():
                    return True
                time.sleep(0.2)
            return port_closed()

        if not self.is_ready(name):
            if not port_closed():
                raise RuntimeError(name + "端口仍被占用且身份探活失败，拒绝报告已停止")
            return False
        try:
            ServiceTransport(cfg).request("POST", "/shutdown", b"{}")
        except Exception:
            # Windows 服务可能在送完响应前主动关闭 socket；连接异常不等于
            # 停止失败，必须以端口最终是否释放作为判断依据。
            if wait_port_closed():
                return True
            raise
        if wait_port_closed(30):
            child = self.children.get(name)
            if child:
                child.wait(timeout=30)
            return True
        raise TimeoutError(name + "服务关闭尚未确认")

    def stop_managed_services(self):
        """无参数；并发关闭本机业务服务，保留远程服务和核心管理API，任一失败明确抛错。"""
        names = ("reminders", "digest", "video", "history", "memory", "state", "qqstate", "observation", "session", "coding", "crawler")
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(names)) as pool:
            results = list(pool.map(self.stop_service, names))
        return dict(zip(names, results))
