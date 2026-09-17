"""异常翻译层：把底层五花八门的报错变成一句用户能看懂的中文。

QQ 群里的用户不关心 curl_cffi 抛了什么，他们只想知道「为什么没出图、我该做什么」。
所以所有对外的失败都必须经过 `describe_exception()`。
"""

from __future__ import annotations

import gemini_webapi as gwa
from curl_cffi.requests import exceptions as curl_exc
from gemini_webapi.constants import AccountStatus

#: gemini_webapi 的 TimeoutError 与内置 TimeoutError 同名，显式区分避免遮蔽
GeminiTimeoutError = gwa.TimeoutError

COOKIE_HINT = (
    "请在插件配置里更新 secure_1psid / secure_1psidts："
    "浏览器打开 https://gemini.google.com 登录 → F12 → 网络 → 刷新页面 → "
    "任选一个请求，复制 Cookie 中 __Secure-1PSID 与 __Secure-1PSIDTS 的值"
)

PROXY_HINT = "国内网络需要代理：确认代理已启动，并在插件配置里填好 proxy_url（例如 http://127.0.0.1:7890）"

#: 账号状态 → (用户可见文案, 建议)。
#:
#: 这些状态来自 Gemini 网页端的账号检查接口。**关键点：Cookie 无效时
#: `client.init()` 依然会「成功」返回**，只是把 account_status 置为 UNAUTHENTICATED，
#: 之后生图只会得到一句「您登录了吗」的闲聊回复。若不单独判断这个状态，
#: 用户只能看到含糊的「没有返回图片」，根本不知道是自己 Cookie 没配好。
ACCOUNT_STATUS_MESSAGES: dict[str, tuple[str, str | None]] = {
    "UNAUTHENTICATED": ("Gemini 登录状态无效：Cookie 未生效或已过期", COOKIE_HINT),
    "ACCESS_TEMPORARILY_UNAVAILABLE": (
        "Google 账号访问被临时限制",
        "多为地区或会话临时问题，可更换代理出口 IP 后重试",
    ),
    "ACCOUNT_REJECTED": (
        "Google 账号访问被拒绝",
        "请到 Google 账号页面检查是否有异常活动提示",
    ),
    "ACCOUNT_UNTRUSTED": (
        "账号未通过 Google 的安全/信任检查",
        "常见于新注册账号，建议换一个常用账号",
    ),
    "TOS_PENDING": (
        "需要先在网页端同意最新服务条款",
        "浏览器打开 https://gemini.google.com 接受条款后再试",
    ),
    "TOS_OUT_OF_DATE": (
        "服务条款已过期",
        "浏览器打开 https://gemini.google.com 接受新条款后再试",
    ),
    "ACCOUNT_REJECTED_BY_GUARDIAN": ("账号被监护人限制，无法使用", "请更换 Google 账号"),
    "GUARDIAN_APPROVAL_REQUIRED": ("账号需要监护人批准才能使用", "请更换账号或在家长控制中放行"),
    "LOCATION_REJECTED": (
        "当前账号/地区不支持 Gemini",
        "换一个受支持地区（如日本、美国）的代理出口 IP，或更换账号",
    ),
}


def account_error_from(client) -> GeminiImageError | None:
    """检查底层客户端的账号状态，异常则返回可直接展示的错误；正常返回 None。"""
    status = getattr(client, "account_status", None)
    if status is None:
        return None

    name = str(getattr(status, "name", "") or "")
    if not name or name == AccountStatus.AVAILABLE.name:
        return None

    message, hint = ACCOUNT_STATUS_MESSAGES.get(
        name,
        (f"Gemini 账号状态异常：{getattr(status, 'description', name)}", None),
    )
    return GeminiImageError(message, hint=hint)


class GeminiImageError(Exception):
    """插件对外抛出的统一异常，`message` 可直接展示给用户。"""

    def __init__(
        self,
        message: str,
        *,
        hint: str | None = None,
        retryable: bool = False,
        retry_delay: int = 60,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.retryable = retryable
        self.retry_delay = retry_delay

    def to_user_text(self) -> str:
        text = f"❌ {self.message}"
        if self.hint:
            text += f"\n💡 {self.hint}"
        return text


def describe_exception(exc: BaseException) -> GeminiImageError:
    """把任意底层异常映射成 `GeminiImageError`。"""
    if isinstance(exc, GeminiImageError):
        return exc

    if isinstance(exc, gwa.AuthError):
        return GeminiImageError(
            "Gemini 登录状态已失效（Cookie 过期或被登出）",
            hint=COOKIE_HINT,
        )

    if isinstance(exc, gwa.UsageLimitExceededError):
        return GeminiImageError(
            "该 Google 账号的今日生图额度已用尽",
            hint="换一个账号的 Cookie，或等额度重置后再试",
            retryable=False,
        )

    if isinstance(exc, gwa.TemporarilyBlockedError):
        return GeminiImageError(
            "触发 Google 风控（429），当前 IP 被临时限流",
            hint="稍后重试；若频繁出现，建议更换代理出口 IP",
            retryable=True,
            retry_delay=300,
        )

    if isinstance(exc, gwa.ImageGenerationError):
        return GeminiImageError(
            "图片生成失败，Gemini 没有返回可解析的图片",
            hint="可能被内容策略拦截，换一个更具体、合规的描述再试",
            retryable=True,
        )

    if isinstance(exc, gwa.ModelInvalidError):
        return GeminiImageError(
            "配置的模型名无效",
            hint="把插件配置里的 model 留空以使用账号默认模型，或改用 gemini-flash / gemini-pro",
        )

    if isinstance(exc, GeminiTimeoutError):
        return GeminiImageError(
            "Gemini 响应超时",
            hint="生图通常需要 20~60 秒，可适当调大 request_timeout 后重试",
            retryable=True,
        )

    if isinstance(exc, (curl_exc.ProxyError, curl_exc.InvalidProxyURL)):
        return GeminiImageError("代理不可用", hint=PROXY_HINT, retryable=True)

    # 顺序很重要：ConnectTimeout 同时是 ConnectionError 与 Timeout 的子类，
    # 归入「超时」更贴切；而 CurlError 是所有 curl_cffi 异常的基类，必须最后兜底。
    if isinstance(exc, curl_exc.Timeout):
        return GeminiImageError(
            "网络请求超时",
            hint="检查代理稳定性，或调大插件配置里的 request_timeout",
            retryable=True,
        )

    if isinstance(exc, curl_exc.ConnectionError):
        return GeminiImageError(
            "连接 gemini.google.com 失败",
            hint=PROXY_HINT,
            retryable=True,
        )

    if isinstance(exc, curl_exc.HTTPError):
        return GeminiImageError(
            f"Gemini 返回异常状态码（{exc}）",
            hint="多为 Cookie 失效或 IP 被限流，可先用 gemini状态 自检",
            retryable=True,
        )

    if isinstance(exc, curl_exc.CurlError):
        return GeminiImageError(
            "网络请求失败（底层 curl 错误）",
            hint=PROXY_HINT,
            retryable=True,
        )

    if isinstance(exc, gwa.GeminiError):
        return GeminiImageError(
            f"Gemini 服务端返回错误：{exc}",
            hint="稍后重试；持续出现请查看 AstrBot 日志中的完整堆栈",
            retryable=True,
        )

    if isinstance(exc, gwa.APIError):
        return GeminiImageError(
            f"gemini-webapi 内部错误：{exc}",
            hint="通常是网页接口结构变化导致，可尝试升级 gemini-webapi",
        )

    # 内置 TimeoutError：asyncio.wait_for 触发时抛出的就是它
    if isinstance(exc, TimeoutError):
        return GeminiImageError(
            "等待 Gemini 响应超时",
            hint="生图通常需要 20~60 秒，可适当调大插件配置里的 request_timeout",
            retryable=True,
        )

    return GeminiImageError(
        f"未预期的错误：{type(exc).__name__}: {exc}",
        hint="请把 AstrBot 日志中的这段堆栈反馈给插件作者",
        retryable=True,
    )
