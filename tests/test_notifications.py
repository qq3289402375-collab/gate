import io
import json
import os
import smtplib
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

import notifications


class NotificationTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    def test_channel_configuration(self):
        self.assertEqual(notifications.available_channels(), ())
        os.environ["QQ_EMAIL"] = "alerts@example.test"
        with self.assertRaises(notifications.DeliveryError):
            notifications.available_channels()
        os.environ.update(QQ_AUTH_CODE="fake-auth-code", PUSHPLUS_TOKEN="fake-token")
        self.assertEqual(notifications.available_channels(), ("qq", "pushplus"))

    @patch("notifications.smtplib.SMTP_SSL")
    def test_qq_sends_single_utf8_batch(self, mock_smtp):
        os.environ.update(QQ_EMAIL="alerts@example.test", QQ_AUTH_CODE="fake-auth-code")
        connection = mock_smtp.return_value
        connection.send_message.return_value = {}
        result = notifications.send("qq", "巡检汇总", "BTC 命中\nETH 命中")
        self.assertEqual(result, "smtp_accepted")
        self.assertEqual(mock_smtp.call_args.args, ("smtp.qq.com", 465))
        connection.login.assert_called_once_with("alerts@example.test", "fake-auth-code")
        connection.send_message.assert_called_once()
        connection.close.assert_called_once()
        message = connection.send_message.call_args.args[0]
        self.assertEqual(message["Subject"], "巡检汇总")
        self.assertIn("BTC 命中\nETH 命中", message.get_content())

    @patch("notifications.smtplib.SMTP_SSL")
    def test_qq_auth_failure_does_not_expose_server_text(self, mock_smtp):
        os.environ.update(QQ_EMAIL="alerts@example.test", QQ_AUTH_CODE="fake-auth-code")
        connection = mock_smtp.return_value
        connection.login.side_effect = smtplib.SMTPAuthenticationError(535, b"fake-auth-code")
        with self.assertRaisesRegex(notifications.DeliveryError, "SMTP 登录失败") as error:
            notifications.send("qq", "巡检", "正文")
        self.assertNotIn("fake-auth-code", str(error.exception))
        connection.send_message.assert_not_called()
        connection.close.assert_called_once()

    @patch("notifications.build_opener")
    def test_pushplus_returns_queue_receipt_and_escapes_html(self, build_opener):
        os.environ["PUSHPLUS_TOKEN"] = "fake-token"
        opener = build_opener.return_value
        opener.open.return_value = io.BytesIO(b'{"code":200,"data":"abc123"}')
        result = notifications.send("pushplus", "巡检", "A < B\nC")
        self.assertEqual(result, "pushplus_queued:abc123")
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://www.pushplus.plus/send")
        body = json.loads(request.data)
        self.assertEqual(body["content"], "A &lt; B<br>C")
        self.assertEqual(body["channel"], "wechat")

    @patch("notifications.build_opener")
    def test_pushplus_treats_service_rejection_as_failure(self, build_opener):
        os.environ["PUSHPLUS_TOKEN"] = "fake-token"
        opener = build_opener.return_value
        opener.open.return_value = io.BytesIO(b'{"code":903,"msg":"fake-token"}')
        with self.assertRaisesRegex(notifications.DeliveryError, "903") as error:
            notifications.send("pushplus", "巡检", "正文")
        self.assertNotIn("fake-token", str(error.exception))
        opener.open.assert_called_once()

    @patch("notifications.build_opener")
    def test_pushplus_http_failure_does_not_retry(self, build_opener):
        os.environ["PUSHPLUS_TOKEN"] = "fake-token"
        opener = build_opener.return_value
        opener.open.side_effect = HTTPError(notifications.PUSHPLUS_URL, 503, "fake-token", {}, None)
        with self.assertRaisesRegex(notifications.DeliveryError, "HTTP 503") as error:
            notifications.send("pushplus", "巡检", "正文")
        self.assertNotIn("fake-token", str(error.exception))
        opener.open.assert_called_once()

    @patch("notifications.build_opener")
    def test_deepseek_without_key_does_not_make_request(self, build_opener):
        self.assertEqual(notifications.explain("公开行情"), ("", {}))
        build_opener.assert_not_called()

    @patch("notifications.build_opener")
    def test_deepseek_selects_source_facts_and_returns_usage(self, build_opener):
        os.environ["DEEPSEEK_API_KEY"] = "fake-api-key"
        opener = build_opener.return_value
        opener.open.return_value = io.BytesIO(json.dumps({
            "choices": [{"finish_reason": "stop", "message": {"content": '{"lines":[2]}'}}],
            "usage": {"prompt_tokens": 47, "completion_tokens": 12, "total_tokens": 59},
        }).encode("utf-8"))
        summary, usage = notifications.explain("BTC 15m 扫低收回\nETH 15m 扫高收回")
        self.assertEqual(summary, "ETH 15m 扫高收回")
        self.assertEqual(usage, {"prompt_tokens": 47, "completion_tokens": 12, "total_tokens": 59})
        request = opener.open.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer fake-api-key")
        body = json.loads(request.data)
        self.assertEqual(body["model"], "deepseek-flash")
        self.assertEqual(body["thinking"], {"type": "disabled"})
        self.assertEqual(body["max_tokens"], 180)

    @patch("notifications.build_opener")
    def test_deepseek_rejects_out_of_range_selection(self, build_opener):
        os.environ["DEEPSEEK_API_KEY"] = "fake-api-key"
        build_opener.return_value.open.return_value = io.BytesIO(json.dumps({
            "choices": [{"finish_reason": "stop", "message": {"content": '{"lines":[2]}'}}],
        }).encode("utf-8"))
        with self.assertRaisesRegex(notifications.DeliveryError, "摘要行号"):
            notifications.explain("只有一行公开行情")
