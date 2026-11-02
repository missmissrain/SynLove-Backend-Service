# 后台业务部署

## 运行边界

个人实例负责对话、微信/QQ连接、个人记忆、消息队列、编码连接器及本机观测。

家庭台式机运行四项后台功能：

| 功能 | 内部兼容ID | 端口 | 出站能力 |
| --- | --- | --- | --- |
| 爬虫中心（新闻采集、RSSHub、摘要、每日新闻） | digest | 8792 | crawlerCenter |
| 网页收集（浏览器 + Gemma） | crawler | 8796 | webCollection |
| 视频理解 | video | 8793 | video |
| 定时任务 | reminders | 8791 | scheduler |

保留旧ID和MCP工具名是为了兼容上下文和调用方，不是保留第二套新闻服务。RSSHub由批量启动器作为爬虫中心的内部依赖管理，不在GUI里重复显示。

## 先准备家庭服务器

1. 使用现有 Python 环境、Node 24、ffmpeg/ffprobe、RSSHub 构建，不重复安装依赖。浏览器功能需要 Python Playwright 1.48+ 与 Chromium；视频功能需要 Pillow、yt-dlp。缺失项会让对应功能灰灯，不会拖垮提醒服务。
2. 模型仍在独立 API：默认 Gemma 8080、ASR 8081、TTS 7862。业务进程不拉起模型，不与模型服务抢生命周期。
3. 首次运行 `python businessRuntime/BackendSetup.py --existing-node-config <服务器现有节点配置路径>`。它只创建缺失配置；已有配置不会覆盖。既有节点资料只复制 Hub 地址及令牌，生成不同的业务节点 ID。
4. 检查 `businessRuntime/config/serviceConfig.json` 和 `newsDigest/config/newsDigest.json`。使用现有端口和真实模型 ID。RSSHub已有独立守护时不传 `-RssHubEntry`；没有守护时用该参数交给批量启动器，避免重复启动。
5. `relayNode` 依赖 `ws`；构建需要 TypeScript 和 Node 类型。若已安装依赖，直接 `npm run build`；也可把已有依赖目录作为受控的部署依赖复用。首次无依赖的全新服务器才按 `package.json` 安装。
6. 使用现有受信任 CA 设置 `NODE_EXTRA_CA_CERTS`。令牌可设置 `RELAY_TOKEN`；不要把 IP、令牌或证书私钥提交到仓库。

## 迁移旧日程

正式冻结前先把新后台代码和依赖准备好。冻结后停止旧四项业务，暂时显示不可用是正常迁移窗口；不要同时启动新旧两个调度器。

本机个人实例根目录执行：

```powershell
.\.venv\Scripts\python.exe tools/BusinessDataExport.py --output runtime/privateMigration/<新目录> --freeze-local
```

该命令先保存私人回滚配置并禁用旧四项业务的自动补拉，再停止旧服务，用SQLite备份接口导出提醒、新闻去重库、每日计划与新闻偏好。它不迁移聊天、账号、密钥、图谱或个人消息状态。输出目录包含私人数据，单独安全传给家庭服务器，不能提交 GitHub。

若旧新闻仍由Windows计划任务或独立 `reminderService.py --loop`运行，必须先停用同一项目的旧计划/进程。导出命令会执行既有旧调度预检，发现活跃旧调度时拒绝冻结。

家庭服务器在四项业务首次启动前执行：

```powershell
python businessRuntime/BusinessDataImport.py --input <收到的迁移目录>
```

只接受冻结快照，校验文件白名单、SHA-256及SQLite完整性，不覆盖已有数据库。导入进行中会留下标记，异常中断后禁止启动调度，必须先核对恢复；不能删除标记后盲目继续。标准迁移路径使用示例配置默认的数据目录，自定义数据库路径应先单独核对。

启动：

```powershell
powershell -File deploy/StartBusiness.ps1 -Python <现有Python> -RssHubEntry <已构建RSSHub入口>
```

已由其他服务管理RSSHub时省略其参数；不用VPS、只走直连接口时加 `-DirectOnly`。默认只监听回环地址，由出站节点访问；不要直接暴露到公网。

本机验证及切换：

```powershell
.\.venv\Scripts\python.exe tools/BusinessServiceSetup.py verify
.\.venv\Scripts\python.exe tools/BusinessServiceSetup.py configure --migration-confirmed --migration-id <导出或导入返回的migrationId>
```

四项健康必须真实通过，收据必须一致。全部通过后才写远端地址并禁用本机RSSHub。已有TTS/ASR/Gemma、QQ/微信及其状态保持原职责。随后通过项目原重启入口加载新配置；不要从备份或旧工作区启动第二个个人实例。

## 结果如何回来

远端提醒/新闻先保存 `runtime/notifications.sqlite`，不持有个人消息平台密钥。本机控制台每30秒拉取结果，原子落盘到当前 `DSH_HOME/backendNotifications`，再确认远端。现有QQ连接器读取该收件箱，用稳定ID加入原有QQ队列，成功入队才删除收件文件。已确认收据在远端和本机均保留。

本机没有运行时，结果留在服务器；控制台恢复后继续接收。本机没有配置QQ接收人时不确认远端通知。最终发送受平台额度和网络影响，不能保证平台级恰好一次发送；网络结果未知可能重复。不要让另一个程序直接发送同一份通知。

本版一个业务部署对应一个个人实例，不共享个人日程或通知库。不应把多个个人实例连到同一业务能力并当成已实现多租户。

## 心跳和灯色

本机可用为浅绿，远程可用为青绿，协议或依赖不可用为灰色。判断依据是功能接口与依赖可用性，不是有没有正在执行任务。

浏览器心跳建立无网络页面并执行协议请求；模型和ASR探活不运行实际推理。新闻中心还检查RSSHub。视频检查ffmpeg、ffprobe、yt-dlp、Pillow、ASR和模型。完整任务成功仍需真实内容验收。

## 隔离验证

测试复用现有依赖，随机端口和临时状态，不发送真实QQ/微信消息：

```powershell
<现有Python> businessRuntime/tests/BusinessServicesTests.py -v
# 在Personal-Agent仓库执行，配套relayNode先构建；Hub测试复用既有消息中转项目。
node tests/BusinessRelayTests.mjs
```
