# synlove-asr · 本地语音转写（faster-whisper）

把 faster-whisper 包成本地 HTTP 服务：**懒加载、闲置自动退出**、CUDA 显存不足自动转 CPU int8。

主项目：[missmissrain/wechat-ai-butler](https://github.com/missmissrain/wechat-ai-butler)。

## 模型与来源

| 模型 | 链接 | 说明 |
|---|---|---|
| small（默认） | https://huggingface.co/Systran/faster-whisper-small | 约 480MB，中文够用 |
| base | https://huggingface.co/Systran/faster-whisper-base | 更快、更糙 |
| tiny | https://huggingface.co/Systran/faster-whisper-tiny | 最省资源 |
| medium | https://huggingface.co/Systran/faster-whisper-medium | 更准、更吃显存 |
| large-v3 | https://huggingface.co/Systran/faster-whisper-large-v3 | 最准，需较大显存 |
| 国内镜像 | https://hf-mirror.com/Systran/faster-whisper-small | 已内置为默认 `HF_ENDPOINT` |

运行时由 `faster-whisper` 自动下载到本地缓存（`%LOCALAPPDATA%\huggingface\hub`），
也可以自己 `huggingface-cli download` 后设置 `HF_HOME`。

## 安装

```bat
pip install -r requirements.txt
:: 想用 GPU：
pip install torch --index-url https://download.pytorch.org/whl/cu121
```

## 用法

```bat
python tools\transcribe.py "D:\path\a.mp3" --language zh
python tools\transcribe.py "D:\path\a.mp4" --model medium
```

行为：端口没监听就后台拉起服务，等健康检查，然后 POST 转写；结果按分段打印，最后给全文。
服务闲置 180 秒自动退出（`--idle-seconds`）。

## HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 模型是否加载、设备、空闲显存 |
| POST | `/transcribe` | `{"path","language","vadFilter"}` → `{"text","segments","device","model"}` |
| POST | `/unload` | 立刻退出进程 |

服务参数：`--port 8081 --model small --device auto --idle-seconds 180`

## 设备选择

`--device auto`（默认）时按空闲显存决定：≥900MB 用 CUDA，否则 CPU int8。
CUDA 分配失败（OOM / cuda failed）会**自动卸载模型、改 CPU int8 重试一次**，
不会整轮失败——这条是一次真实 OOM 之后加的。

## 与视频理解的关系

主项目的 `videoDigest` 用它把视频音轨转成文本，再和关键帧一起喂给视觉模型。
单独用也完全可以：任何本地音视频文件都能转。

## 已知边界

- 只处理本地文件，不接受 URL（下载交给 yt-dlp 那一层）；
- 长音频按 faster-whisper 自带 VAD 切分，整段仍要一次性过模型，超长文件（>1 小时）会很慢；
- 中文术语/人名识别一般，专有名词建议在提示词里给候选。
