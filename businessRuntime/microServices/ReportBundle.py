"""视频报表跨机打包。ReportBundle(root) 仅收集root内Markdown与所引用图片，默认上限256MiB。"""

import re
import tempfile
import zipfile
from pathlib import Path


class ReportBundle:
    """把服务器绝对图片路径改为包内相对路径，不收集任意外部文件。"""

    def __init__(self, root):
        """root为视频服务允许的输出根目录；图片路径必须落在这个目录内。"""
        self.root = Path(root).resolve()

    def publish(self, report, store):
        """report为Markdown路径，store为ArtifactStore；返回ZIP文件ID。"""
        report = Path(report).resolve()
        text, files = report.read_text(encoding="utf-8"), {}
        total = report.stat().st_size

        def rewrite(match):
            """match为Markdown图片引用；改为固定包内文件名，拒绝越界或缺失图片。"""
            nonlocal total
            value = match[2].strip().strip("<>")
            path = Path(value)
            if not path.is_absolute():
                path = report.parent / path
            path = path.resolve()
            if not path.is_relative_to(self.root) or not path.is_file():
                raise ValueError("视频报表图片不在服务输出目录内或已丢失")
            name = "images/" + str(len(files) + 1) + path.suffix.lower()
            files[name] = path
            total += path.stat().st_size
            if total > store.MAX_SIZE:
                raise ValueError("视频报表附件超过256MiB")
            return "![" + match[1] + "](" + name + ")"

        text = re.sub(r"!\[([^\]]*)\]\(([^)\r\n]+)\)", rewrite, text)
        with tempfile.TemporaryDirectory(dir=store.root) as folder:
            path = Path(folder) / "report.zip"
            with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("report.md", text)
                for name, image in files.items():
                    archive.write(image, name)
            return store.publish(path, store.root)
