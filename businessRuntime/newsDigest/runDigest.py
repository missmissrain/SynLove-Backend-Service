"""新闻摘要主编排：采集 → 去重 → Gemma 筛选改写 → QQ 主动推送 → 落日志。

职责：
- 串起 sources/store/filter/push 四个模块，单次运行走完整条链路；
- 每一步都打印步骤名、条数与耗时（前台 cmd 可见）；
- 有"新内容才推"，没有新内容就跳过（不打扰）；`--dry-run` 只打印不发送；
- 结果追加到 runtime/news-digest.log，返回码 0=正常（含"无新内容"），1=失败。

用法：
    python newsDigest\\runDigest.py                # 正常跑一次（有新的才推）
    python newsDigest\\runDigest.py --dry-run      # 只生成不推送（调试用）
    python newsDigest\\runDigest.py --limit=80     # 临时改采集条数
"""

import sys
import os
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:  # 极老的解释器没有 reconfigure
    pass

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
if __name__ == "__main__" and os.environ.get("SYNLOVE_SERVICE_WORKER") != "1":
    from microServices.ServiceCli import ServiceCli
    sys.exit(ServiceCli("digest", ROOT).run(sys.argv[1:]))
sys.path.insert(0, str(HERE))

from config.configLoader import configLoader        # noqa: E402
from sources.browserSource import browserSource    # noqa: E402
from sources.rsshubSource import rsshubSource      # noqa: E402
from sources.hotSource import hotSource            # noqa: E402
from store.newsStore import newsStore              # noqa: E402
from filter.gemmaFilter import gemmaFilter         # noqa: E402
from push.qqPush import qqPush                      # noqa: E402

LOG_PATH = Path(os.environ.get("SYNLOVE_DIGEST_LOG") or ROOT / "runtime" / "news-digest.log")


def log(message):
    """把一行带时间戳的消息同时打印到前台并追加到日志文件。"""
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] {message}"
    print(line, flush=True)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def _format_hot(items, limit=8):
    """把热点列表格式化成一段短文本（最多 limit 条）。"""
    lines = [f"· {item['source']}｜{item['title']}" for item in items[:limit]]
    return "今日热点：\n" + "\n".join(lines) if lines else ""


def main():
    """主流程：返回进程退出码。"""
    started = time.time()
    config = configLoader(ROOT)
    if not config.get("enabled", True):
        log("配置 enabled=false，本次不跑")
        return 0

    dry_run = "--dry-run" in sys.argv
    limit_override = next((arg.split("=", 1)[1] for arg in sys.argv if arg.startswith("--limit=")), None)
    browser = config.get("browser")
    chrome = browserSource(ROOT, browser) if browser else None
    rsshub = rsshubSource(config.get("rsshubBase") or "http://127.0.0.1:1200")
    store = newsStore(Path(os.environ.get("SYNLOVE_DIGEST_DB") or ROOT / "runtime" / "newsDigest.db"))
    try:
        total_fresh = []
        for item_source in config.get("sources", []):
            limit = int(limit_override or item_source.get("limit", 40))
            log(f"源「{item_source['name']}」开始采集（limit={limit}）")
            if item_source.get("type") == "rsshub":
                rows = rsshub.fetch(item_source.get("path") or "/", limit)
            else:
                if chrome is None:
                    raise RuntimeError("配置里没有 browser 段，无法用浏览器源")
                rows = chrome.fetch(item_source["url"], limit)
            # 只算不登记：要等推送**成功**后才写库，否则推失败这批新闻就永远丢了
            fresh = store.take_new(rows, record=False)
            log(f"源「{item_source['name']}」共 {len(rows)} 条，其中新出现 {len(fresh)} 条")
            total_fresh.extend(fresh)

        hot_cfg = config.get("hot") or {}
        hot_items = []
        if hot_cfg.get("enabled", True):
            collector = hotSource(
                config.get("rsshubBase") or "http://127.0.0.1:1200",
                hot_cfg.get("maxWeibo", 3),
                hot_cfg.get("maxBilibili", 2),
                hot_cfg.get("maxRsshub", 2),
            )
            hot_items = collector.fetch()
            for err in collector.errors:
                log("热点来源失败：" + err)
            log(f"热点共 {len(hot_items)} 条")
        hot_text = _format_hot(hot_items)

        if not total_fresh and not hot_text:
            log("没有新内容，跳过推送")
            return 0

        title = ""
        if total_fresh:
            title = gemmaFilter(config.get("gemma")).run(
                total_fresh,
                config.get("interest"),
                int(config.get("maxItems", 3)),
                int(config.get("maxChars", 120)),
            )
            if "没有值得说的" in title:
                log("模型判断没有值得推送的新闻（热点仍会推）")
                title = ""
        message = "\n\n".join(part for part in (title.strip(), hot_text) if part)
        log(f"生成消息（{len(message)} 字）：{message}")

        if dry_run:
            log("--dry-run：不发送")
            return 0
        if not config.get("push").get("enabled", True):
            log("新闻推送通道未启用，本轮仅生成预览")
            return 0

        pusher = qqPush()
        result = pusher.push(message)
        store.record_push("notifications", result["ok"], message, result["detail"])
        if result["ok"]:
            # 推送成功才登记 + 标记；失败什么都不写，下一轮这批新闻还能再推一次
            store.record_new(total_fresh)
            store.mark_pushed(total_fresh)
        log(f"推送结果：{result}")
        log(f"总用时 {time.time() - started:.1f}s，累计 {store.stats()}")
        return 0 if result["ok"] else 1
    except Exception as error:  # noqa: BLE001 —— 顶层兜底，保证日志里有原因
        log(f"运行失败：{type(error).__name__}: {error}")
        return 1
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())
