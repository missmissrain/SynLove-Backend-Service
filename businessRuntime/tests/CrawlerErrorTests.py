"""爬虫错误回传回归测试：使用模拟网络，不访问网站、不调用模型。

无参数；验证证书、DNS及安全拒绝通过MCP明确返回，且编程错误不被伪装成业务成功。
"""
import socket
import ssl
import unittest
from pathlib import Path
from unittest.mock import patch

from microServices.CrawlerAdapter import CrawlerAdapter


class CrawlerErrorTests(unittest.TestCase):
    """覆盖预期网络错误和真实成功路径，不修改私有配置。"""

    def call_tool(self):
        """无参数；建立固定公开地址的HTTP只读调用。"""
        return CrawlerAdapter(Path.cwd()).handle({
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "crawler_fetch",
                       "arguments": {"url": "https://example.com", "mode": "http"}}})

    def test_certificate_error(self):
        """无参数；证书失败必须返回可识别错误，不泄漏底层诊断文本。"""
        with patch.object(CrawlerAdapter, "fetch_http",
                          side_effect=ssl.SSLCertVerificationError("private detail")):
            value = self.call_tool()
        self.assertTrue(value["result"]["isError"])
        self.assertIn("TLS", str(value))
        self.assertNotIn("private detail", str(value))

    def test_dns_error(self):
        """无参数；DNS失败返回MCP错误而不是崩溃子进程。"""
        with patch.object(CrawlerAdapter, "fetch_http", side_effect=socket.gaierror("bad")):
            self.assertIn("DNS", str(self.call_tool()))

    def test_access_denied(self):
        """无参数；私网地址拒绝原因保留，禁止为连通性放开安全限制。"""
        with patch.object(CrawlerAdapter, "fetch_http", side_effect=ValueError("禁止爬取内网和本机地址")):
            self.assertTrue(self.call_tool()["result"]["isError"])

    def test_success(self):
        """无参数；成功路径保持MCP原格式。"""
        with patch.object(CrawlerAdapter, "fetch_http", return_value="public page"):
            value = self.call_tool()
        self.assertEqual(value["result"]["content"][0]["text"], "public page")
        self.assertFalse(value["result"].get("isError", False))


if __name__ == "__main__":
    unittest.main()
