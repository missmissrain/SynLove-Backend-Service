# SynLove Backend Service

SynLove 的公共能力仓库，与 [SynLove-Personal-Agent](https://github.com/missmissrain/SynLove-Personal-Agent) 互为补集。两个仓库合在一起就是完整项目。

这里放的是可以被多个本机实例复用的服务：语音合成、语音识别、本地大模型，以及以后新增的公共后台能力。每个 SynLove 实例自己的微信、QQ、聊天记录、长期记忆、任务状态、日志和调用链都留在本机，不进入这个仓库。

## 包含什么

| 服务 | 默认端口 | 入口 | 用途 |
| --- | --- | --- | --- |
| TTS 语音合成 | 7862 | `synlove-tts/services/breezeTtsService.py` | 把文字合成 24kHz wav；按需加载，合成后释放显存 |
| ASR 语音识别 | 8081 | `synlove-asr/services/whisperService.py` | faster-whisper 转写；懒加载，闲置自动退出 |
| 本地 LLM | 8080 | `synlove-gemma/serve.py` | 用 llama.cpp 提供 OpenAI 兼容接口，带显存闸门 |

TTS 和 ASR 的 `/health` 会返回统一身份字段：

```json
{"ok": true, "service": "tts", "protocol": 1, "loaded": false}
```

`service` 必须是 `tts` 或 `asr`，`protocol` 当前为 `1`。SynLove 控制台用这组字段确认端口后面确实是自己的服务；普通 HTTP 服务或端口占用不会被误认成可用。端口没监听但本机依赖和模型齐备时，控制台会显示“按需待机”，第一次请求时再拉起。

## 本机与远程

在 SynLove 的 `config/serviceConfig.json` 里，每个服务有两个关键字段：

- `baseURL`：本机回环地址或远程服务器地址。
- `management`：`managed` 表示由 SynLove 启动器负责拉起和关闭；`external` 表示服务已经在别处运行，SynLove 只探活，不偷偷补拉第二个进程。

同一台机器部署时，两个仓库可以放在不同目录。远程部署时，只把公共服务指向服务器；个人数据、消息队列和日志仍留在本机。远程机器建议只开内网端口或放在反向代理后面，并为服务设置独立 API Key，不要把管理端口直接暴露到公网。

每日新闻的计划时间、开关及当天执行标记由 `SynLove-Personal-Agent` 的新闻微服务维护。Codex/OpenCode 的完成检测、去重、结果润色和 QQ 推送也在个人实例运行；润色可以调用配置的模型接口。部署本仓库不需要复制个人会话数据库，也不要在服务器另建同一用户的消息消费者或重复定时任务。

## 快速启动

先准备 Python 3.9+；实际推理环境还须满足各子目录的依赖要求。下面每组命令均从仓库根目录开始，在各服务自己的环境中执行。

```bat
:: ASR：安装依赖后启动
cd synlove-asr
pip install -r requirements.txt
python services\whisperService.py --port 8081 --model small --device auto
```

```bat
:: TTS：需要先按 synlove-tts\README.md 准备上游推理代码和权重
cd synlove-tts
python services\breezeTtsService.py --port 7862 --ckpt <权重目录> --idle-seconds 600
```

```bat
:: 本地 LLM：需要 llama.cpp 和 GGUF 权重
cd synlove-gemma
python serve.py --model <模型.gguf> --mmproj <视觉投影.gguf> --port 8080 --ctx 32768 --ngl 99
```

健康检查：

```bat
curl http://127.0.0.1:7862/health
curl http://127.0.0.1:8081/health
curl http://127.0.0.1:8080/v1/models
```

TTS 不带 `instruction` 时会把 `cfgScale` 设为 `1.0`。这是必须的：无音色描述时模板是 `tts_plain`，不支持 classifier-free guidance，沿用 `4.0` 会让标准请求返回 HTTP 400。

## 目录

- `synlove-tts`：语音合成 HTTP 服务、权重说明和命令行工具。
- `synlove-asr`：语音转写 HTTP 服务、依赖和命令行工具。
- `synlove-gemma`：Gemma 下载校验、显存闸门、llama.cpp 启动器和低显存实验。
- `deploy`：远程部署说明和环境变量模板。

密钥、模型权重、用户数据、日志和真实服务器地址不进入仓库。`deploy/services.example.env` 只是占位模板，复制后填写，不要提交真实值。

## 最近更新

### 配套职责说明版

- 与主仓库的完成汇报修复版配套，明确新闻日程、会话读取、去重和 QQ 推送留在个人实例。
- 按服务拆分启动示例，避免连续切换目录导致找不到脚本；补充各服务依赖要求。
- 本轮更新为部署文档调整，TTS、ASR、LLM 的接口保持兼容。

### 健康协议与 TTS 修复版

- TTS 修复无 `instruction` 时 `cfgScale` 默认值导致的 HTTP 400，与主仓库保持一致。
- `/health` 统一返回 `ok`、`service`、`protocol`，供控制台做端口身份确认。
- README 补充本机/远程职责边界、实际启动命令和健康检查方式。
