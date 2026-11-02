"""音频抽取与语音转写步骤。

职责：
- audioExtractor：用 ffmpeg 把视频转成 16kHz 单声道 wav；
- speechRecognizer：通过统一注册表调用 ASR，上传字节；生命周期由服务管理，不按任务卸载共享模型。

注册参数（config 的 whisper 段）：
    port / model / language / idleSeconds
"""

import os
import subprocess
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
from services.ServiceRegistry import ServiceRegistry
from services.ServiceClient import ServiceClient, MAX_AUDIO
from services.LocalServiceLifecycle import LocalServiceLifecycle
FFMPEG = os.environ.get("FFMPEG") or "ffmpeg"


class audioExtractor:
    """从视频里抽一条适合 ASR 的 wav。"""

    def __init__(self, out_dir):
        """out_dir：中间文件目录。"""
        self.out_dir = Path(out_dir)

    def extract(self, video_path):
        """返回 16k 单声道 wav 的路径（失败抛异常）。"""
        self.out_dir.mkdir(parents=True, exist_ok=True)
        wav_path = self.out_dir / (Path(video_path).stem + ".asr.wav")
        started = time.time()
        result = subprocess.run(
            [FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-i", str(video_path),
             "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav_path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if result.returncode != 0 or not wav_path.is_file():
            raise RuntimeError(f"抽音频失败：{(result.stderr or '')[:200]}")
        print(f"[音频] 已抽取 {wav_path.name}（{time.time() - started:.1f}s）", flush=True)
        return str(wav_path)


class speechRecognizer:
    """调用 whisper 服务做转写。"""

    def __init__(self, config):
        """config：whisper 段。"""
        self.vad_filter = bool(config.get("vadFilter", True))
        self.service = ServiceRegistry().get_service("asr")
        self.client = ServiceClient(self.service)
        self.lifecycle = LocalServiceLifecycle("asr", self.service)
        self.language = config.get("language", "zh")
        self.idle = int(config.get("idleSeconds", 120))

    def _ensure(self):
        """仅托管模式按需启动服务；远程模式没有本机副作用。"""
        self.lifecycle.ensure_service(self.idle)

    def transcribe(self, wav_path):
        """返回 {"text": str, "segments": [...], "language": str}。"""
        self._ensure()
        started = time.time()
        if Path(wav_path).stat().st_size > MAX_AUDIO:
            return self._transcribe_chunks(wav_path)
        data = self.client.transcribe(wav_path, self.language, self.vad_filter)
        # 音乐/演唱会这类视频 VAD 会把歌声滤掉：没转出字就关掉 VAD 再试一次
        if not str(data.get("text", "")).strip() and self.vad_filter and self.service.get("dialect") == "whisper":
            print("[ASR] 首次为空，关闭 VAD 重试…", flush=True)
            data = self.client.transcribe(wav_path, self.language, False)
        print(f"[ASR] {len(data.get('text', ''))} 字，{len(data.get('segments', []))} 段，"
              f"用时 {time.time() - started:.1f}s", flush=True)
        return data

    def _transcribe_chunks(self, wav_path):
        """wav_path 为超限 PCM WAV；按字节上限分块上传并还原分段时间偏移。"""
        import tempfile
        import wave
        result = {"text": "", "segments": [], "language": self.language}
        with wave.open(str(wav_path), "rb") as source, tempfile.TemporaryDirectory() as directory:
            params = source.getparams()
            frame_size = params.nchannels * params.sampwidth
            frames = (MAX_AUDIO - 4096) // frame_size
            offset = 0
            while True:
                data = source.readframes(frames)
                if not data:
                    break
                path = Path(directory) / "chunk.wav"
                with wave.open(str(path), "wb") as target:
                    target.setparams(params)
                    target.writeframes(data)
                part = self.client.transcribe(path, self.language, self.vad_filter)
                result["text"] += part["text"]
                for segment in part.get("segments", []):
                    result["segments"].append({**segment, "start": segment["start"] + offset,
                                               "end": segment["end"] + offset})
                offset += len(data) / frame_size / params.framerate
                print(f"[ASR] 分块上传已完成 {offset:.1f}s", flush=True)
        return result
