"""
语音转写命令行：调用本机 whisper 服务（不在就跑起来）。

用法：
    python tools\\transcribe.py "D:\\path\\a.mp3" [--language zh] [--model small]

行为：
- 端口没监听时后台拉起 services/whisperService.py（无窗口），等 /health；
- POST 转写，打印结果文本（带分段时长）；服务闲置会自动退出，不常驻。
"""

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from services.pythonRuntime import service_python
SERVICE = ROOT / "services" / "whisperService.py"
LOG = ROOT / "runtime" / "whisper-server.log"
PORT = int(os.environ.get("WHISPER_PORT") or "8081")
BASE = f"http://127.0.0.1:{PORT}"


def port_open(port):
    """端口是否可连。"""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1.0):
            return True
    except OSError:
        return False


def ensure_service(model, idle_seconds):
    """服务没起来就后台拉起，并等健康检查通过。"""
    if port_open(PORT):
        return
    LOG.parent.mkdir(parents=True, exist_ok=True)
    flags = 0x08000000 | 0x00000008  # CREATE_NO_WINDOW | DETACHED_PROCESS
    with LOG.open("a", encoding="utf-8") as handle:
        subprocess.Popen(
            [service_python(), str(SERVICE), "--port", str(PORT), "--model", model,
             "--idle-seconds", str(idle_seconds)],
            cwd=str(ROOT), stdout=handle, stderr=handle, creationflags=flags,
        )
    for _ in range(40):
        time.sleep(0.5)
        if port_open(PORT):
            return
    raise RuntimeError(f"whisper 服务启动超时，见日志 {LOG}")


def main(argv):
    """入口。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("path", help="本地音频/视频文件路径")
    parser.add_argument("--language", default=None, help="语言代码，如 zh / en；不填自动检测")
    parser.add_argument("--model", default="small")
    parser.add_argument("--idle-seconds", type=int, default=180)
    args = parser.parse_args(argv)

    path = str(Path(args.path).expanduser().resolve())
    if not Path(path).is_file():
        print(f"文件不存在：{path}")
        return 2
    ensure_service(args.model, args.idle_seconds)

    payload = json.dumps({"path": path, "language": args.language}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(f"{BASE}/transcribe", data=payload,
                                     headers={"Content-Type": "application/json"}, method="POST")
    started = time.time()
    try:
        with urllib.request.urlopen(request, timeout=1800) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", "replace")
        print(f"转写失败 HTTP {error.code}: {body[:500]}")
        return 1
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        print(f"转写失败：{error}（日志 {LOG}）")
        return 1

    print(f"用时 {time.time() - started:.1f}s  模型 {data.get('model')}  设备 {data.get('device')}")
    for item in data.get("segments", []):
        print(f"[{item['start']:>7.2f}-{item['end']:>7.2f}] {item['text']}")
    print("--- 全文 ---")
    print(data.get("text", ""))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
