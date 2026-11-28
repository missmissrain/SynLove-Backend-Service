"""
合成模型：用真实尺寸的 MLP 块堆出「一个和 Gemma E4B 同量级」的权重池，供调度实验使用。

负责的功能：
- `synthBlock`：两层 Linear（hidden→intermediate→hidden），参数量按目标显存预算推算；
- `synthStack`：N 个 block 串联，支持按块导出/搬运；
- 之所以不用真模型：GGUF 格式没法直接喂 PyTorch，而调度实验关心的是「每块多大、
  算一次多久、搬一次多久」，用同尺寸同精度的 Linear 块就能复现这些常数，且省掉下载。

注册参数（构造）：
    n_blocks      块数，默认 24
    hidden        隐藏维，默认 3072（Gemma E4B 量级）
    intermediate  中间维，默认 8192
    dtype         权重精度，默认 float16（和真实部署一致，2 字节/参数）
"""

import torch
import torch.nn as nn


class synthBlock(nn.Module):
    """一个假的 Transformer 层：只有两个 Linear，参数量与真实层的 FFN 相当。"""

    def __init__(self, hidden, intermediate, dtype=torch.float16):
        """建两个 Linear 权重。参数 hidden/intermediate：维度；dtype：精度。"""
        super().__init__()
        self.up = nn.Linear(hidden, intermediate, bias=False, dtype=dtype)
        self.down = nn.Linear(intermediate, hidden, bias=False, dtype=dtype)

    def forward(self, x):
        """一层前向。参数 x：输入激活 [batch, hidden]。"""
        return self.down(torch.nn.functional.gelu(self.up(x)))


class synthStack(nn.Module):
    """把若干 synthBlock 串起来，提供按块访问。"""

    def __init__(self, n_blocks=24, hidden=3072, intermediate=8192, dtype=torch.float16):
        """按参数建栈。参数含义见模块头部注释。"""
        super().__init__()
        self.blocks = nn.ModuleList(
            [synthBlock(hidden, intermediate, dtype) for _ in range(n_blocks)]
        )
        self.hidden = hidden
        self.bytes_per_block = 2 * hidden * intermediate * torch.finfo(dtype).bits // 8

    def n_bytes(self):
        """整个栈的权重字节数。"""
        return self.bytes_per_block * len(self.blocks)

    def forward(self, x, block_indices=None):
        """顺序跑指定块。参数 x：输入激活；block_indices：要跑的块下标（默认全部）。"""
        for index in (block_indices if block_indices is not None else range(len(self.blocks))):
            x = self.blocks[index](x)
        return x
