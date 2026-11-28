"""新闻服务工具。默认仅作为MCP→HTTP代理；Worker内运行摘要或维护服务SQLite日程，不创建系统任务。"""

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
CONFIG_PATH = Path(os.environ.get("SYNLOVE_DIGEST_CONFIG") or ROOT / "newsDigest" / "config" / "newsDigest.json")
RUN_SCRIPT = ROOT / "newsDigest" / "runDigest.py"

TOOLS = [
    {"name": "digest_schedule", "description": "查询或修改独立新闻服务的每日计划，时间按服务端时区解释。",
     "inputSchema": {"type": "object", "properties": {
         "action": {"type": "string", "enum": ["get", "set", "enable", "disable", "delete"]},
         "time": {"type": "string", "description": "每日HH:MM"},
         "enabled": {"type": "boolean"},
         "interest": {"type": "string", "description": "新闻关注方向"},
         "maxItems": {"type": "integer", "minimum": 1, "maximum": 8},
         "maxChars": {"type": "integer", "minimum": 60, "maximum": 400}},
         "required": ["action"]}},
    {"name": "digest_run", "description": "执行新闻采集、筛选、摘要与推送；dry_run只生成内容，不发送。",
     "inputSchema": {"type": "object", "properties": {
         "dry_run": {"type": "boolean"},
         "limit": {"type": "integer", "minimum": 1, "maximum": 200}}}},
]


def read_config():
    """无参数；读取服务端新闻配置，读取失败明确报错，避免用空字典覆盖原设置。"""
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
    if not isinstance(config, dict):
        raise ValueError("新闻配置必须为对象")
    return config


def write_config(config):
    """config为完整新闻配置；同目录临时文件原子替换，失败保留原配置。"""
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=CONFIG_PATH.parent,
                                         prefix=".digest-", suffix=".tmp", delete=False) as handle:
            name = handle.name
            json.dump(config, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, CONFIG_PATH)
    finally:
        if name:
            Path(name).unlink(missing_ok=True)


def tool_digest_schedule(arguments):
    """arguments为日程操作与画像设置；数据写服务端SQLite，不操作Windows任务。"""
    from microServices.ScheduleStore import ScheduleStore
    return ScheduleStore(ROOT).configure(arguments, sys.modules[__name__])


def tool_digest_run(arguments):
    """arguments含dry_run/limit；调用服务端流水线，失败通过MCP isError反馈。"""
    command = [sys.executable, str(RUN_SCRIPT)]
    if arguments.get("dry_run", False):
        command.append("--dry-run")
    if "limit" in arguments:
        limit = int(arguments["limit"])
        if not 1 <= limit <= 200:
            raise ValueError("limit必须在1至200之间")
        command.append("--limit=" + str(limit))
    started = time.monotonic()
    result = subprocess.run(command, cwd=str(ROOT), capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=900,
                            env={**os.environ, "SYNLOVE_SERVICE_WORKER": "1", "PYTHONUTF8": "1"})
    output = "\n".join(((result.stdout or "") + (result.stderr or "")).splitlines()[-12:])
    if result.returncode:
        raise RuntimeError(f"新闻摘要失败，退出码{result.returncode}。\n{output}")
    return f"本轮摘要成功，用时{time.monotonic() - started:.1f}s。\n{output}"


def handle_request(message):
    """message为Worker内的JSON-RPC请求；保持digest_schedule/digest_run工具名兼容。"""
    key = message.get("id")
    if message.get("method") == "tools/list":
        return {"jsonrpc": "2.0", "id": key, "result": {"tools": TOOLS}}
    try:
        params = message.get("params") or {}
        args = params.get("arguments") or {}
        if message.get("method") != "tools/call":
            raise ValueError("不支持的业务方法")
        if params.get("name") == "digest_schedule":
            text = tool_digest_schedule(args)
        elif params.get("name") == "digest_run":
            text = tool_digest_run(args)
        else:
            raise ValueError("未知新闻工具")
        body = {"content": [{"type": "text", "text": text}]}
    except Exception as error:
        body = {"isError": True, "content": [{"type": "text", "text": str(error)}]}
    return {"jsonrpc": "2.0", "id": key, "result": body}


if __name__ == "__main__":
    from microServices.ServiceMcpBridge import ServiceMcpBridge
    ServiceMcpBridge("digest", ROOT).run_stdio()
