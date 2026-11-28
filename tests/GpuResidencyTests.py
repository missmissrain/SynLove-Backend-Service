"""显存驻留隔离测试：使用假模型和真实本机文件锁，验证超时、互斥、鉴权及保温。

注册参数无；不访问真实GPU、不调用模型、不读取私人凭据。
"""
import contextlib
import argparse
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from gpuRuntime.GpuCoordinator import GpuCoordinator
from gpuRuntime.LlmRuntime import LlmRuntime
from gpuRuntime.TtsResidency import TtsResidency


class GpuResidencyTests(unittest.TestCase):
    """验证跨模型资源互斥和空闲卸载边界，而非依赖GPU利用率猜测。"""

    def setUp(self):
        """无参数；创建本次独立运行目录，默认TTL600秒。"""
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.coordinator = GpuCoordinator(self.root, "tts")
        self.cleanups = []

    def tearDown(self):
        """无参数；停止测试创建的定时器和HTTP服务后清理临时文件。"""
        for cleanup in reversed(self.cleanups):
            cleanup()
        self.temp.cleanup()

    def make_tts(self):
        """无参数；创建不含任何模型依赖的TTS状态和独占锁。"""
        state = {"runtime": object(), "device": "cuda", "device_arg": "int8",
                 "last_active": time.time(), "busy": False}
        lock = threading.Lock()
        item = TtsResidency(state, lock, self.coordinator)
        self.cleanups.append(item.stopped.set)
        return item

    def make_llm(self):
        """无参数；创建可注入假子进程的LLM生命周期，不执行真实模型命令。"""
        item = LlmRuntime(["unused"], self.root, self.root / "test.log",
                         8180, self.coordinator, 5000)
        self.cleanups.append(item.close_runtime)
        return item

    def test_default_ttl_and_no_unnecessary_eviction(self):
        """无参数；保温默认十分钟，显存充足时不卸载同伴。"""
        self.assertEqual(self.coordinator.idle_seconds, 600)
        with patch.object(self.coordinator, "free_memory", return_value=6000), \
             patch.object(self.coordinator, "release_peer") as peer:
            self.coordinator.ensure_memory(5000)
            peer.assert_not_called()

    def test_pressure_releases_idle_peer_immediately(self):
        """无参数；显存不足即请求同伴让位，不等待600秒到期。"""
        with patch.object(self.coordinator, "free_memory", side_effect=[1000, 5500]), \
             patch.object(self.coordinator, "release_peer", return_value=True) as peer:
            self.coordinator.ensure_memory(5000)
            peer.assert_called_once()

    def test_pressure_never_kills_external_process(self):
        """无参数；同伴不能释放且资源不足时有界失败，不终止其他GPU应用。"""
        self.coordinator.memory_seconds = 0.01
        with patch.object(self.coordinator, "free_memory", return_value=500), \
             patch.object(self.coordinator, "release_peer", return_value=False), \
             patch("gpuRuntime.GpuCoordinator.subprocess.Popen") as proc:
            with self.assertRaises(TimeoutError):
                self.coordinator.ensure_memory(5000)
            proc.assert_not_called()

    def test_real_cross_process_exclusion_and_crash_release(self):
        """无参数；另一个Python持锁时阻塞，持锁进程结束后操作系统自动释放锁。"""
        code = (
            "import sys,time;from gpuRuntime.GpuCoordinator import GpuCoordinator;"
            "c=GpuCoordinator(sys.argv[1],'llm');"
            "slot=c.request_slot();slot.__enter__();print('locked',flush=True);time.sleep(30)"
        )
        proc = subprocess.Popen([sys.executable, "-X", "utf8", "-c", code, str(self.root)],
                                cwd=ROOT, stdout=subprocess.PIPE, text=True,
                                creationflags=0x08000000 if os.name == "nt" else 0)
        try:
            self.assertEqual(proc.stdout.readline().strip(), "locked")
            self.coordinator.wait_seconds = 0.15
            with self.assertRaises(TimeoutError):
                with self.coordinator.request_slot():
                    self.fail("其他进程持锁时不可进入")
            proc.terminate()
            proc.wait(5)
            with self.coordinator.request_slot():
                pass
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(5)
            proc.stdout.close()

    def test_tts_busy_cannot_be_evicted(self):
        """无参数；即使GPU利用率低，持请求锁的TTS也不能被卸载。"""
        item = self.make_tts()
        original = item.state["runtime"]
        with item.lock:
            self.assertFalse(item.unload_idle())
        self.assertIs(item.state["runtime"], original)
        item.state["busy"] = True
        self.assertFalse(item.unload_idle())

    def test_tts_idle_unload_and_reuse_window(self):
        """无参数；十分钟内保持对象身份，超过期限后清理且无需重启HTTP。"""
        item = self.make_tts()
        original = item.state["runtime"]
        item.state["last_active"] = time.time() - 590
        time.sleep(1.1)
        self.assertIs(item.state["runtime"], original)
        item.state["last_active"] = time.time() - 601
        deadline = time.monotonic() + 3
        while item.state["runtime"] is not None and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertIsNone(item.state["runtime"])

    def test_tts_lock_order_and_exception_release(self):
        """无参数；请求先获全局资格再获本地锁，异常后两级锁均释放。"""
        item = self.make_tts()
        with self.assertRaises(ValueError):
            with item.request_slot():
                self.assertFalse(item.unload_idle())
                raise ValueError("假请求异常")
        self.assertTrue(item.unload_idle())
        with self.coordinator.request_slot():
            pass

    def test_llm_busy_protection_and_completion_clock(self):
        """无参数；生成期拒绝让位，生成完成才刷新空闲时钟，热请求复用所属进程。"""
        item = self.make_llm()
        proc = Mock()
        proc.poll.return_value = None
        item.process = proc
        old = item.last_active = time.monotonic() - 590
        with item.request_model():
            self.assertTrue(item.busy)
            self.assertFalse(item.unload_idle())
            self.assertIs(item.process, proc)
        self.assertGreater(item.last_active, old)
        self.assertFalse(item.busy)
        self.assertTrue(item.unload_idle())
        proc.terminate.assert_called_once()

    def test_llm_expiry_terminates_only_owned_child(self):
        """无参数；600秒空闲后只停止本实例创建的模型子进程。"""
        item = self.make_llm()
        proc = Mock()
        proc.poll.return_value = None
        item.process = proc
        item.last_active = time.monotonic() - 601
        deadline = time.monotonic() + 3
        while item.process is not None and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertIsNone(item.process)
        proc.terminate.assert_called_once()

    def test_llm_loader_failure_releases_request_state(self):
        """无参数；模型加载失败后释放资格并清除busy，下一请求仍可继续。"""
        item = self.make_llm()
        with patch.object(item, "ensure_loaded", side_effect=RuntimeError("fake")):
            with self.assertRaises(RuntimeError):
                with item.request_model():
                    pass
        self.assertFalse(item.busy)
        self.assertTrue(item.unload_idle())

    def test_llama_sleep_flag_is_valid_and_owned_by_coordinator(self):
        """无参数；即使旧启动参数为120，实际命令也用当前二进制支持的-1关闭内置计时。"""
        spec = importlib.util.spec_from_file_location("testGemmaServe", ROOT / "synlove-gemma/serve.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        args = argparse.Namespace(model="fake.gguf", internal_port=8180, ctx=65536,
            ngl=99, alias="fake", parallel=1, sleep_idle=120,
            api_key_env="", host="127.0.0.1", mmproj=None)
        command = module.build_args("fake-server", args)
        self.assertEqual(command[command.index("--sleep-idle-seconds") + 1], "-1")

    def test_api_health_and_auth_never_wake_model(self):
        """无参数；真实HTTP健康查询不加载模型，未授权请求不能触发GPU让位或模型加载。"""
        spec = importlib.util.spec_from_file_location("testLlmProxy", ROOT / "synlove-gemma/llmProxy.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        runtime = Mock()
        runtime.status.return_value = {"ok": True, "loaded": False, "busy": False, "keepAliveSeconds": 600}
        module.ProxyHandler.runtime = runtime
        module.ProxyHandler.token_env = "SYNLOVE_TEST_MODEL_TOKEN"
        server = ThreadingHTTPServer(("127.0.0.1", 0), module.ProxyHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.cleanups.append(server.server_close)
        self.cleanups.append(server.shutdown)
        base = "http://127.0.0.1:" + str(server.server_port)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with patch.dict(os.environ, {"SYNLOVE_TEST_MODEL_TOKEN": "test-only"}):
            for path in ("/health", "/v1/models", "/internal/gpu/status"):
                req = urllib.request.Request(base + path, headers={"Authorization": "Bearer test-only"})
                with opener.open(req, timeout=2) as response:
                    self.assertEqual(response.status, 200)
            req = urllib.request.Request(base + "/v1/chat/completions", data=b"{}")
            with self.assertRaises(urllib.error.HTTPError) as caught:
                opener.open(req, timeout=2)
            self.assertEqual(caught.exception.code, 401)
            runtime.request_model.assert_not_called()


if __name__ == "__main__":
    unittest.main()
