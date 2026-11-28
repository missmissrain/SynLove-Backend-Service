# 提交容量准备：CommitCapacity在全局锁内用未触碰的虚拟提交测试系统容量，允许Windows提前扩展自动页面文件。
# 注册参数无；仅在显式配置prepareCommitCapacity时调用，不改变注册表、不触碰物理页，结束时释放全部探测地址。
import ctypes
import os
import shutil
import time
from .MemoryInspector import MemoryInspector


class CommitCapacity:
    """提前验证系统提交额度，避免自动页面文件尚未扩展导致大模型加载被误拒绝。"""

    def prepare_capacity(self, need_mb, seconds=60, reserve_mb=2048):
        """need_mb为目标空闲提交MiB，seconds为最长准备时间，reserve_mb为始终保留的余量。"""
        if os.name != "nt" or ctypes.sizeof(ctypes.c_void_p) < 8:
            return {"ok": False, "error": "容量准备要求64位Windows"}
        mem = MemoryInspector().read_memory()
        if not mem.get("ok"):
            return mem
        if mem["commitFreeMiB"] >= need_mb:
            return {"ok": True, "allocations": 0, "memory": mem}
        free_disk = shutil.disk_usage(os.environ.get("SystemDrive", "C:") + "/").free / 1024 ** 2
        growth = need_mb - mem["commitFreeMiB"]
        if free_disk < growth + 10240:
            return {"ok": False, "error": "系统盘空间不足以准备提交额度并保留10GiB"}
        lib = ctypes.WinDLL("kernel32", use_last_error=True)
        lib.VirtualAlloc.argtypes = [ctypes.c_void_p, ctypes.c_size_t,
                                     ctypes.c_uint32, ctypes.c_uint32]
        lib.VirtualAlloc.restype = ctypes.c_void_p
        lib.VirtualFree.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint32]
        addresses = []
        held = 0
        deadline = time.monotonic() + seconds
        baseline = mem["commitUsedMiB"]
        try:
            while time.monotonic() < deadline:
                mem = MemoryInspector().read_memory()
                if not mem.get("ok"):
                    break
                # 计算释放探测提交后可供模型使用的容量，不能把自身预留当成业务占用。
                if mem["commitFreeMiB"] + held >= need_mb:
                    break
                if held > need_mb or mem["commitUsedMiB"] - held > baseline + 4096:
                    break
                if mem["commitFreeMiB"] < reserve_mb + 256:
                    time.sleep(0.25)
                    continue
                ptr = lib.VirtualAlloc(None, 256 * 1024 ** 2, 0x3000, 0x04)
                if not ptr:
                    break
                addresses.append(ptr)
                held += 256
                time.sleep(0.05)
        finally:
            for ptr in addresses:
                lib.VirtualFree(ptr, 0, 0x8000)
        mem = MemoryInspector().read_memory()
        return {"ok": bool(mem.get("ok") and mem["commitFreeMiB"] >= need_mb),
                "allocations": len(addresses), "preparedMiB": held, "memory": mem}
