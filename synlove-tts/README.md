# synlove-tts · 本地语音合成（Breeze-TTS-2）

把 Breeze-TTS-2 包成一个**用完就退**的本地 HTTP 服务：一次请求合成一段语音（长文本按句切段再拼接），
合成完立刻释放显存。给本地 AI 管家发语音条用，也可以单独调。

主项目：[missmissrain/SynLove-Personal-Agent](https://github.com/missmissrain/SynLove-Personal-Agent)。

## 权重与来源（重要）

| 项目 | 链接 |
|---|---|
| 权重（Hugging Face） | https://huggingface.co/BreezeBlue/breeze-tts-2 |
| 权重（国内镜像） | https://hf-mirror.com/BreezeBlue/breeze-tts-2 |
| 推理代码（上游） | https://github.com/breezeblue-ai/breeze-tts |
| 产品/博客 | https://breezeblue.ai/breeze-tts-2 |
| 许可 | https://huggingface.co/BreezeBlue/Breeze-TTS-2/blob/main/LICENSE |

本仓库**只包含我们自己的服务封装与命令行**，不含权重、不含上游推理代码。

> 许可提醒：上游代码是 Apache 2.0，但**权重与自托管输出仅限研究与非商用**。
> 自用可以，商用前先读上面的许可原文。

权重文件（放到 `breezeTts2/` 目录）：

| 文件 | 大小 |
|---|---|
| `model-00001-of-00002.safetensors` | 4 961 989 890 字节 |
| `model-00002-of-00002.safetensors` | 2 004 567 152 字节 |
| `audio_tokenizer/model.safetensors` | 682 293 092 字节 |
| `tokenizer.json` / `config.json` / `generation_config.json` 等 | 见 HF 仓库 |

## 安装

```bat
:: 1. 上游推理代码（约几十 MB）
git clone https://github.com/breezeblue-ai/breeze-tts.git

:: 2. 它的依赖（建议单独 venv，别污染主环境）
cd breeze-tts
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt

:: 3. 权重下到本仓库的 breezeTts2/（可用 huggingface-cli 或浏览器）
```

目录约定（都可用环境变量改）：

```
synlove-tts/
├─ breeze-tts/          # 上游推理代码（BREEZE_REPO）
├─ breezeTts2/          # 权重（BREEZE_CKPT）
├─ services/            # 我们的服务
├─ tools/speak.py       # 命令行
└─ runtime/             # 日志与合成产物
```

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `BREEZE_REPO` | `./breeze-tts` | 上游推理代码目录 |
| `BREEZE_CKPT` | `./breezeTts2` | 权重目录 |
| `BREEZE_PYTHON` | `<BREEZE_REPO>/.venv/Scripts/python.exe` | 拉起服务用的解释器 |
| `BREEZE_TTS_PORT` | `7862` | 服务端口 |
| `BREEZE_NEED_VRAM_MB` | `6500` | 加载所需空闲显存 |
| `BREEZE_WAIT_VRAM_SECONDS` | `180` | 显存不够时最多等多久 |

## 用法

```bat
python tools\speak.py "你好，我是欣爱。" --voice tiantian2
python tools\speak.py "长文……" --instruction "温柔自信的年轻女性" --max-chars 80
```

首次会自动拉起服务（用 BREEZE_PYTHON），加载权重约 30 秒；合成完服务退出、显存释放。
输出 wav 路径会打印在最后一行。

音色预设：`tiantian`（甜妹）、`tiantian2`（清亮少女，默认）、`yuanqi`（元气）、
`wenrou`（温柔）、`zhixing`（知性）。也可以用 `--instruction` 直接写描述，或用
`--ref-audio` + `--ref-text` 克隆声线。

## HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 是否已加载、设备、空闲显存 |
| POST | `/speak` | `{"text","instruction","cfgScale","maxChars","refAudio","refText","seed"}` → `{"ok","path","duration","chunks"}` |
| POST | `/unload` | 立刻退出进程（释放显存） |

服务参数：`--port 7862 --ckpt <权重> --idle-seconds 600 --out-dir runtime/tts --max-chars 120 --max-total-chars 3000 --exit-after-request 1`

## 为什么这么绕（设计取舍）

- **不常驻**：权重 7GB、eager 推理 7.7GB 显存，8GB 卡上常驻会挤死对话模型 → 每次合成完退出；
- **按句切段**：一次生成太长会爆显存、也会"越说越飘" → 每段 ≤120 字，合成后拼接；
- **加载前查显存**：不够就等本地模型休眠（默认 120 秒空闲）再加载，超时给明确提示，不硬上 OOM；
- **单请求上限**：`max-total-chars` 是防跑飞的闸门（默认 3000 字）。

## 已知边界

- 只支持单路合成（同一时刻一个请求），并发会排队；
- 输出是 24kHz wav；要发微信/QQ 需自己转 mp3（`ffmpeg -i x.wav -codec:a libmp3lame -b:a 64k x.mp3`）；
- 显存 < 6GB 的卡不建议用（会频繁等显存）。
