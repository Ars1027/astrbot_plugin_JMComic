import asyncio
import base64
import copy
import importlib
import io
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock
from zoneinfo import ZoneInfo

from PIL import Image

from test_helpers import _FakeEvent, _FakePage, _install_astrbot_stubs


class _Image:
    def __init__(self, content):
        self.file = "base64://" + base64.b64encode(content).decode()

    @staticmethod
    def fromBytes(content):
        return _Image(content)


class _Event(_FakeEvent):
    def is_admin(self):
        return getattr(self, "role", "member") == "admin"

    def chain_result(self, chain):
        return chain


class RecommendationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _install_astrbot_stubs()
        self.main = importlib.import_module("main")
        self.module = importlib.import_module("recommendation")
        cards = importlib.import_module("album_card")
        patcher = mock.patch.object(cards.Comp, "Image", _Image, create=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.storage = {}
        self.clock = datetime(2026, 9, 11, 8, tzinfo=ZoneInfo("Asia/Shanghai"))
        self.config = {"download": {"download_base_dir": self.tmp.name}}
        buffer = io.BytesIO()
        Image.new("RGB", (2, 3), "white").save(buffer, format="JPEG")
        self.cover = buffer.getvalue()
        self.plugin = self.make_plugin()
        self.service = self.plugin.recommendation

    def make_plugin(self, config=None):
        plugin = self.main.JMComicPlugin(
            context=types.SimpleNamespace(
                send_message=mock.AsyncMock(return_value=True)
            ),
            config=config or self.config,
        )

        async def load(key, default):
            return copy.deepcopy(self.storage.get(key, default))

        async def save(key, value):
            self.storage[key] = copy.deepcopy(value)

        plugin.get_kv_data = mock.AsyncMock(side_effect=load)
        plugin.put_kv_data = mock.AsyncMock(side_effect=save)
        plugin._fetch_recommendation_page = mock.AsyncMock(
            return_value=_FakePage([(str(i), f"标题{i}", []) for i in range(1, 21)])
        )
        service = plugin.recommendation
        service.now = lambda: self.clock
        service._fetch_detail = mock.AsyncMock(
            side_effect=lambda album_id: types.SimpleNamespace(
                name=f"详情{album_id}", tags=["标签一", "标签二"]
            )
        )
        service._fetch_cover = mock.AsyncMock(return_value=self.cover)
        return plugin

    def saved(self):
        return self.storage[self.module.KV_RECOMMENDATION_KEY]

    async def test_default_is_weekly_most_viewed_with_hanman_excluded(self):
        state, cover = await self.service.get_today()
        self.plugin._fetch_recommendation_page.assert_awaited_once_with(1, "mv", "week")
        self.assertEqual(state.source_period, "week")
        self.assertEqual(state.source_excluded_tags, ["韩漫"])
        text = self.service.message(state, cover).chain[-1].text
        self.assertIn("周榜", text)
        self.assertIn("Most Viewed", text)
        self.assertIn("已排除标签: 韩漫", text)

    async def test_filter_uses_details_and_refills_after_fully_excluded_page(self):
        self.service.top_n = 2
        self.plugin._fetch_recommendation_page.side_effect = [
            _FakePage([("1", "a", ["韓漫"]), ("2", "b", ["韩漫"])], page_count=3),
            _FakePage([("3", "c", []), ("4", "d", ["剧情"])], page=2, page_count=3),
            _FakePage([("4", "d", []), ("5", "e", [])], page=3, page_count=3),
        ]
        self.service._fetch_detail.side_effect = lambda album_id: types.SimpleNamespace(
            name=f"详情{album_id}",
            tags=["韓漫"] if album_id == "4" else ["剧情"],
        )
        with mock.patch.object(
            self.module.random, "choice", side_effect=lambda rows: rows[-1]
        ) as choose:
            state, _ = await self.service.get_today()
        self.assertEqual([row.album_id for row in choose.call_args.args[0]], ["3", "5"])
        self.assertEqual(state.album_id, "5")
        self.assertEqual(state.title, "详情5")
        self.assertEqual(state.tags, ["剧情"])
        self.assertEqual(
            self.service._fetch_detail.await_args_list,
            [mock.call("3"), mock.call("4"), mock.call("5")],
        )
        self.assertEqual(self.plugin._fetch_recommendation_page.await_count, 3)

    async def test_repeated_excluded_page_stops_and_push_sends_nothing(self):
        self.plugin._fetch_recommendation_page.return_value = _FakePage(
            [("1", "a", [])], page_count=None
        )
        self.service._fetch_detail.side_effect = lambda _: types.SimpleNamespace(
            name="标题", tags=["韩漫"]
        )
        self.service.targets = ["bot:FriendMessage:456"]
        with self.assertRaisesRegex(ValueError, "暂无"):
            await self.service.push_today()
        self.assertEqual(self.plugin._fetch_recommendation_page.await_count, 2)
        self.service._fetch_detail.assert_awaited_once_with("1")
        self.assertEqual(self.storage, {})
        self.service._fetch_cover.assert_not_awaited()
        self.plugin.context.send_message.assert_not_awaited()

    async def test_hanman_category_is_excluded_when_detail_tags_are_empty(self):
        page = _FakePage([("1", "a", []), ("2", "b", []), ("3", "c", [])])
        page.content = [
            ("1", {"name": "a", "category": {"id": "5", "title": "韓漫"}}),
            ("2", {"name": "b", "category_sub": {"id": "hanman"}}),
            ("3", {"name": "c", "category": {"id": "1", "title": "同人"}}),
        ]
        self.plugin._fetch_recommendation_page.return_value = page
        self.service._fetch_detail.side_effect = lambda _: types.SimpleNamespace(
            name="详情", tags=[]
        )
        state, _ = await self.service.get_today()
        self.assertEqual(state.album_id, "3")
        self.assertEqual(state.tags, [])
        self.service._fetch_detail.assert_awaited_once_with("3")

    async def test_custom_exclusion_is_exact_and_empty_list_disables_filter(self):
        plugin = self.make_plugin(
            {**self.config, "recommendation": {"exclude_tags": [" TAG ", "韓漫"]}}
        )
        plugin._fetch_recommendation_page.return_value = _FakePage(
            [("1", "a", ["tag"]), ("2", "b", ["韩漫"]), ("3", "c", [])]
        )
        plugin.recommendation._fetch_detail.side_effect = lambda _: (
            types.SimpleNamespace(name="标题中有韩漫", tags=["tag-extra", "韩漫风格"])
        )
        state, _ = await plugin.recommendation.get_today()
        self.assertEqual(state.album_id, "3")
        self.storage.clear()
        unfiltered = self.make_plugin(
            {**self.config, "recommendation": {"exclude_tags": []}}
        )
        unfiltered._fetch_recommendation_page.return_value = _FakePage(
            [("1", "a", ["韩漫"])]
        )
        unfiltered.recommendation._fetch_detail.side_effect = lambda _: (
            types.SimpleNamespace(name="标题", tags=["韓漫"])
        )
        state, _ = await unfiltered.recommendation.get_today()
        self.assertEqual(state.album_id, "1")
        self.assertEqual(state.source_excluded_tags, [])

    async def test_time_range_is_configurable_and_kept_until_reset(self):
        for label, period in (
            ("日榜", "day"),
            ("周榜", "week"),
            ("月榜", "month"),
            ("全部时间", "all"),
        ):
            with self.subTest(label=label):
                self.storage.clear()
                plugin = self.make_plugin(
                    {**self.config, "recommendation": {"time_range": label}}
                )
                state, _ = await plugin.recommendation.get_today()
                plugin._fetch_recommendation_page.assert_awaited_once_with(
                    1, "mv", period
                )
                self.assertEqual(state.source_period, period)
        changed = self.make_plugin(
            {**self.config, "recommendation": {"time_range": "日榜"}}
        )
        state, _ = await changed.recommendation.get_today()
        self.assertEqual(state.source_period, "all")
        changed._fetch_recommendation_page.assert_not_awaited()
        await changed.recommendation.reset_today()
        state, _ = await changed.recommendation.get_today()
        self.assertEqual(state.source_period, "day")

    async def test_old_all_time_snapshot_preserves_source_and_blocks_excluded_pick(
        self,
    ):
        self.storage[self.module.KV_RECOMMENDATION_KEY] = {
            "day": self.clock.date().isoformat(),
            "album_id": "1",
            "title": "旧推荐",
            "tags": ["剧情"],
            "details_loaded": True,
            "source_order": "mv",
        }
        state, cover = await self.service.get_today()
        self.assertEqual(state.source_period, "all")
        self.assertEqual(state.source_excluded_tags, [])
        self.assertIn("全部时间", self.service.message(state, cover).chain[-1].text)
        self.plugin._fetch_recommendation_page.assert_not_awaited()
        self.saved()["tags"] = ["韓漫"]
        restarted = self.make_plugin()
        with self.assertRaisesRegex(ValueError, "jm重置推荐"):
            await restarted.recommendation.get_today()
        restarted.recommendation._fetch_cover.assert_not_awaited()
        restarted._fetch_recommendation_page.assert_not_awaited()

    async def test_changed_exclusions_do_not_relabel_saved_pick_until_reset(self):
        original, _ = await self.service.get_today()
        changed = self.make_plugin(
            {**self.config, "recommendation": {"exclude_tags": ["其他标签"]}}
        )
        state, cover = await changed.recommendation.get_today()
        self.assertEqual(state, original)
        self.assertIn(
            "已排除标签: 韩漫",
            changed.recommendation.message(state, cover).chain[-1].text,
        )
        await changed.recommendation.reset_today()
        state, _ = await changed.recommendation.get_today()
        self.assertEqual(state.source_excluded_tags, ["其他标签"])

    async def test_invalid_sort_and_time_range_use_hot_weekly_defaults(self):
        plugin = self.make_plugin(
            {
                **self.config,
                "recommendation": {"order_by": "invalid", "time_range": "invalid"},
            }
        )
        state, _ = await plugin.recommendation.get_today()
        plugin._fetch_recommendation_page.assert_awaited_once_with(1, "mv", "week")
        self.assertEqual(state.source_period, "week")
        self.assertEqual(state.source_order, "mv")

    async def test_candidate_detail_failure_does_not_save_unverified_pick(self):
        async def detail(album_id):
            if album_id != "1":
                raise RuntimeError("detail offline")
            return types.SimpleNamespace(name="安全候选", tags=["剧情"])

        self.service._fetch_detail.side_effect = detail
        with self.assertRaisesRegex(RuntimeError, "offline"):
            await self.service.get_today()
        self.assertEqual(self.storage, {})
        self.service._fetch_cover.assert_not_awaited()

    async def test_candidates_accept_arbitrary_positive_n(self):
        for count in (1, 3, 7, 10, 19):
            with self.subTest(count=count):
                self.storage.clear()
                plugin = self.make_plugin(
                    {**self.config, "recommendation": {"top_n": count}}
                )
                with mock.patch.object(
                    self.module.random, "choice", side_effect=lambda items: items[-1]
                ) as choose:
                    state, _ = await plugin.recommendation.get_today()
                candidates = choose.call_args.args[0]
                self.assertEqual(
                    [x.album_id for x in candidates],
                    [str(i) for i in range(1, count + 1)],
                )
                self.assertEqual(state.album_id, str(count))
                plugin._fetch_recommendation_page.assert_awaited_once_with(
                    1, "mv", "week"
                )

    async def test_short_and_empty_rankings(self):
        self.plugin._fetch_recommendation_page.return_value = _FakePage([])
        with self.assertRaisesRegex(ValueError, "暂无"):
            await self.service.get_today()
        self.assertEqual(self.storage, {})
        self.plugin._fetch_recommendation_page.return_value = _FakePage(
            [("17", "短榜", [])]
        )
        state, _ = await self.service.get_today()
        self.assertEqual(state.album_id, "17")

    async def test_large_n_fetches_multiple_pages_and_trims_last_page(self):
        self.service.top_n = 5
        self.plugin._fetch_recommendation_page.side_effect = [
            _FakePage([("1", "a", []), ("2", "b", [])], page_count=3),
            _FakePage([("3", "c", []), ("4", "d", [])], page=2, page_count=3),
            _FakePage([("5", "e", []), ("6", "f", [])], page=3, page_count=3),
        ]
        with mock.patch.object(
            self.module.random, "choice", side_effect=lambda rows: rows[-1]
        ) as choose:
            state, _ = await self.service.get_today()
        self.assertEqual(state.album_id, "5")
        self.assertEqual(
            [row.album_id for row in choose.call_args.args[0]],
            ["1", "2", "3", "4", "5"],
        )
        self.assertEqual(
            self.plugin._fetch_recommendation_page.await_args_list,
            [
                mock.call(1, "mv", "week"),
                mock.call(2, "mv", "week"),
                mock.call(3, "mv", "week"),
            ],
        )

    async def test_repeated_page_stops_without_duplicate_candidates(self):
        self.service.top_n = 100
        page = _FakePage([("1", "a", []), ("2", "b", [])], page_count=None)
        self.plugin._fetch_recommendation_page.return_value = page
        with mock.patch.object(
            self.module.random, "choice", side_effect=lambda rows: rows[0]
        ) as choose:
            await self.service.get_today()
        self.assertEqual(len(choose.call_args.args[0]), 2)
        self.assertEqual(self.plugin._fetch_recommendation_page.await_count, 2)

    async def test_next_page_failure_does_not_select_from_incomplete_range(self):
        self.service.top_n = 100
        self.plugin._fetch_recommendation_page.side_effect = [
            _FakePage([("1", "a", [])], page_count=2),
            RuntimeError("page 2 offline"),
        ]
        with self.assertRaisesRegex(RuntimeError, "page 2"):
            await self.service.get_today()
        self.assertEqual(self.storage, {})

    async def test_top_n_is_positive_integer_and_legacy_strings_still_work(self):
        for value in (0, -1, 2.5, True, "", "bad"):
            with self.subTest(value=value):
                plugin = self.make_plugin(
                    {**self.config, "recommendation": {"top_n": value}}
                )
                self.assertEqual(plugin.recommendation.top_n, 10)
        for value in (1, 50, "100", 10000):
            with self.subTest(value=value):
                plugin = self.make_plugin(
                    {**self.config, "recommendation": {"top_n": value}}
                )
                self.assertEqual(plugin.recommendation.top_n, int(value))

    async def test_sort_config_is_used_and_saved_until_reset(self):
        plugin = self.make_plugin(
            {**self.config, "recommendation": {"order_by": "Most Viewed"}}
        )
        state, cover = await plugin.recommendation.get_today()
        plugin._fetch_recommendation_page.assert_awaited_once_with(1, "mv", "week")
        self.assertEqual(state.source_order, "mv")
        changed = self.make_plugin(
            {**self.config, "recommendation": {"order_by": "Most Recent"}}
        )
        retained, _ = await changed.recommendation.get_today()
        self.assertIn(
            "Most Viewed",
            changed.recommendation.message(retained, cover).chain[-1].text,
        )
        changed._fetch_recommendation_page.assert_not_awaited()
        await changed.recommendation.reset_today()
        updated, _ = await changed.recommendation.get_today()
        self.assertEqual(updated.source_order, "mr")
        self.assertEqual(updated.source_period, "all")
        changed._fetch_recommendation_page.assert_awaited_once_with(1, "mr", "all")

    async def test_latest_sort_keeps_all_time_for_each_configured_range(self):
        for label in ("日榜", "周榜", "月榜", "全部时间"):
            with self.subTest(label=label):
                self.storage.clear()
                plugin = self.make_plugin(
                    {
                        **self.config,
                        "recommendation": {
                            "order_by": "Most Recent",
                            "time_range": label,
                        },
                    }
                )
                state, cover = await plugin.recommendation.get_today()
                plugin._fetch_recommendation_page.assert_awaited_once_with(
                    1, "mr", "all"
                )
                self.assertEqual(state.source_period, "all")
                self.assertIn(
                    "全部时间 · Most Recent",
                    plugin.recommendation.message(state, cover).chain[-1].text,
                )

    async def test_category_adapter_uses_requested_time_range_and_order(self):
        client = mock.AsyncMock()
        manager = mock.AsyncMock()
        manager.__aenter__.return_value = client
        self.plugin._build_option = mock.Mock(
            return_value=types.SimpleNamespace(
                new_jm_async_client=mock.Mock(return_value=manager)
            )
        )
        fake_jm = types.SimpleNamespace(
            JmMagicConstants=types.SimpleNamespace(
                TIME_TODAY="t",
                TIME_WEEK="w",
                TIME_MONTH="m",
                TIME_ALL="a",
                CATEGORY_ALL="0",
                ORDER_BY_LATEST="mr",
                ORDER_BY_VIEW="mv",
            )
        )
        with mock.patch.dict("sys.modules", {"jmcomic": fake_jm}):
            for period, time_range in (
                ("day", "t"),
                ("week", "w"),
                ("month", "m"),
                ("all", "a"),
            ):
                for order in ("mr", "mv"):
                    await self.main.JMComicPlugin._fetch_recommendation_page(
                        self.plugin, 2, order, period
                    )
                    client.categories_filter.assert_awaited_with(
                        page=2,
                        time=time_range if order == "mv" else "a",
                        category="0",
                        order_by=order,
                    )

    async def test_reset_clears_pick_delivery_records_and_cache_without_sending(self):
        self.configure_targets()
        with mock.patch.object(
            self.module.random, "choice", side_effect=lambda rows: rows[0]
        ):
            await self.service.push_today()
        image_path = next(self.service.cache_dir.glob("*.jpg"))
        unrelated = self.service.cache_dir / "keep.txt"
        unrelated.write_text("keep")
        sends_before = self.plugin.context.send_message.await_count
        await self.service.reset_today()
        self.assertEqual(self.saved(), {})
        self.assertIsNone(self.service.state)
        self.assertFalse(image_path.exists())
        self.assertTrue(unrelated.exists())
        self.assertEqual(self.plugin.context.send_message.await_count, sends_before)
        restarted = self.make_plugin()
        with mock.patch.object(
            self.module.random, "choice", side_effect=lambda rows: rows[-1]
        ):
            fresh, _ = await restarted.recommendation.get_today()
        self.assertEqual(fresh.album_id, "10")
        self.assertEqual(fresh.attempted_targets, [])
        self.assertEqual(fresh.sent_targets, [])

    async def test_reset_waits_for_inflight_push_before_clearing_records(self):
        self.service.targets = ["bot:FriendMessage:456"]
        started = asyncio.Event()
        release = asyncio.Event()

        async def send(*_):
            started.set()
            await release.wait()
            return True

        self.plugin.context.send_message.side_effect = send
        push = asyncio.create_task(self.service.push_today())
        await started.wait()
        reset = asyncio.create_task(self.service.reset_today())
        await asyncio.sleep(0)
        self.assertFalse(reset.done())
        release.set()
        await asyncio.gather(push, reset)
        self.assertEqual(self.saved(), {})
        self.assertIsNone(self.service.state)

    async def test_failed_reset_does_not_clear_memory_or_cover(self):
        state, _ = await self.service.get_today()
        self.plugin.put_kv_data.side_effect = RuntimeError("reset storage failed")
        with self.assertRaisesRegex(RuntimeError, "storage"):
            await self.service.reset_today()
        self.assertEqual(self.service.state.album_id, state.album_id)
        self.assertEqual(len(list(self.service.cache_dir.glob("*.jpg"))), 1)

    async def test_reset_command_requires_admin_and_allowed_session(self):
        self.service.reset_today = mock.AsyncMock()
        event = _Event()
        result = [reply async for reply in self.plugin.reset_recommendation(event)]
        self.assertIn("管理员", result[0].text)
        self.service.reset_today.assert_not_awaited()
        event.role = "admin"
        event._group_id = "999"
        result = [reply async for reply in self.plugin.reset_recommendation(event)]
        self.assertIn("白名单", result[0].text)
        self.service.reset_today.assert_not_awaited()
        event._group_id = None
        result = [reply async for reply in self.plugin.reset_recommendation(event)]
        self.service.reset_today.assert_awaited_once()
        self.assertIn("不会立即群发", result[0].text)
        self.plugin.context.send_message.assert_not_awaited()

    async def test_legacy_day_pick_does_not_get_mislabeled_as_all_time(self):
        await self.service.get_today()
        self.saved().pop("source_order")
        restarted = self.make_plugin()
        state, cover = await restarted.recommendation.get_today()
        self.assertEqual(state.source_order, "day")
        self.assertIn("旧版本缓存", self.service.message(state, cover).chain[-1].text)
        restarted._fetch_recommendation_page.assert_not_awaited()

    async def test_invalid_rank_id_cannot_become_cache_path(self):
        self.plugin._fetch_recommendation_page.return_value = _FakePage(
            [("../escape", "坏数据", [])]
        )
        with self.assertRaisesRegex(ValueError, "无效"):
            await self.service.get_today()
        self.assertEqual(self.storage, {})

    async def test_concurrent_calls_share_one_selection_detail_and_cover(self):
        results = await asyncio.gather(*(self.service.get_today() for _ in range(6)))
        self.assertEqual(len({state.album_id for state, _ in results}), 1)
        self.plugin._fetch_recommendation_page.assert_awaited_once()
        self.assertEqual(self.service._fetch_detail.await_count, 10)
        self.service._fetch_cover.assert_awaited_once()
        self.assertTrue(all(cover == self.cover for _, cover in results))

    async def test_restart_keeps_selection_despite_changed_top_n(self):
        original, _ = await self.service.get_today()
        restarted = self.make_plugin({**self.config, "recommendation": {"top_n": "3"}})
        restored, image = await restarted.recommendation.get_today()
        self.assertEqual(restored, original)
        self.assertEqual(image, self.cover)
        restarted._fetch_recommendation_page.assert_not_awaited()
        restarted.recommendation._fetch_detail.assert_not_awaited()
        restarted.recommendation._fetch_cover.assert_not_awaited()

    async def test_next_day_selects_again_and_cleans_only_owned_old_covers(self):
        await self.service.get_today()
        old = list(self.service.cache_dir.glob("*.jpg"))
        unrelated = self.service.cache_dir / "keep.txt"
        unrelated.write_text("keep")
        self.clock += timedelta(days=1)
        state, _ = await self.service.get_today()
        self.assertEqual(state.day, "2026-09-12")
        self.assertEqual(self.plugin._fetch_recommendation_page.await_count, 2)
        self.assertTrue(all(not p.exists() for p in old))
        self.assertTrue(unrelated.exists())

    async def test_crossing_midnight_during_fetch_does_not_return_stale_pick(self):
        async def fetch(album_id):
            if self.clock.day == 11:
                self.clock += timedelta(days=1)
            return types.SimpleNamespace(name=f"标题{album_id}", tags=[])

        self.service._fetch_detail.side_effect = fetch
        state, _ = await self.service.get_today()
        self.assertEqual(state.day, "2026-09-12")
        self.assertEqual(self.plugin._fetch_recommendation_page.await_count, 2)

    async def test_detail_failure_retries_same_id_even_after_restart(self):
        self.plugin = self.make_plugin(
            {**self.config, "recommendation": {"exclude_tags": []}}
        )
        self.service = self.plugin.recommendation
        self.service._fetch_detail.side_effect = RuntimeError("detail offline")
        with self.assertRaisesRegex(RuntimeError, "offline"):
            await self.service.get_today()
        chosen = self.saved()["album_id"]
        self.assertFalse(self.saved()["details_loaded"])
        restarted = self.make_plugin()
        state, _ = await restarted.recommendation.get_today()
        self.assertEqual(state.album_id, chosen)
        restarted._fetch_recommendation_page.assert_not_awaited()
        restarted.recommendation._fetch_detail.assert_awaited_once_with(chosen)

    async def test_storage_read_failure_does_not_overwrite_existing_pick(self):
        self.plugin.get_kv_data.side_effect = RuntimeError("storage unavailable")
        with self.assertRaisesRegex(RuntimeError, "storage"):
            await self.service.get_today()
        self.plugin._fetch_recommendation_page.assert_not_awaited()
        self.plugin.put_kv_data.assert_not_awaited()

    async def test_storage_write_failure_prevents_publishing_unpersisted_pick(self):
        self.plugin.put_kv_data.side_effect = RuntimeError("disk full")
        with self.assertRaisesRegex(RuntimeError, "disk full"):
            await self.service.get_today()
        self.assertIsNone(self.service.state)
        self.service._fetch_cover.assert_not_awaited()

    async def test_uncertain_storage_write_reloads_committed_choice(self):
        async def commit_then_fail(key, value):
            self.storage[key] = copy.deepcopy(value)
            raise RuntimeError("lost storage acknowledgement")

        self.plugin.put_kv_data.side_effect = commit_then_fail
        with self.assertRaisesRegex(RuntimeError, "acknowledgement"):
            await self.service.get_today()
        chosen_id = self.saved()["album_id"]

        async def save(key, value):
            self.storage[key] = copy.deepcopy(value)

        self.plugin.put_kv_data.side_effect = save
        state, _ = await self.service.get_today()
        self.assertEqual(state.album_id, chosen_id)
        self.plugin._fetch_recommendation_page.assert_awaited_once()

    async def test_cover_failure_falls_back_to_text_with_all_tags(self):
        self.service._fetch_cover.side_effect = RuntimeError("cover offline")
        self.service._fetch_detail.side_effect = lambda _: types.SimpleNamespace(
            name="标题", tags=[f"tag{i}" for i in range(12)]
        )
        state, cover = await self.service.get_today()
        self.assertIsNone(cover)
        chain = self.service.message(state, cover).chain
        self.assertEqual(len(chain), 1)
        self.assertIn("封面暂不可用", chain[0].text)
        self.assertIn("tag11", chain[0].text)
        self.assertIn(f"/jm下载 {state.album_id}", chain[0].text)

    async def test_invalid_image_is_not_sent_and_missing_tags_have_placeholder(self):
        self.service._fetch_cover.return_value = b"<html>CDN error</html>"
        self.service._fetch_detail.side_effect = lambda _: types.SimpleNamespace(
            name="标题", tags=[]
        )
        state, cover = await self.service.get_today()
        self.assertIsNone(cover)
        self.assertIn("暂无标签", self.service.message(state, cover).chain[0].text)

    async def test_corrupt_cached_cover_is_refetched(self):
        await self.service.get_today()
        path = next(self.service.cache_dir.glob("*.jpg"))
        path.write_bytes(b"broken")
        _, cover = await self.service.get_today()
        self.assertEqual(cover, self.cover)
        self.assertEqual(path.read_bytes(), self.cover)
        self.assertEqual(self.service._fetch_cover.await_count, 2)

    async def test_manual_command_works_when_auto_disabled_and_sends_bytes(self):
        self.assertFalse(self.service.enabled)
        event = _Event()
        replies = [reply async for reply in self.plugin.recommend(event)]
        self.assertTrue(event.stopped)
        self.assertEqual(len(replies), 1)
        image, text = replies[0]
        self.assertEqual(
            base64.b64decode(image.file.removeprefix("base64://")), self.cover
        )
        self.assertIn("每日推荐", text.text)
        self.plugin.context.send_message.assert_not_awaited()

    async def test_manual_command_obeys_group_and_private_permissions(self):
        self.plugin.private_whitelist = {"allowed"}
        for event in [_Event(group_id="999"), _Event(sender_id="denied")]:
            replies = [reply async for reply in self.plugin.recommend(event)]
            self.assertEqual(len(replies), 1)
            self.assertIn("白名单", replies[0].text)
        self.plugin._fetch_recommendation_page.assert_not_awaited()

    async def test_manual_error_returns_reason(self):
        self.plugin._fetch_recommendation_page.side_effect = RuntimeError(
            "rank offline"
        )
        replies = [reply async for reply in self.plugin.recommend(_Event())]
        self.assertIn("rank offline", replies[0].text)

    def configure_targets(self):
        self.service.targets = [
            "botA:GroupMessage:123",
            "botA:FriendMessage:456",
            "botB:GroupMessage:123",
        ]
        self.plugin.group_whitelist = {"123"}

    async def test_concurrent_manual_and_push_share_pick_and_distinct_platform_routes(
        self,
    ):
        self.configure_targets()
        manual, _ = await asyncio.gather(
            self.service.get_today(), self.service.push_today()
        )
        self.plugin._fetch_recommendation_page.assert_awaited_once()
        calls = self.plugin.context.send_message.await_args_list
        self.assertEqual([call.args[0] for call in calls], self.service.targets)
        self.assertTrue(
            all(
                f"JM{manual[0].album_id}" in call.args[1].chain[1].text
                for call in calls
            )
        )
        self.assertEqual(self.saved()["sent_targets"], self.service.targets)
        await self.service.push_today()
        self.assertEqual(self.plugin.context.send_message.await_count, 3)

    async def test_push_checks_permissions_without_fetching_if_no_targets_allowed(self):
        self.service.targets = ["bot:GroupMessage:999", "bot:FriendMessage:999"]
        self.plugin.private_whitelist = {"456"}
        await self.service.push_today()
        self.plugin._fetch_recommendation_page.assert_not_awaited()
        self.plugin.context.send_message.assert_not_awaited()

    async def test_single_target_failure_and_false_do_not_block_other_targets(self):
        self.configure_targets()
        self.plugin.context.send_message.side_effect = [
            RuntimeError("remote failed"),
            False,
            True,
        ]
        await self.service.push_today()
        self.assertEqual(self.saved()["attempted_targets"], self.service.targets)
        self.assertEqual(self.saved()["sent_targets"], [self.service.targets[-1]])
        restarted = self.make_plugin()
        restarted.recommendation.targets = self.service.targets
        restarted.group_whitelist = {"123"}
        await restarted.recommendation.push_today()
        restarted.context.send_message.assert_not_awaited()

    async def test_cancel_during_remote_send_does_not_retry_uncertain_delivery(self):
        self.service.targets = ["bot:FriendMessage:456"]
        started = asyncio.Event()

        async def remote_send(*_):
            started.set()
            await asyncio.Future()

        self.plugin.context.send_message.side_effect = remote_send
        task = asyncio.create_task(self.service.push_today())
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.saved()["attempted_targets"], self.service.targets)
        self.assertEqual(self.saved()["sent_targets"], [])
        restarted = self.make_plugin()
        restarted.recommendation.targets = self.service.targets
        await restarted.recommendation.push_today()
        restarted.context.send_message.assert_not_awaited()

    async def test_cannot_send_if_attempt_cannot_be_persisted(self):
        await self.service.get_today()
        self.service.targets = ["bot:FriendMessage:456"]
        self.plugin.put_kv_data.side_effect = RuntimeError("storage failed")
        with self.assertRaisesRegex(RuntimeError, "storage"):
            await self.service.push_today()
        self.plugin.context.send_message.assert_not_awaited()

    async def test_push_with_stale_scheduled_date_is_skipped(self):
        self.configure_targets()
        await self.service.push_today(expected_day="2026-09-10")
        self.plugin._fetch_recommendation_page.assert_not_awaited()

    async def test_target_normalization_deduplicates_group_session_prefixes(self):
        plugin = self.make_plugin(
            {
                **self.config,
                "recommendation": {
                    "targets": [
                        "bot:GroupMessage:100_123",
                        "bot:GroupMessage:123",
                        "bot:FriendMessage:456",
                        "bot:FriendMessage:456",
                        "other:GroupMessage:123",
                        "123",
                        "bot:OtherMessage:1",
                        "bot:FriendMessage:../../x",
                        ":GroupMessage:123",
                    ]
                },
            }
        )
        self.assertEqual(
            plugin.recommendation.targets,
            ["bot:GroupMessage:123", "bot:FriendMessage:456", "other:GroupMessage:123"],
        )

    async def test_invalid_config_disables_schedule_but_keeps_manual(self):
        for cfg in [{"time": "25:00"}, {"time": "9:00"}, {"timezone": "Invalid/Zone"}]:
            plugin = self.make_plugin(
                {**self.config, "recommendation": {"enabled": True, **cfg}}
            )
            self.assertFalse(plugin.recommendation.enabled)
            self.assertTrue(plugin.recommendation.config_error)
        plugin = self.make_plugin({**self.config, "recommendation": {"top_n": "bad"}})
        self.assertEqual(plugin.recommendation.top_n, 10)

    async def test_next_run_uses_config_timezone_and_never_catches_up(self):
        self.assertEqual(self.service.next_run(self.clock).hour, 9)
        after = self.clock.replace(hour=10)
        self.assertEqual(self.service.next_run(after).date().isoformat(), "2026-09-12")
        exact = self.clock.replace(hour=9)
        self.assertEqual(self.service.next_run(exact).date().isoformat(), "2026-09-12")
        utc = self.clock.astimezone(timezone.utc)
        self.assertEqual(
            self.service.next_run(utc).timestamp(),
            self.service.next_run(self.clock).timestamp(),
        )

    async def test_dst_gap_is_skipped_and_fold_runs_only_once(self):
        from datetime import time

        self.service.tz = ZoneInfo("America/New_York")
        self.service.send_time = time(2, 30)
        spring = datetime(2026, 3, 8, 0, tzinfo=self.service.tz)
        self.assertEqual(self.service.next_run(spring).day, 9)
        self.service.send_time = time(1, 30)
        autumn = datetime(2026, 11, 1, 1, 40, tzinfo=self.service.tz)
        self.assertEqual(self.service.next_run(autumn).day, 2)

    async def test_initialize_starts_only_one_task_and_terminate_cleans_up(self):
        self.service.enabled = True
        self.service.targets = ["bot:FriendMessage:123"]

        # Use a real pending coroutine to prove cancellation and duplicate initialization.
        async def pending(_due):
            await asyncio.Future()

        self.service._schedule = mock.AsyncMock(side_effect=pending)
        await self.plugin.initialize()
        task = self.service.task
        await self.service.initialize()
        self.assertIs(self.service.task, task)
        await asyncio.sleep(0)
        await self.plugin.terminate()
        self.assertTrue(task.cancelled())
        self.assertIsNone(self.service.task)

    async def test_disabled_or_empty_targets_do_not_start_timer(self):
        await self.service.initialize()
        self.assertIsNone(self.service.task)
        self.service.enabled = True
        await self.service.initialize()
        self.assertIsNone(self.service.task)

    async def test_scheduler_fires_once_then_waits_for_next_day(self):
        due = self.clock.replace(hour=9)
        self.clock = due
        self.service.push_today = mock.AsyncMock()
        with mock.patch.object(
            self.module.asyncio, "sleep", side_effect=asyncio.CancelledError
        ):
            with self.assertRaises(asyncio.CancelledError):
                await self.service._schedule(due)
        self.service.push_today.assert_awaited_once_with(expected_day="2026-09-11")

    async def test_scheduler_skips_suspension_and_recovers_from_push_error(self):
        due = self.clock.replace(hour=9)
        self.clock = due + timedelta(minutes=5)
        self.service.push_today = mock.AsyncMock(side_effect=RuntimeError("offline"))
        with mock.patch.object(
            self.module.asyncio, "sleep", side_effect=asyncio.CancelledError
        ):
            with self.assertRaises(asyncio.CancelledError):
                await self.service._schedule(due)
        self.service.push_today.assert_not_awaited()
        self.clock = due
        with mock.patch.object(
            self.module.asyncio, "sleep", side_effect=asyncio.CancelledError
        ):
            with self.assertRaises(asyncio.CancelledError):
                await self.service._schedule(due)
        self.service.push_today.assert_awaited_once()

    async def test_async_cover_adapter_uses_upstream_client_and_response_content(self):
        # Validate the adapter independently of the higher-level cover fixture.
        client = mock.AsyncMock()
        client.get_jm_image.return_value = types.SimpleNamespace(content=self.cover)
        manager = mock.AsyncMock()
        manager.__aenter__.return_value = client
        option = types.SimpleNamespace(
            new_jm_async_client=mock.Mock(return_value=manager)
        )
        self.plugin._build_option = mock.Mock(return_value=option)
        fake_jm = types.SimpleNamespace(
            JmcomicText=types.SimpleNamespace(
                get_album_cover_url=mock.Mock(
                    return_value="https://cdn.example/cover.jpg"
                )
            )
        )
        with mock.patch.dict("sys.modules", {"jmcomic": fake_jm}):
            content = await self.module.DailyRecommendation._fetch_cover(
                self.service, "123"
            )
        self.assertEqual(content, self.cover)
        client.get_jm_image.assert_awaited_once_with("https://cdn.example/cover.jpg")
        option.new_jm_async_client.assert_called_once_with(max_clients=1)
        manager.__aexit__.assert_awaited_once()

    async def test_malformed_snapshot_is_reported_without_replacing_it(self):
        self.storage[self.module.KV_RECOMMENDATION_KEY] = {
            "day": "2026-09-11",
            "album_id": "../escape",
        }
        with self.assertRaises(ValueError):
            await self.service.get_today()
        self.plugin._fetch_recommendation_page.assert_not_awaited()
        self.plugin.put_kv_data.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
