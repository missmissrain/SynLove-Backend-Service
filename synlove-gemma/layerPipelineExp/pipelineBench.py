"""
层流水线实验：在有限的显存预算下，比较四种「模型放不下时怎么办」的策略。

负责的功能：
- 量本机 PCIe 带宽与「单块搬运 vs 单块计算」的常数；
- 用合成模型（和 Gemma E4B 同尺寸的 MLP 块）跑四种模式：
    resident  全部常驻 GPU（预算够时的上界）
    partial   超出的块常驻 CPU，每 token 不搬权重（llama.cpp -ngl 的做法）
    serial    朴素逐层载入（每块搬进 GPU 算完丢）
    pipeline  交替放置 + 双缓冲预取（CPU 算的时候 GPU 在搬）
- 先做数值校验（pipeline 与 resident 输出必须一致），再计时；
- 打印 token/s、每 token 搬运字节数、有效带宽，并给出结论。

为什么统一用 fp32 权重：PyTorch 的 CPU fp16 内核是未优化路径（实测 49ms/块，
比 fp32 的 9.5ms 慢 5 倍），用它当 CPU 基线会严重失真；真实部署里 llama.cpp 用的是
int4/int8 量化内核，CPU 侧是带宽受限的。fp32 在 CPU 上恰好也是带宽受限（200MB/9.5ms
≈ 21GB/s，与 DDR 带宽吻合），比 fp16 更接近真实情况。

注册参数（命令行）：
    --blocks     块数，默认 12
    --budget-gb  显存预算，默认 1.2
    --tokens     每种模式计时多少个 token，默认 10
    --group      一组几块（交替粒度），默认 1
"""

import argparse
import os
import sys
import time

import torch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from layerPipeline import layerPipeline, measure_pcie_bandwidth
from syntheticStack import synthStack


def measure_mode(pipe, mode, tokens, hidden):
    """跑 tokens 次前向并计时，返回 (token/s, 每次毫秒)。参数见函数名。"""
    x = torch.randn(1, hidden, dtype=torch.float32)
    with torch.no_grad():
        for _ in range(2):
            pipe.forward_token(x, mode)
        torch.cuda.synchronize()
        started = time.perf_counter()
        for _ in range(tokens):
            pipe.forward_token(x, mode)
        torch.cuda.synchronize()
        elapsed = time.perf_counter() - started
    return tokens / elapsed, elapsed / tokens * 1000


def measure_cpu_only(blocks, tokens, hidden):
    """全 CPU 计时（不建流水线，避免有块已经躺在显存里）。参数 blocks：层块列表。"""
    x = torch.randn(1, hidden, dtype=torch.float32)
    with torch.no_grad():
        for _ in range(2):
            hidden_out = x
            for block in blocks:
                hidden_out = block(hidden_out)
        started = time.perf_counter()
        for _ in range(tokens):
            hidden_out = x
            for block in blocks:
                hidden_out = block(hidden_out)
        elapsed = time.perf_counter() - started
    return tokens / elapsed, elapsed / tokens * 1000


def main():
    """主流程：量常数 → 建栈 → 校验 → 四种模式计时 → 打印结论。"""
    parser = argparse.ArgumentParser()
    parser.add_argument('--blocks', type=int, default=12)
    parser.add_argument('--budget-gb', type=float, default=1.2)
    parser.add_argument('--tokens', type=int, default=10)
    parser.add_argument('--group', type=int, default=1)
    args = parser.parse_args()

    device = torch.cuda.get_device_name(0)
    total_vram = torch.cuda.get_device_properties(0).total_memory / 1024 ** 3
    print(f'显卡：{device}（{total_vram:.1f} GiB）')
    print(f'CPU 线程：{torch.get_num_threads()}')

    bandwidth = measure_pcie_bandwidth(256)
    print(f'PCIe H2D 带宽：pageable {bandwidth["pageable_gbs"]:.1f} GB/s，'
          f'pinned {bandwidth["pinned_gbs"]:.1f} GB/s')

    # 用 fp32 建栈：CPU 侧是带宽受限，贴近真实
    stack = synthStack(n_blocks=args.blocks, hidden=3072, intermediate=8192, dtype=torch.float32)
    block_bytes = stack.bytes_per_block
    total_bytes = stack.n_bytes()
    print(f'模型：{args.blocks} 块 × {block_bytes / 1024 ** 2:.0f} MB = {total_bytes / 1024 ** 3:.2f} GiB')
    print(f'显存预算：{args.budget_gb:.1f} GiB')

    # 单块常数：CPU 计算 vs H2D 搬运
    probe = stack.blocks[0]
    x = torch.randn(1, 3072, dtype=torch.float32)
    with torch.no_grad():
        started = time.perf_counter()
        for _ in range(20):
            probe(x)
        cpu_ms = (time.perf_counter() - started) / 20 * 1000
    copy_ms = block_bytes / 1024 ** 3 / bandwidth['pinned_gbs'] * 1000
    print(f'单块：CPU 算 {cpu_ms:.2f} ms；H2D 搬 {copy_ms:.2f} ms（搬运/计算 = {copy_ms / cpu_ms:.2f}×）')
    print('')

    budget = args.budget_gb * 1024 ** 3
    results = []

    # 1) 全部常驻（仅当预算够）
    if total_bytes <= budget:
        pipe_full = layerPipeline(stack.blocks, block_bytes, total_bytes * 2, group_size=1)
        rate, ms = measure_mode(pipe_full, 'resident', args.tokens, 3072)
        results.append(('resident（全在显存）', rate, ms, 0, '上界'))
        for block in stack.blocks:
            block.to('cpu')
        torch.cuda.empty_cache()
    else:
        results.append(('resident（全在显存）', None, None, 0, '预算不够，跳过'))

    # 2~4) 预算受限的几种策略
    for mode, label in (('cpu', 'cpu（全 CPU，-ngl 0）'),
                        ('partial', 'partial（超出部分留 CPU）'),
                        ('serial', 'serial（朴素逐层载入）'),
                        ('pipeline', 'pipeline（交替 + 双缓冲）')):
        if mode == 'cpu':
            rate, ms = measure_cpu_only(stack.blocks, args.tokens, 3072)
            results.append((label, rate, ms, 0, '基准'))
            continue
        pipe = layerPipeline(stack.blocks, block_bytes, budget, group_size=args.group, ring=2)
        if mode == 'pipeline':
            error = pipe.verify()
            print(f'数值校验：pipeline 与 resident 最大误差 {error:.2e}')
        streamed = sum(len(c) for use_gpu, c in pipe.groups if use_gpu) * block_bytes
        if mode == 'partial':
            streamed = 0
        rate, ms = measure_mode(pipe, mode, args.tokens, 3072)
        results.append((label, rate, ms, streamed, pipe.plan_text()))
        for block in stack.blocks:
            block.to('cpu')
        torch.cuda.empty_cache()

    print('')
    print('=== 结果 ===')
    print(f'{"模式":32s} {"token/s":>9s} {"每 token 耗时":>14s} {"每 token 搬运":>14s}')
    for label, rate, ms, streamed, note in results:
        if rate is None:
            print(f'{label:32s} {"—":>9s} {"—":>14s} {"—":>14s}  {note}')
        else:
            print(f'{label:32s} {rate:9.2f} {ms:11.1f} ms {streamed / 1024 ** 2:11.0f} MB  {note}')

    by_label = {label: rate for label, rate, _, _, _ in results}
    cpu = by_label.get('cpu（全 CPU，-ngl 0）')
    partial = by_label.get('partial（超出部分留 CPU）')
    serial = by_label.get('serial（朴素逐层载入）')
    pipeline = by_label.get('pipeline（交替 + 双缓冲）')
    print('')
    print('=== 对比 ===')
    if cpu and partial:
        print(f'  部分卸载 vs 全 CPU：{partial / cpu:.2f}×（把能塞的都塞进显存，剩下的留 CPU）')
    if serial and pipeline:
        print(f'  流水线 vs 朴素逐层载入：{pipeline / serial:.2f}×（重叠掉的就是 CPU 计算那部分）')
    if partial and pipeline:
        extra = pipeline / partial
        print(f'  流水线 vs 部分卸载：{extra:.2f}×'
              f'{"（更快，值得）" if extra > 1.05 else "（不划算：搬上去的时间比在 CPU 算还长）"}')
    if cpu and pipeline:
        print(f'  流水线 vs 全 CPU：{pipeline / cpu:.2f}×')


if __name__ == '__main__':
    main()
