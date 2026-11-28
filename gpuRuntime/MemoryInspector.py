# 内存观测：MemoryInspector读取Windows物理内存和实际系统提交余量，不依赖Torch或新安装包。
# 注册参数无；单位统一为MiB，接口失败明确返回ok=false，不能当作资源充足。
import ctypes
import os


class MemoryInspector(ctypes.Structure):
    """映射系统PERFORMANCE_INFORMATION，同时提供物理内存与页面文件提交预算。"""

    _fields_ = [
        ("cb", ctypes.c_uint32),
        ("CommitTotal", ctypes.c_size_t), ("CommitLimit", ctypes.c_size_t),
        ("CommitPeak", ctypes.c_size_t), ("PhysicalTotal", ctypes.c_size_t),
        ("PhysicalAvailable", ctypes.c_size_t), ("SystemCache", ctypes.c_size_t),
        ("KernelTotal", ctypes.c_size_t), ("KernelPaged", ctypes.c_size_t),
        ("KernelNonpaged", ctypes.c_size_t), ("PageSize", ctypes.c_size_t),
        ("HandleCount", ctypes.c_uint32), ("ProcessCount", ctypes.c_uint32),
        ("ThreadCount", ctypes.c_uint32),
    ]

    def read_memory(self):
        """无参数；返回可用物理内存及提交余量，采样失败时拒绝伪造零占用。"""
        if os.name != "nt":
            return {"ok": False, "error": "当前内存门槛仅支持Windows服务器"}
        self.cb = ctypes.sizeof(self)
        if not ctypes.windll.psapi.GetPerformanceInfo(ctypes.byref(self), self.cb):
            return {"ok": False, "error": "GetPerformanceInfo调用失败"}
        scale = self.PageSize / 1024 ** 2
        return {
            "ok": True,
            "physicalTotalMiB": round(self.PhysicalTotal * scale, 2),
            "availableRamMiB": round(self.PhysicalAvailable * scale, 2),
            "commitUsedMiB": round(self.CommitTotal * scale, 2),
            "commitLimitMiB": round(self.CommitLimit * scale, 2),
            "commitFreeMiB": round((self.CommitLimit - self.CommitTotal) * scale, 2),
            "commitPeakMiB": round(self.CommitPeak * scale, 2),
        }
