"""视频理解工具（MCP stdio）：下载 → ASR → 抽帧拼图 → VLM → 报表。

**只在用户明确要求"分析/看看这个视频"时调用**；不要因为聊天里出现链接就主动跑。

工具：
    video_digest(pathOrUrl, frames?, language?)
"""

import io
import json
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
RUNNER = ROOT / "videoDigest" / "runVideoDigest.py"
PYTHON = sys.executable

TOOLS = [{
    "name": "video_digest",
    "description": (
        "分析一个视频（本地路径或链接），产出 Markdown 报表（摘要/时间线/转写/关键帧）。"
        "**仅在用户明确要求看/分析视频时调用**，不要主动调用。"
        "首次遇到需要登录的站点会失败并提示用户先登录。"
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "pathOrUrl": {"type": "string", "description": "视频链接或本地文件绝对路径"},
            "frames": {"type": "integer", "minimum": 1, "maximum": 120, "description": "抽多少帧，默认20，最多120"},
            "language": {"type": "string", "description": "ASR 语言，如 zh / en；不填自动"},
        },
        "required": ["pathOrUrl"],
    },
}]


def run_digest(arguments):
    """跑一次 videoDigest 流水线，返回摘要与报表路径。"""
    target = str(arguments.get("pathOrUrl", "")).strip()
    if target == "":
        raise ValueError("pathOrUrl 不能为空")
    command = [PYTHON, str(RUNNER)]
    command += ["--url", target] if target.lower().startswith(("http://", "https://")) else ["--file", target]
    if arguments.get("frames"):
        command += ["--frames", str(int(arguments["frames"]))]
    if arguments.get("language"):
        command += ["--language", str(arguments["language"])]

    result = subprocess.run(
        command, cwd=str(ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=3600,
        env={**__import__("os").environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
    )
    output = ((result.stdout or "") + (result.stderr or "")).strip()
    summary = ""
    report = ""
    for line in output.splitlines():
        if line.startswith("摘要："):
            summary = line[3:].strip()
        elif line.startswith("报表："):
            report = line[3:].strip()
        elif line.startswith("需要登录"):
            raise RuntimeError(f"服务端站点需要登录：{line}。请在服务所在机器配置登录凭据后重试。")
    if result.returncode != 0 and report == "":
        raise RuntimeError(f"视频分析失败（退出码 {result.returncode}）：\n{output[-600:]}")
    if not report or not Path(report).is_file():
        raise RuntimeError("视频处理未生成可验证报表")
    return f"{summary}\n\n报表：{report}" if summary else f"已生成报表：{report}"


def handle(message):
    """JSON-RPC 分发。"""
    method = message.get("method")
    request_id = message.get("id")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": request_id, "result": {
            "protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
            "serverInfo": {"name": "video-digest", "version": "1.0.0"}}}
    if method in ("notifications/initialized", "notifications/cancelled"):
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": request_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = message.get("params") or {}
        try:
            if params.get("name") != "video_digest":
                raise ValueError(f"未知工具：{params.get('name')}")
            text = run_digest(params.get("arguments") or {})
            return {"jsonrpc": "2.0", "id": request_id, "result": {"content": [{"type": "text", "text": text}]}}
        except Exception as error:  # noqa: BLE001
            return {"jsonrpc": "2.0", "id": request_id, "result": {
                "content": [{"type": "text", "text": f"视频分析失败：{type(error).__name__}: {error}"}],
                "isError": True}}
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": f"unsupported: {method}"}}


def main():
    """stdio 循环。"""
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        response = handle(message)
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    from microServices.ServiceMcpBridge import ServiceMcpBridge
    ServiceMcpBridge("video", ROOT).run_stdio()
