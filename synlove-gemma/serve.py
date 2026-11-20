"""启动本地 Gemma 4 E4B（llama.cpp llama-server），带显存闸门与就绪探测。

职责：
- 找 llama-server（显式参数 > 环境变量 > PATH）；
- API先常驻，请求到来时检查显存并按需加载；显存不足则请求空闲TTS卸载；
- 以 OpenAI 兼容接口方式跑在 127.0.0.1:<port>/v1，带视觉投影器（--mmproj）；
- 推理期间持有共享GPU锁，结束后保温600秒，到期只停止模型子进程。

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
    """拼 llama-server 命令行。

    安全模型：llama-server 只绑定 127.0.0.1:<internal-port>，对外端口由
    llmProxy.py 修正代理承担（重排多模态内容 + 转发）。
    --api-key-env：远程部署时读取指定环境变量作为 llama-server Bearer 密钥；
    未配置或对外仅回环时不带 --api-key（保持旧行为）。
    """
    cmd = [
        binary,
        "-m", str(args.model),
        "--host", "127.0.0.1",
        "--port", str(args.internal_port),
        "-c", str(args.ctx),
        "-ngl", str(args.ngl),
        "--jinja",
        "--reasoning", "off",              # 本地模型带隐藏推理会把 max_tokens 吃光
        "--alias", args.alias,
        "--parallel", str(args.parallel),
        "--sleep-idle-seconds", "-1",  # 驻留统一管理，当前llama以-1禁用内部计时。
    ]
    api_key = os.environ.get(args.api_key_env, "") if args.api_key_env else ""
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        if not api_key:
            raise SystemExit(f"非回环监听必须通过 --api-key-env 指定的环境变量提供密钥（{args.api_key_env}）")
        cmd += ["--api-key", api_key]
    if args.mmproj and Path(args.mmproj).is_file():
        cmd += ["--mmproj", str(args.mmproj)]
    return cmd


def resolve_weight_path(raw: str) -> str:
    """把权重路径解析为绝对路径。

    服务可能以仓库根或 gemma/ 为工作目录启动：绝对路径原样返回；
    相对路径先按仓库根解析（存在即用），再按 gemma/ 解析（兼容
    serviceInstall.json 的 gemma/model/... 写法与下载脚本的 model/... 写法）。
    """
    path = Path(raw)
    if path.is_absolute():
        return str(path)
    if path.is_file():
        return str(path.resolve())
    repo_root = Path(__file__).resolve().parent.parent
    for base in (repo_root, repo_root / "gemma"):
        candidate = base / raw
        if candidate.is_file():
            return str(candidate)
    return str(path)  # 保持原样，让下游报权重不存在的明确错误


def main(argv=None):
    """入口：等显存 → 起 llama-server → 等端口就绪。"""
    parser = argparse.ArgumentParser(description="启动本地 Gemma 4 E4B（llama-server）")
    parser.add_argument("--bin", default=None, help="llama-server 路径")
    parser.add_argument("--model", required=True, help="GGUF 权重路径")
    parser.add_argument("--mmproj", default=None, help="视觉投影器路径")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help="对外监听端口（由 llmProxy 承担）")
    parser.add_argument("--internal-port", type=int, default=None,
                        help="llama-server 回环端口；默认 = 对外端口 + 100")
    parser.add_argument("--host", default="127.0.0.1", help="对外监听地址；远程部署时用 0.0.0.0 并配 --api-key-env")
    parser.add_argument("--api-key-env", default="SYNLOVE_LLM_API_KEY",
                        help="llama-server Bearer 密钥的环境变量名（非回环监听时必填）")
    parser.add_argument("--ctx", type=int, default=DEFAULT_CTX)
    parser.add_argument("--ngl", type=int, default=DEFAULT_NGL)
    parser.add_argument("--alias", default=DEFAULT_ALIAS)
    parser.add_argument("--parallel", type=int, default=1)
    parser.add_argument("--sleep-idle", type=int, default=600, help="兼容旧启动参数；驻留策略统一由config/gpuResidency.json控制，默认600秒")
    parser.add_argument("--min-free-vram", type=int, default=DEFAULT_MIN_VRAM)
    parser.add_argument("--wait-seconds", type=int, default=DEFAULT_WAIT)
    parser.add_argument("--dry-run", action="store_true", help="只打印命令")
    args = parser.parse_args(argv)
    args.model = resolve_weight_path(args.model)
    if args.mmproj:
        args.mmproj = resolve_weight_path(args.mmproj)
    if args.internal_port is None:
        args.internal_port = args.port + 100

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

    # API先常驻；模型只在生成请求中加载，避免守护把正常卸载误判为服务故障。
    sys.path.insert(0, str(ROOT.parent))
    from gpuRuntime.GpuCoordinator import GpuCoordinator
    from gpuRuntime.LlmRuntime import LlmRuntime
    from llmProxy import ProxyHandler
    from gpuRuntime.CloudFallback import CloudFallback
    from http.server import ThreadingHTTPServer

    coordinator = GpuCoordinator(ROOT.parent, "llm")
    args.sleep_idle = -1  # 当前llama用-1禁用内部休眠；0是无效值。
    cmd = build_args(binary, args)
    runtime = LlmRuntime(cmd, ROOT, LOG, args.internal_port, coordinator, args.min_free_vram)
    ProxyHandler.runtime = runtime
    ProxyHandler.fallback = CloudFallback(ROOT.parent)
    ProxyHandler.token_env = args.api_key_env
    ProxyHandler.model_alias = args.alias
    ProxyHandler.upstream = f"http://127.0.0.1:{args.internal_port}"
    server = ThreadingHTTPServer((args.host, args.port), ProxyHandler)
    server.daemon_threads = True
    log(f"LLM API就绪，模型按需加载，空闲{coordinator.idle_seconds}秒软卸载")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        runtime.close_runtime()
    return 0


if __name__ == "__main__":
    sys.exit(main())
