"""视频理解报表流水线入口。

用法：
    python videoDigest\\runVideoDigest.py --url "https://..." [--frames 6] [--language zh]
    python videoDigest\\runVideoDigest.py --file "D:\\path\\demo.mp4"
    python videoDigest\\runVideoDigest.py --url ... --send qq   # 报表摘要发到 QQ

输出：runtime/reports/<名字>-<时间>.md（含摘要/时间线/转写/关键帧）
"""

import argparse
import io
import json
import os
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))
if __name__ == "__main__" and os.environ.get("SYNLOVE_SERVICE_WORKER") != "1":
    from microServices.ServiceCli import ServiceCli
    sys.exit(ServiceCli("video", ROOT).run(sys.argv[1:]))

from pipeline.videoPipeline import videoPipeline  # noqa: E402
from steps.videoDownloader import loginRequired  # noqa: E402
from tools.qq_notify import send_qq_text_message  # noqa: E402,F401


def main(argv):
    """入口。"""
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--url", help="视频链接")
    group.add_argument("--file", help="本地视频文件")
    parser.add_argument("--frames", type=int, default=None, help="抽几帧（默认取配置）")
    parser.add_argument("--language", default=None, help="ASR 语言，如 zh / en")
    parser.add_argument("--send", choices=["qq"], default=None, help="把摘要发到 QQ")
    args = parser.parse_args(argv)

    config = json.loads((HERE / "config" / "videoDigest.json").read_text(encoding="utf-8-sig"))
    pipeline = videoPipeline(config, ROOT)
    target = args.url or args.file
    try:
        md_path, summary = pipeline.run(target, frames=args.frames, language=args.language)
    except loginRequired as error:
        print(f"需要登录：{error}", flush=True)
        print("→ 请在服务端配置该站点的登录凭据后重试。", flush=True)
        return 3
    except Exception as error:  # noqa: BLE001
        print(f"流水线失败：{type(error).__name__}: {error}", flush=True)
        return 1

    print("\n摘要：", summary)
    print("报表：", md_path)
    if args.send == "qq":
        try:
            result = send_qq_text_message(f"视频分析完成：{summary}\n\n报表：{md_path}")
            print("已发 QQ：", result.get("id", ""))
        except Exception as error:  # noqa: BLE001
            print(f"发 QQ 失败：{type(error).__name__}: {error}")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
