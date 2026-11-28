# SynLove Backend Service

欣爱的后台服务：模型调度、语音合成、语音识别、本地大模型、业务运行时、中转节点与设备管理。

它只关心「把模型和任务跑起来」，不关心人格与对话怎么组织。

## 功能

### ModelHub（8810）

- 统一登记所有可能占用 GPU 的模型：模型名、服务端口、健康地址、卸载接口
- 显存预算与串行调度：先拿执行锁，再检查显存，必要时请求空闲同伴卸载
- 加载 / 卸载 / 预热接口，健康状态与显存快照
- 死同伴视为未运行，等待有上限，避免整条 GPU 管线被一个失联服务卡死

### Breeze TTS（7862）

- 固定声线合成，支持参考音克隆与情绪指令
- int8 混合加载：6GB 显存可跑，权重按预算分配 GPU / CPU
- 长文本按句分段，逐段合成再拼接
- 闲置卸载显存、按需重新加载
- OpenAI 风格 `/v1/audio/speech`，Bearer 鉴权

### Whisper ASR（8081）

- 语音转写，支持文件与流式
- 与 TTS / LLM 共享显存协调
- OpenAI 风格 `/v1/audio/transcriptions`

### 本地 LLM（8080）

- llama.cpp 服务代理，OpenAI 兼容
- 空闲软卸载、显存阈值控制、失败分类
- 与云端 provider 组成故障转移链

### 设备管理 fleetService（17864）

- 设备登记、心跳、能力表、在线状态
- 指令队列：幂等 ID、状态机、真实回执
- 语音信箱：离线暂存、上线投递
- 播放路由：选目标设备、播放前复核
- 媒体直传：`file.stream` 把音频 / 视频 / 图片推到终端，终端本地队列自动续播
- 由 `personal-agent` 的 CoreService 进程内托管，代码归属本库

### 业务运行时（8791 起）

- 采集、调度、视频、新闻等常驻业务
- 独立进程与端口，失败不影响主对话
- 统一 supervisor 与日志

### 中转节点 relayNode

- 出站 WSS 节点，兼容 VPS 任务协议
- 两种配置：模型节点（chat/tts/asr）与业务节点（采集/调度/视频）
- 与 `synlove-relay` 共用同一套协议

## 目录

| 目录 | 内容 |
|---|---|
| `modelHub/` | 模型登记、显存调度、租约 |
| `gpuRuntime/` | GPU 协调、驻留、内存探测、调度状态 |
| `synlove-tts/` | TTS 服务与权重加载 |
| `synlove-asr/` | ASR 服务 |
| `synlove-gemma/` | 本地 LLM 代理 |
| `fleetService/` | 设备管理、指令、语音信箱、播放路由 |
| `businessRuntime/` | 业务服务宿主与各业务实现 |
| `relayNode/` | 出站中继节点（TS） |
| `config/` | 示例配置（真实配置在 `models/config`） |

## 运行

```bash
python -m modelHub.runModelHub --root . --host 127.0.0.1 --port 8810 \
  --config config/modelHub.server.json

python services/breezeTtsService.py --port 7862 --device int8
```

## 配置

- 模型与服务登记：`models/config/modelHub.server.json`
- 服务地址与鉴权：`models/config/serviceConfig.json`
- 密钥：`models/config/secrets.json`（不入库）
- GPU 策略：`config/gpuResidency.json`

## 测试

```bash
python -m unittest discover -s tests -p "*Tests.py"
```

## 边界

- 不包含人格、渠道、在场判断，这些在 `personal-agent`
- 设备管理代码在本库，但进程由 `personal-agent` 托管（17864）
