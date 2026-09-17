"""指令文本解析与配置读取测试。"""

import asyncio

from conftest import import_plugin_main
from core.constants import COMMAND_SHORT
from core.generator import GenerationResult

main = import_plugin_main()
Plugin = main.GeminiImagePlugin
COMMANDS = (main.COMMAND_MAIN, *main.COMMAND_ALIASES)


def make_plugin(config: dict | None = None) -> object:
    """绕过 __init__ 构造实例，只测纯逻辑方法。"""
    plugin = Plugin.__new__(Plugin)
    plugin.config = config or {}
    return plugin


# --------------------------------------------------------------- 指令名剥离


def test_extract_prompt_basic():
    assert Plugin._extract_prompt("/gemini生图 一只猫", COMMANDS) == "一只猫"


def test_extract_prompt_keeps_inner_spaces():
    assert Plugin._extract_prompt("/gemini生图 一只猫 在屋顶 上", COMMANDS) == "一只猫 在屋顶 上"


def test_extract_prompt_handles_prefix_marks():
    assert Plugin._extract_prompt("！gemini生图 猫", COMMANDS) == "猫"
    assert Plugin._extract_prompt("gemini生图 猫", COMMANDS) == "猫"


def test_extract_prompt_supports_alias():
    assert Plugin._extract_prompt("/gimg 猫", COMMANDS) == "猫"
    assert Plugin._extract_prompt("/gimage 猫", COMMANDS) == "猫"


def test_extract_prompt_without_argument():
    assert Plugin._extract_prompt("/gemini生图", COMMANDS) == ""
    assert Plugin._extract_prompt("/gemini生图   ", COMMANDS) == ""


def test_extract_prompt_for_short_command():
    assert Plugin._extract_prompt("/生图 猫", (COMMAND_SHORT,)) == "猫"


def test_extract_prompt_empty_message():
    assert Plugin._extract_prompt("", COMMANDS) == ""
    assert Plugin._extract_prompt(None, COMMANDS) == ""


def test_extract_prompt_command_inside_text_is_not_stripped():
    text = "帮我 gemini生图 一只猫"
    assert Plugin._extract_prompt(text, COMMANDS) == text


# --------------------------------------------------------------- 短指令开关


def test_short_command_disabled_replies_hint():
    """短指令关闭时必须回复提示，不能静默吞消息（AstrBot 会阻断事件传播）。"""
    plugin = make_plugin({"enable_short_command": False})

    class _Event:
        message_str = "/生图 一只猫"

        def plain_result(self, text):
            return text

    async def collect():
        return [item async for item in plugin.cmd_generate_short(_Event())]

    replies = asyncio.run(collect())
    assert len(replies) == 1
    assert "未开启" in replies[0]


# ------------------------------------------------------------------- 配置读取


def test_cfg_bool_accepts_string_and_bool():
    plugin = make_plugin({"a": True, "b": "true", "c": "false", "d": "0"})
    assert plugin._cfg_bool("a") is True
    assert plugin._cfg_bool("b") is True
    assert plugin._cfg_bool("c") is False
    assert plugin._cfg_bool("d") is False
    assert plugin._cfg_bool("missing", True) is True


def test_cfg_int_falls_back_on_bad_value():
    plugin = make_plugin({"a": "12", "b": "abc"})
    assert plugin._cfg_int("a", 5) == 12
    assert plugin._cfg_int("b", 5) == 5
    assert plugin._cfg_int("missing", 7) == 7


def test_cfg_list_accepts_string_and_list():
    plugin = make_plugin({"a": "111,222\n333", "b": ["444", " 555 "]})
    assert plugin._cfg_list("a") == ["111", "222", "333"]
    assert plugin._cfg_list("b") == ["444", "555"]
    assert plugin._cfg_list("missing") == []


def test_cfg_str_handles_none():
    plugin = make_plugin({"a": None})
    assert plugin._cfg_str("a") == ""
    assert plugin._cfg_str("missing", "x") == "x"


# --------------------------------------------------------------- 回复文字构造


def test_note_text_truncates_long_reply():
    plugin = make_plugin({"include_text": True, "max_text_length": 5})
    result = GenerationResult(prompt="猫", text="一二三四五六七八九")
    assert plugin._note_text(result) == "一二三四五…"


def test_note_text_mentions_dropped_images():
    plugin = make_plugin({"include_text": False, "max_text_length": 0})
    result = GenerationResult(prompt="猫", dropped=2)
    assert "另有 2 张未发送" in plugin._note_text(result)


def test_note_text_can_be_empty():
    plugin = make_plugin({"include_text": False, "max_text_length": 0})
    assert plugin._note_text(GenerationResult(prompt="猫")) == ""


def test_no_image_text_reports_download_failure():
    """生成成功但下载失败时，不能再谎报「没有返回图片」。"""
    plugin = make_plugin({})
    text = plugin._no_image_text(GenerationResult(prompt="猫", download_failed=3))
    assert "下载失败" in text
    assert "3" in text


def test_no_image_text_mentions_web_images():
    plugin = make_plugin({})
    text = plugin._no_image_text(GenerationResult(prompt="猫", web_image_count=2))
    assert "联网搜到" in text
