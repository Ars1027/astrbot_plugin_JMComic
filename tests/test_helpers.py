import asyncio
import base64
import hashlib
import importlib
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


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
    class EventMessageType:
        ALL = "all"

    @staticmethod
    def command(*_args, **_kwargs):
        return lambda func: func

    @staticmethod
    def event_message_type(*_args, **_kwargs):
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
        self.stopped = False
        self.unified_msg_origin = (
            f"group:{group_id}" if group_id else f"private:{sender_id}"
        )

    def get_sender_id(self):
        return self._sender_id

    def get_group_id(self):
        return self._group_id

    def stop_event(self):
        self.stopped = True

    def plain_result(self, text):
        return _Plain(text)


class _FakePage:
    def __init__(self, items, page=1, page_count=1, total=None):
        self.items = items
        self.page = page
        self.page_count = page_count
        self.total = total

    def iter_id_title_tag(self):
        return iter(self.items)


class _FakeAlbum:
    def __init__(self, tags):
        self.tags = tags


class _FakeHttpResponse:
    def __init__(self, body, status=200):
        self.body = body
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def json(self, content_type=None):
        return self.body

    async def text(self):
        return str(self.body)


class _FakeHttpSession:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def post(self, url, json):
        self.calls.append((url, json))
        result = self.handler(url, json)
        if isinstance(result, Exception):
            raise result
        if isinstance(result, tuple):
            body, status = result
            return _FakeHttpResponse(body, status)
        return _FakeHttpResponse(result)


async def _collect_plain(async_iter):
    result = []
    async for item in async_iter:
        result.append(item.text)
    return result


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

    def _install_fake_tag_client(self, plugin, tags_by_id, fail_ids=None):
        calls = []
        fail_ids = {str(item) for item in (fail_ids or set())}

        class _FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def get_album_detail(self, album_id):
                album_id = str(album_id)
                calls.append(album_id)
                if album_id in fail_ids:
                    raise RuntimeError(f"detail failed {album_id}")
                return _FakeAlbum(tags_by_id.get(album_id, []))

        class _FakeOption:
            def new_jm_async_client(self, max_clients=3):
                return _FakeClient()

        plugin._build_option = lambda _base_dir: _FakeOption()
        return calls

    def _install_fake_http_session(self, plugin, handler):
        session = _FakeHttpSession(handler)
        plugin._create_napcat_http_session = lambda: session
        return session

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
            self.assertEqual(plugin.file_delivery_mode, "auto")
            self.assertEqual(plugin.max_base64_file_mb, 80)
            self.assertEqual(plugin.napcat_http_api_base, "")
            self.assertEqual(plugin.napcat_http_timeout_seconds, 900)
            self.assertEqual(plugin.stream_chunk_kb, 512)
            self.assertEqual(plugin.stream_file_retention_seconds, 1800)
            self.assertEqual(plugin.stream_chunk_retries, 2)
            self.assertEqual(plugin.search_page_size, 10)
            self.assertEqual(plugin.search_result_tag_limit, 5)
            self.assertEqual(plugin.search_more_ttl_seconds, 600)
            self.assertTrue(plugin.search_enrich_tags)
            self.assertEqual(plugin.search_enrich_tag_concurrency, 3)

    def test_search_results_show_tags_for_multiple_items(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = self._plugin({"download": {"download_base_dir": tmp}})

            async def fake_fetch(query, page):
                self.assertEqual(query, "猫")
                self.assertEqual(page, 1)
                return _FakePage(
                    [
                        ("101", "title one", ["tag1", "tag2"]),
                        ("102", "title two", ["tag3"]),
                    ]
                )

            plugin._fetch_search_page = fake_fetch
            event = _FakeEvent(text="/jm搜索 猫")
            texts = asyncio.run(_collect_plain(plugin.search(event)))

            self.assertIn("JMComic 搜索: 猫", texts[0])
            self.assertIn("JM101", texts[0])
            self.assertIn("标签: tag1, tag2", texts[0])
            self.assertIn("标签: tag3", texts[0])

    def test_single_search_result_shows_all_tags(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = self._plugin(
                {
                    "download": {"download_base_dir": tmp},
                    "query": {"search_result_tag_limit": 2},
                }
            )

            async def fake_fetch(_query, _page):
                return _FakePage(
                    [("101", "only title", ["a", "b", "c", "d"])],
                )

            plugin._fetch_search_page = fake_fetch
            event = _FakeEvent(text="/jm搜索 only")
            texts = asyncio.run(_collect_plain(plugin.search(event)))

            self.assertIn("标签: a, b, c, d", texts[0])
            self.assertNotIn("等", texts[0])

    def test_api_style_empty_tags_are_enriched_from_detail(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = self._plugin({"download": {"download_base_dir": tmp}})
            calls = self._install_fake_tag_client(
                plugin,
                {"101": ["补全A", "补全B"], "102": ["补全C"]},
            )

            async def fake_fetch(_query, _page):
                return _FakePage(
                    [
                        ("101", "title one", []),
                        ("102", "title two", []),
                    ]
                )

            plugin._fetch_search_page = fake_fetch
            event = _FakeEvent(text="/jm搜索 keyword")
            texts = asyncio.run(_collect_plain(plugin.search(event)))

            self.assertEqual(calls, ["101", "102"])
            self.assertIn("标签: 补全A, 补全B", texts[0])
            self.assertIn("标签: 补全C", texts[0])

    def test_tag_enrichment_only_fetches_visible_chunk(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = self._plugin(
                {
                    "download": {"download_base_dir": tmp},
                    "query": {"search_page_size": 2},
                }
            )
            calls = self._install_fake_tag_client(
                plugin,
                {"101": ["tag1"], "102": ["tag2"], "103": ["tag3"]},
            )

            async def fake_fetch(_query, _page):
                return _FakePage(
                    [
                        ("101", "title one", []),
                        ("102", "title two", []),
                        ("103", "title three", []),
                    ]
                )

            plugin._fetch_search_page = fake_fetch
            event = _FakeEvent(text="/jm搜索 keyword")
            texts = asyncio.run(_collect_plain(plugin.search(event)))

            self.assertEqual(calls, ["101", "102"])
            self.assertIn("JM101", texts[0])
            self.assertIn("标签: tag1", texts[0])
            self.assertNotIn("JM103", texts[0])

    def test_more_enriches_next_chunk_tags(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = self._plugin(
                {
                    "download": {"download_base_dir": tmp},
                    "query": {"search_page_size": 1},
                }
            )
            calls = self._install_fake_tag_client(
                plugin,
                {"101": ["first"], "102": ["second"]},
            )

            async def fake_fetch(_query, _page):
                return _FakePage(
                    [
                        ("101", "title one", []),
                        ("102", "title two", []),
                    ]
                )

            plugin._fetch_search_page = fake_fetch
            search_event = _FakeEvent(text="/jm搜索 keyword")
            asyncio.run(_collect_plain(plugin.search(search_event)))
            more_event = _FakeEvent(text="更多")
            texts = asyncio.run(_collect_plain(plugin.on_more_message(more_event)))

            self.assertEqual(calls, ["101", "102"])
            self.assertIn("JM102", texts[0])
            self.assertIn("标签: second", texts[0])

    def test_tag_enrichment_can_be_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = self._plugin(
                {
                    "download": {"download_base_dir": tmp},
                    "query": {"search_enrich_tags": False},
                }
            )
            calls = self._install_fake_tag_client(plugin, {"101": ["hidden"]})

            async def fake_fetch(_query, _page):
                return _FakePage([("101", "title one", [])])

            plugin._fetch_search_page = fake_fetch
            event = _FakeEvent(text="/jm搜索 keyword")
            texts = asyncio.run(_collect_plain(plugin.search(event)))

            self.assertEqual(calls, [])
            self.assertIn("标签: -", texts[0])
            self.assertNotIn("hidden", texts[0])

    def test_tag_enrichment_failure_keeps_search_successful(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = self._plugin({"download": {"download_base_dir": tmp}})
            calls = self._install_fake_tag_client(
                plugin,
                {"102": ["ok"]},
                fail_ids={"101"},
            )

            async def fake_fetch(_query, _page):
                return _FakePage(
                    [
                        ("101", "broken", []),
                        ("102", "healthy", []),
                    ]
                )

            plugin._fetch_search_page = fake_fetch
            event = _FakeEvent(text="/jm搜索 keyword")
            texts = asyncio.run(_collect_plain(plugin.search(event)))

            self.assertEqual(calls, ["101", "102"])
            self.assertIn("JM101", texts[0])
            self.assertIn("标签: -", texts[0])
            self.assertIn("标签: ok", texts[0])

    def test_search_page_size_and_more_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = self._plugin(
                {
                    "download": {"download_base_dir": tmp},
                    "query": {"search_page_size": 2},
                }
            )

            async def fake_fetch(_query, _page):
                return _FakePage(
                    [
                        ("101", "title one", ["tag1"]),
                        ("102", "title two", ["tag2"]),
                        ("103", "title three", ["tag3"]),
                    ]
                )

            plugin._fetch_search_page = fake_fetch
            event = _FakeEvent(text="/jm搜索 keyword")
            texts = asyncio.run(_collect_plain(plugin.search(event)))

            self.assertIn("JM101", texts[0])
            self.assertIn("JM102", texts[0])
            self.assertNotIn("JM103", texts[0])
            self.assertIn("更多", texts[0])

            more_event = _FakeEvent(text="更多")
            texts = asyncio.run(_collect_plain(plugin.on_more_message(more_event)))
            self.assertIn("JM103", texts[0])
            self.assertTrue(more_event.stopped)

    def test_more_fetches_next_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = self._plugin(
                {
                    "download": {"download_base_dir": tmp},
                    "query": {"search_page_size": 1},
                }
            )
            pages = []

            async def fake_fetch(_query, page):
                pages.append(page)
                if page == 1:
                    return _FakePage([("101", "page one", [])], page=1, page_count=2)
                return _FakePage([("102", "page two", [])], page=2, page_count=2)

            plugin._fetch_search_page = fake_fetch
            search_event = _FakeEvent(text="/jm搜索 keyword")
            self._install_fake_tag_client(plugin, {"101": [], "102": []})
            asyncio.run(_collect_plain(plugin.search(search_event)))

            more_event = _FakeEvent(text="/jm更多")
            texts = asyncio.run(_collect_plain(plugin.more(more_event)))

            self.assertEqual(pages, [1, 2])
            self.assertIn("JM102", texts[0])

    def test_ranking_defaults_to_week_all(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = self._plugin({"download": {"download_base_dir": tmp}})
            calls = []

            async def fake_fetch(period, category, page):
                calls.append((period, category, page))
                return _FakePage([("201", "ranking title", ["hot"])])

            plugin._fetch_ranking_page = fake_fetch
            event = _FakeEvent(text="/jm热门")
            texts = asyncio.run(_collect_plain(plugin.ranking(event)))

            self.assertEqual(calls, [("week", "0", 1)])
            self.assertIn("JMComic 热门榜: 周榜 / 全部", texts[0])
            self.assertIn("JM201", texts[0])

    def test_ranking_accepts_period_category_and_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = self._plugin({"download": {"download_base_dir": tmp}})
            calls = []

            async def fake_fetch(period, category, page):
                calls.append((period, category, page))
                return _FakePage([("202", "ranking title", ["hot"])], page=2, page_count=3)

            plugin._fetch_ranking_page = fake_fetch
            event = _FakeEvent(text="/jm热门 日 同人 2")
            texts = asyncio.run(_collect_plain(plugin.ranking(event)))

            self.assertEqual(calls, [("day", "doujin", 2)])
            self.assertIn("JMComic 热门榜: 日榜 / 同人", texts[0])
            self.assertIn("第 2/3 页", texts[0])

    def test_ranking_rejects_unknown_category(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = self._plugin({"download": {"download_base_dir": tmp}})

            async def fake_fetch(_period, _category, _page):
                raise AssertionError("ranking fetch should not be called")

            plugin._fetch_ranking_page = fake_fetch
            event = _FakeEvent(text="/jm热门 不存在")
            texts = asyncio.run(_collect_plain(plugin.ranking(event)))

            self.assertIn("未知热门榜分类或时间参数", texts[0])
            self.assertIn("支持分类", texts[0])

    def test_account_commands_are_not_implemented(self):
        command_names = [
            name
            for name in dir(self.main.JMComicPlugin)
            if "favorite" in name.lower() or "login" in name.lower() or "收藏" in name
        ]
        self.assertEqual(command_names, [])

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
            self.assertEqual(plugin.file_delivery_mode, "auto")

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

    def test_auto_large_file_uses_napcat_http_stream(self):
        bot = _Bot()
        context = _Context()
        with tempfile.TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "large.pdf"
            file_bytes = b"stream-data-" * 100000
            output_path.write_bytes(file_bytes)
            plugin = self._plugin(
                {
                    "download": {
                        "download_base_dir": tmp,
                        "max_base64_file_mb": 1,
                        "napcat_http_api_base": "http://napcat:3000/",
                        "napcat_http_access_token": "secret",
                        "stream_chunk_kb": 512,
                    }
                },
                context,
            )

            def handler(url, payload):
                action = url.rsplit("/", 1)[-1]
                if action == "upload_file_stream" and payload.get("is_complete"):
                    return {
                        "status": "ok",
                        "retcode": 0,
                        "data": {
                            "status": "file_complete",
                            "file_path": "/napcat/tmp/JM438516.pdf",
                        },
                    }
                if action == "upload_file_stream":
                    return {
                        "status": "ok",
                        "retcode": 0,
                        "data": {"status": "chunk_received"},
                    }
                return {
                    "status": "ok",
                    "retcode": 0,
                    "data": {"file_id": "file-1"},
                }

            session = self._install_fake_http_session(plugin, handler)
            with mock.patch.object(
                Path,
                "read_bytes",
                side_effect=AssertionError("stream mode must not use read_bytes"),
            ):
                asyncio.run(
                    plugin._send_file_or_path(
                        self._sample_task(str(output_path), bot=bot)
                    )
                )

            self.assertEqual(bot.calls, [])
            chunk_calls = [
                payload
                for url, payload in session.calls
                if url.endswith("/upload_file_stream")
                and not payload.get("is_complete")
            ]
            self.assertEqual([item["chunk_index"] for item in chunk_calls], [0, 1, 2])
            self.assertTrue(all(item["total_chunks"] == 3 for item in chunk_calls))
            self.assertTrue(
                all(
                    len(base64.b64decode(item["chunk_data"])) <= 512 * 1024
                    for item in chunk_calls
                )
            )
            expected_hash = hashlib.sha256(file_bytes).hexdigest()
            self.assertTrue(
                all(item["expected_sha256"] == expected_hash for item in chunk_calls)
            )
            group_calls = [
                payload
                for url, payload in session.calls
                if url.endswith("/upload_group_file")
            ]
            self.assertEqual(group_calls[0]["group_id"], "123")
            self.assertEqual(group_calls[0]["file"], "/napcat/tmp/JM438516.pdf")
            self.assertEqual(group_calls[0]["name"], "JM438516_abc12345.pdf")
            self.assertEqual(
                plugin._napcat_http_headers(),
                {"Authorization": "Bearer secret"},
            )
            self.assertEqual(len(context.messages), 2)
            self.assertIn("HTTP Stream", context.messages[0][1].chain[0])
            self.assertIn("文件已上传到群文件", context.messages[1][1].chain[0])

    def test_forced_napcat_stream_does_not_use_base64_for_small_file(self):
        bot = _Bot()
        context = _Context()
        with tempfile.TemporaryDirectory() as tmp:
            output = tempfile.NamedTemporaryFile(suffix=".pdf", dir=tmp, delete=False)
            output.write(b"pdf")
            output.close()
            plugin = self._plugin(
                {
                    "download": {
                        "download_base_dir": tmp,
                        "file_delivery_mode": "napcat_http_stream",
                        "napcat_http_api_base": "http://napcat:3000",
                    }
                },
                context,
            )

            def handler(url, payload):
                if url.endswith("/upload_file_stream") and payload.get("is_complete"):
                    return {
                        "status": "ok",
                        "retcode": 0,
                        "data": {
                            "status": "file_complete",
                            "file_path": "/napcat/tmp/small.pdf",
                        },
                    }
                return {"status": "ok", "retcode": 0, "data": {}}

            session = self._install_fake_http_session(plugin, handler)
            asyncio.run(
                plugin._send_file_or_path(self._sample_task(output.name, bot=bot))
            )

            self.assertEqual(bot.calls, [])
            self.assertTrue(
                any(url.endswith("/upload_file_stream") for url, _ in session.calls)
            )

    def test_stream_chunk_retries_failed_chunk(self):
        context = _Context()
        with tempfile.TemporaryDirectory() as tmp:
            output = tempfile.NamedTemporaryFile(suffix=".pdf", dir=tmp, delete=False)
            output.write(b"x" * (70 * 1024))
            output.close()
            plugin = self._plugin(
                {
                    "download": {
                        "download_base_dir": tmp,
                        "file_delivery_mode": "napcat_http_stream",
                        "napcat_http_api_base": "http://napcat:3000",
                        "stream_chunk_kb": 64,
                        "stream_chunk_retries": 1,
                    }
                },
                context,
            )
            attempts = {0: 0}

            def handler(url, payload):
                if url.endswith("/upload_file_stream") and "chunk_index" in payload:
                    index = payload["chunk_index"]
                    if index == 0:
                        attempts[0] += 1
                        if attempts[0] == 1:
                            return TimeoutError("temporary timeout")
                    return {
                        "status": "ok",
                        "retcode": 0,
                        "data": {"status": "chunk_received"},
                    }
                if url.endswith("/upload_file_stream"):
                    return {
                        "status": "ok",
                        "retcode": 0,
                        "data": {
                            "status": "file_complete",
                            "file_path": "/napcat/tmp/retry.pdf",
                        },
                    }
                return {"status": "ok", "retcode": 0, "data": {}}

            self._install_fake_http_session(plugin, handler)
            asyncio.run(plugin._send_file_or_path(self._sample_task(output.name)))

            self.assertEqual(attempts[0], 2)
            self.assertIn("文件已上传到群文件", context.messages[-1][1].chain[0])

    def test_stream_group_upload_failure_reports_stage_and_local_path(self):
        context = _Context()
        with tempfile.TemporaryDirectory() as tmp:
            output = tempfile.NamedTemporaryFile(suffix=".pdf", dir=tmp, delete=False)
            output.write(b"pdf")
            output.close()
            plugin = self._plugin(
                {
                    "download": {
                        "download_base_dir": tmp,
                        "file_delivery_mode": "napcat_http_stream",
                        "napcat_http_api_base": "http://napcat:3000",
                    }
                },
                context,
            )

            def handler(url, payload):
                if url.endswith("/upload_file_stream") and payload.get("is_complete"):
                    return {
                        "status": "ok",
                        "retcode": 0,
                        "data": {
                            "status": "file_complete",
                            "file_path": "/napcat/tmp/fail.pdf",
                        },
                    }
                if url.endswith("/upload_group_file"):
                    return TimeoutError("QQ upload timeout")
                return {"status": "ok", "retcode": 0, "data": {}}

            self._install_fake_http_session(plugin, handler)
            asyncio.run(plugin._send_file_or_path(self._sample_task(output.name)))

            text = context.messages[-1][1].chain[0]
            self.assertIn("NapCat 上传 QQ 群文件失败", text)
            self.assertIn("QQ upload timeout", text)
            self.assertIn(output.name, text)

    def test_stream_chunk_failure_exhausts_retries_and_keeps_local_file(self):
        context = _Context()
        with tempfile.TemporaryDirectory() as tmp:
            output = tempfile.NamedTemporaryFile(suffix=".pdf", dir=tmp, delete=False)
            output.write(b"pdf")
            output.close()
            plugin = self._plugin(
                {
                    "download": {
                        "download_base_dir": tmp,
                        "file_delivery_mode": "napcat_http_stream",
                        "napcat_http_api_base": "http://napcat:3000",
                        "stream_chunk_retries": 2,
                    }
                },
                context,
            )

            def handler(url, payload):
                if url.endswith("/upload_file_stream") and "chunk_index" in payload:
                    return {
                        "status": "failed",
                        "retcode": 200,
                        "message": "stream unsupported",
                    }
                raise AssertionError("finalize and group upload must not be called")

            session = self._install_fake_http_session(plugin, handler)
            asyncio.run(plugin._send_file_or_path(self._sample_task(output.name)))

            chunk_calls = [
                item for item in session.calls if item[0].endswith("/upload_file_stream")
            ]
            self.assertEqual(len(chunk_calls), 3)
            text = context.messages[-1][1].chain[0]
            self.assertIn("文件分块传输到 NapCat 失败", text)
            self.assertIn("stream unsupported", text)
            self.assertIn(output.name, text)

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
            text = context.messages[0][1].chain[0]
            self.assertIn(output.name, text)
            self.assertIn("napcat_http_api_base", text)

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
