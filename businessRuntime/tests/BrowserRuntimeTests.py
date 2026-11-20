"""浏览器兼容回归：用假驱动核验正常启动、缓存缺失回退和真实错误透传。

参数无；仅运行unittest，不下载浏览器、不访问网络或业务配置。
"""
import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


class BrowserRuntimeTests(unittest.TestCase):
    """对部署账号兼容分支逐项验收。"""

    def setUp(self):
        """无参数；从同目录或正式microServices位置读取候选类。"""
        root = Path(__file__).resolve().parent
        file = root / "BrowserRuntime.py"
        if not file.exists():
            file = root.parent / "microServices" / "BrowserRuntime.py"
        spec = importlib.util.spec_from_file_location("browserCandidate", file)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.driver = SimpleNamespace(chromium=SimpleNamespace(launch=Mock(return_value="browser")))

    def test_normal(self):
        """无参数；已有默认浏览器直接启动一次。"""
        with patch.dict(os.environ, {"SYNLOVE_BROWSER_EXECUTABLE": ""}):
            self.assertEqual(self.module.BrowserRuntime.launch_browser(self.driver), "browser")
        self.driver.chromium.launch.assert_called_once_with(headless=True, timeout=10000)

    def test_missing_cache(self):
        """无参数；只有缓存不存在才使用已安装Edge，并保留宿主的固定参数。"""
        self.driver.chromium.launch.side_effect = [RuntimeError("Executable doesn't exist"), "edge"]
        with patch.dict(os.environ, {"SYNLOVE_BROWSER_EXECUTABLE": ""}), patch.object(Path, "is_file", return_value=True):
            self.assertEqual(self.module.BrowserRuntime.launch_browser(self.driver, args=["--test"]), "edge")
        self.assertEqual(self.driver.chromium.launch.call_count, 2)
        self.assertIn("executable_path", self.driver.chromium.launch.call_args.kwargs)
        self.assertEqual(self.driver.chromium.launch.call_args.kwargs["args"], ["--test"])

    def test_real_error(self):
        """无参数；启动崩溃不得被兼容回退掩盖。"""
        self.driver.chromium.launch.side_effect = RuntimeError("permission denied")
        with patch.dict(os.environ, {"SYNLOVE_BROWSER_EXECUTABLE": ""}):
            with self.assertRaisesRegex(RuntimeError, "permission denied"):
                self.module.BrowserRuntime.launch_browser(self.driver)
        self.assertEqual(self.driver.chromium.launch.call_count, 1)

    def test_explicit(self):
        """无参数；部署显式指定程序时保留该选择。"""
        with patch.dict(os.environ, {"SYNLOVE_BROWSER_EXECUTABLE": "existingBrowser.exe"}):
            self.module.BrowserRuntime.launch_browser(self.driver)
        self.assertEqual(self.driver.chromium.launch.call_args.kwargs["executable_path"], "existingBrowser.exe")


if __name__ == "__main__":
    unittest.main()
