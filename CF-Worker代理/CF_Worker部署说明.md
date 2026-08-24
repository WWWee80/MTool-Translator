# Cloudflare Worker 代理 - 已部署

## 已部署的Worker和自定义域名

| 引擎 | Worker名称 | workers.dev地址（大陆被墙） | 自定义域名（推荐） | 状态 |
|------|-----------|---------------------------|------------------|------|
| 谷歌翻译 | gt-proxy | https://gt-proxy.gcl6771308.workers.dev | https://google.wwwee80.dpdns.org | 已部署，Google可能限流(429) |
| 必应翻译 | bing-proxy | https://bing-proxy.gcl6771308.workers.dev | https://bing.wwwee80.dpdns.org | 已部署，证书签发中 |
| DeepLX | deeplx-proxy | https://deeplx-proxy.gcl6771308.workers.dev | https://deeplx.wwwee80.dpdns.org | 已部署(预留) |

## MTool中填写方法

打开 MTool翻译工具 → API设置 → 代理设置：

1. **谷歌翻译代理**: 填入 `https://google.wwwee80.dpdns.org`
   - 如需多个代理轮换，用逗号分隔：`https://google.wwwee80.dpdns.org,https://另一个地址`

2. **必应翻译代理**: 填入 `https://bing.wwwee80.dpdns.org`
   - 必应翻译也支持多个代理地址（逗号分隔），自动轮换

3. **DeepLX代理**: 填入 `https://deeplx.wwwee80.dpdns.org`（预留，待DeepLX引擎集成后使用）

## 注意事项

1. **谷歌翻译限流**: Google会识别Cloudflare IP为数据中心IP，可能返回429限流。建议：
   - 降低谷歌翻译并发数（建议3-5）
   - 使用必应翻译作为替代（必应对Cloudflare IP限制较少）
   - 部署更多Worker地址轮换

2. **必应翻译**: 必应对Cloudflare IP的限制较少，应该可以正常使用。新域名证书签发可能需要几分钟。

3. **免费额度**: Cloudflare Worker免费版每天10万次请求，翻译工具完全够用。

4. **自定义域名**: 已绑定wwwee80.dpdns.org的子域名，大陆可直接访问，不会被墙。

5. **Worker刚部署或域名刚绑定可能需要1-5分钟生效**，如果测试连接失败请稍等再试。

## 如何创建更多Worker（如需更多谷歌代理地址）

1. 登录 https://dash.cloudflare.com
2. 进入 Workers 和 Pages → 创建 Worker
3. 用以下代码替换（谷歌翻译代理）：

```javascript
addEventListener('fetch', event => {
  event.respondWith(handleRequest(event.request));
});
async function handleRequest(request) {
  if (request.method === 'OPTIONS') {
    return new Response(null, { status: 204, headers: {
      'Access-Control-Allow-Origin': '*',
      'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
      'Access-Control-Allow-Headers': '*',
    }});
  }
  const url = new URL(request.url);
  const targetUrl = 'https://translate.googleapis.com' + url.pathname + url.search;
  const modifiedHeaders = new Headers(request.headers);
  modifiedHeaders.set('Host', 'translate.googleapis.com');
  modifiedHeaders.set('Referer', 'https://translate.google.com/');
  modifiedHeaders.delete('Origin');
  const modifiedRequest = new Request(targetUrl, {
    method: request.method, headers: modifiedHeaders, body: request.body,
  });
  try {
    const response = await fetch(modifiedRequest);
    const body = await response.arrayBuffer();
    const corsHeaders = new Headers(response.headers);
    corsHeaders.set('Access-Control-Allow-Origin', '*');
    return new Response(body, { status: response.status, headers: corsHeaders });
  } catch (error) {
    return new Response(JSON.stringify({ error: error.message }), { status: 500 });
  }
}
```

4. 保存部署后，添加DNS记录（CNAME指向新Worker的workers.dev地址）和Worker路由
5. 在MTool中用逗号分隔添加多个地址
