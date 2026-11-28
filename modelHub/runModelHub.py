"""ModelHub 启动入口。

职责：解析后台仓库、监听地址和模型登记表参数，启动唯一的 ModelHub 调度器。
注册参数：--root 默认当前仓库根目录；--host 默认 127.0.0.1；--port 默认 8798。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent / "personal-agent"))
try:
    from services.LogBus import bind as bind_log
except ImportError:  # 日志库缺失不阻止模型服务启动，但要在标准错误留痕
    bind_log = None
    print("LogBus 不可用：ModelHub 只写标准输出", file=sys.stderr)

try:
    from .ModelHubService import run_model_hub
except ImportError:
    from modelHub.ModelHubService import run_model_hub


def main() -> None:
    """无参数；解析命令行参数并启动 ModelHub。"""
    parser = argparse.ArgumentParser(description="SynLove ModelHub")
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[1]), help="后台仓库根目录")
    parser.add_argument("--host", default="127.0.0.1", help="管理接口监听地址")
    parser.add_argument("--port", type=int, default=8810, help="管理接口端口")
    parser.add_argument("--config", default="", help="模型登记文件路径")
    args = parser.parse_args()
    if bind_log is not None:
        bind_log("modelhub", ROOT)
    run_model_hub(args.root, args.host, args.port, args.config or None)


if __name__ == "__main__":
    main()
