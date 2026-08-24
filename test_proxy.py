import urllib.request, json, re, urllib.parse

# 测试谷歌代理
print('=== 谷歌翻译代理测试 ===')
try:
    url = 'https://gt-proxy.gcl6771308.workers.dev/translate_a/single?client=gtx&sl=en&tl=zh-CN&dt=t&q=hello'
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    resp = urllib.request.urlopen(req, timeout=15)
    data = resp.read().decode('utf-8')
    if 'Sorry' in data or 'unusual' in data.lower():
        print('失败: Google返回限流页面(Sorry) - Cloudflare IP被Google屏蔽')
    else:
        result = json.loads(data)
        translated = ''.join(seg[0] for seg in result[0] if seg[0])
        print(f'成功! 译文: {translated}')
except Exception as e:
    print(f'错误: {e}')

print()
print('=== 必应翻译代理测试 ===')
try:
    url = 'https://bing-proxy.gcl6771308.workers.dev/translator'
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'})
    resp = urllib.request.urlopen(req, timeout=15)
    html = resp.read().decode('utf-8', errors='replace')
    ig_m = re.search(r'IG:"([A-F0-9]+)"', html)
    iid_m = re.search(r'data-iid="([^"]+)"', html)
    ap_m = re.search(r'params_AbusePreventionHelper\s*=\s*\[\s*(\d+)\s*,\s*"([^"]+)"', html)
    if ig_m and ap_m:
        print(f'成功获取token! IG={ig_m.group(1)[:8]}..., key={ap_m.group(1)[:8]}...')
        ig = ig_m.group(1)
        iid = iid_m.group(1) if iid_m else 'translator.5023'
        key = ap_m.group(1)
        token = ap_m.group(2)
        body = urllib.parse.urlencode({
            'text': 'hello', 'fromLang': 'en', 'to': 'zh-Hans',
            'token': token, 'key': key,
            'tryFetchingGenderDebiasedTranslations': 'true',
        }).encode('utf-8')
        url2 = f'https://bing-proxy.gcl6771308.workers.dev/ttranslatev3?isVertical=1&IG={ig}&IID={iid}&SFX=1'
        req2 = urllib.request.Request(url2, data=body, headers={
            'Content-Type': 'application/x-www-form-urlencoded',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Origin': 'https://cn.bing.com',
            'Referer': 'https://cn.bing.com/translator',
        })
        resp2 = urllib.request.urlopen(req2, timeout=15)
        data2 = json.loads(resp2.read().decode('utf-8'))
        if isinstance(data2, list) and len(data2) > 0:
            translated = data2[0].get('translations', [{}])[0].get('text', '')
            print(f'翻译成功! 译文: {translated}')
        else:
            print(f'翻译返回异常: {str(data2)[:200]}')
    else:
        print(f'未能从页面提取token，页面长度: {len(html)}')
        print(f'页面前300字: {html[:300]}')
except Exception as e:
    print(f'错误: {e}')
