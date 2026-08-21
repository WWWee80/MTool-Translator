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
import urllib.request
import urllib.parse
import ssl
import time
import os
import datetime
import threading
import tkinter as tk

# ==================== 版本与更新 ====================
VERSION = "1.0.0"
# GitHub 仓库地址（推送到 GitHub 后改成你的 用户名/仓库名）
GITHUB_REPO = "WWWee80/MTool-Translator"
GITHUB_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
from tkinter import ttk, filedialog, messagebox, scrolledtext
from concurrent.futures import ThreadPoolExecutor, as_completed

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
        self.context = []  # [(原文, 译文), ...] 用于LLM上下文参考
        self.ctx = ssl.create_default_context()
        self.ctx.check_hostname = False
        self.ctx.verify_mode = ssl.CERT_NONE

    def _http_get(self, url, headers=None):
        req = urllib.request.Request(url, headers=headers or {})
        r = urllib.request.urlopen(req, context=self.ctx, timeout=self.timeout)
        return json.loads(r.read().decode('utf-8'))

    def _http_post(self, url, data, headers=None):
        post_data = json.dumps(data).encode('utf-8')
        h = {'Content-Type': 'application/json'}
        if headers:
            h.update(headers)
        req = urllib.request.Request(url, data=post_data, headers=h)
        r = urllib.request.urlopen(req, context=self.ctx, timeout=self.timeout)
        return json.loads(r.read().decode('utf-8'))

    def translate(self, text, source_lang, target_lang):
        raise NotImplementedError


# ==================== 传统翻译引擎 ====================

class GoogleTranslator(TranslatorBase):
    name = "谷歌翻译(免费)"
    def translate(self, text, source_lang, target_lang):
        encoded = urllib.parse.quote(text)
        url = (f'https://translate.googleapis.com/translate_a/single'
               f'?client=gtx&sl={source_lang}&tl={target_lang}&dt=t&q={encoded}')
        data = self._http_get(url, {'User-Agent': 'Mozilla/5.0'})
        return ''.join(seg[0] for seg in data[0] if seg[0])


class BaiduTranslator(TranslatorBase):
    name = "百度翻译"
    need_api_key = True
    need_api_id = True
    def translate(self, text, source_lang, target_lang):
        import hashlib, random
        salt = str(random.randint(32768, 65536))
        sign = hashlib.md5((self.api_id + text + salt + self.api_key).encode()).hexdigest()
        url = 'https://fanyi-api.baidu.com/api/trans/vip/translate'
        params = urllib.parse.urlencode({
            'q': text, 'from': source_lang, 'to': target_lang,
            'appid': self.api_id, 'salt': salt, 'sign': sign
        }).encode()
        req = urllib.request.Request(url, data=params, headers={'Content-Type': 'application/x-www-form-urlencoded'})
        r = urllib.request.urlopen(req, context=self.ctx, timeout=self.timeout)
        data = json.loads(r.read().decode())
        if 'error_code' in data:
            raise Exception(f"百度错误: {data.get('error_msg')}")
        return ''.join(i['dst'] for i in data.get('trans_result', []))


class YoudaoTranslator(TranslatorBase):
    name = "有道翻译"
    need_api_key = True
    need_api_id = True
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
        req = urllib.request.Request(url, data=params, headers={'Content-Type': 'application/x-www-form-urlencoded'})
        r = urllib.request.urlopen(req, context=self.ctx, timeout=self.timeout)
        data = json.loads(r.read().decode())
        if data.get('errorCode') != '0':
            raise Exception(f"有道错误: {data.get('errorCode')}")
        return ''.join(data.get('translation', []))


class DeepLTranslator(TranslatorBase):
    name = "DeepL"
    need_api_key = True
    def translate(self, text, source_lang, target_lang):
        url = 'https://api-free.deepl.com/v2/translate'
        tgt = target_lang.upper().replace('ZH-CN', 'ZH').replace('ZH-TW', 'ZH')
        params = urllib.parse.urlencode({
            'auth_key': self.api_key, 'text': text,
            'source_lang': source_lang.upper(), 'target_lang': tgt
        }).encode()
        req = urllib.request.Request(url, data=params, headers={'Content-Type': 'application/x-www-form-urlencoded'})
        r = urllib.request.urlopen(req, context=self.ctx, timeout=self.timeout)
        data = json.loads(r.read().decode())
        return data['translations'][0]['text']


class MyMemoryTranslator(TranslatorBase):
    name = "MyMemory(免费)"
    need_email = True
    def translate(self, text, source_lang, target_lang):
        encoded = urllib.parse.quote(text)
        url = f'https://api.mymemory.translated.net/get?q={encoded}&langpair={source_lang}|{target_lang}'
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

TRANSLATE_SYSTEM = "你是一个专业的翻译引擎。将用户输入的文本翻译成目标语言，只输出翻译后的文本，不要添加任何解释、注释或引号。保留原文中的换行符、特殊符号和格式。"


class OpenAICompatTranslator(TranslatorBase):
    """通用OpenAI兼容API - 支持硅基流动/Groq/DeepSeek/豆包/百炼/Kimi/智谱/OpenAI等"""
    name = "OpenAI兼容(硅基流动/Groq/DeepSeek等)"
    need_api_key = True
    need_model = True
    need_base_url = True

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
        return result['choices'][0]['message']['content'].strip()


class GeminiTranslator(TranslatorBase):
    """Google Gemini API"""
    name = "Google Gemini"
    need_api_key = True
    need_model = True

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


class OllamaTranslator(TranslatorBase):
    """本地Ollama模型"""
    name = "Ollama本地模型"
    need_model = True
    need_base_url = True

    def translate(self, text, source_lang, target_lang):
        src_name = LANG_NAMES.get(source_lang, source_lang)
        tgt_name = LANG_NAMES.get(target_lang, target_lang)
        base = self.base_url or "http://localhost:11434"
        url = f"{base}/api/chat"
        messages = [{"role": "system", "content": TRANSLATE_SYSTEM}]
        for orig, trans in self.context[-3:]:
            messages.append({"role": "user", "content": f"将以下{src_name}文本翻译成{tgt_name}：{orig}"})
            messages.append({"role": "assistant", "content": trans})
        messages.append({"role": "user", "content": f"将以下{src_name}文本翻译成{tgt_name}，只输出译文：\n{text}"})
        data = {
            "model": self.model, "messages": messages,
            "stream": False, "options": {"temperature": 0.1},
        }
        result = self._http_post(url, data)
        return result['message']['content'].strip()


# ==================== 预设配置 ====================

PRESETS = {
    "硅基流动": {
        "base_url": "https://api.siliconflow.cn/v1",
        "model": "tencent/Hunyuan-MT-7B",
        "hint": "在 https://cloud.siliconflow.cn 注册获取API Key。默认免费模型 tencent/Hunyuan-MT-7B(翻译专用)；付费推荐 Qwen3.5-35B-A3B(性价比最高) 或 DeepSeek-V4-Flash(审核宽松)；点获取模型查看全部"
    },
    "Groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "model": "llama-3.3-70b-versatile",
        "hint": "在 https://console.groq.com 注册，免费、速度极快"
    },
    "DeepSeek": {
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "hint": "在 https://platform.deepseek.com 注册，新用户送500万token"
    },
    "字节豆包": {
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "model": "doubao-1-5-lite-32k-250115",
        "hint": "在 https://console.volcengine.com/ark 注册，有免费额度"
    },
    "阿里云百炼": {
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-turbo",
        "hint": "在 https://bailian.console.aliyun.com 注册，新用户送100万token"
    },
    "月之暗面Kimi": {
        "base_url": "https://api.moonshot.cn/v1",
        "model": "moonshot-v1-8k",
        "hint": "在 https://platform.moonshot.cn 注册"
    },
    "智谱AI": {
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-4-flash",
        "hint": "在 https://open.bigmodel.cn 注册，glm-4-flash免费"
    },
    "OpenAI官方": {
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "hint": "在 https://platform.openai.com 注册，需科学上网"
    },
    "Ollama本地": {
        "base_url": "http://localhost:11434",
        "model": "qwen2.5:7b",
        "hint": "需先安装Ollama(https://ollama.com)并拉取模型：ollama pull qwen2.5:7b"
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
    "Qwen/Qwen2.5-7B-Instruct": 0.0,      # 旧款，可能仍免费
    "Qwen/Qwen2.5-14B-Instruct": 0.0,     # 旧款
    "Qwen/Qwen2.5-72B-Instruct": 0.0,     # 旧款
    # ===== 硅基流动 DeepSeek系列 =====
    "deepseek-ai/DeepSeek-V4-Flash": 2.0,  # 输入1.0 输出2.0 ← 速度快审核松
    "deepseek-ai/DeepSeek-V4-Pro": 24.0,    # 输入12 输出24 ← 顶级质量
    "deepseek-ai/DeepSeek-V3.2": 6.0,       # 输入4.0 输出6.0
    "deepseek-ai/DeepSeek-V3.1-Terminus": 12.0,
    "deepseek-ai/DeepSeek-V3": 2.0,          # 旧款
    "deepseek-ai/DeepSeek-R1": 16.0,         # 旧款推理模型
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
    BaiduTranslator, YoudaoTranslator, DeepLTranslator,
    AzureTranslator, TencentTranslator, AlibabaTranslator, IBMWatsonTranslator,
    OpenAICompatTranslator, GeminiTranslator, OllamaTranslator,
]

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
        self.model_price_map = {}  # 显示名(含价格) -> 真实模型名
        self.preset = tk.StringVar(value="")
        self.max_workers = tk.IntVar(value=8)
        self.max_retry = tk.IntVar(value=3)
        self.chunk_size = tk.IntVar(value=1500)
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

        self._build_ui()
        self._on_api_change()

    def _build_ui(self):
        # 菜单栏
        menubar = tk.Menu(self.root)
        config_menu = tk.Menu(menubar, tearoff=0)
        config_menu.add_command(label="保存配置...", command=self._save_config)
        config_menu.add_command(label="加载配置...", command=self._load_config)
        config_menu.add_separator()
        config_menu.add_command(label="退出", command=self.root.quit)
        menubar.add_cascade(label="配置", menu=config_menu)
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

        ttk.Label(set_frame, text="提示: LLM引擎并发建议设低(4-8)，传统翻译API可设高(16-32)", foreground="gray").grid(row=1, column=0, columnspan=6, sticky=tk.W, padx=5, pady=(5, 0))

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

        # 提示
        self.hint_label = ttk.Label(tab2, text="", foreground="blue", wraplength=800, justify=tk.LEFT)
        self.hint_label.pack(fill=tk.X, pady=5)

        # 引擎说明
        info_frame = ttk.LabelFrame(tab2, text="引擎说明", padding=8)
        info_frame.pack(fill=tk.BOTH, expand=True)
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
        ttk.Button(btn_frame, text="统计信息", command=self._show_stats).pack(side=tk.LEFT, padx=(0, 5))
        ttk.Button(btn_frame, text="费用预估", command=self._show_cost_estimate).pack(side=tk.LEFT, padx=(0, 5))
        ttk.Button(btn_frame, text="详细报告", command=self._show_detailed_stats).pack(side=tk.LEFT)

        # 日志
        log_frame = ttk.LabelFrame(tab3, text="日志", padding=5)
        log_frame.pack(fill=tk.BOTH, expand=True)

        self.log_text = scrolledtext.ScrolledText(log_frame, height=15, font=("Consolas", 9), wrap=tk.WORD)
        self.log_text.pack(fill=tk.BOTH, expand=True)
        self.log_text.config(state=tk.DISABLED)

    def _log(self, msg):
        self.log_text.config(state=tk.NORMAL)
        self.log_text.insert(tk.END, f"[{time.strftime('%H:%M:%S')}] {msg}\n")
        self.log_text.see(tk.END)
        self.log_text.config(state=tk.DISABLED)

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
        try:
            import urllib.request as _req
            headers = {'User-Agent': 'Mozilla/5.0'}
            if api_key:
                headers['Authorization'] = f'Bearer {api_key}'
            req = _req.Request(url, headers=headers)
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
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

            models = sorted(set(models))
            if not models:
                messagebox.showinfo("提示", "未获取到模型列表，API返回格式可能不兼容。")
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

            self.model_entry['values'] = display_names
            # 绑定选中事件
            self.model_entry.bind("<<ComboboxSelected>>", self._on_model_select)

            # 找出免费模型
            free_models = [m for m in models if MODEL_PRICES.get(m) == 0]

            # 如果当前model在列表中，保持选中；否则默认选第一个免费模型或第一个
            current = self.model.get()
            if current not in models:
                if free_models:
                    self.model.set(free_models[0])
                else:
                    self.model.set(models[0])

            self._log(f"获取到 {len(models)} 个模型（免费{free_count}个，已知价格{known_count}个）")
            self._log(f"模型列表: {', '.join(models[:15])}{'...' if len(models) > 15 else ''}")
            msg = f"共获取到 {len(models)} 个模型，已填入Model下拉框（含价格）。\n\n"
            msg += f"免费: {free_count}个 | 已知价格: {known_count}个 | 价格未知: {len(models)-known_count}个\n\n"
            if free_models:
                msg += f"推荐免费模型: {free_models[0]}\n"
            msg += "\n下拉框中 [ ] 内为价格，选中后自动填入真实模型名。"
            messagebox.showinfo("获取成功", msg)
        except Exception as e:
            err_msg = str(e)
            self._log(f"获取模型失败: {err_msg}")
            messagebox.showerror("获取失败", f"无法获取模型列表：\n{err_msg}\n\n请检查Base URL、API Key和网络连接。")

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
        # 更新提示
        hints = {
            "谷歌翻译(免费)": "无需任何参数，直接使用。非官方接口，可能不稳定。",
            "MyMemory(免费)": '匿名5千字符/天；在下方"MyMemory邮箱"填写注册邮箱后，限额提升至5万字符/天。邮箱仅用于API身份识别，无需验证。',
            "百度翻译": "需填写API ID(AppID)和API Key(密钥)。https://fanyi-api.baidu.com",
            "有道翻译": "需填写API ID(应用ID)和API Key(应用密钥)。https://ai.youdao.com",
            "DeepL": "需填写API Key(Auth Key)。https://www.deepl.com/pro-api 免费版50万字符/月",
            "微软翻译(Azure)": "需填写API Key(订阅密钥)和API ID(区域，如eastus)。Base URL可留空用全球端点。https://azure.microsoft.com/services/cognitive-services/translator",
            "腾讯翻译": "需填写API ID(SecretId)和API Key(SecretKey)。https://cloud.tencent.com/product/tmt",
            "阿里翻译": "需填写API ID(AccessKey ID)和API Key(AccessKey Secret)。https://www.aliyun.com/product/ai/alimt",
            "IBM Watson": "需填写API Key(IAM API Key)和Base URL(服务端点)。https://www.ibm.com/cloud/watson-language-translator",
            "OpenAI兼容(硅基流动/Groq/DeepSeek等)": "需填写Base URL、Model、API Key。可从上方快速预设选择，或点获取模型自动拉取。",
            "Google Gemini": "需填写API Key和Model。https://aistudio.google.com 免费层gemini-1.5-flash每分钟15次",
            "Ollama本地模型": "需安装Ollama并拉取模型。Base URL默认http://localhost:11434，Model如qwen2.5:7b",
        }
        self.hint_label.config(text=hints.get(engine_name, ""))

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
        self._log(f"已加载预设: {preset_name}")

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

    def _get_translator(self):
        engine_name = self.api_engine.get()
        engine_cls = next((t for t in TRANSLATORS if t.name == engine_name), GoogleTranslator)
        return engine_cls(
            api_key=self.api_key.get(), api_id=self.api_id.get(),
            model=self.model.get(), base_url=self.base_url.get(),
            email=self.mymemory_email.get()
        )

    def _load_existing(self):
        pf = self.progress_file.get()
        if pf and os.path.exists(pf):
            with open(pf, 'r', encoding='utf-8') as f:
                self.translated = json.load(f)
            self._log(f"加载已有翻译: {len(self.translated)} 条")
        else:
            self._log("没有找到进度文件")

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
        if any(k in msg for k in ['429', 'rate limit', 'too many requests', '限流', 'quota', '频率']):
            return 'rate_limit'
        if any(k in msg for k in ['timeout', 'timed out', '超时', 'connection reset', 'connection refused', '网络']):
            return 'timeout'
        if any(k in msg for k in ['401', '403', 'unauthorized', 'forbidden', 'auth', '密钥', '授权', 'invalid key', 'api key']):
            return 'auth'
        return 'other'

    def _translate_one(self, text, translator, src, tgt):
        if not text or not text.strip():
            return text
        chunk_size = self.chunk_size.get()
        max_retry = self.max_retry.get()

        def _do_translate(t):
            for attempt in range(max_retry + 1):
                # 暂停检查
                while self.is_paused and not self.should_stop:
                    time.sleep(0.3)
                if self.should_stop:
                    return t
                try:
                    result = translator.translate(t, src, tgt)
                    return result if result and result != t else t
                except Exception as e:
                    err_type = self._classify_error(e)
                    # 授权错误不重试（重试也没用）
                    if err_type == 'auth':
                        self.errors += 1
                        self._log(f"授权失败，跳过: {str(e)[:80]}")
                        return t
                    if attempt >= max_retry:
                        self.errors += 1
                        return t
                    # 指数退避：2^attempt 秒，限流额外加倍
                    wait = 2 ** attempt
                    if err_type == 'rate_limit':
                        wait *= 2
                    elif err_type == 'timeout':
                        wait = max(wait, 3)
                    time.sleep(wait)
            return t

        if len(text) > chunk_size:
            chunks = [text[i:i+chunk_size] for i in range(0, len(text), chunk_size)]
            result = ''
            for chunk in chunks:
                r = _do_translate(chunk)
                result += r
                # 更新上下文（仅LLM引擎有效）
                if r != chunk:
                    translator.context.append((chunk, r))
                    if len(translator.context) > 10:
                        translator.context = translator.context[-10:]
            return result if result and result != text else text

        result = _do_translate(text)
        if result != text:
            translator.context.append((text, result))
            if len(translator.context) > 10:
                translator.context = translator.context[-10:]
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

        self.is_running = True
        self.should_stop = False
        self.is_paused = False
        self.count = 0
        self.errors = 0
        self.translated_chars = 0
        self.start_time = time.time()
        self.start_btn.config(state="disabled")
        self.pause_btn.config(state="normal", text="暂停")
        self.stop_btn.config(state="normal")

        pf = self.progress_file.get()
        if pf and os.path.exists(pf):
            with open(pf, 'r', encoding='utf-8') as f:
                self.translated = json.load(f)
            self._log(f"加载已有翻译: {len(self.translated)} 条")

        with open(inf, 'r', encoding='utf-8') as f:
            data = json.load(f)

        to_translate = [k for k in data if k not in self.translated]
        self.total_chars = sum(len(k) for k in to_translate)
        self._log(f"总条目: {len(data)}, 已翻译: {len(self.translated)}, 待翻译: {len(to_translate)}")
        self._log(f"待翻译总字符数: {self.total_chars}")
        self._log(f"引擎: {self.api_engine.get()}, 方向: {src}->{tgt}, 并发: {self.max_workers.get()}")

        # 费用预估
        cost_info = self._estimate_cost(self.total_chars)
        if cost_info:
            self._log(f"费用预估: {cost_info}")

        if not to_translate:
            self._log("全部已翻译，直接生成输出")
            self._save_output(data)
            self._finish()
            return

        self.progress_bar["maximum"] = len(to_translate)
        self.progress_bar["value"] = 0
        threading.Thread(target=self._run_translate, args=(to_translate, data, src, tgt), daemon=True).start()

    def _run_translate(self, to_translate, data, src, tgt):
        translator = self._get_translator()
        start_time = time.time()
        lock = threading.Lock()

        def worker(text):
            if self.should_stop:
                return None
            return (text, self._translate_one(text, translator, src, tgt))

        with ThreadPoolExecutor(max_workers=self.max_workers.get()) as executor:
            futures = {executor.submit(worker, text): text for text in to_translate}
            done = 0
            for future in as_completed(futures):
                if self.should_stop:
                    break
                try:
                    text, result = future.result()
                    if result and result != text:
                        with lock:
                            self.translated[text] = result
                            self.count += 1
                            self.translated_chars += len(text)
                except Exception:
                    self.errors += 1
                done += 1
                if done % 20 == 0 or done == len(to_translate):
                    elapsed = time.time() - start_time
                    rate = done / elapsed if elapsed > 0 else 0
                    remain = (len(to_translate) - done) / rate if rate > 0 else 0
                    self.root.after(0, self._update_progress, done, len(to_translate), rate, remain)
                if done % 200 == 0:
                    self._save_progress()

        self._save_progress()
        self._save_output(data)
        self.root.after(0, self._finish)

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
        self.start_btn.config(state="normal")
        self.pause_btn.config(state="disabled", text="暂停")
        self.stop_btn.config(state="disabled")
        self.status_label.config(text=f"完成！本轮翻译: {self.count} 条, 失败: {self.errors} 条")
        self._log(f"翻译完成！本轮: {self.count} 条, 失败: {self.errors} 条, 耗时: {self.total_elapsed:.1f}秒")
        messagebox.showinfo("完成", f"翻译完成！\n本轮: {self.count} 条\n失败: {self.errors} 条\n耗时: {self.total_elapsed:.1f}秒\n输出: {self.output_file.get()}")

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


def check_for_updates(root):
    """后台检查GitHub Release是否有新版本，有则提示"""
    if GITHUB_REPO.startswith("your-name/"):
        return  # 未配置仓库，跳过
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        req = urllib.request.Request(GITHUB_API, headers={'User-Agent': 'MTool-Translator'})
        resp = urllib.request.urlopen(req, context=ctx, timeout=10)
        data = json.loads(resp.read().decode('utf-8'))
        latest = data.get('tag_name', '').lstrip('v')
        if latest and latest != VERSION:
            download_url = data.get('html_url', '')
            release_notes = data.get('body', '')[:500]
            msg = f"发现新版本 v{latest}（当前 v{VERSION}）\n\n"
            if release_notes:
                msg += f"更新说明:\n{release_notes}\n\n"
            msg += f"下载地址: {download_url}"
            root.after(0, lambda: messagebox.showinfo("发现新版本", msg))
    except Exception:
        pass  # 检查失败静默跳过，不影响使用


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
    app = TranslatorApp(root)
    if dnd_available:
        app._setup_dnd()
    # 后台检查更新（不阻塞启动）
    threading.Thread(target=check_for_updates, args=(root,), daemon=True).start()
    root.mainloop()


if __name__ == '__main__':
    main()
