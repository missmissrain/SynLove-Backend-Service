# 层流水线实验（layerPipelineExp）

验证一个想法：**把模型按层交替放在 GPU 和 CPU 上，CPU 计算的时候让 GPU 异步搬下一组权重，
用「重叠」把搬运时间藏起来。**

## 结论（实测，RTX 4070 Laptop 8G / DDR5 / PCIe 4.0 x8）

| 模式 | token/s | 每 token 耗时 | 每 token 搬运 |
| --- | --- | --- | --- |
| partial（超出部分留 CPU，`-ngl` 做法） | **14.96** | 66.8 ms | 0 MB |
| pipeline（交替 + 双缓冲，本实验方案） | 12.79 | 78.2 ms | 768 MB |
| serial（朴素逐层载入） | 1.99 | 503.6 ms | 768 MB |

- **流水线 vs 朴素逐层载入：6.44×** —— 说明「CPU 算的时候 GPU 在搬」确实有效，重叠是实打实的；
- **流水线 vs 直接留 CPU：0.86×** —— 还是慢了 14%，因为每块搬运 18ms、CPU 算只要 8ms，
  **搬运比计算贵 2.2 倍，重叠得再完美也补不回这个差价**。

数值校验：pipeline 与 partial 输出的最大误差 5.9e-15，调度顺序没错。

## 关键常数（决定一切）

| 常数 | 实测值 |
| --- | --- |
| PCIe H2D（pinned） | 10.2~10.5 GB/s |
| PCIe H2D（pageable） | 7.5 GB/s |
| 单块 192MB 的搬运时间 | 17.9 ms |
| 单块在 CPU 上的计算时间 | 8.0 ms |
| 单块在 GPU 上的计算时间 | 0.4 ms（fp16） |

**搬运/计算 = 2.2×**，这个比值就是流水线的天花板：只要它 > 1，把权重搬上 GPU 回本不了。

## 什么时候这个方案才真的赚

1. **模型大到内存也放不下，必须从磁盘流式**（AirLLM 的场景）。
   此时朴素做法是「读 55ms + 算 8ms」，流水线能把算藏进读里 → 约 1.15×。
   收益不大，但那是唯一的路。
2. **prompt 预填充阶段**（一次处理几百个 token）。此时 GPU 的计算量被放大几十倍，
   搬运相对来说不贵了，把层搬上 GPU 才开始划算。
   逐 token 生成阶段（batch=1、每 token 只算一次）永远是内存带宽受限，搬上去纯亏。

## 复现

```bat
set PYTHONUTF8=1
D:\py39\python.exe pipelineBench.py --blocks 12 --budget-gb 1.2 --tokens 8
D:\py39\python.exe pipelineBench.py --blocks 12 --budget-gb 1.2 --tokens 8 --group 2
```

`--group 2` 就是「头两层 GPU、后两层 CPU」的交替粒度，实测结论与 group=1 一致（6.19× / 0.84×）。

## 文件

| 文件 | 作用 |
| --- | --- |
| `layerPipeline.py` | 调度器：计划（谁常驻、谁交替）+ 四种执行模式 + 双缓冲预取 + 数值校验 |
| `syntheticStack.py` | 合成模型：和 Gemma E4B 同尺寸的 MLP 块，方便控制参数量 |
| `pipelineBench.py` | 实验入口：量常数 → 校验 → 四种模式计时对比 |

## 注意

- 权重必须 `pin_memory()` 才能真异步；不钉住的话 `non_blocking=True` 是假的。
- 只钉「要上 GPU」的块，CPU 组的块保持普通内存——钉住的内存给 CPU 用会更慢，会污染 CPU 基线。
- 实验统一用 fp32：PyTorch 的 CPU fp16 内核未优化（实测 49ms/块，比 fp32 慢 5 倍），
  拿它当基线会把 CPU 说得一无是处。真实部署里 llama.cpp 用 int4/int8 内核，CPU 侧就是带宽受限。
