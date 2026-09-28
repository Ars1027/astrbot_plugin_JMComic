import asyncio
import copy
import importlib
import io
import tempfile
import types
import unittest
from unittest import mock

from PIL import Image

from test_helpers import _Context, _FakeEvent, _install_astrbot_stubs


class _Image:
    def __init__(self, content):
        self.content = content

    @staticmethod
    def fromBytes(content):
        return _Image(content)


class _LookupEvent(_FakeEvent):
    def __init__(self, *args, message_obj=None, handlers=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.message_obj = message_obj
        self._handlers = handlers or {}

    def get_extra(self, key, default=None):
        if key == "handlers_parsed_params":
            return self._handlers
        return default

    def chain_result(self, chain):
        return chain


def _valid_cover():
    image = Image.new("RGB", (1, 1), (20, 40, 60))
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


class LookupTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        _install_astrbot_stubs()
        cls.main = importlib.import_module("main")
        cls.cards = importlib.import_module("album_card")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.plugin = self.main.JMComicPlugin(
            context=_Context(),
            config={"download": {"download_base_dir": self.tmp.name}},
        )
        self.card_patch = mock.patch.object(
            self.cards.Comp, "Image", _Image, create=True
        )
        self.card_patch.start()
        self.addCleanup(self.card_patch.stop)

    async def _texts(self, operation):
        return [reply async for reply in operation]

    async def test_auto_recognizes_first_embedded_id_and_ignores_false_matches(self):
        card = mock.AsyncMock(return_value=self.main.MessageChain(chain=["card"]))
        with mock.patch.object(self.plugin, "_album_card", card):
            event = _LookupEvent(text="请看中文JM 12345，另一个 JM67890")
            replies = await self._texts(self.plugin.on_jm_message(event))
            self.assertEqual(len(replies), 1)
            card.assert_awaited_once_with("12345")
            self.assertTrue(event.stopped)

            card.reset_mock()
            for text, expected in (("lower jm 7", "7"), ("MiXeD Jm\t89", "89")):
                event = _LookupEvent(text=text)
                self.assertEqual(
                    len(await self._texts(self.plugin.on_jm_message(event))), 1
                )
                card.assert_awaited_once_with(expected)
                card.reset_mock()

            for text in ("abcJM123", "JM123def", "x_jm123", "123", "jm_123"):
                skipped = _LookupEvent(text=text)
                self.assertEqual(
                    await self._texts(self.plugin.on_jm_message(skipped)), []
                )
                self.assertFalse(skipped.stopped)

    async def test_auto_skips_commands_self_registered_handlers_acl_and_config(self):
        card = mock.AsyncMock(return_value=self.main.MessageChain(chain=["card"]))
        with mock.patch.object(self.plugin, "_album_card", card):
            cases = [
                _LookupEvent(text="/jm详情 JM123"),
                _LookupEvent(
                    text="JM123",
                    message_obj=types.SimpleNamespace(message_str="/jm详情 JM123"),
                ),
                _LookupEvent(text="JM123", handlers={"identify": {}}),
                _LookupEvent(
                    sender_id="bot",
                    text="JM123",
                    message_obj=types.SimpleNamespace(self_id="bot"),
                ),
            ]
            for event in cases:
                self.assertEqual(
                    await self._texts(self.plugin.on_jm_message(event)), []
                )
                self.assertFalse(event.stopped)
            denied = _LookupEvent(group_id="blocked", text="JM123")
            self.assertEqual(await self._texts(self.plugin.on_jm_message(denied)), [])
            self.assertFalse(denied.stopped)
            self.plugin.group_whitelist = {"allowed"}
            allowed = _LookupEvent(group_id="allowed", text="JM321")
            self.assertEqual(
                len(await self._texts(self.plugin.on_jm_message(allowed))), 1
            )
            card.assert_awaited_once_with("321")

            card.reset_mock()
            self.plugin.private_whitelist = {"allowed"}
            denied = _LookupEvent(sender_id="denied", text="JM123")
            self.assertEqual(await self._texts(self.plugin.on_jm_message(denied)), [])
            self.assertFalse(denied.stopped)
            self.plugin.auto_recognize_jm = False
            disabled = _LookupEvent(text="JM123")
            self.assertEqual(await self._texts(self.plugin.on_jm_message(disabled)), [])
            self.assertFalse(disabled.stopped)
            card.assert_not_awaited()

    async def test_auto_failure_is_silent_and_cancellation_propagates(self):
        failed = mock.AsyncMock(side_effect=RuntimeError("offline"))
        with mock.patch.object(self.plugin, "_album_card", failed):
            event = _LookupEvent(text="JM123")
            self.assertEqual(await self._texts(self.plugin.on_jm_message(event)), [])
            self.assertTrue(event.stopped)

        cancelled = mock.AsyncMock(side_effect=asyncio.CancelledError())
        with mock.patch.object(self.plugin, "_album_card", cancelled):
            with self.assertRaises(asyncio.CancelledError):
                await self._texts(self.plugin.on_jm_message(_LookupEvent(text="JM123")))

    async def test_album_card_fetches_detail_and_valid_cover_with_all_tags(self):
        cover = _valid_cover()
        album = types.SimpleNamespace(name="标题", tags=[f"tag{i}" for i in range(12)])
        with (
            mock.patch.object(
                self.main, "fetch_album_detail", new=mock.AsyncMock(return_value=album)
            ) as detail,
            mock.patch.object(
                self.main, "fetch_album_cover", new=mock.AsyncMock(return_value=cover)
            ) as fetch_cover,
        ):
            card = await self.plugin._album_card("123")
        detail.assert_awaited_once_with(self.plugin, "123")
        fetch_cover.assert_awaited_once_with(self.plugin, "123")
        self.assertEqual(card.chain[0].content, cover)
        self.assertIn("标题: 标题", card.chain[1].text)
        self.assertIn("ID: JM123", card.chain[1].text)
        self.assertIn("tag11", card.chain[1].text)
        self.assertIn("下载: /jm下载 123", card.chain[1].text)
        for unwanted in ("检测到", "识别到", "每日推荐", "来源:"):
            self.assertNotIn(unwanted, card.chain[1].text)
        self.assertNotIn("封面暂不可用", card.chain[1].text)

    async def test_cover_invalid_or_network_failure_becomes_unavailable_text(self):
        album = types.SimpleNamespace(name="标题", tags=["a", "b"])
        with (
            mock.patch.object(
                self.main, "fetch_album_detail", new=mock.AsyncMock(return_value=album)
            ),
            mock.patch.object(
                self.main,
                "fetch_album_cover",
                new=mock.AsyncMock(return_value=b"not-an-image"),
            ),
        ):
            card = await self.plugin._album_card("123")
        self.assertIn("封面暂不可用", card.chain[-1].text)

        with (
            mock.patch.object(
                self.main, "fetch_album_detail", new=mock.AsyncMock(return_value=album)
            ),
            mock.patch.object(
                self.main,
                "fetch_album_cover",
                new=mock.AsyncMock(side_effect=RuntimeError("offline")),
            ),
        ):
            card = await self.plugin._album_card("123")
        self.assertIn("封面暂不可用", card.chain[-1].text)

    async def test_identify_requires_bare_ascii_argument_and_detail_keeps_embedded_extraction(
        self,
    ):
        card = mock.AsyncMock(return_value=self.main.MessageChain(chain=["card"]))
        with mock.patch.object(self.plugin, "_album_card", card):
            replies = await self._texts(
                self.plugin.identify(_LookupEvent(text="/jm识别 jm 123"))
            )
            self.assertEqual(len(replies), 1)
            card.assert_awaited_once_with("123")
            for text in ("/jm识别 hello JM123", "/jm识别 JM123x", "/jm识别"):
                self.assertIn(
                    "用法",
                    (await self._texts(self.plugin.identify(_LookupEvent(text=text))))[
                        0
                    ].text,
                )

            card.reset_mock()
            replies = await self._texts(
                self.plugin.detail(_LookupEvent(text="/jminfo see JM456 now"))
            )
            card.assert_awaited_once_with("456")
            self.assertEqual(len(replies), 1)

    async def test_explicit_commands_work_with_auto_disabled_and_obey_acl(self):
        self.plugin.auto_recognize_jm = False
        card = mock.AsyncMock(return_value=self.main.MessageChain(chain=["card"]))
        with mock.patch.object(self.plugin, "_album_card", card):
            self.assertEqual(
                len(
                    await self._texts(
                        self.plugin.identify(_LookupEvent(text="/jm识别 123"))
                    )
                ),
                1,
            )
            self.assertEqual(
                len(
                    await self._texts(
                        self.plugin.detail(_LookupEvent(text="/jm详情 123"))
                    )
                ),
                1,
            )
        self.plugin.private_whitelist = {"allowed"}
        for operation, command in (
            (self.plugin.identify, "/jm识别 123"),
            (self.plugin.detail, "/jm详情 123"),
        ):
            event = _LookupEvent(sender_id="denied", text=command)
            replies = await self._texts(operation(event))
            self.assertIn("白名单", replies[0].text)

    async def test_missing_title_is_an_explicit_card_error(self):
        with mock.patch.object(
            self.main,
            "fetch_album_detail",
            new=mock.AsyncMock(return_value=types.SimpleNamespace(name="", tags=[])),
        ):
            replies = await self._texts(
                self.plugin.detail(_LookupEvent(text="/jm详情 123"))
            )
        self.assertIn("缺少标题", replies[0].text)

    async def test_explicit_command_errors_are_visible_and_do_not_touch_daily_state(
        self,
    ):
        recommendation = importlib.import_module("recommendation")
        self.plugin.recommendation.state = recommendation.DailyPick(
            "2026-09-28",
            "99",
            title="existing",
            attempted_targets=["a"],
            sent_targets=["a"],
        )
        self.plugin.recommendation.cache_dir.mkdir(parents=True, exist_ok=True)
        marker = self.plugin.recommendation.cache_dir / "marker.jpg"
        marker.write_bytes(b"keep")
        self.plugin.get_kv_data = mock.AsyncMock()
        self.plugin.put_kv_data = mock.AsyncMock()
        before = copy.deepcopy(
            (
                dict(self.plugin.tasks),
                dict(self.plugin.query_sessions),
                self.plugin.recommendation.state,
                marker.read_bytes(),
            )
        )
        with mock.patch.object(
            self.plugin,
            "_album_card",
            new=mock.AsyncMock(side_effect=RuntimeError("offline")),
        ):
            identify = await self._texts(
                self.plugin.identify(_LookupEvent(text="/jm识别 123"))
            )
            detail = await self._texts(
                self.plugin.detail(_LookupEvent(text="/jm详情 123"))
            )
        self.assertIn("offline", identify[0].text)
        self.assertIn("offline", detail[0].text)
        with (
            mock.patch.object(
                self.main,
                "fetch_album_detail",
                new=mock.AsyncMock(
                    return_value=types.SimpleNamespace(name="new", tags=[])
                ),
            ),
            mock.patch.object(
                self.main,
                "fetch_album_cover",
                new=mock.AsyncMock(return_value=_valid_cover()),
            ),
        ):
            for operation, text in (
                (self.plugin.identify, "/jm识别 123"),
                (self.plugin.detail, "/jm详情 123"),
                (self.plugin.on_jm_message, "看看 JM123"),
            ):
                replies = await self._texts(operation(_LookupEvent(text=text)))
                self.assertEqual(len(replies), 1)
                self.assertEqual(len(replies[0]), 2)
                self.assertIn("暂无标签", replies[0][-1].text)
                self.assertIn("ID: JM123", replies[0][-1].text)
        self.assertEqual(
            (
                self.plugin.tasks,
                self.plugin.query_sessions,
                self.plugin.recommendation.state,
                marker.read_bytes(),
            ),
            before,
        )
        self.plugin.get_kv_data.assert_not_awaited()
        self.plugin.put_kv_data.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
