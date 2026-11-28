"""功能：跨库统一的生产根与模型配置定位，替代散落的绝对路径与相对猜测。

注册参数：无。解析顺序为 环境变量 SYNLOVE_MODEL_CONFIG_DIR → 生产根/models/config。
生产根通过“包含 repos 与 models 的最近祖先目录”判定，因此各库可独立部署到别的盘符。
找不到即抛错，不回退到旧目录，避免悄悄读错配置。
"""

import os
from pathlib import Path


def production_root(start):
    """start 为任一库内路径；返回包含 repos 与 models 的生产根目录。"""
    path = Path(start).resolve()
    for parent in [path, *path.parents]:
        if (parent / "repos").is_dir() and (parent / "models").is_dir():
            return parent
    raise FileNotFoundError("无法从 %s 定位生产根目录（需同时包含 repos 与 models）" % path)


def model_config_dir(root):
    """root 为库根或生产根；返回模型配置目录。"""
    override = os.environ.get("SYNLOVE_MODEL_CONFIG_DIR")
    if override:
        return Path(override)
    return production_root(root) / "models" / "config"


def model_config(root, name):
    """root 为库根、name 为配置文件名；返回存在的配置文件路径，缺失即报错。"""
    path = model_config_dir(root) / name
    if not path.is_file():
        raise FileNotFoundError("模型配置不存在：%s" % path)
    return path
