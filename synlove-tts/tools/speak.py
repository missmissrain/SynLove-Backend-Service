"""
Breeze-TTS-2 命令行：调用本地合成服务（不在就唤起），可把语音直接发到 QQ。

用法：
    python tools\\speak.py "你好，我是欣爱。" --instruction "温柔自信的年轻女性" --cfg-scale 4
    ... --max-chars 80            # 每段更短（更快、更稳）

行为：
- 服务没起来就后台拉起（breeze venv 的 python），首次请求加载权重（约 1 分钟）；
- 长文本自动按句切段合成再拼接，避免显存/质量一起崩；
- **每次合成完立即退出**，释放约 7GB 显存；下次调用再加载（首次约 30 秒）。
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
SERVICE = ROOT / "services" / "breezeTtsService.py"
LOG = ROOT / "runtime" / "breeze-tts-server.log"
PORT = int(os.environ.get("BREEZE_TTS_PORT") or "7862")
BASE = f"http://127.0.0.1:{PORT}"
BREEZE_REPO = Path(os.environ.get("BREEZE_REPO") or (ROOT / "breeze-tts"))
VENV_PYTHON = Path(os.environ.get("BREEZE_PYTHON") or (BREEZE_REPO / ".venv" / "Scripts" / "python.exe"))
CKPT = os.environ.get("BREEZE_CKPT") or str(ROOT / "breezeTts2")
FFMPEG = os.environ.get("FFMPEG") or "ffmpeg"

VOICES = {
    "tiantian": "十七八岁的甜妹，声音软糯清甜，语速稍快，尾音轻轻上扬，带一点撒娇感，普通话标准。",
    "tiantian2": "二十岁出头的少女，嗓音清亮甜美，说话轻快俏皮，尾音带笑，像在跟喜欢的人聊天。",
    "yuanqi": "元气满满的少女，声音明亮有活力，语速轻快，情绪饱满，吐字干净利落。",
    "wenrou": "温柔自信的年轻女性，声音清晰，语气亲切，表达轻快而富有感染力。",
    "zhixing": "二十五六岁的知性女性，声音沉稳温柔，语速中慢，吐字清晰，不疾不徐。",
}




def port_open(port):
    """端口是否可连。"""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1.0):
            return True
    except OSError:
        return False


def ensure_service(idle_seconds, max_chars):
    """服务没起来就后台拉起（用 breeze venv 的 python），并等端口。"""
    if port_open(PORT):
        return
    if not VENV_PYTHON.is_file():
        raise RuntimeError(f"找不到 breeze venv python：{VENV_PYTHON}")
    LOG.parent.mkdir(parents=True, exist_ok=True)
    flags = 0x08000000 | 0x00000008  # CREATE_NO_WINDOW | DETACHED_PROCESS
    with LOG.open("a", encoding="utf-8") as handle:
        subprocess.Popen(
            [str(VENV_PYTHON), str(SERVICE), "--port", str(PORT), "--ckpt", CKPT,
             "--idle-seconds", str(idle_seconds), "--out-dir", str(ROOT / "runtime" / "tts"),
             "--max-chars", str(max_chars), "--exit-after-request", "1"],
            cwd=str(BREEZE_REPO), stdout=handle, stderr=handle, creationflags=flags,
        )
    for _ in range(60):
        time.sleep(0.5)
        if port_open(PORT):
            return
    raise RuntimeError(f"Breeze-TTS 服务启动超时，见日志 {LOG}")


def wav_to_mp3(wav_path):
    """wav → mp3（QQ 语音条用 mp3 实测可正常显示）。"""
    mp3_path = str(Path(wav_path).with_suffix(".mp3"))
    result = subprocess.run(
        [FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-i", wav_path,
         "-codec:a", "libmp3lame", "-b:a", "64k", mp3_path],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if result.returncode != 0 or not Path(mp3_path).is_file():
        raise RuntimeError(f"ffmpeg 转 mp3 失败：{(result.stderr or '')[:200]}")
    return mp3_path


def main(argv):
    """入口。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("text")
    parser.add_argument("--voice", default="tiantian2", help="音色预设（默认 tiantian2）：tiantian/tiantian2/yuanqi/wenrou/zhixing")
    parser.add_argument("--instruction", default=None, help="自定义音色描述（优先于 --voice）")
    parser.add_argument("--ref-audio", default=None)
    parser.add_argument("--ref-text", default=None)
    parser.add_argument("--cfg-scale", type=float, default=4.0)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--max-chars", type=int, default=120, help="每段最大字数")
    parser.add_argument("--idle-seconds", type=int, default=300, help="兜底：万一没退出，闲置多久强制退")
    args = parser.parse_args(argv)

    instruction = args.instruction or VOICES.get(str(args.voice or "")) or VOICES["tiantian2"]
    ensure_service(args.idle_seconds, args.max_chars)
    payload = json.dumps({
        "text": args.text,
        "instruction": instruction,
        "refAudio": args.ref_audio,
        "refText": args.ref_text,
        "cfgScale": args.cfg_scale,
        "seed": args.seed,
        "maxChars": args.max_chars,
    }, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(f"{BASE}/speak", data=payload,
                                     headers={"Content-Type": "application/json"}, method="POST")
    started = time.time()
    try:
        with urllib.request.urlopen(request, timeout=3600) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", "replace")
        print(f"合成失败 HTTP {error.code}: {body[:600]}")
        return 1
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        print(f"合成失败：{error}（日志 {LOG}）")
        return 1

    print(f"用时 {time.time() - started:.1f}s  时长 {data.get('duration', 0):.1f}s  "
          f"分段 {data.get('chunks')}  采样率 {data.get('sampleRate')}")
    wav_path = data.get("path", "")
    print(wav_path)

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
