"""跨机器文件交换。ArtifactStore(root) 只允许访问随机ID下文件，默认上限256MiB。"""

import re
import uuid
from pathlib import Path


class ArtifactStore:
    """上传与结果文件都使用不透明ID，禁止任意路径读取及路径穿越。"""

    MAX_SIZE = 256 * 1024 * 1024

    def __init__(self, root):
        """root为服务专属文件目录；自动创建，不读取其他目录。"""
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def resolve(self, key):
        """key为32位十六进制ID及可选安全后缀；返回已校验的文件路径。"""
        if not re.fullmatch(r"[a-f0-9]{32}(?:\.[a-zA-Z0-9]{1,10})?", key):
            raise ValueError("非法文件ID")
        path = self.root / key
        if path.is_symlink() or path.resolve().parent != self.root:
            raise ValueError("禁止外部文件")
        return path

    def receive(self, source, size, suffix=""):
        """source为输入流，size为声明长度；只接收有界文件并返回ID。"""
        if not 0 < size <= self.MAX_SIZE:
            raise ValueError("文件大小必须在1字节至256MiB内")
        suffix = suffix if re.fullmatch(r"\.[a-zA-Z0-9]{1,10}", suffix) else ".bin"
        key = uuid.uuid4().hex + suffix
        path = self.resolve(key)
        try:
            with path.open("xb") as output:
                left = size
                while left:
                    data = source.read(min(left, 1024 * 1024))
                    if not data:
                        raise ValueError("文件传输提前断开")
                    output.write(data)
                    left -= len(data)
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return key

    def publish(self, path, allowed):
        """path为结果文件；allowed为允许发布的根目录，拒绝其他位置和过大文件。"""
        path = Path(path).resolve()
        base = Path(allowed).resolve()
        if not path.is_relative_to(base) or not path.is_file():
            raise ValueError("结果文件不在允许的输出目录")
        with path.open("rb") as source:
            return self.receive(source, path.stat().st_size, path.suffix)

    def expire_files(self, cutoff):
        """cutoff为缓存保留时间戳；只删除本存储内过期随机ID文件，不递归、不处理业务原文件。"""
        for path in self.root.iterdir():
            if (re.fullmatch(r"[a-f0-9]{32}(?:\.[a-zA-Z0-9]{1,10})?", path.name)
                    and not path.is_symlink() and path.is_file() and path.stat().st_mtime < cutoff):
                path.unlink()
