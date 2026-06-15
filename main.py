from __future__ import annotations

import asyncio
import base64
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
FILE_DELIVERY_MODES = {"onebot_group_file_base64"}
FORMAT_ALIASES = {
    "zip": "zip",
    "压缩": "zip",
    "压缩包": "zip",
    "pdf": "pdf",
}


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


@register(
    PLUGIN_NAME,
    "Ars1027",
    "JMComic 的 AstrBot 查询与异步下载插件",
    "v0.1.0",
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
                    "onebot_group_file_base64",
                )
                or "onebot_group_file_base64"
            )
        )
        self.max_base64_file_mb = max(
            1, int(self._cfg("download", "max_base64_file_mb", 50) or 50)
        )
        self.delete_original_after_export = bool(
            self._cfg("download", "delete_original_after_export", True)
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
        return mode if mode in FILE_DELIVERY_MODES else "onebot_group_file_base64"

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
        if len(items) > limit:
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

    def _format_search_page(self, page: Any, query: str) -> str:
        page_no = getattr(page, "page", None)
        page_count = getattr(page, "page_count", None)
        total = getattr(page, "total", None)
        header = f"JMComic 搜索: {query}"
        if page_no is not None and page_count is not None:
            header += f" (第 {page_no}/{page_count} 页)"
        elif total is not None:
            header += f" (约 {total} 条)"

        rows = []
        try:
            iterator = page.iter_id_title_tag()
        except Exception:
            iterator = ((aid, title, []) for aid, title in page)

        for index, item in enumerate(iterator, start=1):
            if len(item) == 3:
                album_id, title, tags = item
            else:
                album_id, title = item
                tags = []
            tag_text = self._limit_join(tags, 5)
            rows.append(f"{index}. JM{album_id}  {title}\n   标签: {tag_text}")
            if index >= 10:
                break

        if not rows:
            return f"{header}\n没有找到结果。"
        return header + "\n" + "\n".join(rows)

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

    async def _call_onebot_group_file_upload(
        self, task: JMComicTask, file_value: str, display_name: str
    ) -> None:
        bot = task.bot_client
        if bot is None:
            raise RuntimeError(
                "当前任务没有可用的 OneBot bot client，无法上传群文件。"
            )
        if not task.delivery_is_group:
            raise RuntimeError("群文件上传仅支持群聊，请在已加入白名单的群内使用 /jm下载。")
        if not task.delivery_session_id:
            raise RuntimeError("当前任务缺少群号，无法上传群文件。")
        target = self._normalize_onebot_id(task.delivery_session_id)
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

    def _group_file_delivery_failure_text(
        self, text: str, output_path: Path, exc: Exception
    ) -> str:
        return (
            text
            + "\nbase64 群文件上传失败，已保存在本机:"
            + "\n诊断: 当前 NapCat/OneBot 可能不支持 upload_group_file 的 base64:// 文件参数，"
            + "或当前会话不是群聊。"
            + "\n可回退方案: 共享 AstrBot 与协议端文件目录，或配置 AstrBot callback_api_base。"
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
        if size > max_bytes:
            await self._send_message(
                task.umo,
                text
                + f"\n文件超过 base64 群文件上传限制 {self.max_base64_file_mb} MB，已保存在本机",
            )
            return

        display_name = self._safe_send_filename(output_path, task)

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
            "/jm详情 <id>\n"
            "/jm下载 <id> [zip|pdf]\n"
            "/jm任务\n"
            "/jm取消 <task_id>\n\n"
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

        try:
            option = self._build_option(self.data_dir / "query-cache")
            async with option.new_jm_async_client(max_clients=3) as client:
                result_page = await client.search_site(query, page=page)
            yield event.plain_result(self._format_search_page(result_page, query))
        except Exception as exc:
            logger.warning(f"JMComic 搜索失败: {exc}")
            yield event.plain_result(f"搜索失败: {exc}")

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
