"""客户端封装测试（全部使用假客户端，不发任何网络请求）。"""

import asyncio
import time

import gemini_webapi as gwa
import pytest
from curl_cffi.requests import exceptions as curl_exc
from gemini_webapi.constants import AccountStatus

from core.client import GeminiImageClient
from core.errors import GeminiImageError


class FakeWebClient:
    """假的 gemini_webapi.GeminiClient。"""

    def __init__(self, status=AccountStatus.AVAILABLE, output=None):
        self.account_status = status
        self.output = output
        self.closed = False
        self.requests: list[dict] = []

    async def close(self, delay: float = 0) -> None:
        self.closed = True

    async def generate_content(self, prompt, files=None, model=None, temporary=False, **kwargs):
        self.requests.append({"prompt": prompt, "files": files, "model": model, "temporary": temporary})
        return self.output

    def resolve_model(self, name):
        raise gwa.ModelInvalidError(f"unknown: {name}")


def patch_ensure(client: GeminiImageClient, fake: FakeWebClient) -> None:
    """让 ensure() 直接返回假客户端。"""

    async def _fake_ensure():
        client._client = fake
        return fake

    client.ensure = _fake_ensure


def test_not_configured():
    client = GeminiImageClient("", "")
    assert client.configured is False
    with pytest.raises(GeminiImageError) as info:
        asyncio.run(client.generate("猫"))
    assert "Cookie" in str(info.value)


def test_describe_reports_state():
    client = GeminiImageClient("PSID", "", proxy="http://127.0.0.1:7890")
    text = client.describe()
    assert "已配置" in text
    assert "7890" in text
    assert "未建立" in text


def test_unauthenticated_client_is_rejected_and_discarded():
    async def scenario():
        client = GeminiImageClient("PSID", "")
        fake = FakeWebClient(status=AccountStatus.UNAUTHENTICATED)
        patch_ensure(client, fake)

        with pytest.raises(GeminiImageError) as info:
            await client._usable_client()

        assert "Cookie" in (info.value.hint or "")
        # 假成功的连接必须被丢掉，否则用户更新 Cookie 后仍会复用它
        assert fake.closed is True
        assert client.ready is False

    asyncio.run(scenario())


def test_location_rejected_gives_specific_reason():
    async def scenario():
        client = GeminiImageClient("PSID", "")
        fake = FakeWebClient(status=AccountStatus.LOCATION_REJECTED)
        patch_ensure(client, fake)

        with pytest.raises(GeminiImageError) as info:
            await client.generate("猫")
        assert "地区" in info.value.message

    asyncio.run(scenario())


def test_unauthenticated_blocks_generate_before_request():
    async def scenario():
        client = GeminiImageClient("PSID", "")
        fake = FakeWebClient(status=AccountStatus.UNAUTHENTICATED)
        patch_ensure(client, fake)

        with pytest.raises(GeminiImageError):
            await client.generate("猫")
        # 关键：不该浪费一次请求去拿那句「您登录了吗」的闲聊回复
        assert fake.requests == []

    asyncio.run(scenario())


def test_generate_passes_arguments_through():
    async def scenario():
        client = GeminiImageClient("PSID", "")
        fake = FakeWebClient(status=AccountStatus.AVAILABLE, output="OK")
        patch_ensure(client, fake)

        result = await client.generate("一只猫", files=["a.png"], temporary=True)
        assert result == "OK"
        assert fake.requests[0]["files"] == ["a.png"]
        assert fake.requests[0]["temporary"] is True
        # 无效模型名会退化为账号默认模型（None），不阻断生图
        assert fake.requests[0]["model"] is None

    asyncio.run(scenario())


def test_generate_wraps_unknown_exception():
    async def scenario():
        client = GeminiImageClient("PSID", "")

        class Boom(FakeWebClient):
            async def generate_content(self, *args, **kwargs):
                raise ValueError("unexpected")

        patch_ensure(client, Boom())
        with pytest.raises(GeminiImageError) as info:
            await client.generate("猫")
        assert "ValueError" in info.value.message

    asyncio.run(scenario())


def test_discard_is_safe_when_never_connected():
    asyncio.run(GeminiImageClient("PSID", "").discard())
    asyncio.run(GeminiImageClient("PSID", "").close())


def test_health_check_without_cookie():
    ok, message = asyncio.run(GeminiImageClient("", "").health_check())
    assert ok is False
    assert "未配置" in message


def test_download_session_uses_plain_redirects(monkeypatch):
    """下载 session 必须关掉 CurlFollow.SAFE。

    该模式会拒绝「重定向到内网 IP」，而经内网代理（172.17.0.1）出网时正好命中，
    表现为图片生成成功却下载失败：
    `curl: (7) Redirect to internal IP ... rejected (SSRF protection)`。
    """
    import core.client as client_mod

    captured: dict = {}

    class RecordingSession:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def close(self):
            return None

    monkeypatch.setattr(client_mod, "AsyncSession", RecordingSession)

    async def scenario():
        client = GeminiImageClient("PSID", "", proxy="http://172.17.0.1:7890")
        patch_ensure(client, FakeWebClient(status=AccountStatus.AVAILABLE))
        await client.ensure()
        await client.create_download_session()

    asyncio.run(scenario())

    assert captured["allow_redirects"] is True
    assert captured["proxy"] == "http://172.17.0.1:7890"
    assert captured["impersonate"]  # 用了底层 client 的指纹或兜底值


def test_throttle_enforces_min_interval():
    """串行锁只保证「不重叠」；两次请求之间还必须留出最小间隔。"""

    async def scenario():
        client = GeminiImageClient("PSID", "", min_interval=0.3)
        patch_ensure(client, FakeWebClient(status=AccountStatus.AVAILABLE, output="OK"))

        start = time.monotonic()
        await client.generate("a")
        first = time.monotonic() - start
        await client.generate("b")
        second = time.monotonic() - start

        assert first < 0.3, "第一次请求不该被节流"
        assert second - first >= 0.25, "第二次请求必须被拉开间隔"

    asyncio.run(scenario())


def test_throttle_can_be_disabled():
    async def scenario():
        client = GeminiImageClient("PSID", "", min_interval=0)
        patch_ensure(client, FakeWebClient(status=AccountStatus.AVAILABLE, output="OK"))

        start = time.monotonic()
        await client.generate("a")
        await client.generate("b")
        assert time.monotonic() - start < 0.2

    asyncio.run(scenario())


def test_rate_limit_triggers_cooldown_and_stops_retrying():
    """吃到 429 要立刻熔断：既不重试撞击，也在静默期内直接拒绝。"""

    async def scenario():
        client = GeminiImageClient("PSID", "", min_interval=0, blocked_cooldown=60)

        class Blocked(FakeWebClient):
            def __init__(self):
                super().__init__(status=AccountStatus.AVAILABLE)
                self.calls = 0

            async def generate_content(self, *args, **kwargs):
                self.calls += 1
                raise gwa.TemporarilyBlockedError("429")

        fake = Blocked()
        patch_ensure(client, fake)

        with pytest.raises(GeminiImageError) as info:
            await client.generate("猫")
        assert "风控" in info.value.message
        assert fake.calls == 1, "风控后不该再重试去撞"

        with pytest.raises(GeminiImageError) as info2:
            await client.generate("猫")
        assert "静默" in info2.value.message
        assert fake.calls == 1, "静默期内不该再发出请求"

    asyncio.run(scenario())


def test_throttle_status_reports_state():
    client = GeminiImageClient("PSID", "", min_interval=7)
    assert "7" in client.throttle_status()

    client._blocked_until = time.time() + 30
    assert "风控静默中" in client.throttle_status()


def test_library_logging_keeps_global_handlers():
    """回归防护：屏蔽库日志时绝不能动全局 loguru handler。

    2026-09-16 实测踩过：调用 `gemini_webapi.set_log_level()` 内部是
    `logger.remove(_handler_id)`，而首次调用时 `_handler_id` 为 None，
    loguru 的 `remove(None)` 会移除**全部** handler ——
    结果是插件加载后 AstrBot 自己再无任何日志输出。
    """
    loguru_logger = pytest.importorskip("loguru", reason="无依赖环境下跳过（AstrBot 必装 loguru）").logger

    before = len(loguru_logger._core.handlers)
    GeminiImageClient._configure_library_logging(verbose=False)
    assert len(loguru_logger._core.handlers) == before, "不得增删全局 loguru handler"

    # 复原，避免影响其它用例的输出
    GeminiImageClient._configure_library_logging(verbose=True)


def test_patch_curl_follow_safe():
    """gemini-webapi 写死的 `CurlFollow.SAFE` 必须被改成「普通跟随重定向」。

    否则经内网代理出网时，代理地址本身会被判成 SSRF 目标直接拒掉
    （`curl: (7) Redirect to internal IP ... rejected (SSRF protection)`），
    而且这句报错还会掩盖真正的失败原因。
    """
    import importlib

    from curl_cffi import CurlFollow as Real

    import core.client as client_mod

    patched = client_mod.patch_curl_follow_safe()
    assert patched, "至少应打上一个模块的补丁"
    # 关键：这个模块被 utils/__init__.py 用同名函数遮蔽过，必须用 importlib 才能拿到
    assert "gemini_webapi.utils.get_access_token" in patched

    for module_name in patched:
        module = importlib.import_module(module_name)
        assert module.CurlFollow.SAFE is True
        # 其余成员应透传真实枚举
        assert module.CurlFollow.ALL == Real.ALL
        assert module.CurlFollow.OBEYCODE == Real.OBEYCODE


def test_network_error_is_retried_once(tmp_path):
    """瞬时网络故障应重试一次，而不是直接把失败抛给用户。"""

    async def scenario():
        client = GeminiImageClient("PSID", "", min_interval=0)

        class Flaky(FakeWebClient):
            def __init__(self):
                super().__init__(status=AccountStatus.AVAILABLE, output="OK")
                self.calls = 0

            async def generate_content(self, *args, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    raise curl_exc.ConnectionError("node hiccup")
                return self.output

        fake = Flaky()
        patch_ensure(client, fake)

        result = await client.generate("猫")
        assert result == "OK"
        assert fake.calls == 2, "第一次失败后应重建连接重试"

    asyncio.run(scenario())
