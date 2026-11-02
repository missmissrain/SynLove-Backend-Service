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

## 快速启动

先准备 Python 3.9+。TTS 和 Gemma 的权重较大，单独安装：

```bat
:: ASR：安装依赖后启动
cd synlove-asr
pip install -r requirements.txt
python services\whisperService.py --port 8081 --model small --device auto

:: TTS：需要先按 synlove-tts\README.md 准备上游推理代码和权重
cd synlove-tts
python services\breezeTtsService.py --port 7862 --ckpt <权重目录> --idle-seconds 600

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

- TTS 修复无 `instruction` 时 `cfgScale` 默认值导致的 HTTP 400，与主仓库保持一致。
- `/health` 统一返回 `ok`、`service`、`protocol`，供控制台做端口身份确认。
- README 补充本机/远程职责边界、实际启动命令和健康检查方式。
