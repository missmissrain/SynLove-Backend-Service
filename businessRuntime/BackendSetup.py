"""后台部署初始化。参数--existing-node-config可复用服务器已有节点资料，只创建缺失配置，不安装依赖。"""

import argparse
import json
import shutil
from pathlib import Path


class BackendSetup:
    """建立独立业务运行配置与出站节点，模型服务沿用家庭服务器现有端口。"""

    def __init__(self, root=None):
        """root为businessRuntime目录；默认使用当前脚本所在目录。"""
        self.root = Path(root or Path(__file__).resolve().parent)

    def initialize_files(self, node_config=None):
        """node_config为可选既有节点配置路径；不会修改既有节点或覆盖业务数据库。"""
        for folder, stem in (("config", "serviceConfig"), ("newsDigest/config", "newsDigest"),
                             ("videoDigest/config", "videoDigest")):
            path = self.root / folder / (stem + ".json")
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(self.root / folder / (stem + ".example.json"), path)
        relay = self.root.parent / "relayNode"
        target = relay / "config" / "node.json"
        if not target.exists():
            value = json.loads((relay / "config" / "node.example.json").read_text(encoding="utf-8-sig"))
            if node_config:
                old = json.loads(Path(node_config).read_text(encoding="utf-8-sig"))
                value["hubUrl"] = old["hubUrl"]
                value["token"] = old.get("token", "${RELAY_TOKEN}")
                value["nodeId"] = (old.get("nodeId") or "synlove-home") + "-business"
            target.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"initialized": True, "config": str(self.root / "config" / "serviceConfig.json"),
                "relayConfig": str(target)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--existing-node-config")
    args = parser.parse_args()
    print(json.dumps(BackendSetup().initialize_files(args.existing_node_config), ensure_ascii=False))
