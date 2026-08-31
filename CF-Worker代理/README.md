# Cloudflare Worker 代理

本目录包含谷歌翻译和必应翻译的 Cloudflare Worker 代理部署文件及教程。

## 文件说明

| 文件 | 用途 |
|------|------|
| `谷歌翻译代理部署说明.md` | 谷歌翻译 Worker 代理详细部署教程 |
| `必应翻译代理部署说明.md` | 必应翻译 Worker 代理详细部署教程 |
| `cf-worker-google-proxy.js` | 谷歌翻译专用代理代码（**tk 签名 + 会话 Cookie 抗限流版**） |
| `_worker.js` | 通用反向代理代码（必应翻译用，已修复 POST 转发，改自 [cf-workers-proxy](https://github.com/jonssonyan/cf-workers-proxy)） |
| `wrangler.toml` | Wrangler CLI 配置（可选，命令行部署用，必应上游已设为 cn.bing.com） |

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

## 注意

- Cloudflare Worker 免费版每天 10万次请求，翻译工具完全够用
- workers.dev 域名在国内被墙，必须绑定自定义域名
- 谷歌免费接口建议低并发（1–2），新版单 Worker 成功率已约 90%，偶发 429 由 MTool 自动重试
- 必应对 Cloudflare IP 限制较少，推荐作为主力，也是谷歌被限流时的兜底
- 想 100% 不被谷歌限流只能换「干净 IP」（住宅代理 / Vercel、Deno、EdgeOne 等其他平台 / 海外 VPS）
