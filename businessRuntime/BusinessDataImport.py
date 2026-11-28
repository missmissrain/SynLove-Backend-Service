"""后台业务数据导入。参数--input为私人迁移目录；必须是冻结快照，只接收固定白名单文件，默认不覆盖已有业务。"""

import argparse
import hashlib
import json
import shutil
import sqlite3
from pathlib import Path
from contextlib import ExitStack, closing
from microServices.ServiceLease import ServiceLease


class BusinessDataImport:
    """导入在服务启动前执行，校验全部文件后再复制；收据支持本机验证数据是否迁对。"""

    ALLOWED = {"runtime/reminders.db", "runtime/newsDigest.db",
               "runtime/microServices/digest/schedule.sqlite", "newsDigest/config/newsDigest.json"}

    def __init__(self, root=None):
        """root为后台businessRuntime目录。"""
        self.root = Path(root or Path(__file__).resolve().parent)

    def import_data(self, source):
        """source为已经冻结的迁移目录；哈希不符、路径穿越或已有数据库一律中止。"""
        source = Path(source).resolve()
        config = self.root / "config" / "serviceConfig.json"
        if config.is_file():
            services = json.loads(config.read_text(encoding="utf-8-sig")).get("services", {})
            for name, expected in (("reminders", "runtime/reminders.db"), ("digest", "runtime/newsDigest.db")):
                value = services.get(name, {})
                if value.get("databasePath", expected).replace("\\", "/") != expected:
                    raise ValueError("自定义数据库路径须单独迁移，标准导入不会猜测路径")
                if value.get("dataDir", "runtime/microServices/" + name).replace("\\", "/") != "runtime/microServices/" + name:
                    raise ValueError("自定义状态目录须单独核对")
        data = json.loads((source / "manifest.json").read_text(encoding="utf-8-sig"))
        if data.get("version") != 1 or data.get("frozen") is not True:
            raise ValueError("需要冻结后的正式迁移快照")
        files = data["files"]
        if not isinstance(files, dict) or not files or not set(files).issubset(self.ALLOWED):
            raise ValueError("迁移文件清单无效")
        ident = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
        for name, digest in files.items():
            path = (source / name).resolve()
            if source not in path.parents or path.is_symlink():
                raise ValueError("迁移路径无效")
            if not path.is_file() or path.stat().st_size > 256 * 1024 * 1024:
                raise ValueError("迁移文件不存在或过大")
            if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise ValueError("迁移文件校验失败")
            if path.suffix in (".sqlite", ".db"):
                with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
                    if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise ValueError("数据库损坏")
                if (self.root / name).exists():
                    raise ValueError("目标已有数据库，拒绝覆盖")
        with ExitStack() as stack:
            locks = [self.root / "runtime" / "supervisor.lock"]
            locks += [self.root / "runtime" / "microServices" / name / "service.lock" for
                      name in ("reminders", "digest", "video", "crawler")]
            for lock in locks:
                lease = ServiceLease(lock).acquire()
                stack.callback(lease.close)
            # 上面的检查仅是预检；持锁后再次防止并发导入覆盖。
            for name in files:
                if name.startswith("runtime/") and (self.root / name).exists():
                    raise ValueError("目标数据库已被其他导入创建")
            marker = self.root / "runtime" / "migrationInProgress.json"
            if marker.exists():
                raise ValueError("存在未完成导入，必须先核对和恢复数据")
            marker.write_text(json.dumps({"migrationId": ident}), encoding="utf-8")
            for name in files:
                target = self.root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    shutil.copyfile(target, target.with_suffix(".beforeMigration.json"))
                shutil.copyfile(source / name, target)
            (self.root / "runtime" / "migrationReceipt.json").write_text(json.dumps(
                {"migrationId": ident, "files": list(files)}, ensure_ascii=False), encoding="utf-8")
            marker.unlink()
        return {"imported": True, "migrationId": ident, "files": len(files)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    args = parser.parse_args()
    print(json.dumps(BusinessDataImport().import_data(args.input), ensure_ascii=False))
