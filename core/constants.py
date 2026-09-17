"""插件级常量。"""

from __future__ import annotations

PLUGIN_NAME = "astrbot_plugin_gemini_image"
PLUGIN_VERSION = "1.1.4"
PLUGIN_AUTHOR = "huashuiyue07"
PLUGIN_DESC = "网页版 Gemini 生图 - 走 gemini.google.com 网页接口，无需 Google API Key"
PLUGIN_REPO = "https://github.com/huashuiyue07/astrbot_plugin_gemini_image"

#: 生成图片的落盘目录名（位于 AstrBot 数据目录下）
IMAGES_DIRNAME = "images"

#: 限流数据的落盘文件名
LIMITER_FILENAME = "usage.json"

#: 默认生图提示词前缀。
#:
#: Gemini 网页版只有在**明确要求生成图片**时才会调用图像生成模型（Nano Banana），
#: 否则它会去联网搜图并返回 WebImage。QQ 用户往往只会写「一只在屋顶上的橘猫」
#: 这种纯描述，所以统一补一个生成意图前缀。
DEFAULT_PROMPT_PREFIX = "Generate an image: "

#: 默认图生图（改图）提示词前缀。
#: 带输入图时不能再强调「generate an image」以外的新图语义，否则容易丢掉原图特征，
#: 因此用 edit 语义。
DEFAULT_EDIT_PROMPT_PREFIX = "Edit the provided image and generate a new image: "

#: 主指令及其别名（英文别名冲突概率极低，中文短指令单独由配置开关控制）
COMMAND_MAIN = "gemini生图"
COMMAND_ALIASES = {"gimg", "gimage"}
COMMAND_STATUS = "gemini状态"
COMMAND_RESET = "gemini重置"
COMMAND_HELP = "gemini帮助"
#: 可选短指令，默认关闭，避免与其它生图插件抢指令
COMMAND_SHORT = "生图"

#: 图片说明文字截断长度上限（配置项默认值）
DEFAULT_MAX_TEXT_LENGTH = 200

#: 下载生成结果图片时的浏览器指纹兜底值。
#: 正常情况下会用底层 GeminiClient 已协商好的指纹，这里只是在拿不到时兜底。
DEFAULT_IMPERSONATE = "chrome120"
