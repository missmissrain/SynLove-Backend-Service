"""统一跨模型显存协调：先持有操作系统文件锁，再检查显存并请求空闲模型让位。

注册参数root为后台仓库、name为模型登记名；config/gpuResidency.json可配置等待策略，
config/gpuModels.json登记所有可能占用GPU的服务、端口、卸载路由与token环境变量。
只访问登记的固定回环卸载端点；不依据GPU利用率猜测任务是否结束，不处理桌面应用。
"""
from gpuRuntime.ProjectPaths import model_config
import contextlib
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from .MemoryInspector import MemoryInspector
from .SchedulerState import SchedulerState
from .CommitCapacity import CommitCapacity


class GpuCoordinator:
    """串行调度共享小显存的模型请求；进程退出时系统自动释放锁。"""

    def __init__(self, root, name):
        """root为共享后台根目录，name为当前模型；不在公开配置中保存凭据。"""
        self.root = Path(root).resolve()
        self.name = name
        path = self.root / "config/gpuResidency.json"
        self.config = json.loads(path.read_text("utf-8-sig")) if path.exists() else {}
        registry_path = self.root / "config/gpuModels.json"
        self.registry = json.loads(registry_path.read_text("utf-8-sig")) if registry_path.exists() else {"models": []}
        self.tokens = self.load_peer_tokens()
        self.config_stamp = None
        self.state = SchedulerState(self.root)
        self.wait_seconds = float(self.config.get("waitSeconds", 1200))
        self.memory_seconds = float(self.config.get(
            "llmMemoryWaitSeconds" if name == "llm" else "memoryWaitSeconds",
            self.config.get("memoryWaitSeconds", 180)))
        if not 0 < self.idle_seconds <= 86400 or not 0 <= self.wait_seconds <= 86400:
            raise ValueError("显存驻留配置的保温和等待期限无效")
        if not 0 <= self.memory_seconds <= 86400:
            raise ValueError("显存等待期限无效")
        self.path = self.root / "runtime/gpuResidency/model.lock"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def refresh_config(self):
        """无参数；按修改时间重读资源策略，保温设置可以即时生效，不改变推理参数。"""
        path = self.root / "config/gpuResidency.json"
        stamp = path.stat().st_mtime_ns
        if stamp != self.config_stamp:
            cfg = json.loads(path.read_text(encoding="utf-8-sig"))
            idle = float(cfg.get("keepAliveSeconds", 600))
            if not 0 < idle <= 86400:
                raise ValueError("保温期限必须在0到86400秒之间")
            self.config = cfg
            self.config_stamp = stamp
            self.wait_seconds = float(cfg.get("waitSeconds", 1200))
            self.memory_seconds = float(cfg.get(
                "llmMemoryWaitSeconds" if self.name == "llm" else "memoryWaitSeconds",
                cfg.get("memoryWaitSeconds", 180)))

    @property
    def idle_seconds(self):
        """无参数；返回热更新后的空闲保温秒数，健康查询不延长计时。"""
        self.refresh_config()
        return float(self.config.get("keepAliveSeconds", 600))

    def load_peer_tokens(self):
        """无参数；仅在内存中读取已登记同伴的服务凭据，不打印或写入公共配置。"""
        names = {item.get("tokenEnv") for item in self.registry.get("models", [])
                 if item.get("tokenEnv")}
        tokens = {name: os.environ.get(name, "") for name in names}
        hub = model_config(self.root, "modelHub.server.json")
        cfg = json.loads(hub.read_text("utf-8-sig")) if hub.exists() else {}
        path = Path(cfg.get("service", {}).get(
            "secretEnvFile", str(self.root.parent / "server/synlove-server.env")))
        if path.is_file():
            for line in path.read_text(encoding="utf-8-sig").splitlines():
                if "=" not in line or line.lstrip().startswith("#"):
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                if key in names and not tokens[key]:
                    tokens[key] = value.strip().strip('"').strip("'")
        return tokens

    @contextlib.contextmanager
    def request_slot(self):
        """无参数；所有CPU/GPU模型共享执行资格，放行前验证同伴已卸载及内存余量。"""
        self.refresh_config()
        self.registry = json.loads((self.root / "config/gpuModels.json").read_text("utf-8-sig"))
        key = self.state.mark_waiting(self.name)
        with self.path.open("a+b", buffering=0) as file:
            if os.fstat(file.fileno()).st_size == 0:
                file.write(b"\0")
            wait = self.config.get("llmSlotWaitSeconds", self.wait_seconds) if self.name == "llm" else self.wait_seconds
            deadline = time.monotonic() + float(wait) if float(wait) > 0 else None
            try:
                while True:
                    file.seek(0)
                    try:
                        if os.name == "nt":
                            import msvcrt
                            msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
                        else:
                            import fcntl
                            fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except OSError:
                        if deadline is not None and time.monotonic() >= deadline:
                            raise TimeoutError("等待其他模型完成推理超时，未打断正在执行的任务")
                        time.sleep(0.1)
            except BaseException:
                self.state.remove_waiting(key)
                raise
            self.state.remove_waiting(key)
            try:
                self.current_key = key
                self.state.mark_active(key, self.name, "releasingPeers")
                self.ensure_memory(0)
                self.state.mark_active(key, self.name, "running",
                                       memory=MemoryInspector().read_memory())
                yield
            finally:
                try:
                    self.state.mark_active(key, self.name, "idle",
                                           memory=MemoryInspector().read_memory())
                finally:
                    self.current_key = None
                    file.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(file.fileno(), fcntl.LOCK_UN)

    def free_memory(self):
        """无参数；读取GPU空闲MiB，探测失败返回None，不通过利用率判定任务空闲。"""
        try:
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5,
                creationflags=0x08000000 if os.name == "nt" else 0)
            return int(result.stdout.strip().splitlines()[0]) if result.returncode == 0 else None
        except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
            return None

    def release_peers(self):
        """无参数；卸载并核验所有其他受管模型；失败时禁止新模型进入执行阶段。"""
        released = []
        failed = []
        skipped = []
        models = sorted(self.registry.get("models", []),
                        key=lambda item: int(item.get("releasePriority", 100)))
        for cfg in models:
            if cfg.get("name") == self.name or not cfg.get("enabled", True) or not cfg.get(
                    "resourceManaged", cfg.get("gpuManaged", True)):
                continue
            port = int(cfg["port"])
            if not 1 <= port <= 65535:
                continue
            path = str(cfg.get("unloadPath", "/internal/gpu/unload"))
            if not path.startswith("/"):
                continue
            headers = {"Content-Type": "application/json"}
            key = self.tokens.get(cfg.get("tokenEnv", ""), "")
            if key:
                headers["Authorization"] = "Bearer " + key
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}{path}",
                data=b"{}", headers=headers, method="POST")
            try:
                with self.opener.open(request, timeout=30) as response:
                    reply = json.loads(response.read(65536))
                if reply.get("ok") is not True or reply.get("loaded"):
                    raise RuntimeError("后端没有确认权重已释放")
                check = urllib.request.Request(
                    f"http://127.0.0.1:{port}/health", headers=headers)
                with self.opener.open(check, timeout=10) as response:
                    health = json.loads(response.read(65536))
                if health.get("ok", True) is False or any(
                        health.get(name) for name in ["loaded", "runtimeLoaded", "modelLoaded", "busy"]):
                    raise RuntimeError("卸载后健康检查仍有驻留或执行任务")
                released.append(cfg["name"])
            except (OSError, ValueError, RuntimeError, urllib.error.HTTPError) as error:
                # 连接被拒绝/重置说明该服务根本没在运行，不可能占用显存；
                # 以前这种情况会被判成「无法确认卸载」，导致执行锁被永久占住。
                reason = getattr(error, "reason", error)
                if isinstance(reason, (ConnectionRefusedError, ConnectionResetError,
                                       ConnectionAbortedError)) or \
                        getattr(reason, "winerror", None) in (10061, 10054, 10053):
                    skipped.append({"model": cfg["name"], "state": "notRunning"})
                    continue
                failed.append({"model": cfg["name"], "error": type(error).__name__})
        self.last_peer_results = {"released": released, "failed": failed,
                                  "notRunning": skipped}
        if failed and self.config.get("strictSingleModel", True):
            raise TimeoutError("无法确认同伴已安全卸载，本次不加载模型：" +
                               ", ".join(item["model"] for item in failed))
        return released

    def release_peer(self):
        """无参数；兼容旧调用名，实际按统一模型登记表请求所有可让位模型。"""
        return bool(self.release_peers())

    def _release_registered_models(self):
        """无参数；统一释放入口，保留独立方法便于日志和后续调度策略扩展。"""
        return self.release_peers()

    def _legacy_release_peer(self, cfg):
        """cfg为旧版单模型配置；仅用于兼容旧部署，不作为新的调度来源。"""
        port = int(cfg["port"])
        if not 1 <= port <= 65535:
            raise ValueError("显存协调端口无效")
        headers = {"Content-Type": "application/json"}
        key = self.tokens.get(cfg["tokenEnv"], "")
        if key:
            headers["Authorization"] = "Bearer " + key
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/internal/gpu/unload",
            data=b"{}", headers=headers, method="POST")
        try:
            with self.opener.open(request, timeout=30) as response:
                reply = json.loads(response.read(65536))
                return reply.get("ok") is True
        except (OSError, ValueError):
            return False

    def ensure_memory(self, need_mb, cold=False):
        """need_mb为冷加载显存预算，cold允许CPU冷加载；暖机只检查基础余量，避免重复预留权重容量。"""
        deadline = time.monotonic() + self.memory_seconds if self.memory_seconds > 0 else None
        policies = self.config.get("resourceBudgets", {})
        policy = dict(policies.get("default", {}))
        if need_mb > 0 or cold:
            policy.update(policies.get(self.name, {}))
        last = ""
        prepared = False
        released = False
        peer_deadline = time.monotonic() + min(float(self.memory_seconds or 180), 120)
        wait = self.config.get("resourceShortageAction", "wait") == "wait"
        while True:
            if not released and (self.config.get("strictSingleModel", True) or
                                 self.config.get("releaseIdlePeersBeforeLoad", True)):
                try:
                    self._release_registered_models()
                    released = True
                except TimeoutError as error:
                    if not wait:
                        raise
                    # 持有执行锁等待同伴时必须有上限，否则同伴异常会让整个 GPU 管线永久停摆。
                    if time.monotonic() >= peer_deadline:
                        raise TimeoutError("等待同伴释放显存超时，已放弃本次加载：" + str(error))
                    self.record_wait("waitingPeerRelease", str(error))
                    time.sleep(1)
                    continue
            mem = MemoryInspector().read_memory()
            free = self.free_memory() if need_mb > 0 else 0
            ram = float(policy.get("minAvailableRamMiB", 1024))
            commit = float(policy.get("minCommitHeadroomMiB", 4096))
            if not mem.get("ok"):
                if not wait:
                    raise TimeoutError("系统内存探测失败，本次不加载模型")
                self.record_wait("waitingResources", "等待系统内存探测恢复")
                time.sleep(1)
                continue
            if (not prepared and policy.get("prepareCommitCapacity") and
                    mem["availableRamMiB"] >= ram and mem["commitFreeMiB"] < commit):
                prepared = True
                self.record_wait("preparingCommit", "准备磁盘页面文件提交容量")
                self.last_commit_preparation = CommitCapacity().prepare_capacity(
                    commit, seconds=float(policy.get("preparationSeconds", 60)))
                continue
            vram_ok = need_mb <= 0 or (free is not None and free >= need_mb)
            memory_ok = mem["availableRamMiB"] >= ram and mem["commitFreeMiB"] >= commit
            if vram_ok and memory_ok:
                if getattr(self, "current_key", None):
                    self.state.mark_active(self.current_key, self.name, "running", memory=mem)
                return
            last = (f"显存空闲{free}MiB/需求{need_mb}MiB，"
                    f"可用内存{mem['availableRamMiB']:.0f}MiB/需求{ram:.0f}MiB，"
                    f"提交余量{mem['commitFreeMiB']:.0f}MiB/需求{commit:.0f}MiB")
            self.record_wait("waitingResources", last)
            if not wait and self.name == "llm" and self.config.get("cloudFallback", {}).get("enabled"):
                raise RuntimeError(last + "；本地模型拒绝加载，交由已配置云端兜底")
            if not wait and deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("资源不足，本次拒绝加载：" + last)
            time.sleep(1)

    def record_wait(self, phase, reason):
        """phase/reason为资源等待阶段及原因；更新同一任务状态，不丢弃或重交业务请求。"""
        if getattr(self, "current_key", None):
            self.state.mark_active(self.current_key, self.name, phase, reason=reason)
