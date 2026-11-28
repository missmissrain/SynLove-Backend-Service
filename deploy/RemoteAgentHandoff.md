# 给家庭服务器Agent的部署任务

请部署当前 `SynLove-Backend-Service` 中新增的四项后台功能。现有VPS只转发，业务实际运行在这台家庭台式机；不要把业务部署到VPS，也不要改动现有chat/tts/asr节点。

先阅读本目录 `BusinessDeployment.md`。复用服务器已有Python、Node、RSSHub、模型API和依赖，不重装已有依赖，不下载模型。检查并报告缺少的浏览器、视频依赖；不要把“端口监听”当作功能验证。

请完成：

1. 同步本仓库当前代码。在现有Python中检查Playwright+Chromium、Pillow、yt-dlp、ffmpeg/ffprobe；确认Gemma和ASR的真实端点及模型ID。
2. 运行 `businessRuntime/BackendSetup.py --existing-node-config <当前运行的节点配置>`，只复用真实Hub地址和有效鉴权。检查新业务节点ID与旧模型节点不同。正确设置现有可信CA，不关闭TLS校验。
3. 使用已有依赖构建 `relayNode`，不重装环境。服务器已有RSSHub守护时复用；否则把已有RSSHub构建入口交给 `deploy/StartBusiness.ps1` 批量管理，不重复启动。
4. 旧日程必须从本机冻结快照迁移。收到私人迁移目录后，在首次启动业务前运行 `BusinessDataImport.py`；校验迁移收据和日程，不覆盖未知数据库。未收到正式快照时先准备环境，不能开启同一用户的第二套新闻或提醒调度。
5. 启动四项业务与出站节点，确认Hub注册 `crawlerCenter/webCollection/video/scheduler`。逐项验证健康身份和依赖；用一条测试新闻、一个小视频、一个公开网页及一次测试提醒验证实际结果。测试不使用私人聊天，不批量调用真实模型。
6. 后台只保存业务结果，不写QQ/微信密钥、不新建消息消费者。结果由本机既有通道接收并发送。
7. 汇报启动入口、进程、四项协议检查、实际功能证据、迁移ID及缺失项；地址、令牌和私人数据只留本机，不提交GitHub。实际通过后再由个人实例运行 `BusinessServiceSetup.py configure --migration-confirmed --migration-id <收据>` 切换。

尚未验证的功能要明确写“未验证”，不能把API测试或模型注册当成真实视频/网页处理成功。
