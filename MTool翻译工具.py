#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MTool翻译工具 - 全功能版
支持：本地模型(Ollama)、OpenAI兼容API(硅基流动/Groq/DeepSeek/豆包/百炼/Kimi/智谱)、
      Gemini、谷歌翻译、百度翻译、有道翻译、DeepL、MyMemory、
      微软Azure翻译、腾讯翻译、阿里翻译、IBM Watson
自定义源/目标语言、并发翻译、断点续译、进度显示、暂停继续、
配置保存加载、拖拽文件、指数退避重试、LLM上下文参考、费用预估、详细统计
"""

import json
import re
import urllib.request
import urllib.parse
import urllib3
import ssl
import time
import os
import sys
import datetime
import threading
import random
import tkinter as tk

# ==================== 版本与更新 ====================
VERSION = "1.1.6"
# GitHub 仓库地址（推送到 GitHub 后改成你的 用户名/仓库名）
GITHUB_REPO = "WWWee80/MTool-Translator"
GITHUB_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
from tkinter import ttk, filedialog, messagebox, scrolledtext
from concurrent.futures import ThreadPoolExecutor, as_completed

# ==================== 全局HTTP连接池（连接复用，避免每次TLS握手） ====================
# num_pools: 不同主机的连接池数量；maxsize: 每个池的最大连接数；cert_reqs=CERT_NONE 跳过证书验证
# timeout: connect=5秒快速失败(避免网络抖动拖死并发槽)，read=60秒(大模型生成可能慢)
HTTP_POOL = urllib3.PoolManager(
    num_pools=20, maxsize=64, retries=False, cert_reqs='CERT_NONE',
    timeout=urllib3.Timeout(connect=5.0, read=60.0)
)
# 禁用urllib3的InsecureRequestWarning（因为跳过了证书验证）
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def pool_request(method, url, body=None, headers=None, timeout=60):
    """通过全局连接池发HTTP请求，返回解析后的JSON。连接自动复用。"""
    resp = HTTP_POOL.request(method, url, body=body, headers=headers or {}, timeout=timeout)
    return json.loads(resp.data.decode('utf-8'))


def fetch_siliconflow_prices():
    """从硅基流动官网pricing页面获取模型实时价格，返回 {模型名: 输出价格(元/百万token)}
    改进版：解析Next.js内嵌JSON，支持转义引号，准确率高"""
    try:
        import urllib.request as _req
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        req = _req.Request('https://siliconflow.cn/pricing',
                           headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'})
        resp = _req.urlopen(req, context=ctx, timeout=15)
        html = resp.read().decode('utf-8')
        result = {}
        # 硅基流动pricing页面用Next.js，模型数据以转义JSON内嵌在HTML中
        # 格式: \"modelName\":\"模型名\" ... \"price\":\"价格\"
        # 先找所有modelName的位置
        model_pattern = r'modelName\\?"\s*:\s*\\?"([^"\\]+)\\?"'
        for m in re.finditer(model_pattern, html):
            name = m.group(1)
            # 在当前modelName之后3000字符内找price（同一个模型对象内）
            end = min(m.end() + 3000, len(html))
            segment = html[m.end():end]
            price_match = re.search(r'price\\?"\s*:\s*\\?"([^"\\]*)\\?"', segment)
            if price_match:
                try:
                    price = float(price_match.group(1))
                    result[name] = price
                except (ValueError, TypeError):
                    pass
        return result
    except Exception:
        return {}


def _model_sort_key(model_name):
    """模型排序键：翻译专用模型优先 → 免费优先 → 模型大小(小=快) → 价格低 → 名称"""
    name_lower = model_name.lower()
    # 翻译专用模型排最前面（Hunyuan-MT是专门翻译模型，速度快质量高）
    is_translation_specialist = 0 if ('hunyuan-mt' in name_lower or 'nllb' in name_lower or 'madlad' in name_lower) else 1
    price = MODEL_PRICES.get(model_name, 999)
    is_free = 0 if price == 0 else 1
    # 从模型名提取大小（7B, 14B, 32B, 72B等），小模型速度快排前面
    size_match = re.search(r'(\d+(?:\.\d+)?)\s*[Bb]', model_name)
    size = float(size_match.group(1)) if size_match else 999
    return (is_translation_specialist, is_free, size, price, model_name.lower())


# ==================== 翻译引擎基类 ====================

class TranslatorBase:
    name = "Base"
    need_api_key = False
    need_api_id = False
    need_model = False
    need_base_url = False
    need_email = False

    def __init__(self, api_key="", api_id="", model="", base_url="", email="", timeout=60):
        self.api_key = api_key
        self.api_id = api_id
        self.model = model
        self.base_url = base_url.rstrip('/') if base_url else ""
        self.email = email
        self.timeout = timeout
        self._context_local = threading.local()  # 线程本地存储，避免多线程上下文错乱
        self.thread_min_interval = getattr(type(self), 'thread_min_interval', 0)  # 每线程最小请求间隔（秒），谷歌翻译设0.8防限流
        self.ctx = ssl.create_default_context()
        self.ctx.check_hostname = False
        self.ctx.verify_mode = ssl.CERT_NONE

    @property
    def context(self):
        """线程本地的上下文列表，每个线程独立，避免高并发时上下文错乱"""
        if not hasattr(self._context_local, 'context'):
            self._context_local.context = []
        return self._context_local.context

    @context.setter
    def context(self, value):
        self._context_local.context = value

    def _http_get(self, url, headers=None):
        return self._do_request_with_timeout('GET', url, headers=headers or {})

    def _http_post(self, url, data, headers=None):
        post_data = json.dumps(data).encode('utf-8')
        h = {'Content-Type': 'application/json'}
        if headers:
            h.update(headers)
        return self._do_request_with_timeout('POST', url, body=post_data, headers=h)

    def _do_request_with_timeout(self, method, url, body=None, headers=None):
        """通过全局连接池发HTTP请求（连接自动复用，无额外线程开销）"""
        # 每线程最小请求间隔（防限流）
        if self.thread_min_interval > 0:
            if not hasattr(self._context_local, 'last_req_time'):
                self._context_local.last_req_time = 0
            elapsed = time.time() - self._context_local.last_req_time
            if elapsed < self.thread_min_interval:
                time.sleep(self.thread_min_interval - elapsed)
            self._context_local.last_req_time = time.time()
        # 默认开启gzip压缩（urllib3自动解压），连接超时5秒快速失败，读取超时用self.timeout
        req_headers = dict(headers or {})
        req_headers.setdefault('Accept-Encoding', 'gzip, deflate')
        timeout = urllib3.Timeout(connect=5.0, read=self.timeout)
        resp = HTTP_POOL.request(method, url, body=body, headers=req_headers, timeout=timeout)
        raw = resp.data.decode('utf-8')
        # 检查HTTP状态码，非200时提取错误信息给出友好提示
        if resp.status != 200:
            try:
                err_data = json.loads(raw)
                err_msg = ""
                if isinstance(err_data, dict):
                    if 'error' in err_data:
                        e = err_data['error']
                        if isinstance(e, dict):
                            err_msg = e.get('message') or e.get('code') or str(e)
                        else:
                            err_msg = str(e)
                    elif 'message' in err_data:
                        err_msg = err_data['message']
                    else:
                        err_msg = str(err_data)[:200]
                else:
                    err_msg = str(err_data)[:200]
            except Exception:
                err_msg = raw[:200]
            # 常见状态码友好提示
            status_hint = {
                401: "API Key无效或已过期",
                402: "账户余额不足或欠费，请充值",
                403: "无权限访问该模型或API",
                404: "Base URL或模型名称错误",
                429: "请求过于频繁，已被限流，请降低并发数",
                500: "服务器内部错误，请稍后重试",
                502: "网关错误，请稍后重试",
                503: "服务不可用，请稍后重试",
            }
            hint = status_hint.get(resp.status, f"HTTP {resp.status}")
            raise Exception(f"{hint}（{err_msg}）")
        return json.loads(raw)

    def translate(self, text, source_lang, target_lang):
        raise NotImplementedError

    def translate_batch(self, texts, source_lang, target_lang):
        """批量翻译，返回原始输出字符串。不支持批量的引擎返回None。"""
        return None


# ==================== 传统翻译引擎 ====================

class GoogleTranslator(TranslatorBase):
    name = "谷歌翻译(免费)"
    supports_batch = True
    # 回退模式用的UA轮换
    _USER_AGENTS = [
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0',
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15',
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
    ]

    def __init__(self, api_key="", api_id="", model="", base_url="", email="", timeout=60, proxy_list=None):
        super().__init__(api_key, api_id, model, base_url, email, timeout)
        # proxy_list: ["https://xxx.workers.dev", ...]  空或None=直连（回退模式用）
        self._proxy_list = [p.rstrip('/') for p in (proxy_list or []) if p and p.strip()]
        self._proxy_index = 0
        self._proxy_lock = threading.Lock()
        self._fail_count = {}
        self.thread_min_interval = 0.8  # 谷歌翻译每线程请求间隔0.8秒，防429限流
        # 优先用pygtrans：自动TKK token+反爬，走translate.google.com
        try:
            from pygtrans import Translate
            self._pygtrans_cls = Translate
        except ImportError:
            self._pygtrans_cls = None

    def _pick_proxy(self):
        """智能选择代理：过滤失败>=3的代理，全部失败则重置计数"""
        with self._proxy_lock:
            if not self._proxy_list:
                return None
            # 过滤掉当前失败次数过多的代理（>=3次视为临时不可用）
            available = [p for p in self._proxy_list if self._fail_count.get(p, 0) < 3]
            # 如果所有代理都失败了，重置计数（可能是临时限流，冷却后恢复）
            if not available:
                for p in self._proxy_list:
                    self._fail_count[p] = 0
                available = self._proxy_list[:]
                print("[代理池] 所有代理冷却完成，已重置失败计数")
            # 轮询选择可用代理
            proxy = available[self._proxy_index % len(available)]
            self._proxy_index += 1
            return proxy

    def _rotate_proxy(self):
        with self._proxy_lock:
            if self._proxy_list:
                cur = self._proxy_list[self._proxy_index]
                self._fail_count[cur] = self._fail_count.get(cur, 0) + 1
                self._proxy_index = (self._proxy_index + 1) % len(self._proxy_list)

    def _reset_proxy_fail(self, proxy):
        with self._proxy_lock:
            if proxy is not None:
                self._fail_count[proxy] = 0

    def translate(self, text, source_lang, target_lang):
        # 优先用pygtrans：自动TKK token，内置429重试，安卓客户端UA
        if self._pygtrans_cls is not None:
            try:
                import urllib3
                urllib3.disable_warnings()
                client = self._pygtrans_cls()
                client.session.verify = False  # 部分环境CA证书不全，禁用避免SSL报错
                src = source_lang if source_lang != 'auto' else 'auto'
                result = client.translate(text, source=src, target=target_lang, timeout=self.timeout)
                if result and getattr(result, 'translatedText', None):
                    return result.translatedText
                # pygtrans返回空结果，记录后回退
                if not hasattr(self, '_pygtrans_empty_warned'):
                    self._pygtrans_empty_warned = True
                    print(f"[谷歌翻译] pygtrans返回空结果，回退到直连模式")
            except Exception as e:
                # pygtrans失败，记录后回退（只警告一次，避免刷屏）
                if not hasattr(self, '_pygtrans_fail_warned'):
                    self._pygtrans_fail_warned = True
                    print(f"[谷歌翻译] pygtrans失败({str(e)[:60]})，回退到直连模式")

        # 回退模式：代理池 + 直连 translate.googleapis.com
        import random
        encoded = urllib.parse.quote(text)
        path = (f'/translate_a/single'
                f'?client=gtx&sl={source_lang}&tl={target_lang}&dt=t&q={encoded}')
        max_retry = 3
        last_error = None
        for attempt in range(max_retry):
            proxy = self._pick_proxy()
            headers = {
                'User-Agent': random.choice(self._USER_AGENTS),
                'Accept': '*/*',
                'Accept-Language': 'en-US,en;q=0.9',
            }
            try:
                if proxy:
                    url = proxy + path
                else:
                    url = 'https://translate.googleapis.com' + path
                data = self._http_get(url, headers)
                result = ''.join(seg[0] for seg in data[0] if seg[0])
                self._reset_proxy_fail(proxy)
                return result
            except Exception as e:
                last_error = e
                err_msg = str(e).lower()
                if any(k in err_msg for k in ['429', 'sorry', 'rate', 'limit', 'blocked', 'forbidden', 'too many', '限流']):
                    self._rotate_proxy()
                    wait = (2 ** attempt) * random.uniform(0.5, 1.5)
                    time.sleep(wait)
                    continue
                time.sleep(random.uniform(0.5, 1.0))
        # 抛出异常时标记rate_limit，便于外层_classify_error正确分类
        err_lower = str(last_error).lower() if last_error else ""
        is_rate_limit = any(k in err_lower for k in ['429', 'sorry', 'rate', 'limit', 'blocked', 'forbidden', 'too many', '限流', 'unusual traffic'])
        if is_rate_limit:
            raise Exception(f"谷歌翻译失败 rate_limit（已重试{max_retry}次）: {last_error}")
        raise Exception(f"谷歌翻译失败（已重试{max_retry}次）: {last_error}")

    def translate_batch(self, texts, source_lang, target_lang):
        """批量翻译：pygtrans原生支持列表，返回译文列表；pygtrans不可用时逐条翻译"""
        if self._pygtrans_cls is not None:
            try:
                import urllib3
                urllib3.disable_warnings()
                client = self._pygtrans_cls()
                client.session.verify = False
                src = source_lang if source_lang != 'auto' else 'auto'
                results = client.translate(texts, source=src, target=target_lang, timeout=self.timeout)
                if results and isinstance(results, list):
                    return [r.translatedText for r in results if hasattr(r, 'translatedText')]
            except Exception:
                pass
        # 回退：逐条翻译
        return [self.translate(t, source_lang, target_lang) for t in texts]


class BaiduTranslator(TranslatorBase):
    name = "百度翻译"
    need_api_key = True
    need_api_id = True
    supports_batch = True
    def translate(self, text, source_lang, target_lang):
        import hashlib, random
        salt = str(random.randint(32768, 65536))
        sign = hashlib.md5((self.api_id + text + salt + self.api_key).encode()).hexdigest()
        url = 'https://fanyi-api.baidu.com/api/trans/vip/translate'
        params = urllib.parse.urlencode({
            'q': text, 'from': source_lang, 'to': target_lang,
            'appid': self.api_id, 'salt': salt, 'sign': sign
        }).encode()
        data = self._do_request_with_timeout('POST', url, body=params,
            headers={'Content-Type': 'application/x-www-form-urlencoded'})
        if 'error_code' in data:
            raise Exception(f"百度错误: {data.get('error_msg')}")
        return ''.join(i['dst'] for i in data.get('trans_result', []))

    def translate_batch(self, texts, source_lang, target_lang):
        """批量翻译：用换行分隔多条文本，一次POST，返回译文列表"""
        import hashlib, random
        combined = '\n'.join(texts)
        salt = str(random.randint(32768, 65536))
        sign = hashlib.md5((self.api_id + combined + salt + self.api_key).encode()).hexdigest()
        url = 'https://fanyi-api.baidu.com/api/trans/vip/translate'
        params = urllib.parse.urlencode({
            'q': combined, 'from': source_lang, 'to': target_lang,
            'appid': self.api_id, 'salt': salt, 'sign': sign
        }).encode()
        data = self._do_request_with_timeout('POST', url, body=params,
            headers={'Content-Type': 'application/x-www-form-urlencoded'})
        if 'error_code' in data:
            raise Exception(f"百度错误: {data.get('error_msg')}")
        return [i['dst'] for i in data.get('trans_result', [])]


class YoudaoTranslator(TranslatorBase):
    name = "有道翻译"
    need_api_key = True
    need_api_id = True
    supports_batch = True
    def translate(self, text, source_lang, target_lang):
        import hashlib, random
        salt = str(random.randint(32768, 65536))
        curtime = str(int(time.time()))
        q = text if len(text) <= 20 else text[:10] + str(len(text)) + text[-10:]
        sign = hashlib.sha256((self.api_id + q + salt + curtime + self.api_key).encode()).hexdigest()
        url = 'https://openapi.youdao.com/api'
        params = urllib.parse.urlencode({
            'q': text, 'from': source_lang, 'to': target_lang,
            'appKey': self.api_id, 'salt': salt, 'sign': sign,
            'signType': 'v3', 'curtime': curtime
        }).encode()
        data = self._do_request_with_timeout('POST', url, body=params,
            headers={'Content-Type': 'application/x-www-form-urlencoded'})
        if data.get('errorCode') != '0':
            raise Exception(f"有道错误: {data.get('errorCode')}")
        return ''.join(data.get('translation', []))

    def translate_batch(self, texts, source_lang, target_lang):
        """批量翻译：用换行分隔多条文本，一次POST，返回译文列表"""
        import hashlib, random
        combined = '\n'.join(texts)
        salt = str(random.randint(32768, 65536))
        curtime = str(int(time.time()))
        q = combined if len(combined) <= 20 else combined[:10] + str(len(combined)) + combined[-10:]
        sign = hashlib.sha256((self.api_id + q + salt + curtime + self.api_key).encode()).hexdigest()
        url = 'https://openapi.youdao.com/api'
        params = urllib.parse.urlencode({
            'q': combined, 'from': source_lang, 'to': target_lang,
            'appKey': self.api_id, 'salt': salt, 'sign': sign,
            'signType': 'v3', 'curtime': curtime
        }).encode()
        data = self._do_request_with_timeout('POST', url, body=params,
            headers={'Content-Type': 'application/x-www-form-urlencoded'})
        if data.get('errorCode') != '0':
            raise Exception(f"有道错误: {data.get('errorCode')}")
        return data.get('translation', [])


class DeepLTranslator(TranslatorBase):
    name = "DeepL"
    need_api_key = True
    supports_batch = True
    def translate(self, text, source_lang, target_lang):
        # 自动识别免费版(:fx结尾)还是付费版，忽略用户填的base_url
        if self.api_key.strip().endswith(':fx'):
            base = 'https://api-free.deepl.com'
        else:
            base = 'https://api.deepl.com'
        url = f'{base}/v2/translate'
        tgt = target_lang.upper().replace('ZH-CN', 'ZH').replace('ZH-TW', 'ZH')
        # DeepL不支持AUTO源语言，auto时省略source_lang参数（自动检测）
        params_dict = {'text': text, 'target_lang': tgt}
        if source_lang.lower() != 'auto':
            params_dict['source_lang'] = source_lang.upper()
        params = urllib.parse.urlencode(params_dict).encode()
        data = self._do_request_with_timeout('POST', url, body=params,
            headers={
                'Content-Type': 'application/x-www-form-urlencoded',
                'Authorization': f'DeepL-Auth-Key {self.api_key}'
            })
        return data['translations'][0]['text']

    def translate_batch(self, texts, source_lang, target_lang):
        """批量翻译：一次POST多个text参数，返回译文列表"""
        if self.api_key.strip().endswith(':fx'):
            base = 'https://api-free.deepl.com'
        else:
            base = 'https://api.deepl.com'
        url = f'{base}/v2/translate'
        tgt = target_lang.upper().replace('ZH-CN', 'ZH').replace('ZH-TW', 'ZH')
        params = [('target_lang', tgt)]
        if source_lang.lower() != 'auto':
            params.append(('source_lang', source_lang.upper()))
        for text in texts:
            params.append(('text', text))
        body = urllib.parse.urlencode(params).encode()
        data = self._do_request_with_timeout('POST', url, body=body,
            headers={
                'Content-Type': 'application/x-www-form-urlencoded',
                'Authorization': f'DeepL-Auth-Key {self.api_key}'
            })
        return [t['text'] for t in data.get('translations', [])]


class BingTranslator(TranslatorBase):
    """必应翻译(免费) - 非官方网页API，无需密钥，自动获取token"""
    name = "必应翻译(免费)"
    need_api_key = False
    need_api_id = False
    need_model = False
    need_base_url = False
    need_email = False
    recommended_workers = 4
    default_timeout = 30

    _token_cache = {}  # {proxy_url: (ig, iid, key, token, expiry)}  每个代理独立缓存token
    _page_lock = threading.Lock()
    _req_count = 0  # SFX计数器，每次请求递增
    _proxy_index = 0
    _proxy_lock = threading.Lock()
    _fail_count = {}

    def __init__(self, api_key="", api_id="", model="", base_url="", email="", timeout=60, proxy_url="", proxy_list=None):
        super().__init__(api_key, api_id, model, base_url, email, timeout)
        # proxy_list: ["https://xxx.workers.dev", ...]  空或None=直连
        self._proxy_list = [p.rstrip('/') for p in (proxy_list or []) if p and p.strip()]
        # 兼容单个proxy_url参数
        if proxy_url and not self._proxy_list:
            self._proxy_list = [proxy_url.rstrip('/')]
        self._base_direct = "https://cn.bing.com"

    def _pick_proxy(self):
        """智能选择代理，失败>=3次的跳过，全部失败则重置"""
        with self._proxy_lock:
            if not self._proxy_list:
                return None  # None表示直连
            available = [p for p in self._proxy_list if self._fail_count.get(p, 0) < 3]
            if not available:
                for p in self._proxy_list:
                    self._fail_count[p] = 0
                available = self._proxy_list[:]
                print("[必应代理池] 所有代理冷却完成，已重置失败计数")
            proxy = available[BingTranslator._proxy_index % len(available)]
            BingTranslator._proxy_index += 1
            return proxy

    def _rotate_proxy(self, proxy):
        """标记代理失败并轮换"""
        if proxy:
            with self._proxy_lock:
                self._fail_count[proxy] = self._fail_count.get(proxy, 0) + 1

    def _get_base(self, proxy):
        """获取请求基地址：代理地址或直连"""
        return proxy if proxy else self._base_direct

    def _fetch_token(self, proxy=None):
        """从必应翻译页面获取IG、IID和防滥用token，每个代理独立缓存"""
        import time as _time
        base = self._get_base(proxy)
        cache_key = proxy or 'direct'
        now = _time.time()
        cached = BingTranslator._token_cache.get(cache_key)
        if cached and now < cached[4]:
            return cached[:4]
        with self._page_lock:
            cached = BingTranslator._token_cache.get(cache_key)
            if cached and _time.time() < cached[4]:
                return cached[:4]
            try:
                req = urllib.request.Request(f"{base}/translator", headers={
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                    'Accept-Language': 'zh-CN,zh;q=0.9',
                })
                resp = urllib.request.urlopen(req, timeout=15)
                html = resp.read().decode('utf-8', errors='replace')
                ig_m = re.search(r'IG:"([A-F0-9]+)"', html)
                ig = ig_m.group(1) if ig_m else ""
                iid_m = re.search(r'data-iid="([^"]+)"', html)
                if iid_m:
                    iid = iid_m.group(1)
                else:
                    iid_m2 = re.search(r'IID:"([^"]+)"', html)
                    iid = iid_m2.group(1) if iid_m2 else "translator.5023"
                ap_m = re.search(r'params_AbusePreventionHelper\s*=\s*\[\s*(\d+)\s*,\s*"([^"]+)"', html)
                key = ap_m.group(1) if ap_m else ""
                token = ap_m.group(2) if ap_m else ""
                if not ig or not token:
                    raise Exception("无法从必应页面获取token")
                expiry = _time.time() + 3000
                BingTranslator._token_cache[cache_key] = (ig, iid, key, token, expiry)
                return (ig, iid, key, token)
            except Exception as e:
                raise Exception(f"获取必应token失败: {e}")

    def _lang_code(self, lang):
        """转换语言代码：zh-CN->zh-Hans, zh-TW->zh-Hant, auto->auto-detect"""
        lang = lang.lower()
        if lang == 'auto':
            return 'auto-detect'
        if lang in ('zh-cn', 'zh'):
            return 'zh-Hans'
        if lang == 'zh-tw':
            return 'zh-Hant'
        return lang

    def translate(self, text, source_lang, target_lang):
        proxy = self._pick_proxy()
        base = self._get_base(proxy)
        ig, iid, key, token = self._fetch_token(proxy)
        src = self._lang_code(source_lang)
        tgt = self._lang_code(target_lang)
        # SFX计数器递增（必应翻译要求每次请求递增）
        BingTranslator._req_count += 1
        sfx = BingTranslator._req_count
        # 必应翻译参数名是 fromLang（不是from），token和key放body
        url = f"{base}/ttranslatev3?isVertical=1&IG={ig}&IID={iid}&SFX={sfx}"
        body_dict = {
            'text': text, 'fromLang': src, 'to': tgt,
            'token': token, 'key': key,
            'tryFetchingGenderDebiasedTranslations': 'true',
        }
        body = urllib.parse.urlencode(body_dict).encode('utf-8')
        headers = {
            'Content-Type': 'application/x-www-form-urlencoded',
            'Origin': 'https://cn.bing.com',
            'Referer': 'https://cn.bing.com/translator',
            'Accept': '*/*',
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36 Edg/151.0.4129.59',
        }
        try:
            data = self._do_request_with_timeout('POST', url, body=body, headers=headers)
        except Exception as e:
            # 请求失败，标记代理失败
            self._rotate_proxy(proxy)
            raise
        if isinstance(data, dict):
            # 验证码或限流
            if data.get('ShowCaptcha'):
                self._rotate_proxy(proxy)
                raise Exception("必应翻译要求验证码，请降低并发或稍后再试")
            sc = data.get('statusCode')
            if sc == 401:
                self._rotate_proxy(proxy)
                raise Exception("必应翻译超限(401)，请稍后再试")
            if sc and sc != 200:
                # token可能过期，清除该代理缓存重试一次
                cache_key = proxy or 'direct'
                if cache_key in BingTranslator._token_cache:
                    del BingTranslator._token_cache[cache_key]
                try:
                    ig, iid, key, token = self._fetch_token(proxy)
                    BingTranslator._req_count += 1
                    url = f"{base}/ttranslatev3?isVertical=1&IG={ig}&IID={iid}&SFX={BingTranslator._req_count}"
                    body = urllib.parse.urlencode(body_dict).encode('utf-8')
                    data = self._do_request_with_timeout('POST', url, body=body, headers=headers)
                except Exception:
                    self._rotate_proxy(proxy)
                    raise
                if isinstance(data, dict) and data.get('statusCode') and data['statusCode'] != 200:
                    self._rotate_proxy(proxy)
                    raise Exception(f"必应翻译错误 statusCode={data.get('statusCode')}: {data.get('errorMessage', '')}")
        if isinstance(data, list) and len(data) > 0:
            # 成功，重置该代理失败计数
            if proxy:
                with self._proxy_lock:
                    self._fail_count[proxy] = 0
            return data[0].get('translations', [{}])[0].get('text', '')
        raise Exception(f"必应翻译返回异常: {str(data)[:200]}")


class MyMemoryTranslator(TranslatorBase):
    name = "MyMemory(免费)"
    need_email = True

    def _detect_lang(self, text):
        """简单语言检测：MyMemory不支持auto，根据字符范围推断源语言"""
        # 日文：平假名或片假名
        if re.search(r'[぀-ゟ゠-ヿ]', text):
            return 'ja'
        # 韩文：Hangul
        if re.search(r'[가-힯ᄀ-ᇿ]', text):
            return 'ko'
        # 中文：CJK统一汉字（且无日文假名）
        if re.search(r'[一-鿿]', text):
            return 'zh-CN'
        # 泰文
        if re.search(r'[ก-๙]', text):
            return 'th'
        # 阿拉伯文
        if re.search(r'[؀-ۿ]', text):
            return 'ar'
        # 俄文/西里尔
        if re.search(r'[Ѐ-ӿ]', text):
            return 'ru'
        # 默认英文
        return 'en'

    def translate(self, text, source_lang, target_lang):
        # MyMemory不支持auto，自动检测源语言
        src = source_lang
        if src.lower() == 'auto':
            src = self._detect_lang(text)
        encoded = urllib.parse.quote(text)
        url = f'https://api.mymemory.translated.net/get?q={encoded}&langpair={src}|{target_lang}'
        if self.email:
            url += f'&de={urllib.parse.quote(self.email)}'
        data = self._http_get(url, {'User-Agent': 'Mozilla/5.0'})
        if data.get('responseStatus') != 200:
            raise Exception(f"MyMemory错误: {data.get('responseDetails')}")
        return data['responseData']['translatedText']


class AzureTranslator(TranslatorBase):
    """微软Azure翻译 - api_id填区域(如eastus)，api_key填订阅密钥，base_url可留空用全球端点"""
    name = "微软翻译(Azure)"
    need_api_key = True
    need_api_id = True
    need_base_url = False
    supports_batch = True
    def translate(self, text, source_lang, target_lang):
        import uuid
        endpoint = self.base_url or "https://api.cognitive.microsofttranslator.com"
        region = self.api_id
        url = f"{endpoint}/translate?api-version=3.0&from={source_lang}&to={target_lang}"
        headers = {
            'Ocp-Apim-Subscription-Key': self.api_key,
            'Content-type': 'application/json',
            'X-ClientTraceId': str(uuid.uuid4()),
        }
        if region:
            headers['Ocp-Apim-Subscription-Region'] = region
        body = [{'text': text}]
        data = self._http_post(url, body, headers)
        if isinstance(data, dict) and 'error' in data:
            raise Exception(f"Azure错误: {data['error'].get('message', str(data))}")
        return data[0]['translations'][0]['text']

    def translate_batch(self, texts, source_lang, target_lang):
        """批量翻译：body传多个对象，一次POST，返回译文列表"""
        import uuid
        endpoint = self.base_url or "https://api.cognitive.microsofttranslator.com"
        region = self.api_id
        url = f"{endpoint}/translate?api-version=3.0&from={source_lang}&to={target_lang}"
        headers = {
            'Ocp-Apim-Subscription-Key': self.api_key,
            'Content-type': 'application/json',
            'X-ClientTraceId': str(uuid.uuid4()),
        }
        if region:
            headers['Ocp-Apim-Subscription-Region'] = region
        body = [{'text': t} for t in texts]
        data = self._http_post(url, body, headers)
        if isinstance(data, dict) and 'error' in data:
            raise Exception(f"Azure错误: {data['error'].get('message', str(data))}")
        return [d['translations'][0]['text'] for d in data]


class TencentTranslator(TranslatorBase):
    """腾讯翻译君 - api_id=SecretId, api_key=SecretKey"""
    name = "腾讯翻译"
    need_api_key = True
    need_api_id = True
    def translate(self, text, source_lang, target_lang):
        import hmac, hashlib
        secret_id = self.api_id
        secret_key = self.api_key
        service = "tmt"
        host = "tmt.tencentcloudapi.com"
        endpoint = "https://" + host
        action = "TextTranslate"
        version = "2018-03-21"

        payload = json.dumps({
            "SourceText": text, "Source": source_lang,
            "Target": target_lang, "ProjectId": 0
        })
        timestamp = int(time.time())
        date = datetime.datetime.utcfromtimestamp(timestamp).strftime("%Y-%m-%d")

        canonical_headers = f"content-type:application/json\nhost:{host}\nx-tc-action:{action.lower()}\n"
        signed_headers = "content-type;host;x-tc-action"
        hashed_payload = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        canonical_request = f"POST\n/\n\n{canonical_headers}\n{signed_headers}\n{hashed_payload}"

        credential_scope = f"{date}/{service}/tc3_request"
        hashed_canonical = hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()
        string_to_sign = f"TC3-HMAC-SHA256\n{timestamp}\n{credential_scope}\n{hashed_canonical}"

        def _sign(key, msg):
            return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()
        secret_date = _sign(("TC3" + secret_key).encode("utf-8"), date)
        secret_service = _sign(secret_date, service)
        secret_signing = _sign(secret_service, "tc3_request")
        signature = hmac.new(secret_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

        authorization = f"TC3-HMAC-SHA256 Credential={secret_id}/{credential_scope}, SignedHeaders={signed_headers}, Signature={signature}"
        headers = {
            "Authorization": authorization, "Content-Type": "application/json",
            "Host": host, "X-TC-Action": action,
            "X-TC-Timestamp": str(timestamp), "X-TC-Version": version,
        }
        data = self._http_post(endpoint + "/", json.loads(payload), headers)
        resp = data.get("Response", {})
        if "Error" in resp:
            raise Exception(f"腾讯错误: {resp['Error'].get('Message', str(resp))}")
        return resp["TargetText"]


class AlibabaTranslator(TranslatorBase):
    """阿里翻译 - api_id=AccessKey ID, api_key=AccessKey Secret"""
    name = "阿里翻译"
    need_api_key = True
    need_api_id = True
    def translate(self, text, source_lang, target_lang):
        import hmac, hashlib, base64, uuid
        access_key_id = self.api_id
        access_key_secret = self.api_key
        params = {
            "Action": "TranslateGeneral", "Version": "2018-10-12",
            "Format": "JSON", "AccessKeyId": access_key_id,
            "SignatureMethod": "HMAC-SHA1", "SignatureVersion": "1.0",
            "SignatureNonce": str(uuid.uuid4()),
            "Timestamp": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "FormatType": "text", "SourceLanguage": source_lang,
            "TargetLanguage": target_lang, "SourceText": text, "Scene": "general",
        }
        sorted_params = sorted(params.items())
        canonicalized = urllib.parse.urlencode(sorted_params)
        string_to_sign = f"GET&%2F&{urllib.parse.quote(canonicalized, safe='')}"
        signature = base64.b64encode(
            hmac.new((access_key_secret + "&").encode(), string_to_sign.encode(), hashlib.sha1).digest()
        ).decode()
        params["Signature"] = signature
        url = "https://mt.aliyuncs.com/?" + urllib.parse.urlencode(params)
        data = self._http_get(url)
        if str(data.get("Code", "200")) != "200":
            raise Exception(f"阿里错误: {data.get('Message', str(data))}")
        return data["Data"]["Translated"]


class IBMWatsonTranslator(TranslatorBase):
    """IBM Watson语言翻译 - api_key=IAM API Key, base_url=服务端点(如https://api.us-south.language-translator.watson.cloud.ibm.com)"""
    name = "IBM Watson"
    need_api_key = True
    need_base_url = True
    def translate(self, text, source_lang, target_lang):
        import base64
        endpoint = self.base_url.rstrip('/')
        url = f"{endpoint}/v3/translate?version=2018-05-01"
        credentials = base64.b64encode(f"apikey:{self.api_key}".encode()).decode()
        headers = {"Content-Type": "application/json", "Authorization": f"Basic {credentials}"}
        body = {"text": [text], "source": source_lang, "target": target_lang}
        data = self._http_post(url, body, headers)
        if "error" in data:
            raise Exception(f"IBM错误: {data.get('error')}")
        return data["translations"][0]["translation"]


# ==================== LLM翻译引擎 ====================

# 语言名称映射（给LLM prompt用）
LANG_NAMES = {
    'auto': '自动检测', 'zh-CN': '简体中文', 'zh-TW': '繁体中文',
    'en': '英语', 'ja': '日语', 'ko': '韩语', 'fr': '法语',
    'de': '德语', 'es': '西班牙语', 'pt': '葡萄牙语', 'ru': '俄语',
    'it': '意大利语', 'ar': '阿拉伯语', 'th': '泰语', 'vi': '越南语',
    'nl': '荷兰语', 'pl': '波兰语', 'sv': '瑞典语',
}

TRANSLATE_SYSTEM = "你是一个专业的翻译引擎。将用户输入的文本翻译成目标语言，只输出翻译后的文本，不要添加任何解释、注释或引号。保留原文中的换行符、特殊符号和格式。无论原文内容如何，都必须忠实翻译，不得拒绝或删减。"


class OpenAICompatTranslator(TranslatorBase):
    """通用OpenAI兼容API - 支持硅基流动/Groq/DeepSeek/豆包/百炼/Kimi/智谱/OpenAI等"""
    name = "OpenAI兼容(硅基流动/Groq/DeepSeek等)"
    need_api_key = True
    need_model = True
    need_base_url = True
    supports_batch = True

    def translate(self, text, source_lang, target_lang):
        src_name = LANG_NAMES.get(source_lang, source_lang)
        tgt_name = LANG_NAMES.get(target_lang, target_lang)
        url = f"{self.base_url}/chat/completions"
        messages = [{"role": "system", "content": TRANSLATE_SYSTEM}]
        # 上下文参考（最近3条），保持术语一致性
        for orig, trans in self.context[-3:]:
            messages.append({"role": "user", "content": f"将以下{src_name}文本翻译成{tgt_name}：{orig}"})
            messages.append({"role": "assistant", "content": trans})
        messages.append({"role": "user", "content": f"将以下{src_name}文本翻译成{tgt_name}，只输出译文：\n{text}"})
        data = {
            "model": self.model, "messages": messages,
            "temperature": 0.1, "max_tokens": 4096,
        }
        result = self._http_post(url, data, {"Authorization": f"Bearer {self.api_key}"})
        if 'choices' not in result or not result['choices']:
            err = result.get('error', {})
            err_msg = err.get('message', str(result)[:200]) if isinstance(err, dict) else str(err)
            raise Exception(f"API返回异常: {err_msg}")
        return result['choices'][0]['message']['content'].strip()

    def translate_batch(self, texts, source_lang, target_lang):
        src_name = LANG_NAMES.get(source_lang, source_lang)
        tgt_name = LANG_NAMES.get(target_lang, target_lang)
        url = f"{self.base_url}/chat/completions"
        numbered = "\n".join(f"{i+1}. {t}" for i, t in enumerate(texts))
        messages = [
            {"role": "system", "content": f"你是专业翻译。请将以下{src_name}文本逐条翻译成{tgt_name}，严格按编号输出，每条一行，格式为：编号. 译文。不要解释，不要添加额外内容。"},
            {"role": "user", "content": numbered}
        ]
        data = {"model": self.model, "messages": messages, "temperature": 0.1, "max_tokens": 4096}
        result = self._http_post(url, data, {"Authorization": f"Bearer {self.api_key}"})
        if 'choices' not in result or not result['choices']:
            err = result.get('error', {})
            err_msg = err.get('message', str(result)[:200]) if isinstance(err, dict) else str(err)
            raise Exception(f"API返回异常: {err_msg}")
        return result['choices'][0]['message']['content'].strip()


class GeminiTranslator(TranslatorBase):
    """Google Gemini API"""
    name = "Google Gemini"
    need_api_key = True
    need_model = True
    supports_batch = True

    def translate(self, text, source_lang, target_lang):
        src_name = LANG_NAMES.get(source_lang, source_lang)
        tgt_name = LANG_NAMES.get(target_lang, target_lang)
        model = self.model or "gemini-1.5-flash"
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={self.api_key}"
        context_text = ""
        for orig, trans in self.context[-3:]:
            context_text += f"原文: {orig}\n译文: {trans}\n\n"
        full_text = (
            f"{TRANSLATE_SYSTEM}\n"
            f"以下是之前的翻译参考（保持术语一致）：\n{context_text}"
            f"将以下{src_name}文本翻译成{tgt_name}，只输出译文：\n{text}"
        )
        data = {
            "contents": [{"parts": [{"text": full_text}]}],
            "generationConfig": {"temperature": 0.1, "maxOutputTokens": 4096},
        }
        result = self._http_post(url, data)
        return result['candidates'][0]['content']['parts'][0]['text'].strip()

    def translate_batch(self, texts, source_lang, target_lang):
        src_name = LANG_NAMES.get(source_lang, source_lang)
        tgt_name = LANG_NAMES.get(target_lang, target_lang)
        model = self.model or "gemini-1.5-flash"
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={self.api_key}"
        numbered = "\n".join(f"{i+1}. {t}" for i, t in enumerate(texts))
        full_text = f"你是专业翻译。请将以下{src_name}文本逐条翻译成{tgt_name}，严格按编号输出，每条一行，格式为：编号. 译文。不要解释，不要添加额外内容。\n\n{numbered}"
        data = {
            "contents": [{"parts": [{"text": full_text}]}],
            "generationConfig": {"temperature": 0.1, "maxOutputTokens": 4096},
        }
        result = self._http_post(url, data)
        return result['candidates'][0]['content']['parts'][0]['text'].strip()


class OllamaTranslator(TranslatorBase):
    """本地Ollama模型"""
    name = "Ollama本地模型"
    need_model = True
    need_base_url = True
    supports_batch = True
    recommended_workers = 2  # 10GB显存跑5GB模型有余量，可试2并发
    default_timeout = 300  # 本地模型处理慢，超时设为300秒
    thread_min_interval = 0.3  # 本地模型请求间隔0.3秒，避免把Ollama跑崩

    def __init__(self, api_key="", api_id="", model="", base_url="", email="", timeout=300, proxy_list=None):
        super().__init__(api_key, api_id, model, base_url, email, timeout)
        self.proxy_list = proxy_list  # Ollama本地模型不需要代理，保留参数兼容
        self._warmed_up = False

    def warmup(self):
        """预热模型：发一个极简请求把模型加载到内存，避免第一条翻译因加载超时"""
        if self._warmed_up:
            return
        base = self.base_url or "http://localhost:11434"
        url = f"{base}/api/chat"
        data = {
            "model": self.model,
            "messages": [{"role": "user", "content": "ok"}],
            "stream": False,
            "keep_alive": "-1s",
            "options": {"num_predict": 1, "temperature": 0.1},
        }
        self._http_post(url, data)
        self._warmed_up = True

    def translate(self, text, source_lang, target_lang):
        src_name = LANG_NAMES.get(source_lang, source_lang)
        if src_name == 'auto':
            src_name = '原文'  # auto时不指定源语言，让模型自动检测
        tgt_name = LANG_NAMES.get(target_lang, target_lang)
        base = self.base_url or "http://localhost:11434"
        url = f"{base}/api/chat"
        # 长文本保护：超过4000字符的文本自动截断（利用16K上下文，大幅减少截断）
        text_original = text
        if len(text) > 4000:
            text = text[:4000]
        # 完全不用上下文（避免上下文累积导致Ollama崩溃）
        messages = [
            {"role": "system", "content": TRANSLATE_SYSTEM},
            {"role": "user", "content": f"将以下{src_name}文本翻译成{tgt_name}，只输出译文：\n{text}"}
        ]
        data = {
            "model": self.model, "messages": messages,
            "stream": False,
            "keep_alive": "-1s",  # 防止模型空闲自动卸载（默认5分钟）
            "options": {
                "temperature": 0.1,
                "num_ctx": 16384,  # 利用40K上下文，提到16K减少截断
                "num_predict": 2048,  # 最大输出token
            },
        }
        try:
            result = self._http_post(url, data)
            if 'message' not in result or 'content' not in result['message']:
                raise Exception(f"Ollama返回格式异常: {str(result)[:200]}")
            translated = result['message']['content'].strip()
            # 如果原文被截断了，在译文末尾标注（提醒用户这条被截断了）
            if len(text_original) > 1500:
                return translated + f"...[原文过长已截断，原长{len(text_original)}字]"
            return translated
        except Exception as e:
            err_msg = str(e)
            # 连接失败或Ollama崩溃时，给出明确提示
            if 'connection' in err_msg.lower() or 'refused' in err_msg.lower() or '111' in err_msg or not err_msg:
                raise Exception(f"Ollama服务未响应（可能已崩溃），请重启ollama serve。原错误：{err_msg or type(e).__name__}")
            # OOM或上下文过长时，用更小的num_ctx重试
            if 'out of memory' in err_msg.lower() or 'context length' in err_msg.lower() or 'too long' in err_msg.lower() or '4096' in err_msg:
                # 先用更短的文本重试
                text_short = text[:2000] if len(text) > 2000 else text
                messages_simple = [
                    {"role": "system", "content": TRANSLATE_SYSTEM},
                    {"role": "user", "content": f"将以下{src_name}文本翻译成{tgt_name}，只输出译文：\n{text_short}"}
                ]
                data_simple = {
                    "model": self.model, "messages": messages_simple,
                    "stream": False,
                    "keep_alive": "-1s",
                    "options": {"temperature": 0.1, "num_ctx": 2048, "num_predict": 1024},
                }
                try:
                    result = self._http_post(url, data_simple)
                    if 'message' in result and 'content' in result['message']:
                        translated = result['message']['content'].strip()
                        if len(text_original) > 800:
                            return translated + f"...[原文过长已截断，原长{len(text_original)}字]"
                        return translated
                except Exception:
                    pass
                raise Exception(f"Ollama OOM或上下文过长（原文{len(text_original)}字），请换更小的模型或减少文本长度。原错误：{err_msg}")
            raise

    def translate_batch(self, texts, source_lang, target_lang):
        src_name = LANG_NAMES.get(source_lang, source_lang)
        if src_name == 'auto':
            src_name = '原文'
        tgt_name = LANG_NAMES.get(target_lang, target_lang)
        base = self.base_url or "http://localhost:11434"
        url = f"{base}/api/chat"
        numbered = "\n".join(f"{i+1}. {t}" for i, t in enumerate(texts))
        messages = [
            {"role": "system", "content": f"你是专业翻译。请将以下{src_name}文本逐条翻译成{tgt_name}，严格按编号输出，每条一行，格式为：编号. 译文。不要解释，不要添加额外内容。"},
            {"role": "user", "content": numbered}
        ]
        data = {
            "model": self.model, "messages": messages, "stream": False,
            "keep_alive": "-1s",
            "options": {"temperature": 0.1, "num_ctx": 16384, "num_predict": 2048},
        }
        result = self._http_post(url, data)
        return result['message']['content'].strip()

PRESETS = {
    "硅基流动": {
        "base_url": "https://api.siliconflow.cn/v1",
        "model": "tencent/Hunyuan-MT-7B",
        "hint": "在 https://cloud.siliconflow.cn 注册获取API Key。默认免费模型 tencent/Hunyuan-MT-7B(翻译专用)；付费推荐 Qwen3.5-35B-A3B(性价比最高) 或 DeepSeek-V4-Flash(审核宽松)；点获取模型查看全部",
        "recommended_workers": 4,
    },
    "Groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "model": "llama-3.3-70b-versatile",
        "hint": "在 https://console.groq.com 注册，免费、速度极快",
        "recommended_workers": 8,
    },
    "DeepSeek": {
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "hint": "在 https://platform.deepseek.com 注册，新用户送500万token",
        "recommended_workers": 4,
    },
    "字节豆包": {
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "model": "doubao-1-5-lite-32k-250115",
        "hint": "在 https://console.volcengine.com/ark 注册，有免费额度",
        "recommended_workers": 4,
    },
    "阿里云百炼": {
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-turbo",
        "hint": "在 https://bailian.console.aliyun.com 注册，新用户送100万token",
        "recommended_workers": 4,
    },
    "月之暗面Kimi": {
        "base_url": "https://api.moonshot.cn/v1",
        "model": "moonshot-v1-8k",
        "hint": "在 https://platform.moonshot.cn 注册",
        "recommended_workers": 4,
    },
    "智谱AI": {
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-4-flash",
        "hint": "在 https://open.bigmodel.cn 注册，glm-4-flash免费",
        "recommended_workers": 4,
    },
    "OpenAI官方": {
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "hint": "在 https://platform.openai.com 注册，需科学上网",
        "recommended_workers": 4,
    },
    "Ollama本地": {
        "base_url": "http://localhost:11434",
        "model": "richardyoung/qwen3-8b-abliterated:Q4_K_M",
        "hint": "Qwen3-8B去审查版，40K上下文，成人游戏文本不拒。安装：ollama pull richardyoung/qwen3-8b-abliterated:Q4_K_M",
        "recommended_workers": 2,
    },
}

# 费用预估费率（每百万字符，人民币；免费额度内不计费）
COST_RATES = {
    "谷歌翻译(免费)": {"type": "free", "note": "完全免费"},
    "MyMemory(免费)": {"type": "per_char", "free_chars": 50000, "per_million": 0, "note": "匿名5千/天，填邮箱5万/天"},
    "百度翻译": {"type": "per_char", "free_chars": 50000, "per_million": 49, "note": "标准版5万字符/月免费"},
    "有道翻译": {"type": "per_char", "free_chars": 0, "per_million": 48, "note": "新用户有免费额度"},
    "DeepL": {"type": "per_char", "free_chars": 500000, "per_million": 0, "note": "免费版50万字符/月"},
    "微软翻译(Azure)": {"type": "per_char", "free_chars": 2000000, "per_million": 70, "note": "免费层200万字符/月"},
    "腾讯翻译": {"type": "per_char", "free_chars": 0, "per_million": 50, "note": "按字符计费"},
    "阿里翻译": {"type": "per_char", "free_chars": 0, "per_million": 50, "note": "按字符计费"},
    "IBM Watson": {"type": "per_char", "free_chars": 1000000, "per_million": 140, "note": "Lite版100万字符/月"},
    "OpenAI兼容(硅基流动/Groq/DeepSeek等)": {"type": "per_token", "note": "LLM按token计费，取决于具体模型"},
    "Google Gemini": {"type": "per_token", "note": "免费层有限速，超出按token计费"},
    "Ollama本地模型": {"type": "free", "note": "完全免费离线"},
}

# 常见模型每百万token价格（人民币，输入+输出综合估算，取输出价为主）
# 价格为近似值，实际以官方为准；0表示免费
MODEL_PRICES = {
    # ===== 硅基流动免费模型 =====
    "tencent/Hunyuan-MT-7B": 0.0,        # 腾讯翻译专用模型，免费，33语种
    "THUDM/GLM-Z1-9B-0414": 0.0,         # 智谱9B，免费
    # ===== 硅基流动 Qwen系列 =====
    "Qwen/Qwen3.6-35B-A3B": 10.8,        # 输入1.8 输出10.8
    "Qwen/Qwen3.6-27B": 18.0,             # 输入3.0 输出18.0
    "Qwen/Qwen3.5-397B-A17B": 7.2,       # 输入1.2 输出7.2 (128k内)
    "Qwen/Qwen3.5-122B-A10B": 6.4,       # 输入0.8 输出6.4 (128k内)
    "Qwen/Qwen3.5-35B-A3B": 3.2,          # 输入0.4 输出3.2 (128k内) ← 性价比最高
    "Qwen/Qwen3.5-27B": 4.8,              # 输入0.6 输出4.8 (128k内)
    "Qwen/Qwen2.5-7B-Instruct": 0.0,      # 免费
    "Qwen/Qwen2.5-14B-Instruct": 0.7,      # 0.7元/百万token（输出）
    "Qwen/Qwen2.5-72B-Instruct": 4.13,     # 4.13元/百万token（输出）
    "Qwen/Qwen2.5-32B-Instruct": 1.26,     # 1.26元/百万token（输出）
    # ===== 硅基流动 DeepSeek系列 =====
    "deepseek-ai/DeepSeek-V4-Flash": 2.0,  # 输入1.0 输出2.0 ← 速度快审核松
    "deepseek-ai/DeepSeek-V4-Pro": 24.0,    # 输入12 输出24 ← 顶级质量
    "deepseek-ai/DeepSeek-V3.2": 6.0,       # 输入4.0 输出6.0
    "deepseek-ai/DeepSeek-V3.1-Terminus": 12.0,
    "deepseek-ai/DeepSeek-V3": 8.0,          # 8.0元/百万token（输出）
    "deepseek-ai/DeepSeek-R1": 16.0,         # 16.0元/百万token（输出）
    # ===== 硅基流动 其他厂商 =====
    "zai-org/GLM-5.2": 28.0,                 # 输入8 输出28
    "zai-org/GLM-4.5-Air": 6.0,              # 输入1 输出6
    "zai-org/GLM-4-32B-0414": 1.89,
    "stepfun-ai/Step-3.5-Flash": 2.1,        # 输入0.7 输出2.1 ← 便宜
    "inclusionAI/Ling-mini-2.0": 2.0,        # 输入0.5 输出2.0 ← 最便宜付费
    "inclusionAI/Ling-flash-2.0": 4.0,
    "ByteDance-Seed/Seed-OSS-36B-Instruct": 4.0,
    "MiniMaxAI/MiniMax-M2.5": 8.4,
    "nex-agi/Nex-N2-Pro": 7.0,
    "moonshotai/Kimi-K2.7-Code": 27.0,
    # ===== DeepSeek官方 =====
    "deepseek-chat": 2.0,
    "deepseek-reasoner": 4.0,
    # ===== 豆包 =====
    "doubao-1-5-lite-32k-250115": 0.3,
    "doubao-1-5-pro-32k-250115": 2.0,
    # ===== 百炼 =====
    "qwen-turbo": 0.3,
    "qwen-plus": 0.8,
    "qwen-max": 2.4,
    # ===== Kimi =====
    "moonshot-v1-8k": 12.0,
    "moonshot-v1-32k": 12.0,
    # ===== 智谱 =====
    "glm-4-flash": 0.0,
    "glm-4-air": 0.1,
    # ===== OpenAI =====
    "gpt-4o-mini": 1.0,
    "gpt-4o": 14.0,
    "gpt-3.5-turbo": 0.5,
    # ===== Groq =====
    "llama-3.3-70b-versatile": 0.0,
    "llama-3.1-8b-instant": 0.0,
    # ===== Gemini =====
    "gemini-1.5-flash": 0.0,
    "gemini-1.5-pro": 5.0,
    # ===== Ollama =====
    "qwen2.5:7b": 0.0,
    "qwen2.5:14b": 0.0,
    "qwen2.5:32b": 0.0,
}

# 所有引擎列表
TRANSLATORS = [
    GoogleTranslator, MyMemoryTranslator,
    BaiduTranslator, YoudaoTranslator, DeepLTranslator, BingTranslator,
    AzureTranslator, TencentTranslator, AlibabaTranslator, IBMWatsonTranslator,
    OpenAICompatTranslator, GeminiTranslator, OllamaTranslator,
]

# 各引擎建议并发数（选择引擎时自动填入，新手友好）
ENGINE_RECOMMENDED_WORKERS = {
    "谷歌翻译(免费)": 3,       # 非官方接口，高并发易429，无代理建议1-2，有代理池可3-4
    "MyMemory(免费)": 8,        # 免费限额5万/天
    "百度翻译": 8,               # 标准版QPS限制
    "有道翻译": 8,
    "DeepL": 8,                  # 官方API，按字符计费
    "必应翻译(免费)": 4,            # 非官方网页API，高并发易被限流
    "微软翻译(Azure)": 8,
    "腾讯翻译": 8,
    "阿里翻译": 8,
    "IBM Watson": 8,
    "OpenAI兼容(硅基流动/Groq/DeepSeek等)": 4,  # 免费模型QPS低，4安全；付费模型可手动调高
    "Google Gemini": 4,          # 免费层15次/分钟
    "Ollama本地模型": 1,         # 本地GPU，并发太高反而慢且易超时，建议1
}

LANGUAGES = [
    ("自动检测", "auto"), ("中文(简体)", "zh-CN"), ("中文(繁体)", "zh-TW"),
    ("英语", "en"), ("日语", "ja"), ("韩语", "ko"),
    ("法语", "fr"), ("德语", "de"), ("西班牙语", "es"),
    ("葡萄牙语", "pt"), ("俄语", "ru"), ("意大利语", "it"),
    ("阿拉伯语", "ar"), ("泰语", "th"), ("越南语", "vi"),
    ("荷兰语", "nl"), ("波兰语", "pl"), ("瑞典语", "sv"),
]


# ==================== GUI ====================

class TranslatorApp:
    def __init__(self, root):
        self.root = root
        self.root.title(f"MTool翻译工具 v{VERSION}")
        self.root.geometry("880x760")
        self.root.minsize(780, 640)

        self.input_file = tk.StringVar()
        self.output_file = tk.StringVar(value="ManualTransFile.json")
        self.progress_file = tk.StringVar(value="trans_progress.json")
        self.api_engine = tk.StringVar(value=TRANSLATORS[0].name)
        self.source_lang = tk.StringVar(value="auto")
        self.target_lang = tk.StringVar(value="zh-CN")
        self.api_key = tk.StringVar()
        self.api_id = tk.StringVar()
        self.model = tk.StringVar()
        self.base_url = tk.StringVar()
        self.mymemory_email = tk.StringVar()
        self.google_proxies = tk.StringVar()  # 谷歌翻译代理地址，逗号/换行分隔，空=直连
        self.bing_proxy = tk.StringVar()  # 必应翻译CF Worker代理地址，空=直连
        self.deeplx_proxy = tk.StringVar()  # DeepLX代理地址，空=直连
        self.model_price_map = {}  # 显示名(含价格) -> 真实模型名
        self.preset = tk.StringVar(value="")
        self.max_workers = tk.IntVar(value=8)
        self.max_retry = tk.IntVar(value=3)
        self.chunk_size = tk.IntVar(value=1500)
        self.batch_size = tk.IntVar(value=5)  # LLM批量翻译条数，0=禁用
        self.protect_placeholders = tk.BooleanVar(value=True)  # 占位符保护开关
        self.protect_patterns = tk.StringVar(value=r"\{[^}]+\}|%[sdif]|\$[a-zA-Z_][a-zA-Z0-9_]*|<[^>]+>")  # 保护规则正则
        self.translation_cache = {}  # 翻译缓存：{原文: 译文}，避免重复翻译
        # 自适应背压：监控429频率自动调整请求间隔（按引擎隔离，避免A引擎限流拖累B引擎）
        self.rate_state = {}  # {engine_name: {"cooldown": 0.0, "429_timestamps": [], "success_count": 0, "total_count": 0}}
        self._rate_lock = threading.Lock()  # 保护上述变量的线程锁
        self.is_running = False
        self.should_stop = False
        self.is_paused = False
        self.translated = {}
        self.count = 0
        self.errors = 0
        self.total_chars = 0
        self.translated_chars = 0
        self.start_time = 0
        self.total_elapsed = 0

        # 新增功能变量
        self.dark_mode = tk.BooleanVar(value=False)
        self.webhook_url = tk.StringVar()
        self.webhook_type = tk.StringVar(value="generic")  # generic / dingtalk / wecom
        self.refine_mode = tk.BooleanVar(value=False)  # 预翻译+LLM精修模式
        self.refine_threshold = tk.IntVar(value=60)  # 质量分数低于此值则精修
        self.refine_workers = tk.IntVar(value=4)  # 精修阶段独立并发数（免费LLM模型QPS低，4安全）
        self.refine_pre_engine = tk.StringVar(value="谷歌翻译")  # 预翻译引擎
        self.refine_pre_workers = tk.IntVar(value=16)  # 预翻译并发
        # 预翻译独立API设置（选了需要密钥的引擎时用，不填则用主API设置页）
        self.pre_api_key = tk.StringVar()
        self.pre_api_id = tk.StringVar()
        self.pre_base_url = tk.StringVar()
        self.pre_model = tk.StringVar()
        self.pre_email = tk.StringVar()
        self.refine_llm_base_url = tk.StringVar(value="https://api.siliconflow.cn/v1")
        self.refine_llm_api_key = tk.StringVar()
        self.refine_llm_model = tk.StringVar(value="tencent/Hunyuan-MT-7B")
        self.refine_prompt = tk.StringVar(value="你是资深本地化审校专家。要求：1.必须保留所有占位符（{name}、%s、$var、<color>等），缺失会导致程序崩溃；2.术语前后一致；3.对话口语化，长度不超过原文120%；4.只输出修正后的译文，不要解释。")

        # ===== 双引擎并行配置（引擎B） =====
        self.enable_engine_b = tk.BooleanVar(value=False)
        self.engine_b_preset = tk.StringVar(value="")
        self.engine_b_base_url = tk.StringVar(value="https://api.siliconflow.cn/v1")
        self.engine_b_api_key = tk.StringVar()
        self.engine_b_model = tk.StringVar(value="tencent/Hunyuan-MT-7B")
        self.engine_b_workers = tk.IntVar(value=4)
        self.engine_b_batch_size = tk.IntVar(value=6)
        self.engine_b_model_price_map = {}  # 引擎B的模型价格映射

        self._loading_config = False
        # 配置文件保存在exe/脚本同目录（兼容PyInstaller打包）
        if getattr(sys, 'frozen', False):
            self._config_file = os.path.join(os.path.dirname(sys.executable), "auto_config.json")
            self._plugin_dir = os.path.join(os.path.dirname(sys.executable), "plugins")
        else:
            self._config_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "auto_config.json")
            self._plugin_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "plugins")

        self._build_ui()
        self._on_api_change()
        self._load_auto_config()
        self._setup_auto_save()
        self._load_plugins()

    def _build_ui(self):
        # 菜单栏
        menubar = tk.Menu(self.root)
        config_menu = tk.Menu(menubar, tearoff=0)
        config_menu.add_command(label="保存配置...", command=self._save_config)
        config_menu.add_command(label="加载配置...", command=self._load_config)
        config_menu.add_separator()
        config_menu.add_command(label="退出", command=self.root.quit)
        menubar.add_cascade(label="配置", menu=config_menu)

        # 主题菜单
        theme_menu = tk.Menu(menubar, tearoff=0)
        theme_menu.add_checkbutton(label="深色模式", variable=self.dark_mode, command=self._toggle_dark_mode)
        menubar.add_cascade(label="主题", menu=theme_menu)

        # 工具菜单
        tools_menu = tk.Menu(menubar, tearoff=0)
        tools_menu.add_command(label="插件管理器", command=self._plugin_manager)
        tools_menu.add_command(label="打开插件目录", command=self._open_plugin_dir)
        tools_menu.add_command(label="新建插件模板", command=self._create_plugin_template)
        tools_menu.add_separator()
        tools_menu.add_command(label="WebHook通知设置", command=self._webhook_settings)
        tools_menu.add_command(label="刷新插件列表", command=self._load_plugins)
        menubar.add_cascade(label="工具", menu=tools_menu)

        help_menu = tk.Menu(menubar, tearoff=0)
        help_menu.add_command(label="检查更新", command=self._check_updates_manual)
        help_menu.add_separator()
        help_menu.add_command(label="关于", command=self._show_about)
        menubar.add_cascade(label="帮助", menu=help_menu)

        self.root.config(menu=menubar)

        main = ttk.Frame(self.root, padding=10)
        main.pack(fill=tk.BOTH, expand=True)

        # Notebook 分页
        nb = ttk.Notebook(main)
        nb.pack(fill=tk.BOTH, expand=True)

        # ===== 页1：基本设置 =====
        tab1 = ttk.Frame(nb, padding=10)
        nb.add(tab1, text="基本设置")

        file_frame = ttk.LabelFrame(tab1, text="文件设置", padding=8)
        file_frame.pack(fill=tk.X, pady=(0, 8))

        ttk.Label(file_frame, text="输入文件:").grid(row=0, column=0, sticky=tk.W, pady=2)
        self.input_file_entry = ttk.Entry(file_frame, textvariable=self.input_file, width=70)
        self.input_file_entry.grid(row=0, column=1, padx=5, pady=2)
        ttk.Button(file_frame, text="浏览...", command=self._browse_input).grid(row=0, column=2, pady=2)

        ttk.Label(file_frame, text="输出文件:").grid(row=1, column=0, sticky=tk.W, pady=2)
        ttk.Entry(file_frame, textvariable=self.output_file, width=70).grid(row=1, column=1, padx=5, pady=2)
        ttk.Button(file_frame, text="浏览...", command=self._browse_output).grid(row=1, column=2, pady=2)

        ttk.Label(file_frame, text="进度文件:").grid(row=2, column=0, sticky=tk.W, pady=2)
        ttk.Entry(file_frame, textvariable=self.progress_file, width=70).grid(row=2, column=1, padx=5, pady=2)
        ttk.Button(file_frame, text="清除进度", command=self._clear_progress).grid(row=2, column=2, pady=2)

        # 语言设置
        lang_frame = ttk.LabelFrame(tab1, text="语言设置", padding=8)
        lang_frame.pack(fill=tk.X, pady=(0, 8))

        ttk.Label(lang_frame, text="源语言:").grid(row=0, column=0, sticky=tk.W, padx=5, pady=2)
        ttk.Combobox(lang_frame, textvariable=self.source_lang,
                     values=[f"{n} ({c})" for n, c in LANGUAGES], state="readonly", width=20).grid(row=0, column=1, padx=5, pady=2)

        ttk.Label(lang_frame, text="目标语言:").grid(row=0, column=2, sticky=tk.W, padx=(20, 5), pady=2)
        ttk.Combobox(lang_frame, textvariable=self.target_lang,
                     values=[f"{n} ({c})" for n, c in LANGUAGES if c != "auto"], state="readonly", width=20).grid(row=0, column=3, padx=5, pady=2)

        # 翻译设置
        set_frame = ttk.LabelFrame(tab1, text="翻译设置", padding=8)
        set_frame.pack(fill=tk.X, pady=(0, 8))

        ttk.Label(set_frame, text="并发数:").grid(row=0, column=0, sticky=tk.W, padx=5, pady=2)
        ttk.Spinbox(set_frame, from_=1, to=64, textvariable=self.max_workers, width=8).grid(row=0, column=1, padx=5, pady=2, sticky=tk.W)

        ttk.Label(set_frame, text="重试次数:").grid(row=0, column=2, sticky=tk.W, padx=(20, 5), pady=2)
        ttk.Spinbox(set_frame, from_=0, to=10, textvariable=self.max_retry, width=8).grid(row=0, column=3, padx=5, pady=2, sticky=tk.W)

        ttk.Label(set_frame, text="分段字符数:").grid(row=0, column=4, sticky=tk.W, padx=(20, 5), pady=2)
        ttk.Spinbox(set_frame, from_=500, to=5000, textvariable=self.chunk_size, width=8).grid(row=0, column=5, padx=5, pady=2, sticky=tk.W)

        ttk.Label(set_frame, text="LLM批量数:").grid(row=0, column=6, sticky=tk.W, padx=(20, 5), pady=2)
        ttk.Spinbox(set_frame, from_=0, to=20, textvariable=self.batch_size, width=8).grid(row=0, column=7, padx=5, pady=2, sticky=tk.W)

        ttk.Checkbutton(set_frame, text="占位符保护", variable=self.protect_placeholders).grid(row=1, column=0, sticky=tk.W, padx=5, pady=(5, 2))
        ttk.Label(set_frame, text="保护规则:").grid(row=1, column=1, sticky=tk.W, padx=(15, 5), pady=(5, 2))
        ttk.Entry(set_frame, textvariable=self.protect_patterns, width=50).grid(row=1, column=2, columnspan=6, padx=5, pady=(5, 2), sticky=tk.W+tk.E)

        ttk.Label(set_frame, text="提示: LLM引擎并发建议设低(4-8)，传统翻译API可设高(16-32)。LLM批量数=每请求合并几条(0=禁用，仅LLM生效)。占位符保护：翻译前将{name}/%s/<color>/$var等替换为唯一标记，翻译后还原，防止被翻译导致程序崩溃。", foreground="gray").grid(row=2, column=0, columnspan=8, sticky=tk.W, padx=5, pady=(2, 0))

        # ===== 预翻译+LLM精修 =====
        refine_frame = ttk.LabelFrame(tab1, text="预翻译 + LLM精修（两阶段独立配置，开启后不依赖上方主设置）", padding=8)
        refine_frame.pack(fill=tk.X, pady=(0, 8))

        ttk.Checkbutton(refine_frame, text="启用精修模式", variable=self.refine_mode).grid(row=0, column=0, columnspan=6, sticky=tk.W, padx=5, pady=2)

        # 第一阶段：预翻译
        ttk.Label(refine_frame, text="【第一阶段·预翻译】", foreground="#2196F3", font=("微软雅黑", 9, "bold")).grid(row=1, column=0, columnspan=3, sticky=tk.W, padx=5, pady=(8, 2))
        ttk.Label(refine_frame, text="引擎:").grid(row=2, column=0, sticky=tk.W, padx=5, pady=2)
        self.refine_pre_combo = ttk.Combobox(refine_frame, textvariable=self.refine_pre_engine,
                                               values=[t.name for t in TRANSLATORS], state="readonly", width=28)
        self.refine_pre_combo.grid(row=2, column=1, padx=5, pady=2, sticky=tk.W)
        self.refine_pre_combo.bind("<<ComboboxSelected>>", self._on_pre_engine_change)
        ttk.Label(refine_frame, text="并发:").grid(row=2, column=2, sticky=tk.W, padx=(15, 5), pady=2)
        ttk.Spinbox(refine_frame, from_=1, to=64, textvariable=self.refine_pre_workers, width=8).grid(row=2, column=3, padx=5, pady=2, sticky=tk.W)

        # 预翻译API设置（折叠式，默认收起；选了需要密钥的引擎时展开填写）
        self._pre_api_expanded = False
        self._pre_api_toggle = ttk.Button(refine_frame, text="展开API设置 ▼（选DeepL/百度等需密钥引擎时填写，不填则用上方API设置页）", command=self._toggle_pre_api)
        self._pre_api_toggle.grid(row=3, column=0, columnspan=7, sticky=tk.W, padx=5, pady=(2, 0))
        self._pre_api_content = ttk.Frame(refine_frame)
        ttk.Label(self._pre_api_content, text="API Key:").grid(row=0, column=0, sticky=tk.W, pady=2)
        self.pre_api_key_entry = ttk.Entry(self._pre_api_content, textvariable=self.pre_api_key, width=40, show="*")
        self.pre_api_key_entry.grid(row=0, column=1, padx=5, pady=2, sticky=tk.W)
        ttk.Label(self._pre_api_content, text="API ID:").grid(row=0, column=2, sticky=tk.W, padx=(15, 5), pady=2)
        self.pre_api_id_entry = ttk.Entry(self._pre_api_content, textvariable=self.pre_api_id, width=30)
        self.pre_api_id_entry.grid(row=0, column=3, padx=5, pady=2, sticky=tk.W)
        ttk.Label(self._pre_api_content, text="Base URL:").grid(row=1, column=0, sticky=tk.W, pady=2)
        self.pre_base_url_entry = ttk.Entry(self._pre_api_content, textvariable=self.pre_base_url, width=40)
        self.pre_base_url_entry.grid(row=1, column=1, padx=5, pady=2, sticky=tk.W)
        ttk.Label(self._pre_api_content, text="Model:").grid(row=1, column=2, sticky=tk.W, padx=(15, 5), pady=2)
        self.pre_model_entry = ttk.Entry(self._pre_api_content, textvariable=self.pre_model, width=30)
        self.pre_model_entry.grid(row=1, column=3, padx=5, pady=2, sticky=tk.W)
        ttk.Label(self._pre_api_content, text="邮箱(MyMemory):").grid(row=2, column=0, sticky=tk.W, pady=2)
        self.pre_email_entry = ttk.Entry(self._pre_api_content, textvariable=self.pre_email, width=40)
        self.pre_email_entry.grid(row=2, column=1, padx=5, pady=2, sticky=tk.W)
        ttk.Label(self._pre_api_content, text="（全部留空则使用上方API设置页的配置）", foreground="gray").grid(row=2, column=2, columnspan=2, sticky=tk.W, padx=5, pady=2)
        self._on_pre_engine_change()  # 初始化输入框启用/禁用状态

        # 第二阶段：LLM精修
        ttk.Label(refine_frame, text="【第二阶段·LLM精修】", foreground="#FF9800", font=("微软雅黑", 9, "bold")).grid(row=5, column=0, columnspan=3, sticky=tk.W, padx=5, pady=(8, 2))
        ttk.Label(refine_frame, text="LLM配置：直接使用上方「API设置」页的OpenAI兼容引擎（BaseURL/Key/模型）", foreground="gray").grid(row=6, column=0, columnspan=7, sticky=tk.W, padx=5, pady=2)
        ttk.Label(refine_frame, text="并发:").grid(row=7, column=0, sticky=tk.W, padx=5, pady=2)
        ttk.Spinbox(refine_frame, from_=1, to=32, textvariable=self.refine_workers, width=8).grid(row=7, column=1, padx=5, pady=2, sticky=tk.W)
        ttk.Label(refine_frame, text="阈值:").grid(row=7, column=2, sticky=tk.W, padx=(15, 5), pady=2)
        ttk.Spinbox(refine_frame, from_=0, to=100, textvariable=self.refine_threshold, width=8).grid(row=7, column=3, padx=5, pady=2, sticky=tk.W)

        ttk.Label(refine_frame, text="精修Prompt:").grid(row=8, column=0, sticky=tk.W, padx=5, pady=2)
        ttk.Entry(refine_frame, textvariable=self.refine_prompt, width=70).grid(row=8, column=1, columnspan=6, padx=5, pady=2, sticky=tk.W+tk.E)
        refine_frame.columnconfigure(6, weight=1)

        ttk.Label(refine_frame, text="流程：①用预翻译引擎（默认谷歌）快速翻译全部 → ②自动评估质量 → ③对低于阈值的条目用API设置页配置的LLM重新精修。开启后预翻译阶段使用本区域配置，精修阶段使用上方API设置页的LLM配置。", foreground="gray", wraplength=800).grid(row=9, column=0, columnspan=7, sticky=tk.W, padx=5, pady=(5, 0))

        # ===== 页2：API设置 =====
        tab2 = ttk.Frame(nb, padding=10)
        nb.add(tab2, text="API设置")

        engine_frame = ttk.LabelFrame(tab2, text="翻译引擎", padding=8)
        engine_frame.pack(fill=tk.X, pady=(0, 8))

        ttk.Label(engine_frame, text="引擎类型:").grid(row=0, column=0, sticky=tk.W, pady=2)
        self.api_combo = ttk.Combobox(engine_frame, textvariable=self.api_engine,
                                       values=[t.name for t in TRANSLATORS], state="readonly", width=35)
        self.api_combo.grid(row=0, column=1, padx=5, pady=2, sticky=tk.W)
        self.api_combo.bind("<<ComboboxSelected>>", self._on_api_change)
        ttk.Button(engine_frame, text="测试连接", command=self._test_connection).grid(row=0, column=2, padx=10, pady=2)

        # 预设
        ttk.Label(engine_frame, text="快速预设:").grid(row=1, column=0, sticky=tk.W, pady=2)
        self.preset_combo = ttk.Combobox(engine_frame, textvariable=self.preset,
                                           values=[""] + list(PRESETS.keys()), state="readonly", width=35)
        self.preset_combo.grid(row=1, column=1, padx=5, pady=2, sticky=tk.W)
        self.preset_combo.bind("<<ComboboxSelected>>", self._on_preset_change)
        ttk.Label(engine_frame, text="(选择后自动填入base_url和model)", foreground="gray").grid(row=1, column=2, sticky=tk.W, padx=5)

        # API参数
        param_frame = ttk.LabelFrame(tab2, text="API参数", padding=8)
        param_frame.pack(fill=tk.X, pady=(0, 8))

        ttk.Label(param_frame, text="Base URL:").grid(row=0, column=0, sticky=tk.W, pady=2)
        self.base_url_entry = ttk.Entry(param_frame, textvariable=self.base_url, width=55)
        self.base_url_entry.grid(row=0, column=1, columnspan=3, padx=5, pady=2, sticky=tk.W)

        ttk.Label(param_frame, text="Model:").grid(row=1, column=0, sticky=tk.W, pady=2)
        self.model_entry = ttk.Combobox(param_frame, textvariable=self.model, width=45)
        self.model_entry.grid(row=1, column=1, columnspan=2, padx=5, pady=2, sticky=tk.W)
        ttk.Button(param_frame, text="获取模型", command=self._fetch_models).grid(row=1, column=3, padx=5, pady=2)

        ttk.Label(param_frame, text="API Key:").grid(row=2, column=0, sticky=tk.W, pady=2)
        self.api_key_entry = ttk.Entry(param_frame, textvariable=self.api_key, width=40, show="*")
        self.api_key_entry.grid(row=2, column=1, padx=5, pady=2, sticky=tk.W)
        ttk.Button(param_frame, text="显示/隐藏", command=self._toggle_key).grid(row=2, column=2, padx=5, pady=2)

        ttk.Label(param_frame, text="API ID:").grid(row=3, column=0, sticky=tk.W, pady=2)
        self.api_id_entry = ttk.Entry(param_frame, textvariable=self.api_id, width=40)
        self.api_id_entry.grid(row=3, column=1, padx=5, pady=2, sticky=tk.W)

        ttk.Label(param_frame, text="MyMemory邮箱:").grid(row=4, column=0, sticky=tk.W, pady=2)
        self.email_entry = ttk.Entry(param_frame, textvariable=self.mymemory_email, width=40)
        self.email_entry.grid(row=4, column=1, padx=5, pady=2, sticky=tk.W)
        ttk.Label(param_frame, text="(填写后限额从5千/天提升至5万/天)", foreground="gray").grid(row=4, column=2, sticky=tk.W, padx=5)

        # ===== 代理设置（折叠式，默认收起，CF Worker代理防限流） =====
        self._proxy_expanded = False
        proxy_frame = ttk.LabelFrame(tab2, text="代理设置（Cloudflare Worker代理，防限流）", padding=8)
        proxy_frame.pack(fill=tk.X, pady=(0, 8))
        self._proxy_toggle_btn = ttk.Button(proxy_frame, text="展开代理设置 ▼", command=self._toggle_proxy)
        self._proxy_toggle_btn.pack(anchor=tk.W)
        self._proxy_content = ttk.Frame(proxy_frame)

        ttk.Label(self._proxy_content, text="谷歌翻译代理:").grid(row=0, column=0, sticky=tk.W, pady=2)
        self.google_proxies_entry = ttk.Entry(self._proxy_content, textvariable=self.google_proxies, width=55)
        self.google_proxies_entry.grid(row=0, column=1, columnspan=2, padx=5, pady=2, sticky=tk.W)
        ttk.Label(self._proxy_content, text="(多个Worker地址逗号分隔，空=直连)", foreground="gray").grid(row=0, column=3, sticky=tk.W, padx=5)

        ttk.Label(self._proxy_content, text="必应翻译代理:").grid(row=1, column=0, sticky=tk.W, pady=2)
        self.bing_proxy_entry = ttk.Entry(self._proxy_content, textvariable=self.bing_proxy, width=55)
        self.bing_proxy_entry.grid(row=1, column=1, columnspan=2, padx=5, pady=2, sticky=tk.W)
        ttk.Label(self._proxy_content, text="(单个Worker地址，空=直连)", foreground="gray").grid(row=1, column=3, sticky=tk.W, padx=5)

        ttk.Label(self._proxy_content, text="DeepLX代理:").grid(row=2, column=0, sticky=tk.W, pady=2)
        self.deeplx_proxy_entry = ttk.Entry(self._proxy_content, textvariable=self.deeplx_proxy, width=55)
        self.deeplx_proxy_entry.grid(row=2, column=1, columnspan=2, padx=5, pady=2, sticky=tk.W)
        ttk.Label(self._proxy_content, text="(预留，DeepLX服务地址，空=直连)", foreground="gray").grid(row=2, column=3, sticky=tk.W, padx=5)

        ttk.Label(self._proxy_content, text="部署方法: 帮助→关于 中查看CF Worker部署代码，每个引擎部署一个Worker", foreground="blue").grid(row=3, column=0, columnspan=4, sticky=tk.W, padx=5, pady=(4, 0))

        # 提示
        self.hint_label = ttk.Label(tab2, text="", foreground="blue", wraplength=800, justify=tk.LEFT)
        self.hint_label.pack(fill=tk.X, pady=5)

        # ===== 高级设置（折叠式，默认收起，内含引擎B双Key加速） =====
        self._advanced_expanded = False
        adv_frame = ttk.LabelFrame(tab2, text="高级设置", padding=8)
        adv_frame.pack(fill=tk.X, pady=(0, 8))
        self._adv_toggle_btn = ttk.Button(adv_frame, text="展开高级 ▼（引擎B双Key加速）", command=self._toggle_advanced)
        self._adv_toggle_btn.pack(anchor=tk.W)
        self._adv_content = ttk.Frame(adv_frame)

        # 引擎B（双Key并行加速）
        self.engine_b_frame = ttk.Frame(self._adv_content)
        self.engine_b_frame.pack(fill=tk.X)

        ttk.Checkbutton(self.engine_b_frame, text="启用引擎B（双Key并行加速）", variable=self.enable_engine_b, command=self._on_engine_b_toggle).grid(row=0, column=0, columnspan=6, sticky=tk.W, padx=5, pady=2)

        ttk.Label(self.engine_b_frame, text="快速预设:").grid(row=1, column=0, sticky=tk.W, pady=2)
        self.engine_b_preset_combo = ttk.Combobox(self.engine_b_frame, textvariable=self.engine_b_preset,
                                                     values=[""] + list(PRESETS.keys()), state="readonly", width=35)
        self.engine_b_preset_combo.grid(row=1, column=1, padx=5, pady=2, sticky=tk.W)
        self.engine_b_preset_combo.bind("<<ComboboxSelected>>", self._on_engine_b_preset_change)
        ttk.Button(self.engine_b_frame, text="复制主配置", command=self._copy_engine_a_to_b).grid(row=1, column=2, sticky=tk.W, padx=5)

        ttk.Label(self.engine_b_frame, text="Base URL:").grid(row=2, column=0, sticky=tk.W, pady=2)
        self.engine_b_base_url_entry = ttk.Entry(self.engine_b_frame, textvariable=self.engine_b_base_url, width=55)
        self.engine_b_base_url_entry.grid(row=2, column=1, columnspan=3, padx=5, pady=2, sticky=tk.W)

        ttk.Label(self.engine_b_frame, text="Model:").grid(row=3, column=0, sticky=tk.W, pady=2)
        self.engine_b_model_entry = ttk.Combobox(self.engine_b_frame, textvariable=self.engine_b_model, width=45)
        self.engine_b_model_entry.grid(row=3, column=1, columnspan=2, padx=5, pady=2, sticky=tk.W)
        ttk.Button(self.engine_b_frame, text="获取模型", command=lambda: self._fetch_models_engine_b()).grid(row=3, column=3, padx=5, pady=2)

        ttk.Label(self.engine_b_frame, text="API Key:").grid(row=4, column=0, sticky=tk.W, pady=2)
        self.engine_b_api_key_entry = ttk.Entry(self.engine_b_frame, textvariable=self.engine_b_api_key, width=40, show="*")
        self.engine_b_api_key_entry.grid(row=4, column=1, padx=5, pady=2, sticky=tk.W)
        ttk.Button(self.engine_b_frame, text="显示/隐藏", command=self._toggle_engine_b_key).grid(row=4, column=2, padx=5, pady=2)

        ttk.Label(self.engine_b_frame, text="并发:").grid(row=5, column=0, sticky=tk.W, pady=2)
        ttk.Spinbox(self.engine_b_frame, from_=1, to=64, textvariable=self.engine_b_workers, width=8).grid(row=5, column=1, padx=5, pady=2, sticky=tk.W)

        ttk.Label(self.engine_b_frame, text="批量:").grid(row=5, column=2, sticky=tk.W, padx=(15, 5), pady=2)
        ttk.Spinbox(self.engine_b_frame, from_=0, to=20, textvariable=self.engine_b_batch_size, width=8).grid(row=5, column=3, padx=5, pady=2, sticky=tk.W)

        ttk.Button(self.engine_b_frame, text="测试连接B", command=self._test_connection_b).grid(row=6, column=0, padx=5, pady=5)
        self.engine_b_hint = ttk.Label(self.engine_b_frame, text="启用后可使用第二个API Key/本地模型并行翻译，速度接近翻倍。支持OpenAI兼容API和Ollama本地模型（Base URL填http://localhost:11434/v1，无需API Key）。", foreground="gray", wraplength=700)
        self.engine_b_hint.grid(row=6, column=1, columnspan=3, sticky=tk.W, padx=5, pady=5)

        self._on_engine_b_toggle()  # 初始化显示状态
        self._adv_content.pack_forget()  # 默认收起

        # 引擎说明
        info_frame = ttk.LabelFrame(tab2, text="引擎说明", padding=8)
        info_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 8))
        info_text = scrolledtext.ScrolledText(info_frame, height=10, font=("微软雅黑", 9), wrap=tk.WORD)
        info_text.pack(fill=tk.BOTH, expand=True)
        info_text.insert(tk.END, """【传统翻译引擎】
• 谷歌翻译(免费) - 非官方接口，无需注册，无限额，稳定性一般
• MyMemory(免费) - 匿名5千字符/天，在API参数中填写邮箱后提升至5万字符/天
• 百度翻译 - 标准版5万字符/月免费，需AppID+密钥
• 有道翻译 - 新用户有免费额度，需应用ID+密钥
• DeepL - 免费版50万字符/月，翻译质量最高，需信用卡验证
• 微软翻译(Azure) - 免费层200万字符/月，需订阅密钥+区域
• 腾讯翻译 - 按字符计费，需SecretId+SecretKey
• 阿里翻译 - 按字符计费，需AccessKey ID+Secret
• IBM Watson - Lite版100万字符/月，需IAM API Key+服务端点

【LLM大模型引擎】
• OpenAI兼容 - 通用接口，支持硅基流动/Groq/DeepSeek/豆包/百炼/Kimi/智谱/OpenAI等，点"获取模型"自动拉取模型列表
• Google Gemini - 谷歌官方，gemini-1.5-flash免费层每分钟15次请求
• Ollama本地模型 - 完全离线免费，需先安装Ollama并拉取模型

【高级功能】
• 暂停/继续 - 翻译中可暂停，当前批次完成后等待，继续不丢失进度
• 配置保存/加载 - 顶部菜单"配置"可保存/加载整套配置为.cfg文件
• 拖拽文件 - 将JSON文件拖入窗口自动填入路径（安装: 打开CMD/PowerShell → 执行 pip install tkinterdnd2 → 重启脚本）
• 指数退避重试 - 限流/超时自动退避重试，授权失败直接跳过
• 上下文参考 - LLM引擎自动使用前3条译文作为参考，保持术语一致
• 费用预估 - 翻译前显示待翻译字符数和预计费用
• 详细报告 - 条目/字符/速度/耗时等完整统计

【推荐】
- 省事：谷歌翻译(免费)，啥都不用填
- 质量好：DeepL免费版 或 硅基流动(注册送免费额度)
- 完全离线免费：Ollama + qwen2.5:7b（需显卡，CPU也能跑但慢）
- 速度极快：Groq（免费，推理速度超快）""")
        info_text.config(state=tk.DISABLED)

        # ===== 页3：翻译进度 =====
        tab3 = ttk.Frame(nb, padding=10)
        nb.add(tab3, text="翻译进度")

        prog_frame = ttk.LabelFrame(tab3, text="进度", padding=8)
        prog_frame.pack(fill=tk.X, pady=(0, 8))

        self.progress_bar = ttk.Progressbar(prog_frame, mode="determinate")
        self.progress_bar.pack(fill=tk.X, pady=(0, 5))

        self.status_label = ttk.Label(prog_frame, text="就绪", font=("微软雅黑", 10))
        self.status_label.pack(anchor=tk.W)

        ttk.Label(prog_frame, text="提示：失败条目不会写入进度文件，翻译完成后重新选择同一文件并点击「开始翻译」即可自动重试失败条目。", foreground="gray", wraplength=800, font=("微软雅黑", 8)).pack(anchor=tk.W, pady=(3, 0))

        # 控制按钮
        btn_frame = ttk.Frame(tab3)
        btn_frame.pack(fill=tk.X, pady=(0, 8))

        self.start_btn = ttk.Button(btn_frame, text="开始翻译", command=self._start_translate)
        self.start_btn.pack(side=tk.LEFT, padx=(0, 5))

        self.pause_btn = ttk.Button(btn_frame, text="暂停", command=self._toggle_pause, state="disabled")
        self.pause_btn.pack(side=tk.LEFT, padx=(0, 5))

        self.stop_btn = ttk.Button(btn_frame, text="停止", command=self._stop_translate, state="disabled")
        self.stop_btn.pack(side=tk.LEFT, padx=(0, 5))

        ttk.Button(btn_frame, text="加载已有翻译", command=self._load_existing).pack(side=tk.LEFT, padx=(0, 5))
        ttk.Button(btn_frame, text="检测翻译不全", command=self._detect_incomplete).pack(side=tk.LEFT, padx=(0, 5))
        ttk.Button(btn_frame, text="统计信息", command=self._show_stats).pack(side=tk.LEFT, padx=(0, 5))
        ttk.Button(btn_frame, text="费用预估", command=self._show_cost_estimate).pack(side=tk.LEFT, padx=(0, 5))
        ttk.Button(btn_frame, text="详细报告", command=self._show_detailed_stats).pack(side=tk.LEFT)

        # 日志
        log_frame = ttk.LabelFrame(tab3, text="日志", padding=5)
        log_frame.pack(fill=tk.BOTH, expand=True)

        self.log_text = scrolledtext.ScrolledText(log_frame, height=15, font=("Consolas", 9), wrap=tk.WORD)
        self.log_text.pack(fill=tk.BOTH, expand=True)
        self.log_text.config(state=tk.DISABLED)

    def _toggle_advanced(self):
        """展开/收起高级设置（引擎B）"""
        if self._advanced_expanded:
            self._adv_content.pack_forget()
            self._adv_toggle_btn.config(text="展开高级 ▼（引擎B双Key加速）")
            self._advanced_expanded = False
        else:
            self._adv_content.pack(fill=tk.X, pady=(5, 0))
            self._adv_toggle_btn.config(text="收起高级 ▲")
            self._advanced_expanded = True

    def _toggle_proxy(self):
        """展开/收起代理设置"""
        if self._proxy_expanded:
            self._proxy_content.pack_forget()
            self._proxy_toggle_btn.config(text="展开代理设置 ▼")
            self._proxy_expanded = False
        else:
            self._proxy_content.pack(fill=tk.X, pady=(5, 0))
            self._proxy_toggle_btn.config(text="收起代理设置 ▲")
            self._proxy_expanded = True

    def _log(self, msg):
        def _append():
            self.log_text.config(state=tk.NORMAL)
            self.log_text.insert(tk.END, f"[{time.strftime('%H:%M:%S')}] {msg}\n")
            self.log_text.see(tk.END)
            self.log_text.config(state=tk.DISABLED)
        try:
            self.root.after(0, _append)
        except Exception:
            pass

    def _toggle_key(self):
        if self.api_key_entry.cget('show') == '*':
            self.api_key_entry.config(show='')
        else:
            self.api_key_entry.config(show='*')

    def _test_connection(self):
        """测试当前API配置是否可用，翻译一句测试文本"""
        engine_name = self.api_engine.get()
        engine_cls = next((t for t in TRANSLATORS if t.name == engine_name), None)
        if not engine_cls:
            messagebox.showwarning("提示", "请先选择翻译引擎")
            return
        # 检查必填参数
        missing = []
        if engine_cls.need_api_key and not self.api_key.get():
            missing.append("API Key")
        if engine_cls.need_api_id and not self.api_id.get():
            missing.append("API ID")
        if engine_cls.need_model and not self.model.get():
            missing.append("Model")
        if engine_cls.need_base_url and not self.base_url.get():
            missing.append("Base URL")
        if missing:
            messagebox.showwarning("参数缺失", f"以下参数未填写：\n{', '.join(missing)}")
            return

        test_text = "Hello, world! This is a connection test."
        src = "en"
        tgt = self._get_lang_code(self.target_lang.get())
        if tgt == "auto":
            tgt = "zh-CN"

        self._log(f"正在测试连接: {engine_name} ...")
        try:
            translator = self._get_translator()
            result = translator.translate(test_text, src, tgt)
            self._log(f"连接成功！译文: {result}")
            messagebox.showinfo("连接成功",
                                f"引擎: {engine_name}\n"
                                f"原文: {test_text}\n"
                                f"译文: {result}\n\n"
                                f"API配置可用，可以开始翻译。")
        except Exception as e:
            err_msg = str(e)
            self._log(f"连接失败: {err_msg}")
            is_timeout = 'timed out' in err_msg.lower() or 'timeout' in err_msg.lower() or 'read timed out' in err_msg.lower()
            if is_timeout:
                hint = (f"引擎: {engine_name}\n"
                       f"错误: 请求超时（模型响应慢或暂时不可用）\n\n"
                       f"可能原因：\n"
                       f"1. 当前模型在平台上过载/排队/维护\n"
                       f"2. 网络连接不稳定\n\n"
                       f"建议：\n"
                       f"• 换个模型试试（如 Qwen/Qwen2.5-7B-Instruct）\n"
                       f"• 稍等几分钟后重试\n"
                       f"• 检查API Key是否有效")
                messagebox.showwarning("连接超时", hint)
            else:
                messagebox.showerror("连接失败",
                                     f"引擎: {engine_name}\n"
                                     f"错误: {err_msg}\n\n"
                                     f"请检查API参数、网络连接和额度。")

    def _format_model_price(self, model_name):
        """格式化模型价格用于显示"""
        price = MODEL_PRICES.get(model_name)
        if price is None:
            return "价格未知"
        if price == 0:
            return "免费"
        return f"{price}元/百万token"

    def _on_model_select(self, event=None):
        """从下拉框选中带价格的模型名时，提取真实模型名"""
        display = self.model_entry.get()
        if display in self.model_price_map:
            real = self.model_price_map[display]
            self.model.set(real)

    def _fetch_models(self):
        """从OpenAI兼容API获取可用模型列表，填充到Model下拉框（含价格）"""
        base = self.base_url.get().rstrip('/')
        api_key = self.api_key.get()
        if not base:
            messagebox.showwarning("提示", "请先填写Base URL")
            return
        if not api_key and self.api_engine.get() != "Ollama本地模型":
            messagebox.showwarning("提示", "请先填写API Key")
            return

        # Ollama用原生/api/tags端点，其他OpenAI兼容API用/models端点
        is_ollama = self.api_engine.get() == "Ollama本地模型"
        if is_ollama:
            url = f"{base}/api/tags"
        else:
            url = f"{base}/models"
        self._log(f"正在获取模型列表: {url}")
        # 放到后台线程执行，避免阻塞UI
        def run_fetch():
            try:
                import urllib.request as _req
                headers = {'User-Agent': 'Mozilla/5.0'}
                if api_key:
                    headers['Authorization'] = f'Bearer {api_key}'
                req = _req.Request(url, headers=headers)
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                # Ollama本地模型：绕过系统代理，避免localhost请求被代理拦截返回502
                if is_ollama:
                    proxy_handler = _req.ProxyHandler({})
                    opener = _req.build_opener(proxy_handler, _req.HTTPSHandler(context=ctx))
                    resp = opener.open(req, timeout=30)
                else:
                    resp = _req.urlopen(req, context=ctx, timeout=30)
                data = json.loads(resp.read().decode('utf-8'))

                # OpenAI兼容格式: {"data": [{"id": "model-name"}, ...]}
                models = []
                if isinstance(data, dict):
                    if 'data' in data and isinstance(data['data'], list):
                        models = [m.get('id', '') for m in data['data'] if m.get('id')]
                    elif 'models' in data and isinstance(data['models'], list):
                        models = [m.get('id', m.get('name', '')) for m in data['models'] if isinstance(m, dict)]
                elif isinstance(data, list):
                    models = [m.get('id', '') for m in data if isinstance(m, dict) and m.get('id')]

                # 硅基流动：从官方文档获取实时价格表
                live_prices = {}
                if 'siliconflow' in base.lower():
                    live_prices = fetch_siliconflow_prices()
                    if live_prices:
                        MODEL_PRICES.update(live_prices)
                        self._log(f"实时价格更新: {len(live_prices)} 个模型（来自硅基流动官方文档）")

                # 排序：免费优先 → 模型大小(小=快) → 价格低 → 名称
                models = sorted(set(models), key=_model_sort_key)
                if not models:
                    self.root.after(0, lambda: messagebox.showinfo("提示", "未获取到模型列表，API返回格式可能不兼容。"))
                    self._log("未获取到模型列表")
                    return

                # 构建显示名（含价格）→ 真实模型名 的映射
                self.model_price_map = {}
                display_names = []
                free_count = 0
                known_count = 0
                for m in models:
                    price_str = self._format_model_price(m)
                    if price_str == "免费":
                        free_count += 1
                        known_count += 1
                    elif price_str != "价格未知":
                        known_count += 1
                    display = f"{m}  [{price_str}]"
                    self.model_price_map[display] = m
                    display_names.append(display)

                def update_ui():
                    self.model_entry['values'] = display_names
                    self.model_entry.bind("<<ComboboxSelected>>", self._on_model_select)
                    # 找出免费模型
                    free_models = [m for m in models if MODEL_PRICES.get(m) == 0]
                    # 推荐模型：优先Hunyuan-MT翻译专用模型，其次其他免费模型，最后第一个
                    recommended = None
                    for m in models:
                        if 'hunyuan-mt' in m.lower():
                            recommended = m
                            break
                    if not recommended and free_models:
                        recommended = free_models[0]
                    if not recommended:
                        recommended = models[0]
                    # 如果当前model在列表中，保持选中；否则默认选推荐模型
                    current = self.model.get()
                    if current not in models:
                        self.model.set(recommended)
                    msg = f"共获取到 {len(models)} 个模型，已填入Model下拉框（含价格）。\n\n"
                    msg += f"免费: {free_count}个 | 已知价格: {known_count}个 | 价格未知: {len(models)-known_count}个\n"
                    if live_prices:
                        msg += f"实时价格: {len(live_prices)}个模型（来自硅基流动API）\n"
                    msg += "\n"
                    msg += f"推荐模型: {recommended}\n"
                    msg += "\n下拉框中 [ ] 内为价格，选中后自动填入真实模型名。"
                    self.root.after(0, lambda: messagebox.showinfo("获取成功", msg))
                self.root.after(0, update_ui)
                self._log(f"获取到 {len(models)} 个模型（免费{free_count}个，已知价格{known_count}个）")
                self._log(f"模型列表: {', '.join(models[:15])}{'...' if len(models) > 15 else ''}")
            except Exception as e:
                err_msg = str(e) if str(e) and str(e) != "None" else "请求失败（可能是系统代理干扰或Ollama未启动）"
                self._log(f"获取模型失败: {err_msg}")
                self.root.after(0, lambda: messagebox.showerror("获取失败", f"无法获取模型列表：\n{err_msg}\n\n请检查Base URL、API Key和网络连接。Ollama本地模型请确保ollama serve已启动。"))
        threading.Thread(target=run_fetch, daemon=True).start()

    def _on_pre_engine_change(self, event=None):
        """预翻译引擎切换时，自动启用/禁用对应的API输入框"""
        engine_name = self.refine_pre_engine.get()
        engine_cls = next((t for t in TRANSLATORS if t.name == engine_name), GoogleTranslator)
        self.pre_api_key_entry.config(state="normal" if engine_cls.need_api_key else "disabled")
        self.pre_api_id_entry.config(state="normal" if engine_cls.need_api_id else "disabled")
        self.pre_base_url_entry.config(state="normal" if engine_cls.need_base_url else "disabled")
        self.pre_model_entry.config(state="normal" if engine_cls.need_model else "disabled")
        self.pre_email_entry.config(state="normal" if engine_cls.need_email else "disabled")

    def _toggle_pre_api(self):
        """展开/收起预翻译API设置"""
        if self._pre_api_expanded:
            self._pre_api_content.grid_remove()
            self._pre_api_toggle.config(text="展开API设置 ▼（选DeepL/百度等需密钥引擎时填写，不填则用上方API设置页）")
            self._pre_api_expanded = False
        else:
            self._pre_api_content.grid(row=4, column=0, columnspan=7, sticky=tk.W, padx=5, pady=2)
            self._pre_api_toggle.config(text="收起API设置 ▲")
            self._pre_api_expanded = True

    def _on_api_change(self, event=None):
        engine_name = self.api_engine.get()
        engine_cls = next((t for t in TRANSLATORS if t.name == engine_name), None)
        if not engine_cls:
            return
        # 显示/隐藏对应输入框
        self.api_key_entry.config(state="normal" if engine_cls.need_api_key else "disabled")
        self.api_id_entry.config(state="normal" if engine_cls.need_api_id else "disabled")
        self.model_entry.config(state="normal" if engine_cls.need_model else "disabled")
        self.base_url_entry.config(state="normal" if engine_cls.need_base_url else "disabled")
        self.email_entry.config(state="normal" if engine_cls.need_email else "disabled")
        # DeepL不需要Base URL和Model，切换时自动清空避免残留值干扰
        if engine_name == "DeepL":
            self.base_url.set("")
            self.model.set("")
        # 代理设置区始终可编辑（选对应引擎时才生效）
        self.google_proxies_entry.config(state="normal")
        self.bing_proxy_entry.config(state="normal")
        # 更新提示
        hints = {
            "谷歌翻译(免费)": "非官方接口，易被限流。建议在下方「谷歌代理池」填写Cloudflare Worker地址（逗号分隔）自动换IP，空=直连。Worker部署代码见帮助→关于。",
            "MyMemory(免费)": '匿名5千字符/天；在下方"MyMemory邮箱"填写注册邮箱后，限额提升至5万字符/天。邮箱仅用于API身份识别，无需验证。',
            "百度翻译": "需填写API ID(AppID)和API Key(密钥)。https://fanyi-api.baidu.com",
            "有道翻译": "需填写API ID(应用ID)和API Key(应用密钥)。https://ai.youdao.com",
            "DeepL": "官方API需填写API Key(Auth Key)，免费版50万字符/月。https://www.deepl.com/pro-api",
            "必应翻译(免费)": "非官方网页API，无需密钥，自动获取token。高并发易被限流，建议并发4。",
            "微软翻译(Azure)": "需填写API Key(订阅密钥)和API ID(区域，如eastus)。Base URL可留空用全球端点。https://azure.microsoft.com/services/cognitive-services/translator",
            "腾讯翻译": "需填写API ID(SecretId)和API Key(SecretKey)。https://cloud.tencent.com/product/tmt",
            "阿里翻译": "需填写API ID(AccessKey ID)和API Key(AccessKey Secret)。https://www.aliyun.com/product/ai/alimt",
            "IBM Watson": "需填写API Key(IAM API Key)和Base URL(服务端点)。https://www.ibm.com/cloud/watson-language-translator",
            "OpenAI兼容(硅基流动/Groq/DeepSeek等)": "需填写Base URL、Model、API Key。可从上方快速预设选择，或点获取模型自动拉取。",
            "Google Gemini": "需填写API Key和Model。https://aistudio.google.com 免费层gemini-1.5-flash每分钟15次",
            "Ollama本地模型": "需安装Ollama并拉取模型。Base URL默认http://localhost:11434，Model如qwen2.5:7b。本地模型建议：并发设1、批量大小设0（关闭批量）、用7B以下小模型，否则容易超时。",
        }
        self.hint_label.config(text=hints.get(engine_name, ""))
        # 自动填入建议并发数（新手友好，主翻译+精修都设）
        rec = ENGINE_RECOMMENDED_WORKERS.get(engine_name)
        if rec:
            self.max_workers.set(rec)
            self.refine_workers.set(rec)
        # Ollama本地模型：默认小批量2条/批（提速30-50%，显存够用）
        if engine_name == "Ollama本地模型":
            self.batch_size.set(2)

    def _on_preset_change(self, event=None):
        preset_name = self.preset.get()
        if not preset_name or preset_name not in PRESETS:
            return
        p = PRESETS[preset_name]
        self.base_url.set(p["base_url"])
        self.model.set(p["model"])
        # 自动切换引擎
        if preset_name == "Ollama本地":
            self.api_engine.set(OllamaTranslator.name)
        else:
            self.api_engine.set(OpenAICompatTranslator.name)
        self._on_api_change()
        self.hint_label.config(text=p["hint"])
        # 预设的建议并发覆盖引擎默认值（主翻译+精修都设）
        rec = p.get("recommended_workers")
        if rec:
            self.max_workers.set(rec)
            self.refine_workers.set(rec)
        self._log(f"已加载预设: {preset_name}")

    # ===== 引擎B相关方法 =====

    def _on_engine_b_toggle(self):
        """切换引擎B启用状态，控制界面显示"""
        enabled = self.enable_engine_b.get()
        state = "normal" if enabled else "disabled"
        for child in self.engine_b_frame.winfo_children():
            if isinstance(child, (ttk.Entry, ttk.Combobox, ttk.Spinbox, ttk.Button)):
                if child not in [self.engine_b_preset_combo, self.engine_b_model_entry]:
                    child.config(state=state)
            elif isinstance(child, ttk.Label) and child != self.engine_b_hint:
                pass
        self.engine_b_preset_combo.config(state="readonly" if enabled else "disabled")
        self.engine_b_model_entry.config(state="normal" if enabled else "disabled")
        if enabled:
            self.engine_b_hint.config(text="引擎B已启用，建议与引擎A使用同一厂商同一模型以保证术语一致性")
        else:
            self.engine_b_hint.config(text="启用后可使用第二个API Key并行翻译，速度接近翻倍")

    def _on_engine_b_preset_change(self, event=None):
        """引擎B选择预设"""
        preset_name = self.engine_b_preset.get()
        if not preset_name or preset_name not in PRESETS:
            return
        p = PRESETS[preset_name]
        self.engine_b_base_url.set(p["base_url"])
        self.engine_b_model.set(p["model"])
        rec = p.get("recommended_workers")
        if rec:
            self.engine_b_workers.set(rec)
        # Ollama本地模型：小批量2条/批，并发设2
        base = p["base_url"]
        if 'localhost:11434' in base or '127.0.0.1:11434' in base:
            self.engine_b_batch_size.set(2)
            self.engine_b_workers.set(2)
        else:
            self.engine_b_batch_size.set(6)
        self._log(f"引擎B已加载预设: {preset_name}")

    def _copy_engine_a_to_b(self):
        """复制引擎A配置到引擎B"""
        self.engine_b_base_url.set(self.base_url.get())
        self.engine_b_model.set(self.model.get())
        self.engine_b_workers.set(self.max_workers.get())
        self.engine_b_batch_size.set(self.batch_size.get())
        # 如果引擎A是Ollama，确保引擎B也用Ollama推荐设置
        base = self.base_url.get()
        if 'localhost:11434' in base or '127.0.0.1:11434' in base:
            self.engine_b_batch_size.set(2)
            self.engine_b_workers.set(2)
        self._log("已复制引擎A配置到引擎B，请填写引擎B的API Key")

    def _toggle_engine_b_key(self):
        """切换引擎B Key显示/隐藏"""
        if self.engine_b_api_key_entry.cget('show') == '*':
            self.engine_b_api_key_entry.config(show='')
        else:
            self.engine_b_api_key_entry.config(show='*')

    def _get_translator_b(self):
        """获取引擎B的翻译器实例（支持Ollama本地模型，自动修正Base URL）"""
        base = self.engine_b_base_url.get().rstrip('/')
        # 从显示名提取真实模型名（显示名格式：模型名 [价格]）
        model_display = self.engine_b_model.get()
        model = self.engine_b_model_price_map.get(model_display, model_display)
        # Ollama: 确保base_url以/v1结尾（OpenAI兼容API端点需要/v1/chat/completions）
        if ('localhost:11434' in base or '127.0.0.1:11434' in base) and not base.endswith('/v1'):
            base = base + '/v1'
        return OpenAICompatTranslator(
            api_key=self.engine_b_api_key.get(),
            api_id="",
            model=model,
            base_url=base,
            email=""
        )

    def _test_connection_b(self):
        """测试引擎B连接（支持Ollama等无需API Key的本地模型）"""
        base = self.engine_b_base_url.get().rstrip('/')
        is_ollama = 'localhost:11434' in base or '127.0.0.1:11434' in base or 'ollama' in base.lower()
        if not self.engine_b_api_key.get() and not is_ollama:
            messagebox.showwarning("提示", "请先填写引擎B的API Key（Ollama本地模型无需Key）")
            return
        test_text = "Hello, world! This is a connection test."
        src = "en"
        tgt = self._get_lang_code(self.target_lang.get())
        if tgt == "auto":
            tgt = "zh-CN"
        self._log(f"正在测试引擎B连接...")
        try:
            translator = self._get_translator_b()
            result = translator.translate(test_text, src, tgt)
            self._log(f"引擎B连接成功！译文: {result}")
            messagebox.showinfo("连接成功", f"引擎B配置可用。\n译文: {result}")
        except Exception as e:
            err_msg = str(e)
            self._log(f"引擎B连接失败: {err_msg[:120]}")
            is_timeout = 'timed out' in err_msg.lower() or 'timeout' in err_msg.lower() or 'read timed out' in err_msg.lower()
            if is_timeout:
                hint = (f"引擎: 引擎B（双Key加速）\n"
                       f"错误: 请求超时（模型响应慢或暂时不可用）\n\n"
                       f"建议：\n"
                       f"• 换个模型试试（如 Qwen/Qwen2.5-7B-Instruct）\n"
                       f"• 稍等几分钟后重试\n"
                       f"• 检查API Key是否有效")
                messagebox.showwarning("连接超时", hint)
            else:
                messagebox.showerror("连接失败",
                                     f"引擎: 引擎B（双Key加速）\n"
                                     f"错误: {err_msg}\n\n"
                                     f"请检查引擎B的API Key、Base URL、模型名称和账户额度。")

    def _fetch_models_engine_b(self):
        """获取引擎B的模型列表（支持Ollama本地模型/api/tags）"""
        base = self.engine_b_base_url.get().rstrip('/')
        api_key = self.engine_b_api_key.get()
        if not base:
            messagebox.showwarning("提示", "请先填写引擎B的Base URL")
            return
        is_ollama = 'localhost:11434' in base or '127.0.0.1:11434' in base or 'ollama' in base.lower()
        if not api_key and not is_ollama:
            messagebox.showwarning("提示", "请先填写引擎B的API Key（Ollama本地模型无需Key）")
            return
        self._log(f"正在获取引擎B模型列表...")
        def run_fetch():
            try:
                import urllib.request as _req
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                if is_ollama:
                    # Ollama: 使用 /api/tags 获取本地模型列表
                    # base可能是 http://localhost:11434 或 http://localhost:11434/v1
                    ollama_base = base.replace('/v1', '').rstrip('/')
                    url = f"{ollama_base}/api/tags"
                    headers = {'User-Agent': 'Mozilla/5.0'}
                else:
                    url = f"{base}/models"
                    headers = {'User-Agent': 'Mozilla/5.0', 'Authorization': f'Bearer {api_key}'}
                req = _req.Request(url, headers=headers)
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                # Ollama本地模型：绕过系统代理，避免localhost请求被代理拦截返回502
                if is_ollama:
                    proxy_handler = _req.ProxyHandler({})
                    opener = _req.build_opener(proxy_handler, _req.HTTPSHandler(context=ctx))
                    resp = opener.open(req, timeout=30)
                else:
                    resp = _req.urlopen(req, context=ctx, timeout=30)
                data = json.loads(resp.read().decode('utf-8'))
                models = []
                if is_ollama:
                    # Ollama返回格式: {"models": [{"name": "qwen2.5:7b", ...}, ...]}
                    if isinstance(data, dict) and 'models' in data:
                        models = [m.get('name', '') for m in data['models'] if m.get('name')]
                elif isinstance(data, dict) and 'data' in data:
                    models = [m.get('id', '') for m in data['data'] if m.get('id')]
                # 硅基流动：从官方文档获取实时价格表
                if 'siliconflow' in base.lower():
                    live_prices_b = fetch_siliconflow_prices()
                    if live_prices_b:
                        MODEL_PRICES.update(live_prices_b)
                        self._log(f"引擎B实时价格更新: {len(live_prices_b)} 个模型（来自硅基流动官方文档）")
                # 排序：免费优先 → 模型大小(小=快) → 价格低 → 名称
                models = sorted(set(models), key=_model_sort_key)
                if not models:
                    self.root.after(0, lambda: messagebox.showinfo("提示", "未获取到模型列表"))
                    return
                self.engine_b_model_price_map = {}
                display_names = []
                for m in models:
                    price_str = self._format_model_price(m)
                    display = f"{m}  [{price_str}]"
                    self.engine_b_model_price_map[display] = m
                    display_names.append(display)
                def update_ui():
                    self.engine_b_model_entry['values'] = display_names
                    if self.engine_b_model.get() not in models:
                        # 优先Hunyuan-MT翻译专用模型，其次免费模型，最后第一个
                        recommended_b = None
                        for m in models:
                            if 'hunyuan-mt' in m.lower():
                                recommended_b = m
                                break
                        if not recommended_b:
                            free_models_b = [m for m in models if MODEL_PRICES.get(m) == 0]
                            recommended_b = free_models_b[0] if free_models_b else models[0]
                        self.engine_b_model.set(recommended_b)
                    messagebox.showinfo("获取成功", f"引擎B共 {len(models)} 个模型")
                self.root.after(0, update_ui)
                self._log(f"引擎B获取到 {len(models)} 个模型")
            except Exception as e:
                err_msg = str(e) if str(e) and str(e) != "None" else "请求失败（可能是系统代理干扰或Ollama未启动）"
                self.root.after(0, lambda: messagebox.showerror("获取失败", f"引擎B: {err_msg[:200]}\n\n请检查Base URL、API Key和网络连接。Ollama本地模型请确保ollama serve已启动。"))
        threading.Thread(target=run_fetch, daemon=True).start()

    def _setup_dnd(self):
        """设置拖拽支持：将JSON文件拖到窗口任意位置自动填入输入路径"""
        try:
            from tkinterdnd2 import DND_FILES
            # 递归注册所有子组件（拖放事件不会从子组件冒泡到root）
            def _register(widget):
                try:
                    widget.drop_target_register(DND_FILES)
                    widget.dnd_bind('<<Drop>>', self._on_drop)
                except Exception:
                    pass
                for child in widget.winfo_children():
                    _register(child)
            _register(self.root)
            self._log("拖拽已启用：将JSON文件拖入窗口任意位置即可自动填入路径")
        except Exception as e:
            self._log(f"拖拽启用失败: {e}。如需拖拽功能，请执行: pip install tkinterdnd2")

    def _on_drop(self, event):
        """处理文件拖放"""
        path = event.data.strip()
        # tkinterdnd2在路径含空格时会用大括号包裹，多个文件用空格分隔
        if path.startswith('{'):
            # 取第一个大括号包裹的路径
            end = path.find('}')
            if end > 0:
                path = path[1:end]
        else:
            # 无大括号，取第一个空格前的内容（多个文件时取第一个）
            if ' ' in path:
                first = path.split(' ')[0]
                if os.path.exists(first):
                    path = first
        if path and os.path.isfile(path):
            self.input_file.set(path)
            d = os.path.dirname(path)
            if not self.output_file.get() or os.path.basename(self.output_file.get()) == "ManualTransFile.json":
                self.output_file.set(os.path.join(d, "ManualTransFile.json"))
            if not self.progress_file.get() or os.path.basename(self.progress_file.get()) == "trans_progress.json":
                self.progress_file.set(os.path.join(d, "trans_progress.json"))
            self._log(f"已拖入文件: {path}")
        else:
            self._log(f"拖入文件无效: {path}")

    def _browse_input(self):
        f = filedialog.askopenfilename(filetypes=[("JSON文件", "*.json"), ("所有文件", "*.*")])
        if f:
            self.input_file.set(f)
            d = os.path.dirname(f)
            if not self.output_file.get() or os.path.basename(self.output_file.get()) == "ManualTransFile.json":
                self.output_file.set(os.path.join(d, "ManualTransFile.json"))
            if not self.progress_file.get() or os.path.basename(self.progress_file.get()) == "trans_progress.json":
                self.progress_file.set(os.path.join(d, "trans_progress.json"))

    def _browse_output(self):
        f = filedialog.asksaveasfilename(defaultextension=".json", filetypes=[("JSON文件", "*.json")])
        if f:
            self.output_file.set(f)

    def _clear_progress(self):
        pf = self.progress_file.get()
        if pf and os.path.exists(pf):
            if messagebox.askyesno("确认", f"确定删除进度文件？\n{pf}\n将清除所有已翻译进度。"):
                os.remove(pf)
                self.translated = {}
                self._log("已清除进度文件")

    def _get_lang_code(self, lang_str):
        if '(' in lang_str and ')' in lang_str:
            return lang_str.rsplit('(', 1)[-1].rstrip(')')
        return lang_str

    def _parse_google_proxies(self):
        """解析谷歌代理池配置字符串，返回地址列表"""
        raw = self.google_proxies.get().strip()
        if not raw:
            return []
        # 支持逗号、分号、换行分隔
        import re
        return [p.strip() for p in re.split(r'[,;\n]+', raw) if p.strip()]

    def _get_translator(self):
        engine_name = self.api_engine.get()
        engine_cls = next((t for t in TRANSLATORS if t.name == engine_name), GoogleTranslator)
        base = self.base_url.get().rstrip('/')
        # 注意：OllamaTranslator使用原生API /api/chat，不需要加/v1后缀
        # 从显示名提取真实模型名（显示名格式：模型名 [价格]）
        model_display = self.model.get()
        model = self.model_price_map.get(model_display, model_display)
        kwargs = dict(
            api_key=self.api_key.get(), api_id=self.api_id.get(),
            model=model, base_url=base,
            email=self.mymemory_email.get()
        )
        if engine_cls is GoogleTranslator:
            kwargs['proxy_list'] = self._parse_google_proxies()
        elif engine_cls is BingTranslator:
            # 必应翻译支持多个代理地址（逗号分隔），自动轮换
            bing_proxy_str = self.bing_proxy.get().strip()
            if bing_proxy_str:
                kwargs['proxy_list'] = [p.strip() for p in bing_proxy_str.split(',') if p.strip()]
        return engine_cls(**kwargs)

    def _load_existing(self):
        pf = self.progress_file.get()
        if pf and os.path.exists(pf):
            with open(pf, 'r', encoding='utf-8') as f:
                self.translated = json.load(f)
            self._log(f"加载已有翻译: {len(self.translated)} 条")
        else:
            self._log("没有找到进度文件")

    def _detect_incomplete(self):
        """检测翻译不全的条目：换行丢失、长度异常、未翻译残留、占位符丢失、错误模式等"""
        inf = self.input_file.get()
        if not inf or not os.path.exists(inf):
            messagebox.showwarning("提示", "请先选择输入文件")
            return
        try:
            with open(inf, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except Exception as e:
            messagebox.showerror("错误", f"读取JSON失败：{e}")
            return
        src = self._get_lang_code(self.source_lang.get())
        tgt = self._get_lang_code(self.target_lang.get())
        if tgt == "auto":
            tgt = "zh-CN"
        # 扫描所有已翻译条目
        issues = []  # [(原文, 译文, 问题描述, 质量分)]
        for source, target in data.items():
            if not source or not source.strip():
                continue
            if not target or not target.strip() or source == target:
                continue  # 未翻译的不算"翻译不全"
            reasons = []
            # 1. 换行符数量不匹配（原文有换行但译文没有，或数量差异大）
            src_nl = source.count('\n')
            tgt_nl = target.count('\n')
            if src_nl > 0 and tgt_nl == 0:
                reasons.append(f"换行丢失（原文{src_nl}个换行，译文0个）")
            elif src_nl > 0 and abs(src_nl - tgt_nl) >= max(2, src_nl // 2):
                reasons.append(f"换行数量异常（原文{src_nl}个，译文{tgt_nl}个）")
            # 2. 长度比异常（译文过短，可能内容丢失）
            s_len = len(source.strip())
            t_len = len(target.strip())
            if s_len > 10:
                ratio = t_len / s_len
                tgt_low = tgt.lower().split('-')[0]
                if tgt_low in ('zh', 'ja', 'ko'):
                    if ratio < 0.2:
                        reasons.append(f"译文过短（长度比{ratio:.2f}，可能内容丢失）")
                else:
                    if ratio < 0.25:
                        reasons.append(f"译文过短（长度比{ratio:.2f}，可能内容丢失）")
            # 3. 错误/拒绝模式
            target_lower = target.lower()
            error_patterns = ["i'm sorry", "i cannot", "i can't", "无法翻译", "翻译失败",
                              "sorry, i", "as an ai", "i don't understand", "[error]"]
            for pat in error_patterns:
                if pat in target_lower:
                    reasons.append(f"包含错误/拒绝模式（{pat}）")
                    break
            # 4. 占位符丢失
            placeholders = re.findall(r'\{[^}]+\}|%[sdif]|\$[a-zA-Z_][a-zA-Z0-9_]*|<[^>]+>', source)
            for ph in placeholders:
                if ph not in target:
                    reasons.append(f"占位符丢失（{ph}）")
                    break
            # 5. 未翻译残留（目标为中文时检测大段英文）
            if tgt_low in ('zh', 'ja', 'ko') and s_len > 20:
                eng_words = re.findall(r'[a-zA-Z]{4,}', target)
                normal_keeps = {'hello', 'world', 'game', 'player', 'level', 'quest', 'item',
                               'skill', 'magic', 'attack', 'defense', 'health', 'mana', 'gold',
                               'experience', 'boss', 'enemy', 'npc', 'dialog', 'menu', 'option',
                               'setting', 'save', 'load', 'start', 'pause', 'quit', 'yes', 'no'}
                suspicious = [w for w in eng_words if w.lower() not in normal_keeps]
                if len(suspicious) >= 5:
                    reasons.append(f"大量未翻译英文残留（{len(suspicious)}个可疑单词）")
            # 5b. 未翻译日语残留（目标为中文时检测日语假名）
            if tgt_low in ('zh', 'zh-cn', 'zh-tw') and s_len > 10:
                # 统计译文中的日语假名（平假名+片假名）
                jp_kana = sum(1 for c in target if '\u3040' <= c <= '\u309f' or '\u30a0' <= c <= '\u30ff')
                # 统计原文中的日语假名（判断源语言是否为日语）
                src_jp = sum(1 for c in source if '\u3040' <= c <= '\u309f' or '\u30a0' <= c <= '\u30ff')
                if src_jp > 0 and jp_kana >= 3:
                    # 原文是日语，译文中仍有大量假名，可能未翻译
                    reasons.append(f"未翻译日语残留（译文中{jp_kana}个假名）")
                elif jp_kana >= 5:
                    # 译文中有较多假名，可能残留
                    reasons.append(f"疑似日语残留（译文中{jp_kana}个假名）")
            # 6. 质量评分（复用精修模式的评估）
            try:
                q = self._assess_quality(source, target, src, tgt)
            except Exception:
                q = 50
            if q < 40:
                reasons.append(f"质量评分低（{q}/100）")
            if reasons:
                issues.append((source, target, "；".join(reasons), q))
        if not issues:
            messagebox.showinfo("检测完成", "未发现翻译不全的条目，所有已翻译条目质量正常。")
            return
        # 显示检测结果对话框
        self._show_incomplete_dialog(issues, data, inf)

    def _show_incomplete_dialog(self, issues, data, inf):
        """显示翻译不全条目检测结果对话框，支持勾选后清空译文重新翻译"""
        dlg = tk.Toplevel(self.root)
        dlg.title(f"检测到 {len(issues)} 条翻译不全")
        dlg.geometry("900x600")
        dlg.transient(self.root)
        dlg.grab_set()
        # 说明
        ttk.Label(dlg, text=f"共检测到 {len(issues)} 条可能翻译不全的条目。勾选后可清空译文，重新翻译。",
                  font=("微软雅黑", 10)).pack(anchor=tk.W, padx=10, pady=(10, 5))
        # 列表区（带滚动条）
        list_frame = ttk.Frame(dlg)
        list_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        canvas = tk.Canvas(list_frame)
        scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=canvas.yview)
        scroll_frame = ttk.Frame(canvas)
        scroll_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=scroll_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        # 全选变量
        select_all_var = tk.BooleanVar(value=True)
        check_vars = []
        # 表头
        header = ttk.Frame(scroll_frame)
        header.pack(fill=tk.X, padx=5, pady=2)
        ttk.Checkbutton(header, variable=select_all_var,
                       command=lambda: [v.set(select_all_var.get()) for v in check_vars]).pack(side=tk.LEFT)
        ttk.Label(header, text="原文（前80字）", width=40, font=("微软雅黑", 9, "bold")).pack(side=tk.LEFT, padx=5)
        ttk.Label(header, text="译文（前80字）", width=40, font=("微软雅黑", 9, "bold")).pack(side=tk.LEFT, padx=5)
        ttk.Label(header, text="问题", width=30, font=("微软雅黑", 9, "bold")).pack(side=tk.LEFT, padx=5)
        ttk.Separator(scroll_frame, orient="horizontal").pack(fill=tk.X, pady=2)
        # 填充条目
        for source, target, reason, q in issues:
            row = ttk.Frame(scroll_frame)
            row.pack(fill=tk.X, padx=5, pady=1)
            var = tk.BooleanVar(value=True)
            check_vars.append(var)
            ttk.Checkbutton(row, variable=var).pack(side=tk.LEFT)
            src_display = source.replace('\n', '\\n')[:80]
            tgt_display = target.replace('\n', '\\n')[:80]
            ttk.Label(row, text=src_display, width=40, anchor="w",
                     font=("微软雅黑", 8), foreground="#333").pack(side=tk.LEFT, padx=5)
            ttk.Label(row, text=tgt_display, width=40, anchor="w",
                     font=("微软雅黑", 8), foreground="#666").pack(side=tk.LEFT, padx=5)
            ttk.Label(row, text=reason, width=30, anchor="w",
                     font=("微软雅黑", 8), foreground="#d32f2f", wraplength=200).pack(side=tk.LEFT, padx=5)
        # 底部按钮
        btn_frame = ttk.Frame(dlg)
        btn_frame.pack(fill=tk.X, padx=10, pady=10)
        def _clear_selected():
            selected = [i for i, v in enumerate(check_vars) if v.get()]
            if not selected:
                messagebox.showinfo("提示", "请先勾选要重新翻译的条目", parent=dlg)
                return
            if not messagebox.askyesno("确认", f"确定清空选中的 {len(selected)} 条译文？\n清空后点击「开始翻译」即可重新翻译这些条目。", parent=dlg):
                return
            # 清空选中条目的译文
            for idx in selected:
                source = issues[idx][0]
                data[source] = source  # 恢复为原文（未翻译状态）
                if source in self.translated:
                    del self.translated[source]
            # 保存回JSON文件
            try:
                with open(inf, 'w', encoding='utf-8') as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                # 保存进度文件
                pf = self.progress_file.get()
                if pf:
                    with open(pf, 'w', encoding='utf-8') as f:
                        json.dump(self.translated, f, ensure_ascii=False, indent=2)
                self._log(f"已清空 {len(selected)} 条翻译不全的条目译文，可重新翻译")
                messagebox.showinfo("完成", f"已清空 {len(selected)} 条译文。\n点击「开始翻译」即可重新翻译这些条目。", parent=dlg)
                dlg.destroy()
            except Exception as e:
                messagebox.showerror("错误", f"保存失败：{e}", parent=dlg)
        ttk.Button(btn_frame, text="全选", command=lambda: [v.set(True) for v in check_vars]).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="全不选", command=lambda: [v.set(False) for v in check_vars]).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="清空选中译文并重新翻译", command=_clear_selected).pack(side=tk.RIGHT, padx=5)
        ttk.Button(btn_frame, text="关闭", command=dlg.destroy).pack(side=tk.RIGHT, padx=5)

    def _show_stats(self):
        inf = self.input_file.get()
        if not inf or not os.path.exists(inf):
            messagebox.showwarning("提示", "请先选择输入文件")
            return
        with open(inf, 'r', encoding='utf-8') as f:
            data = json.load(f)
        diff = sum(1 for k, v in data.items() if k != v)
        messagebox.showinfo("统计", f"总条目: {len(data)}\n已翻译: {diff}\n未翻译: {len(data)-diff}\n进度文件: {len(self.translated)} 条")

    def _classify_error(self, err):
        """分类错误：rate_limit(限流) / timeout(超时) / auth(授权) / other"""
        msg = str(err).lower()
        if any(k in msg for k in ['429', 'rate limit', 'too many requests', '限流', 'quota', '频率',
                                    'sorry', 'blocked', 'too many', 'unusual traffic', 'your computer',
                                    'service unavailable', '503', 'temporarily unavailable']):
            return 'rate_limit'
        if any(k in msg for k in ['timeout', 'timed out', '超时', 'connection reset', 'connection refused', '网络']):
            return 'timeout'
        if any(k in msg for k in ['401', '403', 'unauthorized', 'forbidden', 'auth', '密钥', '授权', 'invalid key', 'api key']):
            return 'auth'
        return 'other'

    def _get_rate_state(self, engine_name):
        """获取指定引擎的背压状态（不存在则初始化）"""
        with self._rate_lock:
            if engine_name not in self.rate_state:
                self.rate_state[engine_name] = {
                    "cooldown": 0.0,
                    "429_timestamps": [],
                    "success_count": 0,
                    "total_count": 0,
                }
            return self.rate_state[engine_name]

    def _apply_rate_cooldown(self, engine_name, is_retry):
        """应用限流冷却，返回需要sleep的秒数（0表示不需要等待）"""
        state = self._get_rate_state(engine_name)
        cd = state["cooldown"]
        if cd <= 0:
            return 0
        # 冷却>2秒时，首次请求也受影响（严重限流时保护API）
        # 冷却<=2秒时，只影响重试（不拖慢正常翻译）
        if cd <= 2 and not is_retry:
            return 0
        # 抖动范围±10%（缩小波动）
        return cd * (0.9 + random.random() * 0.2)

    def _record_rate_success(self, engine_name):
        """记录翻译成功，按成功率衰减冷却（避免太激进）"""
        state = self._get_rate_state(engine_name)
        state["success_count"] += 1
        state["total_count"] += 1
        # 每10次请求检查一次成功率，成功率>90%才衰减
        if state["total_count"] >= 10:
            rate = state["success_count"] / state["total_count"]
            if rate > 0.9 and state["cooldown"] > 0:
                state["cooldown"] = max(0, state["cooldown"] - 0.5)
            # 重置计数器
            state["success_count"] = 0
            state["total_count"] = 0
        # 成功路径也清理过期的429记录（避免旧记录导致误判）
        now = time.time()
        state["429_timestamps"] = [t for t in state["429_timestamps"] if now - t < 60]

    def _record_rate_limit(self, engine_name):
        """记录429限流，指数级增长冷却（上限30秒）"""
        state = self._get_rate_state(engine_name)
        now = time.time()
        state["429_timestamps"].append(now)
        # 清理过期记录
        state["429_timestamps"] = [t for t in state["429_timestamps"] if now - t < 60]
        # 60秒内429超过5次才增加冷却（降低敏感度）
        if len(state["429_timestamps"]) > 5:
            # 指数级增长：×1.5，上限30秒
            state["cooldown"] = min(state["cooldown"] * 1.5 + 0.5, 30.0)
            # 触发冷却后清空429记录，避免重复触发
            state["429_timestamps"] = []

    def _reset_rate_state(self, engine_name=None):
        """重置背压状态（engine_name=None则重置所有引擎）"""
        with self._rate_lock:
            if engine_name:
                if engine_name in self.rate_state:
                    del self.rate_state[engine_name]
            else:
                self.rate_state = {}

    def _protect_text(self, text):
        """将文本中的换行符和占位符替换为唯一标记，返回(替换后文本, 映射列表)
        换行符始终保护（防止LLM翻译时丢失换行后的内容），占位符保护可选"""
        mapping = []
        # 1. 始终保护换行符：统一\r\n和\r为\n，再用特殊标记替换
        #    用〔NL{idx}〕格式（中文方括号），LLM和翻译API不易修改或丢弃
        text = text.replace('\r\n', '\n').replace('\r', '\n')
        nl_count = [0]
        def _nl_replacer(m):
            marker = f"〔NL{nl_count[0]}〕"
            mapping.append((marker, '\n'))
            nl_count[0] += 1
            return marker
        text = re.sub(r'\n', _nl_replacer, text)
        # 2. 占位符保护（可选，由用户开关控制）
        if not self.protect_placeholders.get():
            return text, mapping
        try:
            pattern = self.protect_patterns.get()
            matches = list(re.finditer(pattern, text))
        except Exception:
            return text, mapping
        if not matches:
            return text, mapping
        result = []
        last_end = 0
        ph_count = 0
        for m in matches:
            result.append(text[last_end:m.start()])
            marker = f"〔PHX{ph_count}〕"
            result.append(marker)
            mapping.append((marker, m.group()))
            ph_count += 1
            last_end = m.end()
        result.append(text[last_end:])
        return ''.join(result), mapping

    def _restore_text(self, text, mapping):
        """将译文中的标记还原为原始占位符（支持大小写不敏感匹配），并验证还原结果"""
        if not mapping:
            return text
        result = text
        for marker, original in mapping:
            # 先精确替换
            result = result.replace(marker, original)
            # 再尝试大小写不敏感替换（防止LLM修改了标记大小写）
            pattern = re.compile(re.escape(marker), re.IGNORECASE)
            result = pattern.sub(original, result)
        # 验证1：检查是否还有未还原的标记（〔NL数字〕或〔PHX数字〕）
        leftover = re.findall(r'〔(?:NL|PHX)\d+〕', result)
        if leftover:
            # 标记未被还原，可能是LLM丢弃了标记的一部分
            for marker, original in mapping:
                if marker in leftover:
                    result = result.replace(marker, original)
        # 验证2：换行数对比（标记可能被翻译API完全丢弃）
        expected_nl = sum(1 for _, orig in mapping if orig == '\n')
        actual_nl = result.count('\n')
        if expected_nl > 0 and actual_nl < expected_nl:
            # 译文换行少于原文，补回缺失的换行（保守策略，追加到末尾）
            missing = expected_nl - actual_nl
            result = result + '\n' * missing
        return result

    def _should_skip_translation(self, text):
        """判断文本是否不需要翻译（纯数字、URL、路径、邮箱、占位符等），是则返回True。
        保守策略：只跳过明显不需要翻译的，短英文单词和代码标识符不跳过，让API处理。"""
        if not text or not text.strip():
            return True
        t = text.strip()
        t_lower = t.lower()
        # 1. 路径/文件：包含/且不含空格和中日韩文字，或以常见扩展名结尾
        if '/' in t and ' ' not in t and not re.search(r'[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]', t):
            return True
        if re.search(r'\.(png|jpg|jpeg|gif|bmp|webp|svg|ico|mp3|wav|ogg|flac|aac|mp4|avi|mkv|mov|wmv|flv|json|xml|txt|csv|py|lua|js|ts|html|css|php|java|c|cpp|h|hpp|cs|go|rs|rb|pl|sh|bat|ps1|exe|dll|so|dylib|bin|dat|db|sqlite|zip|rar|7z|tar|gz|bz2|xz|pdf|doc|docx|xls|xlsx|ppt|pptx|odt|ods|odp|epub|mobi|azw3|ttf|otf|woff|woff2|eot)$', t_lower):
            return True
        # 2. URL：包含://或www.
        if '://' in t or t.startswith('www.'):
            return True
        # 3. 邮箱：包含@
        if '@' in t and '.' in t and ' ' not in t:
            return True
        # 4. 纯数字/坐标/十六进制：不含字母和中日韩文字
        if not re.search(r'[a-zA-Z\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]', t):
            return True
        if len(t) > 15 and re.match(r'^[a-fA-F0-9]+$', t):
            return True  # 十六进制/哈希
        # 5. 占位符：包含PHX、{}、%、$、<>等
        if 'PHX' in t or re.search(r'\{[^}]*\}', t) or re.search(r'%[sdif]', t) or re.search(r'\$[a-zA-Z_]', t):
            return True
        if re.match(r'^[\(\{\<\[%\$][^\)\}\>\]]*[\)\}\>\]%]$', t):
            return True
        return False

    def _translate_one(self, text, translator, src, tgt):
        if not text or not text.strip():
            return text
        # 不需要翻译的文本（纯数字、URL、路径、邮箱、占位符等）直接返回原文，不调用API
        if self._should_skip_translation(text):
            return text
        # 翻译缓存：相同文本+语言方向直接返回，避免重复翻译（游戏UI文本重复率高）
        cache_key = f"{src}|{tgt}|{text}"
        if cache_key in self.translation_cache:
            return self.translation_cache[cache_key]
        chunk_size = self.chunk_size.get()
        max_retry = self.max_retry.get()

        def _do_translate(t):
            # 本地Ollama模型不搞任何节流/背压/冷却
            engine_name = getattr(translator, 'name', '')
            is_local = engine_name == 'Ollama本地模型'
            for attempt in range(max_retry + 1):
                # 暂停检查
                while self.is_paused and not self.should_stop:
                    time.sleep(0.3)
                if self.should_stop:
                    return t
                try:
                    result = translator.translate(t, src, tgt)
                    # 检查结果是否有效（非空）
                    if result:
                        return result
                    # 返回空视为失败
                    self.errors += 1
                    if self.errors <= 20:
                        text_preview = t[:80].replace('\n', '\\n')
                        self._log(f"失败({self.errors}) [empty]: 模型返回空 | 文本: {text_preview}")
                    return t
                except Exception as e:
                    err_type = self._classify_error(e)
                    # 授权错误不重试（重试也没用）
                    if err_type == 'auth':
                        self.errors += 1
                        self._log(f"授权失败，跳过: {str(e)[:80]}")
                        return t
                    if attempt >= max_retry:
                        self.errors += 1
                        # 记录前20条失败原因，便于排查（包含文本预览和错误类型）
                        if self.errors <= 20:
                            text_preview = t[:80].replace('\n', '\\n')
                            err_detail = str(e) if str(e) else type(e).__name__
                            self._log(f"失败({self.errors}) [{err_type}]: {err_detail[:150]} | 文本: {text_preview}")
                        return t
                    # 指数退避 + 随机抖动：避免多线程同时苏醒同时重试（雷鸣群效应）
                    # 本地模型不搞指数退避，立即重试
                    if is_local:
                        wait = 0.1
                    else:
                        wait = (2 ** attempt) * (0.5 + random.random())
                        if err_type == 'rate_limit':
                            wait *= 2
                            wait += 1  # 限流额外加基础延迟，避免苏醒后再次挤爆
                        elif err_type == 'timeout':
                            wait = max(wait, 3)
                    if wait > 0:
                        time.sleep(wait)
            return t

        # 分段前先统一换行符，避免\r\n被分段截断导致双换行
        text = text.replace('\r\n', '\n').replace('\r', '\n')
        if len(text) > chunk_size:
            chunks = [text[i:i+chunk_size] for i in range(0, len(text), chunk_size)]
            result = ''
            for chunk in chunks:
                protected, mapping = self._protect_text(chunk)
                r = _do_translate(protected)
                try:
                    r = self._restore_text(r, mapping)
                except Exception:
                    for marker, original in mapping:
                        r = r.replace(marker, original)
                result += r
                # 更新上下文（仅LLM引擎有效）
                if r != chunk:
                    translator.context.append((chunk, r))
                    if len(translator.context) > 10:
                        translator.context = translator.context[-10:]
            final = result if result and result != text else text
            self.translation_cache[cache_key] = final
            return final

        protected, mapping = self._protect_text(text)
        result = _do_translate(protected)
        try:
            result = self._restore_text(result, mapping)
        except Exception as restore_err:
            # 还原失败时用最朴素方式替换标记，避免整条丢失
            for marker, original in mapping:
                result = result.replace(marker, original)
            self._log(f"⚠ 标记还原异常，已用朴素方式替换: {str(restore_err)[:80]}")
        if result != text:
            translator.context.append((text, result))
            if len(translator.context) > 10:
                translator.context = translator.context[-10:]
        # 存入缓存
        self.translation_cache[cache_key] = result
        return result

    def _start_translate(self):
        inf = self.input_file.get()
        if not inf or not os.path.exists(inf):
            messagebox.showwarning("提示", "请先选择输入文件")
            return
        src = self._get_lang_code(self.source_lang.get())
        tgt = self._get_lang_code(self.target_lang.get())
        if src == tgt:
            messagebox.showwarning("提示", "源语言和目标语言不能相同")
            return

        # 检查引擎B配置
        use_engine_b = self.enable_engine_b.get()
        if use_engine_b:
            if not self.engine_b_api_key.get():
                messagebox.showwarning("提示", "引擎B已启用但未填写API Key")
                return
            if not self.engine_b_base_url.get():
                messagebox.showwarning("提示", "引擎B已启用但未填写Base URL")
                return
            if not self.engine_b_model.get():
                messagebox.showwarning("提示", "引擎B已启用但未选择模型")
                return

        self.is_running = True
        self.should_stop = False
        self.is_paused = False
        self.count = 0
        self.errors = 0
        self.translated_chars = 0
        self.start_time = time.time()
        # 开始翻译：重置所有引擎的背压状态（避免上次翻译的冷却残留）
        self._reset_rate_state()
        self.start_btn.config(state="disabled")
        self.pause_btn.config(state="normal", text="暂停")
        self.stop_btn.config(state="normal")

        pf = self.progress_file.get()
        if pf and os.path.exists(pf):
            with open(pf, 'r', encoding='utf-8') as f:
                self.translated = json.load(f)
            self._log(f"加载已有翻译: {len(self.translated)} 条")

        try:
            with open(inf, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            self._log(f"❌ 输入文件不是有效的JSON: {e}")
            messagebox.showerror("文件格式错误",
                f"输入文件不是有效的JSON格式：\n{inf}\n\n"
                f"错误: {e}\n\n"
                f"请选择游戏文本JSON文件（如 ManualTransFile.json），不要拖入.py/.txt等其他格式文件。")
            self.start_btn.config(state="normal")
            self.pause_btn.config(state="disabled")
            self.stop_btn.config(state="disabled")
            self.is_running = False
            return

        to_translate = [k for k in data if k not in self.translated]
        self.to_translate = to_translate  # 保存供结束时统计实际失败数
        self.total_chars = sum(len(k) for k in to_translate)
        self._log(f"总条目: {len(data)}, 已翻译: {len(self.translated)}, 待翻译: {len(to_translate)}")
        self._log(f"待翻译总字符数: {self.total_chars}")

        if use_engine_b:
            self._log(f"双引擎模式: A={self.model.get()}({self.max_workers.get()}并发), B={self.engine_b_model.get()}({self.engine_b_workers.get()}并发)")
        else:
            self._log(f"单引擎: {self.api_engine.get()}, 方向: {src}->{tgt}, 并发: {self.max_workers.get()}")

        # 费用预估
        cost_info = self._estimate_cost(self.total_chars)
        if cost_info:
            self._log(f"费用预估: {cost_info}")
        if use_engine_b:
            self._log(f"引擎B费用预估: 与引擎A相同（双Key并行，费用翻倍）")

        if not to_translate:
            self._log("全部已翻译，直接生成输出")
            self._save_output(data)
            self._finish()
            return

        self.progress_bar["maximum"] = len(to_translate)
        self.progress_bar["value"] = 0

        if use_engine_b:
            threading.Thread(target=self._run_translate_dual, args=(to_translate, data, src, tgt), daemon=True).start()
        else:
            threading.Thread(target=self._run_translate, args=(to_translate, data, src, tgt), daemon=True).start()

    def _parse_batch_result(self, raw_output, expected_count):
        """解析LLM批量翻译输出，返回译文列表，失败返回None。支持多种编号格式和Markdown。"""
        if not raw_output:
            return None
        # 预处理：移除Markdown代码块标记
        text = raw_output
        text = re.sub(r'```[\w]*\n?', '', text)
        text = text.replace('```', '')
        # 按行解析，支持多种编号格式：1. 1、1) 1：1- 1] 1】等
        results = []
        for line in text.split('\n'):
            line = line.strip()
            if not line:
                continue
            # 格式1: 1. 译文 / 1、译文 / 1)译文 等
            m = re.match(r'^\s*(\d+)\s*[\.\、\):：\-\]】]\s*(.+)$', line)
            # 格式2: 编号: 1 译文: xxx / 序号: 1 翻译: xxx
            if not m:
                m = re.match(r'^(?:编号|序号|No\.?)\s*[:：.\-]?\s*(\d+)\s*(?:译文|翻译|translated)?\s*[:：.\-]?\s*(.+)$', line, re.IGNORECASE)
            if m:
                idx = int(m.group(1))
                content = m.group(2).strip()
                # 移除可能的引号包裹
                if (content.startswith('"') and content.endswith('"')) or \
                   (content.startswith('"') and content.endswith('"')) or \
                   (content.startswith("'") and content.endswith("'")):
                    content = content[1:-1].strip()
                while len(results) < idx - 1:
                    results.append(None)
                if len(results) == idx - 1:
                    results.append(content)
                elif idx - 1 < len(results):
                    results[idx - 1] = content
                else:
                    results.append(content)
        # 检查数量
        valid = [r for r in results if r is not None]
        if len(valid) == expected_count:
            return valid
        # 按行解析失败，尝试按空行分隔（无编号的情况）
        if len(valid) == 0:
            blocks = re.split(r'\n\s*\n', text.strip())
            blocks = [b.strip() for b in blocks if b.strip()]
            if len(blocks) == expected_count:
                return blocks
        # 编号不连续但数量够，尝试按出现顺序返回
        if len(valid) >= expected_count:
            return valid[:expected_count]
        return None

    def _translate_batch_worker(self, batch, translator, src, tgt):
        """批量翻译一个批次，返回 [(原文,译文),...]，失败返回空列表。带3次指数退避重试。"""
        # 保护每条文本中的换行符（防止LLM丢失换行后的内容）
        protected_batch = []
        mappings = []
        for text in batch:
            protected, mapping = self._protect_text(text)
            protected_batch.append(protected)
            mappings.append(mapping)
        max_retry = 3
        for attempt in range(max_retry + 1):
            if self.should_stop:
                return []
            try:
                raw = translator.translate_batch(protected_batch, src, tgt)
                # 传统引擎返回列表，LLM引擎返回带编号字符串
                if isinstance(raw, list):
                    parsed = raw
                else:
                    parsed = self._parse_batch_result(raw, len(protected_batch))
                if parsed and len(parsed) == len(protected_batch):
                    # 恢复每条译文中的换行符和占位符
                    restored = []
                    for i, r in enumerate(parsed):
                        if i < len(mappings):
                            r = self._restore_text(r, mappings[i])
                        restored.append(r)
                    return list(zip(batch, restored))
                # 解析失败或数量不对，不重试（重试也会得到同样格式），直接降级
                break
            except Exception as e:
                if attempt >= max_retry:
                    break
                err_str = str(e).lower()
                wait = 2 ** attempt
                if '429' in err_str or 'rate' in err_str or 'limit' in err_str:
                    wait *= 2  # 限流加倍退避
                time.sleep(wait)
        # 批量失败，降级为单条翻译（单条有自己的重试机制）
        results = []
        for text in batch:
            try:
                r = self._translate_one(text, translator, src, tgt)
                if r and r != text:
                    results.append((text, r))
            except Exception:
                pass
        return results

    def _run_translate(self, to_translate, data, src, tgt):
        # 精修模式：第一阶段使用独立的预翻译引擎和并发
        if self.refine_mode.get():
            engine_name = self.refine_pre_engine.get()
            engine_cls = next((t for t in TRANSLATORS if t.name == engine_name), GoogleTranslator)
            # 预翻译API参数：优先用独立配置，不填则用主API设置页
            pre_key = self.pre_api_key.get().strip() or self.api_key.get()
            pre_id = self.pre_api_id.get().strip() or self.api_id.get()
            pre_base = self.pre_base_url.get().strip() or self.base_url.get()
            pre_model = self.pre_model.get().strip() or self.model.get()
            pre_email = self.pre_email.get().strip() or self.mymemory_email.get()
            # 只有GoogleTranslator和BingTranslator接受proxy_list
            if engine_cls is GoogleTranslator:
                translator = engine_cls(
                    api_key=pre_key, api_id=pre_id,
                    model=pre_model, base_url=pre_base,
                    email=pre_email,
                    proxy_list=self._parse_google_proxies()
                )
            elif engine_cls is BingTranslator:
                # 必应翻译支持多个代理地址（逗号分隔），自动轮换
                bing_proxy_str = self.bing_proxy.get().strip()
                bing_proxy_list = [p.strip() for p in bing_proxy_str.split(',') if p.strip()] if bing_proxy_str else None
                translator = engine_cls(
                    api_key=pre_key, api_id=pre_id,
                    model=pre_model, base_url=pre_base,
                    email=pre_email,
                    proxy_list=bing_proxy_list
                )
            else:
                translator = engine_cls(
                    api_key=pre_key, api_id=pre_id,
                    model=pre_model, base_url=pre_base,
                    email=pre_email
                )
            workers = self.refine_pre_workers.get()
            use_independent = bool(self.pre_api_key.get().strip() or self.pre_api_id.get().strip() or self.pre_base_url.get().strip() or self.pre_model.get().strip() or self.pre_email.get().strip())
            config_src = "独立配置" if use_independent else "API设置页"
            self._log(f"【精修模式·第一阶段】引擎={engine_name}, 并发={workers}（{config_src}）")
        else:
            translator = self._get_translator()
            engine_name = self.api_engine.get()
            workers = self.max_workers.get()

        # Ollama本地模型预热：提前加载模型到内存，避免第一条翻译因加载超时
        if engine_name == "Ollama本地模型" and hasattr(translator, 'warmup'):
            try:
                self._log("正在预热Ollama模型...")
                translator.warmup()
                self._log("Ollama模型预热完成")
            except Exception as e:
                self._log(f"Ollama模型预热失败: {e}")

        start_time = time.time()
        lock = threading.Lock()
        last_progress_time = [time.time()]
        last_done = [0]

        # 判断是否启用批量翻译（引擎支持且batch_size>0，Ollama本地模型禁用批量避免卡住）
        is_ollama = getattr(translator, 'name', '') == 'Ollama本地模型'
        use_batch = getattr(translator, 'supports_batch', False) and self.batch_size.get() > 0 and not is_ollama
        if use_batch:
            bs = min(self.batch_size.get(), 6)  # 强制上限6条，限速环境下宁小勿大
            max_chars = 2000  # 每批最大字符数（严禁超过3000，避免单条请求拖死并发槽位）
            # 动态分批：短文本按字符数合并，长文本(>500字)单独翻译
            tasks = []
            short_texts = []
            for text in to_translate:
                if len(text) > 500:
                    tasks.append(('single', text))
                else:
                    short_texts.append(text)
            # 按字符数动态分批
            current_batch = []
            current_chars = 0
            batch_count = 0
            for text in short_texts:
                if current_batch and (current_chars + len(text) > max_chars or len(current_batch) >= bs):
                    tasks.append(('batch', current_batch))
                    batch_count += 1
                    current_batch = [text]
                    current_chars = len(text)
                else:
                    current_batch.append(text)
                    current_chars += len(text)
            if current_batch:
                tasks.append(('batch', current_batch))
                batch_count += 1
            self._log(f"批量模式：{len(short_texts)}条短文本分为{batch_count}批（按字符数≤{max_chars}/批，最多{bs}条/批），{len(to_translate)-len(short_texts)}条长文本单独翻译")
        else:
            tasks = [('single', text) for text in to_translate]

        def worker(task):
            if self.should_stop:
                return []
            task_type, payload = task
            if task_type == 'batch':
                return self._translate_batch_worker(payload, translator, src, tgt)
            else:
                result = self._translate_one(payload, translator, src, tgt)
                if result:
                    return [(payload, result)]
                return []

        # 心跳监控：60秒无进度则提示
        def watchdog():
            while self.is_running and not self.should_stop:
                time.sleep(30)
                if not self.is_running or self.should_stop:
                    break
                elapsed_since_progress = time.time() - last_progress_time[0]
                if elapsed_since_progress > 60 and last_done[0] > 0:
                    self._log(f"⚠ 已 {int(elapsed_since_progress)}秒无新进度，可能API限流或响应慢，正在重试中...")
                    last_progress_time[0] = time.time()  # 重置避免重复提示

        threading.Thread(target=watchdog, daemon=True).start()

        total_entries = len(to_translate)
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(worker, task): task for task in tasks}
            done = 0
            for future in as_completed(futures):
                if self.should_stop:
                    break
                try:
                    results = future.result()
                    for text, result in results:
                        if result:
                            with lock:
                                self.translated[text] = result
                                self.count += 1
                                self.translated_chars += len(text)
                except Exception:
                    self.errors += 1
                done += 1
                last_done[0] = done
                last_progress_time[0] = time.time()
                if done % 5 == 0 or done == len(tasks):
                    elapsed = time.time() - start_time
                    rate = self.count / elapsed if elapsed > 0 else 0
                    remain = (total_entries - self.count) / rate if rate > 0 else 0
                    self.root.after(0, self._update_progress, self.count, total_entries, rate, remain)
                if self.count % 200 == 0:
                    self._save_progress()

        self._save_progress()

        # 失败流转：因限流/超时失败的条目，改用2线程低并发重试（快车不受慢车拖累）
        failed = [text for text in to_translate if text not in self.translated]
        if failed and not self.should_stop:
            # 判断是否本地模型（Ollama），本地模型无限流，不需要等待
            base_local = self.base_url.get().lower()
            is_local_model = 'localhost:11434' in base_local or '127.0.0.1:11434' in base_local
            if is_local_model:
                self._log(f"【失败流转】{len(failed)} 条失败，本地模型直接重试...")
            else:
                # 谷歌翻译限流窗口更长（几分钟），冷却加倍到60秒
                is_google = self.api_engine.get() == "谷歌翻译(免费)"
                cooldown = 60 if is_google else 30
                self._log(f"【失败流转】{len(failed)} 条因限流/超时失败，等待{cooldown}秒让限流窗口过去...")
                for _ in range(cooldown):
                    if self.should_stop:
                        break
                    time.sleep(1)
            if not self.should_stop:
                self._log(f"【失败流转】开始用2线程低并发重试{len(failed)}条...")
                retry_workers = min(2, workers)
                with ThreadPoolExecutor(max_workers=retry_workers) as retry_executor:
                    retry_futures = {retry_executor.submit(worker, ('single', text)): text for text in failed}
                    for future in as_completed(retry_futures):
                        if self.should_stop:
                            break
                        try:
                            results = future.result()
                            for text, result in results:
                                if result:
                                    with lock:
                                        self.translated[text] = result
                                        self.count += 1
                                        self.translated_chars += len(text)
                        except Exception as e:
                            self.errors += 1
                            # 失败流转也记录前20条失败原因
                            if self.errors <= 20:
                                # 从future获取对应的文本
                                failed_text = ""
                                for ft, fut in retry_futures.items():
                                    if fut == future:
                                        failed_text = ft
                                        break
                                text_preview = failed_text[:80].replace('\n', '\\n') if failed_text else "未知"
                                err_detail = str(e) if str(e) else type(e).__name__
                                self._log(f"重试失败({self.errors}): {err_detail[:150]} | 文本: {text_preview}")
                self._save_progress()
                retry_success = sum(1 for text in failed if text in self.translated)
                self._log(f"【失败流转】重试完成：{retry_success}/{len(failed)} 条成功，{len(failed)-retry_success} 条仍失败")

        self._save_output(data)

        # 第二阶段：LLM精修
        if self.refine_mode.get() and not self.should_stop:
            self.root.after(0, lambda: self.status_label.config(text="进入第二阶段：评估译文质量并精修..."))
            self._log("=== 第二阶段：LLM精修开始 ===")
            self._run_refine_phase(data, src, tgt)
            self._save_output(data)

        self.root.after(0, self._finish)

    def _update_progress_dual(self, done, total, rate, remain, a_done, b_done, a_failed, b_failed):
        """双引擎进度更新"""
        self.progress_bar["value"] = done
        pause_str = " [已暂停]" if self.is_paused else ""
        self.status_label.config(
            text=f"进度: {done}/{total} ({done*100//total if total else 0}%) | "
                 f"速度: {rate:.1f}条/秒 | 预计剩余: {remain/60:.1f}分钟 | "
                 f"A:{a_done} B:{b_done} | 失败:A{a_failed}B{b_failed}{pause_str}"
        )

    def _run_translate_dual(self, to_translate, data, src, tgt):
        """双引擎并行翻译：奇偶分片，两个线程池同时跑"""
        translator_a = self._get_translator()
        translator_b = self._get_translator_b()

        workers_a = self.max_workers.get()
        workers_b = self.engine_b_workers.get()
        bs_a = min(self.batch_size.get(), 6)
        bs_b = min(self.engine_b_batch_size.get(), 6)

        # 奇偶分片
        tasks_a = []
        tasks_b = []
        for i, text in enumerate(to_translate):
            if i % 2 == 0:
                tasks_a.append(text)
            else:
                tasks_b.append(text)

        self._log(f"双引擎分片: A引擎 {len(tasks_a)} 条, B引擎 {len(tasks_b)} 条")

        lock = threading.Lock()
        start_time = time.time()
        last_progress_time = [time.time()]
        last_done = [0]
        engine_a_done = [0]
        engine_b_done = [0]
        engine_a_failed = [0]
        engine_b_failed = [0]

        def prepare_batches(texts, bs, max_chars=2000):
            """将文本列表按批量规则分片"""
            if bs <= 0:
                return [('single', t) for t in texts]
            result = []
            short_texts = []
            for text in texts:
                if len(text) > 500:
                    result.append(('single', text))
                else:
                    short_texts.append(text)
            current_batch = []
            current_chars = 0
            for text in short_texts:
                if current_batch and (current_chars + len(text) > max_chars or len(current_batch) >= bs):
                    result.append(('batch', current_batch))
                    current_batch = [text]
                    current_chars = len(text)
                else:
                    current_batch.append(text)
                    current_chars += len(text)
            if current_batch:
                result.append(('batch', current_batch))
            return result

        batches_a = prepare_batches(tasks_a, bs_a)
        batches_b = prepare_batches(tasks_b, bs_b)

        total_tasks = len(batches_a) + len(batches_b)
        total_entries = len(to_translate)

        # 心跳监控
        def watchdog():
            while self.is_running and not self.should_stop:
                time.sleep(30)
                if not self.is_running or self.should_stop:
                    break
                elapsed = time.time() - last_progress_time[0]
                if elapsed > 60 and last_done[0] > 0:
                    self._log(f"⚠ 已 {int(elapsed)}秒无新进度，可能API限流或响应慢...")
                    last_progress_time[0] = time.time()

        threading.Thread(target=watchdog, daemon=True).start()

        def worker_a(task):
            if self.should_stop:
                return []
            task_type, payload = task
            try:
                if task_type == 'batch':
                    results = self._translate_batch_worker(payload, translator_a, src, tgt)
                    with lock:
                        engine_a_done[0] += len(results)
                    return results
                else:
                    result = self._translate_one(payload, translator_a, src, tgt)
                    if result:
                        with lock:
                            engine_a_done[0] += 1
                        return [(payload, result)]
                    return []
            except Exception:
                with lock:
                    engine_a_failed[0] += 1
                return []

        def worker_b(task):
            if self.should_stop:
                return []
            task_type, payload = task
            try:
                if task_type == 'batch':
                    results = self._translate_batch_worker(payload, translator_b, src, tgt)
                    with lock:
                        engine_b_done[0] += len(results)
                    return results
                else:
                    result = self._translate_one(payload, translator_b, src, tgt)
                    if result:
                        with lock:
                            engine_b_done[0] += 1
                        return [(payload, result)]
                    return []
            except Exception:
                with lock:
                    engine_b_failed[0] += 1
                return []

        with ThreadPoolExecutor(max_workers=workers_a) as pool_a, ThreadPoolExecutor(max_workers=workers_b) as pool_b:
            futures_a = {pool_a.submit(worker_a, task): task for task in batches_a}
            futures_b = {pool_b.submit(worker_b, task): task for task in batches_b}

            done = 0
            all_futures = list(futures_a.keys()) + list(futures_b.keys())

            for future in as_completed(all_futures):
                if self.should_stop:
                    break
                try:
                    results = future.result()
                    for text, result in results:
                        if result:
                            with lock:
                                self.translated[text] = result
                                self.count += 1
                                self.translated_chars += len(text)
                except Exception:
                    self.errors += 1

                done += 1
                last_done[0] = done
                last_progress_time[0] = time.time()

                if done % 5 == 0 or done >= total_tasks:
                    elapsed = time.time() - start_time
                    rate = self.count / elapsed if elapsed > 0 else 0
                    remain = (total_entries - self.count) / rate if rate > 0 else 0
                    self.root.after(0, self._update_progress_dual,
                        self.count, total_entries, rate, remain,
                        engine_a_done[0], engine_b_done[0],
                        engine_a_failed[0], engine_b_failed[0])

                if self.count % 200 == 0:
                    self._save_progress()

        self._save_progress()

        # 失败流转：任一引擎失败的条目，用主引擎低并发重试
        failed = [text for text in to_translate if text not in self.translated]
        if failed and not self.should_stop:
            # 判断是否本地模型（Ollama），本地模型无限流，不需要等待
            base_a = self.base_url.get().lower()
            is_local = 'localhost:11434' in base_a or '127.0.0.1:11434' in base_a
            if is_local:
                self._log(f"【失败流转】{len(failed)} 条失败，本地模型直接重试...")
            else:
                is_google = self.api_engine.get() == "谷歌翻译(免费)"
                cooldown = 60 if is_google else 30
                self._log(f"【失败流转】{len(failed)} 条失败，等待{cooldown}秒让限流窗口过去...")
                for _ in range(cooldown):
                    if self.should_stop:
                        break
                    time.sleep(1)
            if not self.should_stop:
                self._log(f"【失败流转】开始用主引擎低并发重试{len(failed)}条...")
                retry_workers = min(2, workers_a)
                with ThreadPoolExecutor(max_workers=retry_workers) as retry_pool:
                    retry_futures = {retry_pool.submit(lambda t: self._translate_one(t, translator_a, src, tgt), text): text for text in failed}
                    for future in as_completed(retry_futures):
                        if self.should_stop:
                            break
                        try:
                            result = future.result()
                            text = retry_futures[future]
                            if result:
                                with lock:
                                    self.translated[text] = result
                                    self.count += 1
                        except Exception:
                            self.errors += 1
                self._save_progress()
                retry_success = sum(1 for text in failed if text in self.translated)
                self._log(f"【失败流转】重试完成：{retry_success}/{len(failed)} 条成功")

        self._save_output(data)

        # 精修阶段
        if self.refine_mode.get() and not self.should_stop:
            self.root.after(0, lambda: self.status_label.config(text="进入第二阶段：评估译文质量并精修..."))
            self._log("=== 第二阶段：LLM精修开始 ===")
            self._run_refine_phase(data, src, tgt)
            self._save_output(data)

        self.root.after(0, self._finish)

    def _assess_quality(self, source, target, src_lang, tgt_lang):
        """评估译文质量，返回0-100分数。低分表示需要精修。
        增强版：增加语义层面检测（生硬直译、常见误译模式、英文残留排除游戏术语）"""
        if not target or not target.strip():
            return 0
        if source == target:
            return 0  # 未翻译
        s_len = len(source.strip())
        t_len = len(target.strip())
        if s_len == 0:
            return 100
        score = 100
        # 1. 长度比异常（根据目标语言动态调整阈值）
        ratio = t_len / s_len
        tgt_low = tgt_lang.lower().split('-')[0]
        if tgt_low in ('zh', 'ja', 'ko'):
            low_very, low, high, high_very = 0.15, 0.25, 3.0, 5.0
        elif tgt_low in ('en', 'fr', 'de', 'es', 'it', 'pt', 'ru'):
            low_very, low, high, high_very = 0.2, 0.3, 4.0, 6.0
        else:
            low_very, low, high, high_very = 0.2, 0.3, 3.0, 5.0
        if ratio < low_very:
            score -= 50
        elif ratio < low:
            score -= 30
        elif ratio > high_very:
            score -= 40
        elif ratio > high:
            score -= 20
        # 2. 错误/拒绝模式
        error_patterns = ["i'm sorry", "i cannot", "i can't", "无法翻译", "翻译失败", "error:",
                          "sorry, i", "as an ai", "i don't understand", "???", "[error]"]
        target_lower = target.lower()
        for pat in error_patterns:
            if pat in target_lower:
                score -= 60
                break
        # 3. 未翻译词检测
        # 3a. 目标是拉丁语言时，检测中文残留
        latin_langs = {'en', 'fr', 'de', 'es', 'it', 'pt', 'ru', 'nl', 'pl', 'sv', 'ar', 'vi', 'th', 'id', 'ms', 'tr'}
        if tgt_low in latin_langs:
            src_cjk = sum(1 for c in source if '\u4e00' <= c <= '\u9fff')
            tgt_cjk = sum(1 for c in target if '\u4e00' <= c <= '\u9fff')
            if src_cjk > 0 and tgt_cjk / src_cjk > 0.7:
                score -= 40
        # 3b. 目标是中日韩语言时，检测未翻译的英文/拉丁文单词残留（排除常见游戏术语）
        if tgt_low in ('zh', 'ja', 'ko'):
            eng_words = re.findall(r'[a-zA-Z]{2,}', target)
            # 排除正常保留的英文词（游戏术语、缩写、品牌等）
            normal_keeps = {'hp', 'mp', 'sp', 'xp', 'lv', 'ui', 'npc', 'boss', 'raid', 'pvp', 'pve',
                           'ok', 'ng', 'dlc', 'rpg', 'fps', 'rts', 'moba', 'mmorpg',
                           'wifi', 'gps', 'url', 'id', 'qr', 'vip', 'svip', 'cpu', 'gpu',
                           'usb', 'hdmi', 'bluetooth', 'app', 'ios', 'android'}
            suspicious = [w for w in eng_words if w.lower() not in normal_keeps]
            if len(suspicious) >= 4:
                score -= 40
            elif len(suspicious) >= 2:
                score -= 20
            # 单个可疑英文单词（可能是角色名）不扣分
        # 4. 语义层面：常见误译/生硬翻译检测（仅中文目标）
        # 整合版：A严重误译/B机器痕迹/C的字密度/D连续重复/E日文音译/I量词错误
        #       J敬语过度/K动词冗余/L冗长表达/N请X吧/O句首了/P把字句/Q标点/R和字句/S数字混用
        # 去掉过于激进的：F逻辑连接词/T句末语气词/M日语敬语直译/G代词指代
        if tgt_low in ('zh', 'zh-cn', 'zh-tw'):
            detected = set()  # 避免重复扣分

            # A. 严重误译词
            severe = [
                ('倒叙', 30, ['倒叙法', '倒叙手法', '倒叙方式']),
                ('插叙', 25, []),
                ('女训练', 25, []), ('男训练', 25, []),
                ('女队员', 20, []), ('男队员', 20, []),
                ('女成员', 20, []), ('男成员', 20, []),
                ('女选手', 20, []), ('男选手', 20, []),
                ('女员工', 20, []), ('男员工', 20, []),
                ('做着', 20, []), ('有着', 15, []),
                ('之时', 20, ['关键之时', '危难之时', '生死之时', '紧要之时', '千钧一发之时']),
                ('之际', 15, ['关键之际', '危难之际', '生死之际', '紧要之际', '千钧一发之际']),
                ('之刻', 20, ['关键之刻', '危难之刻']),
                ('被进行了', 25, []), ('被做出了', 25, []),
                ('被给予了', 20, []), ('被告知了', 15, []),
                ('被通知了', 15, []), ('被教导了', 15, []),
                ('被得到了', 15, []), ('被发现了', 15, []),
                ('被拿到了', 15, []), ('被找到了', 15, []),
                ('被拿走了', 15, []), ('被带走了', 15, []),
                ('进行着', 20, []), ('实施着', 20, []), ('开展着', 20, []),
            ]
            for pat, pen, exc in severe:
                if pat in detected: continue
                if pat in target and not any(e in target for e in exc):
                    score -= pen; detected.add(pat)

            # B. 机器翻译痕迹
            mt = [('的的地',15),('了了',10),('的的',10),('着着',10),('啊啊',10),
                  ('呢呢',10),('吧吧',10),('吗吗',10),('的的的',20),('了了了',15),('着着了',15)]
            for pat, pen in mt:
                if pat in detected: continue
                if pat in target:
                    score -= pen; detected.add(pat)

            # C. "的"字密度（比例计算，比固定阈值更准确）
            de_count = target.count('的')
            cn_chars = sum(1 for c in target if '\u4e00' <= c <= '\u9fff')
            if cn_chars > 10 and '的字密度' not in detected:
                de_ratio = de_count / cn_chars
                if de_ratio > 0.15:
                    score -= min(int((de_ratio - 0.15) * 400), 25)
                    detected.add('的字密度')

            # D. 连续重复字（排除正常拟声词）
            if len(target) > 10 and '连续重复字' not in detected:
                normal_repeats = {'哈','嘿','哼','呜','啊','哦','嗯','哎','哟','哇','嘻'}
                for i in range(len(target) - 2):
                    ch = target[i]
                    if ch == target[i+1] == target[i+2] and ch not in ' \t\n～~…·—-' and ch not in normal_repeats:
                        score -= 15; detected.add('连续重复字'); break

            # E. 日文音译词
            katakana = [
                ('斯国一',15,[]),('斯国矣',15,[]),('斯够以',15,[]),
                ('阿里嘎多',10,[]),('哦哈哟',10,[]),
                ('空尼奇瓦',10,[]),('撒哟娜拉',10,[]),
                ('桑',5,['阿桑','沧桑','桑树','桑叶','扶桑']),
            ]
            for pat, pen, exc in katakana:
                if pat in detected: continue
                if pat in target and not any(e in target for e in exc):
                    score -= pen; detected.add(pat)

            # I. 量词错误
            measure = [('一个们',20),('一位们',20),('一只们',20),('一匹们',20),('一名们',20),('一个位',15)]
            for pat, pen in measure:
                if pat in detected: continue
                if pat in target:
                    score -= pen; detected.add(pat)

            # J. 敬语过度
            polite = [
                ('请允许我',15,[]),('请容我',15,[]),
                ('恕我直言',20,[]),('恕我冒昧',20,[]),
                ('冒昧地问一下',20,[]),('冒昧地问一句',20,[]),
                ('如果不介意的话',15,[]),('如果不嫌弃的话',15,[]),
                ('能否请您',15,[]),('能否麻烦您',15,[]),('麻烦您',5,[]),
            ]
            for pat, pen, exc in polite:
                if pat in detected: continue
                if pat in target and not any(e in target for e in exc):
                    score -= pen; detected.add(pat)

            # K. 动词冗余
            verb_redun = [
                ('进行战斗',15,[]),('进行攻击',15,[]),('进行防御',15,[]),
                ('进行治疗',10,[]),('进行探索',10,[]),('进行保存',10,[]),
                ('进行作战',15,[]),('进行打击',15,[]),
                ('实施攻击',15,[]),('实施打击',15,[]),
                ('加以利用',10,[]),('加以考虑',10,[]),
                ('给予伤害',20,[]),('给予攻击',20,[]),('给予效果',15,[]),
                ('造成给予',25,[]),
                ('给打败',15,[]),('给杀了',15,[]),('给做了',15,[]),
                ('给走了',15,[]),('给看了',15,[]),
            ]
            for pat, pen, exc in verb_redun:
                if pat in detected: continue
                if pat in target and not any(e in target for e in exc):
                    score -= pen; detected.add(pat)

            # L. 冗长表达（去掉过于激进的：的话、感到、已经、一点、变成了）
            verbose = [
                ('关于这件事',15,[]),('关于这个问题',15,[]),
                ('关于这一点',10,[]),('有关于',10,[]),
                ('所谓的',10,['所谓的"','所谓的「','所谓的\'','所谓的《']),
                ('各种各样的',15,[]),('各式各样的',15,[]),
                ('等等等等',20,[]),('等等等等等',25,[]),
                ('在使用的时候',15,[]),('在战斗的时候',15,[]),
                ('在说话的时候',15,[]),('在走路的时候',15,[]),
                ('的时候的话',20,[]),
                ('要进行',10,[]),('要实施',10,[]),
                ('似乎好像',15,[]),('好像似乎',15,[]),
                ('的方面',15,['这方面','那方面','各个方面']),
                ('在这里有',10,[]),('在那里有',10,[]),
                ('大量的',10,[]),('少量的',10,[]),
            ]
            for pat, pen, exc in verbose:
                if pat in detected: continue
                if pat in target and not any(e in target for e in exc):
                    score -= pen; detected.add(pat)

            # N. "请X吧"双重礼貌（正则）
            if '请X吧' not in detected:
                please_ba = re.findall(r'请[\u4e00-\u9fff]{1,3}吧', target)
                normal_ba = {'请走吧','请便吧','请回吧','请坐吧','请做吧','请说吧','请吃吧',
                             '请喝吧','请打吧','请杀吧','请听吧','请看吧','请试试吧','请想想看吧',
                             '请听一下吧','请想一下吧'}
                for m in please_ba:
                    if m not in normal_ba:
                        score -= 12; detected.add('请X吧'); break

            # O. "了"字位置错误（句首"了"）
            if target.strip().startswith('了') and '句首了' not in detected:
                score -= 20; detected.add('句首了')

            # P. "把"字句缺宾语（正则）
            if '把缺宾语' not in detected:
                ba_err = re.findall(r'把(?:就|也|都|又|再|才|刚|已经|正在|将要|想要|打算)(?:走了|看了|来了|去了|做了|吃了|喝了|打了|杀了)', target)
                ba_simple = re.findall(r'(?<![\u4e00-\u9fff])把(?:走了|看了|来了|去了|做了|吃了|喝了|打了|杀了)(?![\u4e00-\u9fff]|吗|呢|吧|啊|哦|嗯|呐|嘛|哟)', target)
                if ba_err or ba_simple:
                    score -= 20; detected.add('把缺宾语')

            # Q. 标点异常
            if '标点异常' not in detected:
                if re.search(r' +[，。、！？；：]', target) or re.search(r'[，。、！？；：] +', target):
                    score -= 10; detected.add('标点异常')
            if '多余句号' not in detected and '。。' in target:
                score -= 15; detected.add('多余句号')

            # R. "和"字缺宾语（正则）
            if '和缺宾语' not in detected:
                he_err = re.findall(r'和(?:就|也|都|又|再|才|已经|一起|一同)(?:去了|来了|走了|做了|看了|打了)', target)
                he_simple = re.findall(r'(?<![\u4e00-\u9fff])和(?:去了|来了|走了|做了|看了|打了)(?![\u4e00-\u9fff]|吗|呢|吧|啊|哦|嗯|呐|嘛|哟)', target)
                if he_err or he_simple:
                    score -= 15; detected.add('和缺宾语')

            # S. 数字混用（中文数字+阿拉伯数字）
            if '数字混用' not in detected:
                if re.search(r'[一二三四五六七八九十百千万]+\d', target) or re.search(r'\d+[一二三四五六七八九十百千万]', target):
                    score -= 15; detected.add('数字混用')
        # 5. 重复字符过多
        if len(target) > 10:
            max_repeat = max(target.count(c) for c in set(target))
            if max_repeat / len(target) > 0.6:
                score -= 30
        # 6. 全是标点/数字
        if all(c in '，。、！？；：""''（）【】.,!?;:\'\"()[] \t\n0123456789' for c in target):
            score -= 50
        # 7. 占位符检查（游戏翻译中缺失占位符会导致崩溃）
        placeholders = re.findall(r'\{[^}]+\}|%[sdif]|\$[a-zA-Z_][a-zA-Z0-9_]*|<[^>]+>', source)
        for ph in placeholders:
            if ph not in target:
                score -= 50
                break
        return max(0, min(100, score))

    def _batch_refine_items(self, items, max_chars=1500):
        """将待精修条目按总字符数分批，避免单批token超限"""
        batches = []
        current = []
        current_chars = 0
        for item in items:
            key, initial, _, _ = item
            est_chars = len(key) + len(initial) + 50  # 原文+初译+格式开销
            if current and current_chars + est_chars > max_chars:
                batches.append(current)
                current = [item]
                current_chars = est_chars
            else:
                current.append(item)
                current_chars += est_chars
        if current:
            batches.append(current)
        return batches

    def _run_refine_phase(self, data, src, tgt):
        """第二阶段：评估质量并用LLM精修低分项（含重试/上下文/强制精修/语言名）"""
        src_name = LANG_NAMES.get(src, src)
        tgt_name = LANG_NAMES.get(tgt, tgt)
        # 找出需要精修的条目（阈值筛选 + 极端长度比强制精修）
        to_refine = []
        threshold = self.refine_threshold.get()
        for key in data:
            translated = self.translated.get(key)
            if translated and translated != key:
                q = self._assess_quality(key, translated, src, tgt)
                # 强制精修条件：极端长度比 + 低分且偏短
                ratio = len(translated) / len(key) if len(key) > 0 else 0
                forced = ratio < 0.15 or ratio > 8  # 极端长度比
                if not forced and q < 70 and ratio < 0.2:
                    forced = True  # 分数偏低且译文偏短，可能漏译
                # 保底速通：非强制精修且质量>75分直接放行，谷歌/DeepL初译已足够，省LLM调用
                if not forced and q > 75:
                    continue
                if q < threshold or forced:
                    to_refine.append((key, translated, q, forced))
        if not to_refine:
            self._log("精修阶段：所有条目质量达标，无需精修")
            return
        forced_count = sum(1 for _, _, _, f in to_refine if f)
        self._log(f"【精修模式·第二阶段】{len(to_refine)} 条待精修（阈值{threshold}分以下{len(to_refine)-forced_count}条 + 极端长度比强制{forced_count}条），LLM={self.model.get() or '未设置'}, 并发={self.refine_workers.get()}")
        # 使用API设置页配置的OpenAI兼容LLM
        try:
            api_key = self.api_key.get()
            base_url = self.base_url.get().rstrip('/')
            model = self.model.get()
            if not api_key or not base_url or not model:
                self._log("精修阶段：API设置页未配置OpenAI兼容LLM（Key/BaseURL/模型），跳过精修。")
                return
            refine_url = base_url + "/v1/chat/completions" if not base_url.endswith("/v1") else base_url + "/chat/completions"
            prompt_template = self.refine_prompt.get()
            # 系统提示：动态插入语言名称
            system_prompt = f"{prompt_template}\n源语言：{src_name}，目标语言：{tgt_name}。"
            lock = threading.Lock()
            refined_context = []  # [(原文, 精修译文), ...] 用于上下文参考
            refined_count = 0
            start_time = time.time()

            def single_refine_worker(item):
                """单条精修（批量失败时的降级方案）"""
                key, initial_trans, q, forced = item
                if self.should_stop:
                    return None
                for attempt in range(4):
                    if self.should_stop:
                        return None
                    try:
                        messages = [{"role": "system", "content": system_prompt}]
                        with lock:
                            ctx_items = refined_context[-2:]
                        for orig, ref in ctx_items:
                            messages.append({"role": "user", "content": f"原文：{orig}\n初译：（已精修，作为术语参考）"})
                            messages.append({"role": "assistant", "content": ref})
                        messages.append({"role": "user", "content": f"原文：{key}\n初译：{initial_trans}\n请输出精修后的译文："})
                        payload = json.dumps({
                            "model": model, "messages": messages,
                            "temperature": 0.3, "max_tokens": 2048,
                        }).encode('utf-8')
                        result = pool_request('POST', refine_url, body=payload, headers={
                            'Content-Type': 'application/json', 'Authorization': f'Bearer {api_key}',
                        }, timeout=120)
                        refined = result['choices'][0]['message']['content'].strip()
                        if refined and refined != initial_trans:
                            phs = re.findall(r'\{[^}]+\}|%[sdif]|\$[a-zA-Z_][a-zA-Z0-9_]*|<[^>]+>', key)
                            if all(ph in refined for ph in phs):
                                # 致命错误检测：AI拒绝模式则不采用（不再比较总分，长度比变化是正常的）
                                refined_lower = refined.lower()
                                ai_reject = any(pat in refined_lower for pat in
                                    ["i'm sorry", "i cannot", "i can't", "无法翻译", "翻译失败", "as an ai", "i don't understand", "sorry, i"])
                                if ai_reject:
                                    return None
                                with lock:
                                    refined_context.append((key, refined))
                                    if len(refined_context) > 10:
                                        refined_context[:] = refined_context[-10:]
                                    return (key, refined)
                                return None
                            return None
                    except Exception as e:
                        if attempt >= 3:
                            return None
                        err_str = str(e).lower()
                        wait = 2 ** attempt
                        if '429' in err_str or 'rate' in err_str or 'limit' in err_str:
                            wait *= 2
                        time.sleep(wait)
                return None

            def batch_refine_worker(batch):
                """批量精修：多条打包一次API调用，失败降级为单条"""
                if self.should_stop:
                    return []
                for attempt in range(4):
                    if self.should_stop:
                        return []
                    try:
                        # 构建批量prompt
                        messages = [{"role": "system", "content": system_prompt + "\n请逐条精修以下文本，严格按编号输出，格式：编号. 精修后译文，不要解释。"}]
                        with lock:
                            ctx_items = refined_context[-2:]
                        for orig, ref in ctx_items:
                            messages.append({"role": "user", "content": f"原文：{orig}\n初译：（已精修，作为术语参考）"})
                            messages.append({"role": "assistant", "content": ref})
                        user_content = ""
                        for i, (key, initial, q, forced) in enumerate(batch):
                            user_content += f"{i+1}. 原文：{key}\n初译：{initial}\n\n"
                        user_content += "请按编号输出精修后的译文，每条一行，格式：编号. 译文"
                        messages.append({"role": "user", "content": user_content})
                        payload = json.dumps({
                            "model": model, "messages": messages,
                            "temperature": 0.3, "max_tokens": 4096,
                        }).encode('utf-8')
                        result = pool_request('POST', refine_url, body=payload, headers={
                            'Content-Type': 'application/json', 'Authorization': f'Bearer {api_key}',
                        }, timeout=120)
                        raw = result['choices'][0]['message']['content'].strip()
                        # 解析批量结果
                        parsed = self._parse_batch_result(raw, len(batch))
                        if not parsed:
                            if attempt >= 3:
                                break
                            continue
                        # 逐条检查占位符 + AI拒绝模式检测，合格的采用
                        results = []
                        for i, (key, initial, q, forced) in enumerate(batch):
                            refined = parsed[i]
                            if refined and refined != initial:
                                phs = re.findall(r'\{[^}]+\}|%[sdif]|\$[a-zA-Z_][a-zA-Z0-9_]*|<[^>]+>', key)
                                if all(ph in refined for ph in phs):
                                    # 致命错误检测：AI拒绝模式则不采用（不再比较总分，长度比变化是正常的）
                                    refined_lower = refined.lower()
                                    ai_reject = any(pat in refined_lower for pat in
                                        ["i'm sorry", "i cannot", "i can't", "无法翻译", "翻译失败", "as an ai", "i don't understand", "sorry, i"])
                                    if ai_reject:
                                        continue
                                    results.append((key, refined))
                                    with lock:
                                        refined_context.append((key, refined))
                                        if len(refined_context) > 10:
                                            refined_context[:] = refined_context[-10:]
                        return results
                    except Exception as e:
                        if attempt >= 3:
                            break
                        err_str = str(e).lower()
                        wait = 2 ** attempt
                        if '429' in err_str or 'rate' in err_str or 'limit' in err_str:
                            wait *= 2
                        time.sleep(wait)
                # 批量失败，降级为单条精修
                results = []
                for item in batch:
                    r = single_refine_worker(item)
                    if r:
                        results.append(r)
                return results

            # 分批（按字符数，每批不超过1500字）
            batches = self._batch_refine_items(to_refine, max_chars=1500)
            self._log(f"精修分批：{len(to_refine)} 条分为 {len(batches)} 批（按字符数≤1500/批）")

            with ThreadPoolExecutor(max_workers=max(1, self.refine_workers.get())) as executor:
                futures = {executor.submit(batch_refine_worker, batch): batch for batch in batches}
                done_items = 0
                for future in as_completed(futures):
                    if self.should_stop:
                        break
                    batch_results = future.result()
                    batch_size = len(futures[future])
                    for key, refined in batch_results:
                        with lock:
                            self.translated[key] = refined
                            refined_count += 1
                    done_items += batch_size
                    if done_items % 10 == 0 or done_items >= len(to_refine):
                        elapsed = time.time() - start_time
                        rate = done_items / elapsed if elapsed > 0 else 0
                        self.root.after(0, lambda d=done_items, t=len(to_refine), r=rate: self.status_label.config(
                            text=f"精修进度: {d}/{t} ({d*100//t if t else 0}%) | 速度: {r:.1f}条/秒 | 已精修: {refined_count}"))
            self._log(f"精修阶段完成：共评估 {len(to_refine)} 条，成功精修 {refined_count} 条")
        except Exception as e:
            self._log(f"精修阶段出错: {e}")

    def _update_progress(self, done, total, rate, remain):
        self.progress_bar["value"] = done
        pause_str = " [已暂停]" if self.is_paused else ""
        self.status_label.config(
            text=f"进度: {done}/{total} ({done*100//total if total else 0}%) | "
                 f"速度: {rate:.1f}条/秒 | 预计剩余: {remain/60:.1f}分钟 | "
                 f"本轮已翻译: {self.count} | 失败: {self.errors}{pause_str}"
        )

    def _toggle_pause(self):
        if not self.is_running:
            return
        self.is_paused = not self.is_paused
        if self.is_paused:
            self.pause_btn.config(text="继续")
            self._log("已暂停，当前批次完成后等待。可点继续恢复翻译。")
        else:
            self.pause_btn.config(text="暂停")
            self._log("继续翻译")

    def _save_progress(self):
        pf = self.progress_file.get()
        if pf:
            with open(pf, 'w', encoding='utf-8') as f:
                json.dump(self.translated, f, ensure_ascii=False)

    def _save_output(self, data):
        outf = self.output_file.get()
        if outf:
            final = {k: self.translated.get(k, k) for k in data}
            with open(outf, 'w', encoding='utf-8') as f:
                json.dump(final, f, ensure_ascii=False, indent=2)
            self._log(f"输出已保存: {outf}")

    def _stop_translate(self):
        self.should_stop = True
        self.is_paused = False
        self._log("正在停止...")

    def _finish(self):
        self.is_running = False
        self.is_paused = False
        self.total_elapsed = time.time() - self.start_time if self.start_time else 0
        # 翻译结束：重置所有引擎的背压状态（避免下次翻译受上次冷却影响）
        self._reset_rate_state()
        self.start_btn.config(state="normal")
        self.pause_btn.config(state="disabled", text="暂停")
        self.stop_btn.config(state="disabled")
        # 实际失败数 = 待翻译条目里最终没翻译成功的（不依赖self.errors，各代码路径不一定都递增）
        if hasattr(self, 'to_translate') and self.to_translate:
            actual_failed = sum(1 for t in self.to_translate if t not in self.translated)
        else:
            actual_failed = self.errors
        self.errors = actual_failed
        self.status_label.config(text=f"完成！本轮翻译: {self.count} 条, 失败: {actual_failed} 条")
        self._log(f"翻译完成！本轮: {self.count} 条, 失败: {actual_failed} 条, 耗时: {self.total_elapsed:.1f}秒")
        finish_msg = f"翻译完成！\n本轮: {self.count} 条\n失败: {actual_failed} 条\n耗时: {self.total_elapsed:.1f}秒\n输出: {self.output_file.get()}"
        if actual_failed > 0:
            finish_msg += f"\n\n{actual_failed} 条失败条目未写入进度，重新选择同一文件并点击「开始翻译」即可自动重试。"
        messagebox.showinfo("完成", finish_msg)
        # 发送WebHook通知
        if self.webhook_url.get().strip():
            threading.Thread(target=self._send_webhook, daemon=True).start()

    # ==================== 配置保存/加载 ====================
    def _save_config(self):
        f = filedialog.asksaveasfilename(
            defaultextension=".cfg", filetypes=[("配置文件", "*.cfg"), ("所有文件", "*.*")],
            initialfile="translator_config.cfg"
        )
        if not f:
            return
        config = {
            "api_engine": self.api_engine.get(),
            "source_lang": self.source_lang.get(),
            "target_lang": self.target_lang.get(),
            "api_key": self.api_key.get(),
            "api_id": self.api_id.get(),
            "model": self.model.get(),
            "base_url": self.base_url.get(),
            "mymemory_email": self.mymemory_email.get(),
            "max_workers": self.max_workers.get(),
            "max_retry": self.max_retry.get(),
            "chunk_size": self.chunk_size.get(),
            "output_file": self.output_file.get(),
            "progress_file": self.progress_file.get(),
        }
        try:
            with open(f, 'w', encoding='utf-8') as fh:
                json.dump(config, fh, ensure_ascii=False, indent=2)
            self._log(f"配置已保存: {f}")
            messagebox.showinfo("成功", f"配置已保存到:\n{f}")
        except Exception as e:
            messagebox.showerror("错误", f"保存失败: {e}")

    def _load_config(self):
        f = filedialog.askopenfilename(
            filetypes=[("配置文件", "*.cfg"), ("所有文件", "*.*")]
        )
        if not f or not os.path.exists(f):
            return
        try:
            with open(f, 'r', encoding='utf-8') as fh:
                config = json.load(fh)
            self.api_engine.set(config.get("api_engine", self.api_engine.get()))
            self.source_lang.set(config.get("source_lang", self.source_lang.get()))
            self.target_lang.set(config.get("target_lang", self.target_lang.get()))
            self.api_key.set(config.get("api_key", ""))
            self.api_id.set(config.get("api_id", ""))
            self.model.set(config.get("model", ""))
            self.base_url.set(config.get("base_url", ""))
            self.mymemory_email.set(config.get("mymemory_email", ""))
            self.max_workers.set(config.get("max_workers", 8))
            self.max_retry.set(config.get("max_retry", 3))
            self.chunk_size.set(config.get("chunk_size", 1500))
            self.output_file.set(config.get("output_file", "ManualTransFile.json"))
            self.progress_file.set(config.get("progress_file", "trans_progress.json"))
            self._on_api_change()
            self._log(f"配置已加载: {f}")
            messagebox.showinfo("成功", f"配置已加载:\n{f}")
        except Exception as e:
            messagebox.showerror("错误", f"加载失败: {e}")

    # ==================== 费用预估 ====================
    def _estimate_cost(self, chars):
        engine_name = self.api_engine.get()
        rate = COST_RATES.get(engine_name)
        if not rate:
            return None
        cost_type = rate.get("type", "per_char")
        note = rate.get("note", "")

        # 完全免费
        if cost_type == "free":
            return f"免费（{note}）"

        # 按字符计费
        if cost_type == "per_char":
            free = rate.get("free_chars", 0)
            per_million = rate.get("per_million", 0)
            if per_million == 0:
                return f"免费额度内（{note}）"
            billable = max(0, chars - free)
            cost = billable / 1_000_000 * per_million
            return f"约{cost:.2f}元（免费额度{free}字符，超出{per_million}元/百万字符，{note}）"

        # 按token计费（LLM）
        if cost_type == "per_token":
            # 估算token数：中文约1字符=1.2token，英文约4字符=1token
            # 混合内容取保守值：字符数 × 0.9 ≈ 输入token；输出token≈输入token
            input_tokens = int(chars * 0.9)
            output_tokens = int(chars * 0.9)  # 译文长度近似
            total_tokens = input_tokens + output_tokens
            model = self.model.get()
            price_per_million = MODEL_PRICES.get(model)
            if price_per_million is not None:
                if price_per_million == 0:
                    return f"约{total_tokens:,}token（模型{model}有免费额度，{note}）"
                cost = total_tokens / 1_000_000 * price_per_million
                return (f"约{total_tokens:,}token，预计{cost:.2f}元"
                        f"（模型{model}约{price_per_million}元/百万token，{note}）")
            else:
                return (f"约{total_tokens:,}token，按token计费"
                        f"（模型{model or '未指定'}价格未知，{note}）")

        return f"未知计费方式（{note}）"

    def _show_cost_estimate(self):
        inf = self.input_file.get()
        if not inf or not os.path.exists(inf):
            messagebox.showwarning("提示", "请先选择输入文件")
            return
        try:
            with open(inf, 'r', encoding='utf-8') as f:
                data = json.load(f)
            total_chars = sum(len(k) for k in data)
            total_entries = len(data)
            translated_entries = sum(1 for k in data if k in self.translated)
            remaining_chars = sum(len(k) for k in data if k not in self.translated)
            cost_info = self._estimate_cost(remaining_chars)
            msg = (
                f"总条目: {total_entries}\n"
                f"已翻译: {translated_entries}\n"
                f"未翻译: {total_entries - translated_entries}\n"
                f"总字符数: {total_chars}\n"
                f"待翻译字符: {remaining_chars}\n"
                f"引擎: {self.api_engine.get()}\n"
                f"费用预估: {cost_info or '未知引擎'}"
            )
            messagebox.showinfo("费用预估", msg)
        except Exception as e:
            messagebox.showerror("错误", f"统计失败: {e}")

    # ==================== 详细统计报告 ====================
    def _show_detailed_stats(self):
        inf = self.input_file.get()
        if not inf or not os.path.exists(inf):
            messagebox.showwarning("提示", "请先选择输入文件")
            return
        try:
            with open(inf, 'r', encoding='utf-8') as f:
                data = json.load(f)
            total = len(data)
            translated = sum(1 for k in data if k != self.translated.get(k, k) and k in self.translated)
            untranslated = total - translated
            total_chars = sum(len(k) for k in data)
            translated_chars = sum(len(k) for k in data if k in self.translated and self.translated[k] != k)
            avg_speed = (self.count / self.total_elapsed) if self.total_elapsed > 0 else 0

            report = (
                f"===== 翻译统计报告 =====\n\n"
                f"【文件】\n"
                f"输入: {inf}\n"
                f"输出: {self.output_file.get()}\n\n"
                f"【条目统计】\n"
                f"总条目: {total}\n"
                f"已翻译: {translated} ({translated*100//total if total else 0}%)\n"
                f"未翻译: {untranslated}\n\n"
                f"【字符统计】\n"
                f"总字符数: {total_chars}\n"
                f"已翻译字符: {translated_chars}\n"
                f"待翻译字符: {total_chars - translated_chars}\n\n"
                f"【本轮运行】\n"
                f"本轮翻译: {self.count} 条\n"
                f"失败: {self.errors} 条\n"
                f"总耗时: {self.total_elapsed:.1f}秒 ({self.total_elapsed/60:.1f}分钟)\n"
                f"平均速度: {avg_speed:.1f}条/秒\n\n"
                f"【配置】\n"
                f"引擎: {self.api_engine.get()}\n"
                f"语言: {self.source_lang.get()} -> {self.target_lang.get()}\n"
                f"并发数: {self.max_workers.get()}\n"
                f"重试次数: {self.max_retry.get()}\n"
                f"分段大小: {self.chunk_size.get()}"
            )
            # 显示在新窗口
            win = tk.Toplevel(self.root)
            win.title("翻译统计报告")
            win.geometry("500x520")
            txt = scrolledtext.ScrolledText(win, font=("Consolas", 10), wrap=tk.WORD)
            txt.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
            txt.insert(tk.END, report)
            txt.config(state=tk.DISABLED)
            ttk.Button(win, text="关闭", command=win.destroy).pack(pady=5)
        except Exception as e:
            messagebox.showerror("错误", f"统计失败: {e}")

    # ==================== 自动保存配置 ====================
    def _load_auto_config(self):
        """启动时加载自动保存的配置"""
        if not os.path.exists(self._config_file):
            return
        try:
            self._loading_config = True
            with open(self._config_file, 'r', encoding='utf-8') as f:
                cfg = json.load(f)
            # 用 _safe_set 包装每个变量设置，单个失败不影响其他
            def _safe(var, val):
                try: var.set(val)
                except Exception: pass
            _safe(self.api_engine, cfg.get("api_engine", self.api_engine.get()))
            _safe(self.source_lang, cfg.get("source_lang", self.source_lang.get()))
            _safe(self.target_lang, cfg.get("target_lang", self.target_lang.get()))
            _safe(self.api_key, cfg.get("api_key", ""))
            _safe(self.api_id, cfg.get("api_id", ""))
            _safe(self.model, cfg.get("model", ""))
            _safe(self.base_url, cfg.get("base_url", ""))
            _safe(self.mymemory_email, cfg.get("mymemory_email", ""))
            _safe(self.google_proxies, cfg.get("google_proxies", ""))
            _safe(self.bing_proxy, cfg.get("bing_proxy", ""))
            _safe(self.deeplx_proxy, cfg.get("deeplx_proxy", ""))
            _safe(self.max_workers, cfg.get("max_workers", 8))
            _safe(self.max_retry, cfg.get("max_retry", 3))
            _safe(self.chunk_size, cfg.get("chunk_size", 1500))
            _safe(self.batch_size, cfg.get("batch_size", 5))
            _safe(self.protect_placeholders, cfg.get("protect_placeholders", True))
            _safe(self.protect_patterns, cfg.get("protect_patterns", r"\{[^}]+\}|%[sdif]|\$[a-zA-Z_][a-zA-Z0-9_]*|<[^>]+>"))
            _safe(self.dark_mode, cfg.get("dark_mode", False))
            _safe(self.webhook_url, cfg.get("webhook_url", ""))
            _safe(self.webhook_type, cfg.get("webhook_type", "generic"))
            _safe(self.refine_mode, cfg.get("refine_mode", False))
            _safe(self.refine_threshold, cfg.get("refine_threshold", 60))
            _safe(self.refine_workers, cfg.get("refine_workers", 4))
            _safe(self.refine_pre_engine, cfg.get("refine_pre_engine", "谷歌翻译"))
            _safe(self.refine_pre_workers, cfg.get("refine_pre_workers", 16))
            _safe(self.pre_api_key, cfg.get("pre_api_key", ""))
            _safe(self.pre_api_id, cfg.get("pre_api_id", ""))
            _safe(self.pre_base_url, cfg.get("pre_base_url", ""))
            _safe(self.pre_model, cfg.get("pre_model", ""))
            _safe(self.pre_email, cfg.get("pre_email", ""))
            _safe(self.refine_llm_base_url, cfg.get("refine_llm_base_url", "https://api.siliconflow.cn/v1"))
            _safe(self.refine_llm_api_key, cfg.get("refine_llm_api_key", ""))
            _safe(self.refine_llm_model, cfg.get("refine_llm_model", "tencent/Hunyuan-MT-7B"))
            _safe(self.refine_prompt, cfg.get("refine_prompt", "你是资深本地化审校专家。要求：1.必须保留所有占位符（{name}、%s、$var、<color>等），缺失会导致程序崩溃；2.术语前后一致；3.对话口语化，长度不超过原文120%；4.只输出修正后的译文，不要解释。"))
            # 引擎B
            _safe(self.enable_engine_b, cfg.get("enable_engine_b", False))
            _safe(self.engine_b_preset, cfg.get("engine_b_preset", ""))
            _safe(self.engine_b_base_url, cfg.get("engine_b_base_url", ""))
            _safe(self.engine_b_model, cfg.get("engine_b_model", "tencent/Hunyuan-MT-7B"))
            _safe(self.engine_b_api_key, cfg.get("engine_b_api_key", ""))
            _safe(self.engine_b_workers, cfg.get("engine_b_workers", 4))
            _safe(self.engine_b_batch_size, cfg.get("engine_b_batch_size", 6))
            try:
                self._on_api_change()
            except Exception:
                pass
            try:
                self._on_engine_b_toggle()
            except Exception:
                pass
            if self.dark_mode.get():
                try: self._apply_dark_theme()
                except Exception: pass
            self._log("已加载自动保存的配置")
        except Exception as e:
            self._log(f"加载自动配置失败: {e}")
        finally:
            self._loading_config = False

    def _setup_auto_save(self):
        """设置配置变量变化时自动保存"""
        for var in [self.api_engine, self.source_lang, self.target_lang,
                    self.api_key, self.api_id, self.model, self.base_url,
                    self.mymemory_email, self.google_proxies, self.bing_proxy, self.deeplx_proxy, self.max_workers, self.max_retry,
                    self.chunk_size, self.batch_size, self.protect_placeholders, self.protect_patterns,
                    self.dark_mode, self.webhook_url, self.webhook_type,
                    self.refine_mode, self.refine_threshold, self.refine_workers,
                    self.refine_pre_engine, self.refine_pre_workers,
                    self.pre_api_key, self.pre_api_id, self.pre_base_url, self.pre_model, self.pre_email,
                    self.refine_llm_base_url, self.refine_llm_api_key, self.refine_llm_model,
                    self.refine_prompt,
                    self.enable_engine_b, self.engine_b_preset, self.engine_b_base_url,
                    self.engine_b_model, self.engine_b_api_key, self.engine_b_workers,
                    self.engine_b_batch_size]:
            var.trace_add("write", self._auto_save_config)

    def _auto_save_config(self, *args):
        """配置变化时自动保存到文件（防抖）"""
        if self._loading_config:
            return
        try:
            cfg = {
                "api_engine": self.api_engine.get(),
                "source_lang": self.source_lang.get(),
                "target_lang": self.target_lang.get(),
                "api_key": self.api_key.get(),
                "api_id": self.api_id.get(),
                "model": self.model.get(),
                "base_url": self.base_url.get(),
                "mymemory_email": self.mymemory_email.get(),
                "google_proxies": self.google_proxies.get(),
                "bing_proxy": self.bing_proxy.get(),
                "deeplx_proxy": self.deeplx_proxy.get(),
                "max_workers": self.max_workers.get(),
                "max_retry": self.max_retry.get(),
                "chunk_size": self.chunk_size.get(),
                "batch_size": self.batch_size.get(),
                "protect_placeholders": self.protect_placeholders.get(),
                "protect_patterns": self.protect_patterns.get(),
                "dark_mode": self.dark_mode.get(),
                "webhook_url": self.webhook_url.get(),
                "webhook_type": self.webhook_type.get(),
                "refine_mode": self.refine_mode.get(),
                "refine_threshold": self.refine_threshold.get(),
                "refine_workers": self.refine_workers.get(),
                "refine_pre_engine": self.refine_pre_engine.get(),
                "refine_pre_workers": self.refine_pre_workers.get(),
                "pre_api_key": self.pre_api_key.get(),
                "pre_api_id": self.pre_api_id.get(),
                "pre_base_url": self.pre_base_url.get(),
                "pre_model": self.pre_model.get(),
                "pre_email": self.pre_email.get(),
                "refine_llm_base_url": self.refine_llm_base_url.get(),
                "refine_llm_api_key": self.refine_llm_api_key.get(),
                "refine_llm_model": self.refine_llm_model.get(),
                "refine_prompt": self.refine_prompt.get(),
                # 引擎B
                "enable_engine_b": self.enable_engine_b.get(),
                "engine_b_preset": self.engine_b_preset.get(),
                "engine_b_base_url": self.engine_b_base_url.get(),
                "engine_b_model": self.engine_b_model.get(),
                "engine_b_api_key": self.engine_b_api_key.get(),
                "engine_b_workers": self.engine_b_workers.get(),
                "engine_b_batch_size": self.engine_b_batch_size.get(),
            }
            with open(self._config_file, 'w', encoding='utf-8') as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
        except Exception:
            pass  # 自动保存失败静默跳过

    # ==================== 深色主题 ====================
    def _toggle_dark_mode(self):
        """切换深色/浅色主题"""
        if self.dark_mode.get():
            self._apply_dark_theme()
        else:
            self._apply_light_theme()

    def _apply_dark_theme(self):
        """应用深色主题"""
        bg = "#2b2b2b"
        fg = "#e0e0e0"
        accent = "#4a9eff"
        entry_bg = "#3c3c3c"
        self.root.configure(bg=bg)
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure(".", background=bg, foreground=fg, fieldbackground=entry_bg)
        style.configure("TFrame", background=bg)
        style.configure("TLabel", background=bg, foreground=fg)
        style.configure("TButton", background=entry_bg, foreground=fg, bordercolor=entry_bg)
        style.map("TButton", background=[("active", accent)], foreground=[("active", "white")])
        style.configure("TEntry", fieldbackground=entry_bg, foreground=fg, insertcolor=fg)
        style.configure("TCombobox", fieldbackground=entry_bg, foreground=fg, background=entry_bg)
        style.configure("TNotebook", background=bg, borderwidth=0)
        style.configure("TNotebook.Tab", background=entry_bg, foreground=fg, padding=[10, 5])
        style.map("TNotebook.Tab", background=[("selected", accent)], foreground=[("selected", "white")])
        style.configure("TLabelframe", background=bg, foreground=fg)
        style.configure("TLabelframe.Label", background=bg, foreground=fg)
        style.configure("TProgressbar", background=accent, troughcolor=entry_bg)
        style.configure("TSpinbox", fieldbackground=entry_bg, foreground=fg)
        style.configure("TScrollbar", background=entry_bg, troughcolor=bg)
        # 菜单深色
        try:
            self.root.option_add("*Menu.Background", "#3c3c3c")
            self.root.option_add("*Menu.Foreground", "#e0e0e0")
            self.root.option_add("*Menu.activeBackground", "#4a9eff")
            self.root.option_add("*Menu.activeForeground", "white")
        except Exception:
            pass

    def _apply_light_theme(self):
        """恢复浅色主题"""
        self.root.configure(bg="SystemButtonFace")
        style = ttk.Style()
        try:
            style.theme_use("vista")
        except Exception:
            try:
                style.theme_use("clam")
            except Exception:
                pass
        style.configure(".", background="SystemButtonFace", foreground="SystemWindowText", fieldbackground="SystemWindow")
        try:
            self.root.option_add("*Menu.Background", "SystemMenu")
            self.root.option_add("*Menu.Foreground", "SystemMenuText")
        except Exception:
            pass

    # ==================== 插件系统 ====================
    def _load_plugins(self):
        """扫描plugins目录，动态加载自定义翻译引擎"""
        plugin_dir = self._plugin_dir
        if not os.path.exists(plugin_dir):
            try:
                os.makedirs(plugin_dir)
                # 创建示例插件
                sample = os.path.join(plugin_dir, "example_plugin.py")
                if not os.path.exists(sample):
                    with open(sample, 'w', encoding='utf-8') as f:
                        f.write('''# 自定义翻译引擎插件示例
# 复制此文件，修改类名和translate方法即可添加自定义引擎
# 插件会自动出现在引擎下拉列表中

class ExampleTranslator:
    name = "示例自定义引擎"
    need_api_key = False
    need_api_id = False
    need_model = False
    need_base_url = False
    need_email = False

    def __init__(self, api_key="", api_id="", model="", base_url="", email="", timeout=60):
        self.api_key = api_key
        self.timeout = timeout

    def translate(self, text, source_lang, target_lang):
        # 在这里实现你的翻译逻辑
        return text
''')
            except Exception:
                pass
            return

        import sys
        import importlib.util
        count = 0
        for filename in os.listdir(plugin_dir):
            if filename.endswith(".py") and not filename.startswith("_"):
                module_name = filename[:-3]
                try:
                    if module_name in sys.modules:
                        importlib.reload(sys.modules[module_name])
                        module = sys.modules[module_name]
                    else:
                        spec = importlib.util.spec_from_file_location(module_name, os.path.join(plugin_dir, filename))
                        module = importlib.util.module_from_spec(spec)
                        sys.modules[module_name] = module
                        spec.loader.exec_module(module)
                    # 查找插件中的翻译引擎类
                    for attr_name in dir(module):
                        attr = getattr(module, attr_name)
                        if isinstance(attr, type) and hasattr(attr, 'name') and hasattr(attr, 'translate') and attr_name != "TranslatorBase":
                            if attr not in TRANSLATORS and attr.name not in [t.name for t in TRANSLATORS]:
                                TRANSLATORS.append(attr)
                                count += 1
                except Exception as e:
                    self._log(f"加载插件 {filename} 失败: {e}")
        if count > 0:
            self._log(f"已加载 {count} 个自定义翻译引擎插件")
            # 刷新引擎下拉框
            if hasattr(self, 'api_combo'):
                self.api_combo['values'] = [t.name for t in TRANSLATORS]

    def _plugin_manager(self):
        """插件管理器窗口：查看已加载插件、插件文件列表"""
        win = tk.Toplevel(self.root)
        win.title("插件管理器")
        win.geometry("560x420")
        win.transient(self.root)

        # 已加载的插件引擎
        ttk.Label(win, text="已加载的自定义翻译引擎：", font=("微软雅黑", 10, "bold")).pack(padx=15, pady=(10, 5), anchor=tk.W)
        loaded_frame = ttk.Frame(win)
        loaded_frame.pack(padx=15, fill=tk.X)
        plugin_engines = [t for t in TRANSLATORS if hasattr(t, '__module__') and t.__module__ not in ('__main__',)]
        if plugin_engines:
            for eng in plugin_engines:
                ttk.Label(loaded_frame, text=f"  ● {eng.name}  (来自: {eng.__module__}.py)").pack(anchor=tk.W, pady=1)
        else:
            ttk.Label(loaded_frame, text="  （暂无自定义插件，内置引擎不计入）", foreground="gray").pack(anchor=tk.W)

        # plugins目录文件列表
        ttk.Label(win, text="plugins 目录中的文件：", font=("微软雅黑", 10, "bold")).pack(padx=15, pady=(15, 5), anchor=tk.W)
        file_frame = ttk.Frame(win)
        file_frame.pack(padx=15, fill=tk.BOTH, expand=True)
        listbox = tk.Listbox(file_frame, height=8)
        scrollbar = ttk.Scrollbar(file_frame, orient="vertical", command=listbox.yview)
        listbox.configure(yscrollcommand=scrollbar.set)
        listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        plugin_dir = self._plugin_dir
        if os.path.exists(plugin_dir):
            files = sorted([f for f in os.listdir(plugin_dir) if f.endswith('.py')])
            for f in files:
                listbox.insert(tk.END, f"  {f}")
            if not files:
                listbox.insert(tk.END, "  （目录为空）")
        else:
            listbox.insert(tk.END, "  （plugins目录不存在）")

        # 按钮区
        btn_frame = ttk.Frame(win)
        btn_frame.pack(pady=10)
        ttk.Button(btn_frame, text="打开插件目录", command=self._open_plugin_dir).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="新建插件模板", command=self._create_plugin_template).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="刷新并重新加载", command=lambda: [self._load_plugins(), win.destroy(), self._plugin_manager()]).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="关闭", command=win.destroy).pack(side=tk.LEFT, padx=5)

    def _open_plugin_dir(self):
        """在资源管理器中打开plugins目录"""
        plugin_dir = self._plugin_dir
        if not os.path.exists(plugin_dir):
            try:
                os.makedirs(plugin_dir)
            except Exception as e:
                messagebox.showerror("错误", f"无法创建插件目录: {e}")
                return
        try:
            os.startfile(plugin_dir)
        except Exception as e:
            messagebox.showerror("错误", f"无法打开目录: {e}\n\n路径: {plugin_dir}")

    def _create_plugin_template(self):
        """在plugins目录创建一个新的插件模板文件"""
        plugin_dir = self._plugin_dir
        if not os.path.exists(plugin_dir):
            try:
                os.makedirs(plugin_dir)
            except Exception as e:
                messagebox.showerror("错误", f"无法创建插件目录: {e}")
                return
        # 让用户输入插件名
        from tkinter import simpledialog
        name = simpledialog.askstring("新建插件", "请输入插件名称（英文，不含.py）：", parent=self.root)
        if not name:
            return
        name = name.strip().replace(' ', '_')
        if not name.isascii() or not name.replace('_', '').isalnum():
            messagebox.showerror("错误", "插件名只能包含英文字母、数字和下划线")
            return
        filepath = os.path.join(plugin_dir, f"{name}.py")
        if os.path.exists(filepath):
            messagebox.showerror("错误", f"文件已存在: {name}.py")
            return
        template = f'''# 自定义翻译引擎插件: {name}
# 在此实现你的翻译逻辑，保存后在「工具→刷新插件列表」即可生效

class {name.title().replace('_', '')}Translator:
    name = "{name}"
    need_api_key = False
    need_api_id = False
    need_model = False
    need_base_url = False
    need_email = False

    def __init__(self, api_key="", api_id="", model="", base_url="", email="", timeout=60):
        self.api_key = api_key
        self.api_id = api_id
        self.model = model
        self.base_url = base_url
        self.email = email
        self.timeout = timeout

    def translate(self, text, source_lang, target_lang):
        """翻译单条文本，返回翻译后的字符串"""
        # TODO: 在这里实现你的翻译逻辑
        # 示例：直接返回原文
        return text
'''
        try:
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(template)
            messagebox.showinfo("成功", f"插件模板已创建: {name}.py\n\n路径: {filepath}\n\n编辑后点击「工具→刷新插件列表」即可加载")
            os.startfile(plugin_dir)
        except Exception as e:
            messagebox.showerror("错误", f"创建失败: {e}")

    # ==================== WebHook通知 ====================
    def _webhook_settings(self):
        """WebHook通知设置对话框"""
        win = tk.Toplevel(self.root)
        win.title("WebHook通知设置")
        win.geometry("560x320")
        win.transient(self.root)
        win.grab_set()

        ttk.Label(win, text="翻译完成/失败后自动发送通知到以下URL：", wraplength=500).pack(padx=15, pady=(15, 5), anchor=tk.W)
        ttk.Entry(win, textvariable=self.webhook_url, width=60).pack(padx=15, pady=5, fill=tk.X)

        # WebHook类型选择
        type_frame = ttk.Frame(win)
        type_frame.pack(padx=15, pady=(10, 5), fill=tk.X)
        ttk.Label(type_frame, text="通知类型：").pack(side=tk.LEFT)
        type_combo = ttk.Combobox(type_frame, textvariable=self.webhook_type, width=20, state="readonly")
        type_combo['values'] = ["generic", "dingtalk", "wecom"]
        type_combo.pack(side=tk.LEFT, padx=5)
        type_desc = ttk.Label(type_frame, text="通用JSON", foreground="gray")
        type_desc.pack(side=tk.LEFT, padx=10)

        def on_type_change(*args):
            t = self.webhook_type.get()
            if t == "dingtalk":
                type_desc.config(text="钉钉群机器人 (text格式)")
            elif t == "wecom":
                type_desc.config(text="企业微信群机器人 (text格式)")
            else:
                type_desc.config(text="通用JSON POST")
        self.webhook_type.trace_add("write", on_type_change)
        on_type_change()

        ttk.Label(win, text="钉钉/企业微信：在群设置→智能群助手→添加机器人→自定义，获取WebHook URL后填入。", foreground="gray", wraplength=500).pack(padx=15, pady=(10, 5), anchor=tk.W)
        ttk.Label(win, text="通知内容：总条目、已翻译、失败数、耗时、平均速度、引擎、输出文件。", foreground="gray", wraplength=500).pack(padx=15, pady=(0, 5), anchor=tk.W)

        btn_frame = ttk.Frame(win)
        btn_frame.pack(pady=10)
        ttk.Button(btn_frame, text="测试发送", command=lambda: self._send_webhook(test=True)).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="确定", command=win.destroy).pack(side=tk.LEFT, padx=5)

    def _send_webhook(self, test=False):
        """翻译完成/失败后发送WebHook通知，支持通用JSON/钉钉/企业微信"""
        url = self.webhook_url.get().strip()
        if not url:
            if test:
                messagebox.showwarning("提示", "请先填写WebHook URL")
            return
        wtype = self.webhook_type.get()
        # 构造状态文本
        if test:
            status = "测试通知"
            total = translated = failed = 0
            elapsed = avg = 0
        else:
            total = self.count + self.errors
            translated = self.count
            failed = self.errors
            elapsed = round(self.total_elapsed, 1)
            avg = round(self.count / self.total_elapsed, 1) if self.total_elapsed > 0 else 0
            if failed > 0:
                status = f"翻译完成（有{failed}条失败）"
            else:
                status = "翻译完成（全部成功）"

        text = (
            f"【MTool翻译工具】{status}\n"
            f"引擎：{self.api_engine.get()}\n"
            f"总条目：{total}  成功：{translated}  失败：{failed}\n"
            f"耗时：{elapsed}秒  平均速度：{avg}条/秒\n"
            f"输出文件：{self.output_file.get()}\n"
            f"时间：{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )

        # 根据类型构造payload
        if wtype == "dingtalk":
            payload = {"msgtype": "text", "text": {"content": text}}
        elif wtype == "wecom":
            payload = {"msgtype": "text", "text": {"content": text}}
        else:
            payload = {
                "event": "test" if test else "translation_complete",
                "app": "MTool翻译工具",
                "version": VERSION,
                "timestamp": datetime.datetime.now().isoformat(),
                "status": status,
                "total_entries": total,
                "translated": translated,
                "failed": failed,
                "elapsed_seconds": elapsed,
                "avg_speed": avg,
                "engine": self.api_engine.get(),
                "output_file": self.output_file.get(),
                "message": text,
            }

        try:
            data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
            resp = HTTP_POOL.request('POST', url, body=data,
                headers={'Content-Type': 'application/json'}, timeout=10)
            resp_body = resp.data.decode('utf-8', errors='replace')
            if test:
                messagebox.showinfo("成功", f"WebHook测试发送成功！\n\n响应: {resp_body[:200]}")
            else:
                self._log(f"WebHook通知已发送 ({wtype})")
        except Exception as e:
            if test:
                messagebox.showerror("失败", f"WebHook发送失败: {e}")
            else:
                self._log(f"WebHook通知发送失败: {e}")

    def _check_updates_manual(self):
        """手动检查更新，弹出对话框显示结果和下载按钮"""
        def _do_check():
            try:
                # 用列表API取最新正式版（避免/latest缓存延迟）
                list_api = f"https://api.github.com/repos/{GITHUB_REPO}/releases?per_page=10"
                releases = pool_request('GET', list_api,
                    headers={'User-Agent': 'MTool-Translator'}, timeout=15)
                data = None
                for rel in releases:
                    if not rel.get('draft', False) and not rel.get('prerelease', False):
                        data = rel
                        break
                if not data:
                    self.root.after(0, lambda: messagebox.showinfo("检查更新", "未找到正式发布版本"))
                    return
                latest = data.get('tag_name', '').lstrip('v')
                html_url = data.get('html_url', '')
                release_notes = data.get('body', '')
                self.root.after(0, lambda: self._show_update_dialog(latest, html_url, release_notes))
            except Exception as e:
                self.root.after(0, lambda: messagebox.showerror("检查失败", f"无法连接到GitHub检查更新：\n{e}"))
        threading.Thread(target=_do_check, daemon=True).start()

    def _show_update_dialog(self, latest, html_url, release_notes):
        """显示更新检查结果对话框"""
        dlg = tk.Toplevel(self.root)
        dlg.title("检查更新")
        dlg.geometry("520x420")
        dlg.transient(self.root)
        dlg.grab_set()

        if latest and _version_greater(latest, VERSION):
            ttk.Label(dlg, text=f"发现新版本！", font=("微软雅黑", 12, "bold"), foreground="#2196F3").pack(pady=(15, 5))
            ttk.Label(dlg, text=f"当前版本: v{VERSION}    最新版本: v{latest}", font=("微软雅黑", 10)).pack(pady=2)
            ttk.Label(dlg, text="更新说明:", font=("微软雅黑", 10, "bold")).pack(anchor=tk.W, padx=15, pady=(10, 2))
            txt = tk.Text(dlg, height=12, wrap=tk.WORD, font=("微软雅黑", 9))
            txt.pack(fill=tk.BOTH, expand=True, padx=15, pady=2)
            txt.insert(tk.END, release_notes if release_notes else "（无更新说明）")
            txt.config(state=tk.DISABLED)
            btn_frame = ttk.Frame(dlg)
            btn_frame.pack(pady=10)
            def _open_download():
                import webbrowser
                webbrowser.open(html_url)
            ttk.Button(btn_frame, text="前往下载", command=_open_download).pack(side=tk.LEFT, padx=5)
            ttk.Button(btn_frame, text="关闭", command=dlg.destroy).pack(side=tk.LEFT, padx=5)
        else:
            ttk.Label(dlg, text="已是最新版本", font=("微软雅黑", 14, "bold"), foreground="#4CAF50").pack(pady=(40, 10))
            ttk.Label(dlg, text=f"当前版本: v{VERSION}", font=("微软雅黑", 10)).pack(pady=5)
            ttk.Button(dlg, text="确定", command=dlg.destroy).pack(pady=20)

    def _show_about(self):
        """显示关于对话框"""
        messagebox.showinfo("关于", f"MTool翻译工具 v{VERSION}\n\n"
                            f"GitHub: https://github.com/{GITHUB_REPO}\n\n"
                            f"支持多引擎批量翻译、预翻译+LLM精修、\n"
                            f"占位符保护、翻译缓存、插件系统、WebHook通知等功能。")


def _version_greater(v1, v2):
    """比较版本号大小，v1 > v2 返回 True"""
    try:
        parts1 = [int(x) for x in v1.split('.')]
        parts2 = [int(x) for x in v2.split('.')]
        # 补齐长度
        max_len = max(len(parts1), len(parts2))
        parts1 += [0] * (max_len - len(parts1))
        parts2 += [0] * (max_len - len(parts2))
        return parts1 > parts2
    except Exception:
        return v1 > v2


def check_for_updates(root):
    """后台检查GitHub Release是否有新版本，有则弹出带下载按钮的对话框"""
    if GITHUB_REPO.startswith("your-name/"):
        return  # 未配置仓库，跳过
    try:
        # 用列表API取最新正式版（避免/latest缓存延迟）
        list_api = f"https://api.github.com/repos/{GITHUB_REPO}/releases?per_page=10"
        releases = pool_request('GET', list_api,
            headers={'User-Agent': 'MTool-Translator'}, timeout=15)
        # 找第一个非草稿、非预发布的版本
        data = None
        for rel in releases:
            if not rel.get('draft', False) and not rel.get('prerelease', False):
                data = rel
                break
        if not data:
            return
        latest = data.get('tag_name', '').lstrip('v')
        # 只有 latest > 当前版本 才提示（避免旧版本误报）
        if latest and _version_greater(latest, VERSION):
            html_url = data.get('html_url', '')
            release_notes = data.get('body', '')
            root.after(0, lambda: _show_startup_update_dialog(root, latest, html_url, release_notes))
    except Exception:
        pass  # 检查失败静默跳过，不影响使用


def _show_startup_update_dialog(root, latest, html_url, release_notes):
    """启动时发现新版本的对话框（带前往下载按钮）"""
    dlg = tk.Toplevel(root)
    dlg.title("发现新版本")
    dlg.geometry("520x420")
    dlg.transient(root)

    ttk.Label(dlg, text=f"发现新版本 v{latest}（当前 v{VERSION}）", font=("微软雅黑", 12, "bold"), foreground="#2196F3").pack(pady=(15, 5))
    ttk.Label(dlg, text="更新说明:", font=("微软雅黑", 10, "bold")).pack(anchor=tk.W, padx=15, pady=(10, 2))
    txt = tk.Text(dlg, height=14, wrap=tk.WORD, font=("微软雅黑", 9))
    txt.pack(fill=tk.BOTH, expand=True, padx=15, pady=2)
    txt.insert(tk.END, release_notes if release_notes else "（无更新说明）")
    txt.config(state=tk.DISABLED)
    btn_frame = ttk.Frame(dlg)
    btn_frame.pack(pady=10)
    def _open_download():
        import webbrowser
        webbrowser.open(html_url)
    ttk.Button(btn_frame, text="前往下载", command=_open_download).pack(side=tk.LEFT, padx=5)
    ttk.Button(btn_frame, text="稍后再说", command=dlg.destroy).pack(side=tk.LEFT, padx=5)


def main():
    # 尝试启用拖拽支持
    try:
        from tkinterdnd2 import TkinterDnD
        root = TkinterDnD.Tk()
        dnd_available = True
    except Exception:
        root = tk.Tk()
        dnd_available = False
    try:
        root.option_add("*Font", "微软雅黑 9")
    except:
        pass
    try:
        app = TranslatorApp(root)
    except Exception as e:
        import traceback
        err = traceback.format_exc()
        log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "crash_log.txt")
        try:
            with open(log_path, 'w', encoding='utf-8') as f:
                f.write(err)
        except Exception:
            pass
        try:
            messagebox.showerror("启动失败", f"程序启动时出错：\n{e}\n\n详细错误已保存到 crash_log.txt")
        except Exception:
            pass
        return
    if dnd_available:
        app._setup_dnd()
    # 后台检查更新（不阻塞启动）
    threading.Thread(target=check_for_updates, args=(root,), daemon=True).start()
    root.mainloop()


if __name__ == '__main__':
    main()
