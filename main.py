from __future__ import annotations

import asyncio
import base64
import hashlib
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
import astrbot.api.message_components as Comp


PLUGIN_NAME = "astrbot_plugin_JMComic"
KV_TASKS_KEY = "jmcomic_tasks"
ACTIVE_STATUSES = {"pending", "running"}
SUPPORTED_FORMATS = {"zip", "pdf"}
FILE_DELIVERY_MODES = {
    "auto",
    "napcat_http_stream",
    "onebot_group_file_base64",
}
FORMAT_ALIASES = {
    "zip": "zip",
    "压缩": "zip",
    "压缩包": "zip",
    "pdf": "pdf",
}
RANKING_PERIOD_ALIASES = {
    "日": "day",
    "日榜": "day",
    "今日": "day",
    "今天": "day",
    "day": "day",
    "daily": "day",
    "周": "week",
    "周榜": "week",
    "週": "week",
    "週榜": "week",
    "本周": "week",
    "week": "week",
    "weekly": "week",
    "月": "month",
    "月榜": "month",
    "本月": "month",
    "month": "month",
    "monthly": "month",
}
RANKING_PERIOD_LABELS = {
    "day": "日榜",
    "week": "周榜",
    "month": "月榜",
}
JM_CATEGORY_ALIASES = {
    "0": "0",
    "all": "0",
    "全部": "0",
    "同人": "doujin",
    "doujin": "doujin",
    "单本": "single",
    "单行本": "single",
    "single": "single",
    "短篇": "short",
    "短漫": "short",
    "short": "short",
    "其他": "another",
    "another": "another",
    "韩漫": "hanman",
    "韓漫": "hanman",
    "hanman": "hanman",
    "美漫": "meiman",
    "meiman": "meiman",
    "cos": "doujin_cosplay",
    "cosplay": "doujin_cosplay",
    "doujin_cosplay": "doujin_cosplay",
    "3d": "3D",
    "英文": "english_site",
    "英文站": "english_site",
    "english": "english_site",
    "english_site": "english_site",
}
JM_CATEGORY_LABELS = {
    "0": "全部",
    "doujin": "同人",
    "single": "单本",
    "short": "短篇",
    "another": "其他",
    "hanman": "韩漫",
    "meiman": "美漫",
    "doujin_cosplay": "Cosplay",
    "3D": "3D",
    "english_site": "英文站",
}
SUPPORTED_CATEGORY_HINT = "全部、同人、单本、短篇、其他、韩漫、美漫、cosplay、3d、英文"


@dataclass
class JMComicTask:
    task_id: str
    album_id: str
    export_format: str
    umo: str
    requester_id: str
    status: str = "pending"
    message: str = ""
    title: str = ""
    output_file: str = ""
    delivery_is_group: bool = False
    delivery_session_id: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    task_handle: asyncio.Task | None = field(default=None, repr=False, compare=False)
    bot_client: Any = field(default=None, repr=False, compare=False)

    def touch(self) -> None:
        self.updated_at = time.time()

    def snapshot(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "album_id": self.album_id,
            "export_format": self.export_format,
            "umo": self.umo,
            "requester_id": self.requester_id,
            "status": self.status,
            "message": self.message,
            "title": self.title,
            "output_file": self.output_file,
            "delivery_is_group": self.delivery_is_group,
            "delivery_session_id": self.delivery_session_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_snapshot(cls, data: dict[str, Any]) -> "JMComicTask":
        status = str(data.get("status", "failed") or "failed")
        if status in ACTIVE_STATUSES:
            status = "interrupted"
        return cls(
            task_id=str(data.get("task_id", "")),
            album_id=str(data.get("album_id", "")),
            export_format=str(data.get("export_format", "zip") or "zip"),
            umo=str(data.get("umo", "")),
            requester_id=str(data.get("requester_id", "")),
            status=status,
            message=str(data.get("message", "") or ""),
            title=str(data.get("title", "") or ""),
            output_file=str(data.get("output_file", "") or ""),
            delivery_is_group=bool(data.get("delivery_is_group", False)),
            delivery_session_id=str(data.get("delivery_session_id", "") or ""),
            created_at=float(data.get("created_at", time.time()) or time.time()),
            updated_at=float(data.get("updated_at", time.time()) or time.time()),
        )


@dataclass
class JMSearchResult:
    album_id: str
    title: str
    tags: list[str] = field(default_factory=list)


@dataclass
class JMQuerySession:
    kind: str
    title: str
    page_no: int
    page_count: int | None = None
    total: int | None = None
    query: str = ""
    period: str = "week"
    category: str = "0"
    items: list[JMSearchResult] = field(default_factory=list)
    offset: int = 0
    shown_count: int = 0
    last_page_item_count: int = 0
    updated_at: float = field(default_factory=time.time)

    def touch(self) -> None:
        self.updated_at = time.time()


class NapCatHttpDeliveryError(RuntimeError):
    def __init__(self, stage: str, message: str):
        super().__init__(message)
        self.stage = stage


@register(
    PLUGIN_NAME,
    "Ars1027",
    "JMComic 的 AstrBot 查询与异步下载插件",
    "v0.2.0",
)
class JMComicPlugin(Star):
    def _cfg(self, block: str, key: str, default):
        block_config = self.config.get(block, {}) or {}
        if isinstance(block_config, dict):
            value = block_config.get(key)
            if value is not None:
                return value
        value = self.config.get(key)
        return default if value is None else value

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        self.enable_private_only = bool(
            self._cfg("access_control", "enable_private_only", True)
        )
        self.group_whitelist = self._normalize_set(
            self._cfg("access_control", "group_whitelist", [])
        )
        self.private_whitelist = self._normalize_set(
            self._cfg("access_control", "private_whitelist", [])
        )

        self.proxy = str(self._cfg("network", "proxy", "") or "").strip()
        self.client_impl = str(
            self._cfg("network", "client_impl", "api") or "api"
        ).strip().lower()
        if self.client_impl not in {"api", "html"}:
            logger.warning(f"未知 JMComic client_impl: {self.client_impl}，已回退为 api")
            self.client_impl = "api"
        self.domains = self._normalize_list(self._cfg("network", "domains", []))
        self.cookies_avs = str(self._cfg("network", "cookies_avs", "") or "").strip()

        self.max_concurrent_tasks = max(
            1, int(self._cfg("download", "max_concurrent_tasks", 1) or 1)
        )
        self.image_concurrency = max(
            1, int(self._cfg("download", "image_concurrency", 10) or 10)
        )
        self.photo_concurrency = max(
            1, int(self._cfg("download", "photo_concurrency", 3) or 3)
        )

        self.default_export_format = self._normalize_format(
            str(self._cfg("download", "default_export_format", "zip") or "zip")
        )
        self.max_send_file_mb = max(
            1, int(self._cfg("download", "max_send_file_mb", 80) or 80)
        )
        self.file_delivery_mode = self._normalize_file_delivery_mode(
            str(
                self._cfg(
                    "download",
                    "file_delivery_mode",
                    "auto",
                )
                or "auto"
            )
        )
        self.max_base64_file_mb = max(
            1, int(self._cfg("download", "max_base64_file_mb", 80) or 80)
        )
        self.napcat_http_api_base = str(
            self._cfg("download", "napcat_http_api_base", "") or ""
        ).strip().rstrip("/")
        self.napcat_http_access_token = str(
            self._cfg("download", "napcat_http_access_token", "") or ""
        ).strip()
        self.napcat_http_timeout_seconds = max(
            30,
            int(
                self._cfg("download", "napcat_http_timeout_seconds", 900)
                or 900
            ),
        )
        self.stream_chunk_kb = max(
            64, int(self._cfg("download", "stream_chunk_kb", 512) or 512)
        )
        self.stream_file_retention_seconds = max(
            60,
            int(
                self._cfg("download", "stream_file_retention_seconds", 1800)
                or 1800
            ),
        )
        self.stream_chunk_retries = max(
            0, int(self._cfg("download", "stream_chunk_retries", 2) or 0)
        )
        self.delete_original_after_export = bool(
            self._cfg("download", "delete_original_after_export", True)
        )
        self.search_page_size = max(
            1, int(self._cfg("query", "search_page_size", 10) or 10)
        )
        search_result_tag_limit = self._cfg("query", "search_result_tag_limit", 5)
        self.search_result_tag_limit = max(
            0,
            int(5 if search_result_tag_limit is None else search_result_tag_limit),
        )
        self.search_more_ttl_seconds = max(
            30, int(self._cfg("query", "search_more_ttl_seconds", 600) or 600)
        )
        self.search_enrich_tags = bool(
            self._cfg("query", "search_enrich_tags", True)
        )
        self.search_enrich_tag_concurrency = max(
            1, int(self._cfg("query", "search_enrich_tag_concurrency", 3) or 3)
        )

        configured_base_dir = str(
            self._cfg("download", "download_base_dir", "") or ""
        ).strip()
        if configured_base_dir:
            self.data_dir = Path(configured_base_dir).expanduser()
        else:
            self.data_dir = (
                Path(get_astrbot_data_path()) / "plugin_data" / PLUGIN_NAME
            )
        self.download_dir = self.data_dir / "downloads"
        self.export_dir = self.data_dir / "exports"

        self.tasks: dict[str, JMComicTask] = {}
        self.query_sessions: dict[str, JMQuerySession] = {}
        self._task_lock = asyncio.Lock()

    async def initialize(self):
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.export_dir.mkdir(parents=True, exist_ok=True)
        await self._load_task_snapshots()
        logger.info(f"JMComic 插件已初始化，数据目录: {self.data_dir}")

    async def terminate(self):
        handles = [
            task.task_handle
            for task in self.tasks.values()
            if task.task_handle and not task.task_handle.done()
        ]
        for handle in handles:
            handle.cancel()
        if handles:
            await asyncio.gather(*handles, return_exceptions=True)
        await self._save_task_snapshots()
        logger.info("JMComic 插件已卸载")

    @staticmethod
    def _normalize_set(values: Any) -> set[str]:
        return set(JMComicPlugin._normalize_list(values))

    @staticmethod
    def _normalize_list(values: Any) -> list[str]:
        if not values:
            return []
        if isinstance(values, (str, int)):
            values = [values]
        result = []
        for value in values:
            text = str(value).strip()
            if text:
                result.append(text)
        return result

    @staticmethod
    def _normalize_format(value: str) -> str:
        return FORMAT_ALIASES.get(value.strip().lower(), "zip")

    @staticmethod
    def _normalize_file_delivery_mode(value: str) -> str:
        mode = value.strip().lower()
        return mode if mode in FILE_DELIVERY_MODES else "auto"

    @staticmethod
    def _parse_format_token(value: str) -> str | None:
        return FORMAT_ALIASES.get(value.strip().lower())

    @staticmethod
    def _format_time(ts: float) -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))

    @staticmethod
    def _format_size(size: int) -> str:
        units = ["B", "KB", "MB", "GB"]
        amount = float(size)
        for unit in units:
            if amount < 1024 or unit == units[-1]:
                if unit == "B":
                    return f"{int(amount)} {unit}"
                return f"{amount:.1f} {unit}"
            amount /= 1024
        return f"{size} B"

    @staticmethod
    def _limit_join(values: Any, limit: int = 8) -> str:
        if not values:
            return "-"
        items = [str(item) for item in values if str(item).strip()]
        if not items:
            return "-"
        if limit > 0 and len(items) > limit:
            return ", ".join(items[:limit]) + f" 等{len(items)}项"
        return ", ".join(items)

    @staticmethod
    def _extract_jm_id(text: str) -> str | None:
        match = re.search(r"\d+", text or "")
        return match.group(0) if match else None

    @staticmethod
    def _strip_command(event: AstrMessageEvent, *names: str) -> str:
        text = (event.message_str or "").strip()
        if not text:
            return ""
        parts = text.split(maxsplit=1)
        command = parts[0].lstrip("/")
        if command in names:
            return parts[1].strip() if len(parts) > 1 else ""
        return text

    def _sender_id(self, event: AstrMessageEvent) -> str:
        try:
            return str(event.get_sender_id())
        except Exception:
            return "unknown"

    def _group_id(self, event: AstrMessageEvent) -> str | None:
        try:
            group_id = event.get_group_id()
        except Exception:
            return None
        return str(group_id) if group_id else None

    @staticmethod
    def _event_bot_client(event: AstrMessageEvent):
        for attr in ("bot", "client"):
            bot = getattr(event, attr, None)
            if bot is not None:
                return bot
        message_obj = getattr(event, "message_obj", None)
        return getattr(message_obj, "bot", None)

    def _is_allowed(self, event: AstrMessageEvent) -> tuple[bool, str]:
        group_id = self._group_id(event)
        if group_id:
            if self.group_whitelist:
                if group_id in self.group_whitelist:
                    return True, ""
                return False, "当前群聊不在 JMComic 插件白名单中。"
            if self.enable_private_only:
                return False, "JMComic 插件默认仅私聊可用；群聊请先配置白名单。"
            return True, ""

        sender_id = self._sender_id(event)
        if self.private_whitelist and sender_id not in self.private_whitelist:
            return False, "你不在 JMComic 插件私聊白名单中。"
        return True, ""

    def _build_option(self, base_dir: Path):
        import jmcomic

        client_conf: dict[str, Any] = {
            "impl": self.client_impl,
            "async_impl": "async_api",
            "postman": {
                "meta_data": {
                    "proxies": self.proxy or None,
                    "cookies": {"AVS": self.cookies_avs} if self.cookies_avs else None,
                }
            },
        }
        if self.domains:
            client_conf["domain"] = {
                "api": self.domains,
                "html": self.domains,
            }

        option_conf = {
            "log": False,
            "client": client_conf,
            "download": {
                "cache": True,
                "image": {
                    "decode": True,
                },
                "threading": {
                    "image": self.image_concurrency,
                    "photo": self.photo_concurrency,
                },
            },
            "dir_rule": {
                "base_dir": str(base_dir),
                "rule": "Bd / Aid / Pindex",
            },
        }
        return jmcomic.JmOption.construct(option_conf)

    async def _load_task_snapshots(self):
        try:
            raw = await self.get_kv_data(KV_TASKS_KEY, {})
        except Exception as exc:
            logger.warning(f"读取 JMComic 任务快照失败: {exc}")
            return
        if not isinstance(raw, dict):
            return
        for task_id, item in raw.items():
            if not isinstance(item, dict):
                continue
            task = JMComicTask.from_snapshot(item)
            if task.task_id:
                self.tasks[str(task_id)] = task

    async def _save_task_snapshots(self):
        try:
            snapshots = {
                task_id: task.snapshot()
                for task_id, task in self.tasks.items()
            }
            await self.put_kv_data(KV_TASKS_KEY, snapshots)
        except Exception as exc:
            logger.warning(f"保存 JMComic 任务快照失败: {exc}")

    async def _set_task_state(
        self,
        task_id: str,
        status: str | None = None,
        message: str | None = None,
        title: str | None = None,
        output_file: str | None = None,
    ) -> JMComicTask | None:
        async with self._task_lock:
            task = self.tasks.get(task_id)
            if not task:
                return None
            if status is not None:
                task.status = status
            if message is not None:
                task.message = message
            if title is not None:
                task.title = title
            if output_file is not None:
                task.output_file = output_file
            task.touch()
            await self._save_task_snapshots()
            return task

    async def _active_task_count(self) -> int:
        async with self._task_lock:
            return sum(1 for task in self.tasks.values() if task.status in ACTIVE_STATUSES)

    @staticmethod
    def _int_or_none(value: Any) -> int | None:
        if value is None or value == "":
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _normalize_tags(tags: Any) -> list[str]:
        if not tags:
            return []
        if isinstance(tags, (str, int)):
            tags = [tags]
        result = []
        for tag in tags:
            text = str(tag).strip()
            if text:
                result.append(text)
        return result

    @staticmethod
    def _coerce_search_result(item: Any) -> JMSearchResult | None:
        if isinstance(item, JMSearchResult):
            return item
        if isinstance(item, dict):
            album_id = item.get("album_id") or item.get("id") or item.get("aid")
            title = item.get("title") or item.get("name") or item.get("album_name")
            tags = item.get("tags") or item.get("tag") or []
            if album_id and title:
                return JMSearchResult(str(album_id), str(title), JMComicPlugin._normalize_tags(tags))
            return None

        if isinstance(item, (tuple, list)):
            if len(item) >= 3:
                album_id, title, tags = item[:3]
            elif len(item) >= 2:
                album_id, title = item[:2]
                tags = []
            else:
                return None
            return JMSearchResult(str(album_id), str(title), JMComicPlugin._normalize_tags(tags))

        album_id = (
            getattr(item, "album_id", None)
            or getattr(item, "id", None)
            or getattr(item, "aid", None)
        )
        title = (
            getattr(item, "title", None)
            or getattr(item, "name", None)
            or getattr(item, "album_name", None)
        )
        if not album_id or not title:
            return None
        return JMSearchResult(
            str(album_id),
            str(title),
            JMComicPlugin._normalize_tags(getattr(item, "tags", [])),
        )

    def _extract_page_results(self, page: Any) -> list[JMSearchResult]:
        try:
            iterator = page.iter_id_title_tag()
        except Exception:
            try:
                iterator = iter(page)
            except TypeError:
                iterator = iter(())

        results: list[JMSearchResult] = []
        for item in iterator:
            result = self._coerce_search_result(item)
            if result is not None:
                results.append(result)
        return results

    def _page_no(self, page: Any, fallback: int) -> int:
        return self._int_or_none(
            getattr(page, "page", None)
            or getattr(page, "page_no", None)
            or getattr(page, "current_page", None)
        ) or fallback

    def _page_count(self, page: Any) -> int | None:
        return self._int_or_none(
            getattr(page, "page_count", None)
            or getattr(page, "pages", None)
            or getattr(page, "total_pages", None)
        )

    def _page_total(self, page: Any) -> int | None:
        return self._int_or_none(
            getattr(page, "total", None)
            or getattr(page, "total_count", None)
            or getattr(page, "count", None)
        )

    def _new_query_session(
        self,
        *,
        kind: str,
        title: str,
        page: Any,
        fallback_page: int,
        query: str = "",
        period: str = "week",
        category: str = "0",
    ) -> JMQuerySession:
        items = self._extract_page_results(page)
        page_no = self._page_no(page, fallback_page)
        return JMQuerySession(
            kind=kind,
            title=title,
            page_no=page_no,
            page_count=self._page_count(page),
            total=self._page_total(page),
            query=query,
            period=period,
            category=category,
            items=items,
            last_page_item_count=len(items),
        )

    def _query_header(self, session: JMQuerySession) -> str:
        header = session.title
        if session.page_no is not None and session.page_count is not None:
            header += f" (第 {session.page_no}/{session.page_count} 页)"
        elif session.total is not None:
            header += f" (约 {session.total} 条)"
        return header

    def _session_can_fetch_next_page(self, session: JMQuerySession) -> bool:
        if session.page_count is not None:
            return session.page_no < session.page_count
        return session.last_page_item_count > 0

    def _session_has_more(self, session: JMQuerySession) -> bool:
        return session.offset < len(session.items) or self._session_can_fetch_next_page(session)

    def _query_session_chunk(self, session: JMQuerySession) -> list[JMSearchResult]:
        return session.items[session.offset: session.offset + self.search_page_size]

    async def _fetch_album_tags(self, client: Any, album_id: str) -> list[str]:
        album = await client.get_album_detail(album_id)
        return self._normalize_tags(getattr(album, "tags", []))

    async def _enrich_missing_tags(self, items: list[JMSearchResult]) -> None:
        if not self.search_enrich_tags:
            return

        missing = [item for item in items if not item.tags]
        if not missing:
            return

        option = self._build_option(self.data_dir / "query-cache")
        semaphore = asyncio.Semaphore(self.search_enrich_tag_concurrency)
        async with option.new_jm_async_client(
            max_clients=self.search_enrich_tag_concurrency
        ) as client:
            async def enrich(item: JMSearchResult) -> None:
                async with semaphore:
                    try:
                        tags = await self._fetch_album_tags(client, item.album_id)
                    except Exception as exc:
                        logger.warning(f"JMComic 标签补全失败 JM{item.album_id}: {exc}")
                        return
                    if tags:
                        item.tags = tags

            await asyncio.gather(*(enrich(item) for item in missing))

    def _render_query_session_chunk_text(
        self,
        session: JMQuerySession,
        chunk: list[JMSearchResult],
    ) -> str:
        header = self._query_header(session)
        if not chunk:
            return f"{header}\n没有更多结果。"

        tag_limit = 0 if len(session.items) == 1 else self.search_result_tag_limit
        rows = []
        for index, item in enumerate(chunk, start=session.shown_count + 1):
            tag_text = self._limit_join(item.tags, tag_limit)
            rows.append(f"{index}. JM{item.album_id}  {item.title}\n   标签: {tag_text}")

        session.offset += len(chunk)
        session.shown_count += len(chunk)
        session.touch()

        lines = [header, *rows]
        if self._session_has_more(session):
            lines.append(f"发送“更多”继续显示，或使用 /jm更多。")
        return "\n".join(lines)

    async def _render_query_session_chunk(self, session: JMQuerySession) -> str:
        chunk = self._query_session_chunk(session)
        await self._enrich_missing_tags(chunk)
        return self._render_query_session_chunk_text(session, chunk)

    def _store_query_session(self, umo: str, session: JMQuerySession) -> None:
        self._purge_expired_query_sessions()
        self.query_sessions[umo] = session

    def _get_query_session(self, umo: str) -> JMQuerySession | None:
        session = self.query_sessions.get(umo)
        if session is None:
            return None
        if time.time() - session.updated_at > self.search_more_ttl_seconds:
            self.query_sessions.pop(umo, None)
            return None
        return session

    def _purge_expired_query_sessions(self) -> None:
        now = time.time()
        expired = [
            key
            for key, session in self.query_sessions.items()
            if now - session.updated_at > self.search_more_ttl_seconds
        ]
        for key in expired:
            self.query_sessions.pop(key, None)

    def _format_search_page(self, page: Any, query: str) -> str:
        session = self._new_query_session(
            kind="search",
            title=f"JMComic 搜索: {query}",
            page=page,
            fallback_page=1,
            query=query,
        )
        if not session.items:
            return f"{self._query_header(session)}\n没有找到结果。"
        return self._render_query_session_chunk_text(
            session,
            self._query_session_chunk(session),
        )

    async def _fetch_search_page(self, query: str, page: int):
        option = self._build_option(self.data_dir / "query-cache")
        async with option.new_jm_async_client(max_clients=3) as client:
            return await client.search_site(query, page=page)

    async def _fetch_ranking_page(self, period: str, category: str, page: int):
        option = self._build_option(self.data_dir / "query-cache")
        method_name = f"{period}_ranking"
        async with option.new_jm_async_client(max_clients=3) as client:
            method = getattr(client, method_name)
            return await method(page=page, category=category)

    def _parse_ranking_args(self, arg_text: str) -> tuple[str, str, int, str]:
        period = "week"
        category = "0"
        page = 1
        for token in arg_text.split():
            text = token.strip()
            if not text:
                continue
            if text.isdigit():
                page = max(1, int(text))
                continue
            period_key = text.lower()
            normalized_period = RANKING_PERIOD_ALIASES.get(period_key)
            if normalized_period:
                period = normalized_period
                continue
            category_key = text.lower()
            normalized_category = JM_CATEGORY_ALIASES.get(category_key)
            if normalized_category:
                category = normalized_category
                continue
            return "", "", 1, f"未知热门榜分类或时间参数: {text}\n支持分类: {SUPPORTED_CATEGORY_HINT}"
        return period, category, page, ""

    async def _build_more_response(self, event: AstrMessageEvent) -> str:
        session = self._get_query_session(event.unified_msg_origin)
        if session is None:
            return "当前会话没有可继续显示的 JMComic 搜索或热门榜结果。"

        if session.offset >= len(session.items):
            if not self._session_can_fetch_next_page(session):
                return "当前会话的 JMComic 结果已经显示完了。"
            next_page = session.page_no + 1
            try:
                if session.kind == "ranking":
                    page = await self._fetch_ranking_page(
                        session.period,
                        session.category,
                        next_page,
                    )
                else:
                    page = await self._fetch_search_page(session.query, next_page)
            except Exception as exc:
                logger.warning(f"JMComic 更多结果获取失败: {exc}")
                return f"获取更多结果失败: {exc}"

            session.items = self._extract_page_results(page)
            session.offset = 0
            session.page_no = self._page_no(page, next_page)
            session.page_count = self._page_count(page)
            session.total = self._page_total(page)
            session.last_page_item_count = len(session.items)
            session.touch()
            if not session.items:
                return "当前会话的 JMComic 结果已经显示完了。"

        return await self._render_query_session_chunk(session)

    def _format_album_detail(self, album: Any) -> str:
        lines = [
            f"标题: {getattr(album, 'name', '-')}",
            f"ID: JM{getattr(album, 'album_id', getattr(album, 'id', '-'))}",
            f"作者: {self._limit_join(getattr(album, 'authors', []), 6)}",
            f"页数: {getattr(album, 'page_count', '-')}",
            f"章节: {len(album) if hasattr(album, '__len__') else '-'}",
            f"发布: {getattr(album, 'pub_date', '-')}",
            f"更新: {getattr(album, 'update_date', '-')}",
            f"观看: {getattr(album, 'views', '-')}",
            f"喜欢: {getattr(album, 'likes', '-')}",
            f"标签: {self._limit_join(getattr(album, 'tags', []), 12)}",
        ]
        works = getattr(album, "works", [])
        actors = getattr(album, "actors", [])
        if works:
            lines.append(f"作品: {self._limit_join(works, 8)}")
        if actors:
            lines.append(f"人物: {self._limit_join(actors, 8)}")

        episodes = getattr(album, "episode_list", []) or []
        if episodes:
            lines.append("章节:")
            for episode in episodes[:8]:
                photo_id, index, title = episode[:3]
                lines.append(f"  {index}. JM{photo_id} {title}")
            if len(episodes) > 8:
                lines.append(f"  ... 还有 {len(episodes) - 8} 章")

        description = str(getattr(album, "description", "") or "").strip()
        if description:
            lines.append(f"简介: {description[:180]}")
        return "\n".join(lines)

    def _find_export_file(self, task_id: str, suffix: str) -> Path | None:
        task_export_dir = self.export_dir / task_id
        if not task_export_dir.exists():
            return None
        files = sorted(
            task_export_dir.glob(f"**/*.{suffix}"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        return files[0] if files else None

    async def _send_message(self, umo: str, text: str):
        await self.context.send_message(umo, MessageChain().message(text))

    @staticmethod
    def _safe_send_filename(output_path: Path, task: JMComicTask) -> str:
        suffix = output_path.suffix or f".{task.export_format}"
        return f"JM{task.album_id}_{task.task_id}{suffix}"

    @staticmethod
    def _normalize_onebot_id(value: str) -> int | str:
        text = str(value or "").strip()
        return int(text) if text.isdigit() else text

    @staticmethod
    async def _maybe_await(value):
        if asyncio.iscoroutine(value) or hasattr(value, "__await__"):
            return await value
        return value

    def _group_delivery_target(self, task: JMComicTask) -> int | str:
        if not task.delivery_is_group:
            raise RuntimeError(
                "群文件上传仅支持群聊，请在已加入白名单的群内使用 /jm下载。"
            )
        if not task.delivery_session_id:
            raise RuntimeError("当前任务缺少群号，无法上传群文件。")
        return self._normalize_onebot_id(task.delivery_session_id)

    async def _call_onebot_group_file_upload(
        self, task: JMComicTask, file_value: str, display_name: str
    ) -> None:
        bot = task.bot_client
        if bot is None:
            raise RuntimeError(
                "当前任务没有可用的 OneBot bot client，无法上传群文件。"
            )
        target = self._group_delivery_target(task)
        call_action = getattr(bot, "call_action", None)
        if callable(call_action):
            await self._maybe_await(
                call_action(
                    "upload_group_file",
                    group_id=target,
                    file=file_value,
                    name=display_name,
                )
            )
            return
        upload_group_file = getattr(bot, "upload_group_file", None)
        if callable(upload_group_file):
            await self._maybe_await(
                upload_group_file(
                    group_id=target,
                    file=file_value,
                    name=display_name,
                )
            )
            return
        raise RuntimeError("当前 OneBot client 不支持 upload_group_file。")

    async def _upload_onebot_base64_group_file(
        self, task: JMComicTask, output_path: Path, display_name: str
    ) -> None:
        file_bytes = output_path.read_bytes()
        encoded_file = "base64://" + base64.b64encode(file_bytes).decode("ascii")
        await self._call_onebot_group_file_upload(task, encoded_file, display_name)

    def _napcat_http_headers(self) -> dict[str, str]:
        headers = {}
        if self.napcat_http_access_token:
            headers["Authorization"] = f"Bearer {self.napcat_http_access_token}"
        return headers

    def _create_napcat_http_session(self):
        import aiohttp

        timeout = aiohttp.ClientTimeout(total=self.napcat_http_timeout_seconds)
        return aiohttp.ClientSession(headers=self._napcat_http_headers(), timeout=timeout)

    async def _napcat_http_post(
        self,
        session: Any,
        action: str,
        payload: dict[str, Any],
    ) -> Any:
        url = f"{self.napcat_http_api_base}/{action}"
        try:
            async with session.post(url, json=payload) as response:
                status_code = int(getattr(response, "status", 0) or 0)
                try:
                    body = await response.json(content_type=None)
                except Exception as exc:
                    raw_text = await response.text()
                    raise RuntimeError(
                        f"NapCat HTTP 返回非 JSON 响应 ({status_code}): {raw_text[:300]}"
                    ) from exc
        except Exception as exc:
            if isinstance(exc, RuntimeError):
                raise
            raise RuntimeError(f"请求 NapCat HTTP API 失败: {exc}") from exc

        if status_code < 200 or status_code >= 300:
            raise RuntimeError(f"NapCat HTTP {status_code}: {body}")
        if not isinstance(body, dict):
            raise RuntimeError(f"NapCat HTTP 返回格式异常: {body!r}")
        if body.get("status") == "failed" or int(body.get("retcode", 0) or 0) != 0:
            message = body.get("message") or body.get("wording") or body
            raise RuntimeError(f"NapCat action {action} 失败: {message}")
        return body.get("data", body)

    @staticmethod
    def _calculate_file_sha256(output_path: Path) -> str:
        digest = hashlib.sha256()
        with output_path.open("rb") as file_obj:
            while True:
                block = file_obj.read(1024 * 1024)
                if not block:
                    break
                digest.update(block)
        return digest.hexdigest()

    async def _upload_stream_chunk_with_retry(
        self,
        session: Any,
        payload: dict[str, Any],
    ) -> Any:
        chunk_index = int(payload["chunk_index"])
        last_error: Exception | None = None
        for attempt in range(self.stream_chunk_retries + 1):
            try:
                return await self._napcat_http_post(
                    session,
                    "upload_file_stream",
                    payload,
                )
            except Exception as exc:
                last_error = exc
                if attempt >= self.stream_chunk_retries:
                    break
                await asyncio.sleep(0)
        raise NapCatHttpDeliveryError(
            "stream_transfer",
            f"分片 {chunk_index + 1} 上传失败: {last_error}",
        )

    async def _upload_napcat_http_stream_group_file(
        self,
        task: JMComicTask,
        output_path: Path,
        display_name: str,
    ) -> None:
        if not self.napcat_http_api_base:
            raise NapCatHttpDeliveryError(
                "configuration",
                "未配置 napcat_http_api_base，无法使用 NapCat HTTP Stream。",
            )

        target = self._group_delivery_target(task)
        file_size = output_path.stat().st_size
        if file_size <= 0:
            raise NapCatHttpDeliveryError("stream_transfer", "导出文件大小为 0。")

        chunk_size = self.stream_chunk_kb * 1024
        total_chunks = (file_size + chunk_size - 1) // chunk_size
        stream_id = uuid4().hex
        retention_ms = self.stream_file_retention_seconds * 1000
        sha256 = await asyncio.to_thread(self._calculate_file_sha256, output_path)

        try:
            async with self._create_napcat_http_session() as session:
                with output_path.open("rb") as file_obj:
                    for chunk_index in range(total_chunks):
                        chunk_data = await asyncio.to_thread(file_obj.read, chunk_size)
                        if not chunk_data:
                            raise NapCatHttpDeliveryError(
                                "stream_transfer",
                                f"读取分片 {chunk_index + 1} 时提前到达文件末尾。",
                            )
                        payload = {
                            "stream_id": stream_id,
                            "chunk_data": base64.b64encode(chunk_data).decode("ascii"),
                            "chunk_index": chunk_index,
                            "total_chunks": total_chunks,
                            "file_size": file_size,
                            "expected_sha256": sha256,
                            "filename": display_name,
                            "file_retention": retention_ms,
                        }
                        await self._upload_stream_chunk_with_retry(session, payload)

                try:
                    result = await self._napcat_http_post(
                        session,
                        "upload_file_stream",
                        {"stream_id": stream_id, "is_complete": True},
                    )
                except Exception as exc:
                    raise NapCatHttpDeliveryError(
                        "stream_finalize",
                        f"NapCat 合并流式文件失败: {exc}",
                    ) from exc

                if not isinstance(result, dict) or result.get("status") != "file_complete":
                    raise NapCatHttpDeliveryError(
                        "stream_finalize",
                        f"NapCat 流式文件状态异常: {result!r}",
                    )
                napcat_file_path = str(result.get("file_path", "") or "").strip()
                if not napcat_file_path:
                    raise NapCatHttpDeliveryError(
                        "stream_finalize",
                        "NapCat 未返回流式文件路径。",
                    )

                try:
                    await self._napcat_http_post(
                        session,
                        "upload_group_file",
                        {
                            "group_id": str(target),
                            "file": napcat_file_path,
                            "name": display_name,
                            "upload_file": True,
                        },
                    )
                except Exception as exc:
                    raise NapCatHttpDeliveryError(
                        "group_upload",
                        f"NapCat 上传 QQ 群文件失败: {exc}",
                    ) from exc
        except NapCatHttpDeliveryError:
            raise
        except Exception as exc:
            raise NapCatHttpDeliveryError(
                "stream_transfer",
                f"NapCat HTTP Stream 传输失败: {exc}",
            ) from exc

    def _group_file_delivery_failure_text(
        self, text: str, output_path: Path, exc: Exception
    ) -> str:
        return (
            text
            + "\nbase64 群文件上传失败，已保存在本机:"
            + f"\n{output_path}"
            + "\n诊断: 当前 NapCat/OneBot 可能不支持 upload_group_file 的 base64:// 文件参数，"
            + "或当前会话不是群聊。"
            + "\n可回退方案: 共享 AstrBot 与协议端文件目录，或配置 AstrBot callback_api_base。"
            + f"\n错误: {exc}"
        )

    def _http_stream_delivery_failure_text(
        self,
        text: str,
        output_path: Path,
        exc: Exception,
    ) -> str:
        stage = getattr(exc, "stage", "stream_transfer")
        stage_labels = {
            "configuration": "NapCat HTTP Stream 配置不完整",
            "stream_transfer": "文件分块传输到 NapCat 失败",
            "stream_finalize": "NapCat 合并流式文件失败",
            "group_upload": "NapCat 上传 QQ 群文件失败",
        }
        diagnosis = stage_labels.get(stage, "NapCat HTTP Stream 发送失败")
        advice = (
            "请确认 NapCat HTTP Server 已启用、两个容器位于同一 Docker 网络，"
            "且 napcat_http_api_base 与 Token 配置正确。"
        )
        if stage == "group_upload":
            advice = (
                "文件已经传到 NapCat，但上传 QQ 群文件失败；请检查 NapCat 日志、"
                "群文件空间和 Bot 的群文件权限。"
            )
        return (
            text
            + f"\n{diagnosis}，原文件仍保存在 AstrBot:"
            + f"\n{output_path}"
            + f"\n诊断: {advice}"
            + f"\n错误: {exc}"
        )

    async def _send_file_or_path(self, task: JMComicTask):
        output_path = Path(task.output_file)
        if not output_path.exists():
            await self._send_message(
                task.umo,
                f"JMComic 任务 {task.task_id} 已完成，但未找到导出文件。\n"
                f"保存目录: {self.export_dir / task.task_id}",
            )
            return

        size = output_path.stat().st_size
        title = task.title or f"JM{task.album_id}"
        text = (
            f"JMComic 下载完成\n"
            f"任务: {task.task_id}\n"
            f"标题: {title}\n"
            f"文件: {output_path.name}\n"
            f"大小: {self._format_size(size)}"
        )

        max_bytes = self.max_base64_file_mb * 1024 * 1024
        use_http_stream = self.file_delivery_mode == "napcat_http_stream" or (
            self.file_delivery_mode == "auto" and size > max_bytes
        )
        display_name = self._safe_send_filename(output_path, task)

        if use_http_stream:
            if not self.napcat_http_api_base:
                exc = NapCatHttpDeliveryError(
                    "configuration",
                    "未配置 napcat_http_api_base，无法使用 NapCat HTTP Stream。",
                )
                await self._send_message(
                    task.umo,
                    self._http_stream_delivery_failure_text(text, output_path, exc),
                )
                return
            await self._send_message(
                task.umo,
                text
                + "\n文件较大，正在通过 NapCat HTTP Stream 分块传输并上传群文件。",
            )
            try:
                await self._upload_napcat_http_stream_group_file(
                    task,
                    output_path,
                    display_name,
                )
                await self._send_message(task.umo, text + "\n文件已上传到群文件。")
            except Exception as exc:
                logger.warning(f"上传 JMComic NapCat HTTP Stream 群文件失败: {exc}")
                await self._send_message(
                    task.umo,
                    self._http_stream_delivery_failure_text(text, output_path, exc),
                )
            return

        if size > max_bytes:
            await self._send_message(
                task.umo,
                text
                + f"\n文件超过 base64 群文件上传限制 {self.max_base64_file_mb} MB，已保存在本机"
                + f"\n{output_path}",
            )
            return

        try:
            await self._upload_onebot_base64_group_file(task, output_path, display_name)
            await self._send_message(task.umo, text + "\n文件已上传到群文件。")
            return
        except Exception as exc:
            logger.warning(f"上传 JMComic base64 群文件失败: {exc}")
            await self._send_message(
                task.umo,
                self._group_file_delivery_failure_text(text, output_path, exc),
            )
            return

    async def _run_download_task(self, task_id: str):
        task = self.tasks[task_id]
        task_download_dir = self.download_dir / task_id
        task_export_dir = self.export_dir / task_id
        task_download_dir.mkdir(parents=True, exist_ok=True)
        task_export_dir.mkdir(parents=True, exist_ok=True)

        await self._set_task_state(task_id, "running", "正在获取并下载")
        await self._send_message(
            task.umo,
            f"JMComic 任务 {task_id} 已开始下载 JM{task.album_id}，"
            f"导出格式: {task.export_format.upper()}",
        )

        try:
            import jmcomic

            option = self._build_option(task_download_dir)
            if task.export_format == "pdf":
                extra = jmcomic.Feature.export_pdf(
                    pdf_dir=str(task_export_dir),
                    filename_rule="[JM{Aid}]{Atitle}",
                    delete_original_file=self.delete_original_after_export,
                )
            else:
                extra = jmcomic.Feature.export_zip(
                    zip_dir=str(task_export_dir),
                    filename_rule="[JM{Aid}]{Atitle}",
                    delete_original_file=self.delete_original_after_export,
                )

            album, _downloader = await jmcomic.download_album_async(
                task.album_id,
                option=option,
                extra=extra,
                check_exception=True,
            )
            output_file = self._find_export_file(task_id, task.export_format)
            if output_file is None:
                raise FileNotFoundError(
                    f"导出插件未生成 {task.export_format.upper()} 文件"
                )

            finished = await self._set_task_state(
                task_id,
                "completed",
                "下载完成",
                title=str(getattr(album, "name", "") or ""),
                output_file=str(output_file),
            )
            if finished:
                await self._send_file_or_path(finished)
        except asyncio.CancelledError:
            await self._set_task_state(task_id, "cancelled", "任务已取消")
            await self._send_message(task.umo, f"JMComic 任务 {task_id} 已取消。")
            raise
        except Exception as exc:
            logger.warning(f"JMComic 下载任务失败 {task_id}: {exc}")
            await self._set_task_state(task_id, "failed", str(exc))
            await self._send_message(
                task.umo,
                f"JMComic 任务 {task_id} 失败:\n{exc}",
            )

    async def _create_download_task(
        self,
        event: AstrMessageEvent,
        album_id: str,
        export_format: str,
    ) -> JMComicTask:
        task_id = uuid4().hex[:8]
        group_id = self._group_id(event)
        sender_id = self._sender_id(event)
        task = JMComicTask(
            task_id=task_id,
            album_id=album_id,
            export_format=export_format,
            umo=event.unified_msg_origin,
            requester_id=sender_id,
            delivery_is_group=bool(group_id),
            delivery_session_id=group_id or sender_id,
            bot_client=self._event_bot_client(event),
        )
        async with self._task_lock:
            self.tasks[task_id] = task
            task.task_handle = asyncio.create_task(self._run_download_task(task_id))
            await self._save_task_snapshots()
        return task

    @filter.command("jm帮助", alias={"jmhelp"})
    async def help(self, event: AstrMessageEvent):
        event.stop_event()
        yield event.plain_result(
            "JMComic 插件指令:\n"
            "/jm搜索 <关键词> [页码]\n"
            "/jm热门 [日|周|月] [分类] [页码]\n"
            "/jm更多 或直接发送“更多”\n"
            "/jm详情 <id>\n"
            "/jm下载 <id> [zip|pdf]\n"
            "/jm任务\n"
            "/jm取消 <task_id>\n\n"
            f"热门榜分类: {SUPPORTED_CATEGORY_HINT}\n"
            "默认仅私聊可用；群聊需在插件配置中加入白名单。"
        )

    @filter.command("jm搜索", alias={"jmsearch"})
    async def search(self, event: AstrMessageEvent):
        allowed, reason = self._is_allowed(event)
        if not allowed:
            event.stop_event()
            yield event.plain_result(reason)
            return

        event.stop_event()
        arg_text = self._strip_command(event, "jm搜索", "jmsearch")
        tokens = arg_text.split()
        if not tokens:
            yield event.plain_result("用法: /jm搜索 <关键词> [页码]")
            return

        page = 1
        if len(tokens) >= 2 and tokens[-1].isdigit():
            page = max(1, int(tokens.pop()))
        query = " ".join(tokens).strip()
        if not query:
            yield event.plain_result("请提供搜索关键词。")
            return

        self.query_sessions.pop(event.unified_msg_origin, None)
        try:
            result_page = await self._fetch_search_page(query, page)
            session = self._new_query_session(
                kind="search",
                title=f"JMComic 搜索: {query}",
                page=result_page,
                fallback_page=page,
                query=query,
            )
            if not session.items:
                yield event.plain_result(f"{self._query_header(session)}\n没有找到结果。")
                return
            text = await self._render_query_session_chunk(session)
            self._store_query_session(event.unified_msg_origin, session)
            yield event.plain_result(text)
        except Exception as exc:
            logger.warning(f"JMComic 搜索失败: {exc}")
            yield event.plain_result(f"搜索失败: {exc}")

    @filter.command("jm热门", alias={"jm排行", "jmranking"})
    async def ranking(self, event: AstrMessageEvent):
        allowed, reason = self._is_allowed(event)
        if not allowed:
            event.stop_event()
            yield event.plain_result(reason)
            return

        event.stop_event()
        arg_text = self._strip_command(event, "jm热门", "jm排行", "jmranking")
        period, category, page, error = self._parse_ranking_args(arg_text)
        if error:
            yield event.plain_result(error)
            return

        self.query_sessions.pop(event.unified_msg_origin, None)
        try:
            result_page = await self._fetch_ranking_page(period, category, page)
            period_label = RANKING_PERIOD_LABELS.get(period, period)
            category_label = JM_CATEGORY_LABELS.get(category, category)
            session = self._new_query_session(
                kind="ranking",
                title=f"JMComic 热门榜: {period_label} / {category_label}",
                page=result_page,
                fallback_page=page,
                period=period,
                category=category,
            )
            if not session.items:
                yield event.plain_result(f"{self._query_header(session)}\n没有找到结果。")
                return
            text = await self._render_query_session_chunk(session)
            self._store_query_session(event.unified_msg_origin, session)
            yield event.plain_result(text)
        except Exception as exc:
            logger.warning(f"JMComic 热门榜查询失败: {exc}")
            yield event.plain_result(f"热门榜查询失败: {exc}")

    @filter.command("jm更多", alias={"jmmore"})
    async def more(self, event: AstrMessageEvent):
        allowed, reason = self._is_allowed(event)
        if not allowed:
            event.stop_event()
            yield event.plain_result(reason)
            return

        event.stop_event()
        yield event.plain_result(await self._build_more_response(event))

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_more_message(self, event: AstrMessageEvent):
        text = (event.message_str or "").strip()
        if text != "更多":
            return

        allowed, reason = self._is_allowed(event)
        event.stop_event()
        if not allowed:
            yield event.plain_result(reason)
            return
        yield event.plain_result(await self._build_more_response(event))

    @filter.command("jm详情", alias={"jminfo"})
    async def detail(self, event: AstrMessageEvent):
        allowed, reason = self._is_allowed(event)
        if not allowed:
            event.stop_event()
            yield event.plain_result(reason)
            return

        event.stop_event()
        arg_text = self._strip_command(event, "jm详情", "jminfo")
        album_id = self._extract_jm_id(arg_text)
        if not album_id:
            yield event.plain_result("用法: /jm详情 <id>")
            return

        try:
            option = self._build_option(self.data_dir / "query-cache")
            async with option.new_jm_async_client(max_clients=3) as client:
                album = await client.get_album_detail(album_id)
            yield event.plain_result(self._format_album_detail(album))
        except Exception as exc:
            logger.warning(f"JMComic 详情查询失败: {exc}")
            yield event.plain_result(f"详情查询失败: {exc}")

    @filter.command("jm下载", alias={"jmdl"})
    async def download(self, event: AstrMessageEvent):
        allowed, reason = self._is_allowed(event)
        if not allowed:
            event.stop_event()
            yield event.plain_result(reason)
            return

        event.stop_event()
        arg_text = self._strip_command(event, "jm下载", "jmdl")
        tokens = arg_text.split()
        if not tokens:
            yield event.plain_result("用法: /jm下载 <id> [zip|pdf]")
            return

        album_id = self._extract_jm_id(tokens[0])
        if not album_id:
            yield event.plain_result("请提供有效的 JM ID。")
            return

        export_format = self.default_export_format
        if len(tokens) >= 2:
            export_format = self._parse_format_token(tokens[1]) or ""
        if export_format not in SUPPORTED_FORMATS:
            yield event.plain_result("导出格式仅支持 zip 或 pdf。")
            return

        if await self._active_task_count() >= self.max_concurrent_tasks:
            yield event.plain_result(
                f"当前已有 {self.max_concurrent_tasks} 个 JMComic 下载任务在运行，"
                "请稍后再试。"
            )
            return

        task = await self._create_download_task(event, album_id, export_format)
        yield event.plain_result(
            f"JMComic 下载任务已创建: {task.task_id}\n"
            f"目标: JM{album_id}\n"
            f"格式: {export_format.upper()}\n"
            "完成后会自动发送文件或保存路径。"
        )

    @filter.command("jm任务", alias={"jmtasks"})
    async def list_tasks(self, event: AstrMessageEvent):
        allowed, reason = self._is_allowed(event)
        if not allowed:
            event.stop_event()
            yield event.plain_result(reason)
            return

        event.stop_event()
        umo = event.unified_msg_origin
        async with self._task_lock:
            tasks = [
                task
                for task in self.tasks.values()
                if task.umo == umo
            ]
        tasks.sort(key=lambda item: item.created_at, reverse=True)
        tasks = tasks[:10]
        if not tasks:
            yield event.plain_result("当前会话没有 JMComic 任务。")
            return

        lines = ["当前会话 JMComic 任务:"]
        for task in tasks:
            title = f" | {task.title}" if task.title else ""
            lines.append(
                f"{task.task_id} | JM{task.album_id} | "
                f"{task.export_format.upper()} | {task.status}{title}\n"
                f"  创建: {self._format_time(task.created_at)}"
                f" | 信息: {task.message or '-'}"
            )
        yield event.plain_result("\n".join(lines))

    @filter.command("jm取消", alias={"jmcancel"})
    async def cancel_task(self, event: AstrMessageEvent):
        allowed, reason = self._is_allowed(event)
        if not allowed:
            event.stop_event()
            yield event.plain_result(reason)
            return

        event.stop_event()
        arg_text = self._strip_command(event, "jm取消", "jmcancel")
        task_id = arg_text.split()[0] if arg_text.split() else ""
        if not task_id:
            yield event.plain_result("用法: /jm取消 <task_id>")
            return

        async with self._task_lock:
            task = self.tasks.get(task_id)
            if not task or task.umo != event.unified_msg_origin:
                message = "未找到当前会话中的这个任务。"
                handle = None
                can_cancel = False
            elif task.status not in ACTIVE_STATUSES:
                message = f"任务 {task_id} 当前状态为 {task.status}，无法取消。"
                handle = None
                can_cancel = False
            else:
                message = ""
                handle = task.task_handle
                can_cancel = True

        if not can_cancel:
            yield event.plain_result(message)
            return

        if handle and not handle.done():
            handle.cancel()
            yield event.plain_result(f"已请求取消 JMComic 任务 {task_id}。")
        else:
            await self._set_task_state(task_id, "cancelled", "任务已取消")
            yield event.plain_result(f"JMComic 任务 {task_id} 已标记为取消。")
