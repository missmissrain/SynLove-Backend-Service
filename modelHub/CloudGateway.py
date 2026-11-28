"""ModelHub 云端模型反代。

职责：把 /<provider>/v1/... 原样转发到对应云端或本地网关，注入密钥，
并把每次调用的状态、token 与耗时写进 UsageMeter。
注册参数：root 为后台仓库根目录；config 为已加载的 modelHub 配置。
"""

from __future__ import annotations

import http.client
import json
import os
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .UsageMeter import UsageMeter


class CloudGateway:
    """按路径前缀转发云端模型请求，密钥只在服务端保存。"""

    def __init__(self, root: str | os.PathLike[str], config: dict[str, Any]):
        """root 为后台仓库根目录；config 为 modelHub.json 内容。"""
        self.root = Path(root).resolve()
        self.cloud = config.get("cloud") or {}
        self.enabled = bool(self.cloud.get("enabled", True))
        self.providers: dict[str, dict[str, Any]] = dict(self.cloud.get("providers") or {})
        secrets_path = Path(os.environ.get("MODELHUB_SECRETS", self.root / "config" / "modelHubSecrets.json"))
        self.secrets = self._read_secrets(secrets_path)
        usage_path = Path(os.environ.get("MODELHUB_USAGE", self.root / "runtime" / "modelHub" / "usage.jsonl"))
        self.meter = UsageMeter(usage_path, int(self.cloud.get("usageRetentionDays", 30)))
        self.timeout = float(self.cloud.get("timeoutSeconds", 600))

    def _read_secrets(self, path: Path) -> dict[str, Any]:
        """读取私有密钥文件；不存在时返回空表。"""
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8-sig"))
        except ValueError:
            return {}

    def api_key(self, name: str, provider: dict[str, Any]) -> str:
        """返回 provider 的密钥：显式 apiKey > secrets 文件 > 环境变量。"""
        if provider.get("apiKey"):
            return str(provider["apiKey"])
        secret = ((self.secrets.get("providers") or {}).get(name) or {}).get("apiKey")
        if secret:
            return str(secret)
        env_name = provider.get("apiKeyEnv")
        return str(os.environ.get(env_name, "")) if env_name else ""

    def _target(self, path: str) -> tuple[str, str, str] | None:
        """把 /<provider>/v1/... 解析成 (provider, upstream_url, rest)。"""
        raw = path.split("?", 1)[0]
        query = path[len(raw):]
        parts = raw.lstrip("/").split("/", 1)
        if len(parts) != 2 or parts[0] not in self.providers:
            return None
        provider = self.providers[parts[0]]
        rest = parts[1]
        if rest == "v1":
            rest = ""
        elif rest.startswith("v1/"):
            rest = rest[3:]
        base = str(provider.get("baseURL") or "").rstrip("/")
        if not base:
            return None
        return parts[0], base + ("/" + rest if rest else "") + query, rest

    def handle(self, handler: Any) -> bool:
        """尝试按云端路由处理请求；不是云端路径时返回 False。"""
        if not self.enabled:
            return False
        route = handler.path.split("?", 1)[0]
        if route == "/v1/usage" and handler.command in ("GET", "HEAD"):
            return self._reply_json(handler, 200, self.meter.summary())
        if route in ("/v1/cloud/status", "/cloud/status") and handler.command == "GET":
            return self._reply_json(handler, 200, {"ok": True, "providers": sorted(self.providers.keys()),
                                                   "usage": self.meter.summary()["today"]})
        target = self._target(handler.path)
        if target is None:
            return False
        name, url, _rest = target
        self._proxy(handler, name, url)
        return True

    def _reply_json(self, handler: Any, code: int, payload: dict[str, Any]) -> bool:
        """输出 JSON 响应并返回 True。"""
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        handler.send_response(code)
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        handler.send_header("Content-Length", str(len(data)))
        handler.end_headers()
        handler.wfile.write(data)
        return True

    def _read_body(self, handler: Any) -> bytes:
        """读取请求体，上限 20MB。"""
        length = min(int(handler.headers.get("Content-Length") or 0), 20 * 1024 * 1024)
        return handler.rfile.read(length) if length else b""

    def _proxy(self, handler: Any, name: str, url: str) -> None:
        """转发一次请求，记录状态与用量；失败时返回 502。"""
        provider = self.providers[name]
        body = self._read_body(handler)
        model = ""
        if body and "application/json" in (handler.headers.get("Content-Type") or ""):
            try:
                model = str(json.loads(body).get("model") or "")
            except ValueError:
                model = ""
        headers = {"Content-Type": handler.headers.get("Content-Type") or "application/json",
                   "Accept": handler.headers.get("Accept") or "application/json"}
        key = self.api_key(name, provider)
        if key:
            headers["Authorization"] = "Bearer " + key
        for header, value in (provider.get("headers") or {}).items():
            headers[str(header)] = str(value)

        parts = urlsplit(url)
        connection_class = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
        started = time.time()
        status = 502
        error = ""
        stream = False
        prompt_tokens = 0
        completion_tokens = 0
        try:
            connection = connection_class(parts.netloc, timeout=self.timeout)
            path = parts.path + ("?" + parts.query if parts.query else "")
            connection.request(handler.command, path, body=body if body else None, headers=headers)
            response = connection.getresponse()
            status = response.status
            content_type = response.getheader("Content-Type") or "application/json"
            stream = "text/event-stream" in content_type
            handler.send_response(status)
            handler.send_header("Content-Type", content_type)
            if stream:
                handler.send_header("Cache-Control", "no-cache")
                handler.end_headers()
                while True:
                    chunk = response.read(4096)
                    if not chunk:
                        break
                    handler.wfile.write(chunk)
                    handler.wfile.flush()
                raw = b""
            else:
                raw = response.read()
                handler.send_header("Content-Length", str(len(raw)))
                handler.end_headers()
                handler.wfile.write(raw)
            if raw and "json" in content_type:
                try:
                    payload = json.loads(raw)
                    usage = payload.get("usage") or {}
                    prompt_tokens = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
                    completion_tokens = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
                except ValueError:
                    pass
            connection.close()
        except Exception as exc:  # noqa: BLE001 - 代理失败要落台账而不是中断服务
            error = "%s: %s" % (type(exc).__name__, str(exc))
            status = status if status != 502 else 502
            try:
                self._reply_json(handler, 502, {"ok": False, "error": error[:200]})
            except Exception:
                pass
        self.meter.record(name, model, status, (time.time() - started) * 1000,
                          prompt_tokens, completion_tokens, stream, error)
