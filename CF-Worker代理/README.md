# Cloudflare Worker 代理

本目录包含谷歌翻译和必应翻译的 Cloudflare Worker 代理部署文件及教程。

## 文件说明

| 文件 | 用途 |
|------|------|
| `谷歌翻译代理部署说明.md` | 谷歌翻译 Worker 代理详细部署教程 |
| `必应翻译代理部署说明.md` | 必应翻译 Worker 代理详细部署教程 |
| `cf-worker-google-proxy.js` | 谷歌翻译专用代理代码 |
| `_worker.js` | 通用反向代理代码（必应翻译用，来自 [cf-workers-proxy](https://github.com/jonssonyan/cf-workers-proxy)） |
| `wrangler.toml` | Wrangler CLI 配置（可选，命令行部署用） |

## 快速开始

1. 部署谷歌翻译代理 → 查看 `谷歌翻译代理部署说明.md`
2. 部署必应翻译代理 → 查看 `必应翻译代理部署说明.md`
3. 在 MTool 翻译工具 → API设置 → 代理设置 中填入代理地址

## 注意

- Cloudflare Worker 免费版每天 10万次请求，翻译工具完全够用
- workers.dev 域名在国内被墙，必须绑定自定义域名
- 谷歌翻译建议部署 3-6 个 Worker 轮换降低限流
- 必应对 Cloudflare IP 限制较少，推荐作为主力
