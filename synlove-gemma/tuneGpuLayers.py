"""在目标机器上找 `-ngl` 的最佳值：把层数从低到高扫一遍，记生成速度与显存峰值。

职责：
- 用 llama-bench 逐档跑（-p 0 -n N，只测逐 token 生成，正是聊天时的真实场景）；
- 每档同时后台轮询 nvidia-smi，记录显存峰值（llama-bench 自己不给这个数）；
- 按「显存还剩多少」给出推荐值：留够 headroom 的最大层数；
- 打印一张表，并把推荐命令直接打出来，粘到 serve.py 就能用。

为什么不用 llama-server 逐档试：每次都要重新加载模型（几十秒），扫 6 档就是好几分钟。
llama-bench 一个进程内就把 pp/tg 测完，快一个数量级。

注意（很重要）：
- llama-bench 不支持 --mmproj。如果线上要开视觉，显存峰值还要加上投影器那 0.9GB，
  本工具会按 `--mmproj-mb` 把这个预留量算进推荐里。
- 1060 是 Pascal，只能用 CUDA 12 的构建；用 CUDA 13 编译的二进制在这张卡上跑不起来。

注册参数（命令行）：
    --bench          llama-bench 路径（默认找 PATH，或环境变量 LLAMA_BENCH_BIN）
    --model          GGUF 权重路径
    --ladder         要扫的层数，逗号分隔，默认 0,8,16,24,28,32,99
    --gen            每档生成多少 token，默认 64
    --repeats        每档重复次数，默认 3
    --headroom-mb    推荐时要求保留的空闲显存（MB），默认 400
    --mmproj-mb      给视觉投影器预留的显存（MB），默认 950；不开视觉就设 0
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DEFAULT_LADDER = "0,8,16,24,28,32,99"


def log(message):
    """带时间戳打印。"""
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def find_binary(explicit):
    """按 参数 → 环境变量 → PATH 的顺序找 llama-bench。"""
    for candidate in (explicit, os.environ.get("LLAMA_BENCH_BIN")):
        if candidate and Path(candidate).is_file():
            return candidate
    found = shutil.which("llama-bench")
    if not found:
        raise SystemExit("没找到 llama-bench：用 --bench 指定，或加入 PATH")
    return found


def read_vram():
    """读一次 (已用, 总量) MB；读不到返回 (None, None)。"""
    try:
        output = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10)
        used, total = [int(item.strip()) for item in output.stdout.strip().splitlines()[0].split(",")]
        return used, total
    except Exception:  # noqa: BLE001 —— 探测失败不该中断实验
        return None, None


class vramWatcher:
    """后台轮询显存，记录峰值。用于包裹一次 llama-bench 运行。"""

    def __init__(self, interval=0.2):
        """参数 interval：采样间隔秒数。"""
        self.interval = interval
        self.peak = 0
        self.total = None
        self._stop = threading.Event()
        self._thread = None

    def __enter__(self):
        """开始采样。"""
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_exc):
        """停止采样。"""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def _loop(self):
        """采样循环。"""
        while not self._stop.is_set():
            used, total = read_vram()
            if used is not None:
                self.peak = max(self.peak, used)
                self.total = total
            time.sleep(self.interval)


def run_bench(binary, model, ngl, gen, repeats):
    """跑一档 llama-bench，返回 (tg token/s, 错误说明或 None)。

    两个坑：
    1. llama-bench 没有 `-c` 参数（上下文由 -p/-n 隐含），传了会报 invalid parameter；
    2. 权重路径里带中文时不能直接传绝对路径 —— Windows 下 CRT 会按当前代码页把
       argv 转成 ANSI，中文路径直接变成乱码，llama-bench 报 "failed to load model"。
       所以这里切到权重所在目录，只传文件名。
    """
    model_path = Path(model)
    cmd = [binary, "-m", model_path.name, "-ngl", str(ngl), "-p", "0", "-n", str(gen),
           "-r", str(repeats), "--output", "json"]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                            errors="replace", cwd=str(model_path.parent))
    if result.returncode != 0:
        tail = (result.stderr or result.stdout or "").strip().splitlines()
        return None, tail[-1][:160] if tail else f"退出码 {result.returncode}"
    try:
        rows = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None, "输出不是 JSON（llama-bench 版本太老？）"
    for row in rows:
        if row.get("n_gen", 0) and row.get("n_prompt", 0) == 0:
            return float(row.get("avg_ts", 0.0)), None
    for row in rows:
        speed = float(row.get("avg_ts", 0.0))
        if speed:
            return speed, None
    return None, "没解析到速度"


def main(argv=None):
    """主流程：逐档扫 → 打表 → 给推荐。"""
    parser = argparse.ArgumentParser(description="扫 -ngl 找显存与速度的平衡点")
    parser.add_argument("--bench", default=None, help="llama-bench 路径")
    parser.add_argument("--model", required=True, help="GGUF 权重路径")
    parser.add_argument("--ladder", default=DEFAULT_LADDER, help="要扫的层数")
    parser.add_argument("--gen", type=int, default=64, help="每档生成 token 数")
    parser.add_argument("--repeats", type=int, default=3, help="每档重复次数")
    parser.add_argument("--headroom-mb", type=int, default=400, help="要求的空闲显存")
    parser.add_argument("--mmproj-mb", type=int, default=950, help="给视觉投影器预留（0=不开视觉）")
    args = parser.parse_args(argv)

    if not Path(args.model).is_file():
        raise SystemExit(f"权重不存在：{args.model}")
    bench = find_binary(args.bench)
    ladder = [int(item.strip()) for item in args.ladder.split(",") if item.strip()]

    used, total = read_vram()
    if total is None:
        raise SystemExit("读不到显存（nvidia-smi 不可用）")
    log(f"显卡显存 {total} MB，当前已用 {used} MB（空闲 {total - used} MB）")
    log(f"扫描层数：{ladder}，每档生成 {args.gen} token ×{args.repeats}")
    log(f"推荐标准：显存峰值 + 视觉预留 {args.mmproj_mb}MB 之后，仍留 {args.headroom_mb}MB 空闲")
    print("")

    rows = []
    for ngl in ladder:
        log(f"跑 -ngl {ngl} …")
        with vramWatcher() as watcher:
            speed, error = run_bench(bench, args.model, ngl, args.gen, args.repeats)
        if error:
            rows.append({"ngl": ngl, "speed": None, "peak": watcher.peak, "note": error})
            log(f"  -ngl {ngl} 失败：{error}")
            continue
        rows.append({"ngl": ngl, "speed": speed, "peak": watcher.peak, "note": ""})
        log(f"  -ngl {ngl}：{speed:.2f} token/s，显存峰值 {watcher.peak} MB")

    print("")
    print("=== 结果 ===")
    print(f'{"-ngl":>5s} {"token/s":>9s} {"显存峰值":>10s} {"剩余":>8s}  说明')
    best = None
    for row in rows:
        if row["speed"] is None:
            print(f'{row["ngl"]:5d} {"—":>9s} {row["peak"]:8d}MB {"—":>8s}  {row["note"]}')
            continue
        left = total - row["peak"] - args.mmproj_mb
        print(f'{row["ngl"]:5d} {row["speed"]:9.2f} {row["peak"]:8d}MB {left:6d}MB  ')
        if left >= args.headroom_mb:
            if best is None or row["speed"] > best["speed"] * 1.02:
                best = row

    print("")
    if best is None:
        print("没有一档满足显存要求：把 --mmproj-mb 调小、或降低上下文、或不用视觉")
        return 1
    print(f'推荐：-ngl {best["ngl"]}（{best["speed"]:.2f} token/s，'
          f'扣掉视觉预留后还剩 {total - best["peak"] - args.mmproj_mb}MB）')
    print("")
    print("用这条启动（按需改端口/上下文）：")
    print(f'  python serve.py --model "{args.model}" --ngl {best["ngl"]}')
    print("")
    print("提示：本工具只测逐 token 生成（-p 0），真实跑的上下文越大，KV cache 占用越多。")
    print("      llama.cpp 新版本还支持 --fit-target <MiB>，可以按显存余量自动决定层数，")
    print("      可以用它交叉验证这里的推荐值。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
