# Cloudflare Worker 代理

本目录包含谷歌翻译和必应翻译的 Cloudflare Worker 代理部署文件及教程。

## 文件说明

| 文件 / 目录 | 用途 |
|------|------|
| `谷歌翻译代理部署说明.md` | 谷歌翻译 Worker 代理详细部署教程 |
| `必应翻译代理部署说明.md` | 必应翻译 Worker 代理详细部署教程 |
| `cf-worker-google-proxy.js` | 谷歌翻译专用代理代码（**tk 签名 + 会话 Cookie 抗限流版**） |
| `_worker.js` | 通用反向代理代码（必应翻译用，衍生自 cf-workers-proxy，**GPL-3.0**） |
| `wrangler.toml` | Wrangler CLI 配置（可选，命令行部署用，必应上游已设为 cn.bing.com） |
| `THIRD_PARTY_NOTICES.md` | **第三方开源声明、来源与许可一览（务必阅读）** |
| `licenses/` | 第三方协议全文：`GPL-3.0.txt`、`MIT-Stichoza.txt` |

## 快速开始

1. 部署谷歌翻译代理 → 查看 `谷歌翻译代理部署说明.md`
2. 部署必应翻译代理 → 查看 `必应翻译代理部署说明.md`
3. 在 MTool 翻译工具 → API设置 → 代理设置 中填入代理地址

## 2026-08 更新内容

- **谷歌代理升级抗限流**：旧版裸转发 `translate.googleapis.com` 极易被 Google 判为机器人返回 429
  （Cloudflare 共享数据中心 IP，成功率仅约 20%）。新版内置 tk 签名算法、自动获取会话 Cookie、
  9 个谷歌域名随机轮换 + 失败整轮重试，**实测成功率约 90%**，且对 MTool 返回格式完全兼容。
- **必应代理修复**：① 修复 POST 请求体转发导致的 500；② 上游域名由 `www.bing.com`
  改为 `cn.bing.com`（前者翻译接口返回空响应）。

## 开源许可与致谢

本目录代码参考 / 衍生自开源项目，已按其协议合规标注，**完整说明见
[`THIRD_PARTY_NOTICES.md`](./THIRD_PARTY_NOTICES.md)**：

- `_worker.js` 衍生自 [jonssonyan/cf-workers-proxy](https://github.com/jonssonyan/cf-workers-proxy)，
  采用 **GPL-3.0**（文件头已保留原作者署名、标注修改内容，协议全文见 `licenses/GPL-3.0.txt`）。
- 谷歌代理的 tk 签名算法参考 MIT 协议的
  [Stichoza/google-translate-php](https://github.com/Stichoza/google-translate-php)
  （Copyright © 2013 Levan Velijanashvili），为独立 JS 实现；抗限流工程思路致谢
  [UniClawAI/google_translate](https://github.com/UniClawAI/google_translate)（该仓库无 LICENSE，仅思路参考）。

> 注意：`_worker.js` 因 GPL-3.0 具有 Copyleft 传染性，再分发或衍生须继续以 GPL-3.0 开源并提供源码。

## 注意

- Cloudflare Worker 免费版每天 10万次请求，翻译工具完全够用
- workers.dev 域名在国内被墙，必须绑定自定义域名
- 谷歌免费接口建议低并发（1–2），新版单 Worker 成功率已约 90%，偶发 429 由 MTool 自动重试
- 必应对 Cloudflare IP 限制较少，推荐作为主力，也是谷歌被限流时的兜底
- 想 100% 不被谷歌限流只能换「干净 IP」（住宅代理 / Vercel、Deno、EdgeOne 等其他平台 / 海外 VPS）
