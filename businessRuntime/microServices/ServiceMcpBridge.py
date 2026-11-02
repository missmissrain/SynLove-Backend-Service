"""MCP到业务服务桥接。ServiceMcpBridge(name,root) 保留stdio协议，只通过统一端点调用业务。"""

import json
import sys
from pathlib import Path

from microServices.ServiceManager import ServiceManager
from microServices.ServiceTransport import ServiceTransport


class ServiceMcpBridge:
    """工具定义和执行都来自服务端；桥接器不读取业务库、不加载模型。"""

    def __init__(self, name, root=None):
        """name为固定服务名；root为调用方代码目录。"""
        self.name = name
        self.root = Path(root or Path(__file__).resolve().parents[1])
        self.manager = ServiceManager(self.root)

    def handle(self, message):
        """message为MCP请求；初始化在本机完成，工具请求转发到同一服务端。"""
        method = message.get("method")
        key = message.get("id")
        if key is None:
            return None
        if method == "initialize":
            return {"jsonrpc": "2.0", "id": key, "result": {
                "protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                "serverInfo": {"name": "synlove-" + self.name, "version": "2.0.0"}}}
        if method == "ping":
            return {"jsonrpc": "2.0", "id": key, "result": {}}
        if method not in ("tools/list", "tools/call"):
            return {"jsonrpc": "2.0", "id": key,
                    "error": {"code": -32601, "message": "unsupported method"}}
        try:
            self.manager.ensure_service(self.name)
            cfg = self.manager.registry.get_service(self.name)
            transport = ServiceTransport(cfg)
            message = json.loads(json.dumps(message))
            if self.name == "video" and method == "tools/call":
                args = message.setdefault("params", {}).setdefault("arguments", {})
                target = str(args.get("pathOrUrl", ""))
                if not target.startswith(("http://", "https://", "artifact:")):
                    args["pathOrUrl"] = "artifact:" + transport.upload(target)
            result = transport.call(message)
            # 把远端报表下载到调用机，模型看到的是确实存在的本机文件。
            if self.name == "video" and method == "tools/call":
                for artifact in result.get("result", {}).get("artifacts", []):
                    key_file = artifact["id"]
                    from microServices.ArtifactStore import ArtifactStore
                    target = ArtifactStore(self.root / "runtime" / "serviceDownloads").resolve(key_file)
                    target.write_bytes(transport.download(key_file))
                    result["result"].setdefault("content", []).append({
                        "type": "text", "text": "已下载报表：" + str(target)})
            result["id"] = key
            return result
        except Exception as error:
            return {"jsonrpc": "2.0", "id": key, "result": {
                "isError": True, "content": [{"type": "text", "text": str(error)}]}}

    def run_stdio(self):
        """无参数；逐行处理有界JSON输入；异常不破坏后续协议会话。"""
        for line in sys.stdin:
            try:
                if len(line) > 1024 * 1024:
                    raise ValueError("MCP请求过大")
                response = self.handle(json.loads(line))
            except (ValueError, TypeError, AttributeError):
                response = {"jsonrpc": "2.0", "id": None,
                            "error": {"code": -32700, "message": "invalid request"}}
            if response is not None:
                print(json.dumps(response, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    ServiceMcpBridge(sys.argv[1]).run_stdio()
