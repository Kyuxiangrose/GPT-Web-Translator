# GPT 网页翻译（DeepSeek 本机版）

这是一个可加载到 Microsoft Edge 和 Google Chrome 的 Manifest V3 网页翻译扩展。扩展只连接本机 `127.0.0.1:8765`，DeepSeek API Key 只由本机后端从当前 Windows 用户配置或环境变量读取。交付包内含独立的 `GPT-Web-Translator-Backend.exe`，正常使用无需另行安装 Python。

## 主要能力

- 默认自动判断并翻译非简体中文网页；简体中文页面不请求 API。
- 支持“中文界面 + 外语正文”的混合页面；例如安装 GitHub 中文化插件后，仍会翻译仓库说明、README 和英文提交说明，同时跳过中文导航、代码、路径与文件名。
- X/Twitter、YouTube 及其他网站的用户名、账号显示名、昵称、频道名、创作者名和 `@handle` 保持原文；独立身份节点不发送，混合文本中的身份名称在本机替换为确定性占位符，译文返回后再逐字符还原。
- 中文 / 双语两种显示模式，切换时只复用已有译文。
- 停止翻译与恢复原文分离。
- 当前网站自动翻译规则保存在浏览器本机。
- 弹窗显示当前网页和本插件累计的真实 Token、按官方单价计算的实际费用，以及独立读取的 DeepSeek 账户当前余额；历史清除需二次确认。
- 支持 SPA、无限滚动和动态新增内容。
- 跳过输入框、编辑区、密码、代码、隐藏内容和技术结构。
- 只改可见文本，不改链接 `href`、查询参数或锚点。
- SQLite 持久缓存、请求合并、并发限制、超时和重试。

## 第一次使用

1. 双击 `配置API密钥.bat`，输入 DeepSeek API Key。只需配置一次；以后升级或更换插件目录仍会自动使用。
2. 双击 `启动翻译服务.bat`；脚本会优先运行已打包的独立后端 EXE。
3. Edge 打开 `edge://extensions`，或 Chrome 打开 `chrome://extensions`。
4. 开启开发者模式，选择“加载解压缩的扩展”，选择本项目的 `extension` 文件夹。
5. 打开外语网页；点击扩展图标可切换中文/双语、停止、恢复或设置当前网站规则。

如需开机自动启动本机服务，可双击 `安装开机启动.bat`；撤销时双击 `移除开机启动.bat`。

## 后端状态

服务启动后，可在本机打开 `http://127.0.0.1:8765/health`。返回值只报告服务是否已配置，不会显示 API Key。

日志位于 `backend/logs/service.log`，缓存位于 `backend/data/translations.sqlite3`。日志不会记录 API Key 或完整网页文本。

项目同时保留后端 Python 源码，便于长期审查和维护；只有在独立 EXE 缺失时，启动脚本才会尝试系统 Python。

## 当前 DeepSeek 配置

- Base URL：`https://api.deepseek.com`
- 接口：`POST /chat/completions`
- 默认模型：`deepseek-flash`（DeepSeek-V4.1-Flash）
- 输出：JSON Output
- 翻译模式：非思考模式

模型和连接参数保存在 `%LOCALAPPDATA%\GPT-Web-Translator\.env`；参照 DeepSeek 官方更新日志调整即可。官方资料：

- https://api-docs.deepseek.com/updates/
- https://api-docs.deepseek.com/quick_start/pricing
- https://api-docs.deepseek.com/api/create-chat-completion
- https://api-docs.deepseek.com/guides/json_mode/

## 安全说明

- 不要把 `%LOCALAPPDATA%\GPT-Web-Translator\.env` 发给别人，也不要提交到 Git。
- 不要把 API Key 写入扩展、`manifest.json`、HTML 或浏览器存储。
- 后端固定监听 `127.0.0.1`，并拒绝普通网页 Origin 调用翻译接口。
- 本项目只发送网页可见的自然语言文本；不读取表单值和正在编辑的内容。

## 自动化测试

- 后端：`python -m unittest discover -s backend/tests -v`
- 扩展 DOM：需要 Node.js 与 Playwright，运行 `node --test tests/extension.test.js`

真实 DeepSeek 请求需要用户本人的 Key 和余额；自动化测试使用本地桩实现，不产生 API 费用。

本交付版本已通过：后端 44 项测试、扩展 21 项 DOM/行为与后台统计测试、Microsoft Edge 真实加载扩展的端到端测试，以及独立 Windows EXE 的实际启动与健康检查。另已检查 Manifest V3、最小权限、API Key 泄漏、Windows cmd CRLF/PowerShell 5.1 编码兼容性和打包内容。

## 1.0.7：实际用量计费与官方余额（2026-09-19）

- 默认模型更新为官方当前名称 `deepseek-flash`；旧配置中的 `deepseek-v4-flash` 会自动转换，无需重新输入 API Key。
- 每次成功响应继续以 DeepSeek 返回的缓存命中、缓存未命中和输出 Token 为真实用量来源，不按字符数估算。
- 当前 Flash 人民币单价更新为：空闲时段每百万 Token 分别为 ¥0.02、¥1.00、¥4.00；高峰时段分别为 ¥0.04、¥2.00、¥8.00。
- 对分类完整的旧记录执行一次价格重算，修正 1.0.6 使用旧价目表造成的金额偏差；缺少分类字段的旧记录仍不猜测金额。
- 弹窗新增 DeepSeek 官方账户余额。余额通过 `/user/balance` 读取、最多每分钟刷新一次，与本插件累计费用分开显示。
- API Key 仍只由本机后端使用，扩展只能收到余额数字，不能读取 Key。
- 已通过 44 项后端测试、21 项扩展测试和隔离的真实 MV3 浏览器端到端测试；测试使用本地模拟响应，不消耗真实 API 余额。

当前价格版本为 `deepseek-cn-2026-09-10`。官方依据：[DeepSeek 模型与人民币价格](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)、[DeepSeek Token 用量](https://api-docs.deepseek.com/quick_start/token_usage/)、[DeepSeek 余额接口](https://api-docs.deepseek.com/zh-cn/api/get-user-balance/)。

## 1.0.8：扩展重载错误修复（2026-09-19）

- 修复更新扩展后，旧网页脚本因扩展上下文失效而留下 `Extension context invalidated` 红色错误的问题。
- 同时兼容消息发送时的同步异常与 Promise 拒绝；旧页面会安静提示刷新，不再产生未处理异常。

## 1.0.6：API Key 跨版本保留（2026-09-18）

- API Key 改为保存在当前 Windows 用户的 `%LOCALAPPDATA%\GPT-Web-Translator\.env`。
- 从旧版升级时，启动脚本会自动把项目目录中的旧 `.env` 迁移到用户配置目录。
- 下载新版到不同文件夹后，仍可直接使用已经配置过的 Key，无需重新输入。
- `.env` 仍被 Git 忽略，不会上传到 GitHub。

## 1.0.3：Token 统计（2026-09-12）

现有安装升级：在原目录停止并重新启动本机服务，然后在 chrome://extensions 找到本扩展并点击重新加载，最后刷新要翻译的网页。新版 EXE 与源码均已更新；不用重新配置 API Key。请继续使用原来的 extension 目录，以保留原扩展身份和浏览器设置。

- 当前网页：每次文档加载生成独立 page_id；所有分批请求与动态加载的请求累加到同一文档，刷新后新文档从零开始。停止/继续、恢复原文、中文/双语切换不会重置这个编号。
- 历史累计：主账本沿用 backend/data/translations.sqlite3，新建独立统计表；SQLite 事务和唯一事件编号保证并发累加与去重。原译文表、缓存键和配置不变。关闭浏览器、重启服务或电脑后保留已写入的统计。
- 只使用成功响应 usage.total_tokens 的整数值，不估算，也不根据字符数或输入/输出字段猜测。DeepSeek 的提示缓存 Token 已包含在其返回的 total_tokens 中，照常统计。
- 本机译文缓存命中不调用 API，返回 usage.total_tokens = 0。模式切换复用页面已有译文，也不会增加 Token。
- API 的 HTTP 错误不计数；HTTP 成功后即使译文 JSON 格式不合格、需要重试，或用户刚好停止翻译，该成功响应的真实用量仍计入。每次成功重试分别计入。
- 少数成功响应没有有效 total_tokens 时保留“用量未知”标记，界面提示仅显示已知用量，不伪造 0 消耗。服务离线时浏览器显示最近同步值并标明离线，服务恢复后自动校准。
- 点击“清除历史统计”后先选择取消或确认。确认只重置历史显示累计，不清除当前文档统计、译文缓存或配置；清除之后才收到的成功响应计入新累计。
- 统计从 1.0.3 启用后开始。旧缓存不是完整消费流水，无法可靠补回升级前历史。

扩展向本机 POST /v1/usage 读取统计，POST /v1/usage/clear 清除统计（必须 confirmed=true）。沿用原来的扩展 Origin 检查和 X-GWT-Client 请求头，统计仅包含整数、匿名文档编号及账本版本，不包含 API Key、网页 URL 或原文。翻译响应与部分失败响应附带 token_stats 快照；扩展后台用递增 revision 串行同步到 chrome.storage.local 的 gwtTokenStats，不累加客户端 usage。只保留最近 256 个文档的离线显示副本，本机主账本保留所有已记录统计。

### 本次验证

- 34 项 Python 后端测试通过。
- 20 项扩展 DOM/行为与后台并发测试通过（13 项 DOM/行为 + 7 项后台统计测试）。
- 在隔离的 Microsoft Edge Chromium 配置中实际加载 MV3 扩展，连接本次修改后的 Python HTTP 服务：41 段分三批为 341 Token，另一个标签页为 101 Token，历史合计 442；刷新命中本机缓存后当前页为 0、历史仍为 442。确认清除后历史归零，另一个页面当前值仍为 101；新增动态段落后历史变为 101、该页变为 202。
- 已验证弹窗取消/确认清除、中文/双语切换无额外消费、服务离线显示副本、浏览器和服务重启后保留累计。
- 已重新打包独立 Windows EXE，实际启动两次并验证磁盘中的 456 Token 测试记录重启后仍为 456。
- 本次测试替换的是 DeepSeek 上游响应，未使用真实 API Key 或余额；没有自动操作用户日常 Chrome 配置。

运行新增测试：

```
python -m unittest discover -s backend/tests -v
node --test tests/extension.test.js tests/token-background.test.js
```

完整浏览器测试脚本是 tests/token-e2e.js，使用 backend/tests/token_e2e_fixture.py；需设置 GWT_PYTHON 为 Python 可执行文件、GWT_TEST_WORK 为临时测试目录，并保证 Playwright 可用。测试在独立目录和浏览器配置中运行，不接触真实 Key 或现有数据库。

字段依据：[DeepSeek Chat Completions 官方响应结构](https://api-docs.deepseek.com/api/create-chat-completion/)。持久存储说明：[Chrome storage.local 官方文档](https://developer.chrome.com/docs/extensions/reference/api/storage/)。

## 1.0.4：金额统计（2026-09-12）

统计卡片新增“当前消耗金额”和“总消耗金额”，以人民币显示到小数点后 6 位。仍只保留一个“清除历史统计”入口；确认后在同一个 SQLite 事务中同时清零历史 Token 与历史金额，当前网页两项统计保留。

金额不是按总 Token 粗略换算。后端在每次成功的 DeepSeek 响应中读取 prompt_cache_hit_tokens、prompt_cache_miss_tokens、completion_tokens、model 和 created，按照响应时段对应的官方人民币单价计算，并把该次金额固化为整数纳元（十亿分之一元）。旧账不会因为以后单价变化而被重新计算。

当前价格版本为 deepseek-cn-2026-08-17：

| 模型与时段 | 缓存命中输入/百万 Token | 缓存未命中输入/百万 Token | 输出/百万 Token |
|---|---:|---:|---:|
| deepseek-v4-flash 空闲 | ¥0.05 | ¥1.50 | ¥4.50 |
| deepseek-v4-flash 高峰 | ¥0.10 | ¥3.00 | ¥9.00 |
| deepseek-v4-pro 空闲 | ¥0.15 | ¥4.50 | ¥13.50 |
| deepseek-v4-pro 高峰 | ¥0.30 | ¥9.00 | ¥27.00 |

北京时间 09:00-12:00、14:00-18:00 按高峰价，其余按空闲价。deepseek-v4-flash-vision-exp 使用官方表中与 flash 相同的价格。

1.0.3 已保存 total_tokens，但没有保存计算金额必需的缓存和输出分类。升级时保留已有 Token，把这些旧请求标为“金额明细未知”，不猜测历史金额。界面显示从 1.0.4 起能够精确计算的已知金额；清除一次历史后，两项历史累计从同一时点重新开始。

金额统计沿用同一 SQLite 文件，在 token_history、token_pages 和 token_events 中增加金额与计价审计字段；浏览器离线镜像同步保存金额快照。没有新增扩展权限或新的清除按钮。

本版本通过 38 项后端测试、20 项扩展/后台测试及真实 MV3 隔离浏览器端到端测试。端到端固定用量结果：第一页 341 Token/¥0.000548，第二页 101 Token/¥0.000126，总计 442 Token/¥0.000673；一次确认同时清零 Token 和金额历史，当前页统计保留；动态请求、缓存零费用、标签页隔离、离线镜像和重启持久化均通过。

金额依据：[DeepSeek 模型与人民币价格](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)、[DeepSeek usage 缓存字段](https://api-docs.deepseek.com/zh-cn/api/create-chat-completion)。

## 1.0.5：修正金额未更新（2026-09-12）

DeepSeek 实际响应的模型名可能是 deepseek-flash，尽管请求配置为 deepseek-v4-flash。1.0.4 已保存真实 Token 和缓存命中/未命中分类，但由于响应别名不在价目表中，金额记录为未知。本版以本机请求的已验证模型配置计价，同时识别官方模型别名。

启动时会对 1.0.4 已保存且分类完整、价格模型可识别的事件自动补算金额。补算在同一 SQLite 事务中完成，已补算的事件不会再次补算；历史清除前的事件只修复其当前网页记录，不会重新进入已清零的历史。旧版根本没有分类字段的事件仍显示金额未知。

针对实际账本的只读副本验证：最近三笔共 3,090 Token，补算后当前网页及历史金额均为 ¥0.011190（实际整数值为 11,189,600 纳元），未知金额笔数从 3 降为 0。已通过 39 项后端测试。
