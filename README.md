# SynLove · 后台服务

模型与设备的运行时：ModelHub 统一管理本地模型与显存，fleetService 管理终端，
businessRuntime 承载提醒、日报、视频摘要等业务服务。端口由部署配置决定，不在文档中固定。

## 服务

| 服务 | 说明 |
|---|---|
| ModelHub | 模型登记、租约、显存调度、云回退、用量统计 |
| Breeze TTS | 固定参考声合成 |
| Whisper ASR | 批量转写 |
| 本地 LLM | Gemma 等本地模型 |
| 设备管理 | `fleetService`：设备、配对、指令 |
| 提醒 | 到期提醒，自迭代服务定时驱动 |
| 新闻日报 | 每日领取一次，自迭代服务定时检查 |
| 视频摘要 | 按需 |
| 中继节点 | relayNode 连接 VPS Hub |

## ModelHub

- 登记模型权重（相对路径或环境变量），扫描显存与文件大小
- 按租约加载/卸载，支持 keep-alive、空闲释放、优先级
- 本地模型不可用时按配置回退云端
- 对外：健康检查、模型清单、租约与用量接口（路径见部署配置）

## 设备管理

- 设备登记、一次性配对码、能力表校验
- 指令队列：探测、shell、GUI、截图、媒体、通知、音频路由、定位、摄像头等
- 能力不在设备心跳里就拒绝下发，不假装成功

## 业务运行时

- `ServiceHost`：每进程一个服务，串行执行槽，HTTP JSON-RPC
- `schedule_tick`：领取到期提醒或当日日报；调度权可交给自迭代服务

## 配置

- 模型与密钥：部署根 `models/config/`
- 服务登记：`config/modelHub.server.json`（相对路径）
- 业务服务：`businessRuntime/config/serviceConfig.json`
- 机器相关路径只放在环境变量或部署环境文件

## 日志

统一写入生产根 `runtime/logs/`：`all.log` 汇总，`modelhub.log`、`business-*.log` 分服务。
路径解析与日志实现复用主库的公共模块。

## 安全

- 仓库不含权重、密钥、设备身份
- 权重路径用相对路径或环境变量
- 管理口只绑定回环，非回环必须独立鉴权
