"""Gemma 筛选与改写：把新闻标题列表交给本机 llama-server，返回一条可推送的短消息。

职责：
- 用**硬约束提示词**（必须写主体/地点/数字、禁止空泛评价、限字数）让 7.5B 的 Gemma 也能稳定产出；
- 调用 OpenAI 兼容接口（本机 8080），失败重试一次；
- 输出超字数时按句号截断（不硬切半句），并打印提示便于调参。

注册参数（来自配置 gemma 段）：
    endpoint        默认 http://127.0.0.1:8080/v1/chat/completions
    model           默认 gemma-4-E4B-it-Q4_K_M
    timeoutSeconds  默认 240（冷启动要等模型从休眠状态重载，约 30 秒）
    maxChars        消息字数上限（由调用方传入，默认 300）
    maxItems        一条消息最多写几条（由调用方传入，默认 5）
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:  # 极老的解释器没有 reconfigure
    pass


class gemmaFilter:
    """用本机 Gemma 做"挑几条 + 改写成一条消息"。"""

    def __init__(self, gemma_config):
        """gemma_config：配置里的 gemma 段（endpoint/model/timeoutSeconds）。"""
        self.endpoint = gemma_config["endpoint"]
        self.model = gemma_config["model"]
        self.timeout = int(gemma_config.get("timeoutSeconds", 240))
        self.key_env = gemma_config.get("apiKeyEnv", "")
        from services.ServiceRegistry import ServiceRegistry
        from services.ServiceClient import ServiceClient
        self.client = ServiceClient(ServiceRegistry().get_service("llm"))

    def build_prompt(self, items, interest, max_items, max_chars):
        """拼提示词：关注画像 + 标题清单 + 硬约束 + 一个范例（防止退化成"分点复述标题"）。"""
        titles = "\n".join(f"- [{item.get('time', '')}] {item['title']}" for item in items)
        return f"""你是一个家庭助理，要发一条新闻简报给家里人。

关注方向：{interest}

这是新闻标题列表：
{titles}

要求：
1. 只挑 {max_items} 条以内真正符合关注方向的，其余全部丢掉；一条都不符合就只回复"没有值得说的"；
2. 第一行先写一句 15~25 字的概括：今天最值得留意的是什么；
3. 后面每条新闻单独一行，以"· "开头；每行必须写清"谁/哪个地方/做了什么"，能带数字就带数字（照抄，不要写"某地""有关消息"）；
4. 标题本身是疑问句、你又回答不了的，不要选它；不许用"有消息说""有人问""据说"开头；
5. 不允许出现"值得关注""挺热门的""很重要"这类空话，也不要编造标题里没有的细节；
6. 总共不超过 {max_chars} 字；
7. 只输出简报正文，不要编号、不要"以下是"。

范例（模仿这种结构和口气，不要照抄内容）：
今天值得留意的是政策落地和民生两件事。
· 武汉启动脑机接口数据集项目，覆盖医疗、工业等场景
· 郑州前三季度 GDP 11862 亿元，同比增长 4.9%
· 流感季比往年提前，有症状别硬扛，尽早去医院

请先在心里筛，然后直接输出简报。"""

    def run(self, items, interest, max_items=5, max_chars=300):
        """按业务字数返回消息；网络异常最多重试一次，HTTP 鉴权或参数错误直接上抛。"""
        prompt = self.build_prompt(items, interest, max_items, max_chars)
        last_error = None
        for attempt in (1, 2):
            started = time.time()
            try:
                text = self._request(prompt)
            except (urllib.error.URLError, TimeoutError, OSError) as error:
                last_error = error
                print(f"[筛选] 第 {attempt} 次请求失败：{error}", flush=True)
                continue
            elapsed = time.time() - started
            text = text.strip()
            if len(text) > max_chars:
                cut = text[:max_chars]
                index = max(cut.rfind("。"), cut.rfind("！"), cut.rfind("？"), cut.rfind("\n"))
                text = cut[: index + 1] if index >= 0 else cut
                print(f"[筛选] 输出 {len(text)} 字，已按句子截断到 {max_chars} 字以内", flush=True)
            print(f"[筛选] 完成：{len(text)} 字，用时 {elapsed:.1f}s", flush=True)
            return text
        raise RuntimeError(f"Gemma 调用失败：{last_error}")

    def _request(self, prompt):
        """POST 到 llama-server，返回 choices[0].message.content。"""
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
            "max_tokens": 400,
        }
        data = self.client.chat(payload)
        return data["choices"][0]["message"]["content"]
