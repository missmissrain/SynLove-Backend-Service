# SynLove Backend Service

这是 SynLove 的后台服务仓库，与 `SynLove-Personal-Agent` 互为补集。

它用于在家庭服务器或云服务器上统一运行可被多个本机实例复用的服务：

- TTS 语音合成
- ASR 语音识别
- LLM 推理服务
- 后续可加入新闻抓取、视频处理、公共资料索引和版本更新服务

每个 SynLove 本机实例自己的微信、QQ、聊天记录、长期记忆、任务状态、日志和调用链都保留在本机。这个仓库只提供无用户状态的公共能力，不保存个人会话。

两个仓库合在一起就是完整项目：

- `SynLove-Personal-Agent`：GUI、消息通道、本地记忆、本地状态、本地日志和本地编码器。
- `SynLove-Backend-Service`：TTS、ASR、LLM 以及其他可复用后台能力。

本机部署时，后台服务可以和个人智能体放在同一台机器；远程部署时，只需在 GUI 的 Backend Service 配置中填写服务器地址和端口。

TTS 和 ASR 的 `/health` 会返回 `service`、`protocol: 1` 和运行信息，供本机 GUI 进行端口身份确认。LLM 使用兼容接口的健康响应和模型服务状态确认。

## 目录

- `synlove-tts`：TTS HTTP 服务
- `synlove-asr`：ASR HTTP 服务
- `synlove-gemma`：Gemma 推理和模型启动工具
- `deploy`：远程服务器启动模板和环境变量示例

密钥、模型权重、用户数据和实际服务器地址不进入仓库。
