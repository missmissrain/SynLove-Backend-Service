"""模型登记与权重清单。

职责：读取公开的 ModelHub 配置，扫描登记的权重路径，返回稳定的模型信息。
注册参数：root 为后台仓库根目录，config_path 为模型登记 JSON 路径。
默认参数均在配置文件中以中文注释字段名表达，代码不保存密钥。
"""

from __future__ import annotations

import glob
import json
import os
from pathlib import Path
from typing import Any


class ModelRegistry:
    """维护模型元数据和服务器上的权重文件清单。"""

    def __init__(self, root: str | os.PathLike[str], config_path: str | os.PathLike[str] | None = None):
        """root 为后台仓库根目录；config_path 为可选登记文件路径。"""
        self.root = Path(root).resolve()
        self.config_path = Path(config_path) if config_path else self.root / "config" / "modelHub.json"
        if not self.config_path.is_absolute():
            self.config_path = self.root / self.config_path
        self.config_path = self.config_path.resolve()
        self.data = self._read_config()

    def _read_config(self) -> dict[str, Any]:
        """无参数；读取登记配置，不存在时返回空模型表。"""
        if not self.config_path.exists():
            return {"version": 1, "models": []}
        return json.loads(self.config_path.read_text(encoding="utf-8-sig"))

    def reload(self) -> None:
        """无参数；重新读取登记文件，供管理接口热更新配置。"""
        self.data = self._read_config()

    def models(self) -> list[dict[str, Any]]:
        """无参数；返回登记的模型副本，避免调用者修改内部配置。"""
        return [dict(item) for item in self.data.get("models", []) if isinstance(item, dict)]

    def _expand_patterns(self, patterns: list[str]) -> list[Path]:
        """patterns 为权重路径或通配符；返回存在且去重后的文件路径。"""
        result: list[Path] = []
        seen: set[str] = set()
        for raw in patterns:
            path = Path(os.path.expandvars(str(raw)))
            if not path.is_absolute():
                path = self.root / path
            matches = glob.glob(str(path), recursive=True) if any(char in str(path) for char in "*?[") else [str(path)]
            for item in matches:
                candidate = Path(item)
                if not candidate.is_file():
                    continue
                key = str(candidate.resolve()).lower()
                if key not in seen:
                    seen.add(key)
                    result.append(candidate.resolve())
        return result

    def inventory(self) -> list[dict[str, Any]]:
        """无参数；扫描全部登记模型并返回权重大小、存在性和服务信息。"""
        output: list[dict[str, Any]] = []
        for item in self.models():
            files = self._expand_patterns(item.get("weightPaths", []))
            size = sum(path.stat().st_size for path in files)
            record = dict(item)
            record["weights"] = [
                {"path": str(path), "sizeBytes": path.stat().st_size}
                for path in files
            ]
            record["weightCount"] = len(files)
            record["weightBytes"] = size
            record["weightsReady"] = bool(files) and bool(item.get("weightPaths"))
            output.append(record)
        return output
