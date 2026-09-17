"""网页版 Gemini 客户端封装。

只依赖浏览器 Cookie（`__Secure-1PSID` / `__Secure-1PSIDTS`）走 gemini.google.com
的内部接口，**不使用 Google 官方 API，也不需要 API Key**。

职责：
- 懒初始化：第一次真正用到时才建立连接，避免拖慢 AstrBot 启动；
- 串行化：同一个 Google 账号并发请求极易触发风控，这里用锁强制排队；
- 自愈：Cookie 失效时丢弃旧客户端、重读缓存并重建，再重试一次。
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import os
import time
from pathlib import Path

import gemini_webapi as gwa
from astrbot.api import logger
from curl_cffi import CurlHttpVersion
from curl_cffi.requests import AsyncSession
from curl_cffi.requests import exceptions as curl_exc
from gemini_webapi import GeminiClient

from .constants import DEFAULT_IMPERSONATE
from .cookies import load_cached_1psidts
from .errors import COOKIE_HINT, GeminiImageError, account_error_from, describe_exception


def patch_curl_follow_safe() -> list[str]:
    """把 gemini-webapi 里写死的 `CurlFollow.SAFE` 换成「普通跟随重定向」。

    ⚠️ 为什么必须打这个补丁

    gemini-webapi 在三处把 `allow_redirects` 写死为 `CurlFollow.SAFE`：

    - `utils/get_access_token.py:282`
    - `types/image.py:103`
    - `types/video.py:93`

    该模式的语义是「跟随重定向，但拒绝跳到内网/私有 IP」。当我们只能经**内网代理**
    出网时（容器里的 `172.17.0.1:7890` 就是典型），它会把**代理地址本身**判成 SSRF 目标
    直接拒掉：

        curl: (7) Redirect to internal IP 172.17.0.1 rejected (SSRF protection)

    更恶劣的是它会**掩盖真正的失败原因** —— 比如凭据失效导致 Google 返回重定向时，
    报出来的却是这句误导性的 SSRF 错误，让人误以为是网络/代理坏了。

    做法是在**模块级**把 `CurlFollow` 这个名字替换掉，让 `SAFE` 等价于 `True`，
    对上游代码零侵入。

    ⚠️ 有个隐蔽的坑：`gemini_webapi/utils/__init__.py` 里有
    `from .get_access_token import InitSession, get_access_token`，
    使得 `import gemini_webapi.utils.get_access_token as m` 拿到的是**函数**而非模块
    （Python 3.7+ 的 `import a.b as c` 会优先取 `getattr(a, "b")`），
    此时 `m.CurlFollow = ...` 会**静默失效**。必须用 `importlib.import_module`。

    Returns
    -------
    `list[str]`
        实际打了补丁的模块名，便于日志确认是否生效。
    """
    try:
        from curl_cffi import CurlFollow as _RealCurlFollow
    except ImportError:  # pragma: no cover - curl_cffi 是 gemini-webapi 的硬依赖
        return []

    class _PatchedCurlFollow:
        """只改写 SAFE 的语义，其余成员透传真实枚举。"""

        def __getattr__(self, name: str):
            if name == "SAFE":
                return True
            return getattr(_RealCurlFollow, name)

    patched: list[str] = []
    for module_name in (
        "gemini_webapi.utils.get_access_token",
        "gemini_webapi.types.image",
        "gemini_webapi.types.video",
    ):
        try:
            module = importlib.import_module(module_name)
        except ImportError:  # pragma: no cover - 版本差异时静默跳过
            continue
        if hasattr(module, "CurlFollow"):
            module.CurlFollow = _PatchedCurlFollow()
            patched.append(module_name)
    return patched


class GeminiImageClient:
    """`gemini_webapi.GeminiClient` 的常驻封装。"""

    def __init__(
        self,
        secure_1psid: str = "",
        secure_1psidts: str = "",
        *,
        proxy: str | None = None,
        cookie_dir: str | Path | None = None,
        auto_refresh: bool = True,
        init_timeout: float = 60.0,
        watchdog_timeout: float = 180.0,
        generate_timeout: float = 300.0,
        min_interval: float = 5.0,
        blocked_cooldown: float = 300.0,
        verbose: bool = False,
    ) -> None:
        self._secure_1psid = (secure_1psid or "").strip()
        self._secure_1psidts = (secure_1psidts or "").strip()
        self._proxy = (proxy or "").strip() or None
        self._cookie_dir = Path(cookie_dir) if cookie_dir else None
        self._auto_refresh = bool(auto_refresh)
        self._init_timeout = float(init_timeout)
        self._watchdog_timeout = float(watchdog_timeout)
        self._generate_timeout = float(generate_timeout)
        self._verbose = bool(verbose)

        self._client: GeminiClient | None = None
        #: 建立连接用；避免并发首请求把 init 跑两遍
        self._init_lock = asyncio.Lock()
        #: 生成用；同一账号串行，降低被风控的概率
        self._run_lock = asyncio.Lock()

        #: 两次真实请求之间的最小间隔（秒），0 = 不限制。
        #: 串行锁只保证「不同时发」，不保证「不连着发」——A 结束、B 立刻开始，
        #: 在 Google 眼里仍是连续高频请求。这里强制拉开间隔。
        self._min_interval = max(0.0, float(min_interval))
        #: 吃到 429 风控后的强制静默时长（秒）
        self._blocked_cooldown = max(0.0, float(blocked_cooldown))
        self._last_request_at = 0.0
        self._blocked_until = 0.0

        # 把 gemini-webapi 自己的日志与 AstrBot 的日志隔离开（见方法内注释）
        self._configure_library_logging(self._verbose)

        # 修正 gemini-webapi 写死的 CurlFollow.SAFE（详见函数注释）：
        # 经内网代理出网时它会把代理本身当成 SSRF 目标拒掉，还会掩盖真实错误
        patched = patch_curl_follow_safe()
        if patched:
            logger.debug(f"[gemini-image] 已修正 CurlFollow.SAFE 行为：{patched}")

    @staticmethod
    def _configure_library_logging(verbose: bool = False) -> None:
        """只屏蔽 gemini-webapi 自己的日志，**绝不能**碰全局 logger。

        ⚠️ 血泪教训：不要用 `gemini_webapi.set_log_level()`。
        它内部是 `logger.remove(_handler_id)`，而首次调用时 `_handler_id` 为 None，
        loguru 的 `remove(None)` 语义是「移除**所有** handler」——
        会把 AstrBot 自己注册的日志 handler 全部删掉，
        表现为插件加载之后整个机器人**再无任何日志输出**，直到重启容器。
        （该库 docstring 里其实写了这个警告，容易漏看。）

        正确做法是用 loguru 的模块级开关：`disable("gemini_webapi")` 只丢弃
        `record["name"]` 以 gemini_webapi 开头的记录，对 AstrBot 毫无影响。
        """
        try:
            from loguru import logger as _loguru
        except ImportError:  # pragma: no cover - AstrBot 必然自带 loguru
            return

        if verbose:
            _loguru.enable("gemini_webapi")
        else:
            _loguru.disable("gemini_webapi")

    # ------------------------------------------------------------------ 状态

    @property
    def configured(self) -> bool:
        """是否已配置必要的 Cookie。"""
        return bool(self._secure_1psid)

    @property
    def proxy(self) -> str | None:
        return self._proxy

    @property
    def ready(self) -> bool:
        """连接是否已经建立。"""
        return self._client is not None

    def describe(self) -> str:
        """给 `gemini状态` 指令用的一行摘要。"""
        parts = [
            f"Cookie: {'已配置' if self.configured else '未配置'}",
            f"代理: {self._proxy or '未使用'}",
            f"连接: {'已建立' if self.ready else '未建立'}",
        ]
        return " | ".join(parts)

    # -------------------------------------------------------------- 生命周期

    async def _create_client(self) -> GeminiClient:
        """真正建立连接（会发一次真实请求校验 Cookie）。"""
        if not self.configured:
            raise GeminiImageError("尚未配置 Gemini 网页版 Cookie", hint=COOKIE_HINT)

        # 让 gemini-webapi 把刷新后的 Cookie 落到我们的数据目录，而不是系统临时目录
        if self._cookie_dir is not None:
            self._cookie_dir.mkdir(parents=True, exist_ok=True)
            os.environ["GEMINI_COOKIE_PATH"] = str(self._cookie_dir)

        # 配置里的 1PSIDTS 往往比缓存里的旧，优先用缓存（若可用）
        cached = load_cached_1psidts(self._secure_1psid, self._cookie_dir)
        psidts = cached or self._secure_1psidts
        if cached and cached != self._secure_1psidts:
            logger.debug("[gemini-image] 使用缓存中较新的 __Secure-1PSIDTS")

        client = GeminiClient(
            self._secure_1psid,
            psidts,
            proxy=self._proxy,
        )
        await client.init(
            timeout=self._init_timeout,
            # 常驻服务自己管理生命周期，不让它闲置自动关闭
            auto_close=False,
            auto_refresh=self._auto_refresh,
            watchdog_timeout=self._watchdog_timeout,
            verbose=self._verbose,
        )
        return client

    async def ensure(self) -> GeminiClient:
        """确保连接可用并返回底层客户端。"""
        async with self._init_lock:
            if self._client is None:
                logger.info("[gemini-image] 正在建立与 gemini.google.com 的连接…")
                try:
                    self._client = await self._create_client()
                except Exception as exc:
                    raise describe_exception(exc) from exc
                logger.info("[gemini-image] 连接就绪")
            return self._client

    async def discard(self) -> None:
        """丢弃当前连接（下次调用会自动重建）。"""
        async with self._init_lock:
            client, self._client = self._client, None
        if client is not None:
            with contextlib.suppress(Exception):
                await client.close()

    async def _usable_client(self) -> GeminiClient:
        """取一个「账号状态正常」的客户端。

        Cookie 无效时 `init()` 也会返回成功（账号状态被置为 UNAUTHENTICATED），
        这里把这种「假成功」挡掉，并丢弃连接——用户按提示更新 Cookie、重载插件后
        就能重新建立有效会话。
        """
        client = await self.ensure()
        error = account_error_from(client)
        if error is not None:
            logger.warning(f"[gemini-image] 账号状态异常：{error.message}")
            await self.discard()
            raise error
        return client

    async def close(self) -> None:
        """插件卸载/重载时释放连接。"""
        await self.discard()

    # ------------------------------------------------------------------ 生成

    async def generate(
        self,
        prompt: str,
        *,
        files: list[str] | None = None,
        model: str | None = None,
        temporary: bool = True,
    ) -> gwa.ModelOutput:
        """调用一次生成，返回原始 `ModelOutput`。

        Cookie 失效会自动重建客户端并重试一次；其余异常统一翻译成中文提示。
        """
        if not self.configured:
            raise GeminiImageError("尚未配置 Gemini 网页版 Cookie", hint=COOKIE_HINT)

        async with self._run_lock:
            # 全局节流 + 风控熔断：放在锁内，保证计时不被并发请求打乱
            await self._throttle()

            last_error: GeminiImageError | None = None
            for attempt in (1, 2):
                client = await self._usable_client()
                try:
                    resolved = self._resolve_model(client, model)
                    return await asyncio.wait_for(
                        client.generate_content(
                            prompt,
                            files=files or None,
                            model=resolved,
                            temporary=temporary,
                        ),
                        timeout=self._generate_timeout,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    error = describe_exception(exc)

                    # 吃到风控就立刻熔断：继续重试只会把账号推向更严重的限制
                    if isinstance(exc, gwa.TemporarilyBlockedError):
                        self._blocked_until = time.time() + self._blocked_cooldown
                        logger.warning(
                            "[gemini-image] 触发 Google 风控（429），"
                            f"静默 {int(self._blocked_cooldown)} 秒后再接受请求"
                        )
                        raise error from exc

                    # 凭据失效 / 连接被拒 / 瞬时网络抖动：丢弃连接后重试一次。
                    # 网络类错误也纳入重试，是因为节点抖动常常只持续几秒，
                    # 重建连接（可能换到别的节点）后大概率就恢复了。
                    if attempt == 1 and isinstance(
                        exc,
                        (
                            gwa.AuthError,
                            curl_exc.ConnectionError,
                            curl_exc.Timeout,
                            curl_exc.CurlError,
                        ),
                    ):
                        logger.warning(
                            f"[gemini-image] 第 {attempt} 次生成失败（{type(exc).__name__}），重建连接后重试"
                        )
                        await self.discard()
                        last_error = error
                        await asyncio.sleep(1)
                        continue
                    raise error from exc

            raise last_error or GeminiImageError("生成失败")

    # ------------------------------------------------------------------ 节流

    async def _throttle(self) -> None:
        """全局节流 + 风控熔断。

        为什么光有串行锁不够：锁只保证两个请求不重叠，但「A 刚结束、B 立刻开始」
        在账号侧看依然是连续高频行为。这里做两件事——

        1. 强制两次请求之间至少间隔 `min_interval` 秒；
        2. 一旦吃到 429，进入 `blocked_cooldown` 秒静默期，期间直接拒绝，不再去试探
           （风控期间继续撞，是最容易把账号做死的操作）。
        """
        now = time.time()

        remaining = self._blocked_until - now
        if remaining > 0:
            raise GeminiImageError(
                f"Gemini 风控静默中，请 {int(remaining) + 1} 秒后再试",
                hint="刚触发了 Google 的限流保护。若频繁出现，说明请求太密，"
                "建议调大 min_request_interval 或降低每日配额",
                retryable=True,
                retry_delay=int(remaining) + 1,
            )

        if self._min_interval > 0:
            gap = self._min_interval - (now - self._last_request_at)
            if gap > 0:
                logger.debug(f"[gemini-image] 节流等待 {gap:.1f}s")
                await asyncio.sleep(gap)

        self._last_request_at = time.time()

    def throttle_status(self) -> str:
        """给 `gemini状态` 展示的节流情况。"""
        now = time.time()
        if self._blocked_until > now:
            return f"风控静默中（{int(self._blocked_until - now)} 秒后恢复）"
        if self._min_interval <= 0:
            return "未启用（不限制请求间隔）"
        return f"最小请求间隔 {self._min_interval:.0f} 秒"

    # -------------------------------------------------------------- 结果下载

    async def create_download_session(self) -> AsyncSession:
        """创建一个用于下载「生成结果图片」的 session。

        为什么不复用 gemini-webapi 自建的 session：
        `gemini_webapi/types/image.py:103` 在 fallback 路径里把 `allow_redirects`
        写死为 `CurlFollow.SAFE`，而该模式会拒绝「重定向/连接到内网 IP」。
        当出口必须走内网代理（容器里的 `http://172.17.0.1:7890` 就是典型）时，
        图片下载会直接被拒：

            curl: (7) Redirect to internal IP 172.17.0.1 rejected (SSRF protection)

        结果是图片明明生成成功了，却一张都发不出去。这里改成普通的重定向跟随，
        其余参数（指纹、cookie、代理、HTTP 版本）与原生实现保持一致。
        """
        client = self._client
        cookies = getattr(client, "cookies", None) if client is not None else None
        impersonate = getattr(client, "impersonate", None) or DEFAULT_IMPERSONATE
        return AsyncSession(
            impersonate=impersonate,
            allow_redirects=True,
            http_version=CurlHttpVersion.NONE,
            cookies=cookies,
            proxy=self._proxy,
            timeout=120,
        )

    def _resolve_model(self, client: GeminiClient, model: str | None):
        """把配置里的模型名解析成 `AvailableModel`。

        解析失败不该阻断生图——退回账号默认模型即可。
        """
        name = (model or "").strip()
        if not name:
            return None
        try:
            return client.resolve_model(name)
        except Exception as exc:  # noqa: BLE001 - 任何解析问题都退化为默认模型
            logger.warning(f"[gemini-image] 模型 {name!r} 无法解析，改用账号默认模型：{exc}")
            return None

    # ------------------------------------------------------------------ 自检

    async def health_check(self) -> tuple[bool, str]:
        """给 `gemini状态` 用的连通性自检，返回 (是否可用, 文案)。"""
        if not self.configured:
            return False, "未配置 Cookie"
        try:
            await self.ensure()
        except GeminiImageError as exc:
            return False, exc.message
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

        # 连接能建立不代表账号可用，这里给出更具体的原因
        error = account_error_from(self._client)
        if error is not None:
            return False, error.message
        return True, "连接正常"
