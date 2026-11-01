"""
层流水线调度器：把一部分层常驻显存、其余层在 GPU 与 CPU 之间交替放置，
并在 CPU 计算时让 GPU 异步预取下一组权重（双缓冲），把搬运和计算叠起来。

负责的功能：
- `_build_plan`：按显存预算决定「多少块常驻 GPU」以及「剩余块交替分到 GPU 槽位还是 CPU」；
- `forward_token`：一次完整前向（一个 token），支持四种执行模式：
    resident  全部常驻 GPU（预算够才允许）
    partial   常驻块在 GPU、其余常驻 CPU，层顺序不变，只在一次边界上换设备
              （这才是 llama.cpp `-ngl` 的真实行为，每 token 不搬权重）
    serial    朴素逐层载入：每块临时搬进 GPU，算完就丢（不做预取）
    pipeline  交替放置 + 双缓冲预取（本实验的核心方案）
- `verify`：用同一份输入比对 pipeline 与 resident 的输出，确保调度没算错（顺序不能乱）。

关键实现细节：
- 只有「要上 GPU」的块才 `pin_memory()`，CPU 组的块保持普通内存 ——
  钉住的内存给 CPU 计算会更慢，不能拿它当 CPU 基线；
- 复制走独立的 `torch.cuda.Stream`，计算流用 event 等它，才能真的重叠；
- 槽位是固定的 GPU 张量，用 `copy_` 覆写，避免每 token 重新分配显存。

注册参数（构造）：
    blocks           nn.ModuleList，按顺序的层块（都在 CPU 上）
    bytes_per_block  单块权重字节数
    vram_budget      允许使用的显存字节数（不含激活与框架开销）
    group_size       一组几块，默认 1；2 就对应「头两层、后两层」的交替方式
    ring             双缓冲槽位数，默认 2
"""

import time

import torch


class layerPipeline:
    """交替放置 + 双缓冲预取的层流水线。"""

    def __init__(self, blocks, bytes_per_block, vram_budget, group_size=1, ring=2):
        """建槽位、算调度计划、把要常驻的直接搬到 GPU。参数含义见模块头部注释。"""
        self.blocks = blocks
        self.n_blocks = len(blocks)
        self.bytes_per_block = bytes_per_block
        self.vram_budget = vram_budget
        self.group_size = max(1, group_size)
        self.ring = max(2, ring)

        self.copy_stream = torch.cuda.Stream()
        self.slots = []          # 每个槽位是一组模块（长度 = group_size）
        self.slot_events = []
        self.resident_count = 0
        self.groups = []         # [(是否走 GPU, [块下标...])]

        self._build_plan()

    # ---------------------------------------------------------------- 计划

    def _build_plan(self):
        """算「多少块常驻」与「剩余组交替分给 GPU 还是 CPU」，并把常驻块搬上 GPU。"""
        fit = int(self.vram_budget // self.bytes_per_block)
        self.resident_count = max(0, min(self.n_blocks, fit - self.ring * self.group_size))

        for index in range(self.resident_count):
            self.blocks[index].to('cuda')

        rest = list(range(self.resident_count, self.n_blocks))
        if rest:
            template = self.blocks[rest[0]]
            hidden = template.up.out_features
            intermediate = template.up.in_features
            dtype = template.up.weight.dtype
            for _ in range(self.ring):
                slot = [type(template)(intermediate, hidden, dtype).to('cuda')
                        for _ in range(self.group_size)]
                self.slots.append(slot)
                self.slot_events.append(torch.cuda.Event())

        for start in range(0, len(rest), self.group_size):
            chunk = rest[start:start + self.group_size]
            use_gpu = (start // self.group_size) % 2 == 0
            self.groups.append((use_gpu, chunk))

        # 只有走 GPU 的块需要钉住主机内存，否则 non_blocking 拷贝实际是同步的
        for use_gpu, chunk in self.groups:
            if not use_gpu:
                continue
            for index in chunk:
                for param in self.blocks[index].parameters():
                    param.data = param.data.pin_memory()

    def plan_text(self):
        """给人看的调度计划摘要。"""
        gpu_groups = [chunk for use_gpu, chunk in self.groups if use_gpu]
        cpu_groups = [chunk for use_gpu, chunk in self.groups if not use_gpu]
        return (f'常驻 GPU {self.resident_count} 块；交替上 GPU {sum(len(c) for c in gpu_groups)} 块'
                f'（{len(gpu_groups)} 组）；留 CPU {sum(len(c) for c in cpu_groups)} 块')

    # ---------------------------------------------------------------- 执行

    def forward_token(self, x, mode='pipeline'):
        """跑一次完整前向（一个 token）。参数 x：输入激活（CPU）；mode：见模块头部。"""
        if mode == 'resident':
            return self._run_resident(x)
        if mode == 'cpu':
            return self._run_cpu(x)
        if mode == 'partial':
            return self._run_partial(x)
        if mode == 'serial':
            return self._run_serial(x)
        return self._run_pipeline(x)

    def _run_cpu(self, x):
        """全 CPU：所有块都在内存里算（llama.cpp `-ngl 0` 的做法）。"""
        hidden = x
        for index in range(self.n_blocks):
            hidden = self.blocks[index](hidden)
        return hidden

    def _run_resident(self, x):
        """全部层都在 GPU。"""
        hidden = x.to('cuda')
        for index in range(self.n_blocks):
            hidden = self.blocks[index](hidden)
        return hidden.to('cpu')

    def _run_partial(self, x):
        """常驻块在 GPU、其余块在 CPU；只在中间换一次设备，每 token 不搬权重。"""
        hidden = x.to('cuda')
        for index in range(self.resident_count):
            hidden = self.blocks[index](hidden)
        if self.resident_count < self.n_blocks:
            hidden = hidden.to('cpu')
            for index in range(self.resident_count, self.n_blocks):
                hidden = self.blocks[index](hidden)
        return hidden

    def _run_serial(self, x):
        """朴素逐层载入：常驻块先算，剩下每块「搬进 GPU → 算 → 搬走」。"""
        hidden = x.to('cuda')
        for index in range(self.resident_count):
            hidden = self.blocks[index](hidden)
        for index in range(self.resident_count, self.n_blocks):
            block = self.blocks[index]
            block.to('cuda')
            hidden = block(hidden)
            block.to('cpu')
        return hidden.to('cpu')

    def _run_pipeline(self, x):
        """交替放置 + 双缓冲：CPU 组计算时，GPU 流正在搬下一组权重。"""
        hidden = x.to('cuda')
        for index in range(self.resident_count):
            hidden = self.blocks[index](hidden)

        pending = None      # (槽位下标, event)
        rounds = 0
        # 预取第一组 GPU 权重
        first = self._first_gpu_group()
        if first is not None:
            pending = self._prefetch(first, rounds)
            rounds += 1

        for group_index, (use_gpu, chunk) in enumerate(self.groups):
            if use_gpu:
                slot_index, event = pending
                torch.cuda.current_stream().wait_event(event)
                hidden = self._slot_forward(self.slots[slot_index], chunk, hidden)
            else:
                next_group = self._next_gpu_group(group_index)
                if next_group is not None:
                    pending = self._prefetch(next_group, rounds)
                    rounds += 1
                hidden_cpu = hidden.to('cpu')
                for index in chunk:
                    hidden_cpu = self.blocks[index](hidden_cpu)
                hidden = hidden_cpu.to('cuda')
        return hidden.to('cpu')

    def _first_gpu_group(self):
        """第一个走 GPU 的组。"""
        for use_gpu, chunk in self.groups:
            if use_gpu:
                return chunk
        return None

    def _next_gpu_group(self, from_index):
        """找 from_index 之后第一个 GPU 组。参数 from_index：当前组下标。"""
        for group_index in range(from_index + 1, len(self.groups)):
            use_gpu, chunk = self.groups[group_index]
            if use_gpu:
                return chunk
        return None

    def _prefetch(self, chunk, round_index):
        """把一组权重异步拷进槽位。参数 chunk：块下标列表；round_index：第几轮（决定槽位）。"""
        slot_index = round_index % self.ring
        slot = self.slots[slot_index]
        event = self.slot_events[slot_index]
        with torch.cuda.stream(self.copy_stream):
            for offset, index in enumerate(chunk):
                source = self.blocks[index]
                for src, dst in zip(source.parameters(), slot[offset].parameters()):
                    dst.data.copy_(src.data, non_blocking=True)
            event.record(self.copy_stream)
        return slot_index, event

    def _slot_forward(self, slot, chunk, hidden):
        """在槽位上顺序算这一组。参数 slot：槽位模块列表；chunk：对应的块下标；hidden：激活。"""
        for offset in range(len(chunk)):
            hidden = slot[offset](hidden)
        return hidden

    # ---------------------------------------------------------------- 校验

    def verify(self):
        """用固定输入比对 pipeline 与 partial 的输出，返回最大绝对误差。

        为什么和 partial 比而不是和 resident 比：预算受限时并不是所有块都在 GPU 上，
        resident 根本跑不起来；partial 在数学上完全等价（只是放在不同设备上算），
        拿它当基准才能验证「调度顺序有没有搞错」。
        """
        dtype = self.blocks[0].up.weight.dtype
        x = torch.randn(1, self.blocks[0].up.in_features, dtype=dtype)
        with torch.no_grad():
            reference = self._run_partial(x.clone())
            got = self._run_pipeline(x.clone())
        return float((reference.float() - got.float()).abs().max())


def measure_pcie_bandwidth(size_mb=256):
    """量一次 H2D 拷贝的实际带宽（pageable 与 pinned 各一次）。

    参数 size_mb：每次拷贝的张量大小（MB）。返回 {'pageable_gbs': x, 'pinned_gbs': y}。
    """
    elements = size_mb * 1024 * 1024 // 2      # fp16
    result = {}
    for name, pinned in (('pageable', False), ('pinned', True)):
        host = torch.empty(elements, dtype=torch.float16)
        if pinned:
            host = host.pin_memory()
        gpu = torch.empty_like(host, device='cuda')
        torch.cuda.synchronize()
        started = time.perf_counter()
        for _ in range(3):
            gpu.copy_(host, non_blocking=pinned)
        torch.cuda.synchronize()
        elapsed = time.perf_counter() - started
        result[f'{name}_gbs'] = 3 * host.numel() * 2 / elapsed / 1024 ** 3
        del host, gpu
        torch.cuda.empty_cache()
    return result
