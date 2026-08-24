// MTool翻译工具 - 谷歌翻译CF Worker代理
// 部署：复制此文件内容到 Cloudflare Workers，保存部署即可
// 用法：把你的Worker URL（如 https://xxx.workers.dev）填到MTool的"谷歌代理池"里

const PROXY_TARGET = "translate.googleapis.com";

export default {
  async fetch(request) {
    const url = new URL(request.url);

    // 根路径返回提示
    if (url.pathname === "/" || url.pathname === "") {
      return new Response(
        "MTool Google Translate Proxy is running. Use /translate_a/single?...",
        { headers: { "Content-Type": "text/plain; charset=utf-8" } }
      );
    }

    // 只允许谷歌翻译路径，防止被滥用
    if (!url.pathname.startsWith("/translate_a/")) {
      return new Response("Forbidden: only /translate_a/ paths allowed", {
        status: 403,
      });
    }

    // 构建目标URL
    const targetUrl = new URL(`https://${PROXY_TARGET}${url.pathname}${url.search}`);

    // 转发请求
    const newRequest = new Request(targetUrl, {
      method: request.method,
      headers: request.headers,
      body: request.body,
      redirect: "follow",
    });

    try {
      const response = await fetch(newRequest);
      // 直接返回响应，不修改内容（谷歌翻译返回JSON，不需要替换域名）
      return new Response(response.body, {
        status: response.status,
        headers: response.headers,
      });
    } catch (error) {
      return new Response(`Proxy error: ${error.message}`, { status: 502 });
    }
  },
};
