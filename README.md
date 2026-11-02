# SynLove-Backend-Service

发行版本：2026.11，配套 SynLove-Personal-Agent v2.0。该版本包含 Windows 家庭服务器侧的推理兼容实现、后台业务和出站节点；模型权重与部署参数由使用者单独准备。

SynLove 的公共能力仓库，与 [SynLove-Personal-Agent](https://github.com/missmissrain/SynLove-Personal-Agent) 互为补集。两个仓库合在一起就是完整项目。

这里提供语音合成、语音识别、大模型、爬虫中心、网页收集、视频理解和定时任务。可以与个人实例部署在同一台机器，也可以放到家庭服务器，通过直连接口或 VPS 中转调用。微信、QQ、聊天记录、长期记忆和个人实例观测仍由个人实例维护。

## 包含什么

| 服务 | 默认端口 | 入口 | 用途 |
| --- | --- | --- | --- |
| TTS 语音合成 | 7862 | `synlove-tts/services/breezeTtsService.py` | 24kHz wav；懒加载，可配置闲置退出 |
| ASR 语音识别 | 8081 | `synlove-asr/services/whisperService.py` | faster-whisper 转写；懒加载，闲置自动退出 |
| 本地 LLM | 8080 | `synlove-gemma/serve.py` | 用 llama.cpp 提供 OpenAI 兼容接口，带显存闸门 |
| 爬虫中心 | 8792 | `businessRuntime/microServices/ServiceHost.py digest` | 统一新闻采集、去重、Gemma 摘要和每日计划；RSSHub 是内部依赖 |
| 网页收集 | 8796 | `businessRuntime/microServices/ServiceHost.py crawler` | 隔离浏览器渲染公开网页，再由 Gemma 整理 |
| 视频理解 | 8793 | `businessRuntime/microServices/ServiceHost.py video` | 上传文件或公开链接，经 ASR、画面分析生成报告 |
| 定时任务 | 8791 | `businessRuntime/microServices/ServiceHost.py reminders` | 持久保存提醒和到期结果，离线后等待个人实例接收 |

TTS 和 ASR 的 `/health` 会返回统一身份字段：

```json
{"ok": true, "service": "tts", "protocol": 1, "loaded": false}
```

`service` 必须是 `tts` 或 `asr`，`protocol` 当前为 `1`。`loaded:false` 表示权重尚未加载，接口仍可接收请求。部署到远程时应保持 API 与守护器运行，不能用客户端本机依赖代替远端接口检查。

## 低配置机器兼容

| 项目 | 实现 | 使用条件 |
| --- | --- | --- |
| TTS 混合加载 | `--device <GiB数字>` 通过设备映射限制 GPU 权重预算，剩余部分放 CPU | 需要上游推理代码、accelerate 与足够系统内存 |
| TTS INT8 | `--device int8 --int8-ckpt <文件> --int8-gpu-gb 3.6` 使用 ConvRot INT8 权重 | 需要匹配的量化 checkpoint；普通 BF16 权重不能直接替代 |
| 较旧 NVIDIA 架构 | INT8 运行时使用 comfy-kitchen eager 后端，避免不支持的预编译 CUDA kernel | 速度依显卡和 torch 版本变化，不能保证所有显卡兼容 |
| ASR CPU | `--device cpu --model small` 使用 faster-whisper CPU INT8 | 减少与 TTS / Gemma 的显存竞争 |
| Gemma 量化 | llama.cpp + GGUF、可选视觉投影器，显存门控和空闲休眠 | 权重、投影器、上下文大小与 llama.cpp 构建须匹配 |
| 视觉兼容代理 | `llmProxy.py` 将消息中的图片片段前置，其余内容和鉴权转发到回环上游 | 默认外部 API 8080、内部推理 8180 |
| 后台运行 | RSSHub 强制生产模式；浏览器位置由 `PLAYWRIGHT_BROWSERS_PATH` 注入 | Windows 计划任务账户须有模型、浏览器和数据目录权限 |

低显存通常以速度和系统内存换取可运行性。真实验收中 TTS 冷加载与合成可能耗时数分钟，请同时配置主程序、Hub、节点和模型请求超时。不要只延长 GUI 超时。

建议推理使用独立 Python 3.12 环境；ASR/TTS 依赖见各子目录。INT8 的附加依赖见 `synlove-tts/requirementsInt8.txt`，既有环境可继续复用，无须重复安装。权重及第三方运行库遵循各自许可。

## API 与默认端口

| 服务 | 方法与路径 | 请求与返回 |
| --- | --- | --- |
| TTS · 7862 | `GET /health`；`POST /v1/audio/speech` | 合成请求含 model/input/voice/response_format；返回音频字节 |
| ASR · 8081 | `GET /health`；`POST /v1/audio/transcriptions` | multipart file/model/language；返回 text 等转写字段 |
| LLM · 8080 | `GET /v1/models`；`POST /v1/chat/completions` | OpenAI 兼容消息与可选流式输出 |
| 内部 LLM · 8180 | llama.cpp 推理入口 | 仅回环监听，不直接公开 |
| 业务 · 8791/8792/8793/8796 | `GET /health`；`POST /v1/rpc` | 统一 JSON-RPC 工具协议及持久任务处理；详见部署文档 |
| 内部 RSSHub · 1200 | RSS 路由与 `/healthz` | 为爬虫中心提供订阅，通常无需公网开放 |

非回环监听应设置对应 `SYNLOVE_*_API_KEY`；通过 HTTPS/WSS 和可信 CA 跨网络连接。模型 Hub 默认 9010，可在 VPS 上配置 TLS 或反向代理 443；FRP SSH 远控由独立仓库提供，不是本仓库的业务接口。

## 本机与远程

在 SynLove 的 `config/serviceConfig.json` 里，每个服务有两个关键字段：

- `baseURL`：本机回环地址或远程服务器地址。
- `management`：`managed` 表示由 SynLove 启动器负责拉起和关闭；`external` 表示服务已经在别处运行，SynLove 只探活，不偷偷补拉第二个进程。

同一台机器部署时，两个仓库可以放在不同目录。远程部署时，只把公共服务指向服务器；个人数据、消息队列和日志仍留在本机。远程机器建议只开内网端口或放在反向代理后面，并为服务设置独立 API Key，不要把管理端口直接暴露到公网。

每日新闻归爬虫中心维护，不再部署第二个新闻摘要服务。定时任务和新闻日程使用本部署自己的 SQLite；提醒、新闻结果先进入持久通知队列，个人实例拉取落盘后确认，再通过本机既有 QQ 队列发送。服务器不保存 QQ/微信密钥，也不启动消息消费者。Codex/OpenCode 和个人会话仍在个人实例运行。

目前通知接收默认复用个人实例已配置的 QQ 接收人；微信主动推送受平台窗口约束，不能据此保证离线数小时后的主动发送。本方案保证通知保存和本机持久入队，平台最终发送仍受通道可用性、配额及未知网络结果影响。

多用户部署时，每个个人实例的日程、通知库、API 密钥及能力路由须独立，不能把多个用户混进同一个业务节点。本版提供单个个人实例的独立后台部署，不宣称已实现共享业务数据库的多租户隔离。

## 启动四项后台功能

详细步骤见 [后台业务部署](deploy/BusinessDeployment.md)。先准备现有模型 API、RSSHub 和功能依赖，不需要重装已有环境。

```powershell
# 只创建缺失配置，不覆盖已有数据库或模型配置
python businessRuntime/BackendSetup.py --existing-node-config <家庭服务器现有node.json>

# relayNode首次部署需要ws和TypeScript；已有依赖可直接复用，不必重装
cd relayNode
npm run build
cd ..

# 复用服务器已有Python和RSSHub构建，批量启动业务与出站节点
powershell -File deploy/StartBusiness.ps1 -Python <现有python.exe> -RssHubEntry <RSSHub/dist/index.mjs>
```

`businessRuntime/config/serviceConfig.json` 配置服务器自身的模型地址；`relayNode/config/node.json` 配置现有 VPS 连接。自签证书使用可信 CA 和 `NODE_EXTRA_CA_CERTS`，不能关闭 TLS 校验。`RELAY_TOKEN` 可由环境提供，不写进公开模板。

四项出站能力名为 `crawlerCenter`、`webCollection`、`video`、`scheduler`，与已运行的 `chat/tts/asr` 节点并存。VPS 无须增加业务代码。节点只开放固定业务路径，不能通过中转执行任意 HTTP、shell 或关闭服务。

远端业务 API 有身份心跳：`{"ok":true,"service":"digest","protocol":1}`。模型、RSSHub、ASR、浏览器或视频依赖缺失时，对应功能返回 `ok:false`。模型探活不执行付费生成，因而不能替代真实任务验收。

旧安装先按部署文档冻结并导入日程，再切换个人实例。端口扫描不能绕过旧日程迁移；切换工具会校验四项真实健康及迁移收据，失败时保持原地址。

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
- `businessRuntime`：四项业务实现、独占调度、数据迁移和持久通知。
- `relayNode`：家庭服务器出站节点，复用既有 Hub 协议，不运行 VPS Hub。

密钥、模型权重、用户数据、日志和真实服务器地址不进入仓库。`deploy/services.example.env` 只是占位模板，复制后填写，不要提交真实值。

## 最近更新

### 2026.11 · 2026-11-03 · 配套主程序 v2.0

- 纳入服务器侧 TTS 混合加载、INT8 ConvRot、较旧 GPU eager 兼容与空闲退出实现。
- 纳入 ASR CPU INT8/CUDA 降级、Gemma 视觉代理、鉴权和进程监督实现。
- 保留服务器侧业务就绪检查，RSSHub 生产运行设置；浏览器路径改为部署环境注入。
- 补充默认端口、API、量化前提、超时边界和依赖说明。
- 来源为家庭服务器代码快照，未用个人电脑上的旧后台副本覆盖优化实现。

以下为历史更新，继续保留以便查询版本演进。

### 后台功能拆分版

- 爬虫中心统一新闻采集和每日新闻；网页收集、视频理解和定时任务可独立部署。
- 增加固定业务能力转发、文件附件、持久结果拉取及本机队列去重。
- 增加部署入口、日程迁移校验与功能依赖心跳；个人 GUI 区分本机和远程可用。
- 代码支持不代表服务器已部署，实际以节点能力和功能协议检查为准。


### 健康协议与 TTS 修复版

- TTS 修复无 `instruction` 时 `cfgScale` 默认值导致的 HTTP 400，与主仓库保持一致。
- `/health` 统一返回 `ok`、`service`、`protocol`，供控制台做端口身份确认。
- README 补充本机/远程职责边界、实际启动命令和健康检查方式。
