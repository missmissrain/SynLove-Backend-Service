# synlove-gemma · 本地 Gemma 4 E4B 推理

把 Gemma 4 E4B（GGUF）跑成本机 OpenAI 兼容服务：**分块并行下载 + SHA-256 校验 + 显存闸门 + 就绪探测**。
给本地 AI 管家做摘要、图谱抽取、图片描述用，也可以直接 `curl` 调用。

主项目：[missmissrain/SynLove-Personal-Agent](https://github.com/missmissrain/SynLove-Personal-Agent)。

## 权重与来源（重要）

| 项目 | 链接 |
|---|---|
| 权重（Hugging Face） | https://huggingface.co/unsloth/gemma-4-E4B-it-GGUF |
| 权重（国内镜像） | https://hf-mirror.com/unsloth/gemma-4-E4B-it-GGUF |
| llama.cpp（推理引擎） | https://github.com/ggml-org/llama.cpp |

| 文件 | 大小（字节） |
|---|---|
| `gemma-4-E4B-it-Q4_K_M.gguf` | 4 977 171 584 |
| `mmproj-BF16.gguf`（视觉投影器） | 991 552 320 |

本仓库**只包含下载/校验/启动脚本**，不含权重、不含 llama.cpp。

## 下载

```bat
python downloadGemmaModel.py
```

行为：按 64MB 分片、8 线程 Range 并行下载（走 `hf-mirror.com`），
分片合并后算 SHA-256，结果写入 `下载校验.json`（含每个文件的 size 与 sha256）。
中断后重跑会复用已下好的分片。

想用官方源就把脚本里的 `MIRROR_ROOT` 换成 `https://huggingface.co/unsloth/gemma-4-E4B-it-GGUF/resolve/main`。

## 编译 llama.cpp

```bat
git clone https://github.com/ggml-org/llama.cpp
cd llama.cpp
cmake -B build -DGGML_CUDA=ON
cmake --build build --config Release -j
```

产物一般在 `llama.cpp\build\bin\Release\llama-server.exe`（或 `build-ninja\bin\`）。
把它加进 PATH，或用 `--bin` 指定。

## 启动

```bat
python serve.py ^
  --model model\gemma-4-E4B-it-Q4_K_M.gguf ^
  --mmproj model\mmproj-BF16.gguf ^
  --port 8080 --ctx 32768 --ngl 99
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `--port` | 8080 | 只绑 `127.0.0.1` |
| `--ctx` | 32768 | 上下文 |
| `--ngl` | 99 | 卸载到 GPU 的层数（全量） |
| `--alias` | `gemma-4-E4B-it-Q4_K_M` | 客户端调用的模型名 |
| `--sleep-idle` | 120 | 空闲多少秒卸显存（`--sleep-idle-seconds`） |
| `--min-free-vram` | 5200 | 启动前要求的空闲显存（MB） |
| `--wait-seconds` | 300 | 显存不够时最多等多久 |
| `--dry-run` | — | 只打印命令 |

启动器会：等显存够 → 起进程 → 轮询端口直到就绪（最多 180 秒）→ 打印 pid 与日志路径。
日志在 `runtime/gemma-server.log`。

## 调用

OpenAI 兼容，任选一种：

```bat
curl http://127.0.0.1:8080/v1/chat/completions ^
  -H "Content-Type: application/json" ^
  -H "Authorization: Bearer local-only" ^
  -d "{\"model\":\"gemma-4-E4B-it-Q4_K_M\",\"messages\":[{\"role\":\"user\",\"content\":\"你好\"}]}"
```

图片描述（多模态）：

```bat
curl http://127.0.0.1:8080/v1/chat/completions ^
  -H "Content-Type: application/json" ^
  -d "{\"model\":\"gemma-4-E4B-it-Q4_K_M\",\"messages\":[{\"role\":\"user\",\"content\":[{\"type\":\"text\",\"text\":\"描述这张图\"},{\"type\":\"image_url\",\"image_url\":{\"url\":\"data:image/jpeg;base64,<...>\"}}]}]}"
```

## 显存与取舍

- Q4_K_M + 视觉投影器约占 **5GB**，**8GB 显存可跑**；
- `--reasoning off`：本地模型带隐藏推理会把 `max_tokens` 吃光，工具循环不稳；
- `--sleep-idle-seconds`：空闲自动卸显存，唤醒约十几秒（后台任务不急，值得）；
- 启动前等显存（默认 5200MB）：宁可晚点可用，也不要和游戏/训练互相拖死。
- **显存不够时不要指望"逐层流式"**：单 token 生成是内存带宽受限的，把权重搬上 GPU
  花的时间比在 CPU 上直接算还长（实测搬运/计算 = 2.2 倍）。正确做法是 `-ngl` 静态切分：
  能塞进显存的层常驻，塞不下的层常驻内存用 CPU 算，**每 token 只搬 6KB 的激活**。

## 找 `-ngl` 的最佳值（`tuneGpuLayers.py`）

换一张显卡、换一个上下文长度，最优层数都不一样，别猜，直接扫：

```bat
set PYTHONUTF8=1
python tuneGpuLayers.py --model model\gemma-4-E4B-it-Q4_K_M.gguf --ladder 0,8,16,24,28,32,99
```

它会用 `llama-bench` 逐档跑逐 token 生成，同时后台采样显存峰值，最后给出
「扣掉视觉预留后还剩多少」的推荐值。实测（RTX 4070 Laptop 8G，debug 构建）：

| -ngl | token/s | 显存峰值 |
| --- | --- | --- |
| 0（全 CPU） | 3.09 | 1503 MB |
| 16 | 4.79 | 2403 MB |
| 24 | 6.46 | 2875 MB |
| 99（全 GPU） | **53.64** | 4098 MB |

两个值得注意的现象：

1. **部分卸载不是线性下降**。24/34 层在 GPU 上只有 6.5 token/s，全放进去是 53.6 ——
   因为剩下的 CPU 层成了瓶颈，GPU 在等它。所以「能全放就全放」比「放一部分省着用」快得多；
2. 这次用的是 debug 构建（llama-bench 自己会警告 `asserts enabled`），真实 Release 构建会更快，
   但**相对趋势不变**。

`--mmproj-mb 950` 是给视觉投影器预留的显存，不开视觉就设 0。

## 目录

```
downloadGemmaModel.py   分块并行下载 + 校验（产出 下载校验.json）
serve.py                显存闸门 + 启动 llama-server + 就绪探测
tuneGpuLayers.py        扫 -ngl：逐档测生成速度 + 显存峰值，给推荐值
layerPipelineExp/       层流水线实验（交替放置 + 双缓冲，已验证不如静态切分）
model/                  权重落盘位置（.gitignore 已挡）
runtime/                服务日志
```

## 已知边界

- 只面向单机单卡；多卡需要自己加 `--split-mode`；
- 量化权重是 Q4_K_M，精度换速度；要更准换 Q5/Q8（显存随之上涨）；
- 模型许可证见 Hugging Face 仓库页面（Gemma 系有自己的使用条款）。
