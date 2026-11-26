"""RSSHub请求头兼容补丁。参数--root为已有RSSHub目录；不安装依赖，仅放宽失效的默认Chrome版本预设。"""
import argparse
import json
import re
import shutil
import time
from pathlib import Path


class RepairRsshubHeaders:
    """保留Chrome/Windows身份，移除过期版本约束；源码和构建文件同时备份并修改。"""

    def repair_headers(self, root):
        """root为操作者明确指定的RSSHub目录；仅接受已知生成函数，未知构建直接停止。"""
        root = Path(root).resolve()
        source = root / "lib/utils/header-generator.ts"
        builds = list((root / "dist").glob("header-generator-*.mjs"))
        if not source.is_file() or len(builds) != 1:
            raise ValueError("RSSHub源码或生成器构建不唯一，未修改")
        marker = "/* SynLove header preset compatibility */"
        preset = marker + "{browsers:['chrome'],operatingSystems:['windows']}"
        changes = {}
        for path in [source, builds[0]]:
            text = path.read_text(encoding="utf-8-sig")
            if marker in text:
                continue
            if path == source:
                old = "preset: Partial<HeaderGeneratorOptions> = PRESETS.MODERN_MACOS_CHROME"
                if text.count(old) != 1:
                    raise ValueError("RSSHub源生成函数不匹配")
                new = text.replace(old, "preset: Partial<HeaderGeneratorOptions> = " + preset)
            else:
                pattern = r"(\w+)=\((\w+)=(\w+)\.MODERN_MACOS_CHROME\)=>"
                new, count = re.subn(pattern, lambda m: m[1] + "=(" + m[2] + "=" + preset + ")=>", text)
                if count != 1:
                    raise ValueError("RSSHub构建生成函数不匹配")
            changes[path] = new
        if not changes:
            return {"changed": False, "alreadyApplied": True}
        backup = root / "备份" / ("SynLoveRsshubHeadersv1.0-" + time.strftime("%Y%m%d%H%M%S"))
        backup.mkdir(parents=True)
        for path, text in changes.items():
            dest = backup / path.relative_to(root)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest)
            path.write_text(text, encoding="utf-8")
        (backup / "version.json").write_text(json.dumps({
            "version": "1.0", "time": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "files": [str(p.relative_to(root)) for p in changes]}), encoding="utf-8")
        (backup / "版本说明.md").write_text(
            "只修正默认请求头预设与已有依赖数据不匹配的问题。保留原源码和构建供回滚；重建RSSHub后需重新核验。",
            encoding="utf-8")
        return {"changed": True, "files": len(changes)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    args = parser.parse_args()
    print(json.dumps(RepairRsshubHeaders().repair_headers(args.root)))
