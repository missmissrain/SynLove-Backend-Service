"""把下载/转写/画面描述整理成一份 Markdown 报表（同时输出 JSON 便于程序消费）。

职责：
- 用本机 Gemma 生成 200 字以内的一句话摘要（失败则用转写首句兜底）；
- 输出时间线表（时间 / 画面 / 该时段台词）；
- 附完整转写与元信息；
- 落盘 `runtime/reports/<slug>-<时间>.md` 与同名 .json。

注册参数（config）：
    gemma.endpoint / gemma.model（复用 VLM 同一服务）
    paths.reports（输出目录）
"""

import io
import json
import re
import sys
import time
import urllib.request
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from services.ServiceRegistry import ServiceRegistry
from services.ServiceClient import ServiceClient

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


class reportBuilder:
    """生成 Markdown 报表。"""

    def __init__(self, config, out_dir):
        """config：顶层配置；out_dir：报表目录。"""
        self.client = ServiceClient(ServiceRegistry().get_service("llm"))
        self.model = self.client.config["model"]
        self.out_dir = Path(out_dir)

    def _ask(self, prompt, max_tokens=260):
        """问一下本地模型，失败返回空串。"""
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
            "max_tokens": max_tokens,
        }
        try:
            data = self.client.chat(payload)
            return str(data["choices"][0]["message"]["content"]).strip()
        except Exception as error:  # noqa: BLE001
            print(f"[报表] 摘要生成失败：{type(error).__name__}: {error}", flush=True)
            return ""

    def _summary(self, transcript, frames):
        """生成摘要；没有转写时退回画面描述拼接。"""
        visual = "；".join(item["desc"] for item in frames if item["desc"])[:800]
        if transcript.strip() == "" and visual == "":
            return "（没有可用的转写或画面信息）"
        prompt = (
            "下面是一段视频的语音转写和画面描述。请用一段中文总结它在讲什么、关键信息是什么，"
            "不超过 200 字，不要分点。\n\n"
            f"【转写】\n{transcript[:3000]}\n\n【画面】\n{visual}\n"
        )
        summary = self._ask(prompt)
        if summary:
            return summary
        return (transcript.strip().split("。")[0][:120] + "。") if transcript.strip() else visual[:150]

    def _timeline_rows(self, frames, segments):
        """把帧的时间与转写段落对齐，生成表格行。"""
        rows = []
        for item in frames:
            moment = float(item["time"])
            nearby = " ".join(
                seg.get("text", "") for seg in segments
                if abs(float(seg.get("start", 0)) - moment) <= 2.5
            ).strip()
            rows.append((moment, item.get("desc", "") or "（无描述）", nearby or "—"))
        return rows

    def build(self, meta, asr, frames):
        """生成报表文件，返回 (md 路径, 摘要)。"""
        transcript = str(asr.get("text", "")).strip()
        segments = asr.get("segments", []) or []
        summary = self._summary(transcript, frames)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        slug = re.sub(r"[^0-9A-Za-z\u4e00-\u9fa5]+", "-", Path(meta.get("source", "video")).stem)[:40] or "video"
        self.out_dir.mkdir(parents=True, exist_ok=True)
        md_path = self.out_dir / f"{slug}-{stamp}.md"
        json_path = self.out_dir / f"{slug}-{stamp}.json"

        lines = [
            f"# 视频分析报告：{meta.get('title', slug)}",
            "",
            "| 项 | 值 |",
            "|---|---|",
            f"| 来源 | {meta.get('source', '')} |",
            f"| 时长 | {meta.get('duration', 0):.1f}s |",
            f"| 生成时间 | {time.strftime('%Y-%m-%d %H:%M:%S')} |",
            f"| 转写 | {len(transcript)} 字 / {len(segments)} 段 |",
            f"| 抽帧 | {len(frames)} 帧 |",
            "",
            "## 一句话摘要",
            "",
            summary,
            "",
            "## 时间线",
            "",
            "| 时间 | 画面 | 台词 |",
            "|---|---|---|",
        ]
        for moment, desc, line in self._timeline_rows(frames, segments):
            lines.append(f"| {moment:.1f}s | {desc} | {line} |")
        lines.extend(["", "## 完整转写", "", transcript or "（无语音内容）", ""])
        sheets = meta.get("sheets") or []
        if sheets:
            lines.extend(["## 关键帧拼图", ""])
            for sheet in sheets:
                lines.append(f"### {sheet.get('range', '')}")
                lines.append("")
                lines.append(f"![{sheet.get('range', '')}]({Path(sheet['path']).as_posix()})")
                lines.append("")
                lines.append(sheet.get("desc", "") or "（无描述）")
                lines.append("")

        with md_path.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write("\n".join(lines))
        with json_path.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps({
                "meta": meta, "summary": summary, "asr": asr, "frames": frames,
            }, ensure_ascii=False, indent=2))
        print(f"[报表] 已生成 {md_path.name}", flush=True)
        return str(md_path), summary
