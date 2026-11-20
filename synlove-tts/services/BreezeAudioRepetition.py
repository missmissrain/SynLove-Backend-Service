# -*- coding: utf-8 -*-
# 功能：仅对已生成音频的第一码本施加重复惩罚，避免文本token被误作音频词表索引导致CUDA越界。
# 注册参数：penalty为重复惩罚系数，默认1.1，与既有快速推理配置一致。
class BreezeAudioRepetition:
    """负责原生生成路径的音频专用重复惩罚，不处理初始文本token。"""

    def __init__(self, penalty=1.1):
        """penalty为大于零的重复惩罚系数；保留既有快速运行时的系数。"""
        self.penalty = float(penalty)
        if self.penalty <= 0:
            raise ValueError("重复惩罚系数必须大于零")

    def __call__(self, input_ids, scores):
        """input_ids初始为二维文本、生成后为三维音频；只惩罚三维音频历史的第一码本。"""
        import torch
        if self.penalty == 1.0 or input_ids.ndim != 3:
            return scores
        for index in range(input_ids.shape[0]):
            tokens = input_ids[index, :, 0].unique()
            tokens = tokens[(tokens >= 0) & (tokens < scores.shape[-1])]
            values = scores[index, tokens]
            scores[index, tokens] = torch.where(
                values > 0, values / self.penalty, values * self.penalty)
        return scores
