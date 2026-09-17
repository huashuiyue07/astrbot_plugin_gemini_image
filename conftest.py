"""pytest 全局配置。

- 把仓库根目录加入 sys.path，使测试可直接 `import core.*`；
- 注入 `astrbot` 模块桩，避免测试依赖真实 AstrBot 运行环境；
- 若当前环境没装 `gemini_webapi`，再补一个最小桩，让测试在无网/未装依赖时也能跑；
- 提供 `import_plugin_main()`，把 main.py 作为「包内模块」载入，
  使其中的相对导入 `from .core import ...` 能正确解析。
"""

import enum
import importlib.util
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


def _make_module(name: str) -> types.ModuleType:
    mod = types.ModuleType(name)
    mod.__name__ = name
    mod.__package__ = name
    sys.modules[name] = mod
    return mod


# --------------------------------------------------------------------- astrbot

astrbot = _make_module("astrbot")
api = _make_module("astrbot.api")


class _Logger:
    """与 astrbot.api.logger 接口一致的空实现桩"""

    def debug(self, *args, **kwargs):
        pass

    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        pass


for _mod in (astrbot, api):
    _mod.__path__ = []  # 声明为包，保证后续子模块可注册

api.logger = _Logger()


class AstrBotConfig(dict):
    """astrbot.api.AstrBotConfig 的最小桩（插件内仅作类型标注与 .get 使用）"""


api.AstrBotConfig = AstrBotConfig


# ------------------------------------------------------------ message_components
mc = _make_module("astrbot.api.message_components")


class _Component:
    """消息组件桩：接受任意位置/关键字参数，仅用于类型判定"""

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.kwargs or self.args})"


for _name in (
    "Plain",
    "Image",
    "File",
    "Video",
    "Record",
    "At",
    "Reply",
    "Face",
    "Json",
):
    setattr(mc, _name, type(_name, (_Component,), {}))


# ------------------------------------------------------------------------- event
event_mod = _make_module("astrbot.api.event")


class MessageChain:
    """astrbot.api.event.MessageChain 的最小桩"""

    def __init__(self, chain=None):
        self.chain = list(chain or [])


class AstrMessageEvent:
    """事件桩"""

    def __init__(self, bot=None):
        self.bot = bot


class _Filter:
    """`filter` 桩：任意 `@filter.<anything>(...)` 均原样返回被装饰函数"""

    def __getattr__(self, item):
        def _decorator(*args, **kwargs):
            if args and callable(args[0]):
                return args[0]

            def _wrap(func):
                return func

            return _wrap

        return _decorator


event_mod.MessageChain = MessageChain
event_mod.AstrMessageEvent = AstrMessageEvent
event_mod.filter = _Filter()


# -------------------------------------------------------------------------- star
star_mod = _make_module("astrbot.api.star")


class Context:
    pass


class Star:
    def __init__(self, context=None):
        self.context = context


class StarTools:
    @staticmethod
    def get_data_dir(name):
        raise RuntimeError("StarTools 仅在真实 AstrBot 环境中可用")


def register(*args, **kwargs):
    def _wrap(cls):
        return cls

    return _wrap


star_mod.Context = Context
star_mod.Star = Star
star_mod.StarTools = StarTools
star_mod.register = register


# ------------------------------------------------------------------- curl_cffi
try:  # gemini_webapi 的依赖，装了真实依赖就不需要桩
    import curl_cffi  # noqa: F401
except ImportError:  # pragma: no cover - 仅在无依赖环境下走到
    _curl = _make_module("curl_cffi")
    _curl_requests = _make_module("curl_cffi.requests")
    _curl_exc = _make_module("curl_cffi.requests.exceptions")

    # 继承关系与真实 curl_cffi 保持一致，异常映射的先后顺序才有意义
    _CurlError = type("CurlError", (Exception,), {})
    _RequestException = type("RequestException", (_CurlError,), {})
    _ConnectionError = type("ConnectionError", (_RequestException,), {})
    _Timeout = type("Timeout", (_RequestException,), {})
    for _name, _bases in (
        ("CurlError", (_CurlError,)),
        ("RequestException", (_RequestException,)),
        ("ConnectionError", (_ConnectionError,)),
        ("Timeout", (_Timeout,)),
        ("ConnectTimeout", (_ConnectionError, _Timeout)),
        ("ReadTimeout", (_Timeout,)),
        ("HTTPError", (_RequestException,)),
        ("ProxyError", (_RequestException,)),
        ("InvalidProxyURL", (_RequestException,)),
    ):
        setattr(_curl_exc, _name, type(_name, _bases, {}))

    _curl_requests.exceptions = _curl_exc

    class _CurlHttpVersion(enum.IntEnum):
        NONE = 0
        V1_0 = 1
        V1_1 = 2
        V2_0 = 3
        V2TLS = 4
        V3 = 5

    class _CurlFollow(enum.IntEnum):
        NONE = 0
        ALL = 1
        OBEYCODE = 2
        FIRSTONLY = 3
        SAFE = 4

    class _AsyncSession:
        """curl_cffi.requests.AsyncSession 的最小桩"""

        def __init__(self, **kwargs):
            self.kwargs = kwargs

        async def close(self):
            return None

        async def get(self, *args, **kwargs):
            raise NotImplementedError

    _curl.CurlHttpVersion = _CurlHttpVersion
    _curl.CurlFollow = _CurlFollow
    _curl.requests = _curl_requests
    _curl_requests.AsyncSession = _AsyncSession


# ------------------------------------------------------------------ gemini_webapi
try:  # 装了真实依赖就直接用，测试更贴近实际
    import gemini_webapi  # noqa: F401
except ImportError:  # pragma: no cover - 仅在无依赖环境下走到
    gwa = _make_module("gemini_webapi")

    class _GeminiBase(Exception):
        pass

    for _exc in (
        "AuthError",
        "APIError",
        "ImageGenerationError",
        "GeminiError",
        "ModelInvalidError",
        "TimeoutError",
        "UsageLimitExceededError",
        "TemporarilyBlockedError",
    ):
        setattr(gwa, _exc, type(_exc, (_GeminiBase,), {}))

    class _Base:
        def __init__(self, **kwargs):
            for key, value in kwargs.items():
                setattr(self, key, value)

    class GeminiClient(_Base):
        pass

    class Image(_Base):
        async def save(self, path="temp", filename=None, verbose=False, **kwargs):
            raise NotImplementedError

    for _name in ("WebImage", "GeneratedImage", "ModelOutput", "AvailableModel", "ChatSession"):
        setattr(gwa, _name, type(_name, (_Base,), {}))
    gwa.Image = Image

    # 账号状态枚举：client/errors 用它判断 Cookie 是否真的生效
    _gwa_constants = _make_module("gemini_webapi.constants")

    class _AccountStatus(enum.IntEnum):
        AVAILABLE = 1000
        ACCESS_TEMPORARILY_UNAVAILABLE = 1014
        UNAUTHENTICATED = 1016
        ACCOUNT_REJECTED = 1021
        ACCOUNT_UNTRUSTED = 1033
        TOS_PENDING = 1040
        TOS_OUT_OF_DATE = 1042
        ACCOUNT_REJECTED_BY_GUARDIAN = 1054
        GUARDIAN_APPROVAL_REQUIRED = 1057
        LOCATION_REJECTED = 1060

    _gwa_constants.AccountStatus = _AccountStatus
    gwa.constants = _gwa_constants
    gwa.GeminiClient = GeminiClient
    gwa.logger = api.logger
    gwa.set_log_level = lambda *a, **k: None


# ------------------------------------------------------------------ 插件入口载入


def import_plugin_main():
    """把仓库根目录当作包，载入其中的 main.py 并返回该模块。"""
    pkg_name = ROOT.name
    if pkg_name not in sys.modules:
        pkg = types.ModuleType(pkg_name)
        pkg.__path__ = [str(ROOT)]
        sys.modules[pkg_name] = pkg

    full_name = f"{pkg_name}.main"
    if full_name in sys.modules:
        return sys.modules[full_name]

    spec = importlib.util.spec_from_file_location(full_name, ROOT / "main.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = mod
    spec.loader.exec_module(mod)
    return mod
