import urllib.request, re, time

time.sleep(5)
print('=== 必应代理自定义域名测试 ===')
try:
    url = 'https://bing.wwwee80.dpdns.org/translator'
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    resp = urllib.request.urlopen(req, timeout=30)
    html = resp.read().decode('utf-8', errors='replace')
    ig_m = re.search(r'IG:"([A-F0-9]+)"', html)
    print(f'页面长度: {len(html)}, IG找到: {bool(ig_m)}')
    if ig_m:
        print('成功! 必应代理自定义域名正常工作')
    else:
        print(f'页面前200字: {html[:200]}')
except Exception as e:
    print(f'错误: {e}')
