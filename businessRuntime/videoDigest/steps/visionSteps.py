"""抽帧 + 拼图 + 画面描述（VLM）。

为什么拼图：一次 VLM 调用看 4~5 帧，比逐帧调用省 4~5 倍请求与图片 token；
20 帧 → 5 张拼图 → 5 次调用。

注册参数：
    frames / frameWidth / perSheet / sheetColumns / gemma.*
"""

import base64
import io
import json
import math
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from services.ServiceRegistry import ServiceRegistry
from services.ServiceClient import ServiceClient

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

FFMPEG = os.environ.get("FFMPEG") or "ffmpeg"


class frameSampler:
    """按时间均匀抽帧（默认 20 帧）。"""

    def __init__(self, out_dir, frame_width=640):
        """out_dir：帧目录；frame_width：缩放宽度。"""
        self.out_dir = Path(out_dir)
        self.width = int(frame_width)

    def _duration(self, video_path):
        """ffprobe 取时长（秒）。"""
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(video_path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        try:
            return float(result.stdout.strip())
        except ValueError:
            return 0.0

    def sample(self, video_path, count=20):
        """返回 [(时间秒, 图片路径)]。"""
        self.out_dir.mkdir(parents=True, exist_ok=True)
        duration = self._duration(video_path)
        if duration <= 0:
            raise RuntimeError("取不到视频时长（ffprobe 失败）")
        count = max(1, int(count))
        step = duration / (count + 1)
        frames = []
        for index in range(count):
            moment = round(step * (index + 1), 2)
            target = self.out_dir / f"frame-{index + 1:03d}.jpg"
            result = subprocess.run(
                [FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-ss", str(moment),
                 "-i", str(video_path), "-frames:v", "1", "-vf", f"scale={self.width}:-2", str(target)],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
            )
            if result.returncode == 0 and target.is_file():
                frames.append((moment, str(target)))
        print(f"[抽帧] 时长 {duration:.1f}s，取到 {len(frames)} 帧", flush=True)
        return frames


class contactSheetBuilder:
    """把连续几帧拼成一张大图，并在每格左上角标注时间。"""

    def __init__(self, out_dir, per_sheet=4, columns=2, cell_width=480):
        """out_dir：拼图目录；per_sheet：每张拼几张；columns：每行几格。"""
        self.out_dir = Path(out_dir)
        self.per_sheet = max(1, int(per_sheet))
        self.columns = max(1, int(columns))
        self.cell_width = int(cell_width)

    def build(self, frames):
        """返回 [{"path","frames":[(t,path)...],"range":"t1–t2"}]。"""
        from PIL import Image, ImageDraw

        self.out_dir.mkdir(parents=True, exist_ok=True)
        sheets = []
        for index in range(0, len(frames), self.per_sheet):
            group = frames[index:index + self.per_sheet]
            cells = []
            for moment, path in group:
                image = Image.open(path).convert("RGB")
                ratio = self.cell_width / image.width
                image = image.resize((self.cell_width, max(1, int(image.height * ratio))), Image.LANCZOS)
                draw = ImageDraw.Draw(image)
                label = f"t={moment:.1f}s"
                draw.rectangle([(0, 0), (len(label) * 7 + 8, 18)], fill=(0, 0, 0))
                draw.text((4, 3), label, fill=(255, 255, 255))
                cells.append(image)
            cell_h = max(item.height for item in cells)
            rows = math.ceil(len(cells) / self.columns)
            canvas = Image.new("RGB", (self.cell_width * self.columns, cell_h * rows), (20, 20, 20))
            for position, image in enumerate(cells):
                row, column = divmod(position, self.columns)
                canvas.paste(image, (column * self.cell_width, row * cell_h))
            path = self.out_dir / f"sheet-{index // self.per_sheet + 1:02d}.jpg"
            canvas.save(path, "JPEG", quality=85)
            times = [moment for moment, _ in group]
            sheets.append({"path": str(path), "frames": group,
                           "range": f"{min(times):.1f}s–{max(times):.1f}s"})
        print(f"[拼图] {len(frames)} 帧 → {len(sheets)} 张拼图（每张 {self.per_sheet} 帧）", flush=True)
        return sheets


class visionDescriber:
    """对拼图逐张调用本机 Gemma，解析出每格的画面描述。"""

    def __init__(self, config):
        """config：gemma 段。"""
        self.client = ServiceClient(ServiceRegistry().get_service("llm"))
        self.model = self.client.config["model"]

    def _ask(self, prompt, image_path):
        """单张拼图问一次模型，返回文本。"""
        image = base64.b64encode(Path(image_path).read_bytes()).decode("ascii")
        payload = {
            "model": self.model,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image}"}},
                ],
            }],
            "temperature": 0.2,
            "max_tokens": 400,
        }
        try:
            data = self.client.chat(payload)
            return str(data["choices"][0]["message"]["content"]).strip()
        except Exception as error:  # noqa: BLE001
            print(f"[VLM] 拼图失败：{type(error).__name__}: {error}", flush=True)
            return ""

    def describe_sheets(self, sheets, transcript=""):
        """逐张拼图描述，返回 (逐帧描述列表, 拼图描述列表)。"""
        described = []
        sheet_notes = []
        started = time.time()
        for index, sheet in enumerate(sheets):
            times = [f"{moment:.1f}s" for moment, _ in sheet["frames"]]
            prompt = (
                f"这是一张视频截图拼图（{len(times)} 格，从左到右、从上到下，左上角标注了时间）。"
                f"各格时间依次为：{'、'.join(times)}。\n"
                "请逐格输出画面描述，每格一行，格式严格为 `t=时间: 描述`，描述不超过 35 字，不要总结、不要编号。"
            )
            text = self._ask(prompt, sheet["path"])
            parsed = []
            for line in text.splitlines():
                match = re.match(r"\s*t?[=＝]?\s*([\d.]+)\s*s?\s*[:：]\s*(.+)", line)
                if match:
                    parsed.append((float(match.group(1)), match.group(2).strip()))
            if not parsed:
                parsed = [(moment, "") for moment, _ in sheet["frames"]]
            by_time = {round(moment, 1): desc for moment, desc in parsed}
            for moment, frame_path in sheet["frames"]:
                described.append({
                    "time": moment,
                    "frame": frame_path,
                    "sheet": sheet["path"],
                    "desc": by_time.get(round(moment, 1), ""),
                })
            sheet_notes.append({"range": sheet["range"], "path": sheet["path"], "desc": text})
            print(f"[VLM] 拼图 {index + 1}/{len(sheets)}（{sheet['range']}）完成", flush=True)
        print(f"[VLM] 全部完成，用时 {time.time() - started:.1f}s", flush=True)
        return described, sheet_notes
