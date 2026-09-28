"""Shared album queries and byte-backed message cards."""

from __future__ import annotations

import io

from astrbot.api.event import MessageChain
import astrbot.api.message_components as Comp


async def fetch_album_detail(plugin, album_id: str):
    option = plugin._build_option(plugin.data_dir / "query-cache")
    async with option.new_jm_async_client(max_clients=3) as client:
        return await client.get_album_detail(album_id)


async def fetch_album_cover(plugin, album_id: str, cache_dir=None) -> bytes:
    import jmcomic

    option = plugin._build_option(cache_dir or plugin.data_dir / "query-cache")
    async with option.new_jm_async_client(max_clients=1) as client:
        url = jmcomic.JmcomicText.get_album_cover_url(album_id)
        response = await client.get_jm_image(url)
        return bytes(response.content)


def validate_cover(content: bytes):
    from PIL import Image

    if not content or len(content) > 10 * 1024 * 1024:
        raise ValueError("封面为空或超过 10 MB")
    with Image.open(io.BytesIO(content)) as image:
        image.verify()


def album_message(
    album_id: str,
    title: str,
    tags: list[str],
    cover: bytes | None,
    prefix: str = "",
) -> MessageChain:
    text = (
        f"{prefix}标题: {title}\n"
        f"ID: JM{album_id}\n"
        f"标签: {'、'.join(tags) if tags else '暂无标签'}\n"
        f"下载: /jm下载 {album_id}"
    )
    components = []
    if cover is not None:
        components.append(Comp.Image.fromBytes(cover))
    else:
        text += "\n封面暂不可用"
    components.append(Comp.Plain(text=text))
    return MessageChain(chain=components)
