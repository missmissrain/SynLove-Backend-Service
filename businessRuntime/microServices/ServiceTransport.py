"""业务HTTP客户端。ServiceTransport(cfg) 只访问配置端点，不跟随重定向，不自动重做写操作。"""

import json
import os
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from services.ServiceCredential import ServiceCredential
from services.ServiceTls import ServiceTls


class ServiceTransport:
    """提交任务后轮询同一ID；网络失败保留ID供查询，不回退本机执行。"""

    def __init__(self, cfg):
        """cfg为ServiceRegistry返回的单个服务配置。"""
        self.cfg = cfg

    def request(self, method, path, body=None, content_type="application/json", limit=8*1024*1024):
        """method/path为固定API，body为字节；响应有界读取，错误不回显服务端私密内容。"""
        key = ServiceCredential.resolve_key(self.cfg)
        if self.cfg.get("management") == "disabled":
            raise RuntimeError("服务已禁用")
        headers = {"Content-Type": content_type}
        if key:
            headers["Authorization"] = "Bearer " + key
        req = urllib.request.Request(self.cfg["baseURL"] + path, data=body,
                                     method=method, headers=headers)
        opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=ServiceTls.create_context(self.cfg)))
        for handler in opener.handlers:
            if isinstance(handler, urllib.request.HTTPRedirectHandler):
                handler.redirect_request = lambda *args, **kwargs: None
        try:
            with opener.open(req, timeout=min(self.cfg["timeoutSeconds"], 30)) as response:
                data = response.read(limit + 1)
                if len(data) > limit:
                    raise RuntimeError("服务响应超过大小限制")
                return data
        except urllib.error.HTTPError as error:
            raise RuntimeError(f"服务返回HTTP {error.code}") from None

    def call(self, message, request_id=None):
        """message为MCP请求，request_id可用于查询已提交任务；返回MCP响应。"""
        if message.get("method") == "tools/list" and request_id is None:
            return {"jsonrpc": "2.0", "id": message.get("id"),
                    "result": json.loads(self.request("GET", "/tools"))}
        key = request_id or uuid.uuid4().hex
        payload = json.dumps({"requestId": key, "message": message}, ensure_ascii=False).encode()
        try:
            result = json.loads(self.request("POST", "/rpc", payload))
            deadline = time.monotonic() + self.cfg["timeoutSeconds"] + 30
            while result["state"] == "running":
                if time.monotonic() >= deadline:
                    raise TimeoutError("客户端等待超时，服务端任务可能仍在运行")
                time.sleep(0.3)
                result = json.loads(self.request("GET", "/operations/" + key))
            if result["state"] == "unknown":
                raise RuntimeError("服务或业务进程中断，任务结果未知；可能已产生副作用，请核查后再决定是否重做")
            if result["state"] == "expired":
                raise RuntimeError("该请求的结果缓存已过期，历史操作不会重新执行")
            return result["result"]
        except Exception as error:
            raise RuntimeError(f"请求未确认完成；requestId={key}；{type(error).__name__}: {error}") from None

    def upload(self, path):
        """path为调用机文件；上传内容后返回远端不透明ID，不传递本机绝对路径。"""
        path = Path(path)
        if not path.is_file() or not 0 < path.stat().st_size <= 256 * 1024 * 1024:
            raise ValueError("上传文件不存在或超过256MiB")
        return json.loads(self.request("POST", "/files?suffix=" + path.suffix,
                                       path.read_bytes(), "application/octet-stream"))["id"]

    def download(self, key):
        """key为服务端返回的文件ID；返回文件字节，最多256MiB。"""
        return self.request("GET", "/files/" + key, limit=256 * 1024 * 1024)
