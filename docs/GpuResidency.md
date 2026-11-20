# 模型保温与显存让位

TTS 和 LLM 的 HTTP 服务常驻，模型在首次有效推理时按需加载。一次推理完成后保温十分钟，同一模型的后续请求复用已加载的模型；空闲十分钟后释放模型，HTTP 端口继续可用。

## 调度规则

- 默认空闲期限为 600 秒，从生成处理完成时开始计时。健康检查和模型列表查询不刷新期限，也不触发权重加载。
- 在小显存共享部署中，TTS 与 LLM 通过同一操作系统文件锁串行执行加载及推理。前一个任务完成后，另一个请求取得资格。
- 待加载模型显存不足时，立即请求另一个空闲模型卸载，不必等剩余保温时间。
- 能否卸载以模型执行锁为准，不依赖瞬时 GPU 利用率。CPU 预处理、等待数据等阶段即使利用率为零，也不能打断当前请求。
- TTS 释放运行时引用并清理 CUDA 缓存；LLM 结束其所属的 llama-server 子进程。HTTP 服务与守护程序继续运行。
- 不结束其他 GPU 应用。若其他程序占用导致显存仍不足，请求有界等待并返回失败。
- 进程异常退出后，操作系统会释放共享锁，避免留下永不过期的忙碌标记。

## 配置

后台仓库 `config/gpuResidency.json`：

```json
{
  "keepAliveSeconds": 600,
  "waitSeconds": 1200,
  "memoryWaitSeconds": 180,
  "services": {
    "tts": {"port": 7862, "tokenEnv": "SYNLOVE_TTS_API_KEY", "memoryMarginMb": 1200},
    "llm": {"port": 8080, "tokenEnv": "SYNLOVE_LLM_API_KEY"}
  }
}
```

配置优先于旧启动脚本中的独立 `--sleep-idle` 和 `--idle-seconds`。LLM 的内置休眠定时器关闭，由统一生命周期管理保温。原量化、模型权重、CPU/GPU 混合放置及生成参数保持原配置。

`memoryMarginMb` 是INT8加载前显存预算之外的预留，默认1500 MiB。6GB服务器实测软卸载后CUDA上下文仍会占少量显存，因此其配置采用1200 MiB，配合现有3.6 GiB权重放置预算；它不修改权重放置，只用于加载前的可用显存检查。

共享锁位于后台仓库 `runtime/gpuResidency/model.lock`。两个服务必须使用同一个后台根目录。锁文件存在不代表锁仍被占用，不要手动删除正在使用的锁文件。

## 诊断

- TTS `GET /health`：`loaded`、`busy`、`keepAliveSeconds`。
- LLM `GET /health`：`loaded`、`busy`、`idleSeconds`、`keepAliveSeconds`。
- LLM `GET /internal/gpu/status`：仅回环客户端。
- 两者 `POST /internal/gpu/unload`：仅回环客户端并使用对应服务的 Bearer 鉴权；忙碌返回 409，空闲卸载成功返回 200。

这些诊断接口不应作为公网中转的独立能力发布。模型卸载后，CUDA 驱动上下文、显示桌面以及其他程序仍可占少量或较多显存；“卸载模型”不代表整张显卡显存必须为零。

LLM 模型进程卸载后，其内部 KV 缓存随之释放；调用方仍需在每次 API 请求中提供对话上下文。

## 隔离验证

```text
python -X utf8 -m unittest discover -s tests -p GpuResidencyTests.py -v
```

使用假模型和真实跨进程文件锁验证保温、到期卸载、执行保护、鉴权及异常释放，不调用真实模型。
