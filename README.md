# Gemini 网页版生图 — AstrBot 插件

在 QQ 里发一句描述，插件调用 **网页版 gemini.google.com**（Nano Banana 图像模型）生成图片，
并把图片作为回复发给发送者。

**不使用 Google 官方 API，也不需要 API Key** —— 只依赖浏览器 Cookie（`__Secure-1PSID` / `__Secure-1PSIDTS`）
走网页版内部接口。

## 特点

- **文生图**：`/gemini生图 一只戴着墨镜的柴犬在沙滩上冲浪，插画风格`
- **图生图／改图**：发一张图 + `/gemini生图 把它改成夜景`，交给 Gemini 图像编辑
- **回复发送者**：群聊里自动 @ 发送者，图只回给提问的人
- 群白/黑名单、管理员限定、冷却时间、每日配额，防止被群友刷爆账号
- Cookie 失效自动重建连接并重试；后台自动刷新 Cookie 并**跨重启复用**
- 所有失败都会翻译成一句人话（Cookie 过期 / 代理不通 / 风控限流 / 额度用尽 / 超时）

## 原理

| 项 | 说明 |
|---|---|
| 认证 | 浏览器 Cookie `__Secure-1PSID` + `__Secure-1PSIDTS` |
| 接口 | `gemini.google.com` 网页版内部 RPC（通过 [gemini-webapi](https://github.com/HanaokaYuzu/Gemini-API)） |
| 生图模型 | Gemini 内置的 Nano Banana，由提示词触发，无需单独指定 |
| 与官方 API 的区别 | 不消耗 Google AI Studio 的 API 额度，用账号在网页端的免费额度 |

## 安装

```bash
# 1. 把插件目录放进 AstrBot 的插件目录
cp -r astrbot_plugin_gemini_image ~/astrbot/data/plugins/

# 2. 装依赖（Docker 部署则进容器执行）
pip install -r ~/astrbot/data/plugins/astrbot_plugin_gemini_image/requirements.txt

# 3. 重启 AstrBot
docker restart astrbot
```

然后在 AstrBot Web UI（默认 http://localhost:6185）→ 插件管理 → Gemini 网页版生图 → 填配置。

> 要求 Python ≥ 3.11（AstrBot 4.16+ 满足）。

## 获取 Cookie（必做）

1. 浏览器打开 https://gemini.google.com 并登录 Google 账号；
2. 按 `F12` → 切到 **网络 / Network** 面板 → 刷新页面；
3. 随便点一个请求 → 找到 **Cookie / 请求标头**；
4. 复制这两个值：
   - `__Secure-1PSID`
   - `__Secure-1PSIDTS`
5. 填进插件配置的 `secure_1psid` / `secure_1psidts`。

> **强烈建议按下面的方式取（直接决定能用多久）：**
>
> 1. 开一个**无痕窗口**（`Ctrl+Shift+N` / `Cmd+Shift+N`）登录 https://gemini.google.com
> 2. 按上面步骤取出两个值
> 3. **立刻关闭这个无痕窗口** —— 这一步必须做
>
> 原因：`__Secure-1PSIDTS` 是**短期凭证**，约 10~20 分钟就会被轮换一次，
> 之后靠插件的自动刷新持续续期。如果取 Cookie 的那个浏览器会话还开着，
> 它会和插件**抢着轮换**，谁后轮换谁有效 —— 插件手里那份随时可能失效。
>
> 同理：**配置好之后不要频繁重载插件或重启容器**。每次重连都要重新等一轮自动刷新，
> 期间凭证若正好过期就会掉线，且**无法自动恢复，只能重新取 Cookie**。
>
> 建议使用**专门的 Google 账号**，避免影响你自己的浏览器登录。

## 配置项

| 配置 | 默认 | 说明 |
|---|---|---|
| secure_1psid | — | **必填**，`__Secure-1PSID` |
| secure_1psidts | — | 强烈建议填写，`__Secure-1PSIDTS` |
| use_proxy / proxy_url | 开 / `http://127.0.0.1:7890` | **国内必须**，容器内填宿主机 IP（如 `http://172.17.0.1:7890`） |
| auto_refresh_cookies | 开 | 后台刷新 1PSIDTS 并落盘，重启后自动复用 |
| model | 空 | 留空用账号默认；可填 `gemini-flash` / `gemini-pro` |
| temporary_chat | 开 | 生图不写入账号对话历史 |
| prompt_prefix | `Generate an image: ` | 文生图前缀（Gemini 只有明确要求「生成」才会走图像模型） |
| edit_prompt_prefix | `Edit the provided image…` | 图生图前缀 |
| max_images | 4 | 一次最多发几张图 |
| max_input_images | 1 | 图生图最多读几张输入图，0 = 关闭图生图 |
| include_text / max_text_length | 开 / 200 | 是否附带 Gemini 的文字说明及其截断长度 |
| at_sender | 开 | 群聊回复时 @ 发送者 |
| send_progress | 开 | 先发「正在生成」提示 |
| init_timeout / request_timeout / watchdog_timeout | 60 / 300 / 180 | 连接、生图、流式看门狗超时（秒） |
| min_request_interval | 5 | **全局最小请求间隔（秒）**，防连续高频请求，0 = 不限 |
| blocked_cooldown_seconds | 300 | 触发风控（429）后的静默时长（秒） |
| allow_group / group_whitelist / group_blacklist | 开 / 空 / 空 | 群聊开关与名单（多个群号用逗号分隔） |
| admin_only | 关 | 仅管理员可用 |
| cooldown_seconds / daily_quota / global_daily_quota | 30 / 20 / 200 | 冷却秒数 / 每人每日次数 / 全站每日总次数（0 = 不限） |
| enable_short_command | 关 | 是否注册短指令 `/生图`（避免与其它插件冲突） |
| image_retain_days | 3 | 本地图片保留天数，0 = 不清理 |
| verbose_log | 关 | 输出 gemini-webapi 调试日志 |

## 指令

| 指令 | 说明 |
|---|---|
| `/gemini生图 <画面描述>` | 生成图片（别名 `/gimg`、`/gimage`） |
| `/gemini生图 <修改要求>`（消息里带图） | 图生图／改图 |
| `/gemini状态` | 自检 Cookie / 代理 / 连接 / 今日用量 |
| `/gemini重置` | 管理员：清空限流数据并重建连接 |
| `/gemini帮助` | 显示帮助 |
| `/生图 <画面描述>` | 短指令，默认关闭，需在配置里开启 |

## 常见问题

**1. 一直提示「连接 gemini.google.com 失败」**
国内服务器没走代理。开启 `use_proxy` 并确认 `proxy_url` 可通：
`docker exec -it astrbot curl -x http://172.17.0.1:7890 -I https://gemini.google.com`
（容器内的 `127.0.0.1` 是容器自己，要填宿主机的地址。）

**2. 提示「Cookie 过期或被登出」**
重新按上面的步骤取一次 Cookie。开了自动刷新的话，多数情况下无需人工干预；
但 Google 主动失效会话时只能手动更新。

**3. 返回的是网络图片而不是 AI 生成图**
Gemini 把提示词理解成了「搜图」。把描述写具体些，或把 `prompt_prefix` 改强硬：
`Generate an image, do not search the web: `

**4. 提示「额度已用尽」或「触发风控」**
免费账号有生图次数限制；高频请求会被 429。降低 `daily_quota` / `global_daily_quota`、
调大 `cooldown_seconds` 与 `min_request_interval`，或更换代理出口 IP。

**5. 会不会被封号？**
插件内置了多层防护，从紧到松依次是：

1. **请求串行**——同一账号同一时刻只会有一个请求在跑，绝不并发；
2. **全局最小间隔**（`min_request_interval`，默认 5 秒）——避免「一个人刚发完、另一个人立刻发」
   形成连续高频，这是最容易触发风控的情形；
3. **风控熔断**（`blocked_cooldown_seconds`，默认 300 秒）——一旦吃到 429 立刻静默，
   期间不再发出任何请求；
4. **每人每日配额** + **全站每日总配额**。

最有效的做法是把 `min_request_interval` 保持在 5 秒以上，并使用**专门的 Google 账号**。
另外注意：取出 Cookie 后，插件的自动刷新可能导致浏览器端需要重新登录，这是正常现象。

**6. 换了一台机器/容器重建后要重新填 Cookie 吗？**
不需要。`auto_refresh_cookies` 开启时，刷新后的 `__Secure-1PSIDTS` 会落盘到插件数据目录，
重启后会优先复用缓存中较新的值。

**5. 能连上但账号状态异常**
网页版生图要求账号年满 18 岁、所在国家/地区在支持范围内。用 `/gemini状态` 查看具体提示。

## 免责声明

本插件通过**非官方**方式调用 Gemini 网页版接口，可能因 Google 调整接口而失效；
自动刷新 Cookie 可能导致浏览器端重新登录；高频使用存在账号被限流的风险。
请自行评估，建议使用专门的 Google 账号，并控制调用频率。

## License

MIT
