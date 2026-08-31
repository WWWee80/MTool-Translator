/*
 * 通用 Cloudflare Worker 反向代理（用于必应翻译等场景）
 * ---------------------------------------------------------------------------
 * 本文件是 jonssonyan/cf-workers-proxy 的衍生作品（Modified Version）：
 *   原始项目 : https://github.com/jonssonyan/cf-workers-proxy
 *   原始作者 : jonssonyan
 *   开源协议 : GNU General Public License v3.0 (GPL-3.0)
 *   协议全文 : 见同目录 licenses/GPL-3.0.txt
 *
 * 相对上游的修改内容（2026-08，by WWWee80 / MTool-Translator）：
 *   1. 修复 POST/PUT/PATCH 请求体透传导致的 HTTP 500：
 *      改为先 await request.arrayBuffer() 读取后再转发，避免 ReadableStream
 *      与 content-length 冲突；
 *   2. 转发前删除 host / content-length / accept-encoding 等逐跳(hop-by-hop)头；
 *   3. 精简未使用的 UA/IP/Region 黑白名单等可选逻辑，响应统一补 CORS 头；
 *   4. 错误响应改为 JSON，便于上层定位。
 *
 * 按 GPL-3.0 要求：本衍生文件同样以 GPL-3.0 开源，保留原作者版权声明；
 * 分发时须同时提供源码。本程序不提供任何担保（NO WARRANTY）。
 * ---------------------------------------------------------------------------
 */

// 修复点：
// 1. POST/PUT/PATCH 时把请求体读成 ArrayBuffer 再转发，避免 ReadableStream 与 content-length 冲突
// 2. 删除 host / content-length / accept-encoding 等会导致上游异常的逐跳头
// 3. 仅替换 Origin/Referer/Cookie 等头里出现的代理域名，其余原样透传

const HOP_BY_HOP = [
  "host", "content-length", "accept-encoding", "connection",
  "keep-alive", "proxy-authenticate", "proxy-authorization",
  "te", "trailer", "transfer-encoding", "upgrade",
];

function buildRequestHeaders(request, proxyHostname, originHostname) {
  const h = new Headers(request.headers);
  for (const name of HOP_BY_HOP) h.delete(name);
  for (const [key, value] of h) {
    if (value.includes(originHostname)) {
      h.set(key, value.split(originHostname).join(proxyHostname));
    }
  }
  return h;
}

function buildResponseHeaders(originalResponse, proxyHostname, originHostname) {
  const h = new Headers(originalResponse.headers);
  for (const name of ["content-encoding", "transfer-encoding", "content-length"]) {
    h.delete(name);
  }
  for (const [key, value] of h) {
    if (value.includes(proxyHostname)) {
      h.set(key, value.split(proxyHostname).join(originHostname));
    }
  }
  h.set("access-control-allow-origin", "*");
  return h;
}

async function rewriteText(response, proxyHostname, originHostname) {
  let text = await response.text();
  return text.split(proxyHostname).join(originHostname);
}

async function nginx() {
  return `<!DOCTYPE html><html><head><title>Welcome to nginx!</title></head><body><h1>Welcome to nginx!</h1></body></html>`;
}

export default {
  async fetch(request, env) {
    try {
      const {
        PROXY_HOSTNAME,
        PROXY_PROTOCOL = "https",
        PATHNAME_REGEX,
        URL302,
        KEEP_PATH = false,
      } = env;

      const url = new URL(request.url);
      const originHostname = url.hostname;

      if (
        !PROXY_HOSTNAME ||
        (PATHNAME_REGEX && !new RegExp(PATHNAME_REGEX).test(url.pathname))
      ) {
        return URL302
          ? Response.redirect(
              KEEP_PATH
                ? (URL302 + "/" + url.pathname).replace(/\/+/g, "/")
                : URL302,
              302
            )
          : new Response(await nginx(), {
              headers: { "Content-Type": "text/html; charset=utf-8" },
            });
      }

      url.host = PROXY_HOSTNAME;
      url.protocol = PROXY_PROTOCOL;

      const fwdHeaders = buildRequestHeaders(request, PROXY_HOSTNAME, originHostname);

      const init = {
        method: request.method,
        headers: fwdHeaders,
        redirect: "follow",
      };

      // 关键修复：带 body 的方法先把请求体读成 ArrayBuffer，避免流透传导致的 500
      if (!["GET", "HEAD"].includes(request.method.toUpperCase())) {
        const buf = await request.arrayBuffer();
        if (buf.byteLength > 0) init.body = buf;
      }

      const upstream = new Request(url.toString(), init);
      const originalResponse = await fetch(upstream);

      const respHeaders = buildResponseHeaders(
        originalResponse,
        PROXY_HOSTNAME,
        originHostname
      );

      const contentType = respHeaders.get("content-type") || "";
      let body;
      if (contentType.includes("text/") || contentType.includes("json") || contentType.includes("javascript")) {
        body = await rewriteText(originalResponse, PROXY_HOSTNAME, originHostname);
      } else {
        body = originalResponse.body;
      }

      return new Response(body, {
        status: originalResponse.status,
        headers: respHeaders,
      });
    } catch (error) {
      return new Response(
        JSON.stringify({ error: error.message || "Internal Server Error" }),
        { status: 500, headers: { "Content-Type": "application/json; charset=utf-8" } }
      );
    }
  },
};
