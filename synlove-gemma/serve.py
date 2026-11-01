"""启动本地 Gemma 4 E4B（llama.cpp llama-server），带显存闸门与就绪探测。

职责：
- 找 llama-server（显式参数 > 环境变量 > PATH）；
- 启动前检查空闲显存，不够就等（默认要 5200MB），避免和别的 GPU 任务互相拖死；
- 以 OpenAI 兼容接口方式跑在 127.0.0.1:<port>/v1，带视觉投影器（--mmproj）；
- 等到端口可连才返回，日志单独落文件。

注册参数（命令行，默认值见 DEFAULT_* 常量）：
    --bin            llama-server 可执行文件（默认按 PATH 找，或环境变量 LLAMA_SERVER_BIN）
    --model          GGUF 权重路径
    --mmproj         视觉投影器路径（给了就开多模态）
    --port           监听端口，默认 8080
    --ctx            上下文长度，默认 32768
    --ngl            卸载到 GPU 的层数，默认 99（全量）
    --alias          模型名（客户端调用时用），默认 gemma-4-E4B-it-Q4_K_M
    --min-free-vram  启动所需空闲显存（MB），默认 5200
    --wait-seconds   等显存的最长时间，默认 300
    --parallel       并发槽位，默认 1
    --dry-run        只打印命令，不启动
"""

import argparse
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent
DEFAULT_PORT = 8080
DEFAULT_CTX = 32768
DEFAULT_NGL = 99
DEFAULT_ALIAS = "gemma-4-E4B-it-Q4_K_M"
DEFAULT_MIN_VRAM = 5200
DEFAULT_WAIT = 300
LOG = ROOT / "runtime" / "gemma-server.log"


def log(message):
    """带时间戳打印（启动方通常会重定向到日志）。"""
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)


def find_binary(explicit):
    """按 参数 → 环境变量 → PATH 的顺序找 llama-server。"""
    for candidate in (explicit, os.environ.get("LLAMA_SERVER_BIN")):
        if candidate and Path(candidate).is_file():
            return str(candidate)
    found = shutil.which("llama-server")
    if found:
        return found
    raise SystemExit("没找到 llama-server：用 --bin 指定，或把它加入 PATH（见 README 的编译步骤）")


def free_vram_mb():
    """读一次空闲显存（MB）；没有 nvidia-smi 时返回 None。"""
    try:
        output = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15)
        used, total = [int(item.strip()) for item in output.stdout.strip().splitlines()[0].split(",")]
        return total - used
    except Exception:  # noqa: BLE001 —— 探测失败不该阻止启动
        return None


def wait_for_vram(min_free_mb, timeout_s, poll_s=20):
    """等到显存空出来；超时返回 False（探测不到显存则直接放行）。"""
    deadline = time.time() + timeout_s
    while True:
        free = free_vram_mb()
        if free is None:
            return True
        if free >= min_free_mb:
            return True
        if time.time() >= deadline:
            log(f"显存等待超时：free={free}MB < need={min_free_mb}MB")
            return False
        log(f"显存不足（free={free}MB < need={min_free_mb}MB），继续等…")
        time.sleep(min(poll_s, max(1, int(deadline - time.time()))))


def port_open(port):
    """端口是否可连。"""
    with socket.socket() as probe:
        probe.settimeout(1.5)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def build_args(binary, args):
    """拼 llama-server 命令行。"""
    cmd = [
        binary,
        "-m", str(args.model),
        "--host", "127.0.0.1",
        "--port", str(args.port),
        "-c", str(args.ctx),
        "-ngl", str(args.ngl),
        "--jinja",
        "--reasoning", "off",              # 本地模型带隐藏推理会把 max_tokens 吃光
        "--alias", args.alias,
        "--parallel", str(args.parallel),
        "--sleep-idle-seconds", str(args.sleep_idle),
    ]
    if args.mmproj and Path(args.mmproj).is_file():
        cmd += ["--mmproj", str(args.mmproj)]
    return cmd


def main(argv=None):
    """入口：等显存 → 起 llama-server → 等端口就绪。"""
    parser = argparse.ArgumentParser(description="启动本地 Gemma 4 E4B（llama-server）")
    parser.add_argument("--bin", default=None, help="llama-server 路径")
    parser.add_argument("--model", required=True, help="GGUF 权重路径")
    parser.add_argument("--mmproj", default=None, help="视觉投影器路径")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--ctx", type=int, default=DEFAULT_CTX)
    parser.add_argument("--ngl", type=int, default=DEFAULT_NGL)
    parser.add_argument("--alias", default=DEFAULT_ALIAS)
    parser.add_argument("--parallel", type=int, default=1)
    parser.add_argument("--sleep-idle", type=int, default=120, help="空闲多少秒后卸显存（0=不卸）")
    parser.add_argument("--min-free-vram", type=int, default=DEFAULT_MIN_VRAM)
    parser.add_argument("--wait-seconds", type=int, default=DEFAULT_WAIT)
    parser.add_argument("--dry-run", action="store_true", help="只打印命令")
    args = parser.parse_args(argv)

    if not Path(args.model).is_file():
        raise SystemExit(f"权重不存在：{args.model}（先跑 downloadGemmaModel.py）")
    if port_open(args.port):
        log(f"端口 {args.port} 已在监听，直接复用")
        return 0

    binary = find_binary(args.bin)
    cmd = build_args(binary, args)
    if args.dry_run:
        print(" ".join(cmd))
        return 0

    if not wait_for_vram(args.min_free_vram, args.wait_seconds):
        log("显存一直不够，放弃启动（稍后重试即可）")
        return 2

    LOG.parent.mkdir(parents=True, exist_ok=True)
    flags = 0x08000000 | 0x00000008  # CREATE_NO_WINDOW | DETACHED_PROCESS
    log("启动：" + " ".join(cmd[:8]) + " …")
    with LOG.open("a", encoding="utf-8") as handle:
        process = subprocess.Popen(cmd, cwd=str(ROOT), stdout=handle, stderr=handle,
                                   creationflags=flags)
    deadline = time.time() + 180
    while time.time() < deadline:
        if port_open(args.port):
            log(f"就绪：http://127.0.0.1:{args.port}/v1（pid {process.pid}，日志 {LOG}）")
            return 0
        if process.poll() is not None:
            log(f"进程退出（code {process.returncode}），看日志 {LOG}")
            return 1
        time.sleep(1)
    log(f"180 秒内没就绪，看日志 {LOG}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
