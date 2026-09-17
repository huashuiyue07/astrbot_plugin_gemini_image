"""生图编排：提示词构造 → 调用 → 挑图 → 落盘。

只关心「怎么把一次生图请求做完整」，不掺和 AstrBot 的消息发送细节。
"""

from __future__ import annotations

import contextlib
import inspect
import time
from dataclasses import dataclass, field
from pathlib import Path

from astrbot.api import logger

from .client import GeminiImageClient
from .constants import DEFAULT_EDIT_PROMPT_PREFIX, DEFAULT_PROMPT_PREFIX
from .errors import GeminiImageError


@dataclass(slots=True)
class GenerationResult:
    """一次生图的结构化结果。"""

    prompt: str
    text: str = ""
    image_paths: list[str] = field(default_factory=list)
    #: Gemini 顺带回的网络图片数量（说明提示词被理解成了「搜图」）
    web_image_count: int = 0
    #: 因为超过 max_images 而没有发送的图片数
    dropped: int = 0
    #: 图生成出来了、但下载落盘失败的张数（网络/代理/SSRF 拦截等）
    download_failed: int = 0

    @property
    def has_images(self) -> bool:
        return bool(self.image_paths)


class ImageGenerator:
    def __init__(
        self,
        client: GeminiImageClient,
        output_dir: str | Path,
        *,
        max_images: int = 4,
        prompt_prefix: str = DEFAULT_PROMPT_PREFIX,
        edit_prompt_prefix: str = DEFAULT_EDIT_PROMPT_PREFIX,
        model: str | None = None,
        temporary: bool = True,
    ) -> None:
        self.client = client
        self.output_dir = Path(output_dir)
        self.max_images = max(1, int(max_images))
        self.prompt_prefix = prompt_prefix if prompt_prefix is not None else ""
        self.edit_prompt_prefix = edit_prompt_prefix if edit_prompt_prefix is not None else ""
        self.model = (model or "").strip() or None
        self.temporary = bool(temporary)

    # ------------------------------------------------------------------ 提示词

    def build_prompt(self, prompt: str, *, editing: bool) -> str:
        """补上生成/编辑意图。

        Gemini 网页版对「给图片」和「生成图片」区分得很死：只说描述它就去联网搜图，
        返回的是 WebImage 而不是 AI 生成的图。所以这里统一补前缀。
        """
        text = (prompt or "").strip()
        if editing:
            if not text:
                text = "基于这张图生成一张新图"
            prefix = self.edit_prompt_prefix
        else:
            prefix = self.prompt_prefix
        return f"{prefix}{text}" if prefix else text

    # ------------------------------------------------------------------ 生成

    async def generate(
        self,
        prompt: str,
        *,
        input_images: list[str] | None = None,
    ) -> GenerationResult:
        """执行一次生图。`input_images` 非空时走图生图（改图）。"""
        text = (prompt or "").strip()
        images = [str(p) for p in (input_images or []) if p]
        if not text and not images:
            raise GeminiImageError(
                "请描述你想生成的画面",
                hint="例：gemini生图 一只戴着墨镜的柴犬在沙滩上冲浪",
            )

        full_prompt = self.build_prompt(text, editing=bool(images))
        logger.debug(f"[gemini-image] 发送提示词：{full_prompt[:200]}")

        started = time.monotonic()
        output = await self.client.generate(
            full_prompt,
            files=images or None,
            model=self.model,
            temporary=self.temporary,
        )
        logger.debug(f"[gemini-image] Gemini 返回耗时 {time.monotonic() - started:.1f}s")

        result = GenerationResult(prompt=text, text=self._extract_text(output))

        candidate = self._chosen_candidate(output)
        if candidate is None:
            return result

        # 只要 AI 生成的图；web_images 是「联网搜到的图」，发出去等于答非所问
        generated = list(getattr(candidate, "generated_images", []) or [])
        result.web_image_count = len(getattr(candidate, "web_images", []) or [])

        if len(generated) > self.max_images:
            result.dropped = len(generated) - self.max_images
            generated = generated[: self.max_images]

        result.image_paths, result.download_failed = await self._save_images(generated)
        return result

    # ------------------------------------------------------------------ 内部

    @staticmethod
    def _chosen_candidate(output):
        """安全取出被选中的候选回复。"""
        try:
            return output.candidates[output.chosen]
        except (AttributeError, IndexError, TypeError):
            try:
                return output.candidates[0]
            except (AttributeError, IndexError, TypeError):
                return None

    @staticmethod
    def _extract_text(output) -> str:
        try:
            return (output.text or "").strip()
        except Exception:  # noqa: BLE001 - 取文字失败不影响发图
            return ""

    async def _save_images(self, images: list) -> tuple[list[str], int]:
        """把图片落盘，返回 (成功保存的绝对路径列表, 失败张数)。"""
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # 自己建下载 session：gemini-webapi 自建的 session 写死了 CurlFollow.SAFE，
        # 走内网代理时会被 SSRF 保护拒掉（详见 client.create_download_session 注释）。
        session = None
        try:
            session = await self.client.create_download_session()
        except Exception as exc:  # noqa: BLE001 - 建不出来就退回原生行为
            logger.warning(f"[gemini-image] 创建下载 session 失败，改用默认方式：{exc}")

        saved: list[str] = []
        failed = 0
        try:
            for image in images:
                try:
                    path = await self._save_one(image, session)
                except Exception as exc:  # noqa: BLE001 - 单张失败不该让整体失败
                    failed += 1
                    logger.warning(f"[gemini-image] 图片保存失败：{exc}")
                    continue
                if path:
                    saved.append(path)
                else:
                    failed += 1
        finally:
            if session is not None:
                with contextlib.suppress(Exception):
                    await session.close()
        return saved, failed

    async def _save_one(self, image, session=None) -> str | None:
        kwargs: dict = {"path": str(self.output_dir), "verbose": False}
        if session is not None:
            kwargs["client"] = session

        call = image.save(**kwargs)
        # 不同版本 gemini-webapi 的 save 可能是同步/异步，这里都兼容
        if inspect.isawaitable(call):
            call = await call
        path = str(call or "").strip()
        if not path:
            return None

        file_path = Path(path)
        if not file_path.is_absolute():
            file_path = (self.output_dir / file_path).resolve()
        if not file_path.is_file() or file_path.stat().st_size == 0:
            logger.warning(f"[gemini-image] 保存结果无效：{file_path}")
            return None
        return str(file_path)
