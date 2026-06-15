import asyncio
import base64
import importlib
import sys
import tempfile
import types
import unittest


class _Logger:
    def warning(self, *_args, **_kwargs):
        pass

    def info(self, *_args, **_kwargs):
        pass


class _MessageChain:
    def __init__(self, chain=None):
        self.chain = chain or []

    def message(self, text):
        self.chain.append(text)
        return self


class _Filter:
    @staticmethod
    def command(*_args, **_kwargs):
        return lambda func: func


class _Star:
    def __init__(self, context):
        self.context = context


class _Plain:
    def __init__(self, text):
        self.text = text


class _Context:
    def __init__(self):
        self.messages = []

    async def send_message(self, umo, chain):
        self.messages.append((umo, chain))


class _Bot:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    async def call_action(self, action, **params):
        if self.fail:
            raise RuntimeError("upload failed")
        self.calls.append((action, params))
        return {}


class _MethodOnlyBot:
    def __init__(self):
        self.calls = []

    async def upload_group_file(self, **params):
        self.calls.append(("upload_group_file", params))
        return {}



class _FakeEvent:
    def __init__(self, sender_id="10001", group_id=None, text="", bot=None):
        self._sender_id = sender_id
        self._group_id = group_id
        self.message_str = text
        self.bot = bot
        self.unified_msg_origin = (
            f"group:{group_id}" if group_id else f"private:{sender_id}"
        )

    def get_sender_id(self):
        return self._sender_id

    def get_group_id(self):
        return self._group_id


def _install_astrbot_stubs():
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    event = types.ModuleType("astrbot.api.event")
    star = types.ModuleType("astrbot.api.star")
    components = types.ModuleType("astrbot.api.message_components")
    core = types.ModuleType("astrbot.core")
    utils = types.ModuleType("astrbot.core.utils")
    path_mod = types.ModuleType("astrbot.core.utils.astrbot_path")

    api.AstrBotConfig = dict
    api.logger = _Logger()
    event.AstrMessageEvent = object
    event.MessageChain = _MessageChain
    event.filter = _Filter()
    star.Context = object
    star.Star = _Star
    star.register = lambda *_args, **_kwargs: (lambda cls: cls)
    components.Plain = _Plain
    path_mod.get_astrbot_data_path = tempfile.gettempdir

    sys.modules.setdefault("astrbot", astrbot)
    sys.modules.setdefault("astrbot.api", api)
    sys.modules.setdefault("astrbot.api.event", event)
    sys.modules.setdefault("astrbot.api.star", star)
    sys.modules.setdefault("astrbot.api.message_components", components)
    sys.modules.setdefault("astrbot.core", core)
    sys.modules.setdefault("astrbot.core.utils", utils)
    sys.modules.setdefault("astrbot.core.utils.astrbot_path", path_mod)


class JMComicPluginHelperTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _install_astrbot_stubs()
        cls.main = importlib.import_module("main")

    def _plugin(self, config, context=None):
        return self.main.JMComicPlugin(context=context or _Context(), config=config)

    def _sample_task(
        self,
        path,
        *,
        group=True,
        bot=None,
        export_format="pdf",
    ):
        return self.main.JMComicTask(
            task_id="abc12345",
            album_id="438516",
            export_format=export_format,
            umo="group:123" if group else "private:1",
            requester_id="1",
            status="completed",
            title="sample",
            output_file=path,
            delivery_is_group=group,
            delivery_session_id="123" if group else "1",
            bot_client=bot,
        )

    def test_extract_jm_id(self):
        self.assertEqual(self.main.JMComicPlugin._extract_jm_id("JM438516"), "438516")
        self.assertEqual(self.main.JMComicPlugin._extract_jm_id("abc123def"), "123")
        self.assertIsNone(self.main.JMComicPlugin._extract_jm_id("no id"))

    def test_nested_config_is_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = self._plugin(
                {
                    "access_control": {
                        "enable_private_only": True,
                        "group_whitelist": ["123"],
                        "private_whitelist": ["456"],
                    },
                    "network": {"proxy": "http://127.0.0.1:7890"},
                    "download": {
                        "download_base_dir": tmp,
                        "default_export_format": "pdf",
                    },
                }
            )
            self.assertTrue(plugin.enable_private_only)
            self.assertEqual(plugin.group_whitelist, {"123"})
            self.assertEqual(plugin.private_whitelist, {"456"})
            self.assertEqual(plugin.proxy, "http://127.0.0.1:7890")
            self.assertEqual(plugin.default_export_format, "pdf")
            self.assertEqual(plugin.file_delivery_mode, "onebot_group_file_base64")
            self.assertEqual(plugin.max_base64_file_mb, 50)

    def test_invalid_file_delivery_mode_falls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = self._plugin(
                {
                    "download": {
                        "download_base_dir": tmp,
                        "file_delivery_mode": "invalid",
                    },
                }
            )
            self.assertEqual(plugin.file_delivery_mode, "onebot_group_file_base64")

    def test_permission_defaults_to_private_or_whitelisted_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = self._plugin(
                {
                    "access_control": {
                        "enable_private_only": True,
                        "group_whitelist": ["123"],
                    },
                    "download": {"download_base_dir": tmp},
                }
            )
            self.assertTrue(plugin._is_allowed(_FakeEvent(group_id=None))[0])
            self.assertTrue(plugin._is_allowed(_FakeEvent(group_id="123"))[0])
            self.assertFalse(plugin._is_allowed(_FakeEvent(group_id="999"))[0])

    def test_active_snapshot_becomes_interrupted(self):
        task = self.main.JMComicTask.from_snapshot(
            {
                "task_id": "abc",
                "album_id": "438516",
                "export_format": "zip",
                "umo": "private:1",
                "requester_id": "1",
                "status": "running",
            }
        )
        self.assertEqual(task.status, "interrupted")

    def test_group_file_upload_uses_base64_call_action(self):
        bot = _Bot()
        context = _Context()
        with tempfile.TemporaryDirectory() as tmp:
            output = tempfile.NamedTemporaryFile(suffix=".pdf", dir=tmp, delete=False)
            output.write(b"pdf")
            output.close()
            plugin = self._plugin({"download": {"download_base_dir": tmp}}, context)

            asyncio.run(
                plugin._send_file_or_path(self._sample_task(output.name, bot=bot))
            )

            self.assertEqual(len(bot.calls), 1)
            action, params = bot.calls[0]
            self.assertEqual(action, "upload_group_file")
            self.assertEqual(params["group_id"], 123)
            self.assertEqual(params["name"], "JM438516_abc12345.pdf")
            self.assertTrue(params["file"].startswith("base64://"))
            payload = params["file"].removeprefix("base64://")
            self.assertEqual(base64.b64decode(payload), b"pdf")
            self.assertIn("group:123", context.messages[0][0])

    def test_group_file_upload_can_use_direct_method(self):
        bot = _MethodOnlyBot()
        context = _Context()
        with tempfile.TemporaryDirectory() as tmp:
            output = tempfile.NamedTemporaryFile(suffix=".zip", dir=tmp, delete=False)
            output.write(b"zip")
            output.close()
            plugin = self._plugin({"download": {"download_base_dir": tmp}}, context)

            asyncio.run(
                plugin._send_file_or_path(
                    self._sample_task(output.name, bot=bot, export_format="zip")
                )
            )

            self.assertEqual(bot.calls[0][0], "upload_group_file")
            self.assertEqual(bot.calls[0][1]["name"], "JM438516_abc12345.zip")

    def test_private_chat_does_not_upload_group_file(self):
        bot = _Bot()
        context = _Context()
        with tempfile.TemporaryDirectory() as tmp:
            output = tempfile.NamedTemporaryFile(suffix=".pdf", dir=tmp, delete=False)
            output.write(b"pdf")
            output.close()
            plugin = self._plugin({"download": {"download_base_dir": tmp}}, context)

            asyncio.run(
                plugin._send_file_or_path(
                    self._sample_task(output.name, group=False, bot=bot)
                )
            )

            self.assertEqual(bot.calls, [])
            text = context.messages[0][1].chain[0]
            self.assertIn("upload_group_file", text)
            self.assertIn(output.name, text)

    def test_large_file_does_not_base64_encode_or_upload(self):
        bot = _Bot()
        context = _Context()
        with tempfile.TemporaryDirectory() as tmp:
            output = tempfile.NamedTemporaryFile(suffix=".pdf", dir=tmp, delete=False)
            output.seek((1024 * 1024) + 1)
            output.write(b"x")
            output.close()
            plugin = self._plugin(
                {
                    "download": {
                        "download_base_dir": tmp,
                        "max_base64_file_mb": 1,
                    }
                },
                context,
            )

            asyncio.run(plugin._send_file_or_path(self._sample_task(output.name, bot=bot)))

            self.assertEqual(bot.calls, [])
            self.assertEqual(len(context.messages), 1)
            self.assertIn(output.name, context.messages[0][1].chain[0])

    def test_upload_failure_reports_saved_path(self):
        bot = _Bot(fail=True)
        context = _Context()
        with tempfile.TemporaryDirectory() as tmp:
            output = tempfile.NamedTemporaryFile(suffix=".pdf", dir=tmp, delete=False)
            output.write(b"pdf")
            output.close()
            plugin = self._plugin({"download": {"download_base_dir": tmp}}, context)

            asyncio.run(
                plugin._send_file_or_path(self._sample_task(output.name, bot=bot))
            )

            text = context.messages[0][1].chain[0]
            self.assertIn("upload failed", text)
            self.assertIn(output.name, text)


if __name__ == "__main__":
    unittest.main()
