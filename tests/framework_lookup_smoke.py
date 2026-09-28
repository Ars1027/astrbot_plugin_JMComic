"""Run separately with an installed AstrBot environment; no platform sends."""

import asyncio
import io
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


async def check(root):
    from PIL import Image as PILImage
    from astrbot.api.event import AstrMessageEvent
    from astrbot.api.message_components import Image, Plain
    from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
    from astrbot.core.platform.message_type import MessageType
    from astrbot.core.platform.platform_metadata import PlatformMetadata
    from astrbot.core.star.star_handler import EventType, star_handlers_registry

    import main

    plugin = main.JMComicPlugin(
        SimpleNamespace(), {"download": {"download_base_dir": root}}
    )
    handlers = [
        handler
        for handler in star_handlers_registry.get_handlers_by_event_type(
            EventType.AdapterMessageEvent
        )
        if handler.handler_module_path == "main"
    ]
    assert any(handler.handler_name == "on_jm_message" for handler in handlers)
    buffer = io.BytesIO()
    PILImage.new("RGB", (2, 3), "white").save(buffer, format="JPEG")

    def event_for(text, sender="123"):
        message = AstrBotMessage()
        message.type = MessageType.FRIEND_MESSAGE
        message.self_id = "999"
        message.sender = MessageMember(sender, "tester")
        message.message = [Plain(text)]
        message.message_str = text
        message.message_id = "test"
        event = AstrMessageEvent(
            text.lstrip("/!"),
            message,
            PlatformMetadata("aiocqhttp", "test", "test-bot"),
            sender,
        )
        event.is_at_or_wake_command = True
        return event

    async def dispatch(event):
        active = []
        params = {}
        for handler in handlers:
            if all(f.filter(event, {}) for f in handler.event_filters):
                active.append(handler)
                if "parsed_params" in event.get_extra():
                    params[handler.handler_full_name] = event.get_extra("parsed_params")
            event.get_extra().pop("parsed_params", None)
        event.set_extra("handlers_parsed_params", params)
        results = []
        for handler in active:
            if event.is_stopped():
                break
            results.extend([item async for item in handler.handler(plugin, event)])
        return results

    with (
        patch.object(
            main,
            "fetch_album_detail",
            AsyncMock(return_value=SimpleNamespace(name="Sample", tags=["a", "b"])),
        ) as detail,
        patch.object(
            main, "fetch_album_cover", AsyncMock(return_value=buffer.getvalue())
        ),
    ):
        for text in (
            "看看 JM 123456 和 jm789",
            "/jm识别 JM 123456",
            "!jmlookup jm123456",
            "/jm详情 JM123456",
            "jminfo JM123456",
        ):
            detail.reset_mock()
            result = await dispatch(event_for(text))
            assert len(result) == 1, text
            assert len(result[0].chain) == 2, text
            assert isinstance(result[0].chain[0], Image), text
            assert result[0].chain[0].file.startswith("base64://"), text
            assert "ID: JM123456" in result[0].chain[1].text, text
            detail.assert_awaited_once_with(plugin, "123456")
        for text, sender in (("123456", "123"), ("JM123456", "999")):
            detail.reset_mock()
            assert await dispatch(event_for(text, sender)) == []
            detail.assert_not_awaited()
    print(
        "AstrBot real registry/filter/event/card smoke: 7 scenarios passed (no sends)"
    )


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    with tempfile.TemporaryDirectory(
        prefix="jm-framework-", ignore_cleanup_errors=True
    ) as root:
        os.environ["ASTRBOT_ROOT"] = root
        asyncio.run(check(root))
