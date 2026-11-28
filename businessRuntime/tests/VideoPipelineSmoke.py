"""视频流水线轻量冒烟。复用已有Pillow/ffmpeg，临时生成一秒视频，ASR和Gemma用HTTP替身，不调用真实模型。"""

import json
import os
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "videoDigest")]


class VideoPipelineSmoke:
    """验证真实音频抽取、抽帧、拼图、API调用和报告文件，不能代替真实模型效果评测。"""

    def run_smoke(self):
        """无参数；所有视频、音频、图片、配置和报告仅保存在临时目录。"""
        calls = []

        class Handler(BaseHTTPRequestHandler):
            """测试模型HTTP端点，只返回固定内容。"""

            def log_message(self, *args):
                """args为标准访问日志字段；测试不记录请求正文。"""
                return

            def do_POST(self):
                """无参数；消费真实上传，再返回ASR或Chat兼容结果。"""
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                calls.append(self.path)
                data = {"text": "这是隔离测试短视频。", "segments": [], "language": "zh"} if \
                    self.path.endswith("/transcriptions") else {
                        "choices": [{"message": {"content": "白色背景的测试画面，用于验证视频流程。"}}]}
                body = json.dumps(data, ensure_ascii=False).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                video = root / "sample.mp4"
                subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                                "-f", "lavfi", "-i", "color=c=white:s=160x120:d=1",
                                "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                                "-shortest", "-c:v", "mpeg4", "-c:a", "aac", str(video)],
                               check=True, timeout=30)
                base = "http://127.0.0.1:" + str(server.server_address[1]) + "/v1"
                registry = root / "serviceConfig.json"
                registry.write_text(json.dumps({"version": 1, "services": {
                    "llm": {"baseURL": base, "management": "external", "model": "fake-gemma", "timeoutSeconds": 10},
                    "asr": {"baseURL": base, "management": "external", "model": "fake-asr", "timeoutSeconds": 10},
                }}), encoding="utf-8")
                previous = os.environ.get("SYNLOVE_SERVICE_CONFIG")
                os.environ["SYNLOVE_SERVICE_CONFIG"] = str(registry)
                try:
                    from pipeline.videoPipeline import videoPipeline
                    config = json.loads((ROOT / "videoDigest/config/videoDigest.example.json").read_text(encoding="utf-8-sig"))
                    config.update({"frames": 2, "frameWidth": 160, "cellWidth": 160})
                    report, summary = videoPipeline(config, root).run(str(video), frames=2)
                    text = Path(report).read_text(encoding="utf-8")
                    assert Path(report).is_file() and summary.strip()
                    assert any(path.endswith("/transcriptions") for path in calls), calls
                    assert sum(path.endswith("/chat/completions") for path in calls) >= 2, calls
                    assert len(list(root.rglob("frame-*.jpg"))) == 2
                    assert list(root.rglob("sheet-*.jpg"))
                    assert "这是隔离测试短视频。" in text
                    print(json.dumps({"passed": True, "frames": 2, "report": True,
                                      "asrHttp": True, "visionAndSummaryHttp": True,
                                      "realModelCalls": False}, ensure_ascii=False))
                finally:
                    if previous is None:
                        os.environ.pop("SYNLOVE_SERVICE_CONFIG", None)
                    else:
                        os.environ["SYNLOVE_SERVICE_CONFIG"] = previous
        finally:
            server.shutdown()
            server.server_close()
            thread.join(5)


if __name__ == "__main__":
    VideoPipelineSmoke().run_smoke()
