# Changelog

本文件记录 astrbot_plugin_gemini_image 的所有重要变更。

## [1.1.4] - 2026-09-17

### Fixed

- **图片下载失败不再「一次就放弃」**：单张下载失败时会**重建连接重试一次**。
  下载环节的失败往往是瞬时的 —— 图片 CDN 对出口 IP 的临时判定、URL 签名校验抖动、
  连接复用失效等，实测遇到过 `HTTP 403` 而**重试即成功**；此前失败一次就丢给用户、
  让用户自己再发一遍，体感很差
- **修正下载失败时的提示文案**：原文案把「下载被拒绝（CDN 限流 / 链接校验）」
  笼统说成「网络/代理不稳定」，会把排查方向带偏

## [1.1.3] - 2026-09-17

### Fixed

- **`CurlFollow.SAFE` 引起的连接失败与「误报」**。gemini-webapi 在
  `utils/get_access_token.py`、`types/image.py`、`types/video.py` 三处把
  `allow_redirects` 写死为 `CurlFollow.SAFE`，而该模式会拒绝「重定向到内网 IP」。
  经内网代理出网时（容器里的 `http://172.17.0.1:7890` 就是典型），**代理地址本身**
  会被判成 SSRF 目标并直接拒掉：

      curl: (7) Redirect to internal IP 172.17.0.1 rejected (SSRF protection)

  更糟的是它**掩盖了真正的失败原因** —— 凭据失效等引起的重定向也会报这一句，
  极易误判成「代理坏了」。现于**模块级**把 `CurlFollow.SAFE` 修正为普通跟随重定向
  （`core/client.py::patch_curl_follow_safe()`），对上游代码零侵入
- **网络类错误不再直接失败**：连接被拒 / 超时等瞬时故障，现在会丢弃连接并重试一次
  （此前只对凭据失效重试，一次几秒的节点抖动就会让用户直接看到失败）

### Notes

- 补丁有个隐蔽的坑值得记下：`gemini_webapi/utils/__init__.py` 里
  `from .get_access_token import InitSession, get_access_token` 遮蔽了同名子模块，
  导致 `import gemini_webapi.utils.get_access_token as m` 拿到的是**函数**而不是模块
  （Python 3.7+ 的 `import a.b as c` 等价于 `getattr(a, "b")`），
  直接 `m.CurlFollow = ...` 会**静默失效**。必须用 `importlib.import_module`

## [1.1.2] - 2026-09-17

### Fixed

- **短指令「生图」关闭时会静默吞掉消息**。AstrBot 的指令注册是静态的：handler 一旦注册
  就会被路由命中，执行后事件停止传播——之前关闭时直接 return，用户发 `/生图` 没有任何
  反馈，其它插件的同名指令也被一并挡住。现改为回复明确的未开启提示并引导改用
  `/gemini生图`，配置项 hint 同步说明该行为
- **测试桩导致无依赖环境下 2 项测试失败**：conftest 的 curl_cffi 桩在循环里重复创建
  同名异常类，`ConnectTimeout` 继承的是第一版基类，与挂到模块上的第二版
  `ConnectionError` / `Timeout` 没有 isinstance 关系，异常映射测试落空；loguru 回归
  测试在未安装 loguru 的环境直接 ModuleNotFoundError。现修复桩的类创建方式，
  loguru 测试改为无依赖时跳过

## [1.1.1] - 2026-09-16

### Fixed

- **严重：插件加载后 AstrBot 全局日志消失**。v1.0.1 为屏蔽 gemini-webapi 的噪音日志，
  调用了 `gemini_webapi.set_log_level()`；而该函数内部是
  `logger.remove(_handler_id)`，首次调用时 `_handler_id` 为 `None` ——
  loguru 的 `remove(None)` 语义是**移除全部 handler**，把 AstrBot 自己注册的日志输出
  一并删掉了，表现为插件加载后机器人**再无任何日志输出**，直到重启容器。
  现改用 loguru 的模块级开关 `logger.disable("gemini_webapi")`，
  只丢弃该库自己的记录，对 AstrBot 零影响。
  已补回归测试：屏蔽前后全局 handler 数量必须不变。

## [1.1.0] - 2026-09-16

### Added

- **全局最小请求间隔**（`min_request_interval`，默认 5 秒）。串行锁只保证两个请求「不重叠」，
  但「A 刚结束、B 立刻开始」在账号侧看依然是连续高频请求——现在强制拉开间隔，
  这是防风控最有效的一环
- **风控熔断**（`blocked_cooldown_seconds`，默认 300 秒）。一旦吃到 429 就进入静默期，
  期间直接拒绝新请求，不再去撞击（风控期间继续撞是最伤账号的操作）
- **全站每日总配额**（`global_daily_quota`，默认 200 次）。单人配额挡不住
  「人多时被轮流刷」，总量上限是账号安全的最后一道闸
- `gemini状态` 现在会展示节流状态与全站今日总量

### Changed

- 触发风控时不再「重建连接重试一次」，改为直接熔断（少一次撞击）

## [1.0.1] - 2026-09-16

### Fixed

- **经内网代理出网时图片下载必失败**：gemini-webapi 自建下载 session 时把
  `allow_redirects` 写死为 `CurlFollow.SAFE`，而该模式会拒绝「重定向到内网 IP」。
  当出口必须经过内网代理（容器里的 `http://172.17.0.1:7890` 就是典型）时，
  下载会被判成 SSRF 目标直接拒掉，表现为「图片生成成功却一张都发不出去」，
  日志为 `curl: (7) Redirect to internal IP ... rejected (SSRF protection)`。
  现改为自建下载 session 并正常跟随重定向
- 下载失败时不再谎报「Gemini 没有返回图片」，改为明确提示失败张数

### Changed

- `requirements.txt` 显式声明 `curl-cffi>=0.16.2,<0.17`：gemini-webapi 2.1.1 用到了
  curl_cffi 0.16 新增的 `CurlFollow` / `CurlHttpVersion`，一旦被其它插件降级，
  插件会直接加载失败；显式声明后 AstrBot 的依赖预检查能自动把它装回来
- 默认屏蔽 gemini-webapi 的非错误日志：它的裸 loguru 记录缺少 AstrBot 日志 handler
  需要的 `plugin_tag` 字段，会把日志刷成 `KeyError: 'plugin_tag'`；
  配置里打开 `verbose_log` 可恢复完整日志

### Tests

- 单元测试 42 → 64 项，新增下载 session 参数、下载失败计数、降级路径等用例

## [1.0.0] - 2026-09-16

### Added

- 首次发布：通过网页版 gemini.google.com（Nano Banana）生成图片，走 Cookie 认证，
  **不使用 Google 官方 API、无需 API Key**
- 文生图：`/gemini生图 <画面描述>`，别名 `/gimg`、`/gimage`
- 图生图／改图：消息携带图片时自动走 Gemini 图像编辑
- 结果回复发送者：群聊自动 @ 发送者，支持私聊与群聊
- 客户端封装：懒初始化、请求串行化、Cookie 失效自动重建并重试一次
- Cookie 增强：后台自动刷新 `__Secure-1PSIDTS`，并在进程重启后优先复用缓存中较新的值
  （gemini-webapi 只写不读，这里补齐了读取环节）
- 限流：按用户冷却时间 + 每日配额，跨天自动重置，数据持久化到插件数据目录
- 权限：群白名单 / 群黑名单 / 管理员限定
- 生图提示词前缀可配置，规避 Gemini「搜图而非生图」的默认行为
- 异常翻译层：把 Cookie 失效、代理不可用、风控限流、额度用尽、超时等
  统一转成用户可读的中文提示
- `gemini状态` 自检指令、`gemini重置` 管理指令、`gemini帮助`
- 本地图片按天数自动清理
- 单元测试 42 项，覆盖限流、生图编排、异常映射与指令解析
