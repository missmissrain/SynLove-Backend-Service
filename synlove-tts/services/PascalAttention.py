# 功能：为无原生BF16的旧显卡提供FP32注意力运算，保留INT8权重及GPU/CPU原布局。
# 注册参数：install的device为cuda或cpu，默认cuda；只替换当前进程的eager注意力函数。
class PascalAttention:
    """仅提升注意力中间运算精度，输出恢复输入类型，不改提示词或声音引导。"""

    device = "cuda"

    @classmethod
    def install(cls, device="cuda"):
        """device指定注意力计算设备；安装在模型创建前，原生权重文件保持不变。"""
        import models.breeze as breeze
        if device not in ("cuda", "cpu"):
            raise ValueError("attention device must be cuda or cpu")
        cls.device = device
        breeze.eager_attention_forward = cls.forward

    @classmethod
    def forward(cls, module, query, key, value, attention_mask,
                scaling, dropout=0.0, **kwargs):
        """输入为模型Q/K/V和遮罩；FP32完成分组扩展、矩阵乘法及softmax，再还原输出设备类型。"""
        import torch
        target = query.device if cls.device == "cuda" else torch.device("cpu")
        dtype, device = query.dtype, query.device
        q = query.to(device=target, dtype=torch.float32).contiguous()
        k = key.to(device=target, dtype=torch.float32).contiguous()
        v = value.to(device=target, dtype=torch.float32).contiguous()
        groups = module.num_key_value_groups
        if groups != 1:
            k = k.repeat_interleave(groups, dim=1)
            v = v.repeat_interleave(groups, dim=1)
        weights = torch.matmul(q, k.transpose(2, 3)) * scaling
        if attention_mask is not None:
            mask = attention_mask[:, :, :, : k.shape[-2]].to(
                device=target, dtype=torch.float32)
            weights = weights + mask
        weights = torch.softmax(weights, dim=-1)
        if dropout:
            weights = torch.nn.functional.dropout(
                weights, p=dropout, training=module.training)
        output = torch.matmul(weights, v).transpose(1, 2).contiguous()
        return output.to(device=device, dtype=dtype), None
