# -*- coding: utf-8 -*-
# 功能：校验API独立引导参数，复用已加载模型执行原生三分支双引导，输出干声及实际模型入口证据。
# 注册参数：runtime为现有Breeze运行时；ref_scale/ins_scale分别为参考音频和文字引导强度，无默认启用值。
import json
import inspect
import math
import time
import traceback
from pathlib import Path
from BreezeAudioRepetition import BreezeAudioRepetition


class BreezeDualGuidance:
    """负责可选的原生双引导推理；未指定独立参数时保留原流式路径。"""

    def __init__(self, runtime, ref_scale, ins_scale):
        """runtime为已加载运行时；ref_scale/ins_scale为经validate_payload校验的非负有限数值。"""
        self.runtime = runtime
        self.ref_scale = ref_scale
        self.ins_scale = ins_scale

    @staticmethod
    def validate_payload(payload):
        """payload为HTTP请求；两个权重必须成对提供，并带参考音频、参考文本及情绪提示；零值有效。"""
        ref = payload.get("cfgScaleRef")
        ins = payload.get("cfgScaleIns")
        if ref is None and ins is None:
            return None, None
        if ref is None or ins is None:
            raise ValueError("cfgScaleRef与cfgScaleIns必须同时提供")
        values = []
        for key, value in (("cfgScaleRef", ref), ("cfgScaleIns", ins)):
            if isinstance(value, bool):
                raise ValueError(key + "必须为数值")
            try:
                value = float(value)
            except (TypeError, ValueError):
                raise ValueError(key + "必须为数值") from None
            if not math.isfinite(value) or value < 0:
                raise ValueError(key + "必须为非负有限数值")
            values.append(value)
        if not (payload.get("refAudioBase64") or payload.get("refAudio")):
            raise ValueError("双引导需要参考音频")
        if not str(payload.get("refText") or "").strip():
            raise ValueError("双引导需要refText")
        if not str(payload.get("instruction") or "").strip():
            raise ValueError("双引导需要instruction")
        return tuple(values)

    def generate_audio(self, inputs, request_id, seed, out_dir):
        """inputs为原生三分支模型输入，request_id/seed沿用请求，out_dir为审计父目录；返回单声道float32音频。"""
        import numpy as np
        import torch
        from breeze_infer.runtime import set_all_seeds

        runtime = self.runtime
        model = runtime.model
        path = Path(out_dir) / "guidanceAudit" / (request_id + ".json")
        path.parent.mkdir(parents=True, exist_ok=True)
        report = {"requestId": request_id, "mode": "nativeDualCfg",
                  "cfgScaleRef": self.ref_scale, "cfgScaleIns": self.ins_scale,
                  "seed": seed, "startedAt": time.time(), "complete": False,
                  "modelMerges": [], "branches": {}}
        # 与既有DepthDecoderGraph保持一致：小型码本输出头采用FP32，避免放大低精度logits误差。
        head = model.depth_decoder.codebooks_head
        head.weight.data = head.weight.data.float()
        report["depthHeadDtype"] = str(head.weight.dtype)

        def cast_head_input(module, args, kwargs):
            """module为码本输出头；仅将其隐藏态输入对齐FP32权重，缓存位置等参数原样保留。"""
            if args:
                args = (args[0].to(module.weight.dtype), *args[1:])
            elif "hidden_states" in kwargs:
                kwargs = dict(kwargs)
                kwargs["hidden_states"] = kwargs["hidden_states"].to(module.weight.dtype)
            return args, kwargs

        head_hook = head.register_forward_pre_hook(cast_head_input, with_kwargs=True)
        # 快速单CFG可能已将主输出头提升为FP32；原生路径的隐藏态仍可能为BF16。
        # 仅对齐本次原生调用的输入，不改主输出头权重或全局采样过程。
        report["backboneHeadDtype"] = str(model.lm_head.weight.dtype)
        lm_hook = model.lm_head.register_forward_pre_hook(cast_head_input, with_kwargs=True)
        for name in ("uncond", "ref", "ins"):
            key = "cfg_" + name
            report["branches"][name] = {
                "inputShape": list(inputs[key + "_prompt_ids"].shape),
                "textTokens": int(inputs[key + "_text_ids_mask"].sum().item())}
        original = model._merge_input_ids_with_input_values
        signature = inspect.signature(original)

        def observed_merge(*args, **kwargs):
            """args/kwargs为原模型嵌入入口参数；原样调用并记录实际输入与嵌入形状。"""
            result = original(*args, **kwargs)
            values = signature.bind(*args, **kwargs).arguments
            ids = values.get("input_ids")
            row = {"inputShape": list(ids.shape) if ids is not None else None,
                   "embeddingShape": list(result["inputs_embeds"].shape),
                   "hasReferenceValues": values.get("input_values") is not None}
            report["modelMerges"].append(row)
            return result

        # 此服务已有串行请求锁；只在本次原生生成范围内观测，finally恢复原方法。
        model._merge_input_ids_with_input_values = observed_merge
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
        try:
            set_all_seeds(seed)
            with torch.inference_mode():
                result = model.generate(
                    **inputs, output_audio=True, audio_tokenizer=runtime.audio_tokenizer,
                    max_new_tokens=runtime.config.max_new_tokens,
                    repetition_penalty=1.0,
                    logits_processor=[BreezeAudioRepetition(runtime.config.repetition_penalty)],
                    return_dict_in_generate=False)
            audio = result[0].detach().float().cpu().numpy().reshape(-1).astype(np.float32)
            if not np.isfinite(audio).all() or len(audio) < runtime.sample_rate:
                raise RuntimeError("双引导模型未生成有效音频")
            report.update(complete=True, audioSamples=len(audio),
                          seconds=len(audio) / runtime.sample_rate)
            return audio
        except Exception as exc:
            report["error"] = str(exc)
            report["traceback"] = traceback.format_exc()
            raise
        finally:
            head_hook.remove()
            lm_hook.remove()
            model._merge_input_ids_with_input_values = original
            report["endedAt"] = time.time()
            path.write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
