"""INT8 ConvRot 运行时（移植自 ComfyUI-Breeze-TTS-2 节点包 int8.py，Apache-2.0）。

量化 checkpoint 里每个被量化的 Linear 存储：
  weight       : torch.int8，离线分组旋转（W_rot = W @ H^T）
  weight_scale : float32 [out_features, 1] 每输出行 scale
  comfy_quant  : uint8 JSON {"format": "int8_tensorwise", "convrot": true, "convrot_groupsize": G}

本机适配（GTX 1060 / Pascal sm_61）：
- comfy-kitchen 的 CUDA kernel 只编译到 sm_70+，在 Pascal 上会报
  "no kernel image is available"，因此 import 时强制 eager 后端
  （纯 PyTorch：激活逐行 int8 量化 + torch._int_mm，权重全程 int8 不反量化）。
- 实测（1060, 2048x2048, M=1）：~0.68ms/层，数值与 bf16 参考一致。
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass

import torch
from torch import nn

logger = logging.getLogger("BreezeTTS2-int8")

# Pascal 无 sm_61 int8 CUDA kernel，强制纯 PyTorch eager 路径
import comfy_kitchen  # noqa: E402

comfy_kitchen.set_backend_priority(["eager"])

QUANT_META_SUFFIX = "comfy_quant"
SUPPORTED_FORMAT = "int8_tensorwise"


def validate_group_size(group_size: int, in_features: int) -> None:
    if group_size < 4 or group_size & (group_size - 1) != 0 or math.log(group_size, 4) % 1 != 0:
        raise ValueError(f"ConvRot group size must be a power of four (4/16/64/256/...), got {group_size}")
    if in_features % group_size != 0:
        raise ValueError(f"in_features={in_features} is not divisible by group size {group_size}")


@dataclass
class QuantLayerInfo:
    prefix: str
    group_size: int
    in_features: int = 0
    out_features: int = 0
    has_bias: bool = False


def _shard_files(checkpoint) -> list:
    from pathlib import Path

    path = Path(checkpoint)
    if path.is_dir():
        index_path = path / "model.safetensors.index.json"
        if index_path.is_file():
            weight_map = json.loads(index_path.read_text(encoding="utf-8"))["weight_map"]
            return [path / shard for shard in sorted(set(weight_map.values()))]
        return [path / "model.safetensors"]
    return [path]


def scan_checkpoint_quantization(checkpoint) -> dict:
    """读取 checkpoint 内每个 *.comfy_quant 键，返回 {层名: QuantLayerInfo}。

    浮点 checkpoint 返回 {}。遇到不支持的量化格式直接抛错，
    避免 INT8 权重被当浮点静默误读。
    """
    from safetensors import safe_open

    quant_map: dict = {}
    for shard in _shard_files(checkpoint):
        if not shard.is_file():
            continue
        with safe_open(str(shard), framework="pt", device="cpu") as f:
            key_names = list(f.keys())
            for key in [k for k in key_names if k.endswith(f".{QUANT_META_SUFFIX}")]:
                meta = json.loads(f.get_tensor(key).numpy().tobytes())
                if meta.get("format") != SUPPORTED_FORMAT or meta.get("convrot") is not True:
                    raise RuntimeError(
                        f"{shard.name}:{key} uses quant format {meta!r}; only "
                        f"'{SUPPORTED_FORMAT}' with convrot=true is supported."
                    )
                prefix = key[: -len(f".{QUANT_META_SUFFIX}")]
                quant_map[prefix] = QuantLayerInfo(
                    prefix=prefix,
                    group_size=int(meta["convrot_groupsize"]),
                    in_features=int(meta.get("in_features", 0)),
                    out_features=int(meta.get("out_features", 0)),
                    has_bias=bool(meta.get("has_bias", f"{prefix}.bias" in key_names)),
                )
    return quant_map


class ConvRotInt8Linear(nn.Module):
    """nn.Linear 的 drop-in 替换，执行 INT8 ConvRot 前向。"""

    def __init__(self, in_features: int, out_features: int, bias: bool, group_size: int):
        super().__init__()
        self.in_features = in_features
        self.out_features = out_features
        self.convrot_groupsize = group_size
        self.weight = nn.Parameter(torch.empty(out_features, in_features, dtype=torch.int8), requires_grad=False)
        self.weight_scale = nn.Parameter(torch.empty(out_features, 1, dtype=torch.float32), requires_grad=False)
        self.bias = nn.Parameter(torch.empty(out_features), requires_grad=False) if bias else None
        self.quant_format = SUPPORTED_FORMAT

    def _load_from_state_dict(self, state_dict, prefix, local_metadata, strict, missing_keys, unexpected_keys, error_msgs):
        state_dict.pop(f"{prefix}{QUANT_META_SUFFIX}", None)
        super()._load_from_state_dict(
            state_dict, prefix, local_metadata, strict, missing_keys, unexpected_keys, error_msgs
        )

    def forward(self, x):
        return comfy_kitchen.int8_linear(
            x.contiguous(),
            self.weight,
            self.weight_scale,
            self.bias,
            out_dtype=x.dtype,
            convrot=True,
            convrot_groupsize=self.convrot_groupsize,
        )


def replace_quantized_linears(model: nn.Module, quant_map: dict) -> list:
    """把 quant_map 里命中的每个 nn.Linear 换成 ConvRotInt8Linear。

    必须在权重载入前调用。目标层缺失/不是 nn.Linear/形状不符都会抛错。
    """
    modules = dict(model.named_modules())
    replaced = []
    for prefix, info in quant_map.items():
        target = modules.get(prefix)
        if target is None:
            raise RuntimeError(
                f"Quantized layer '{prefix}' not found in the model. The checkpoint "
                "does not match this model build."
            )
        if isinstance(target, ConvRotInt8Linear):
            replaced.append(prefix)
            continue
        if not isinstance(target, nn.Linear):
            raise RuntimeError(f"Quantized layer '{prefix}' is {type(target).__name__}, expected nn.Linear.")
        if info.in_features and target.in_features != info.in_features:
            raise RuntimeError(
                f"Quantized layer '{prefix}' in_features mismatch: checkpoint {info.in_features}, model {target.in_features}."
            )
        if info.out_features and target.out_features != info.out_features:
            raise RuntimeError(
                f"Quantized layer '{prefix}' out_features mismatch: checkpoint {info.out_features}, model {target.out_features}."
            )
        validate_group_size(info.group_size, target.in_features)
        parent_name, _, child_name = prefix.rpartition(".")
        setattr(modules[parent_name] if parent_name else model, child_name,
                ConvRotInt8Linear(target.in_features, target.out_features, target.bias is not None, info.group_size))
        replaced.append(prefix)
    return replaced


def quantized_parameter_count(model: nn.Module):
    params = sum(m.weight.numel() for m in model.modules() if isinstance(m, ConvRotInt8Linear))
    modules = sum(1 for m in model.modules() if isinstance(m, ConvRotInt8Linear))
    return params, modules
