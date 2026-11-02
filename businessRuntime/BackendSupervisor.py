"""后台业务批量启动器。参数--config指定服务配置，--relay-config可选出站节点，--rsshub-entry可选已构建RSSHub入口。"""

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import concurrent.futures
from pathlib import Path
from microServices.ServiceLease import ServiceLease
from services.ServiceRegistry import ServiceRegistry


class BackendSupervisor:
    """一个部署仅一个监督进程，子服务独占自身状态；进程退出按退避重启，不杀端口上的陌生进程。"""

    def __init__(self, config, relay_config=None, rsshub_entry=None):
        """config为业务注册表，relay_config为节点配置，rsshub_entry为部署者已有的RSSHub构建。"""
        self.root = Path(__file__).resolve().parent
        self.registry = ServiceRegistry(config)
        self.relay = Path(relay_config).resolve() if relay_config else None
        self.rsshub = Path(rsshub_entry).resolve() if rsshub_entry else None
        self.closed = threading.Event()
        self.children = {}
        self.logs = []
        self.env = {**os.environ, "SYNLOVE_SERVICE_CONFIG": str(self.registry.path.resolve()),
                    "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}

    def commands(self):
        """无参数；只允许固定四项业务、可选RSSHub和可选出站节点，不加载个人实例。"""
        jobs = {}
        for name in ("reminders", "digest", "video", "crawler"):
            cfg = self.registry.get_service(name)
            jobs[name] = ([sys.executable, str(self.root / "microServices" / "ServiceHost.py"),
                           name, "--host", cfg["host"], "--port", str(cfg["port"])], self.root)
        node = shutil.which("node")
        if self.rsshub:
            if not node or not self.rsshub.is_file():
                raise ValueError("RSSHub入口或Node不存在")
            jobs["rsshub"] = ([node, str(self.rsshub)], self.rsshub.parent.parent)
        if self.relay:
            entry = self.root.parent / "relayNode" / "dist" / "tools" / "startNode.js"
            if not node or not entry.is_file() or not self.relay.is_file():
                raise ValueError("请先构建relayNode并填写节点配置")
            jobs["relay"] = ([node, str(entry), "--config", str(self.relay)], entry.parents[2])
        return jobs

    def run_services(self):
        """无参数；持有监督锁；任何持续崩溃达到5次时停止整个批次并明确返回失败。"""
        runtime = self.root / "runtime"
        runtime.mkdir(exist_ok=True)
        if (runtime / "migrationInProgress.json").exists():
            raise RuntimeError("数据导入未完成，拒绝启动调度")
        lease = ServiceLease(runtime / "supervisor.lock").acquire()
        options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        try:
            jobs = self.commands()
            failures = {name: 0 for name in jobs}
            due = {name: 0 for name in jobs}
            while not self.closed.is_set():
                for name, (command, cwd) in jobs.items():
                    child = self.children.get(name)
                    if child and child.poll() is None:
                        continue
                    if child:
                        failures[name] += 1
                        self.children.pop(name)
                        due[name] = time.monotonic() + min(60, 2 ** failures[name])
                        if failures[name] >= 5:
                            raise RuntimeError(name + "持续退出，检查该服务日志后重启")
                    if time.monotonic() < due[name]:
                        continue
                    out = (runtime / (name + ".log")).open("ab")
                    self.logs.append(out)
                    self.children[name] = subprocess.Popen(command, cwd=str(cwd), env=self.env,
                                                           stdout=out, stderr=out, **options)
                self.closed.wait(1)
            return 0
        finally:
            # 先让自己启动的宿主关闭Worker和锁，避免TerminateProcess遗留孙进程。
            from microServices.ServiceTransport import ServiceTransport

            def stop_service(name):
                """name为本批次业务；请求固定关闭端点，不寻找或终止其他端口进程。"""
                child = self.children.get(name)
                if child is None or child.poll() is not None:
                    return
                try:
                    ServiceTransport({**self.registry.get_service(name), "timeoutSeconds": 5}).request(
                        "POST", "/shutdown", b"{}")
                except Exception:
                    pass

            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(stop_service, ("reminders", "digest", "video", "crawler")))
            for child in self.children.values():
                if child.poll() is None:
                    try:
                        child.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        if os.name == "nt":
                            subprocess.run(["taskkill", "/F", "/T", "/PID", str(child.pid)],
                                           timeout=15, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                           creationflags=subprocess.CREATE_NO_WINDOW)
                        else:
                            child.terminate()
            for child in self.children.values():
                try:
                    child.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=10)
            for out in self.logs:
                out.close()
            lease.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--relay-config")
    parser.add_argument("--rsshub-entry")
    args = parser.parse_args()
    app = BackendSupervisor(args.config, args.relay_config, args.rsshub_entry)
    signal.signal(signal.SIGINT, lambda *_: app.closed.set())
    signal.signal(signal.SIGTERM, lambda *_: app.closed.set())
    raise SystemExit(app.run_services())
