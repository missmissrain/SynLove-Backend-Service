"""业务微服务HTTP入口。参数service/host/port/config；默认仅回环，非回环必须配置独立Bearer密钥。"""

import argparse
import concurrent.futures
import json
import os
import re
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from services.ServiceRegistry import ServiceRegistry
from microServices.ArtifactStore import ArtifactStore
from microServices.McpAdapter import McpAdapter
from microServices.OperationStore import OperationStore
from microServices.ServiceLease import ServiceLease
from microServices.ServiceTransport import ServiceTransport
from microServices.BoundedHttpServer import BoundedHttpServer


class ServiceHost:
    """每进程一个业务服务，一个状态目录和一个串行执行槽；HTTP查询不等待长任务。"""

    SERVICES = ("reminders", "digest", "video", "crawler")

    def __init__(self, name, registry, root=ROOT):
        """name为固定服务名；registry提供超时与密钥引用；root允许隔离测试目录。"""
        if name not in self.SERVICES:
            raise ValueError("未知业务服务")
        self.name, self.root, self.cfg = name, Path(root), registry.get_service(name)
        if (self.root / "runtime" / "migrationInProgress.json").exists():
            raise RuntimeError("数据导入未完成，拒绝启动")
        from microServices.FeatureReadiness import FeatureReadiness
        self.readiness = FeatureReadiness(self.root, name, registry)
        self.config_path = str(registry.path.resolve())
        self.state = (self.root / self.cfg["dataDir"] if self.cfg.get("dataDir")
                      else self.root / "runtime" / "microServices" / name).resolve()
        self.lease = ServiceLease(self.state / "service.lock").acquire()
        try:
            self.store = OperationStore(self.state / "operations.sqlite")
            self.files = ArtifactStore(self.state / "files")
        except BaseException:
            self.lease.close()
            raise
        self.slot = threading.Lock()
        self.registration = threading.Lock()
        self.last_maintenance = 0
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        self.closed = threading.Event()
        self.adapter = None
        self.server = None
        self.key = os.environ.get(self.cfg.get("apiKeyEnv", ""), "")
        self.catalog_lock = threading.Lock()
        self.catalog = None
        self.snapshot_lock = threading.Lock()
        self.snapshot_at = 0
        self.snapshot_data = None
        self.database_lease = None
        if name in ("reminders", "digest"):
            env = self.worker_environment()
            key = "SYNLOVE_" + name.upper() + "_DB"
            fallback = self.root / "runtime" / ("reminders.db" if name == "reminders" else "newsDigest.db")
            database = Path(env.get(key) or os.environ.get(key) or fallback).resolve()
            try:
                self.database_lease = ServiceLease(Path(str(database) + ".api.lock")).acquire()
            except BaseException:
                self.close()
                raise

    def worker_environment(self):
        """无参数；统一业务执行与只读API的服务端路径，避免读写落到不同数据库。"""
        env = {"SYNLOVE_SERVICE_DATA_DIR": str(self.state), "SYNLOVE_SERVICE_CONFIG": self.config_path}
        env["SYNLOVE_NOTIFICATION_DB"] = str(self.root / "runtime" / "notifications.sqlite")
        if self.cfg.get("databasePath") and self.name in ("reminders", "digest"):
            env["SYNLOVE_" + self.name.upper() + "_DB"] = str((self.root / self.cfg["databasePath"]).resolve())
        if self.cfg.get("databasePath") and self.name == "state":
            env["SYNLOVE_STATE_DB"] = str((self.root / self.cfg["databasePath"]).resolve())
        if self.cfg.get("databasePath") and self.name == "qqstate":
            env["SYNLOVE_QQ_STATE_DB"] = str((self.root / self.cfg["databasePath"]).resolve())
        if self.cfg.get("databasePath") and self.name == "observation":
            env["SYNLOVE_OBSERVATION_DB"] = str((self.root / self.cfg["databasePath"]).resolve())
        if self.name == "digest":
            env["SYNLOVE_DIGEST_LOG"] = str(self.state / "digest.log")
        if self.cfg.get("businessConfigPath") and self.name in ("reminders", "digest"):
            env["SYNLOVE_" + self.name.upper() + "_CONFIG"] = str(
                (self.root / self.cfg["businessConfigPath"]).resolve())
        if self.name == "video" and self.cfg.get("workDir"):
            env["SYNLOVE_VIDEO_WORK_DIR"] = str((self.root / self.cfg["workDir"]).resolve())
        if self.name == "memory":
            if self.cfg.get("timelineDir"):
                env["SYNLOVE_MEMORY_TIMELINE_DIR"] = str((self.root / self.cfg["timelineDir"]).resolve())
            if self.cfg.get("graphDir"):
                env["SYNLOVE_MEMORY_GRAPH_DIR"] = str((self.root / self.cfg["graphDir"]).resolve())
        return env

    def business_snapshot(self):
        """无参数；同一时刻仅一次只读查询，缓存2秒，不能修改日程或触发推送。"""
        if self.name not in ("reminders", "digest"):
            raise ValueError("该服务没有业务快照")
        with self.snapshot_lock:
            if self.snapshot_data is None or time.monotonic() - self.snapshot_at > 2:
                result = McpAdapter(self.root, self.name, 10, self.worker_environment()).call(
                    {"id": 0, "method": "service/snapshot"})
                self.snapshot_data = {"service": self.name, "protocol": 1, **result["result"]}
                self.snapshot_at = time.monotonic()
            return self.snapshot_data

    def tool_catalog(self):
        """无参数；独立读取无副作用工具定义，长任务执行时仍能完成MCP挂载。"""
        if self.name == "core":
            raise ValueError("核心控制服务不提供MCP工具")
        with self.catalog_lock:
            if self.catalog is None:
                adapter = McpAdapter(self.root, self.name, 15, {
                    "SYNLOVE_SERVICE_DATA_DIR": str(self.state),
                    "SYNLOVE_SERVICE_CONFIG": self.config_path})
                result = adapter.call({"jsonrpc": "2.0", "id": 0, "method": "tools/list"})
                if "tools" not in result.get("result", {}):
                    raise RuntimeError("服务未能提供工具定义")
                self.catalog = result["result"]
            return self.catalog

    def submit(self, payload, internal=False):
        """payload含稳定requestId和message；重复请求只查已有状态，忙时拒绝新请求。"""
        # 槽位领取与ID落库必须同一临界区，否则同ID的并发请求会在落库前被误判为新任务。
        with self.registration:
            return self.submit_locked(payload, internal)

    def submit_locked(self, payload, internal):
        """payload/internal为已串行化的提交参数；登记完成后才允许下个请求检查状态。"""
        if self.closed.is_set():
            raise BlockingIOError("服务正在关闭")
        if not isinstance(payload, dict):
            raise ValueError("请求必须是对象")
        key, message = payload.get("requestId"), payload.get("message")
        if not isinstance(key, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", key):
            raise ValueError("requestId格式错误")
        allowed = ("tools/list", "tools/call", "service/tick") if internal else ("tools/list", "tools/call")
        if not isinstance(message, dict) or message.get("method") not in allowed:
            raise ValueError("只允许tools/list和tools/call")
        params = message.get("params", {})
        if not isinstance(params, dict) or not isinstance(params.get("arguments", {}), dict):
            raise ValueError("params和arguments必须是对象")
        if not self.slot.acquire(False):
            prior = self.store.get(key)
            if prior:
                # 仍校验摘要，防止忙时同ID不同参数被误认成功。
                return self.store.begin(key, message)
            raise BlockingIOError("服务忙，请稍后提交新任务")
        try:
            if time.monotonic() - self.last_maintenance > 3600:
                cutoff = time.time() - float(self.cfg.get("cacheRetentionDays", 7)) * 86400
                self.store.expire_results(cutoff)
                self.files.expire_files(cutoff)
                self.last_maintenance = time.monotonic()
            prior = self.store.begin(key, message)
            if prior:
                self.slot.release()
                return prior
            self.pool.submit(self.execute, key, message)
            return {"id": key, "state": "running", "result": None}
        except BaseException:
            self.slot.release()
            raise

    def execute(self, key, message):
        """key为已登记请求ID；隔离执行业务并持久化MCP结果，最终释放唯一执行槽。"""
        state = "done"
        try:
            message = json.loads(json.dumps(message))
            if self.name == "video" and message["method"] == "tools/call":
                args = message.setdefault("params", {}).setdefault("arguments", {})
                frames = args.get("frames")
                if frames is not None and (isinstance(frames, bool) or not isinstance(frames, int)
                                           or not 1 <= frames <= 120):
                    raise ValueError("frames必须为1至120的整数")
                target = args.get("pathOrUrl", "")
                if target.startswith("artifact:"):
                    args["pathOrUrl"] = str(self.files.resolve(target[9:]))
                elif not target.startswith(("http://", "https://")):
                    raise ValueError("视频文件必须先上传为artifact，不能引用调用机或任意服务器路径")
                else:
                    host = urlsplit(target).hostname or ""
                    allowed = self.cfg.get("allowedVideoHosts", [
                        "bilibili.com", "b23.tv", "douyin.com", "xiaohongshu.com", "xhslink.com",
                        "youtube.com", "youtu.be", "weibo.com", "zhihu.com"])
                    if not any(host == name or host.endswith("." + name) for name in allowed):
                        raise ValueError("该视频域名不在服务端allowedVideoHosts中；可以先上传视频文件")
                    from microServices.CrawlerAdapter import CrawlerAdapter
                    CrawlerAdapter(self.root).validate_url(target)
            worker_env = {**self.worker_environment(), "SYNLOVE_OPERATION_ID": self.name + ":" + key}
            self.adapter = McpAdapter(self.root, self.name, self.cfg["timeoutSeconds"], worker_env)
            if self.closed.is_set():
                raise RuntimeError("服务正在关闭，任务未执行")
            result = self.adapter.call(message)
            if self.name == "video":
                self.publish_reports(result)
        except Exception as error:
            if self.adapter is not None and message.get("method") != "tools/list":
                state = "unknown"
            result = {"jsonrpc": "2.0", "id": message.get("id"), "result": {
                "isError": True, "content": [{"type": "text", "text": str(error)}]}}
        finally:
            self.adapter = None
        try:
            self.store.finish(key, result, state)
        finally:
            self.slot.release()

    def publish_reports(self, result):
        """result为视频MCP结果；只发布reports目录内报表，附带下载ID替代跨机路径。"""
        body = result.get("result", {})
        for block in body.get("content", []):
            text = block.get("text", "")
            match = re.search(r"(?:报表：|已生成报表：)\s*(.+)", text)
            if match and Path(match[1].strip()).is_file():
                report = Path(match[1].strip()).resolve()
                work = self.root / self.cfg["workDir"] if self.cfg.get("workDir") else self.root / "runtime"
                allowed = (work / "reports").resolve()
                if not report.is_relative_to(allowed):
                    raise ValueError("报表不在视频服务输出目录内")
                from microServices.ReportBundle import ReportBundle
                key = ReportBundle(work).publish(report, self.files)
                block["text"] = text[:match.start()] + "报表文件ID：" + key
                body.setdefault("artifacts", []).append({"id": key, "name": "report.zip"})

    def core_request(self, method, path, body=None):
        """method/path为GUI请求；只转发固定本机控制台API，禁止客户端选择上游地址。"""
        if self.name != "core" or not path.startswith("/api/") or "\r" in path or "\n" in path:
            raise ValueError("非法核心API")
        cfg = {**self.cfg, "baseURL": self.cfg.get("upstreamURL", "http://127.0.0.1:3082"),
               "apiKeyEnv": ""}
        upstream = urlsplit(cfg["baseURL"])
        if upstream.hostname not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("核心服务必须与其状态控制台同机部署")
        return ServiceTransport(cfg).request(method, path, body)

    def serve(self, host, port):
        """host/port为监听地址；非回环监听强制Bearer；主线程负责关闭执行池和锁。"""
        if host not in ("127.0.0.1", "localhost", "::1") and not self.key:
            self.close()
            raise ValueError("非回环监听必须设置服务apiKeyEnv对应密钥")

        from microServices.ServiceHttpHandler import ServiceHttpHandler
        try:
            self.server = BoundedHttpServer((host, port), ServiceHttpHandler)
        except BaseException:
            self.close()
            raise
        self.server.service = self
        self.server.daemon_threads = True
        if self.cfg.get("background", True) and self.name in ("reminders", "digest"):
            threading.Thread(target=self.run_schedule, daemon=True).start()
        try:
            self.server.serve_forever(poll_interval=0.2)
        finally:
            self.close()

    def run_schedule(self):
        """无参数；服务端独占调度，提醒每30秒轮询，新闻按服务时区每日领取一次。"""
        import time
        from microServices.ScheduleStore import ScheduleStore
        schedule = ScheduleStore(self.root, self.state / "schedule.sqlite") if self.name == "digest" else None
        while not self.closed.wait(30):
            try:
                if self.slot.locked():
                    continue
                key = schedule.claim_due() if schedule else "reminder-tick-" + str(int(time.time() // 30))
                if not key:
                    continue
                method = "tools/call" if schedule else "service/tick"
                message = {"jsonrpc": "2.0", "id": key, "method": method}
                if schedule:
                    message["params"] = {"name": "digest_run", "arguments": {}}
                self.submit({"requestId": key, "message": message}, internal=True)
                if schedule:
                    schedule.confirm_enqueued(key)
            except Exception as error:
                print(f"{self.name}调度失败：{type(error).__name__}", file=sys.stderr, flush=True)

    def close(self):
        """无参数；先阻止新调度，再停止业务子进程并等待执行池退出，最后释放独占锁。"""
        self.closed.set()
        with self.registration:
            if self.adapter:
                self.adapter.close()
        self.pool.shutdown(wait=True, cancel_futures=True)
        if self.server:
            self.server.server_close()
        if self.database_lease:
            self.database_lease.close()
        self.lease.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("service", choices=ServiceHost.SERVICES)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int)
    parser.add_argument("--config")
    args = parser.parse_args()
    registry = ServiceRegistry(args.config)
    cfg = registry.get_service(args.service)
    service = ServiceHost(args.service, registry)
    import signal
    def stop_service(signum, frame):
        """signum/frame为系统退出信号；在其他线程关闭HTTP循环避免主线程死锁。"""
        if service.server:
            threading.Thread(target=service.server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, stop_service)
    signal.signal(signal.SIGINT, stop_service)
    service.serve(args.host, args.port or cfg["port"])
