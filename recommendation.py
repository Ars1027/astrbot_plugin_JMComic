"""Daily selection, cover caching and scheduled delivery for JMComic."""

from __future__ import annotations

import asyncio
import copy
import io
import random
import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from astrbot.api import logger
from astrbot.api.event import MessageChain
import astrbot.api.message_components as Comp


KV_RECOMMENDATION_KEY = "jmcomic_daily_recommendation"
ORDER_LABELS = {
    "mr": "全部时间 · Most Recent（最新发布）",
    "mv": "全部时间 · Most Viewed（最多观看）",
    "day": "日榜（旧版本缓存，可用 /jm重置推荐 更新）",
}


@dataclass
class DailyPick:
    day: str
    album_id: str
    title: str = ""
    tags: list[str] = field(default_factory=list)
    details_loaded: bool = False
    attempted_targets: list[str] = field(default_factory=list)
    sent_targets: list[str] = field(default_factory=list)
    source_order: str = "mr"

    @classmethod
    def restore(cls, raw):
        if not raw:
            return None
        if not isinstance(raw, dict):
            raise ValueError("每日推荐快照格式无效")
        day = raw.get("day", "")
        album_id = raw.get("album_id", "")
        if date.fromisoformat(day).isoformat() != day or not re.fullmatch(
            r"[0-9]+", album_id
        ):
            raise ValueError("每日推荐快照的日期或 ID 无效")
        fields = {}
        for key in ("tags", "attempted_targets", "sent_targets"):
            value = raw.get(key, [])
            if not isinstance(value, list) or any(
                not isinstance(item, str) for item in value
            ):
                raise ValueError(f"每日推荐快照的 {key} 无效")
            fields[key] = list(value)
        title = raw.get("title", "")
        if not isinstance(title, str):
            raise ValueError("每日推荐快照的标题无效")
        source_order = raw.get("source_order", "day")
        if source_order not in ORDER_LABELS:
            raise ValueError("每日推荐快照的来源无效")
        return cls(
            day,
            album_id,
            title,
            details_loaded=bool(raw.get("details_loaded")),
            source_order=source_order,
            **fields,
        )


class DailyRecommendation:
    def __init__(self, plugin):
        self.plugin = plugin
        self.enabled = bool(plugin._cfg("recommendation", "enabled", False))
        self.config_error = ""
        try:
            value = str(plugin._cfg("recommendation", "time", "09:00"))
            if not re.fullmatch(r"[0-9]{2}:[0-9]{2}", value):
                raise ValueError("时间须为 HH:MM")
            self.send_time = time.fromisoformat(value)
        except ValueError:
            self.send_time = time(9)
            self.config_error = "recommendation.time 必须为有效的 HH:MM，定时推送已停用"
            self.enabled = False
        try:
            self.tz = ZoneInfo(
                str(plugin._cfg("recommendation", "timezone", "Asia/Shanghai"))
            )
        except (ZoneInfoNotFoundError, ValueError):
            self.tz = timezone(timedelta(hours=8))
            self.config_error = "recommendation.timezone 无效或缺少 tzdata；定时推送已停用，手动推荐按 UTC+8 计日"
            self.enabled = False
        try:
            raw_top_n = str(plugin._cfg("recommendation", "top_n", 10)).strip()
            if not re.fullmatch(r"[0-9]+", raw_top_n):
                raise ValueError
            self.top_n = int(raw_top_n)
            if self.top_n < 1:
                raise ValueError
        except (TypeError, ValueError):
            self.top_n = 10
            logger.warning("recommendation.top_n 必须是正整数，已回退为 10")
        if self.config_error:
            logger.warning(self.config_error)

        order_label = str(plugin._cfg("recommendation", "order_by", "Most Recent"))
        if order_label not in {"Most Recent", "Most Viewed"}:
            logger.warning("recommendation.order_by 无效，已回退为 Most Recent")
        self.order_by = "mv" if order_label == "Most Viewed" else "mr"

        self.targets = []
        for target in plugin._normalize_list(
            plugin._cfg("recommendation", "targets", [])
        ):
            try:
                platform_id, kind, session_id = target.split(":", 2)
                # OneBot group sessions may include a per-user prefix; route to the group.
                if kind == "GroupMessage":
                    session_id = session_id.split("_")[-1]
                if (
                    not platform_id
                    or kind not in {"GroupMessage", "FriendMessage"}
                    or not re.fullmatch(r"[0-9]+", session_id)
                ):
                    raise ValueError
                canonical = f"{platform_id}:{kind}:{session_id}"
                if canonical not in self.targets:
                    self.targets.append(canonical)
            except ValueError:
                logger.warning(
                    f"已忽略无效的每日推荐会话 ID: {target}；请使用 /sid 获取"
                )

        self.cache_dir = plugin.data_dir / "recommendations"
        self.state: DailyPick | None = None
        self._loaded = False
        self._lock = asyncio.Lock()
        self._push_lock = asyncio.Lock()
        self.task: asyncio.Task | None = None

    def now(self):
        return datetime.now(self.tz)

    def next_run(self, now: datetime) -> datetime:
        """Return the next future local wall time; skip nonexistent DST times."""
        day = now.astimezone(self.tz).date()
        while True:
            wall = datetime.combine(day, self.send_time)
            candidate = wall.replace(tzinfo=self.tz)
            round_trip = candidate.astimezone(timezone.utc).astimezone(self.tz)
            # In an autumn fold, schedule only the first occurrence.
            if (
                round_trip.replace(tzinfo=None) == wall
                and candidate.timestamp() > now.timestamp()
            ):
                return candidate
            day += timedelta(days=1)

    async def initialize(self):
        async with self._lock:
            try:
                await self._load()
            except Exception as exc:
                logger.warning(f"每日推荐快照读取失败，将在下次请求时重试: {exc}")
            self._purge_covers(self.now().date().isoformat())
        if self.enabled and self.targets and (self.task is None or self.task.done()):
            due = self.next_run(self.now())
            self.task = asyncio.create_task(
                self._schedule(due), name="jmcomic-daily-recommendation"
            )

    async def close(self):
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None

    async def _schedule(self, due: datetime):
        while True:
            now = self.now()
            delay = due.timestamp() - now.timestamp()
            if delay > 0:
                await asyncio.sleep(min(delay, 60))
                continue
            # No catch-up after machine suspension or a blocked event loop.
            if -delay < 60 and now.date() == due.date():
                try:
                    await self.push_today(expected_day=due.date().isoformat())
                except Exception as exc:
                    logger.warning(f"每日推荐定时推送失败: {exc}")
            due = self.next_run(self.now())

    async def _load(self):
        if not self._loaded:
            raw = await self.plugin.get_kv_data(KV_RECOMMENDATION_KEY, {})
            self.state = DailyPick.restore(raw)
            self._loaded = True

    async def _save(self, state: DailyPick):
        # Do not expose an unpersisted choice or delivery claim to another caller.
        try:
            await self.plugin.put_kv_data(KV_RECOMMENDATION_KEY, asdict(state))
        except BaseException:
            # The write may have committed before an error/cancellation was reported.
            # Reload before another mutation rather than overwriting that decision.
            self._loaded = False
            raise
        self.state = state

    def _purge_covers(self, day: str):
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        for path in self.cache_dir.iterdir():
            if re.fullmatch(
                r"[0-9]{4}-[0-9]{2}-[0-9]{2}-[0-9]+\.jpg(?:\.tmp)?", path.name
            ):
                if path.is_file() and not path.name.startswith(day + "-"):
                    path.unlink()

    async def _fetch_detail(self, album_id: str):
        option = self.plugin._build_option(self.plugin.data_dir / "query-cache")
        async with option.new_jm_async_client(max_clients=3) as client:
            return await client.get_album_detail(album_id)

    async def _fetch_cover(self, album_id: str) -> bytes:
        import jmcomic

        option = self.plugin._build_option(self.cache_dir)
        async with option.new_jm_async_client(max_clients=1) as client:
            url = jmcomic.JmcomicText.get_album_cover_url(album_id)
            response = await client.get_jm_image(url)
            return bytes(response.content)

    @staticmethod
    def _validate_cover(content: bytes):
        from PIL import Image

        if not content or len(content) > 10 * 1024 * 1024:
            raise ValueError("封面为空或超过 10 MB")
        with Image.open(io.BytesIO(content)) as image:
            image.verify()

    async def _cover(self, state: DailyPick) -> bytes | None:
        path = self.cache_dir / f"{state.day}-{state.album_id}.jpg"
        try:
            if path.exists():
                content = path.read_bytes()
                try:
                    await asyncio.to_thread(self._validate_cover, content)
                    return content
                except Exception:
                    path.unlink()
            content = await asyncio.wait_for(
                self._fetch_cover(state.album_id), timeout=60
            )
            await asyncio.to_thread(self._validate_cover, content)
            temp = path.with_suffix(".jpg.tmp")
            temp.write_bytes(content)
            temp.replace(path)
            return content
        except Exception as exc:
            logger.warning(f"每日推荐 JM{state.album_id} 封面暂不可用: {exc}")
            return None

    async def _fetch_candidates(self):
        candidates = []
        seen_ids = set()
        page_number = 1
        while len(candidates) < self.top_n:
            page = await self.plugin._fetch_recommendation_page(
                page_number, self.order_by
            )
            items = self.plugin._extract_page_results(page)
            previous_count = len(candidates)
            for item in items:
                if not re.fullmatch(r"[0-9]+", item.album_id):
                    raise ValueError("推荐列表返回了无效的作品 ID")
                if item.album_id in seen_ids:
                    continue
                seen_ids.add(item.album_id)
                candidates.append(item)
                if len(candidates) == self.top_n:
                    break
            # Stop at an empty/repeated page even if the upstream total is stale.
            if len(candidates) == previous_count:
                break
            page_count = self.plugin._page_count(page)
            if page_count is not None and page_number >= page_count:
                break
            page_number += 1
        return candidates

    async def reset_today(self):
        # Use the same lock order as push_today. Finish an in-flight delivery before
        # clearing its records so an old batch cannot mark the new pick as sent.
        async with self._push_lock:
            async with self._lock:
                try:
                    await self.plugin.put_kv_data(KV_RECOMMENDATION_KEY, {})
                except BaseException:
                    self._loaded = False
                    raise
                self.state = None
                self._loaded = True
                day = self.now().date().isoformat()
                try:
                    if self.cache_dir.exists():
                        for path in self.cache_dir.iterdir():
                            if (
                                re.fullmatch(
                                    re.escape(day) + r"-[0-9]+\.jpg(?:\.tmp)?",
                                    path.name,
                                )
                                and path.is_file()
                            ):
                                path.unlink()
                except OSError as exc:
                    logger.warning(f"每日推荐已重置，但封面缓存清理失败: {exc}")

    async def get_today(self) -> tuple[DailyPick, bytes | None]:
        async with self._lock:
            await self._load()
            # Requests completing across midnight must not publish yesterday's selection.
            for _ in range(2):
                day = self.now().date().isoformat()
                self._purge_covers(day)
                if self.state is None or self.state.day != day:
                    candidates = await self._fetch_candidates()
                    if not candidates:
                        raise ValueError("当前推荐列表暂无可推荐的作品")
                    chosen = random.choice(candidates)
                    # Persist ID before fetching details so retries cannot select another book.
                    await self._save(
                        DailyPick(day, chosen.album_id, source_order=self.order_by)
                    )
                state = copy.deepcopy(self.state)
                if not state.details_loaded:
                    album = await self._fetch_detail(state.album_id)
                    state.title = str(getattr(album, "name", "") or "").strip()
                    if not state.title:
                        raise ValueError("推荐作品详情缺少标题，请稍后重试")
                    state.tags = self.plugin._normalize_tags(getattr(album, "tags", []))
                    state.details_loaded = True
                    await self._save(state)
                cover = await self._cover(state)
                if self.now().date().isoformat() == day:
                    return copy.deepcopy(state), cover
            raise RuntimeError("日期已变化，请重新获取每日推荐")

    @staticmethod
    def message(state: DailyPick, cover: bytes | None) -> MessageChain:
        text = (
            f"JMComic 每日推荐 · {state.day}\n"
            f"来源: {ORDER_LABELS[state.source_order]}\n"
            f"标题: {state.title}\n"
            f"ID: JM{state.album_id}\n"
            f"标签: {'、'.join(state.tags) if state.tags else '暂无标签'}\n"
            f"下载: /jm下载 {state.album_id}"
        )
        components = []
        if cover is not None:
            components.append(Comp.Image.fromBytes(cover))
        else:
            text += "\n封面暂不可用"
        components.append(Comp.Plain(text=text))
        return MessageChain(chain=components)

    def _target_allowed(self, target: str) -> bool:
        _platform, kind, session_id = target.split(":", 2)
        group_id = session_id if kind == "GroupMessage" else None
        allowed, reason = self.plugin._is_session_allowed(group_id, session_id)
        if not allowed:
            logger.warning(f"每日推荐跳过 {target}: {reason}")
        return allowed

    async def push_today(self, expected_day: str | None = None):
        async with self._push_lock:
            if expected_day and self.now().date().isoformat() != expected_day:
                return
            targets = [
                target for target in self.targets if self._target_allowed(target)
            ]
            if not targets:
                return
            state, cover = await self.get_today()
            if expected_day and state.day != expected_day:
                return
            for target in targets:
                async with self._lock:
                    await self._load()
                    if (
                        self.now().date().isoformat() != state.day
                        or self.state.day != state.day
                    ):
                        return
                    if target in self.state.attempted_targets:
                        continue
                    current = copy.deepcopy(self.state)
                    current.attempted_targets.append(target)
                    # A crash/timeout may occur after the remote platform accepted the message.
                    # Record the attempt first; never automatically resend an uncertain delivery.
                    await self._save(current)
                try:
                    sent = await asyncio.wait_for(
                        self.plugin.context.send_message(
                            target, self.message(state, cover)
                        ),
                        timeout=60,
                    )
                    if sent is False:
                        raise RuntimeError("未找到匹配的平台实例")
                    async with self._lock:
                        if self.state.day == state.day:
                            current = copy.deepcopy(self.state)
                            current.sent_targets.append(target)
                            await self._save(current)
                except Exception as exc:
                    logger.warning(f"每日推荐发送至 {target} 失败（不自动重试）: {exc}")
