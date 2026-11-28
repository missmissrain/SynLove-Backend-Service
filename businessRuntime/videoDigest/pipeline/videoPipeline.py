"""视频理解流水线：下载 → 抽音频 → ASR → 抽帧 → VLM → 报表。

设计取舍：
- 每一步都可单独失败（ASR 空、VLM 失败），只要视频在就能出报表；
- ASR 与 VLM 经统一配置调用；共享模型的空闲释放由服务负责，不由视频任务强制卸载；
- 全程前台打印步骤与耗时，便于 cmd 里看进度。
"""

import io
import os
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

sys.path.insert(0, str(ROOT))

from steps.videoDownloader import videoDownloader  # noqa: E402
from steps.audioSteps import audioExtractor, speechRecognizer  # noqa: E402
from steps.visionSteps import frameSampler, contactSheetBuilder, visionDescriber  # noqa: E402
from report.reportBuilder import reportBuilder  # noqa: E402


class videoPipeline:
    """把一条视频从 URL 变成一份报表。"""

    def __init__(self, config, root=None):
        """config：顶层配置；root：项目根目录（默认由文件位置推导）。"""
        self.config = config
        self.root = Path(root) if root is not None else ROOT
        paths = config.get("paths", {})
        self.media_dir = self.root / str(paths.get("media", "runtime/videoDigest"))
        self.report_dir = self.root / str(paths.get("reports", "runtime/reports"))
        if os.environ.get("SYNLOVE_VIDEO_WORK_DIR"):
            work = Path(os.environ["SYNLOVE_VIDEO_WORK_DIR"]).resolve()
            self.media_dir = work / "media"
            self.report_dir = work / "reports"

    def run(self, target, frames=None, language=None):
        """执行整条链路，返回报表路径与摘要。"""
        started = time.time()
        frames = int(frames or self.config.get("frames", 20))
        if not 1 <= frames <= 120:
            raise ValueError("抽帧数必须为1至120")
        whisper_cfg = dict(self.config.get("whisper", {}))
        if language:
            whisper_cfg["language"] = language

        print(f"[1/5] 下载：{target}", flush=True)
        login_cfg = dict(self.config.get("login", {}))
        if self.config.get("loginTimeoutSeconds"):
            login_cfg["timeoutSeconds"] = self.config["loginTimeoutSeconds"]
        downloader = videoDownloader({**self.config.get("ytdlp", {}),
                                      "maxSeconds": self.config.get("maxSeconds", 600)}, self.media_dir, login_cfg)
        video_path = downloader.fetch(target)

        print("[2/5] 抽音频", flush=True)
        extractor = audioExtractor(self.media_dir)
        wav_path = extractor.extract(video_path)

        print("[3/5] 语音转写", flush=True)
        recognizer = speechRecognizer(whisper_cfg)
        asr = {"text": "", "segments": [], "language": language or whisper_cfg.get("language", "")}
        try:
            asr = recognizer.transcribe(wav_path)
        except Exception as error:  # noqa: BLE001
            print(f"[ASR] 失败（继续出报表）：{type(error).__name__}: {error}", flush=True)

        print("[4/5] 抽帧 + 画面描述", flush=True)
        sampler = frameSampler(self.media_dir / "frames", self.config.get("frameWidth", 640))
        frames_list = []
        try:
            frames_list = sampler.sample(video_path, frames)
        except Exception as error:  # noqa: BLE001
            print(f"[抽帧] 失败：{type(error).__name__}: {error}", flush=True)
        describer = visionDescriber(self.config.get("gemma", {}))
        sheet_builder = contactSheetBuilder(
            self.media_dir / "sheets",
            int(self.config.get("perSheet", 4)),
            int(self.config.get("sheetColumns", 2)),
            int(self.config.get("cellWidth", 480)),
        )
        sheets = sheet_builder.build(frames_list)
        described, sheet_notes = describer.describe_sheets(sheets, asr.get("text", ""))

        print("[5/5] 生成报表", flush=True)
        duration = 0.0
        try:
            import subprocess

            probe = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=nw=1:nk=1", video_path],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
            )
            duration = float(probe.stdout.strip() or 0)
        except Exception:  # noqa: BLE001
            duration = 0.0
        meta = {
            "source": str(target),
            "title": Path(video_path).stem,
            "duration": duration,
            "video": video_path,
            "sheets": sheet_notes,
        }
        builder = reportBuilder(self.config, self.report_dir)
        md_path, summary = builder.build(meta, asr, described)
        print(f"[完成] 总用时 {time.time() - started:.1f}s → {md_path}", flush=True)
        return md_path, summary
