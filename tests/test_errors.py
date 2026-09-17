"""异常映射测试。

重点在于**判断顺序**：curl_cffi 的异常是层层继承的
（`ConnectTimeout` 同时是 `ConnectionError` 与 `Timeout` 的子类，
`CurlError` 是所有 curl 异常的基类），顺序写错就会把超时报成「连接失败」。
"""

import gemini_webapi as gwa
from curl_cffi.requests import exceptions as ce
from gemini_webapi.constants import AccountStatus

from core.errors import GeminiImageError, account_error_from, describe_exception


def test_unexpected_exception_falls_back():
    err = describe_exception(ValueError("boom"))
    assert isinstance(err, GeminiImageError)
    assert "ValueError" in err.message


def test_passthrough_gemini_image_error():
    original = GeminiImageError("已经是用户可见文案")
    assert describe_exception(original) is original


def test_auth_error_mentions_cookie():
    err = describe_exception(gwa.AuthError())
    assert "Cookie" in (err.hint or "")


def test_usage_limit_and_blocked():
    assert "额度" in describe_exception(gwa.UsageLimitExceededError()).message
    assert "风控" in describe_exception(gwa.TemporarilyBlockedError()).message


def test_gemini_timeout_not_confused_with_builtin():
    assert "超时" in describe_exception(gwa.TimeoutError()).message
    assert "超时" in describe_exception(TimeoutError()).message


def test_proxy_error():
    err = describe_exception(ce.ProxyError("proxy down"))
    assert "代理" in err.message
    assert err.hint


def test_timeout_is_not_reported_as_connection_failure():
    assert "超时" in describe_exception(ce.Timeout("t")).message
    # ConnectTimeout 继承自 ConnectionError，但语义上应归为超时
    assert "超时" in describe_exception(ce.ConnectTimeout("t")).message


def test_connection_error():
    assert "连接" in describe_exception(ce.ConnectionError("c")).message


def test_http_error_is_not_swallowed_by_curlerror():
    # HTTPError 是 CurlError 的子孙，若 CurlError 分支在前就会被吃掉
    assert "状态码" in describe_exception(ce.HTTPError("502")).message


def test_bare_curlerror_is_network_failure():
    assert "网络" in describe_exception(ce.CurlError("x")).message


# --------------------------------------------------------------- 账号状态映射


class FakeAccountClient:
    def __init__(self, status):
        self.account_status = status


def test_available_account_has_no_error():
    assert account_error_from(FakeAccountClient(AccountStatus.AVAILABLE)) is None
    assert account_error_from(FakeAccountClient(None)) is None
    assert account_error_from(object()) is None


def test_unauthenticated_points_to_cookie():
    err = account_error_from(FakeAccountClient(AccountStatus.UNAUTHENTICATED))
    assert err is not None
    assert "Cookie" in (err.hint or "")


def test_location_rejected_is_explained():
    err = account_error_from(FakeAccountClient(AccountStatus.LOCATION_REJECTED))
    assert err is not None
    assert "地区" in err.message
    assert err.hint


def test_tos_states_are_explained():
    for status in (AccountStatus.TOS_PENDING, AccountStatus.TOS_OUT_OF_DATE):
        err = account_error_from(FakeAccountClient(status))
        assert err is not None
        assert "条款" in err.message


def test_every_known_status_has_a_message():
    """新增枚举值时不该悄悄退化成「未知异常」。"""
    for status in AccountStatus:
        if status is AccountStatus.AVAILABLE:
            continue
        err = account_error_from(FakeAccountClient(status))
        assert err is not None, status
        assert "账号状态异常" not in err.message, f"{status.name} 缺少专属文案"


def test_unknown_status_still_reports_something():
    class Weird:
        class _Status:
            name = "BRAND_NEW_STATUS"
            description = "brand new"

        account_status = _Status()

    err = account_error_from(Weird())
    assert err is not None
    assert "brand new" in err.message
