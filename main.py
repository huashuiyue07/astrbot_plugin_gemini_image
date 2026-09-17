"""astrbot_plugin_gemini_image — 网页版 Gemini 生图插件。

在 QQ 里发一句描述，插件调用 **gemini.google.com 网页版**（Nano Banana）
生成图片，并把图片作为回复发给发送者。

- 走浏览器 Cookie（__Secure-1PSID / __Secure-1PSIDTS）认证，**不使用 Google 官方 API**；
- 支持文生图与图生图（带图改图）；
- 群聊按需 @ 发送者，支持群白/黑名单、管理员限定、冷却与每日配额。
"""

from __future__ import annotations

import asyncio
import re
import time
import traceback
from pathlib import Path

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register

from .core import (
    COMMAND_ALIASES,
    COMMAND_HELP,
    COMMAND_MAIN,
    COMMAND_RESET,
    COMMAND_SHORT,
    COMMAND_STATUS,
    DEFAULT_EDIT_PROMPT_PREFIX,
    DEFAULT_MAX_TEXT_LENGTH,
    DEFAULT_PROMPT_PREFIX,
    IMAGES_DIRNAME,
    LIMITER_FILENAME,
    PLUGIN_AUTHOR,
    PLUGIN_DESC,
    PLUGIN_NAME,
    PLUGIN_REPO,
    PLUGIN_VERSION,
    GeminiImageClient,
    GeminiImageError,
    ImageGenerator,
    UsageLimiter,
    describe_exception,
)

#: 清理后台任务的执行间隔（秒）
CLEAN_INTERVAL_SEC = 12 * 3600


@register(PLUGIN_NAME, PLUGIN_AUTHOR, PLUGIN_DESC, PLUGIN_VERSION, PLUGIN_REPO)
class GeminiImagePlugin(Star):
    """网页版 Gemini 生图插件。"""

    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config if config is not None else {}

        self.data_dir = self._resolve_data_dir()
        self.images_dir = self.data_dir / IMAGES_DIRNAME
        self.images_dir.mkdir(parents=True, exist_ok=True)

        # ---- Web 客户端（懒连接，第一次生图时才真正建立） ----
        self.client = GeminiImageClient(
            self._cfg_str("secure_1psid"),
            self._cfg_str("secure_1psidts"),
            proxy=self._cfg_str("proxy_url") if self._cfg_bool("use_proxy", False) else "",
            cookie_dir=self.data_dir / "cookies",
            auto_refresh=self._cfg_bool("auto_refresh_cookies", True),
            init_timeout=self._cfg_int("init_timeout", 60),
            watchdog_timeout=self._cfg_int("watchdog_timeout", 180),
            generate_timeout=self._cfg_int("request_timeout", 300),
            min_interval=self._cfg_int("min_request_interval", 5),
            blocked_cooldown=self._cfg_int("blocked_cooldown_seconds", 300),
            verbose=self._cfg_bool("verbose_log", False),
        )

        # ---- 生图编排 ----
        self.generator = ImageGenerator(
            self.client,
            self.images_dir,
            max_images=self._cfg_int("max_images", 4),
            prompt_prefix=self._cfg_str("prompt_prefix") or DEFAULT_PROMPT_PREFIX,
            edit_prompt_prefix=self._cfg_str("edit_prompt_prefix") or DEFAULT_EDIT_PROMPT_PREFIX,
            model=self._cfg_str("model"),
            temporary=self._cfg_bool("temporary_chat", True),
        )

        # ---- 限流 ----
        self.limiter = UsageLimiter(
            self.data_dir / LIMITER_FILENAME,
            cooldown_seconds=self._cfg_int("cooldown_seconds", 30),
            daily_quota=self._cfg_int("daily_quota", 20),
            global_daily_quota=self._cfg_int("global_daily_quota", 200),
        )

        # ---- 图片缓存清理 ----
        self._clean_task: asyncio.Task | None = None
        if self._cfg_int("image_retain_days", 3) > 0:
            try:
                self._clean_task = asyncio.create_task(self._clean_loop())
            except RuntimeError:
                logger.warning("[gemini-image] 无法启动缓存清理任务：当前没有运行中的事件循环")

        logger.info(
            f"Gemini 生图插件已加载 | 数据目录: {self.data_dir} | "
            f"{'Cookie 已配置' if self.client.configured else '⚠️ 尚未配置 Cookie'}"
        )

    async def terminate(self) -> None:
        """插件卸载/重载时释放连接与后台任务。"""
        task = self._clean_task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        try:
            await self.client.close()
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[gemini-image] 关闭连接失败：{exc}")

    # ================================================================== 指令

    @filter.command(COMMAND_MAIN, alias=COMMAND_ALIASES)
    async def cmd_generate(self, event: AstrMessageEvent):
        """生成图片：gemini生图 <画面描述>（消息里带图则为图生图）"""
        prompt = self._extract_prompt(event.message_str, (COMMAND_MAIN, *COMMAND_ALIASES))
        async for item in self._run_generate(event, prompt):
            yield item

    @filter.command(COMMAND_SHORT)
    async def cmd_generate_short(self, event: AstrMessageEvent):
        """短指令「生图」，默认关闭以避免与其它插件抢指令"""
        if not self._cfg_bool("enable_short_command", False):
            return
        prompt = self._extract_prompt(event.message_str, (COMMAND_SHORT,))
        async for item in self._run_generate(event, prompt):
            yield item

    @filter.command(COMMAND_STATUS)
    async def cmd_status(self, event: AstrMessageEvent):
        """查看插件自检状态"""
        if not event.is_admin() and not event.is_private_chat():
            yield event.plain_result("🚫 该指令仅限管理员或私聊使用")
            return

        yield event.plain_result("🔍 正在自检，请稍候…")

        ok, message = await self.client.health_check()
        used, quota = await self.limiter.usage(event.get_sender_id())
        total = await self.limiter.total_usage()
        global_quota = self._cfg_int("global_daily_quota", 200)

        lines = [
            f"{'✅' if ok else '❌'} 连接自检：{message}",
            f"🍪 Cookie：{'已配置' if self.client.configured else '未配置（请填写 secure_1psid / secure_1psidts）'}",
            f"🌐 代理：{self.client.proxy or '未使用（国内网络建议配置）'}",
            f"🧠 模型：{self._cfg_str('model') or '账号默认'}",
            f"🖼 单次最多发图：{self._cfg_int('max_images', 4)} 张",
            f"🚦 防封节流：{self.client.throttle_status()}",
            f"⏱ 你的冷却：{self._cfg_int('cooldown_seconds', 30)} 秒 | 你的日配额："
            f"{quota if quota else '不限'}（今日已用 {used}）",
            f"📊 全站今日总量：{total} / {global_quota if global_quota else '不限'}",
            f"📁 数据目录：{self.data_dir}",
        ]
        yield event.plain_result("\n".join(lines))

    @filter.command(COMMAND_RESET)
    async def cmd_reset(self, event: AstrMessageEvent):
        """重置限流数据并重建连接（管理员）"""
        if not event.is_admin():
            yield event.plain_result("🚫 该指令仅限管理员使用")
            return
        cleared = await self.limiter.reset()
        await self.client.discard()
        yield event.plain_result(f"♻️ 已重置 {cleared} 个用户的用量，连接已释放（下次生图会重新建立）")

    @filter.command(COMMAND_HELP)
    async def cmd_help(self, event: AstrMessageEvent):
        """查看帮助"""
        yield event.plain_result(self._help_text())

    # ============================================================== 主流程

    async def _run_generate(self, event: AstrMessageEvent, prompt: str):
        """生成指令的公共流程（命令行与短指令共用）。"""
        denied = await self._guard(event)
        if denied:
            yield event.plain_result(denied)
            return

        prompt = (prompt or "").strip()
        try:
            input_images = await self._collect_input_images(event)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[gemini-image] 输入图片解析失败：{exc}")
            input_images = []

        if not prompt and not input_images:
            yield event.plain_result(self._help_text())
            return

        if self._cfg_bool("send_progress", True):
            action = "改图" if input_images else "生成"
            yield event.plain_result(f"🎨 正在{action}，请稍候…（通常 20~60 秒）")

        try:
            result = await self.generator.generate(prompt, input_images=input_images)
        except asyncio.CancelledError:
            raise
        except GeminiImageError as exc:
            logger.warning(f"[gemini-image] 生成失败：{exc.message}")
            yield event.plain_result(exc.to_user_text())
            return
        except Exception as exc:  # noqa: BLE001
            logger.error(
                f"[gemini-image] 未预期错误：{type(exc).__name__}: {exc}\n{traceback.format_exc()}"
            )
            yield event.plain_result(describe_exception(exc).to_user_text())
            return

        # 只有真出了图才计用量，避免失败也扣配额
        if result.has_images:
            await self.limiter.consume(event.get_sender_id())

        if not result.has_images:
            yield event.plain_result(self._no_image_text(result))
            return

        yield event.chain_result(self._build_chain(event, result))

    # ============================================================== 权限

    async def _guard(self, event: AstrMessageEvent) -> str | None:
        """返回拒绝文案；None 表示放行。"""
        group_id = str(event.get_group_id() or "")
        if group_id:
            if not self._cfg_bool("allow_group", True):
                return "🚫 本插件未开启群聊使用"
            whitelist = self._cfg_list("group_whitelist")
            if whitelist and group_id not in whitelist:
                return "🚫 本群不在生图白名单内"
            if group_id in self._cfg_list("group_blacklist"):
                return "🚫 本群已被禁用生图"

        if self._cfg_bool("admin_only", False) and not event.is_admin():
            return "🚫 生图功能仅限管理员使用"

        return await self.limiter.check(str(event.get_sender_id()))

    async def _collect_input_images(self, event: AstrMessageEvent) -> list[str]:
        """取出用户消息里附带的图片（图生图用），转成本地路径。"""
        limit = self._cfg_int("max_input_images", 1)
        if limit <= 0:
            return []

        collected: list[str] = []
        for component in event.get_messages():
            if not isinstance(component, Comp.Image):
                continue
            try:
                path = await component.convert_to_file_path()
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[gemini-image] 输入图片下载失败：{exc}")
                continue
            if path:
                collected.append(path)
            if len(collected) >= limit:
                break
        return collected

    # ============================================================== 消息构造

    def _build_chain(self, event: AstrMessageEvent, result) -> list:
        """构造回复消息链：@发送者 + 图片 + 说明文字。"""
        chain: list = []

        if self._cfg_bool("at_sender", True) and not event.is_private_chat():
            sender = str(event.get_sender_id())
            if sender:
                chain.append(Comp.At(qq=sender))

        for path in result.image_paths:
            chain.append(Comp.Image(file=path))

        note = self._note_text(result)
        if note:
            chain.append(Comp.Plain(note))
        return chain

    def _note_text(self, result) -> str:
        """图片后面的说明文字（Gemini 的回复摘要 + 省略提示）。"""
        parts: list[str] = []
        if self._cfg_bool("include_text", True) and result.text:
            limit = self._cfg_int("max_text_length", DEFAULT_MAX_TEXT_LENGTH)
            text = result.text.replace("\n", " ").strip()
            if limit > 0 and len(text) > limit:
                text = text[:limit] + "…"
            if text:
                parts.append(text)
        if result.dropped > 0:
            parts.append(f"（另有 {result.dropped} 张未发送）")
        return "\n".join(parts)

    def _no_image_text(self, result) -> str:
        """没出图时给出可执行的原因说明。"""
        if result.download_failed > 0:
            return (
                f"⚠️ 图片其实生成出来了，但有 {result.download_failed} 张下载失败。\n"
                "💡 通常是网络/代理不稳定导致的，稍后重试一次；\n"
                "若持续失败，检查代理是否可用（或把出口切到别的节点）。"
            )

        if result.web_image_count > 0:
            return (
                f"⚠️ Gemini 这次返回的是联网搜到的 {result.web_image_count} 张图，而不是 AI 生成的图。\n"
                "💡 把描述写得更明确一点，例如：\n"
                "gemini生图 画一张：一只戴着墨镜的柴犬在沙滩上冲浪，插画风格\n"
                "（也可以在插件配置里把 prompt_prefix 改得更强硬，如 \"Generate an image, do not search the web: \"）"
            )

        detail = f"\n💬 Gemini 说：{result.text[:200]}" if result.text else ""
        return (
            "⚠️ Gemini 没有返回图片。\n"
            "💡 常见原因：描述过于抽象、触发了内容策略、或账号当前不支持生图。换个说法再试一次吧。"
            f"{detail}"
        )

    def _help_text(self) -> str:
        prefix = self._cfg_str("command_prefix_hint") or "/"
        lines = [
            "🎨 网页版 Gemini 生图（无需 API Key）",
            "",
            f"{prefix}{COMMAND_MAIN} <画面描述> — 生成图片",
            f"{prefix}{COMMAND_MAIN}（带图片）<修改要求> — 图生图／改图",
            f"{prefix}{COMMAND_STATUS} — 自检 Cookie / 代理 / 连接",
            f"{prefix}{COMMAND_RESET} — 管理员：重置限流并重建连接",
            f"{prefix}{COMMAND_HELP} — 显示本帮助",
            "",
            "示例：",
            f"{prefix}{COMMAND_MAIN} 一只戴着墨镜的柴犬在沙滩上冲浪，插画风格",
        ]
        if self._cfg_bool("enable_short_command", False):
            lines.append(f"{prefix}{COMMAND_SHORT} <画面描述> — 同上（短指令已开启）")
        return "\n".join(lines)

    # ============================================================== 工具

    @staticmethod
    def _extract_prompt(message: str, commands: tuple[str, ...]) -> str:
        """从原始消息里剥掉指令名，取出后面的画面描述。

        不依赖 AstrBot 的参数解析，避免中文描述被空格切碎。
        """
        text = (message or "").strip()
        if not text:
            return ""

        for command in commands:
            if not command:
                continue
            pattern = rf"^[/!！。．,，]?\s*{re.escape(command)}\s*"
            stripped = re.sub(pattern, "", text, count=1)
            if stripped != text:
                return stripped.strip()

        # 兜底：首词正好是指令名（可能被其它字符隔开）
        head, _, tail = text.partition(" ")
        if head.lstrip("/!！。．") in commands:
            return tail.strip()
        return text

    def _resolve_data_dir(self) -> Path:
        try:
            data_dir = Path(StarTools.get_data_dir(PLUGIN_NAME))
        except Exception as exc:  # noqa: BLE001 - 拿不到就退化为插件目录
            logger.error(f"[gemini-image] 获取数据目录失败，改用插件目录：{exc}")
            data_dir = Path(__file__).parent / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        return data_dir

    def _cfg(self, key: str, default=None):
        value = None
        try:
            value = self.config.get(key, default)
        except Exception:  # noqa: BLE001
            return default
        return default if value is None else value

    def _cfg_str(self, key: str, default: str = "") -> str:
        return str(self._cfg(key, default) or "")

    def _cfg_bool(self, key: str, default: bool = False) -> bool:
        value = self._cfg(key, default)
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on", "是")
        return bool(value)

    def _cfg_int(self, key: str, default: int = 0) -> int:
        try:
            return int(self._cfg(key, default))
        except (TypeError, ValueError):
            return default

    def _cfg_list(self, key: str) -> list[str]:
        """读取列表配置，兼容「列表」与「逗号/换行分隔的字符串」两种填法。"""
        value = self._cfg(key, [])
        if isinstance(value, (list, tuple, set)):
            items = [str(v).strip() for v in value]
        else:
            items = [v.strip() for v in re.split(r"[,，\n]", str(value or ""))]
        return [v for v in items if v]

    # ============================================================== 缓存清理

    async def _clean_loop(self) -> None:
        interval = max(1, CLEAN_INTERVAL_SEC)
        while True:
            await asyncio.sleep(interval)
            try:
                removed = await asyncio.to_thread(self._clean_once)
                if removed:
                    logger.info(f"[gemini-image] 已清理 {removed} 个过期图片")
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[gemini-image] 清理缓存失败：{exc}")

    def _clean_once(self) -> int:
        retain_days = self._cfg_int("image_retain_days", 3)
        if retain_days <= 0:
            return 0
        deadline = time.time() - retain_days * 86400
        removed = 0
        for file in self.images_dir.glob("*"):
            try:
                if file.is_file() and file.stat().st_mtime < deadline:
                    file.unlink()
                    removed += 1
            except OSError:
                continue
        return removed
