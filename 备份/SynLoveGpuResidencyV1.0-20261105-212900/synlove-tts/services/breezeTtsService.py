"""
Breeze-TTS-2 语音合成服务（本地 HTTP，懒加载 + 长文本分段 + 闲置释放显存）。

为什么这么设计：
- 权重约 7GB、eager 推理要 7.7GB 显存，**不能常驻**——首次请求才加载，闲置 N 秒进程退出；
- 6GB 显存机器可用 --device 3.5 混合加载：transformer 按预算部分驻 GPU，其余驻 CPU，
  加载和推理速度都明显好于纯 CPU；
- 文本一长（小说/长段）一次生成会爆显存、也会越说越飘——**按句切段**逐段合成再拼接；
- 加载前检查空闲显存；不够就等 Gemma 自动休眠（默认 120s 空闲）再加载，超时给明确提示。

接口：
    POST /v1/audio/speech   OpenAI 风格 JSON，返回 WAV 字节；支持 Bearer 鉴权
    GET  /health            是否已加载、设备、空闲显存
    POST /speak             {"text": "...", "instruction": "...", "cfgScale": 4, "maxChars": 120}
                            → {"ok":true, "path":"...wav", "duration":秒, "chunks":段数}
    POST /unload            立刻退出进程（释放显存）

启动参数：
    --port 7862             监听端口
    --ckpt <项目>/models/breezeTts2
    --idle-seconds 600      闲置多久退出（0=不退出）
    --out-dir runtime/tts
    --max-chars 120         每段最大字数
    --max-total-chars 3000  单次请求最大总字数（超出直接拒绝，防跑飞）
    --exit-after-request 0  默认共享服务，空闲退出；1 仅供旧单次调用
"""

import argparse
import json
import os
import re
import sys
import threading
import time
import traceback
import uuid
from http.server import ThreadingHTTPServer
if __package__:
    from .AudioHttp import AudioHttp
else:
    from AudioHttp import AudioHttp
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

REPO = os.environ.get("BREEZE_REPO", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "vendor", "breeze-tts"))
sys.path.insert(0, REPO)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAX_NEW_TOKENS = 1500
MAX_SEQ_LEN = 2048
REPETITION_PENALTY = 1.1
# 加载所需的显存（MB）。低于这个值先等 Gemma 休眠，再试。
NEED_VRAM_MB = int(os.environ.get("BREEZE_NEED_VRAM_MB") or "6500")
WAIT_VRAM_SECONDS = int(os.environ.get("BREEZE_WAIT_VRAM_SECONDS") or "180")

_lock = threading.Lock()
_state = {
    "runtime": None,
    "device": "",
    "device_arg": None,
    "sample_rate": 24000,
    "last_active": time.time(),
    "ckpt": "",
    "out_dir": os.path.join(ROOT, "runtime", "tts"),
    "max_chars": 120,
    "max_total_chars": 3000,
    "exit_after_request": False,
    "busy": False,
}


def log(message):
    """带时间戳打印（启动方重定向到日志）。"""
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)


def free_vram_mb():
    """返回当前空闲显存（MB）；没有 CUDA 时返回 -1。"""
    try:
        import torch  # noqa: PLC0415

        if not torch.cuda.is_available():
            return -1
        free, _total = torch.cuda.mem_get_info()
        return int(free / 1024 / 1024)
    except Exception:
        return -1


def wait_for_vram():
    """等 Gemma 自动休眠腾出显存；超时抛错并给出可操作提示。"""
    deadline = time.time() + WAIT_VRAM_SECONDS
    while True:
        free = free_vram_mb()
        if free < 0 or free >= NEED_VRAM_MB:
            return free
        if time.time() >= deadline:
            raise RuntimeError(
                f"空闲显存只有 {free}MB，加载实测约 6.5GB。"
                f"请先让 Gemma 空闲 2 分钟自动休眠（或停掉 Gemma）后重试。"
            )
        log(f"显存不足（{free}MB < {NEED_VRAM_MB}MB），等 Gemma 休眠…")
        time.sleep(15)


def _prefer_cuda_get_device(model):
    """替换 fast_streaming._get_device：混合放置时任取一个 GPU 参数作为运行时设备。

    FastBreezeStreamingRuntime 要求 device.type=="cuda"；accelerate 混合放置时
    第一个参数可能在 CPU，这里改为在全部参数中找 GPU 张量，纯 CPU 时回退原实现。
    """
    for param in model.parameters():
        if param.device.type == "cuda":
            return param.device
    return torch.device("cpu")


def load_engine_hybrid(ckpt, gpu_gb):
    """混合加载：按 gpu_gb 预算把部分权重驻留 GPU，其余驻 CPU（accelerate 钩子搬运）。

    适用于权重 7GB 但显存只有 6GB 的机器：transformer 主体按预算放 GPU，
    音频 tokenizer（约 0.65GB）也放 GPU；CPU 侧只承担放不下的层。
    返回 (tokenizer, model, audio_tokenizer)。
    """
    import torch
    from models.breeze import BreezeForConditionalGeneration  # noqa: PLC0415
    from qwen_tts import Qwen3TTSTokenizer  # noqa: PLC0415
    from transformers import AutoTokenizer  # noqa: PLC0415

    import models.fast_streaming as fast_streaming_module  # noqa: PLC0415
    original_get_device = fast_streaming_module._get_device
    fast_streaming_module._get_device = _prefer_cuda_get_device
    ckpt_path = Path(ckpt)
    try:
        log(f"混合加载：GPU 预算 {gpu_gb}GiB，其余驻 CPU…")
        tokenizer = AutoTokenizer.from_pretrained(ckpt_path, fix_mistral_regex=False)
        # 必须声明 CPU 预算，否则 accelerate 会把放不下的权重卸到磁盘（极慢）。
        max_memory = {0: f"{int(gpu_gb) if float(gpu_gb).is_integer() else gpu_gb}GiB", "cpu": "12GiB"}
        model = BreezeForConditionalGeneration.from_pretrained(
            ckpt_path,
            dtype=torch.bfloat16,
            attn_implementation="eager",
            device_map="auto",
            max_memory=max_memory,
        )
        model.eval()
        gpu_layers = sum(1 for param in model.parameters() if param.device.type == "cuda")
        cpu_gb = sum(param.numel() * param.element_size() for param in model.parameters()
                     if param.device.type == "cpu") / 1024 ** 3
        gpu_gb_actual = sum(param.numel() * param.element_size() for param in model.parameters()
                            if param.device.type == "cuda") / 1024 ** 3
        log(f"权重放置：GPU {gpu_gb_actual:.2f}GB / CPU {cpu_gb:.2f}GB（GPU 张量 {gpu_layers} 个）")
        audio_tokenizer = Qwen3TTSTokenizer.from_pretrained(
            str(ckpt_path / "audio_tokenizer"), device_map="cuda")
        return tokenizer, model, audio_tokenizer
    finally:
        fast_streaming_module._get_device = original_get_device


def load_engine_int8(bf16_ckpt, int8_ckpt, gpu_gb, depth_fp32=False):
    """INT8 ConvRot 量化加载（GTX 1060 6GB 专用快速路径）。

    权重来自社区量化版 Breeze-TTS-2-int8-hybrid（4.35GiB，backbone+text_encoder
    为 ConvRot int8，depth decoder 保持 bf16）。设备布局策略：
    - text_encoder / text_encoder_proj 驻 CPU：每段只跑一次 prefill，离载代价小；
    - 其余（backbone、depth decoder、lm_head、embeddings）全驻 GPU：逐帧热循环
      必须留在显存带宽内；
    - 音频 codec（0.65GB）驻 GPU：fast streaming 的 codec 解码要求与模型同设备。
    相比 bf16 混合加载（GPU 仅 2.9GB，其余受内存带宽限制，RTF≈10），
    预期 RTF 降到 ~1.5-2。
    """
    import torch
    import accelerate
    from accelerate.utils import set_module_tensor_to_device
    from safetensors import safe_open
    from transformers import AutoConfig, AutoTokenizer
    from models.breeze import BreezeForConditionalGeneration  # noqa: PLC0415
    from qwen_tts import Qwen3TTSTokenizer  # noqa: PLC0415

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import breeze_int8_convrot as int8q  # noqa: PLC0415  （import 时强制 eager 后端）

    started = time.time()
    args_depth_fp32 = bool(depth_fp32)
    # 显存闸门：模型 GPU 部分 + codec 0.65GB + 激活裕量
    need_mb = int(gpu_gb * 1024 + 1500)
    deadline = time.time() + WAIT_VRAM_SECONDS
    while True:
        free = free_vram_mb()
        if free < 0 or free >= need_mb:
            break
        if time.time() >= deadline:
            raise RuntimeError(
                f"空闲显存只有 {free}MB，INT8 加载需约 {need_mb}MB。"
                f"请等 Gemma 空闲休眠（或停掉 Gemma）后重试。")
        log(f"显存不足（{free}MB < {need_mb}MB），等 LLM 休眠…")
        time.sleep(15)

    tokenizer = AutoTokenizer.from_pretrained(bf16_ckpt, fix_mistral_regex=False)
    cfg = AutoConfig.from_pretrained(bf16_ckpt, trust_remote_code=True)
    cfg._attn_implementation = "eager"

    import models.fast_streaming as fast_streaming_module  # noqa: PLC0415
    original_get_device = fast_streaming_module._get_device
    fast_streaming_module._get_device = _prefer_cuda_get_device
    try:
        log(f"INT8 加载：GPU 预算 {gpu_gb}GiB（text_encoder 驻 CPU）…")
        with accelerate.init_empty_weights():  # 默认 include_buffers=False，buffer 保持实体
            model = BreezeForConditionalGeneration._from_config(cfg)
        qmap = int8q.scan_checkpoint_quantization(int8_ckpt)
        replaced = int8q.replace_quantized_linears(model, qmap)
        log(f"INT8：{len(replaced)}/{len(qmap)} 个 Linear 已替换为 ConvRotInt8Linear")
        # tied embedding：指向同一 Parameter，载入 depth_decoder 侧即同时填充 backbone 侧
        model.backbone_model.embed_tokens.embed_audio_tokens.weight = (
            model.depth_decoder.model.embed_tokens.weight)

        # 手工设备布局：text_encoder 驻 CPU，其余 GPU
        no_split = list(getattr(model, "_no_split_modules", []) or [])

        def dev_for(name):
            # embed_text_tokens 仅 prefill 查表用（1.07GB），驻 CPU 省显存；
            # text_encoder 曾驻 CPU 但 int8 Linear 在 CPU 上极慢（35.9s/请求），必须驻 GPU
            return "cpu" if name.startswith("embed_text_tokens.") else "cuda:0"

        # 加载全部参数（跳过量化元数据与 checkpoint 内嵌的 codec_model.*）
        param_names = dict(model.named_parameters(remove_duplicate=False))
        loaded = 0
        with safe_open(int8_ckpt, framework="pt", device="cpu") as f:
            for name in f.keys():
                if name.endswith(".comfy_quant") or name.startswith("codec_model."):
                    continue
                if name not in param_names:
                    continue
                tensor = f.get_tensor(name)
                # Pascal（GTX 10 系）无原生 bf16：depth decoder 是逐帧热循环，
                # bf16 模拟运算极慢，可选转 fp32（权重仅 +0.2GB）
                if args_depth_fp32 and name.startswith("depth_decoder."):
                    tensor = tensor.float()
                set_module_tensor_to_device(model, name, dev_for(name), value=tensor)
                loaded += 1
        # 载入后重新 tie：set_module_tensor_to_device 会替换 Parameter 对象，
        # 载入前的 tie 会被 depth_decoder 侧的加载破坏（backbone 侧残留 meta 张量）
        model.backbone_model.embed_tokens.embed_audio_tokens.weight = (
            model.depth_decoder.model.embed_tokens.weight)
        # buffer 跟随所属模块设备：有直属参数的模块按参数设备，无参数模块按顶层归属
        # （rotary_emb 等只有 inv_freq buffer，跳过会残留 CPU 张量导致前向设备错配）
        for module_name, module in model.named_modules():
            if module_name.startswith("codec_model"):
                continue  # 未使用的内嵌 codec，不参与推理
            target = None
            for p in module.parameters(recurse=False):
                target = p.device
                break
            if target is None:
                target = torch.device("cpu") if module_name.startswith(
                    ("embed_text_tokens",)) else torch.device("cuda:0")
            for bname, buf in module._buffers.items():
                if buf is not None and buf.device != target:
                    module._buffers[bname] = buf.to(target)

        # embed_text_tokens 驻 CPU 的透明对齐钩子：入参搬到模块设备，输出搬回 GPU。
        # 该模块仅 prefill 查表（1.07GB），CPU 查表毫秒级；钩子让卸载对调用方透明。
        model_device = torch.device("cuda:0")

        def _cpu_pre_hook(module, args, kwargs):
            dev = next(module.parameters()).device

            def mv(t):
                return t.to(dev) if torch.is_tensor(t) else t

            return tuple(mv(a) for a in args), {k: mv(v) for k, v in kwargs.items()}

        def _cpu_post_hook(module, args, output):
            if torch.is_tensor(output):
                return output.to(model_device)
            if hasattr(output, "keys"):
                for k in list(output.keys()):
                    v = output[k]
                    if torch.is_tensor(v):
                        output[k] = v.to(model_device)
                    elif isinstance(v, (list, tuple)):
                        output[k] = type(v)(t.to(model_device) if torch.is_tensor(t) else t for t in v)
                return output
            return output

        for _sub in (model.embed_text_tokens,):
            _sub.register_forward_pre_hook(_cpu_pre_hook, with_kwargs=True)
            _sub.register_forward_hook(_cpu_post_hook)

        gpu_gb_actual = sum(p.numel() * p.element_size() for p in model.parameters()
                            if p.device.type == "cuda") / 1024 ** 3
        cpu_gb_actual = sum(p.numel() * p.element_size() for p in model.parameters()
                            if p.device.type == "cpu") / 1024 ** 3
        log(f"INT8 权重载入 {loaded} 个张量：GPU {gpu_gb_actual:.2f}GB / CPU {cpu_gb_actual:.2f}GB，"
            f"用时 {time.time() - started:.1f}s")
        model.eval()
        for p_ in model.parameters():
            p_.requires_grad_(False)

        audio_tokenizer = Qwen3TTSTokenizer.from_pretrained(
            str(Path(bf16_ckpt) / "audio_tokenizer"), device_map="cuda")
        return tokenizer, model, audio_tokenizer
    finally:
        fast_streaming_module._get_device = original_get_device


def load_engine(ckpt, device_arg=None):
    """懒加载模型与流式运行时（首次请求调用，之后复用）。

    device_arg 取值：
    - auto/cuda：整体加载到 GPU（原始行为，约需 7.7GB 空闲显存）；
    - cpu：纯 CPU；
    - 数字字符串如 "3.5"：混合放置，GPU 预算 3.5GiB，其余驻 CPU（6GB 显存机器用）；
    - "int8"：INT8 ConvRot 量化路径（需 --int8-ckpt 指定量化权重）。
    纯 CPU 不走显存闸门；混合模式按预算+裕量等待空闲显存。
    """
    with _lock:
        if _state["runtime"] is not None:
            return _state["runtime"]
        from breeze_infer.runtime import (  # noqa: PLC0415
            load_runtime,
            resolve_device,
            update_generation_config_for_breeze,
        )
        from models.fast_streaming import FastBreezeStreamingRuntime, FastStreamingConfig  # noqa: PLC0415

        started = time.time()
        if device_arg == "int8":
            tokenizer, model, audio_tokenizer = load_engine_int8(
                ckpt, _state["int8_ckpt"], float(_state.get("int8_gpu_gb") or 3.6),
                depth_fp32=bool(_state.get("int8_depth_fp32")))
            device = f"int8-convrot(gpu {_state.get('int8_gpu_gb') or 3.6}GiB+te-cpu)"
        else:
            gpu_budget = 0.0
            try:
                gpu_budget = float(device_arg)
            except (TypeError, ValueError):
                gpu_budget = 0.0
            hybrid = gpu_budget > 0
            if hybrid:
                # 预算 + 0.9GB（激活/临时缓冲裕量），等待 LLM 休眠腾显存
                need_mb = int(gpu_budget * 1024 + 900)
                deadline = time.time() + WAIT_VRAM_SECONDS
                while True:
                    free = free_vram_mb()
                    if free < 0 or free >= need_mb:
                        break
                    if time.time() >= deadline:
                        raise RuntimeError(
                            f"空闲显存只有 {free}MB，混合加载需约 {need_mb}MB。"
                            f"请等 Gemma 空闲休眠（或停掉 Gemma）后重试。")
                    log(f"显存不足（{free}MB < {need_mb}MB），等 LLM 休眠…")
                    time.sleep(15)
                tokenizer, model, audio_tokenizer = load_engine_hybrid(ckpt, gpu_budget)
                device = f"cuda+cpu-offload({gpu_budget}GiB)"
            else:
                device = resolve_device(None if device_arg in (None, "auto") else device_arg)
                free = -1
                if device.startswith("cuda"):
                    free = wait_for_vram()
                log(f"加载 Breeze-TTS-2（device={device}, bf16 eager，加载前空闲显存 {free}MB）…")
                tokenizer, model, audio_tokenizer = load_runtime(Path(ckpt), device=device, attn_implementation="eager")
        update_generation_config_for_breeze(model)
        if device_arg == "int8":
            # Pascal（CC 6.1）不支持 Triton（≥7.0），depth decoder 的
            # torch.compile fast 路径不可用；backbone decode 可用原生 CUDA Graph
            config = FastStreamingConfig(
                max_new_tokens=MAX_NEW_TOKENS,
                max_seq_len=MAX_SEQ_LEN,
                fast_all=None,
                fast_text_encoder=False,
                fast_backbone_prefill=bool(_state.get("int8_fast_backbone", True)),
                fast_backbone_decode=bool(_state.get("int8_fast_backbone", True)),
                fast_depth_decoder=False,
                fast_codec=False,
                repetition_penalty=REPETITION_PENALTY,
            )
        else:
            config = FastStreamingConfig(
                max_new_tokens=MAX_NEW_TOKENS,
                max_seq_len=MAX_SEQ_LEN,
                fast_all=False,
            fast_text_encoder=False,
            fast_backbone_prefill=False,
            fast_backbone_decode=False,
            fast_depth_decoder=False,
            fast_codec=False,
            repetition_penalty=REPETITION_PENALTY,
        )
        runtime = FastBreezeStreamingRuntime(model, audio_tokenizer, config, tokenizer=tokenizer)
        _state["runtime"] = runtime
        _state["device"] = device
        _state["sample_rate"] = int(runtime.sample_rate)
        log(f"模型就绪，用时 {time.time() - started:.1f}s，采样率 {_state['sample_rate']}")
        return runtime


def split_text(text, max_chars):
    """按句号/问号/感叹号/分号/换行切段，超长句再硬切，保证每段不超过 max_chars。"""
    pieces = re.split(r"(?<=[。！？；!?;\n])", text)
    chunks = []
    buffer = ""
    for piece in pieces:
        piece = piece.strip()
        if piece == "":
            continue
        while len(piece) > max_chars:
            chunks.append(piece[:max_chars])
            piece = piece[max_chars:]
        if len(buffer) + len(piece) <= max_chars:
            buffer += piece
        else:
            if buffer != "":
                chunks.append(buffer)
            buffer = piece
    if buffer != "":
        chunks.append(buffer)
    return chunks


def speak(text, instruction, ref_audio, ref_text, cfg_scale, seed, max_chars):
    """分段合成后拼接，返回 (wav 路径, 时长秒, 段数)。"""
    import numpy as np  # noqa: PLC0415
    import soundfile as sf  # noqa: PLC0415
    from breeze_infer.runtime import set_all_seeds  # noqa: PLC0415
    from breeze_infer.templates import get_template, prepare_inputs, select_template_name  # noqa: PLC0415

    runtime = load_engine(_state["ckpt"], _state["device_arg"])
    chunks = split_text(text, max_chars)
    if not chunks:
        raise ValueError("切分后没有可合成的内容")
    gap = np.zeros(int(_state["sample_rate"] * 0.25), dtype=np.float32)

    pieces = []
    total_started = time.time()
    for index, chunk in enumerate(chunks):
        request_id = f"req-{index}-{uuid.uuid4().hex[:6]}"
        request = {"id": request_id, "text": chunk, "speaker": "S0"}
        if instruction:
            request["instruction"] = instruction
        if ref_audio:
            request["ref_audio_path"] = ref_audio
            request["ref_text"] = ref_text or ""
        template = get_template(select_template_name(request))
        set_all_seeds(int(seed) + index)
        started = time.time()
        inputs = prepare_inputs(
            runtime.tokenizer,
            runtime.audio_tokenizer,
            runtime.model,
            [request],
            template,
            guidance_scale=float(cfg_scale),
            guidance_scale_ref=None,
            guidance_scale_ins=None,
        )
        audio_parts = []
        for piece in runtime.iter_audio_chunks(inputs, request_id=request_id, seed=int(seed) + index):
            audio_parts.append(piece.audio)
        if not audio_parts:
            log(f"第 {index + 1}/{len(chunks)} 段没有生成音频，跳过：{chunk[:20]}")
            continue
        audio = np.concatenate([np.asarray(item).reshape(-1) for item in audio_parts]).astype(np.float32)
        pieces.append(audio)
        pieces.append(gap)
        log(f"第 {index + 1}/{len(chunks)} 段完成：{len(chunk)} 字，{len(audio) / _state['sample_rate']:.1f}s，"
            f"用时 {time.time() - started:.1f}s")

    if not pieces:
        raise RuntimeError("所有分段都合成失败")
    merged = np.concatenate(pieces[:-1])  # 去掉最后一段尾部的静音
    os.makedirs(_state["out_dir"], exist_ok=True)
    path = os.path.join(_state["out_dir"], f"tts-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}.wav")
    sf.write(path, merged, _state["sample_rate"], subtype="PCM_16")
    duration = len(merged) / float(_state["sample_rate"])
    log(f"合成完成：{os.path.basename(path)} 共 {len(chunks)} 段 / {duration:.1f}s，总用时 {time.time() - total_started:.1f}s")
    return path, duration, len(chunks)


class Handler(AudioHttp):
    """极简 HTTP：/health、/speak、/unload。"""

    server_version = "breeze-tts-service/1.2"
    token_env = "SYNLOVE_TTS_API_KEY"
    request_lock = threading.Lock()

    def _reply(self, code, payload):
        """写 JSON 响应。"""
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        """健康检查。"""
        if not self.authorized():
            return
        if self.path.startswith("/health"):
            return self._reply(200, {
                "ok": True,
                "service": "tts",
                "protocol": 1,
                "loaded": _state["runtime"] is not None,
                "device": _state["device"],
                "ckpt": _state["ckpt"],
                "freeVramMb": free_vram_mb(),
            })
        return self._reply(404, {"ok": False, "error": "unknown path"})

    def do_POST(self):
        """合成 / 卸载入口。"""
        if not self.authorized():
            return
        route = self.path.split("?")[0]
        if route in ("/unload", "/speak") and not self.is_local():
            return self._reply(403, {"ok": False, "error": "local endpoint only"})
        if route == "/unload":
            if not self.request_lock.acquire(False):
                return self._reply(409, {"ok": False, "error": "busy"})
            self._reply(200, {"ok": True, "note": "服务即将退出，显存会释放"})
            log("收到 /unload，退出进程")
            os._exit(0)
        if route not in ("/speak", "/v1/audio/speech", "/audio/speech"):
            return self._reply(404, {"ok": False, "error": "unknown path"})
        if not self.request_lock.acquire(False):
            return self._reply(429, {"ok": False, "error": "busy"})
        ref = None
        try:
            payload = self.read_json()
            binary = route != "/speak"
            if binary and payload.get("response_format", "wav") != "wav":
                raise ValueError("Breeze 服务只支持 response_format=wav")
            if binary and payload.get("refAudio"):
                raise ValueError("跨进程接口请使用 refAudioBase64 上传参考音频")
            if payload.get("refAudioBase64"):
                import base64
                import tempfile
                audio = base64.b64decode(payload["refAudioBase64"], validate=True)
                with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
                    handle.write(audio)
                    ref = handle.name
                payload["refAudio"] = ref
            text = str(payload.get("input" if binary else "text", "")).strip()
            if text == "":
                raise ValueError("text 不能为空")
            if len(text) > _state["max_total_chars"]:
                raise ValueError(
                    f"文本过长（{len(text)} 字），单次上限 {_state['max_total_chars']} 字；请分段调用。"
                )
            _state["busy"] = True
            _state["last_active"] = time.time()
            try:
                path, duration, count = speak(
                text,
                str(payload.get("instruction") or "").strip() or None,
                payload.get("refAudio"),
                payload.get("refText"),
                float(payload.get("cfgScale") or 1.0),  # 官方默认 1.0；>1 需模板带 negative prompt
                int(payload.get("seed") or 42),
                int(payload.get("maxChars") or _state["max_chars"]),
                )
            finally:
                _state["busy"] = False
            _state["last_active"] = time.time()
            if binary:
                self.reply_audio(path)
            else:
                self._reply(200, {"ok": True, "path": path, "duration": duration,
                                         "chunks": count, "sampleRate": _state["sample_rate"]})
            # 显卡宝贵：回完就退出，下次用再加载
            if _state["exit_after_request"]:
                try:
                    self.wfile.flush()
                except OSError:
                    pass
                threading.Timer(2.0, lambda: os._exit(0)).start()
            return
        except ValueError as error:
            self.close_connection = True
            return self._reply(400, {"ok": False, "error": str(error)})
        except Exception as error:  # noqa: BLE001
            log("合成失败：" + "".join(traceback.format_exception_only(type(error), error)).strip())
            return self._reply(500, {"ok": False, "error": f"{type(error).__name__}: {error}"})
        finally:
            if ref:
                os.unlink(ref)
            _state["last_active"] = time.time()
            self.request_lock.release()

    def log_message(self, fmt, *args):  # noqa: D401
        """压掉默认访问日志。"""
        return


def idle_watchdog(idle_seconds):
    """闲置超时退出（释放约 7.7GB 显存）。"""
    if idle_seconds <= 0:
        return
    while True:
        time.sleep(max(10, idle_seconds // 6))
        if _state["runtime"] is None or Handler.request_lock.locked():
            continue
        if time.time() - _state["last_active"] >= idle_seconds:
            log(f"闲置 {idle_seconds}s，退出以释放显存")
            os._exit(0)


def main(argv):
    """入口。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=7862)
    parser.add_argument("--host", default="127.0.0.1", help="监听地址，默认仅本机")
    parser.add_argument("--api-key-env", default="SYNLOVE_TTS_API_KEY", help="Bearer 密钥环境变量")
    parser.add_argument("--ckpt", default=os.path.join(ROOT, "models", "breezeTts2"))
    parser.add_argument("--idle-seconds", type=int, default=600)
    parser.add_argument("--out-dir", default=_state["out_dir"])
    parser.add_argument("--max-chars", type=int, default=120)
    parser.add_argument("--max-total-chars", type=int, default=3000)
    parser.add_argument("--exit-after-request", type=int, default=0, help="旧单次模式开关；共享服务保持默认 0")
    parser.add_argument("--device", default=os.environ.get("BREEZE_DEVICE", "auto"),
                        help="auto/cuda/cpu/数字(GPU预算GiB)/int8")
    parser.add_argument("--int8-ckpt", default=os.environ.get("BREEZE_INT8_CKPT", ""),
                        help="INT8 ConvRot 量化权重文件（--device int8 时必填）")
    parser.add_argument("--int8-gpu-gb", type=float, default=float(os.environ.get("BREEZE_INT8_GPU_GB") or 3.6),
                        help="INT8 模式 GPU 预算 GiB（text_encoder 之外的权重上限，默认 3.6）")
    parser.add_argument("--int8-depth-fp32", type=int, default=int(os.environ.get("BREEZE_INT8_DEPTH_FP32") or 0),
                        help="INT8 模式下 depth decoder 权重转 fp32（Pascal 无原生 bf16，fp32 更快）")
    parser.add_argument("--int8-fast-all", type=int, default=int(os.environ.get("BREEZE_INT8_FAST_ALL") or 0),
                        help="INT8 模式下开启官方 fast_all（CUDA Graph 捕获，消除逐 op 开销）")
    args = parser.parse_args(argv)
    Handler.token_env = args.api_key_env
    if args.host not in ("127.0.0.1", "localhost", "::1") and not os.environ.get(args.api_key_env):
        parser.error("非回环监听必须配置 Bearer 密钥")
    _state["ckpt"] = args.ckpt
    _state["device_arg"] = args.device
    _state["int8_ckpt"] = args.int8_ckpt
    _state["int8_gpu_gb"] = args.int8_gpu_gb
    _state["int8_depth_fp32"] = bool(args.int8_depth_fp32)
    _state["int8_fast_all"] = bool(args.int8_fast_all)
    _state["out_dir"] = args.out_dir
    _state["max_chars"] = args.max_chars
    _state["max_total_chars"] = args.max_total_chars
    _state["exit_after_request"] = bool(args.exit_after_request)
    threading.Thread(target=idle_watchdog, args=(args.idle_seconds,), daemon=True).start()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    log(f"Breeze-TTS 服务启动：端口 {args.port} 权重 {args.ckpt} 闲置退出 {args.idle_seconds}s "
        f"每段≤{args.max_chars}字 单次≤{args.max_total_chars}字")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main(sys.argv[1:])
