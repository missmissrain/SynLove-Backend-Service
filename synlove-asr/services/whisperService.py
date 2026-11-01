"""
语音转写服务（faster-whisper，本地 HTTP）。

职责：
- 首次请求时**懒加载**模型（默认 small；显存不足自动退 CPU int8），之后复用；
- 提供 `POST /transcribe {"path": "...", "language": "zh"}` 与 `GET /health`；
- **闲置 N 秒自动退出进程**（释放端口与显存）；
- 模型下载默认走国内镜像（HF_ENDPOINT=https://hf-mirror.com，可用环境变量覆盖）。

启动参数（均可选）：
    --port 8081         监听端口
    --model small       模型：tiny/base/small/medium/large-v3
    --device auto       auto / cuda / cpu；auto 时按空闲显存决定
    --idle-seconds 180  闲置多久自动退出（0=不退出）
"""

import argparse
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# 国内直连 HuggingFace 常常超时；镜像对 faster-whisper 的模型同样有效
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

_lock = threading.Lock()
_state = {"model": None, "device": "", "last_active": time.time(), "model_name": "", "requested_device": "auto"}


def log(message):
    """带时间戳打印到 stdout（启动方负责重定向）。"""
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)


def pick_device(requested):
    """按空闲显存选设备：auto 且空闲 <900MB 时用 CPU。"""
    if requested in ("cuda", "cpu"):
        return requested
    try:
        import torch
    except Exception:
        return "cpu"
    if not torch.cuda.is_available():
        return "cpu"
    try:
        free, _total = torch.cuda.mem_get_info()
        if free / 1024 / 1024 >= 900:
            return "cuda"
        log(f"空闲显存不足（{free / 1024 / 1024:.0f}MB），改用 CPU")
    except Exception:
        return "cpu"
    return "cpu"


def load_model(model_name, requested_device):
    """懒加载模型；重复调用复用已加载实例。"""
    with _lock:
        if _state["model"] is not None:
            return _state["model"], _state["device"]
        from faster_whisper import WhisperModel

        device = pick_device(requested_device)
        started = time.time()
        log(f"加载 faster-whisper 模型 {model_name}（device={device}）…")
        if device == "cuda":
            try:
                model = WhisperModel(model_name, device="cuda", compute_type="float16")
            except Exception as error:  # 常见于缺 cuDNN：退回 CPU
                log(f"CUDA 加载失败（{error}），退回 CPU int8")
                device = "cpu"
                model = WhisperModel(model_name, device="cpu", compute_type="int8")
        else:
            model = WhisperModel(model_name, device="cpu", compute_type="int8")
        _state["model"] = model
        _state["device"] = device
        log(f"模型就绪，用时 {time.time() - started:.1f}s")
        return model, device


def transcribe(path, language, vad_filter=True):
    """转写一个本地音频/视频文件，返回全文与分段；CUDA 显存不足时自动改 CPU 重试一次。"""
    if not os.path.isfile(path):
        raise FileNotFoundError(path)
    model, device = load_model(_state["model_name"], _state["requested_device"])

    def run(current_model):
        """真正解码（faster-whisper 是生成器，异常可能发生在取段时）。"""
        started = time.time()
        raw_segments, info = current_model.transcribe(path, language=language, vad_filter=bool(vad_filter))
        segments = [
            {"start": round(float(item.start), 2), "end": round(float(item.end), 2), "text": (item.text or "").strip()}
            for item in raw_segments
        ]
        return segments, info, time.time() - started

    try:
        segments, info, elapsed = run(model)
    except RuntimeError as error:
        message = str(error).lower()
        if device != "cuda" or ("out of memory" not in message and "cuda failed" not in message):
            raise
        log(f"CUDA 显存不足（{error}），卸载后改 CPU int8 重试")
        with _lock:
            _state["model"] = None
            _state["device"] = ""
        try:
            import torch
            torch.cuda.empty_cache()
        except Exception:
            pass
        model, device = load_model(_state["model_name"], "cpu")
        segments, info, elapsed = run(model)
    text = "".join(item["text"] for item in segments).strip()
    log(f"转写完成：{os.path.basename(path)} 用时 {elapsed:.1f}s 字数 {len(text)} 语言 {info.language}")
    return {"ok": True, "text": text, "segments": segments, "device": device,
            "model": _state["model_name"], "language": info.language}


class Handler(BaseHTTPRequestHandler):
    """极简 HTTP：/health 与 /transcribe。"""

    server_version = "whisper-service/1.1"

    def _reply(self, code, payload):
        """写 JSON 响应。"""
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        """健康检查。"""
        if self.path.startswith("/health"):
            return self._reply(200, {"ok": True, "service": "asr", "protocol": 1,
                                     "loaded": _state["model"] is not None,
                                     "device": _state["device"], "model": _state.get("model_name")})
        return self._reply(404, {"ok": False, "error": "unknown path"})

    def do_POST(self):
        """转写入口 / 卸载。"""
        if self.path.startswith("/unload"):
            self._reply(200, {"ok": True, "note": "服务即将退出"})
            log("收到 /unload，退出进程")
            os._exit(0)
        if not self.path.startswith("/transcribe"):
            return self._reply(404, {"ok": False, "error": "unknown path"})
        try:
            length = int(self.headers.get("Content-Length") or "0")
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            _state["last_active"] = time.time()
            result = transcribe(str(payload.get("path", "")), payload.get("language"), bool(payload.get("vadFilter", True)))
            _state["last_active"] = time.time()
            return self._reply(200, result)
        except Exception as error:  # noqa: BLE001
            log(f"转写失败：{type(error).__name__}: {error}")
            return self._reply(500, {"ok": False, "error": f"{type(error).__name__}: {error}"})

    def log_message(self, fmt, *args):  # noqa: D401
        """压掉默认访问日志。"""
        return


def idle_watchdog(idle_seconds):
    """闲置超时后退出进程（释放端口与显存）。"""
    if idle_seconds <= 0:
        return
    while True:
        time.sleep(min(15, max(5, idle_seconds // 4)))
        if _state["model"] is None:
            continue
        if time.time() - _state["last_active"] >= idle_seconds:
            log(f"闲置 {idle_seconds}s，退出以释放端口/显存")
            os._exit(0)


def main(argv):
    """入口：解析参数 → 起服务。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8081)
    parser.add_argument("--model", default="small")
    parser.add_argument("--device", default="auto")
    parser.add_argument("--idle-seconds", type=int, default=180)
    args = parser.parse_args(argv)

    _state["model_name"] = args.model
    _state["requested_device"] = args.device
    threading.Thread(target=idle_watchdog, args=(args.idle_seconds,), daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    log(f"whisper 服务启动：端口 {args.port} 模型 {args.model} device={args.device} 闲置退出 {args.idle_seconds}s")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main(sys.argv[1:])
