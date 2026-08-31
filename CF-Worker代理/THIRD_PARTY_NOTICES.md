# 第三方开源声明与许可（Third-Party Notices）

本目录（`CF-Worker代理`）的 Cloudflare Worker 代码在编写过程中参考 / 衍生自以下开源项目。
在此向相关作者致谢，并按其开源协议保留版权与许可声明。

## 一览表

| 文件 | 参考/来源 | 作者/版权人 | 协议 | 关系 |
|---|---|---|---|---|
| `_worker.js`（必应通用反代） | [jonssonyan/cf-workers-proxy](https://github.com/jonssonyan/cf-workers-proxy) | jonssonyan | **GPL-3.0** | 衍生作品（Modified Version），已修复 POST 转发并精简 |
| `cf-worker-google-proxy.js` 的 tk 算法 | [Stichoza/google-translate-php](https://github.com/Stichoza/google-translate-php) | Copyright (c) 2013 Levan Velijanashvili | **MIT** | 参考其公开 tk/TKK 算法做独立 JS 实现 |
| `cf-worker-google-proxy.js` 抗限流思路 | [UniClawAI/google_translate](https://github.com/UniClawAI/google_translate) | UniClawAI | 无 LICENSE（仅思路致谢，未逐字复制） | 方法参考 |
| 多 TLD 端点轮换 | googletrans / gtts 等社区通用实现 | 各项目 | 多为 MIT/Apache-2.0 | 通用做法 |

协议全文见 `licenses/` 目录：
- `licenses/GPL-3.0.txt`：GNU 通用公共许可证 v3.0 全文
- `licenses/MIT-Stichoza.txt`：Stichoza 项目的 MIT 许可原文

---

## 1. cf-workers-proxy（GPL-3.0）—— `_worker.js`

- 项目：https://github.com/jonssonyan/cf-workers-proxy
- 协议：GNU General Public License v3.0
- 关系：`_worker.js` 是在其代码基础上修改的**衍生作品**。按 GPL-3.0 要求：
  1. 保留原作者版权与许可声明（已写入 `_worker.js` 文件头）；
  2. 标注修改内容（已写入文件头“相对上游的修改内容”）；
  3. 衍生作品继续以 **GPL-3.0** 开源，分发时必须同时提供完整源码（本仓库公开即满足）；
  4. 不提供任何担保。

## 2. google-translate-php（MIT）—— tk 算法

- 项目：https://github.com/Stichoza/google-translate-php
- 版权：Copyright (c) 2013 Levan Velijanashvili
- 协议：MIT License。MIT 允许商用、修改、分发，唯一要求是**保留版权与许可声明**，
  已在 `cf-worker-google-proxy.js` 文件头注明，原文存于 `licenses/MIT-Stichoza.txt`。
- 说明：Google Translate 的 tk(TKK) 签名是公开通用算法，在大量不同语言的开源项目中
  都有等价实现，本文件为独立的 JavaScript 实现，并非逐行移植。

## 3. UniClawAI/google_translate（无 LICENSE）—— 思路致谢

- 项目：https://github.com/UniClawAI/google_translate
- 该仓库**未附带 LICENSE 文件**。按默认著作权规则，无许可即“保留所有权利”，
  因此本项目**未逐字复制其源代码**，仅参考其“会话 Cookie + tk + 会话复用以降低风控”
  这一不受著作权保护的**工程思路**，并独立实现为 Cloudflare Worker 版本。特此致谢。

---

## 协议兼容性与使用提醒

- `_worker.js` 为 **GPL-3.0**，具有 Copyleft 传染性：任何再分发或基于它的衍生作品
  都必须继续以 GPL-3.0 开源并提供源码。该义务仅作用于 `_worker.js` 这一衍生文件本身。
- `cf-worker-google-proxy.js` 中仅 tk 算法部分采用 MIT 许可代码，MIT 为宽松协议，
  与任何其他协议兼容；其余 Worker 编排逻辑为本项目作者原创。
- 若你将本目录代码用于自己的项目，请保留以上全部声明。
