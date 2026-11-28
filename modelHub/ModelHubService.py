"""统一模型调度 HTTP 服务。

职责：提供模型目录、权重清单、显存状态、模型健康检查和安全的加载/卸载协调。
它保持 TTS、LLM、ASR、ACE-Step、MOSS 等原服务端口与业务 API 不变，
只通过各服务约定的管理接口操作驻留状态。
注册参数：root 为后台根目录；config_path 为 modelHub.json；host/port 为管理接口。
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .CloudGateway import CloudGateway
from .GpuInspector import GpuInspector
from .ModelLease import ModelLease
from .ModelRegistry import ModelRegistry
from gpuRuntime.MemoryInspector import MemoryInspector
from gpuRuntime.SchedulerState import SchedulerState


class ModelHubService:
    """单实例模型调度器；所有 API 操作共享一个进程内锁和跨进程租约。"""

    def __init__(self, root: str | os.PathLike[str], config_path: str | os.PathLike[str] | None = None):
        """root 为后台仓库根目录；config_path 可选，用于服务器覆盖默认登记表。"""
        self.root = Path(root).resolve()
        self.registry = ModelRegistry(self.root, config_path)
        self.secret_values = self._read_secret_env()
        runtime = self.root / "runtime" / "modelHub"
        self.inspector = GpuInspector()
        self.lease = ModelLease(runtime / "scheduler.lock", runtime / "lease.json")
        self.operation_lock = threading.RLock()
        self.cloud = CloudGateway(self.root, self.registry.data)
        self.started_at = time.time()

    def _read_secret_env(self) -> dict[str, str]:
        """无参数；读取配置中指定的本机 env 文件，仅在内存中使用，不打印或写回。"""
        raw_path = self.registry.data.get("service", {}).get("secretEnvFile", "")
        if raw_path and not os.path.isabs(raw_path):
            raw_path = str((Path(self.root) / raw_path).resolve())
        if not raw_path:
            return {}
        path = Path(os.path.expandvars(str(raw_path)))
        if not path.is_absolute():
            path = self.root / path
        if not path.is_file():
            return {}
        values: dict[str, str] = {}
        for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            text = line.strip()
            if not text or text.startswith("#") or "=" not in text:
                continue
            key, value = text.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
        return values

    def _request_json(self, url: str, method: str = "GET", token_env: str = "", timeout: float = 5.0) -> dict[str, Any]:
        """url 为回环管理接口；method 为 GET/POST；token_env 为可选鉴权环境变量名。"""
        headers = {"Accept": "application/json"}
        token = (os.environ.get(token_env, "") or self.secret_values.get(token_env, "")) if token_env else ""
        if token:
            headers["Authorization"] = "Bearer " + token
        request = urllib.request.Request(url, headers=headers, data=b"{}" if method == "POST" else None, method=method)
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=timeout) as response:
                raw = response.read(1024 * 1024)
                return {"httpStatus": response.status, **json.loads(raw.decode("utf-8"))}
        except urllib.error.HTTPError as error:
            return {"httpStatus": error.code, "ok": False, "error": error.read(4096).decode("utf-8", "replace")}
        except Exception as error:  # noqa: BLE001
            return {"httpStatus": 0, "ok": False, "error": f"{type(error).__name__}: {error}"}

    def _model(self, name: str) -> dict[str, Any] | None:
        """name 为登记模型名；返回对应配置或 None。"""
        return next((item for item in self.registry.models() if item.get("name") == name), None)

    def model_status(self, item: dict[str, Any]) -> dict[str, Any]:
        """item 为模型登记；调用其健康管理接口，区分服务在线与权重已加载。"""
        result = dict(item)
        health = item.get("healthUrl")
        result["serviceOnline"] = False
        result["loaded"] = False
        if health:
            reply = self._request_json(str(health), token_env=str(item.get("tokenEnv", "")))
            result["serviceOnline"] = 200 <= int(reply.get("httpStatus", 0)) < 500 and reply.get("ok", True) is not False
            result["loaded"] = bool(reply.get("loaded") or reply.get("runtimeLoaded") or reply.get("modelLoaded"))
            result["health"] = {key: value for key, value in reply.items() if key not in {"authorization", "token"}}
        return result

    def status(self) -> dict[str, Any]:
        """无参数；返回 ModelHub 自身、显存、模型目录和当前租约。"""
        lease_path = self.lease.lease_path
        lease = None
        if lease_path.exists():
            try:
                lease = json.loads(lease_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                lease = None
        inventory = self.registry.inventory()
        with ThreadPoolExecutor(max_workers=max(1, min(8, len(inventory)))) as pool:
            models = list(pool.map(self.model_status, inventory))
        return {
            "ok": True,
            "service": "modelhub",
            "protocol": 1,
            "uptimeSeconds": round(time.time() - self.started_at, 2),
            "gpu": self.inspector.status(),
            "memory": MemoryInspector().read_memory(),
            "scheduler": SchedulerState(self.root).read_status(),
            "lease": lease,
            "models": models,
        }

    def unload(self, name: str) -> dict[str, Any]:
        """name 为模型登记名；只调用原服务管理接口，不终止未知进程。"""
        item = self._model(name)
        if item is None:
            return {"ok": False, "error": "模型未登记"}
        url = item.get("unloadUrl")
        if not url:
            return {"ok": False, "error": "模型未配置安全卸载接口"}
        with self.operation_lock:
            reply = self._request_json(str(url), "POST", str(item.get("tokenEnv", "")), timeout=30)
        return {"ok": bool(reply.get("ok")) and not bool(reply.get("loaded")),
                "model": name, "reply": reply}

    def load(self, name: str) -> dict[str, Any]:
        """name 为模型登记名；调用可选加载接口，否则返回原服务的按需加载说明。"""
        item = self._model(name)
        if item is None:
            return {"ok": False, "error": "模型未登记"}
        url = item.get("loadUrl")
        if url:
            with self.operation_lock:
                reply = self._request_json(str(url), "POST", str(item.get("tokenEnv", "")), timeout=30)
            return {"ok": bool(reply.get("ok")), "model": name, "reply": reply}
        state = self.model_status(item)
        return {
            "ok": bool(item.get("enabled", True) and state.get("serviceOnline")),
            "model": name,
            "loaded": bool(state.get("loaded")),
            "mode": "lazy",
            "message": "该后端保持 API 常驻并按首个业务请求载入权重，ModelHub 不伪造一次推理请求。",
        }

    def unload_peers(self, target: str) -> dict[str, Any]:
        """target 为目标模型；按卸载优先级请求其他可让位模型软卸载。"""
        item = self._model(target)
        if item is None or not item.get("enabled", True) or not item.get("healthUrl"):
            return {"ok": False, "target": target, "error": "目标模型未登记或没有运行接口",
                    "released": []}
        records = []
        for item in sorted(self.registry.models(), key=lambda value: int(value.get("releasePriority", 100))):
            if item.get("name") == target or not item.get("enabled", True) or not item.get(
                    "resourceManaged", item.get("gpuManaged", False)):
                continue
            result = self.unload(str(item.get("name")))
            result.setdefault("model", item.get("name"))
            records.append(result)
        return {"ok": all(item.get("ok") for item in records), "target": target, "released": records}

    def health(self) -> dict[str, Any]:
        """无参数；返回不触发模型加载的轻量健康结果。"""
        return {"ok": True, "service": "modelhub", "protocol": 1,
                "gpu": self.inspector.status(), "memory": MemoryInspector().read_memory()}


class ModelHubHandler(BaseHTTPRequestHandler):
    """ModelHub 管理接口处理器，仅监听服务器回环地址。"""

    hub: ModelHubService | None = None

    def log_message(self, _format: str, *_args) -> None:
        """压低默认访问日志，避免把请求内容写入日志。"""
        return

    def _reply(self, code: int, payload: dict[str, Any]) -> None:
        """code 为 HTTP 状态码，payload 为 JSON 响应对象。"""
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        """处理健康、模型目录、用量与云端反代。"""
        if self.hub is None:
            return self._reply(503, {"ok": False, "error": "modelhub 未初始化"})
        if self.hub.cloud.handle(self):
            return
        route = self.path.split("?", 1)[0]
        if route in {"/health", "/healthz"}:
            return self._reply(200, self.hub.health())
        if route in {"/v1/models", "/v1/gpu/models"}:
            return self._reply(200, {"ok": True, "models": self.hub.status()["models"]})
        if route in {"/v1/gpu/status", "/status"}:
            return self._reply(200, self.hub.status())
        return self._reply(404, {"ok": False, "error": "route not found"})

    def do_POST(self) -> None:
        """处理模型卸载与同伴让位请求，操作前读取 JSON 请求体。"""
        if self.hub is None:
            return self._reply(503, {"ok": False, "error": "modelhub 未初始化"})
        if self.hub.cloud.handle(self):
            return
        route = self.path.split("?", 1)[0]
        length = min(int(self.headers.get("Content-Length") or 0), 1024 * 1024)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._reply(400, {"ok": False, "error": "请求体不是 JSON"})
        if not isinstance(payload, dict):
            return self._reply(400, {"ok": False, "error": "请求体必须是JSON对象"})
        if route == "/v1/gpu/unload":
            name = str(payload.get("model") or "")
            result = self.hub.unload(name)
            return self._reply(200 if result.get("ok") else 409, result)
        if route == "/v1/gpu/load":
            name = str(payload.get("model") or "")
            result = self.hub.load(name)
            return self._reply(200 if result.get("ok") else 409, result)
        if route == "/v1/gpu/release-peers":
            target = str(payload.get("target") or "")
            result = self.hub.unload_peers(target)
            return self._reply(200 if result.get("ok") else 409, result)
        if route == "/v1/gpu/reload":
            self.hub.registry.reload()
            return self._reply(200, {"ok": True, "models": self.hub.registry.inventory()})
        return self._reply(404, {"ok": False, "error": "route not found"})

    def do_PUT(self) -> None:
        """PUT 只用于云端反代。"""
        if self.hub is None or not self.hub.cloud.handle(self):
            return self._reply(404, {"ok": False, "error": "route not found"})

    def do_DELETE(self) -> None:
        """DELETE 只用于云端反代。"""
        if self.hub is None or not self.hub.cloud.handle(self):
            return self._reply(404, {"ok": False, "error": "route not found"})

    def _serve(self) -> None:
        """无参数；兼容测试调用的统一 HTTP 处理入口。"""
        return


def run_model_hub(root: str, host: str = "127.0.0.1", port: int = 8798, config_path: str | None = None) -> None:
    """root 为后台仓库根目录，host/port 为管理接口监听地址，config_path 为登记表路径。"""
    hub = ModelHubService(root, config_path)
    ModelHubHandler.hub = hub
    server = ThreadingHTTPServer((host, port), ModelHubHandler)
    server.daemon_threads = True
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] ModelHub 就绪：{host}:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
