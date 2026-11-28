"""隔离业务调用入口。参数 service 选择固定MCP模块；stdin单条JSON，stdout只输出协议结果。"""

import contextlib
import importlib.util
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class McpWorker:
    """按固定服务名加载已有实现，不接受客户端指定Python模块或任意命令。"""

    MODULES = {
        "reminders": "reminders/mcp/reminderMcp.py",
        "digest": "newsDigest/mcp/digestMcp.py",
        "video": "videoDigest/mcp/videoMcp.py",
    }

    def __init__(self, name):
        """name为白名单服务名；业务实现只在实际调用时加载。"""
        self.name = name

    def handle(self, message):
        """message为MCP JSON-RPC请求；调用已有工具分发，保持原有工具名和参数。"""
        if message.get("method") == "service/snapshot" and self.name in ("reminders", "digest"):
            from microServices.BusinessSnapshot import BusinessSnapshot
            return {"jsonrpc": "2.0", "id": message.get("id"),
                    "result": BusinessSnapshot(self.name, ROOT).read_snapshot()}
        if self.name == "crawler":
            from microServices.CrawlerAdapter import CrawlerAdapter
            return CrawlerAdapter(ROOT).handle(message)
        spec = importlib.util.spec_from_file_location("synlove_worker_" + self.name,
                                                     ROOT / self.MODULES[self.name])
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        if message.get("method") == "service/tick" and self.name == "reminders":
            count = module.svc.fire_due()
            return {"jsonrpc": "2.0", "id": message.get("id"), "result": {"fired": count}}
        handler = module.handle_request if self.name == "digest" else module.handle
        return handler(message)


if __name__ == "__main__":
    os.environ["SYNLOVE_SERVICE_WORKER"] = "1"
    message = json.loads(sys.stdin.buffer.readline())
    with contextlib.redirect_stdout(sys.stderr):
        result = McpWorker(sys.argv[1]).handle(message)
    print(json.dumps(result, ensure_ascii=False), flush=True)
