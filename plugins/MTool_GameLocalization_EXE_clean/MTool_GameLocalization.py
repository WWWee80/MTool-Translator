# -*- coding: utf-8 -*-
"""
MTool 万能游戏本地化 - SExtractor 原生桥接版
直接使用随插件打包的 SExtractor src 引擎模块。
"""
import os, re, sys, json, ast, threading, traceback, importlib, types, pathlib, tempfile, shutil, subprocess, csv, urllib.request

def _norm_rpgmv_ctrl(s):
    """剥 RPG Maker MV/MZ 文本控制码（\\V[n]、\\C[n]、\\I[n]、\\n<…> 等）。
    提到模块级，避免类加载顺序问题。"""
    if not isinstance(s, str):
        return s
    s = re.sub(r"\\[VvNnIiCcGgPpJjSsFfBbAaKk]\s*\[\s*\d+\s*\]", "", s)
    s = re.sub(r"\\[VvNnIiCcGgPpJjSsFfBbAaKk]", "", s)
    s = re.sub(r"\\n<[^>]+>", "", s)
    s = re.sub(r"\\[<>.\\|!~$?#* ]", "", s)
    return s.strip()

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext

# MTool 的插件加载器使用 importlib 从文件路径载入插件，默认不会把 plugins 目录加入 sys.path。
# 先显式加入当前插件目录，确保同目录依赖（如 xp3_adapter.py）稳定可导入。
_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)

from xp3_adapter import XP3Archive, XP3EncryptedError, XP3FormatError, find_external_tool

PLUGIN_NAME = "万能游戏本地化（SExtractor原生）"
RUNTIME_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_SExtractorRuntime")
SRC_DIR = os.path.join(RUNTIME_DIR, "src")
LIBS_DIR = os.path.join(RUNTIME_DIR, "libs")
ENGINE_INI = os.path.join(SRC_DIR, "engine.ini")

def _ensure_runtime_imports():
    for p in (RUNTIME_DIR, SRC_DIR, LIBS_DIR):
        if os.path.isdir(p) and p not in sys.path:
            sys.path.insert(0, p)
    try:
        import rapidjson  # noqa: F401
    except Exception:
        import json as _json
        m = types.ModuleType("rapidjson")
        m.loads = _json.loads
        def _dumps(obj, ensure_ascii=False, write_mode=0, **kwargs):
            indent = 2 if write_mode else None
            return _json.dumps(obj, ensure_ascii=ensure_ascii, indent=indent,
                               separators=None if indent else (",", ":"))
        m.dumps = _dumps
        sys.modules["rapidjson"] = m
    try:
        from libs.lzss import lzss_s  # noqa: F401
    except Exception:
        try:
            fp = os.path.join(LIBS_DIR, "lzss", "lzss_s.py")
            spec = importlib.util.spec_from_file_location("libs.lzss.lzss_s", fp)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            sys.modules["libs.lzss.lzss_s"] = mod
        except Exception:
            pass

# SExtractor 运行时（var_extract / helper_write / common）懒加载：
# helper_write 顶层 import pandas（约 280ms），只有真正执行提取时才需要，
# 放到首次使用时再导入，避免拖慢主程序启动。
EXVAR = None
replaceOrig = None
common = None
_RUNTIME_READY = False

def _ensure_runtime():
    """首次需要提取功能时才加载 SExtractor 运行时。返回是否加载成功。"""
    global EXVAR, replaceOrig, common, _RUNTIME_READY
    if _RUNTIME_READY:
        return EXVAR is not None
    _RUNTIME_READY = True
    _ensure_runtime_imports()
    try:
        from var_extract import gExtractVar as _EXVAR
        from helper_write import replaceOrig as _replaceOrig
        import common as _common
        EXVAR, replaceOrig, common = _EXVAR, _replaceOrig, _common
    except Exception:
        EXVAR = replaceOrig = common = None
    return EXVAR is not None

def _decode_ini_value(v):
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        try:
            return ast.literal_eval(v)
        except Exception:
            return v[1:-1]
    return v

def _parse_engine_ini(path):
    engines = {}
    section = None
    pending_sample = None
    sample_lines = []
    lines = pathlib.Path(path).read_text(encoding="utf-8", errors="replace").splitlines()

    def finish_sample():
        nonlocal pending_sample, sample_lines
        if pending_sample is not None:
            raw = "\n".join(sample_lines)
            if raw.endswith('"""'):
                raw = raw[:-3]
            engines[pending_sample]["sample"] = raw.lstrip("\r\n")
        pending_sample = None
        sample_lines = []

    for line in lines:
        m = re.match(r"^\s*\[([^\]]+)\]\s*$", line)
        if pending_sample is not None:
            if line.strip().endswith('"""'):
                sample_lines.append(line)
                finish_sample()
            else:
                sample_lines.append(line)
            continue
        if m:
            finish_sample()
            section = m.group(1)
            if section.startswith("Engine_"):
                engines.setdefault(section, {})
            continue
        if not section or not section.startswith("Engine_"):
            continue
        stripped = line.strip()
        if not stripped or stripped.startswith(";") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if k != "sample":
            v = v.replace("\\\\", "\\")
        if k == "sample" and v.startswith('"""'):
            body = v[3:]
            if body.endswith('"""'):
                engines[section]["sample"] = body[:-3]
            else:
                pending_sample = section
                sample_lines = [body]
        else:
            engines[section][k] = _decode_ini_value(v)
    finish_sample()
    return engines



def _save_translations(path, data):
    """保存 MTool 兼容翻译字典；保留可选元数据键，主程序会在翻译时忽略 _mtool_meta。"""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


class TypeTreeGenHelper:
    """从游戏的 Managed DLL 生成 typetree，解决序列化文件不带 typetree 时
    UnityPy 无法解析 MonoBehaviour 的问题（依赖可选包 TypeTreeGeneratorAPI）。
    """
    def __init__(self):
        self.gen = None
        self.error = ""
        self._managed_dir = None
        self._node_cache = {}

    def _ensure_generator(self, managed_dir):
        if self.gen is not None and self._managed_dir == managed_dir:
            return self.gen
        if self.gen is not None and self._managed_dir != managed_dir:
            self.gen = None
            self._node_cache = {}
        try:
            from TypeTreeGeneratorAPI import TypeTreeGenerator
        except Exception as e:
            self.error = "未安装 TypeTreeGeneratorAPI: " + str(e)
            return None
        gen = TypeTreeGenerator("6000.0.0", "AssetRipper")
        loaded = 0
        for fn in os.listdir(managed_dir):
            if not fn.lower().endswith(".dll"):
                continue
            try:
                with open(os.path.join(managed_dir, fn), "rb") as f:
                    gen.load_dll(f.read())
                loaded += 1
            except Exception:
                pass
        if not loaded:
            self.error = "Managed 目录中没有可加载的 DLL"
            return None
        self.gen = gen
        self._managed_dir = managed_dir
        return gen

    def _raw_nodes(self, script):
        asm = str(getattr(script, "m_AssemblyName", "") or "")
        ns = str(getattr(script, "m_Namespace", "") or "")
        cls = str(getattr(script, "m_ClassName", "") or "")
        if not cls:
            return None
        if asm.lower().endswith(".dll"):
            asm = asm[:-4]
        fullname = ns + "." + cls if ns else cls
        key = (asm, fullname)
        if key in self._node_cache:
            return self._node_cache[key]
        nodes = None
        try:
            raw = self.gen.get_nodes(asm, fullname)
            nodes = [{"m_Type": n.m_Type, "m_Name": n.m_Name, "m_Level": n.m_Level,
                      "m_MetaFlag": n.m_MetaFlag, "m_ByteSize": 0, "m_Version": 0}
                     for n in raw]
        except Exception:
            nodes = None
        self._node_cache[key] = nodes
        return nodes

    def nodes_for_object(self, obj):
        """根据 MonoBehaviour 的 m_Script 引用生成 typetree 节点；失败返回 None。"""
        gen = self.gen
        if gen is None:
            return None
        try:
            mb = obj.read(check_read=False)
            script = mb.m_Script.read() if mb.m_Script else None
        except Exception:
            return None
        if script is None:
            return None
        return self._raw_nodes(script)


def _locate_us_heap(raw):
    """定位 .NET 程序集的 #US（用户字符串）堆，返回 (堆绝对文件偏移, 堆大小)；失败返回 None。"""
    try:
        if len(raw) < 0x40 or raw[:2] != b"MZ":
            return None
        e_lfanew = int.from_bytes(raw[0x3C:0x40], "little")
        if raw[e_lfanew:e_lfanew+4] != b"PE\x00\x00":
            return None
        coff = e_lfanew + 4
        nsec = int.from_bytes(raw[coff+2:coff+4], "little")
        opt_size = int.from_bytes(raw[coff+16:coff+18], "little")
        opt = coff + 20
        magic = int.from_bytes(raw[opt:opt+2], "little")
        dd = opt + (112 if magic == 0x20B else 96)  # CLR 数据目录
        clr_rva = int.from_bytes(raw[dd+14*8:dd+14*8+4], "little")
        if not clr_rva:
            return None
        sec_tab = opt + opt_size

        def rva2off(rva):
            for i2 in range(nsec):
                s = sec_tab + i2*40
                va = int.from_bytes(raw[s+12:s+16], "little")
                rs = int.from_bytes(raw[s+8:s+12], "little")
                if va <= rva < va + max(rs, 1):
                    pr = int.from_bytes(raw[s+20:s+24], "little")
                    return pr + (rva - va)
            return None

        clr_off = rva2off(clr_rva)
        if clr_off is None:
            return None
        meta_rva = int.from_bytes(raw[clr_off+8:clr_off+12], "little")
        meta_off = rva2off(meta_rva)
        if meta_off is None or raw[meta_off:meta_off+4] != b"BSJB":
            return None
        ver_len = int.from_bytes(raw[meta_off+12:meta_off+16], "little")
        p = meta_off + 16 + ver_len
        p += 2 + 2  # flags + stream count
        nstreams = int.from_bytes(raw[p-2:p], "little")
        for _ in range(nstreams):
            off = int.from_bytes(raw[p:p+4], "little")
            size = int.from_bytes(raw[p+4:p+8], "little")
            q = p + 8
            while raw[q]:
                q += 1
            name = raw[p+8:q].decode("ascii", "ignore")
            p = q + 1
            p = meta_off + ((p - meta_off + 3) & ~3)  # align to 4 within file
            if name == "#US":
                return meta_off + off, size
        return None
    except Exception:
        return None


def _iter_us_entries(heap):
    """遍历 #US 堆条目，yield (start, hdr, ln, text)。
    hdr/ln 为长度前缀信息，text 已剥离末尾 flag 字符。"""
    i = 0
    while i < len(heap) - 1:
        b = heap[i]
        if b & 0x80 == 0:
            ln = b & 0x7F
            hdr = 1
        elif b & 0xC0 == 0x80:
            ln = ((b & 0x3F) << 8) | heap[i+1]
            hdr = 2
        else:  # 4字节形式（.NET 规范为大端，元数据压缩整数没有 3 字节形式）
            if i+3 >= len(heap):
                break
            ln = ((b & 0x3F) << 24) | (heap[i+1] << 16) | (heap[i+2] << 8) | heap[i+3]
            hdr = 4
        if ln == 0:  # 空字符串（堆首及中间都可能出现）
            i += hdr
            continue
        if ln < 1 or i+hdr+ln > len(heap):
            break
        data = heap[i+hdr: i+hdr+ln]
        if len(data) % 2:
            data = data[:-1]  # 末尾可能是不完整字节
        try:
            text = data.decode("utf-16-le")
        except Exception:
            i += hdr + ln
            continue
        # 条目末尾附一个带结束 flag 的字符（\x00/\x01）
        if text.endswith("\x00") or text.endswith("\x01"):
            text = text[:-1]
        start = i
        i += hdr + ln
        if isinstance(text, str) and text.strip():
            yield start, hdr, ln, text


def _parse_dotnet_us_heap(dll_path):
    """解析 .NET 程序集的 #US（用户字符串）堆，返回 [(heap_offset, text), ...]。
    游戏内嵌对白大多以字符串常量形式存放在 Assembly-CSharp.dll 的 #US 堆中。
    """
    out = []
    try:
        with open(dll_path, "rb") as f:
            raw = f.read()
        loc = _locate_us_heap(raw)
        if not loc:
            return out
        heap = raw[loc[0]: loc[0]+loc[1]]
        for start, _hdr, _ln, text in _iter_us_entries(heap):
            out.append((start, text))
    except Exception:
        return out
    return out


# 纯 ASCII 标识符（value/KOMA/this/Item...）：这类字符串常是 ES3 存档键、JSON 属性名
# （"value"）、API 内部字面量，回填翻译会直接损坏玩家存档结构，必须一律跳过。
_ASCII_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,31}")
# 短词保护（≤8字符且无句读/空白）：品种名/菜单词/对象名（ニワトリ、カモ…）常被
# GameObject.Find / 字典键 / 存档键按原文引用，回填后与旧存档或资源名失配 → 空引用。
# 长对白不受影响；这些短词由 XUnity 运行时层继续翻译。
_SHORT_WORD_RE = re.compile(r"^[^\s。、！？!?.,…「」『』【】()（）\-]{1,8}$")


def _patch_dotnet_us_heap(dll_path, translations, max_report=30):
    """原位覆写 #US 堆字符串常量（游戏内嵌对白的离线汉化核心）。

    关键约束：条目总长必须保持不变——#US 中的字符串由 ldstr token 按堆内偏移引用，
    若某条目长度变化，其后所有字符串的偏移全部错位。因此：
      - 译文 UTF-16 字节数 ≤ 原文字符区时：覆写 + 空格填充到等长；
      - 译文更长时：跳过该条目（保留日文，交给 XUnity 等运行时方案兜底）。
    返回 {"replaced", "skipped", "skipped_samples", "changed", ["error"]}。
    """
    rep = {"replaced": 0, "skipped": 0, "skipped_samples": [], "changed": False}
    try:
        with open(dll_path, "rb") as f:
            raw = bytearray(f.read())
        loc = _locate_us_heap(bytes(raw))
        if not loc:
            return rep
        off0, size = loc
        heap = bytearray(raw[off0:off0+size])
        for start, hdr, ln, text in _iter_us_entries(bytes(heap)):
            trans = translations.get(text)
            if not isinstance(trans, str) or not trans or trans == text:
                continue
            # 防污染保护：纯 ASCII 标识符一律不回填（存档键/序列化属性名）
            if _ASCII_IDENT_RE.fullmatch(text):
                rep["skipped"] += 1
                if len(rep["skipped_samples"]) < max_report:
                    rep["skipped_samples"].append("[标识符]" + text[:36])
                continue
            # 短词保护：无标点短词可能是对象名/查找键/存档键
            if _SHORT_WORD_RE.fullmatch(text.strip()):
                rep["skipped"] += 1
                if len(rep["skipped_samples"]) < max_report:
                    rep["skipped_samples"].append("[短词]" + text[:36])
                continue
            # 纯 ASCII（可含空格/标点）：Unity 内部对象名（如 "Main Camera"）、
            # 引擎字面量、API 键。GameObject.Find("Main Camera") 按名查找，
            # 回填翻译后查找失败 → Awake 阶段 NullReferenceException。
            if text.isascii():
                rep["skipped"] += 1
                if len(rep["skipped_samples"]) < max_report:
                    rep["skipped_samples"].append("[ASCII]" + text[:36])
                continue
            blob_at = start + hdr
            flag = heap[blob_at + ln - 1]  # 保留条目末尾的 flag 字节
            usable = ln - 1  # 字符区字节数（UTF-16LE；规范上 ln=2n+1，恒可整除）
            tb = trans.encode("utf-16-le")
            pad = b"\x20\x00" * ((usable - len(tb)) // 2)  # 空格填充
            newblob = tb + pad + bytes([flag])
            if len(tb) > usable or len(newblob) != ln:
                rep["skipped"] += 1
                if len(rep["skipped_samples"]) < max_report:
                    rep["skipped_samples"].append(text[:40])
                continue
            heap[blob_at: blob_at+ln] = newblob
            rep["replaced"] += 1
        if rep["replaced"]:
            raw[off0:off0+size] = heap
            with open(dll_path, "wb") as f:
                f.write(bytes(raw))
            rep["changed"] = True
    except Exception as e:
        rep["error"] = f"{type(e).__name__}: {e}"
    return rep


def _patch_serialized_text_blobs(asset_path, translations, max_report=30):
    """Unity 序列化资源里“无定位对白”的长度前缀原位覆写。

    背景：结构化解析失败的 MonoBehaviour（缺 typetree / ttgen 也拿不到）常把整段对白
    存成 int32 小端长度 + UTF-8/UTF-16 的串。提取时二进制补扫（_scan_prefixed）能把这些
    串捞进翻译表，但因为没解析出对象，写回阶段拿不到 file/path_id/field 定位，只能干看着
    ——这正是“翻了几千条只写回几十处”的根因。

    这里在输出副本上对每个文件再扫一遍长度前缀窗口，双重锚定（前缀长度合法 且 内容恰好
    解码等于某条原文）才写：译文字节 ≤ 原串则原位覆写并补空格到等长——前缀不动、总长不变，
    序列化偏移完全安全，等价于 DLL #US 的等长覆写；译文更长则跳过（交给运行时方案兜底）。
    """
    rep = {"replaced": 0, "skipped": 0, "skipped_samples": [], "changed": False}
    try:
        with open(asset_path, "rb") as f:
            data = bytearray(f.read())
    except Exception as e:
        rep["error"] = f"{type(e).__name__}: {e}"
        return rep
    n = len(data)
    i = 0
    replaced = skipped = 0
    samples = []

    def _sample(label, text):
        if len(samples) < max_report:
            samples.append("[" + label + "]" + text[:36])

    while i + 4 <= n:
        ln = int.from_bytes(data[i:i + 4], 'little')
        if 1 <= ln <= 8192 and i + 4 + ln <= n:
            b = bytes(data[i + 4:i + 4 + ln])
            s = None
            enc = None
            try:
                s = b.decode('utf-8')
                enc = 'utf-8'
            except Exception:
                try:
                    s = b.decode('utf-16-le')
                    enc = 'utf-16-le'
                except Exception:
                    s = None
            if s is not None and not (enc == 'utf-16-le' and (len(b) % 2)):
                t = translations.get(s)
                if isinstance(t, str) and t and t != s:
                    # 与 DLL #US 一致的保护：纯 ASCII / 标识符 / 无标点短词是
                    # 对象名或查找键，写翻译会破坏 Find/回调 → 一律不写。
                    if s.isascii() or _ASCII_IDENT_RE.fullmatch(s) or _SHORT_WORD_RE.fullmatch(s.strip()):
                        skipped += 1
                        _sample("标识符/短词", s)
                    elif enc == 'utf-8':
                        tb = t.encode('utf-8')
                        if len(tb) <= ln:
                            data[i + 4:i + 4 + ln] = tb + b'\x20' * (ln - len(tb))
                            replaced += 1
                        else:
                            skipped += 1
                            _sample("译文过长", s)
                    else:  # utf-16-le：等码元覆写，补 U+0020 到等长
                        tb = t.encode('utf-16-le')
                        if len(tb) <= ln:
                            data[i + 4:i + 4 + ln] = tb + b'\x20\x00' * ((ln - len(tb)) // 2)
                            replaced += 1
                        else:
                            skipped += 1
                            _sample("译文过长", s)
        i += 1
    if replaced:
        try:
            with open(asset_path, "wb") as f:
                f.write(bytes(data))
            rep["changed"] = True
        except Exception as e:
            rep["error"] = f"写入失败 {type(e).__name__}: {e}"
    rep["replaced"] = replaced
    rep["skipped"] = skipped
    rep["skipped_samples"] = samples
    return rep


# 部分汉化流程会把原文换行(\n / \r\n)写成“换行占位标记”以便过模型，例如 〔NL0〕、
# 〔NL0]、[NL0]、〔NL1〕。这些标记在原文数据里并不存在，若原样写回，游戏会把它们当
# 普通字符显示出来（表现为文本里冒出 〔NL0] / 〔NL1] / [NLO] 之类）。写回时统一还原成换行。
_NL_MARKER_RE = re.compile(r'[〔\[]\s*NL\s*[A-Za-z0-9]+\s*[\]〕]')


def _sanitize_translations(translations):
    """就地清理翻译表：仅改写“含换行占位标记”的译文字符串，_mtool_meta 等不受影响。"""
    if isinstance(translations, dict):
        for k, v in list(translations.items()):
            if isinstance(v, str) and _NL_MARKER_RE.search(v):
                translations[k] = _NL_MARKER_RE.sub('\n', v)
    return translations


class UnityPyBackend:
    """Unity 主后端：可选 UnityPy 结构化解析。
    目标：优先读取 SerializedFile / AssetBundle 中的 TextAsset、MonoBehaviour、脚本化对象，
    并为每条文本记录 file/path_id/type/field 路径，便于后续精确写回。
    序列化文件未内嵌 typetree 时，用 TypeTreeGeneratorAPI 从 Managed DLL 生成 typetree 解析。
    UnityPy 未安装时不抛到主流程，由 BackendManager 自动降级。
    """
    TEXT_CLASS_NAMES = {"TextAsset", "MonoBehaviour", "ScriptableObject"}
    UNITY_EXTS = {".assets", ".sharedassets", ".resource", ".ress", ".bundle", ".unity3d", ".level"}
    # 引擎/调试自带的英文占位，对翻译没有价值
    TEXTASSET_JUNK_RE = re.compile(
        r'^\s*\{?"?(MeasurementCount|TestSuite)"?\s*[:=]', re.I)

    def __init__(self, filter_enabled=True, filter_mode="标准"):
        self.filter_enabled = filter_enabled
        self.filter_mode = filter_mode
        self.available = False
        self.error = ""
        self.UnityPy = None
        self.ttgen = TypeTreeGenHelper()
        try:
            import UnityPy  # type: ignore
            self.UnityPy = UnityPy
            self.available = True
        except Exception as e:
            self.error = str(e)

    def _should_parse_obj(self, obj):
        try:
            name = obj.type.name
            return name in self.TEXT_CLASS_NAMES or "Localization" in name or "StringTable" in name
        except Exception:
            return False

    @staticmethod
    def _obj_type_name(obj):
        try:
            return obj.type.name
        except Exception:
            return str(getattr(obj, "type", "Unknown"))

    @staticmethod
    def _safe_container(obj):
        try:
            return obj.container or ""
        except Exception:
            return ""

    @classmethod
    def _walk_strings(cls, value, path="", limit=200, out=None):
        if out is None:
            out=[]
        if len(out) >= limit:
            return out
        if isinstance(value, str):
            out.append((path, value))
        elif isinstance(value, dict):
            for k,v in value.items():
                cls._walk_strings(v, f"{path}.{k}" if path else str(k), limit, out)
                if len(out) >= limit:
                    break
        elif isinstance(value, (list, tuple)):
            for i,v in enumerate(value):
                cls._walk_strings(v, f"{path}[{i}]", limit, out)
                if len(out) >= limit:
                    break
        else:
            # UnityPy class wrappers expose fields through __dict__.
            d=getattr(value, "__dict__", None)
            if isinstance(d, dict):
                for k,v in d.items():
                    if k in {"object_reader", "assets_file", "__node__"}:
                        continue
                    if k.startswith("_"):
                        continue
                    cls._walk_strings(v, f"{path}.{k}" if path else k, limit, out)
                    if len(out) >= limit:
                        break
        return out

    @staticmethod
    def _filter(text, enabled, mode):
        if not isinstance(text, str) or not text.strip():
            return False
        if not enabled:
            return True
        ok, _, _ = TextQualityFilter.accept(text, mode)
        return ok

    def _candidate_files(self, root):
        files=[]
        for dp, dirs, fs in os.walk(root):
            dirs[:] = [d for d in dirs if d.lower() not in {"_mtool_output", "_data_backup", "_font_backup", "__pycache__", ".git"}]
            for fn in fs:
                low=fn.lower(); ext=os.path.splitext(low)[1]
                if ext in self.UNITY_EXTS or low.startswith("globalgamemanagers") or low.startswith("sharedassets") or low.startswith("level"):
                    files.append(os.path.join(dp, fn))
        return sorted(files)

    def _managed_dir(self, root):
        for dp, dirs, fs in os.walk(root):
            if dp.endswith(("Managed", "Managed\\")) or os.path.basename(dp) == "Managed":
                return dp
            # 避免深入无关的巨型目录
            dirs[:] = [d for d in dirs if d.lower() not in {"_mtool_output", "_data_backup", "__pycache__"}]
        return None

    def _managed_assemblies(self, root):
        """列出参与文本提取/回填的 Managed 程序集（游戏代码程序集，跳过系统 BCL）。"""
        managed = self._managed_dir(root)
        if not managed:
            return []
        out = []
        for fn in os.listdir(managed):
            if not fn.lower().endswith(".dll"):
                continue
            if not (fn.lower().startswith("assembly-csharp") or "firstpass" in fn.lower()):
                continue
            fp = os.path.join(managed, fn)
            try:
                if os.path.getsize(fp) <= 64 * 1024 * 1024:
                    out.append(fp)
            except OSError:
                continue
        return out

    def _extract_dll_strings(self, root, result, locations, stat):
        """提取 Managed 程序集 #US 堆中的字符串常量（游戏对白的主要载体）。"""
        managed = self._managed_dir(root)
        if not managed:
            return
        for fn in os.listdir(managed):
            if not fn.lower().endswith(".dll"):
                continue
            fp = os.path.join(managed, fn)
            # 跳过纯系统/引擎程序集，避免把 .NET BCL 的报错文案当游戏文本
            if not (fn.lower().startswith("assembly-csharp") or "firstpass" in fn.lower()):
                continue
            try:
                size_ok = os.path.getsize(fp) <= 64 * 1024 * 1024
            except OSError:
                continue
            if not size_ok:
                continue
            rel = os.path.relpath(fp, root).replace("\\", "/")
            added = 0
            for off, text in _parse_dotnet_us_heap(fp):
                text = text.strip("\ufeff\x00\r\n\t ")
                if len(text) < 2:
                    continue
                if self.TEXTASSET_JUNK_RE.match(text):
                    continue
                if not self._filter(text, self.filter_enabled, self.filter_mode):
                    stat["filtered"] = stat.get("filtered", 0) + 1
                    continue
                result.setdefault(text, "")
                locations.setdefault(text, []).append(
                    {"file": rel, "path_id": 0, "type": "DotNetString",
                     "container": "", "field": f"#US@{off}"})
                added += 1
            stat["dll_strings"] = stat.get("dll_strings", 0) + added

    def extract(self, root, progress=None, filter_enabled=None, filter_mode=None):
        if not self.available:
            raise RuntimeError("UnityPy 未安装：" + (self.error or "unknown error"))
        # 以本次 UI 设置为准；旧版本这里固定使用初始化时的过滤配置，
        # 导致用户关闭过滤后 UnityPy 仍会过滤掉真实对白。
        if filter_enabled is not None:
            self.filter_enabled = bool(filter_enabled)
        if filter_mode is not None:
            self.filter_mode = filter_mode
        result={}
        locations={}
        errors=[]
        files=self._candidate_files(root)
        parsed_objects=0
        filtered=0
        mb_unparsed=0  # 结构化解析失败（缺 typetree 且 ttgen 也拿不到）的 MonoBehaviour 数
        stat_extra={"filtered":0,"dll_strings":0,"filter_reasons":{},"errors":[]}
        # 预热 typetree 生成器（需要游戏 Managed 目录）
        managed=self._managed_dir(root)
        if managed:
            self.ttgen._ensure_generator(managed)
        for idx,fp in enumerate(files,1):
            rel=os.path.relpath(fp,root).replace('\\','/')
            before=len(result)
            try:
                env=self.UnityPy.load(fp)
                for obj in env.objects:
                    if not self._should_parse_obj(obj):
                        continue
                    parsed_objects += 1
                    typ=self._obj_type_name(obj)
                    path_id=getattr(obj,'path_id',0)
                    container=self._safe_container(obj)
                    # TextAsset：直接抓 m_Script（m_Name 是资源标识符，不抓）
                    if typ == "TextAsset":
                        try:
                            inst=obj.parse_as_object()
                            vals=[]
                            if isinstance(getattr(inst,'m_Script',None), str): vals.append(('m_Script', inst.m_Script))
                        except Exception:
                            vals=[]
                            try: vals.extend(self._walk_strings(obj.parse_as_dict(), limit=20))
                            except Exception: pass
                    else:
                        vals=[]; parsed_ok=False
                        try:
                            vals=self._walk_strings(obj.parse_as_dict(), limit=300); parsed_ok=True
                        except Exception:
                            pass
                        if not vals and typ == "MonoBehaviour":
                            # 序列化文件未带 typetree：从 Managed DLL 生成后重试
                            nodes=self.ttgen.nodes_for_object(obj)
                            if nodes:
                                try:
                                    vals=self._walk_strings(obj.read_typetree(nodes), limit=300); parsed_ok=True
                                except Exception:
                                    vals=[]
                            if not parsed_ok:
                                # 结构化通道彻底拿不到——对白常锁在这类 MB 里，记数以便补跑二进制扫描
                                mb_unparsed += 1
                    for field,text in vals:
                        if field == 'm_Name':
                            continue  # 对象名是逻辑标识符（Find/引用按名字），翻译会导致空引用崩溃
                        text=text.strip('\ufeff\x00\r\n\t ')
                        if len(text) < 2 or self.TEXTASSET_JUNK_RE.match(text):
                            continue
                        if not self._filter(text,self.filter_enabled,self.filter_mode):
                            filtered += 1
                            continue
                        result.setdefault(text, "")
                        loc={"file":rel,"path_id":int(path_id or 0),"type":typ,"container":container,"field":field}
                        locations.setdefault(text, []).append(loc)
            except Exception as e:
                errors.append(f"{rel}: {type(e).__name__}: {e}")
            if progress:
                try: progress(idx, max(1,len(files)), rel, len(result)-before)
                except Exception: pass
        # Managed 程序集字符串常量：多数 Unity 游戏的对白主体
        try:
            self._extract_dll_strings(root, result, locations, stat_extra)
        except Exception as e:
            errors.append(f"Managed DLL 字符串提取失败: {type(e).__name__}: {e}")
        # 散落文本文件（StreamingAssets/游戏目录下的 json/csv/txt/utf16 等）：
        # UnityPy 只覆盖序列化资源，这些文件传统深度扫描通道才认得——此前在
        # UnityPy 可用时被整体跳过，导致明文散落文本全部漏扫。
        text_scanned = 0
        try:
            text_scanned = self._extract_text_files(root, result, stat_extra)
        except Exception as e:
            errors.append(f"散落文本扫描失败: {type(e).__name__}: {e}")
        # 无 typetree 的 MonoBehaviour（Utage/宴 等自定义剧本组件常见）解析不出——
        # 对已列入候选的结构化序列化文件补跑一次二进制字符串扫描（Unity 长度前缀
        # UTF-8/UTF-16 通道），把锁在里面的对白捞出来。显式跳过 .resS/.resource
        # 纯资源流与超大文件，避免扫 1GB 音画流的无谓耗时与噪声。仅在确有解析失败
        # 时触发：完全解析成功的游戏不跑，免得引入二进制扫描噪声。
        unity_bin_scanned = 0
        if mb_unparsed > 0:
            try:
                unity_bin_scanned = self._scan_unity_serialized(files, result, stat_extra)
            except Exception as e:
                errors.append(f"Unity 序列化补扫失败: {type(e).__name__}: {e}")
        # UnityPy 对某些自定义 MonoBehaviour / Bundle 只能看到少量对象时，
        # 立即补跑深度扫描。否则“UnityPy 成功”会阻止传统扫描兜底，最终可能只剩 0~1 条。
        if len(result) <= 1:
            try:
                fallback_stat = UnityDeepExtractor.scan(
                    root, result, min_len=2,
                    filter_enabled=self.filter_enabled,
                    filter_mode=self.filter_mode,
                    progress=progress, deep=True)
                errors.extend(fallback_stat.get("errors", []))
                stat_extra["fallback_scanned"] = True
                stat_extra["fallback_items"] = fallback_stat.get("items", 0)
            except Exception as e:
                errors.append(f"Unity 深度兜底失败: {type(e).__name__}: {e}")
        meta={"engine":"unity","backend":"UnityPy","locations":locations,"parsed_objects":parsed_objects}
        if self.ttgen.gen is None and self.ttgen.error:
            meta["ttgen"]="不可用（"+self.ttgen.error+"）；MonoBehaviour 解析受限"
        result["_mtool_meta"]=meta
        errors.extend(stat_extra.get("errors",[]))
        stat={"files":len(files),"items":len(result)-1 if "_mtool_meta" in result else len(result),
              "filtered":filtered+stat_extra.get("filtered",0),"errors":errors,"backend":"UnityPy",
              "filter_reasons":stat_extra.get("filter_reasons",{}),
              "locations":locations,"parsed_objects":parsed_objects,"dll_strings":stat_extra.get("dll_strings",0),
              "text_files_scanned":text_scanned,"mb_unparsed":mb_unparsed,"unity_bin_scanned":unity_bin_scanned}
        return result,stat

    def _scan_unity_serialized(self, files, result, stat):
        """对已列入候选的结构化序列化文件补跑二进制字符串扫描，恢复锁在无 typetree
        的 MonoBehaviour 里的文本（Utage/宴 剧本等）。跳过 .resS/.resource 纯资源流
        与 >256MB 的大文件，只扫 .assets/level*/globalgamemanagers 这类小体积结构化档。
        只跑 prefixed 通道：runs/utf16 在结构化序列化数据上几乎全是把 ASCII 误读成
        UTF-16 的乱码（实测 5000 条里仅 ~24 条真日文），会严重污染翻译表。"""
        scanned = 0
        for fp in files:
            low = os.path.basename(fp).lower()
            if low.endswith(('.ress', '.resource')):
                continue  # 纯资源流（音频/贴图/视频），无结构化文本，且动辄上 GB
            try:
                if os.path.getsize(fp) > 256 * 1024 * 1024:
                    continue
            except OSError:
                continue
            UnityDeepExtractor._scan_binary_file(fp, result, 2, self.filter_enabled, self.filter_mode, stat,
                                                 channels=('prefixed',), require_jp=True)
            scanned += 1
        return scanned

    # 散落文本文件：仅扫"数据型"文本扩展名；代码/网页类（.js/.lua/.html…）不进翻译表
    SCATTERED_TEXT_EXTS = {'.txt', '.json', '.csv', '.tsv', '.xml', '.yaml', '.yml',
                           '.ini', '.cfg', '.conf', '.bytes', '.lang', '.loc', '.locale'}
    SCATTERED_SKIP_FILES = {'output_log.txt', 'player.log', 'player-prev.log',
                            'error.log', 'boot.config', 'global-metadata.dat'}
    SCATTERED_SKIP_DIRS = {'_mtool_output', '_data_backup', '_font_backup', '__pycache__',
                           '.git', 'logs', 'crash', 'crashes', 'save', 'saves', 'savedata'}
    # 回写只允许这几种：格式化安全（json结构化 / 整行精确替换）
    SCATTERED_INJECT_EXTS = {'.json', '.txt', '.csv', '.tsv', '.bytes'}
    _STRIP_INVISIBLE = '\ufeff\x00\r\n\t \u200b\u200c\u200d\u2060\u00ad'

    def _extract_text_files(self, root, result, stat):
        scanned = 0
        for dp, dirs, fs in os.walk(root):
            dirs[:] = [d for d in dirs if d.lower() not in self.SCATTERED_SKIP_DIRS and not d.startswith('.')]
            for fn in fs:
                if fn.lower() in self.SCATTERED_SKIP_FILES:
                    continue
                ext = os.path.splitext(fn.lower())[1]
                if ext not in self.SCATTERED_TEXT_EXTS:
                    continue
                fp = os.path.join(dp, fn)
                try:
                    if os.path.getsize(fp) > 64 * 1024 * 1024:
                        continue
                except OSError:
                    continue
                before = len(result)
                UnityDeepExtractor._scan_text_file(fp, result, 2, self.filter_enabled,
                                                   self.filter_mode, stat)
                scanned += 1
                _ = before
        return scanned

    def _inject_text_files(self, out_dir, translations, errors):
        """散落文本文件的安全写回：json 走结构化值替换（不碰键名），其余只做
        整行精确替换；匹配键与提取侧同一套剥离规则，保证回写命中。"""
        total = 0
        for dp, dirs, fs in os.walk(out_dir):
            dirs[:] = [d for d in dirs if d.lower() not in self.SCATTERED_SKIP_DIRS and not d.startswith('.')]
            for fn in fs:
                ext = os.path.splitext(fn.lower())[1]
                if ext not in self.SCATTERED_INJECT_EXTS:
                    continue
                fp = os.path.join(dp, fn)
                try:
                    if os.path.getsize(fp) > 16 * 1024 * 1024:
                        continue
                    with open(fp, 'rb') as f:
                        raw = f.read()
                    # 编码探测与提取侧 _scan_text_file 对齐：UTF-16(BOM) 文本若硬按
                    # utf-8 解码会整文件 UnicodeDecodeError，导致该文件可翻行全部漏写。
                    has_bom = raw.startswith(b'\xef\xbb\xbf')
                    has_bom16 = raw[:2] in (b'\xff\xfe', b'\xfe\xff')
                    nul_density = (raw.count(0) / len(raw)) if raw else 0
                    enc_order = ['utf-8-sig', 'utf-8']
                    if has_bom16:
                        enc_order.insert(0, 'utf-16')
                    elif len(raw) % 2 == 0 and raw.count(0) >= max(4, len(raw) // 8):
                        enc_order += ['utf-16-le', 'utf-16-be']
                    enc_order += ['cp932', 'shift_jis', 'gbk', 'big5']
                    text = None
                    chosen_enc = None
                    for enc in enc_order:
                        try:
                            t = raw.decode(enc)
                            if UnityDeepExtractor._textish(t):
                                text = t
                                chosen_enc = enc
                                break
                        except Exception:
                            pass
                    if text is None:
                        continue  # 识别不了编码的文件不碰，避免把二进制当文本写坏
                    changed = 0
                    if ext in ('.json', '.bytes') and text.lstrip()[:1] in ('{', '['):
                        try:
                            obj = json.loads(text)
                        except Exception:
                            obj = None
                        if obj is not None:
                            changed = self._replace_json_values(obj, translations)
                            if changed:
                                with open(fp, 'w', encoding='utf-8') as f:
                                    json.dump(obj, f, ensure_ascii=False, indent=2)
                    else:
                        lines = text.splitlines(keepends=True)
                        out_lines = []
                        for line in lines:
                            body = line.rstrip('\r\n')
                            eol = line[len(body):]
                            key = body.strip(self._STRIP_INVISIBLE)
                            dst = translations.get(key)
                            if isinstance(dst, str) and dst and dst != key:
                                out_lines.append(dst + eol)
                                changed += 1
                            else:
                                out_lines.append(line)
                        if changed:
                            # 按原编码回写：BOM/无 BOM 保持一致；先整段编码成功再落盘，
                            # 防止目标编码收不下译文时把文件截成空。
                            if chosen_enc == 'utf-8-sig' and not has_bom:
                                out_enc = 'utf-8'
                            else:
                                out_enc = chosen_enc
                            try:
                                out_bytes = ''.join(out_lines).encode(out_enc)
                            except Exception:
                                out_bytes = None
                            if out_bytes is None:
                                errors.append(f"{os.path.relpath(fp, out_dir)}: 译文含 {out_enc} 无法表示的字符，已跳过")
                            else:
                                with open(fp, 'wb') as f:
                                    f.write(out_bytes)
                    total += changed
                except Exception as e:
                    errors.append(f"{os.path.relpath(fp, out_dir)}: 散落文本回写失败 {type(e).__name__}: {e}")
        return total

    @staticmethod
    def _replace_json_values(obj, translations):
        """只替换 JSON 的字符串"值"，绝不改键名/结构；自引用防护64层。
        回退匹配：先做严格查，命中失败再用 _norm_rpgmv_ctrl 剥控制码再查。"""
        n = 0
        STRIP = '\ufeff\x00\r\n\t \u200b\u200c\u200d\u2060\u00ad'
        def walk(o, depth=0):
            nonlocal n
            if depth > 64 or not isinstance(o, (dict, list)):
                return
            if isinstance(o, dict):
                for k in list(o.keys()):
                    v = o[k]
                    if isinstance(v, str):
                        stripped = v.strip(STRIP)
                        dst = translations.get(stripped)
                        if not (isinstance(dst, str) and dst and dst != v):
                            norm = _norm_rpgmv_ctrl(stripped)
                            if norm != stripped:
                                dst = translations.get(norm)
                        if isinstance(dst, str) and dst and dst != v:
                            o[k] = dst
                            n += 1
                    else:
                        walk(v, depth + 1)
            else:
                for i in range(len(o)):
                    v = o[i]
                    if isinstance(v, str):
                        stripped = v.strip(STRIP)
                        dst = translations.get(stripped)
                        if not (isinstance(dst, str) and dst and dst != v):
                            norm = _norm_rpgmv_ctrl(stripped)
                            if norm != stripped:
                                dst = translations.get(norm)
                        if isinstance(dst, str) and dst and dst != v:
                            o[i] = dst
                            n += 1
                    else:
                        walk(v, depth + 1)
        walk(obj)
        return n

    def _replace_in_value(self, value, translations, changed, path="", depth=0):
        if depth>64: return value  # 自引用/超深结构防护
        if isinstance(value, str):
            if value in translations and isinstance(translations[value], str) and translations[value] and translations[value] != value:
                changed.append((path,value,translations[value])); return translations[value]
            return value
        if isinstance(value, dict):
            for k in list(value.keys()):
                value[k]=self._replace_in_value(value[k],translations,changed,f"{path}.{k}" if path else str(k),depth+1)
            return value
        if isinstance(value, list):
            for i in range(len(value)):
                value[i]=self._replace_in_value(value[i],translations,changed,f"{path}[{i}]",depth+1)
            return value
        return value

    @staticmethod
    def _set_path(value, path, replacement):
        """按 parse_as_dict() 返回的字段路径精准修改字符串；失败返回False。"""
        if not path:
            return False
        parts=[]
        for m in re.finditer(r'([^\.\[\]]+)|\[(\d+)\]', path):
            parts.append(m.group(1) if m.group(1) is not None else int(m.group(2)))
        cur=value
        try:
            for part in parts[:-1]:
                if isinstance(part, int):
                    cur=cur[part]
                else:
                    cur=cur[part]
            last=parts[-1]
            if isinstance(cur, dict) and isinstance(last, str):
                if isinstance(cur.get(last), str):
                    cur[last]=replacement; return True
            elif isinstance(cur, list) and isinstance(last, int):
                if isinstance(cur[last], str):
                    cur[last]=replacement; return True
        except Exception:
            return False
        return False

    def inject(self, root, translations, out_dir, progress=None, locations=None):
        if not self.available:
            raise RuntimeError("UnityPy 未安装：" + (self.error or "unknown error"))
        os.makedirs(out_dir, exist_ok=True)
        errors=[]; changed_total=0; files=self._candidate_files(root)
        # 兼容旧调用：定位元数据缺失时尝试从翻译表自带的 _mtool_meta 恢复
        if not locations:
            meta = translations.get('_mtool_meta')
            if isinstance(meta, dict) and isinstance(meta.get('locations'), dict):
                locations = meta['locations']
        # 保存为副本，绝不覆盖源游戏。
        copy_list=[]
        for dp,dirs,fs in os.walk(root):
            dirs[:] = [d for d in dirs if d.lower() not in {"_mtool_output", "_data_backup", "_font_backup", "__pycache__", ".git"}]
            rel=os.path.relpath(dp,root)
            if rel=='.': rel=''
            for fn in fs:
                copy_list.append((os.path.join(dp,fn), os.path.join(out_dir,rel,fn)))
        for k,(src,dst) in enumerate(copy_list,1):
            os.makedirs(os.path.dirname(dst),exist_ok=True)
            try: shutil.copy2(src,dst)
            except Exception as e: errors.append(f"{os.path.relpath(src,root)}:复制失败 {e}")
            if progress and k % 400 == 0:
                progress(0, 1, f"复制游戏文件 {k}/{len(copy_list)}", 0)

        # Managed 程序集 #US 字符串回填：游戏内嵌对白（代码字符串常量）的原位汉化。
        # 此时输出目录已是完整副本，直接在副本上等长覆写，不影响程序集布局。
        dll_replaced = dll_skipped = 0
        dll_skipped_samples = []
        try:
            for fp in self._managed_assemblies(out_dir):
                r = _patch_dotnet_us_heap(fp, translations)
                dll_replaced += r.get("replaced", 0)
                dll_skipped += r.get("skipped", 0)
                for s in r.get("skipped_samples", []):
                    if s not in dll_skipped_samples:
                        dll_skipped_samples.append(s)
                if r.get("error"):
                    errors.append(f"{os.path.basename(fp)}: #US 回填失败 {r['error']}")
        except Exception as e:
            errors.append(f"Managed DLL 字符串回填失败: {type(e).__name__}: {e}")

        # 优先使用提取阶段记录的 file/path_id/type/field 定位，避免“同一句话”在
        # 资源名、内部字段、脚本字段中被全局误替换。没有定位元数据时才允许
        # 在已知文本对象内做有限的 source→translation fallback。
        by_file={}
        for src, locs in (locations or {}).items():
            if not isinstance(locs, list): continue
            for loc in locs:
                if not isinstance(loc, dict) or not loc.get('file'): continue
                by_file.setdefault(loc['file'], []).append((src, loc))

        # 预热 typetree 生成器
        managed=self._managed_dir(root)
        if managed:
            self.ttgen._ensure_generator(managed)
        for idx,fp in enumerate(files,1):
            rel=os.path.relpath(fp,root).replace('\\','/')
            targeted = by_file.get(rel, [])
            if not targeted and by_file:
                # 游戏根目录选了上层文件夹时，rel 会带游戏子文件夹前缀；
                # 对定位键做后缀匹配，避免精确匹配全部脱靶导致 0 写回
                for k, v in by_file.items():
                    if rel.endswith('/' + k) or k.endswith('/' + rel):
                        targeted = v
                        break
            used_targeted = bool(targeted)
            if (locations or by_file) and not targeted:
                # 定位元数据存在而本文件没有目标对象：定位模式只改已记录的对象，
                # 无需对整文件做 parse_as_dict 全量解析（大游戏的主要卡顿来源）。
                if progress: progress(idx,max(1,len(files)),rel+" · 无定位目标，跳过",0)
                continue
            try:
                env=self.UnityPy.load(fp)
                local=0; scanned=0
                targets_by_pid={}
                for src,loc in targeted:
                    try:
                        pid=int(loc.get('path_id',0)); targets_by_pid.setdefault(pid,[]).append((src,loc))
                    except Exception: pass

                for obj in env.objects:
                    if not self._should_parse_obj(obj):
                        continue
                    pid=int(getattr(obj,'path_id',0) or 0)
                    if used_targeted and pid not in targets_by_pid:
                        continue  # 定位模式：只解析目标 path_id 对象
                    scanned+=1
                    if progress and scanned % 100 == 0:
                        progress(idx,max(1,len(files)),f"{rel} · 解析对象 {scanned}",local)
                    try:
                        tree=obj.parse_as_dict()
                    except Exception:
                        tree=None
                    if tree is None:
                        # 与提取阶段一致：用 Managed DLL 生成的 typetree 解析后补丁
                        nodes=self.ttgen.nodes_for_object(obj)
                        if not nodes:
                            continue
                        try:
                            tree=obj.read_typetree(nodes)
                        except Exception:
                            continue
                    else:
                        nodes=None
                    changed=[]

                    if used_targeted:
                        for src,loc in targets_by_pid[pid]:
                            dst=translations.get(src)
                            if not isinstance(dst,str) or not dst or dst==src:
                                continue
                            field=str(loc.get('field') or '')
                            # 字段白名单：只写显示文本字段（UGUI Text 的 m_Text、TMP 的 m_text）。
                            # 其余一律拒写——m_Name 是按名引用的逻辑标识符；动画触发器
                            # （m_NormalTrigger 等）、按钮回调方法名（m_MethodName）、
                            # 反射类型名（*AssemblyTypeName）、动作ID（m_ActionId）都是
                            # 逻辑字段，写入翻译会导致按钮点击失效（反射找不到方法）、
                            # 动画状态机失配 → 游戏无法操作。
                            leaf=field.rsplit('.',1)[-1].split('[')[0].strip() if field else ''
                            if leaf.lower() not in ('m_text','text'):
                                continue
                            if field and self._set_path(tree, field, dst):
                                changed.append((field,src,dst))
                    else:
                        # 旧版/外部翻译表没有 _mtool_meta：仅限 TextAsset 或显式
                        # Localization/StringTable 对象做 fallback，不碰普通 MonoBehaviour。
                        typ=self._obj_type_name(obj)
                        if typ == 'TextAsset' or 'Localization' in typ or 'StringTable' in typ:
                            newtree=self._replace_in_value(tree,translations,changed)
                        else:
                            newtree=tree

                    if changed:
                        obj.patch(tree, nodes=nodes)
                        local += len(changed)
                if local:
                    dst=os.path.join(out_dir,rel); os.makedirs(os.path.dirname(dst),exist_ok=True)
                    raw=env.file.save()
                    with open(dst,'wb') as f: f.write(raw)
                    changed_total += local
                if progress:
                    progress(idx,max(1,len(files)),rel,local)
            except Exception as e:
                errors.append(f"{rel}: UnityPy写回失败 {type(e).__name__}: {e}")
        # 无定位对白原位覆写：结构化解析失败（缺 typetree）被二进制补扫捞出的串没有
        # file/path_id/field 定位，对象通道够不着；这里按 int32 长度前缀精确锚定做等长
        # 覆写。跳过纯资源流(.resS/.resource)与超大文件（与提取侧 _scan_unity_serialized 同界）。
        blob_replaced = blob_skipped = 0
        blob_skipped_samples = []
        try:
            for fp in files:
                rel = os.path.relpath(fp, root).replace('\\', '/')
                low = os.path.basename(fp).lower()
                if low.endswith(('.ress', '.resource')):
                    continue
                try:
                    if os.path.getsize(fp) > 128 * 1024 * 1024:
                        continue
                except OSError:
                    continue
                dst = os.path.join(out_dir, rel)
                if not os.path.isfile(dst):
                    continue
                r = _patch_serialized_text_blobs(dst, translations)
                blob_replaced += r.get("replaced", 0)
                blob_skipped += r.get("skipped", 0)
                for s in r.get("skipped_samples", []):
                    if s not in blob_skipped_samples:
                        blob_skipped_samples.append(s)
                if r.get("error"):
                    errors.append(f"{rel}: 资源串原位覆写失败 {r['error']}")
        except Exception as e:
            errors.append(f"资源串原位覆写失败: {type(e).__name__}: {e}")
        changed_total += blob_replaced
        # 散落文本文件回写（与提取侧 _extract_text_files 对应）：json结构化/整行精确替换
        text_replaced = 0
        try:
            text_replaced = self._inject_text_files(out_dir, translations, errors)
        except Exception as e:
            errors.append(f"散落文本回写失败: {type(e).__name__}: {e}")
        return {"files":len(files),"replaced":changed_total,"errors":errors,"out_dir":out_dir,"backend":"UnityPy",
                "dll_replaced":dll_replaced,"dll_skipped":dll_skipped,"dll_skipped_samples":dll_skipped_samples[:30],
                "blob_replaced":blob_replaced,"blob_skipped":blob_skipped,
                "blob_skipped_samples":blob_skipped_samples[:30],
                "text_replaced":text_replaced}


class AssetRipperBackend:
    """AssetRipper 兜底：把 Unity 游戏导出成 Unity Project，再从 YAML/TextAsset 等提取可翻译文本。
    注意：这是兜底提取/分析后端；AssetRipper 本身主要负责导出 Unity Project，不在这里假装可以无条件原样重建所有 Bundle。
    """
    EXE_NAMES=("AssetRipper.exe","AssetRipper.GUI.Free.exe","AssetRipper.GUI.Free","AssetRipper.CLI.exe","AssetRipperCLI.exe","AssetRipper")

    def __init__(self):
        self.config_file=os.path.join(os.path.dirname(os.path.abspath(__file__)), ".assetripper_path.json")
        self.path=self._load_saved_path() or self.find_exe()

    def _load_saved_path(self):
        try:
            if os.path.isfile(self.config_file):
                with open(self.config_file, "r", encoding="utf-8") as f:
                    p=json.load(f).get("path", "")
                if p and os.path.isfile(p):
                    return p
        except Exception:
            pass
        return None

    def set_path(self, path):
        self.path=os.path.abspath(path) if path else None
        try:
            with open(self.config_file, "w", encoding="utf-8") as f:
                json.dump({"path": self.path or ""}, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    @staticmethod
    def find_exe():
        here=os.path.dirname(os.path.abspath(__file__))
        candidates=[]
        for base in (here, os.path.join(here,"tools"), os.path.join(here,"_AssetRipper"), os.path.join(here,"_tools")):
            for n in AssetRipperBackend.EXE_NAMES:
                candidates.append(os.path.join(base,n))
        which=subprocess.run(["where","AssetRipper.exe"],capture_output=True,text=True,encoding="utf-8",errors="replace") if os.name=='nt' else None
        if which and which.returncode==0:
            candidates.extend([x.strip() for x in which.stdout.splitlines() if x.strip()])
        for p in candidates:
            if os.path.isfile(p): return p
        return None

    def available(self): return bool(self.path and os.path.exists(self.path))

    def export_project(self, root, out_dir, timeout=1800):
        if not self.available(): raise RuntimeError("未找到 AssetRipper 可执行文件")
        os.makedirs(out_dir,exist_ok=True)
        # 不同 AssetRipper/CLI 构建的参数略有差异，按兼容性从强到弱尝试。
        commands = [
            [self.path, "--cli", "-i", root, "-o", out_dir, "--mode", "Unity", "--ignore-streaming-assets", "false"],
            [self.path, "--cli", "-i", root, "-o", out_dir, "--mode", "Unity"],
            [self.path, "-i", root, "-o", out_dir, "--mode", "Unity"],
            [self.path, "-i", root, "-o", out_dir],
        ]
        last = ""
        for cmd in commands:
            try:
                cp=subprocess.run(cmd,capture_output=True,text=True,encoding="utf-8",errors="replace",timeout=timeout)
            except Exception as e:
                last=str(e); continue
            if cp.returncode==0:
                return out_dir
            last=(cp.stderr or cp.stdout or "")[-2000:]
        raise RuntimeError(last or "AssetRipper 导出失败")

    def extract(self, root, filter_enabled=True, filter_mode="标准", progress=None):
        work=tempfile.mkdtemp(prefix="mtool_assetripper_")
        try:
            export_dir=self.export_project(root,work)
            result={}; errors=[]
            # 优先 Unity 项目文本/YAML；使用 UnityDeepExtractor 的文本层过滤。
            stat=UnityDeepExtractor.scan(export_dir,result,min_len=2,filter_enabled=filter_enabled,filter_mode=filter_mode,progress=progress,deep=True)
            # AssetRipper 会输出很多代码/元数据，过滤器负责挡掉大部分；保留来源信息。
            result["_mtool_meta"]={"engine":"unity","backend":"AssetRipper","export_dir":"<temporary>","note":"兜底后端：从 AssetRipper 导出的 Unity Project 中提取文本"}
            errors.extend(stat.get("errors",[]))
            stat["backend"]="AssetRipper"
            stat["errors"]=errors
            return result,stat
        finally:
            shutil.rmtree(work,ignore_errors=True)


class UnityBackendManager:
    """Unity 后端路由：UnityPy → AssetRipper → legacy 字符串扫描。"""
    MODES=("自动", "UnityPy", "AssetRipper", "传统扫描")
    def __init__(self):
        self.unitypy=UnityPyBackend()
        self.assetripper=AssetRipperBackend()

    def status(self):
        try:
            from TypeTreeGeneratorAPI import TypeTreeGenerator  # noqa: F401
            ttgen=True
        except Exception:
            ttgen=False
        return {"UnityPy":self.unitypy.available,"AssetRipper":self.assetripper.available(),"TTGen":ttgen,"UnityPyError":self.unitypy.error,"AssetRipperPath":self.assetripper.path}

    def extract(self, root, backend="自动", filter_enabled=True, filter_mode="标准", progress=None):
        if backend in ("自动","UnityPy") and self.unitypy.available:
            try:
                r,st=self.unitypy.extract(root,progress=progress,filter_enabled=filter_enabled,filter_mode=filter_mode)
                # UnityPy 有结构化结果就直接采用；只在确实没有对象时继续 fallback。
                if st.get("items",0)>0 or backend=="UnityPy":
                    return r,st
            except Exception as e:
                if backend=="UnityPy": raise
        if backend in ("自动","AssetRipper") and self.assetripper.available():
            try:
                return self.assetripper.extract(root,filter_enabled=filter_enabled,filter_mode=filter_mode,progress=progress)
            except Exception:
                if backend=="AssetRipper": raise
        # legacy 最后兜底：避免因缺少可选后端导致整个插件不可用。
        result={}; st=UnityDeepExtractor.scan(root,result,min_len=2,filter_enabled=filter_enabled,filter_mode=filter_mode,progress=progress,deep=True)
        st["backend"]="传统扫描"
        if backend == "自动":
            st.setdefault("warnings", []).append("UnityPy/AssetRipper 均不可用，已退回传统扫描；安装后可再次执行深度提取。")
        result["_mtool_meta"]={"engine":"unity","backend":"传统扫描"}
        return result,st

    def inject(self, root, translations, out_dir, backend="自动", progress=None, locations=None):
        # UnityPy 才负责结构化直接写回；AssetRipper 明确不冒充“原包重建器”。
        if backend in ("自动","UnityPy") and self.unitypy.available:
            return self.unitypy.inject(root,translations,out_dir,progress=progress,locations=locations)
        if backend=="AssetRipper":
            raise RuntimeError("AssetRipper 后端在本插件中用于兜底提取/分析；原始 AssetBundle 的精准重建需要专用回包工具，不会强行覆盖原包。")
        # 传统扫描不能安全写回 Unity 二进制
        raise RuntimeError("当前 Unity 后端没有安全写回能力；请使用 UnityPy，并确保 UnityPy 可用。")


class TextQualityFilter:
    """翻译前文本质量过滤器：严格/标准/激进三档；目标是挡掉二进制垃圾、资源名和明显代码，避免白耗翻译 API。"""
    MODES = ("严格", "标准", "激进")

    @classmethod
    def score(cls, text):
        if not isinstance(text, str):
            return 0, ["非字符串"]
        t = text.strip()
        if not t:
            return 0, ["空文本"]
        reasons=[]; score=100
        cjk = len(re.findall(r'[\u4e00-\u9fff]', t))
        kana = len(re.findall(r'[\u3040-\u30ff]', t))
        hangul = len(re.findall(r'[\uac00-\ud7af]', t))
        letters = len(re.findall(r'[A-Za-z]', t))
        digits = len(re.findall(r'\d', t))
        bad = len(re.findall(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', t))
        punct = len(re.findall(r'[^\w\s\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af\u00c0-\u024f\u3040-\u30ff]', t, re.UNICODE))
        n=max(len(t),1)
        if bad:
            return 0,["控制字符"]
        if re.match(r'^(?:https?://|ftp://|www\.)', t, re.I) or re.match(r'^[A-Za-z]:[\\/]', t):
            return 2,["路径/URL"]
        if re.search(r'^(?:Assets|Packages|StreamingAssets|Resources)[\\/]', t, re.I):
            return 3,["Unity资源路径"]
        if re.fullmatch(r'[0-9a-fA-F]{8,64}', t):
            return 4,["Hash"]
        if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_./:-]{1,80}', t) and ('_' in t or ':' in t or '/' in t or digits>0):
            score -= 55; reasons.append("疑似变量/资源名")
        if re.search(r'(?:function|const|let|var|return|class|using|namespace|public|private|protected)\s+', t):
            score -= 70; reasons.append("疑似代码")
        if re.search(r'[{}\[\]();]{3,}', t) and cjk+kana+hangul == 0:
            score -= 55; reasons.append("代码符号密集")
        ascii_words = re.findall(r"[A-Za-z]{2,}", t)
        if len(t) <= 20 and ascii_words and len(ascii_words) >= 2 and " " in t:
            # 正常英文短句：Welcome back! / Hello, world! 不因标点被误杀。
            pass
        elif len(t) <= 20 and (cjk+kana+hangul) <= 2 and punct >= max(2, len(t)//5) and kana == 0:
            score -= 45; reasons.append("短字符串符号异常")
        if letters+digits > 0 and (letters+digits)/n < 0.35 and cjk+kana+hangul == 0:
            score -= 30; reasons.append("英文/数字占比过低")
        if len(t) >= 8 and cjk+kana+hangul == 0:
            maxrun=max((len(x) for x in re.findall(r'(.)\1+', t)), default=1)
            if maxrun/n > 0.55:
                score -= 40; reasons.append("重复字符异常")
        if '\ufffd' in t or '�' in t:
            score -= 70; reasons.append("解码替换字符")
        # 混合脚本异常：少量 CJK + 大量随机 ASCII/符号，比正常对白更可疑。
        # 含假名的文本几乎一定是日文原文（常带<color>/<size>标签），豁免符号类惩罚。
        if (cjk+kana+hangul) and len(t) <= 24 and punct >= 1 and (digits > 0 or letters > 0) and kana == 0:
            if (digits + punct) >= 2 and len(t) <= 12 and (cjk+kana+hangul) <= 2:
                score -= 60; reasons.append("CJK与数字/符号混合异常")
            elif (cjk+kana+hangul) <= 2 and len(t) <= 6 and letters <= 2:
                score -= 30; reasons.append("短文本脚本混杂")
            elif letters > (cjk+kana+hangul)*6 and punct >= 2:
                score -= 35; reasons.append("脚本混合异常")
        # 单词过长通常像二进制可打印段；自然语言长 token 很少连续到 24+。
        if re.search(r'[A-Za-z0-9]{24,}', t):
            score -= 35; reasons.append("超长连续字母数字")
        return max(0,min(100,score)), reasons

    @classmethod
    def accept(cls, text, mode):
        score, reasons = cls.score(text)
        if mode == "严格":
            threshold=78
        elif mode == "激进":
            threshold=48
        else:
            threshold=62
        # 明确垃圾，无论档位都不放行；激进只放宽普通可疑项。
        hard = {"空文本","控制字符","路径/URL","Unity资源路径","Hash"}
        if any(r in hard for r in reasons):
            return False, score, reasons
        return score >= threshold, score, reasons

# ==================== Unity 深度提取器 ====================
class UnityDeepExtractor:
    """尽可能完整的 Unity 文本发现器。
    设计原则：专用格式优先、二进制多编码兜底、分块读取避免大文件占满内存。
    不承诺能解析所有自定义/加密 AssetBundle；但会覆盖常见可直接发现的文本载体。
    """
    TEXT_EXTS = {
        '.txt','.json','.csv','.tsv','.xml','.yaml','.yml','.ini','.cfg','.conf',
        '.po','.lang','.loc','.locale','.bytes','.asset','.lua','.js','.mjs','.ts',
        '.text','.template','.html','.htm','.md','.properties'
    }
    BINARY_EXTS = {'.assets','.ress','.resource','.bundle','.unity3d','.sharedassets','.data','.dat','.level','.bin'}
    SKIP_DIRS = {'_mtool_output','_data_backup','_font_backup','__pycache__','.git','logs','crash','crashes','save','saves','savedata'}
    SKIP_FILES = {'output_log.txt','player.log','player-prev.log','error.log','boot.config','global-metadata.dat'}

    @classmethod
    def scan(cls, root, result, min_len=2, filter_enabled=True, filter_mode='标准', progress=None, deep=True):
        files = cls._collect_files(root, deep=deep)
        stat={'files':0,'items':0,'filtered':0,'filter_reasons':{},'errors':[],'passes':{}}
        for idx, fp in enumerate(files,1):
            rel=os.path.relpath(fp,root).replace('\\','/')
            if progress:
                try: progress(idx,len(files),rel,0)
                except Exception: pass
            stat['files'] += 1
            before=len(result)
            try:
                ext=os.path.splitext(fp)[1].lower()
                if ext in cls.TEXT_EXTS or cls._looks_like_text_file(fp):
                    cls._scan_text_file(fp,result,min_len,filter_enabled,filter_mode,stat)
                    stat['passes']['text']=stat['passes'].get('text',0)+1
                if ext in cls.BINARY_EXTS or cls._looks_like_unity_binary(fp):
                    cls._scan_binary_file(fp,result,min_len,filter_enabled,filter_mode,stat)
                    stat['passes']['binary']=stat['passes'].get('binary',0)+1
                # 对无扩展名的 *_Data 核心文件再做一次二进制探测
                if not ext and ('_data' in rel.lower() or os.path.basename(fp).lower() in ('globalgamemanagers','main data')):
                    cls._scan_binary_file(fp,result,min_len,filter_enabled,filter_mode,stat)
                    stat['passes']['core']=stat['passes'].get('core',0)+1
            except Exception as e:
                stat['errors'].append(f'{rel}: {type(e).__name__}: {e}')
            if progress:
                try: progress(idx,len(files),rel,len(result)-before)
                except Exception: pass
        stat['items']=len(result)
        return stat

    @classmethod
    def _collect_files(cls, root, deep=True):
        out=[]
        for dp, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d.lower() not in cls.SKIP_DIRS and not d.startswith('.')]
            for fn in files:
                low=fn.lower()
                if low in cls.SKIP_FILES: continue
                fp=os.path.join(dp,fn)
                try:
                    size=os.path.getsize(fp)
                except OSError:
                    continue
                ext=os.path.splitext(low)[1]
                # 通用名 .bin 纳入二进制扫描，但限制体积——大 .bin 多为整包资源，
                # 三通道扫描耗时且收益低；16MB 内的才值得探测嵌入字符串。
                if ext == '.bin' and size > 16*1024*1024:
                    continue
                interesting=(ext in cls.TEXT_EXTS or ext in cls.BINARY_EXTS or
                             low.endswith('_data') or low in ('globalgamemanagers','main data'))
                if interesting:
                    # 大文本/资源分块扫描也能处理，默认允许到 512 MiB；超大文件仍跳过以防误扫整盘镜像。
                    if size <= 512*1024*1024:
                        out.append(fp)
                elif deep and size <= 16*1024*1024 and cls._name_suggests_localization(low):
                    out.append(fp)
        # 小文件优先，便于快速看到真实结果；核心二进制放后面
        out.sort(key=lambda x:(os.path.getsize(x) if os.path.exists(x) else 0, x.lower()))
        return out

    @staticmethod
    def _name_suggests_localization(name):
        keys=('local','locale','lang','language','string','dialog','dialogue','text','message','subtitle','translation','i18n','l10n','story','scenario','script','csv','json')
        return any(k in name for k in keys)

    @classmethod
    def _looks_like_text_file(cls, fp):
        try:
            with open(fp,'rb') as f: head=f.read(8192)
            if not head: return False
            if b'\x00' in head and not (head.startswith(b'\xff\xfe') or head.startswith(b'\xfe\xff')):
                return False
            # UTF-8/UTF-16/常见单字节文本粗判
            for enc in ('utf-8','utf-16','cp932','shift_jis','gbk','big5'):
                try:
                    t=head.decode(enc)
                    if cls._textish(t): return True
                except Exception: pass
            return False
        except Exception:
            return False

    @staticmethod
    def _textish(t):
        if not t: return False
        # 零宽/格式字符不计入可打印率：一个U+200B不该让整份脚本文件被判为二进制
        t = t.replace('\u200b','').replace('\u200c','').replace('\u200d','') \
             .replace('\u2060','').replace('\u00ad','')
        if not t: return False
        printable=sum(1 for c in t if c.isprintable() or c in '\r\n\t')
        return printable/max(1,len(t)) >= 0.92

    @staticmethod
    def _looks_like_unity_binary(fp):
        low=os.path.basename(fp).lower()
        path=fp.lower().replace('\\','/')
        return (low.startswith(('sharedassets','level','globalgamemanagers')) or
                '/resources.assets' in path or '/sharedassets' in path or
                low.endswith(('.assets','.ress','.resource','.bundle','.unity3d')))

    @staticmethod
    def _has_kana(text):
        """是否含日文假名（平/片假名、半角片假名）。ASCII 路径、编号，以及把 ASCII
        误读成 UTF-16 的 CJK 区乱码（敳潵牣…）都不含假名，靠它挡掉——真实日文对白
        几乎必有假名（助词/送假名/语尾），是强信号。"""
        return any(('぀'<=c<='ヿ') or ('ｦ'<=c<='ﾟ') or c in '々〆゛゜ゝゞー'
                   for c in text)

    @classmethod
    def _add(cls, text, result, min_len, filter_enabled, filter_mode, stat, require_jp=False):
        if not isinstance(text,str): return
        # 剥离零宽字符/软连字符等格式字符：这类字符常用于隐形文本，且会干扰
        # 可打印率判断与译文精确匹配；剥离后再进翻译表。
        text=text.strip('﻿\x00\r\n\t ​‌‍⁠­')
        if len(text)<min_len: return
        if require_jp and not cls._has_kana(text):
            return  # 只收日文(含假名)——结构化补扫通道里 ASCII 路径/UTF-16乱码非翻译目标
        ok=True; reasons=[]
        if filter_enabled:
            ok,_,reasons=TextQualityFilter.accept(text,filter_mode)
        if not ok:
            stat['filtered'] += 1
            for r in (reasons or ['低可信度']): stat['filter_reasons'][r]=stat['filter_reasons'].get(r,0)+1
            return
        result.setdefault(text,'')

    @classmethod
    def _scan_text_file(cls, fp, result, min_len, filter_enabled, filter_mode, stat):
        raw=None
        try:
            with open(fp,'rb') as f: raw=f.read()
        except Exception as e:
            stat['errors'].append(f'{os.path.basename(fp)}: {e}'); return
        decs=[]
        # 编码探测顺序：UTF-16 必须"有 BOM 或 NUL 密度达标"才尝试——
        # 否则偶数长度的 SJIS/GBK 文件会被 UTF-16 解码成"可打印假名乱码"并抢先命中。
        has_bom = raw[:2] in (b'\xff\xfe', b'\xfe\xff')
        nul_density = (raw.count(0) / len(raw)) if raw else 0
        try_utf16 = has_bom or (len(raw) % 2 == 0 and raw.count(0) >= max(4, len(raw) // 8))
        enc_order = ['utf-8-sig', 'utf-8']
        if has_bom:
            enc_order.insert(0, 'utf-16')
        elif try_utf16:
            enc_order += ['utf-16-le', 'utf-16-be']
        enc_order += ['cp932', 'shift_jis', 'gbk', 'big5']
        for enc in enc_order:
            try:
                t=raw.decode(enc)
                if UnityDeepExtractor._textish(t):
                    decs.append(t); break
            except Exception: pass
        if not decs: return
        text=decs[0]
        ext=os.path.splitext(fp)[1].lower()
        if ext=='.json':
            try:
                obj=json.loads(text)
                cls._walk_json(obj,result,min_len,filter_enabled,filter_mode,stat)
                return
            except Exception: pass
        if ext in ('.csv','.tsv'):
            delim='\t' if ext=='.tsv' else ','
            for line in text.splitlines():
                try:
                    import csv as _csv
                    for cell in next(_csv.reader([line],delimiter=delim)):
                        cls._add(cell,result,min_len,filter_enabled,filter_mode,stat)
                except Exception:
                    for cell in line.split(delim): cls._add(cell,result,min_len,filter_enabled,filter_mode,stat)
            return
        for line in text.splitlines():
            # YAML/INI/脚本：优先提取引号中的值，同时保留整行中的自然语言
            for m in re.finditer(r'(?<!\\)["\']([^"\'\n\r]{%d,})["\']' % max(2,min_len), line):
                cls._add(m.group(1),result,min_len,filter_enabled,filter_mode,stat)
            cls._add(line.strip(),result,min_len,filter_enabled,filter_mode,stat)

    @classmethod
    def _walk_json(cls,obj,result,min_len,filter_enabled,filter_mode,stat,depth=0):
        if depth>20:return
        if isinstance(obj,str): cls._add(obj,result,min_len,filter_enabled,filter_mode,stat)
        elif isinstance(obj,list):
            for x in obj: cls._walk_json(x,result,min_len,filter_enabled,filter_mode,stat,depth+1)
        elif isinstance(obj,dict):
            for k,v in obj.items():
                # 键名默认不翻，避免资源字段污染；但本地化字段名可作为弱候选
                if isinstance(v,str):
                    cls._add(v,result,min_len,filter_enabled,filter_mode,stat)
                else:
                    cls._walk_json(v,result,min_len,filter_enabled,filter_mode,stat,depth+1)

    @classmethod
    def _scan_binary_file(cls, fp, result, min_len, filter_enabled, filter_mode, stat,
                          channels=('prefixed','runs','utf16'), require_jp=False):
        # 多通道：Unity 长度前缀 UTF-8/UTF-16 + 原始字符串窗口。
        # channels 可裁剪通道——结构化 .assets 上 utf16/runs 通道几乎全是把 ASCII
        # 误读成 UTF-16 的乱码（"Vertical"→"嘀牥楴慣l"），只留 prefixed 即可保住真对白。
        # require_jp：只收日文(含假名)——结构化补扫的目标就是捞日文对白，ASCII 路径、
        # 着色器名等非翻译目标不进来（也顺带挡掉 CJK 区误读乱码）。
        try:
            size=os.path.getsize(fp)
            with open(fp,'rb') as f:
                offset=0; overlap=b''; chunk_size=4*1024*1024
                while True:
                    buf=f.read(chunk_size)
                    if not buf: break
                    data=overlap+buf
                    base=offset-len(overlap)
                    if 'prefixed' in channels: cls._scan_prefixed(data,result,min_len,filter_enabled,filter_mode,stat,require_jp)
                    if 'runs' in channels: cls._scan_runs(data,result,min_len,filter_enabled,filter_mode,stat)
                    if 'utf16' in channels: cls._scan_utf16_runs(data,result,min_len,filter_enabled,filter_mode,stat)
                    overlap=data[-512:]
                    offset += len(buf)
                    if size>0 and offset>size: break
        except Exception as e:
            stat['errors'].append(f'{os.path.basename(fp)}: {e}')

    @classmethod
    def _scan_prefixed(cls,data,result,min_len,filter_enabled,filter_mode,stat,require_jp=False):
        n=len(data); i=0
        while i+4<=n:
            ln=int.from_bytes(data[i:i+4],'little')
            if 1<=ln<=8192 and i+4+ln<=n:
                b=data[i+4:i+4+ln]
                for enc in ('utf-8','utf-16-le','cp932'):
                    try:
                        s=b.decode(enc)
                        if len(s)>=min_len and cls._textish(s):
                            cls._add(s,result,min_len,filter_enabled,filter_mode,stat,require_jp); break
                    except Exception: pass
            i+=1

    @classmethod
    def _run_text_ok(cls, s):
        """原始二进制run通道的附加校验：随机高字节经cp932解码出的
        '可打印假名/汉字乱码'必须挡掉，否则扫描纯随机二进制也会产出上万条
        垃圾条目逐条送翻译API。规则：纯ASCII放行；含非ASCII时要求
        假名计数>=2且占比>=12%（且长度>=6）或 CJK计数>=4且占比>=50%——
        绝对计数门槛挡掉短随机 run 的比例波动。"""
        if s.isascii(): return True
        n=max(len(s),1)
        kana=sum(1 for c in s if '\u3040'<=c<='\u30ff')
        if kana>=2 and kana/n>=0.12 and n>=6: return True
        cjk=sum(1 for c in s if '\u4e00'<=c<='\u9fff')
        return cjk>=4 and cjk/n>=0.5

    @classmethod
    def _scan_runs(cls,data,result,min_len,filter_enabled,filter_mode,stat):
        starts=[]; cur=bytearray()
        for b in data:
            if 0x20<=b<=0x7e or b>=0xC0 or b in (9,10,13):
                cur.append(b)
            else:
                # 原始二进制字符串扫描比长度前缀/文本文件更容易误收垃圾；ASCII/混合裸串提高最短长度。
                if len(cur)>=max(12,min_len*2):
                    try:
                        for enc in ('utf-8','cp932'):
                            try:
                                s=bytes(cur).decode(enc).strip()
                                if len(s)>=min_len and cls._textish(s) and cls._run_text_ok(s):
                                    cls._add(s,result,min_len,filter_enabled,filter_mode,stat); break
                            except Exception: pass
                    finally: cur.clear()
                else: cur.clear()
        if len(cur)>=max(12,min_len*2):
            for enc in ('utf-8','cp932'):
                try:
                    s=bytes(cur).decode(enc).strip()
                    if len(s)>=min_len and cls._textish(s) and cls._run_text_ok(s): cls._add(s,result,min_len,filter_enabled,filter_mode,stat); break
                except Exception: pass

    @classmethod
    def _scan_utf16_runs(cls,data,result,min_len,filter_enabled,filter_mode,stat):
        # UTF-16LE/BE 连续文本 run：按 16位码元值判可打印性（旧"单字节为0"判据
        # 对日文/中文全漏——CJK 码位>0x3000，LE 两字节均非零）。
        # 歧义仲裁：同一字节区域会被 2端序×2相位 读出多种解码，按"文本质量分"
        # （假名/ASCII/空白加权 + CJK）贪心保留最优解码，错位变体不进翻译表。
        min_bytes=max(2,min_len)*2
        n=len(data)
        def _ok(c):
            return (c in (9,10,13)) or (0x20<=c<=0x7E) or (0xA0<=c<=0xD7A3) or (0xFF61<=c<=0xFF9F)
        def _q(s):
            L=max(len(s),1)
            kana=sum(1 for c in s if '\u3040'<=c<='\u30ff')
            cjk=sum(1 for c in s if '\u4e00'<=c<='\u9fff')
            asc=sum(1 for c in s if c.isascii() and (c.isalnum() or c==' '))
            return (2.0*kana + 1.5*asc + 1.2*cjk)/L
        candidates=[]  # (start, end, q, combo, s, anchored)
        combos = (('utf-16-le',0),('utf-16-le',1),('utf-16-be',0),('utf-16-be',1))
        for ci,(endian,phase) in enumerate(combos):
            le = endian=='utf-16-le'
            i=phase
            while i+1<n:
                c = data[i] | (data[i+1]<<8) if le else (data[i+1] | (data[i]<<8))
                if _ok(c):
                    j=i; cur=bytearray()
                    kana_pairs=0; ascii_pairs=0; total_pairs=0
                    while j+1<n:
                        c2 = data[j] | (data[j+1]<<8) if le else (data[j+1] | (data[j]<<8))
                        if not _ok(c2): break
                        cur += data[j:j+2]; j+=2
                        total_pairs+=1
                        if 0x3040<=c2<=0x30FF: kana_pairs+=1
                        elif 0x20<=c2<=0x7E: ascii_pairs+=1
                    if len(cur)>=min_bytes:
                        # 解码前快速预过滤：随机二进制会产生海量 CJK 垃圾 run，
                        # 只值得解码的是 锚定 / 含假名 / 纯ASCII 三类
                        start_ok = (i-2 >= 0 and data[i-2:i]==b'\x00\x00') or i==0
                        end_ok = (j+2 > n) or (data[j:j+2]==b'\x00\x00')
                        anchored = start_ok and end_ok
                        if anchored or kana_pairs>=2 or ascii_pairs==total_pairs:
                            try:
                                s=bytes(cur).decode(endian).strip()
                                if len(s)>=min_len and cls._textish(s):
                                    candidates.append((i, j, _q(s), ci, s, anchored))
                            except Exception: pass
                    i=j if j>i else i+2
                else:
                    i+=2
        # 全局组合裁决：错相位/错端序的"移位变体"与真文本内容密度完全相同，
        # 逐条判不死；但真存储（NUL 终止/长度前缀）的双端锚定字节总量必然集中在
        # 正确的 (端序,相位) 组合上——按组合的锚定字节总量选唯一赢家组合。
        anchored_bytes={}
        for i,j,q,ci,s,anchored in candidates:
            if anchored:
                anchored_bytes[ci]=anchored_bytes.get(ci,0)+(j-i)
        if anchored_bytes:
            win=max(anchored_bytes, key=lambda ci:(anchored_bytes[ci], -ci))
            final=[c for c in candidates if c[3]==win and c[5]]
        else:
            final=candidates
        final.sort(key=lambda t:(-t[2], -(t[1]-t[0]), t[3]))
        taken=[]
        for start,end,q,ci,s,anchored in final:
            if any(start<b and end>a for a,b in taken):
                continue
            taken.append((start,end))
            if anchored:
                ok = cls._run_text_ok(s)
            else:
                # 无锚候选加严：没有 NUL 结构背书的纯 CJK UTF-16 run 极罕见
                #（随机高字节恰好落进 CJK 区间即可伪造），要求假名占比达标
                #（真日文文本假名占比普遍 >0.15，随机混入的远低于此）或纯 ASCII。
                kana=sum(1 for c in s if '\u3040'<=c<='\u30ff')
                ok = s.isascii() or (kana>=3 and kana/max(len(s),1)>=0.15)
            if ok:
                cls._add(s,result,min_len,filter_enabled,filter_mode,stat)

class SExtractorHeadless:
    def __init__(self, runtime_dir=RUNTIME_DIR):
        self.runtime_dir = runtime_dir
        self.src_dir = os.path.join(runtime_dir, "src")
        self.engine_ini = os.path.join(self.src_dir, "engine.ini")
        self.engines = _parse_engine_ini(self.engine_ini) if os.path.isfile(self.engine_ini) else {}
        self.unity_backends = UnityBackendManager()

    def reload(self):
        self.engines = _parse_engine_ini(self.engine_ini) if os.path.isfile(self.engine_ini) else {}

    def meta(self, name):
        return self.engines.get("Engine_" + name, {})

    def list_engines(self):
        out = []
        for sec, meta in self.engines.items():
            if sec.startswith("Engine_"):
                name = sec[7:]
                if os.path.isfile(os.path.join(self.src_dir, "extract_" + name + ".py")):
                    out.append((name, str(meta.get("file", "") or ""),
                                str(meta.get("postfix", "") or ""),
                                bool(meta.get("regDic"))))
        return sorted(out, key=lambda x: x[0].lower())

    @staticmethod
    def parse_sample(sample):
        reg, extra = {}, {}
        for raw in (sample or "").splitlines():
            line = raw.strip()
            if not line or line.startswith((";", "#", "/")) or "=" not in line:
                continue
            if line.startswith("<"):
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            v = v.replace("\\\\", "\\")
            if k == "separate":
                extra["contentSeparate"] = v
            elif k == "flag":
                for flag in v.split(","):
                    flag = flag.strip()
                    if flag:
                        extra[flag] = True
            elif "_skip" in k or "_search" in k:
                reg[k] = v
            else:
                if k == "struct":
                    k = "structure"
                extra[k] = v
        return reg, extra

    def configure(self, engine_name, sample_override=None, encode_read=None, encode_write=None):
        if not _ensure_runtime():
            raise RuntimeError("SExtractor 运行时加载失败")
        EXVAR.clearBeforeExtract()
        EXVAR.engineName = engine_name
        meta = self.meta(engine_name)
        EXVAR.fileType = str(meta.get("file", "txt") or "txt")
        EXVAR.Postfix = str(meta.get("postfix", "") or "")
        reg, extra = self.parse_sample(
            sample_override if sample_override is not None else meta.get("sample", "")
        )
        for k, v in extra.items():
            if hasattr(EXVAR, k):
                if isinstance(v, str) and v.lower() in ("true", "false"):
                    v = v.lower() == "true"
                elif isinstance(v, str) and v.isdecimal() and k not in ("extraData", "extractKey"):
                    v = int(v)
                setattr(EXVAR, k, v)
        EXVAR.regDic = reg
        # 决定读取编码：JSON 引擎（RPGMV/MZ、GalGame JSON 等）永远是 UTF-8，
        # 用户在 UI 错选 cp932/shift_jis 会让 _prepare 直接 UnicodeDecodeError。
        # 这里对 JSON fileType 强制优选 utf-8，并把用户的原始选择留作回退链兜底。
        _single_byte_east_asia = {"cp932", "shift_jis", "gbk", "big5", "latin-1"}
        if encode_read:
            if EXVAR.fileType == "json" and encode_read.lower() in _single_byte_east_asia:
                EXVAR._encode_read_fallback = encode_read  # 留给 _prepare() 的回退链
                EXVAR.OldEncodeName = "utf-8"
            else:
                EXVAR._encode_read_fallback = None
                EXVAR.OldEncodeName = encode_read
        elif EXVAR.fileType == "json":
            EXVAR._encode_read_fallback = None
            EXVAR.OldEncodeName = "utf-8"
        else:
            EXVAR._encode_read_fallback = None
            EXVAR.OldEncodeName = "cp932"
        EXVAR.NewEncodeName = encode_write or EXVAR.OldEncodeName
        # 写回编码：JSON 引擎（RPGMV/MZ 等）也必须是 UTF-8，否则中文译文会 UnicodeEncodeError。
        # 用户在 UI 选 cp932/shift_jis/gbk/big5 写 JSON 时强制改用 utf-8（cp932 不含汉字）。
        _write_single_byte_east_asia = {"cp932", "shift_jis", "gbk", "big5", "latin-1"}
        if EXVAR.fileType == "json" and (EXVAR.NewEncodeName or "").lower() in _write_single_byte_east_asia:
            EXVAR.NewEncodeName = "utf-8"
        EXVAR.printSetting = [False, False, False, False, False]
        EXVAR.window = None
        EXVAR.transReplace = False
        EXVAR.preReplace = False
        EXVAR.dynamicReplace = None
        EXVAR.textConf = {}
        EXVAR.nameList = []
        module = self._load_module(engine_name)
        EXVAR.mainParse = module.parseImp
        EXVAR.replaceOnceImp = module.replaceOnceImp
        EXVAR.readFileDataImp = getattr(module, "readFileDataImp", None)
        EXVAR.replaceEndImp = getattr(module, "replaceEndImp", None)
        if EXVAR.fileType == "bin":
            cs = EXVAR.contentSeparate
            if cs is None:
                EXVAR.contentSeparate = b""
            elif isinstance(cs, str):
                try:
                    EXVAR.contentSeparate = cs.encode("latin-1").decode("unicode_escape").encode("latin-1")
                except Exception:
                    EXVAR.contentSeparate = cs.encode("latin-1", "ignore")
        elif EXVAR.contentSeparate is None:
            EXVAR.contentSeparate = ""
        return module

    def _load_module(self, name):
        _ensure_runtime_imports()
        modname = "extract_" + name
        if modname in sys.modules:
            return importlib.reload(sys.modules[modname])
        return importlib.import_module(modname)

    @staticmethod
    def deal_once(text, ctrl):
        if not text:
            return False
        try:
            text = replaceOrig(text, ctrl) if replaceOrig else text
        except Exception:
            pass
        EXVAR.listOrig.append(text)
        return True

    @staticmethod
    def _read_text_fallback(fp, primary, mode="r", **kw):
        """按 primary 编码打开 fp 失败时依次回退到常见编码。
        返回文本/字符串内容；最后兜底用 'utf-8' errors='replace' 避免再次炸异常。
        仅供 _prepare() 在读取文本/JSON 文件时使用，不影响读二进制 bin。
        """
        try:
            with open(fp, mode, encoding=primary, **kw) as f:
                return f.read()
        except (UnicodeDecodeError, LookupError):
            # 二进制读，按列表顺序探测解码；优先 utf-8 系列（RPGMV JSON 几乎都是 utf-8）
            with open(fp, "rb") as fb:
                raw = fb.read()
            for enc in ("utf-8-sig", "utf-8", primary, "cp932", "shift_jis", "gbk", "big5"):
                try:
                    return raw.decode(enc)
                except (UnicodeDecodeError, LookupError):
                    continue
            return raw.decode("utf-8", errors="replace")

    @staticmethod
    def _load_json_fallback(fp, primary):
        """读 JSON：用户编码 → utf-8 等回退。返回 dict/list。"""
        try:
            with open(fp, "r", encoding=primary) as f:
                return json.load(f)
        except (UnicodeDecodeError, LookupError):
            # JSON 严格，不允许 errors='replace' 当数据；走多编码探测
            with open(fp, "rb") as fb:
                raw = fb.read()
            for enc in ("utf-8-sig", "utf-8", primary, "cp932", "shift_jis", "gbk", "big5"):
                try:
                    return json.loads(raw.decode(enc))
                except (UnicodeDecodeError, LookupError, json.JSONDecodeError):
                    continue
            # 实在不行抛出原始 UnicodeDecodeError 让上层感知
            raise

    def _prepare(self, fp):
        ftype = EXVAR.fileType
        EXVAR.clearBeforeParse()
        EXVAR.listOrig.clear()
        EXVAR.listCtrl.clear()
        EXVAR.contentInfos = {}
        EXVAR.insertContent = {}
        if EXVAR.readFileDataImp:
            if ftype == "bin":
                with open(fp, "rb") as f:
                    content, inserts = EXVAR.readFileDataImp(f, EXVAR.contentSeparate)
            else:
                # 文本读取：用户在 UI 选 cp932/shift_jis 等单字节编码，但文件实际是 UTF-8
                # 时会 UnicodeDecodeError；这里加 utf-8 系列兜底，避免整批文件全军覆没
                from io import StringIO
                data = self._read_text_fallback(fp, EXVAR.OldEncodeName)
                content, inserts = EXVAR.readFileDataImp(StringIO(data), EXVAR.contentSeparate)
            EXVAR.content, EXVAR.insertContent = content, inserts or {}
            return
        if ftype == "json":
            # JSON 不允许 replace：UTF-8 文件在 cp932 下会 UnicodeDecodeError，统一走回退
            EXVAR.content = self._load_json_fallback(fp, EXVAR.OldEncodeName)
            return
        if ftype == "txt":
            data = self._read_text_fallback(fp, EXVAR.OldEncodeName, newline="")
            # contentSeparate 为 None/空 都按换行分行（None 时 re.split(None) 会崩）
            if not EXVAR.contentSeparate:
                parts = data.split("\r\n")
                content = []
                for part in parts:
                    content.extend(part.split("\n"))
                EXVAR.content = content
            else:
                EXVAR.content = re.split(EXVAR.contentSeparate, data)
            return
        with open(fp, "rb") as f:
            data = f.read()
        cs = EXVAR.contentSeparate or b""
        if EXVAR.section:
            start, end = common.getInterval(EXVAR.section, data)
            if cs == b"":
                EXVAR.content = [bytearray(data[start:end])]
            else:
                EXVAR.content = re.split(cs, data[start:end])
            EXVAR.insertContent[0] = data[:start]
            EXVAR.insertContent[len(EXVAR.content)] = data[end:]
        elif cs == b"":
            EXVAR.content = [bytearray(data)]
        else:
            EXVAR.content = re.split(cs, data)

    def _root_fingerprint(self, root, max_files=5000):
        """扫描游戏目录建立轻量指纹；不读取大文件正文。"""
        ext_counts = {}; names = []; dirs = set(); total = 0
        try:
            for dp, dnames, files in os.walk(root):
                dnames[:] = [d for d in dnames if d.lower() not in {"_mtool_output","_data_backup","_font_backup","__pycache__",".git"}]
                rel_dp = os.path.relpath(dp, root).replace('\\','/').lower()
                if rel_dp != '.': dirs.add(rel_dp)
                for fn in files:
                    low = fn.lower(); ext = os.path.splitext(low)[1]
                    ext_counts[ext] = ext_counts.get(ext,0)+1
                    if len(names) < max_files: names.append(low)
                    total += 1
                    if total >= max_files: break
                if total >= max_files: break
        except Exception:
            pass
        return {"ext":ext_counts,"names":set(names),"dirs":dirs,"total":total}

    def _engine_heuristic_score(self, engine_name, fp):
        meta=self.meta(engine_name); postfix=str(meta.get("postfix","") or "").lower(); ftype=str(meta.get("file","txt") or "txt").lower()
        ext,names,dirs=fp["ext"],fp["names"],fp["dirs"]; score=0.0; reasons=[]
        if postfix:
            cnt=ext.get(postfix,0)
            if cnt:
                score += min(70,18+12*cnt); reasons.append(f"后缀 {postfix} ×{cnt}")
        if engine_name == "RPGMV":
            if "package.json" in names or "www/data" in dirs or any(n.startswith("map") and n.endswith(".json") for n in names):
                score += 70; reasons.append("RPG Maker MV/MZ 目录特征")
            if ext.get('.json',0) >= 5: score += 10; reasons.append("大量 JSON")
        elif engine_name == "RPGVX":
            if any(e in ext for e in ('.rvdata','.rvdata2','.rxdata')): score += 75; reasons.append("RGSS 数据文件")
            if any(x in names for x in ('game.rgss3a','game.rgss2a','game.rgssad')): score += 35; reasons.append("RGSS 归档")
        elif engine_name == "RenPy":
            topdirs={d.split('/')[0] for d in dirs}
            if 'game' in topdirs or any(n.endswith(('.rpy','.rpyc','.rpa')) for n in names): score += 60; reasons.append("Ren'Py game/脚本特征")
            score += min(25,8*int(ext.get('.rpy',0)>0)+8*int(ext.get('.rpa',0)>0)+5*int(ext.get('.rpyc',0)>0))
        elif engine_name == 'Unity':
            # Unity 原生 Windows 游戏的最强指纹：主 EXE + UnityPlayer.dll。
            # 仅靠 .ks/.txt/.ini 等后缀很容易被 SExtractor 的 RealLive 等规则误判。
            has_unity_player = 'unityplayer.dll' in names
            has_game_exe = any(n.endswith('.exe') and n != 'unitycrashhandler64.exe' for n in names)
            if has_unity_player and has_game_exe:
                score += 180; reasons.append('UnityPlayer.dll + 游戏 EXE 强特征')
            elif has_unity_player:
                score += 150; reasons.append('UnityPlayer.dll 强特征')
            if any(d.endswith('_data') or '/managed' in d or '/resources' in d for d in dirs) or any(n.endswith(('.assets','.bundle','.unity3d','.ress','.resource')) for n in names):
                score += 90; reasons.append('Unity *_Data/assets/bundle 特征')
            if any('globalgamemanagers' in n for n in names): score += 20; reasons.append('globalgamemanagers')
        elif engine_name == 'LiveMaker':
            # LiveMaker/LiveNovel 的最强指纹是 .lsb 编译脚本；辅以 live.dll / .lpb 工程文件。
            n_lsb = ext.get('.lsb', 0)
            if n_lsb:
                score += min(170, 70 + 20 * n_lsb); reasons.append(f'.lsb 脚本 ×{n_lsb}')
            if any(n in names for n in ('live.dll', 'livemaker.exe')) or ext.get('.lpb', 0):
                score += 45; reasons.append('LiveMaker 运行时特征')
        elif engine_name == "Unity_dat":
            if any(d.endswith('_data') or '/managed' in d or '/resources' in d for d in dirs) or any(n.endswith('.assets') for n in names):
                score += 65; reasons.append("Unity *_Data/assets 特征")
        elif engine_name == "TXT":
            if ext.get('.txt',0): score += min(20,5+ext.get('.txt',0))
        elif engine_name == "JSON":
            if ext.get('.json',0): score += min(20,5+ext.get('.json',0))
        elif engine_name == "BIN":
            c=ext.get('.bin',0)+ext.get('.dat',0)
            if c: score += min(18,4+c)
        if ftype == 'json' and ext.get('.json',0): score += min(8,2+ext.get('.json',0)*0.5)
        elif ftype == 'txt':
            c=sum(ext.get(e,0) for e in ('.txt','.ks','.scr','.ast','.spt','.csv','.tsv','.xml','.yaml','.yml','.ini','.rpy'))
            if c: score += min(10,2+c*0.2)
        if not postfix and engine_name in ('TXT','BIN','JSON'): score *= 0.55
        return score,reasons

    def autodetect(self, root, top_n=6, probe_top=3, progress=None):
        """自动识别最可能引擎：目录特征初筛 + 前几名真实试提取。"""
        if not os.path.isdir(root): return []
        fp=self._root_fingerprint(root); cand=[]
        custom_names=['Unity','LiveMaker']
        for name in custom_names:
            score,reasons=self._engine_heuristic_score(name,fp)
            if score>0: cand.append({'engine':name,'score':score,'reasons':reasons,'probe':0,'files':0})
        for name,_,_,_ in self.list_engines():
            score,reasons=self._engine_heuristic_score(name,fp)
            if score>0: cand.append({"engine":name,"score":score,"reasons":reasons,"probe":0,"files":0})
        cand.sort(key=lambda x:(-x['score'],x['engine'].lower())); cand=cand[:max(top_n,probe_top)]
        for idx,item in enumerate(cand[:probe_top]):
            name=item['engine']
            try:
                module=self.configure(name); files=self._files(root,name)[:3]; item['files']=len(files); extracted=0
                for j,fpath in enumerate(files):
                    EXVAR.workpath=os.path.dirname(fpath); EXVAR.filename=os.path.splitext(os.path.basename(fpath))[0]; EXVAR.isStart=1 if j==0 else (3 if j==len(files)-1 else 2)
                    try:
                        self._prepare(fpath); module.parseImp(EXVAR.content,EXVAR.listCtrl,self.deal_once); extracted += len([t for t in EXVAR.listOrig if isinstance(t,str) and t])
                    except Exception: continue
                item['probe']=extracted
                if extracted>0:
                    item['score'] += min(80,25+extracted*4); item['reasons'].append(f"试提取 {extracted} 条")
                elif files:
                    item['score'] -= 12; item['reasons'].append("试提取无文本")
                else:
                    item['score'] -= 6; item['reasons'].append("无匹配文件")
            except Exception as e:
                item['reasons'].append(f"试提取失败: {type(e).__name__}")
            if progress:
                try: progress(idx+1,min(probe_top,len(cand)),name)
                except Exception: pass
        cand.sort(key=lambda x:(-x['score'],x['engine'].lower())); return cand[:top_n]

    def _files(self, root, engine_name):
        meta = self.meta(engine_name)
        postfix = str(meta.get("postfix", "") or "").lower()
        ftype = str(meta.get("file", "txt") or "txt").lower()
        skip_ext = {".png",".jpg",".jpeg",".gif",".webp",".bmp",".ico",".mp3",".ogg",".wav",
                    ".flac",".mp4",".mkv",".avi",".webm",".dll",".exe",".so",".dylib",
                    ".ttf",".otf",".woff",".woff2",".zip",".rar",".7z",".gz",".pdf",".pak"}
        res = []
        for dp, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in {
                "_mtool_output","_data_backup","_font_backup","__pycache__",".git"}]
            for fn in files:
                low = fn.lower()
                fp = os.path.join(dp, fn)
                if postfix:
                    if not low.endswith(postfix):
                        continue
                elif ftype == "txt":
                    if not low.endswith((".txt",".ks",".scr",".asm",".ast",".spt",".script",
                                          ".rpy",".csv",".tsv",".xml",".yaml",".yml",".ini")):
                        continue
                elif ftype == "json":
                    if not low.endswith(".json"):
                        continue
                else:
                    if os.path.splitext(low)[1] in skip_ext:
                        continue
                try:
                    if os.path.getsize(fp) > 128 * 1024 * 1024:
                        continue
                except OSError:
                    continue
                res.append(fp)
        return sorted(res)

    @staticmethod
    def _xp3_files(root):
        out=[]
        for dp, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d.lower() not in {"_mtool_output","_data_backup","_font_backup","__pycache__",".git"}]
            for fn in files:
                if fn.lower().endswith('.xp3'):
                    out.append(os.path.join(dp, fn))
        return sorted(out)

    def _xp3_unpack(self, xp3_path, dst):
        """解包 Kirikiri XP3；优先内置标准XP3后端，失败时可调用本地 krkrxp3.exe。"""
        os.makedirs(dst, exist_ok=True)
        try:
            arc=XP3Archive(xp3_path)
            if arc.encrypted:
                raise XP3EncryptedError('XP3 文件含加密条目')
            arc.extract_to(dst)
            return 'builtin'
        except XP3EncryptedError:
            tool=find_external_tool(os.path.dirname(xp3_path))
            if tool and os.path.basename(tool).lower() == 'krkrxp3.exe':
                cp=subprocess.run([tool,'-m','extract',xp3_path,dst], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=300)
                if cp.returncode==0:
                    return 'external'
            raise

    def _krkr_extract(self, root, sample_override=None, encode_read=None, progress=None, filter_enabled=True, filter_mode='标准'):
        """处理普通 Krkr_Reg 文本 + XP3 内文本，合并为一个翻译字典。"""
        merged={}; stat={'files':0,'items':0,'filtered':0,'filter_reasons':{},'errors':[],'xp3':0,'xp3_errors':0}
        # 普通散落脚本
        base_result, base_stat=self.extract(root,'Krkr_Reg',sample_override,encode_read,progress,filter_enabled,filter_mode, include_xp3=False)
        merged.update(base_result)
        stat['files']+=base_stat.get('files',0); stat['filtered']+=base_stat.get('filtered',0)
        for k,v in base_stat.get('filter_reasons',{}).items(): stat['filter_reasons'][k]=stat['filter_reasons'].get(k,0)+v
        stat['errors'].extend(base_stat.get('errors',[]))
        xp3s=self._xp3_files(root)
        stat['xp3']=len(xp3s)
        if not xp3s:
            stat['items']=len(merged); return merged,stat
        temp_root=tempfile.mkdtemp(prefix='mtool_krkr_')
        try:
            for ai,xp3 in enumerate(xp3s,1):
                sub=os.path.join(temp_root,str(ai))
                try:
                    self._xp3_unpack(xp3,sub)
                    r,st=self.extract(sub,'Krkr_Reg',sample_override,encode_read,None,filter_enabled,filter_mode, include_xp3=False)
                    merged.update(r)
                    stat['files']+=st.get('files',0); stat['filtered']+=st.get('filtered',0)
                    for k,v in st.get('filter_reasons',{}).items(): stat['filter_reasons'][k]=stat['filter_reasons'].get(k,0)+v
                    stat['errors'].extend([f'{os.path.relpath(xp3,root)}: {e}' for e in st.get('errors',[])])
                except Exception as e:
                    stat['xp3_errors']+=1; stat['errors'].append(f'{os.path.relpath(xp3,root)}: XP3处理失败: {type(e).__name__}: {e}')
                if progress:
                    try: progress(ai,len(xp3s),os.path.relpath(xp3,root),0)
                    except Exception: pass
        finally:
            shutil.rmtree(temp_root,ignore_errors=True)
        stat['items']=len(merged); return merged,stat

    def _krkr_inject(self, root, translations, out_dir=None, sample_override=None, encode_read=None, encode_write=None, progress=None):
        """写回普通脚本及XP3；保留非文本资源，输出汉化后的文件副本。"""
        out_dir=out_dir or os.path.join(root,'_mtool_output')
        os.makedirs(out_dir,exist_ok=True)
        total=0; errors=[]; xp3s=self._xp3_files(root)
        # 先复制普通目录结构，再对普通脚本写回
        for dp, dirs, files in os.walk(root):
            rel=os.path.relpath(dp,root)
            if rel=='.': rel=''
            dirs[:] = [d for d in dirs if d.lower() not in {'_mtool_output','_data_backup','_font_backup','__pycache__','.git'}]
            for fn in files:
                if fn.lower().endswith('.xp3'): continue
                src=os.path.join(dp,fn); dst=os.path.join(out_dir,rel,fn)
                os.makedirs(os.path.dirname(dst),exist_ok=True)
                try: shutil.copy2(src,dst)
                except Exception as e: errors.append(f'{os.path.relpath(src,root)}:复制失败 {e}')
        try:
            st=self.inject(root,'Krkr_Reg',translations,out_dir=out_dir,sample_override=sample_override,encode_read=encode_read,encode_write=encode_write,progress=progress,include_xp3=False)
            total+=st.get('replaced',0); errors.extend(st.get('errors',[]))
        except Exception as e: errors.append(f'普通Krkr脚本写回失败: {e}')
        # XP3：解包整个归档 → 在副本中写回 → 重新打包
        for ai,xp3 in enumerate(xp3s,1):
            rel=os.path.relpath(xp3,root); out_xp3=os.path.join(out_dir,rel); work=tempfile.mkdtemp(prefix='mtool_krkr_pack_')
            try:
                mode=self._xp3_unpack(xp3,work)
                inject_root=tempfile.mkdtemp(prefix='mtool_krkr_inj_')
                try:
                    shutil.copytree(work,inject_root,dirs_exist_ok=True)
                    st=self.inject(inject_root,'Krkr_Reg',translations,out_dir=inject_root,sample_override=sample_override,encode_read=encode_read,encode_write=encode_write,progress=None,include_xp3=False)
                    total+=st.get('replaced',0); errors.extend([f'{rel}: {e}' for e in st.get('errors',[])])
                    if mode=='external':
                        tool=find_external_tool(os.path.dirname(xp3))
                        if not tool or os.path.basename(tool).lower()!='krkrxp3.exe': raise RuntimeError('外部XP3工具不可用')
                        os.makedirs(os.path.dirname(out_xp3),exist_ok=True)
                        cp=subprocess.run([tool,'-m','repack',inject_root,out_xp3],capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=300)
                        if cp.returncode!=0: raise RuntimeError(cp.stderr[-500:] or cp.stdout[-500:] or 'krkrxp3重包失败')
                    else:
                        os.makedirs(os.path.dirname(out_xp3),exist_ok=True)
                        XP3Archive.pack_dir(inject_root,out_xp3)
                finally:
                    shutil.rmtree(inject_root,ignore_errors=True)
            except Exception as e:
                errors.append(f'{rel}: XP3写回失败: {type(e).__name__}: {e}')
                # 保留原始XP3到输出，避免输出目录缺档
                try:
                    os.makedirs(os.path.dirname(out_xp3),exist_ok=True); shutil.copy2(xp3,out_xp3)
                except Exception: pass
            finally:
                shutil.rmtree(work,ignore_errors=True)
        return {'files':len(xp3s),'replaced':total,'errors':errors,'out_dir':out_dir,'xp3':len(xp3s)}

    # ==================== LiveMaker（.lsb 脚本，依赖 pylivemaker）====================
    # LiveMaker/LiveNovel 的 .lsb 是编译字节码，自行解析不现实，桥接成熟库 pylivemaker：
    #   提取：lmlsb extractcsv  → CSV(ID,Label,Context,Original text,Translated text)
    #   回写：lmlsb insertcsv   （按 ID 匹配，原地改 .lsb）
    #   归档：lmar x            （从 exe/dat 解出 .lsb；无 repack，走散装补丁）
    # 与 Unity 一样属于“虚拟引擎”：不在 engine.ini 里，由 extract/inject 特判分派。

    @staticmethod
    def _livemaker_available():
        try:
            import livemaker  # noqa: F401
            return True
        except Exception:
            return False

    @staticmethod
    def _lsb_files(root):
        out = []
        for dp, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d.lower() not in {"_mtool_output", "_data_backup", "_font_backup", "__pycache__", ".git"}]
            for fn in files:
                if fn.lower().endswith('.lsb'):
                    out.append(os.path.join(dp, fn))
        return sorted(out)

    @staticmethod
    def _livemaker_pick_archive(root):
        """游戏根目录里挑最可能的 LiveMaker 归档（exe/dat），按体积大优先。"""
        cands = []
        try:
            for fn in os.listdir(root):
                fp = os.path.join(root, fn)
                if not os.path.isfile(fp):
                    continue
                low = fn.lower()
                if (low.endswith('.exe') or low.endswith('.dat')) and low not in ('unins000.exe', 'unitycrashhandler64.exe'):
                    try:
                        sz = os.path.getsize(fp)
                    except Exception:
                        sz = 0
                    cands.append((sz, fp))
        except Exception:
            pass
        cands.sort(reverse=True)
        return cands[0][1] if cands else None

    @staticmethod
    def _run_lm(tool, args, timeout=600):
        """调用 pylivemaker CLI（tool 取 'lmlsb'/'lmar'）。用当前解释器 -c 载入库入口，
        与运行环境一致，无需 PATH 里有 lmlsb.exe。返回 CompletedProcess。"""
        cmd = [sys.executable, '-c', 'from livemaker.cli import %s; %s()' % (tool, tool)] + [str(a) for a in args]
        return subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=timeout)

    def _lsb_extract_rows(self, lsb_path):
        """对单个 .lsb 跑 lmlsb extractcsv，返回行 [[ID,Label,Context,Original,Translated],...]（去表头）。失败返回 None。"""
        tmp = lsb_path + ".mtool_extract.csv"
        try:
            cp = self._run_lm('lmlsb', ['extractcsv', lsb_path, tmp, '-e', 'utf-8', '--overwrite'])
            if cp.returncode != 0 or not os.path.isfile(tmp):
                return None
            rows = []
            # 与 pylivemaker 完全一致的 open/csv 设置，保证往返格式兼容
            with open(tmp, 'r', encoding='utf-8', newline='\n') as f:
                reader = csv.reader(f, delimiter=',', quotechar='"', quoting=csv.QUOTE_MINIMAL)
                for i, row in enumerate(reader):
                    if not row:
                        continue
                    if i == 0 and row[0] == 'ID':  # 跳过表头
                        continue
                    rows.append((list(row) + ['', '', '', '', ''])[:5])
            return rows
        except Exception:
            return None
        finally:
            try:
                if os.path.isfile(tmp):
                    os.remove(tmp)
            except Exception:
                pass

    def _lsb_inject_one(self, lsb_path, translations):
        """对单个 .lsb 回填译文：extractcsv 取带 ID 的原始行 → 按原文查表填“Translated text” → insertcsv 原地改。
        返回 (回填条数, 错误信息或None)。"""
        rows = self._lsb_extract_rows(lsb_path)
        if rows is None:
            return 0, "extractcsv 失败"
        n = 0
        for r in rows:
            orig = r[3]
            dst = translations.get(orig)
            if isinstance(dst, str) and dst and dst != orig:
                r[4] = dst
                n += 1
        if n == 0:
            return 0, None
        tmp = lsb_path + ".mtool_insert.csv"
        try:
            with open(tmp, 'w', encoding='utf-8', newline='\n') as f:
                w = csv.writer(f, delimiter=',', quotechar='"', quoting=csv.QUOTE_MINIMAL)
                w.writerow(["ID", "Label", "Context", "Original text", "Translated text"])
                for r in rows:
                    w.writerow(r)
            cp = self._run_lm('lmlsb', ['insertcsv', lsb_path, tmp, '-e', 'utf-8', '--no-backup'])
            if cp.returncode != 0:
                return 0, (cp.stderr or cp.stdout or 'insertcsv 失败')[-300:]
            return n, None
        finally:
            try:
                if os.path.isfile(tmp):
                    os.remove(tmp)
            except Exception:
                pass

    def _livemaker_extract(self, root, progress=None, filter_enabled=True, filter_mode='标准'):
        if not self._livemaker_available():
            raise RuntimeError("未安装 pylivemaker。请在对话框点击「安装 pylivemaker」后重试。")
        merged = {}
        stat = {'files': 0, 'items': 0, 'filtered': 0, 'filter_reasons': {}, 'errors': [], 'backend': 'pylivemaker', 'warnings': []}
        lsb_files = self._lsb_files(root)
        tmp_extract = None
        if not lsb_files:  # 无散装 .lsb：尝试从 exe/dat 归档解包
            arc = self._livemaker_pick_archive(root)
            if arc:
                tmp_extract = tempfile.mkdtemp(prefix='mtool_lm_arc_')
                cp = self._run_lm('lmar', ['x', arc, '-o', tmp_extract])
                lsb_files = self._lsb_files(tmp_extract)
                if not lsb_files:
                    stat['errors'].append(f"归档 {os.path.basename(arc)} 未解出 .lsb：{(cp.stderr or cp.stdout or '')[-200:]}")
        if not lsb_files:
            if tmp_extract:
                shutil.rmtree(tmp_extract, ignore_errors=True)
            stat['warnings'].append("未找到 .lsb 脚本，也无法从 exe/dat 归档解出。请确认这是 LiveMaker 游戏。")
            return merged, stat
        try:
            total = len(lsb_files)
            for i, lsb in enumerate(lsb_files, 1):
                rows = self._lsb_extract_rows(lsb)
                if rows is None:
                    stat['errors'].append(f"{os.path.basename(lsb)}: extractcsv 失败")
                else:
                    stat['files'] += 1
                    for r in rows:
                        orig = r[3]
                        if not isinstance(orig, str) or not orig:
                            continue
                        if filter_enabled:
                            ok, q, rs = TextQualityFilter.accept(orig, filter_mode)
                            if not ok:
                                stat['filtered'] += 1
                                key = rs[0] if rs else '低可信度'
                                stat['filter_reasons'][key] = stat['filter_reasons'].get(key, 0) + 1
                                continue
                        merged.setdefault(orig, "")
                if progress:
                    try:
                        progress(i, total, os.path.basename(lsb), len(rows or []))
                    except Exception:
                        pass
        finally:
            if tmp_extract:
                shutil.rmtree(tmp_extract, ignore_errors=True)
        stat['items'] = len(merged)
        return merged, stat

    def _livemaker_inject(self, root, translations, out_dir=None, progress=None):
        if not self._livemaker_available():
            raise RuntimeError("未安装 pylivemaker。请在对话框点击「安装 pylivemaker」后重试。")
        out_dir = out_dir or os.path.join(root, '_mtool_output')
        os.makedirs(out_dir, exist_ok=True)
        errors = []
        warnings = []
        total = 0
        loose = self._lsb_files(root)
        if loose:
            # 情况A：散装 .lsb —— 复制整个游戏目录到输出，随后对副本里的 .lsb 打补丁（drop-in 汉化包）
            for dp, dirs, files in os.walk(root):
                rel = os.path.relpath(dp, root)
                rel = '' if rel == '.' else rel
                dirs[:] = [d for d in dirs if d.lower() not in {'_mtool_output', '_data_backup', '_font_backup', '__pycache__', '.git'}]
                for fn in files:
                    src = os.path.join(dp, fn)
                    dst = os.path.join(out_dir, rel, fn)
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    try:
                        shutil.copy2(src, dst)
                    except Exception as e:
                        errors.append(f'{os.path.relpath(src, root)}: 复制失败 {e}')
            targets = self._lsb_files(out_dir)
        else:
            # 情况B：打包在 exe/dat —— 解包到输出目录，对解出的 .lsb 打补丁（LiveMaker 优先加载散装 .lsb）
            arc = self._livemaker_pick_archive(root)
            if not arc:
                raise RuntimeError("未找到 .lsb，也未找到可解包的 LiveMaker 归档（exe/dat）。")
            cp = self._run_lm('lmar', ['x', arc, '-o', out_dir])
            targets = self._lsb_files(out_dir)
            if not targets:
                raise RuntimeError(f"解包归档失败或无 .lsb：{(cp.stderr or cp.stdout or '')[-300:]}")
            warnings.append("文本打包在 exe/dat 中：已输出散装 .lsb 补丁，请把输出目录里的文件覆盖回游戏目录（LiveMaker 会优先加载散装 .lsb）。")
        total_files = len(targets)
        for i, lsb in enumerate(targets, 1):
            n = 0
            try:
                n, err = self._lsb_inject_one(lsb, translations)
                total += n
                if err:
                    errors.append(f'{os.path.basename(lsb)}: {err}')
            except Exception as e:
                errors.append(f'{os.path.basename(lsb)}: {type(e).__name__}: {e}')
            if progress:
                try:
                    progress(i, total_files, os.path.basename(lsb), n)
                except Exception:
                    pass
        return {'files': total_files, 'replaced': total, 'errors': errors, 'out_dir': out_dir, 'warnings': warnings}

    def extract(self, root, engine_name, sample_override=None, encode_read=None, progress=None, filter_enabled=True, filter_mode="标准", include_xp3=True):
        if engine_name == 'LiveMaker':
            return self._livemaker_extract(root, progress=progress, filter_enabled=filter_enabled, filter_mode=filter_mode)
        if engine_name == 'Unity':
            result, stat = self.unity_backends.extract(root, backend=getattr(self, 'unity_backend_mode', '自动'), filter_enabled=filter_enabled, filter_mode=filter_mode, progress=progress)
            return result, stat
        if include_xp3 and engine_name in ("Krkr_Reg", "Krkr") and self._xp3_files(root):
            return self._krkr_extract(root, sample_override, encode_read, progress, filter_enabled, filter_mode)
        module = self.configure(engine_name, sample_override=sample_override, encode_read=encode_read)
        files = self._files(root, engine_name)
        result, errors = {}, []
        filtered = 0
        filter_reasons = {}
        for idx, fp in enumerate(files, 1):
            try:
                EXVAR.workpath = os.path.dirname(fp)
                EXVAR.filename = os.path.splitext(os.path.basename(fp))[0]
                EXVAR.isStart = 1 if idx == 1 else (3 if idx == len(files) else 2)
                self._prepare(fp)
                module.parseImp(EXVAR.content, EXVAR.listCtrl, self.deal_once)
                for text in EXVAR.listOrig:
                    if isinstance(text, str) and text:
                        if filter_enabled:
                            ok, q, rs = TextQualityFilter.accept(text, filter_mode)
                            if not ok:
                                filtered += 1
                                key = rs[0] if rs else "低可信度"
                                filter_reasons[key] = filter_reasons.get(key, 0) + 1
                                continue
                        result.setdefault(text, "")
                if progress:
                    progress(idx, len(files), os.path.relpath(fp, root), len(EXVAR.listOrig))
            except Exception as e:
                errors.append(f"{os.path.relpath(fp, root)}: {type(e).__name__}: {e}")
        return result, {"files": len(files), "items": len(result), "filtered": filtered, "filter_reasons": filter_reasons, "errors": errors}

    def inject(self, root, engine_name, translations, out_dir=None, sample_override=None, encode_read=None, encode_write=None, progress=None, include_xp3=True):
        # 写回前统一还原译文里的换行占位标记（〔NL0]/〔NL1] 等→\n），否则游戏会把
        # 标记当正文显示。就地改写该次调用持有的翻译字典，写回产物即为最终形态。
        _sanitize_translations(translations)
        if engine_name == 'LiveMaker':
            return self._livemaker_inject(root, translations, out_dir=out_dir, progress=progress)
        if engine_name == 'Unity':
            out_dir = out_dir or os.path.join(root, '_mtool_output')
            locs = None
            meta = translations.get('_mtool_meta')
            if isinstance(meta, dict):
                locs = meta.get('locations')
            return self.unity_backends.inject(root, translations, out_dir, backend=getattr(self, 'unity_backend_mode', '自动'), progress=progress, locations=locs)
        if include_xp3 and engine_name in ("Krkr_Reg", "Krkr") and self._xp3_files(root):
            return self._krkr_inject(root, translations, out_dir, sample_override, encode_read, encode_write, progress)
        module = self.configure(engine_name, sample_override=sample_override, encode_read=encode_read, encode_write=encode_write)
        files = self._files(root, engine_name)
        out_dir = out_dir or os.path.join(root, "_mtool_output")
        total, errors = 0, []
        for idx, fp in enumerate(files, 1):
            try:
                EXVAR.workpath = os.path.dirname(fp)
                EXVAR.filename = os.path.splitext(os.path.basename(fp))[0]
                EXVAR.isStart = 1 if idx == 1 else (3 if idx == len(files) else 2)
                self._prepare(fp)
                module.parseImp(EXVAR.content, EXVAR.listCtrl, self.deal_once)
                local = 0
                for i in range(len(EXVAR.listOrig) - 1, -1, -1):
                    src = EXVAR.listOrig[i]
                    dst = translations.get(src)
                    if dst is None:
                        # 控码归一化后备匹配：处理 \V[\d+] \C[\d+] \n<…> 等差异
                        # （抽出的 listOrig 保留原文控制码，123.json 等老翻译表 key 是剥净的）
                        src_norm = self._norm_rpgmv_ctrl(src)
                        if src_norm and src_norm != src:
                            dst = translations.get(src_norm)
                    if not isinstance(dst, str) or not dst or dst == src:
                        continue
                    ok = module.replaceOnceImp(EXVAR.content, [EXVAR.listCtrl[i]], [dst])
                    if ok is not False:
                        local += 1
                _end = getattr(module, "replaceEndImp", None)
                if _end:
                    _end(EXVAR.content)
                self._write(os.path.join(out_dir, os.path.relpath(fp, root)))
                total += local
                if progress:
                    progress(idx, len(files), os.path.relpath(fp, root), local)
            except Exception as e:
                errors.append(f"{os.path.relpath(fp, root)}: {type(e).__name__}: {e}")
        return {"files": len(files), "replaced": total, "errors": errors, "out_dir": out_dir}

    @staticmethod
    def _norm_rpgmv_ctrl(s):
        """剥掉 RPG Maker MV/MZ 文本控制码，便于和已剥干净的翻译字典 key 对齐匹配。
        不剥 \\\\ 本身（保留原文中的反斜杠转义），不影响正常文本。"""
        import re as _re
        if not isinstance(s, str):
            return s
        s = _re.sub(r"\\[VvNnIiCcGgPpJjSsFfBbAaKk]\s*\[\s*\d+\s*\]", "", s)
        s = _re.sub(r"\\[VvNnIiCcGgPpJjSsFfBbAaKk]", "", s)
        s = _re.sub(r"\\n<[^>]+>", "", s)
        s = _re.sub(r"\\[<>.\|!~\$?#* ]", "", s)
        return s.strip()

    def _write(self, dst):
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if EXVAR.fileType == "json":
            with open(dst, "w", encoding=EXVAR.NewEncodeName or "utf-8") as f:
                json.dump(EXVAR.content, f, ensure_ascii=False, indent=getattr(EXVAR, "indent", 2))
            return
        if EXVAR.fileType == "txt":
            sep = "\r\n"
            if EXVAR.contentSeparate:
                try:
                    sep = EXVAR.contentSeparate.encode().decode("unicode_escape")
                except Exception:
                    sep = EXVAR.contentSeparate
            if getattr(EXVAR, "newline", None):
                try:
                    sep = EXVAR.newline.encode().decode("unicode_escape")
                except Exception:
                    pass
            with open(dst, "w", encoding=EXVAR.NewEncodeName or "utf-8", newline="") as f:
                for i, part in enumerate(EXVAR.content):
                    f.write(part if isinstance(part, str) else str(part))
                    if i < len(EXVAR.content)-1 and getattr(EXVAR, "addSeparate", True):
                        f.write(sep)
            return
        sep = EXVAR.contentSeparate
        if isinstance(sep, str):
            sep = sep.encode("latin-1").decode("unicode_escape").encode("latin-1")
        sep = sep or b""
        data = bytearray()
        for i, part in enumerate(EXVAR.content):
            if i in EXVAR.insertContent:
                data.extend(EXVAR.insertContent[i])
            data.extend(part)
            if i < len(EXVAR.content)-1 and getattr(EXVAR, "addSeparate", True):
                data.extend(sep)
        if len(EXVAR.content) in EXVAR.insertContent:
            data.extend(EXVAR.insertContent[len(EXVAR.content)])
        with open(dst, "wb") as f:
            f.write(data)


def _load_trans(path):
    with open(path, "r", encoding="utf-8-sig") as f:
        obj = json.load(f)
    if isinstance(obj, dict):
        # 保留 _mtool_meta（dict 值）：内含提取阶段记录的定位信息，写回时用于精准命中
        return {k:v for k,v in obj.items() if isinstance(k,str) and (isinstance(v,str) or (k=="_mtool_meta" and isinstance(v,dict)))}
    out={}
    if isinstance(obj, list):
        for x in obj:
            if not isinstance(x,dict): continue
            src=None
            for k in ("message","msgRN","origRN","orig"):
                if isinstance(x.get(k),str):
                    src=x[k]; break
            if src is not None:
                dst=x.get("translation",x.get("trans",x.get("new","")))
                if isinstance(dst,str): out[src]=dst
    return out


_APP=None
_BRIDGE=None
_DIALOG=None

class UniversalDialog:
    def __init__(self, app, bridge):
        self.app=app; self.bridge=bridge
        self.win=tk.Toplevel(app.root); self.win.title(PLUGIN_NAME); self.win.geometry("920x700")
        self.game=tk.StringVar(); self.engine=tk.StringVar(); self.trans=tk.StringVar(); self.out=tk.StringVar()
        self.er=tk.StringVar(value="cp932"); self.ew=tk.StringVar(value="cp932")
        self.filter_enabled=tk.BooleanVar(value=True); self.filter_mode=tk.StringVar(value="标准")
        frm=ttk.Frame(self.win,padding=10); frm.pack(fill=tk.BOTH,expand=True)

        def row(label,var,cmd=None):
            r=ttk.Frame(frm); r.pack(fill=tk.X,pady=3)
            ttk.Label(r,text=label,width=12).pack(side=tk.LEFT)
            ttk.Entry(r,textvariable=var).pack(side=tk.LEFT,fill=tk.X,expand=True)
            if cmd: ttk.Button(r,text="浏览",command=cmd).pack(side=tk.LEFT,padx=4)
        row("游戏目录",self.game,self.pick_game)
        detect_row=ttk.Frame(frm); detect_row.pack(fill=tk.X,pady=2)
        self.detect_status=tk.StringVar(value="未识别")
        ttk.Button(detect_row,text="🤖 自动识别引擎",command=self.autodetect).pack(side=tk.LEFT)
        ttk.Label(detect_row,textvariable=self.detect_status,foreground="gray").pack(side=tk.LEFT,padx=8)
        vals=["选择引擎", "Unity（深度提取）", "LiveMaker（LSB脚本）"]+[x[0] for x in bridge.list_engines()]
        self.ec=ttk.Combobox(frm,textvariable=self.engine,values=vals,state="readonly",width=42)
        self.ec.pack(fill=tk.X,pady=3); self.engine.set(vals[0])
        self.ec.bind("<<ComboboxSelected>>", lambda e: self.load_rule())
        backend_row=ttk.Frame(frm); backend_row.pack(fill=tk.X,pady=3)
        ttk.Label(backend_row,text="Unity后端",width=12).pack(side=tk.LEFT)
        self.unity_backend=tk.StringVar(value="自动")
        self.unity_backend_combo=ttk.Combobox(backend_row,textvariable=self.unity_backend,values=["自动","UnityPy","AssetRipper","传统扫描"],state="readonly",width=18)
        self.unity_backend_combo.pack(side=tk.LEFT)
        self.unity_backend_status=tk.StringVar(value="未检测")
        ttk.Label(backend_row,textvariable=self.unity_backend_status,foreground="gray").pack(side=tk.LEFT,padx=8)
        ttk.Button(backend_row,text="检测后端",command=self.check_unity_backends).pack(side=tk.LEFT,padx=3)
        ttk.Button(backend_row,text="安装 UnityPy",command=self.install_unitypy).pack(side=tk.LEFT,padx=3)
        ttk.Button(backend_row,text="选择 AssetRipper",command=self.pick_assetripper).pack(side=tk.LEFT,padx=3)
        lm_row=ttk.Frame(frm); lm_row.pack(fill=tk.X,pady=3)
        ttk.Label(lm_row,text="LiveMaker",width=12).pack(side=tk.LEFT)
        self.lm_status=tk.StringVar(value="未检测")
        ttk.Label(lm_row,textvariable=self.lm_status,foreground="gray").pack(side=tk.LEFT,padx=8)
        ttk.Button(lm_row,text="检测 pylivemaker",command=self.check_livemaker).pack(side=tk.LEFT,padx=3)
        ttk.Button(lm_row,text="安装 pylivemaker",command=self.install_pylivemaker).pack(side=tk.LEFT,padx=3)
        ttk.Label(frm,text="SExtractor规则（可直接编辑；默认使用 engine.ini 内置规则）").pack(anchor=tk.W,pady=(6,2))
        self.rule=scrolledtext.ScrolledText(frm,height=8,wrap=tk.NONE)
        self.rule.pack(fill=tk.X,pady=(0,4))
        row("翻译JSON",self.trans,self.pick_trans)
        row("输出目录",self.out,self.pick_out)
        rr=ttk.Frame(frm); rr.pack(fill=tk.X,pady=3)
        ttk.Label(rr,text="读取编码",width=12).pack(side=tk.LEFT)
        ttk.Combobox(rr,textvariable=self.er,values=["cp932","shift_jis","utf-8","utf-16","gbk","big5","latin-1"],width=14).pack(side=tk.LEFT)
        ttk.Label(rr,text="写入编码",width=12).pack(side=tk.LEFT,padx=(20,0))
        ttk.Combobox(rr,textvariable=self.ew,values=["cp932","shift_jis","utf-8","utf-16","gbk","big5","latin-1"],width=14).pack(side=tk.LEFT)
        fr=ttk.Frame(frm); fr.pack(fill=tk.X,pady=4)
        ttk.Checkbutton(fr,text="翻译前智能过滤",variable=self.filter_enabled).pack(side=tk.LEFT)
        ttk.Label(fr,text="级别:").pack(side=tk.LEFT,padx=(18,4))
        ttk.Combobox(fr,textvariable=self.filter_mode,values=["严格","标准","激进"],state="readonly",width=10).pack(side=tk.LEFT)
        ttk.Label(fr,text="严格=少漏翻译垃圾少；标准=平衡；激进=尽量不漏文本",foreground="gray").pack(side=tk.LEFT,padx=8)
        ttk.Label(frm,text="Kirikiri：检测到 .xp3 时自动解包/翻译/重新打包（未加密 XP3 走内置后端；加密档需本机 krkrxp3.exe）",foreground="gray",wraplength=880).pack(anchor=tk.W,pady=(0,4))
        bb=ttk.Frame(frm); bb.pack(fill=tk.X,pady=8)
        ttk.Button(bb,text="提取文本",command=self.extract).pack(side=tk.LEFT,padx=3)
        ttk.Button(bb,text="导入并写回",command=self.inject).pack(side=tk.LEFT,padx=3)
        ttk.Button(bb,text="刷新引擎",command=self.refresh).pack(side=tk.LEFT,padx=3)
        ttk.Button(bb,text="引擎列表",command=self.show_engines).pack(side=tk.LEFT,padx=3)
        self.status=tk.StringVar(value="就绪"); ttk.Label(frm,textvariable=self.status).pack(anchor=tk.W)
        self.pb=ttk.Progressbar(frm,mode="determinate"); self.pb.pack(fill=tk.X,pady=4)
        self.log=scrolledtext.ScrolledText(frm,height=20); self.log.pack(fill=tk.BOTH,expand=True)
        self._setup_dnd()
        self.load_rule()
        self.check_unity_backends(silent=True)

    def _setup_dnd(self):
        """插件窗口自身支持拖入游戏 EXE/文件夹/JSON。
        依赖 tkinterdnd2；主程序若已启用拖拽则复用其 TkDnD 根窗口。
        """
        self._dnd_available = False
        try:
            from tkinterdnd2 import DND_FILES
            self._dnd_available = True

            def _register(widget):
                try:
                    widget.drop_target_register(DND_FILES)
                    widget.dnd_bind('<<Drop>>', self._on_drop)
                except Exception:
                    pass
                try:
                    for child in widget.winfo_children():
                        _register(child)
                except Exception:
                    pass

            _register(self.win)
            self.msg("拖拽已启用：可把游戏 EXE / 游戏文件夹 / 翻译 JSON 直接拖入本窗口")
        except Exception as e:
            self._dnd_error = str(e)
            self.msg("拖拽未启用：请安装 tkinterdnd2；点击‘选择游戏目录’仍可正常使用")

    @staticmethod
    def _parse_drop_paths(data):
        """解析 tkinterdnd2 的 DND_FILES；支持大括号路径、空格和多个文件。"""
        import shlex
        s = (data or '').strip()
        if not s:
            return []
        # Windows tkinterdnd2 常见格式：{C:/My Game/game.exe} {D:/x/y}
        paths = re.findall(r'\{([^}]*)\}', s)
        if paths:
            # 花括号之外的裸路径也兼容
            rest = re.sub(r'\{[^}]*\}', ' ', s).strip()
            if rest:
                try:
                    paths.extend(shlex.split(rest, posix=False))
                except Exception:
                    paths.extend(rest.split())
            return [x.strip().strip('"') for x in paths if x.strip()]
        try:
            return [x.strip().strip('"') for x in shlex.split(s, posix=False) if x.strip()]
        except Exception:
            return [s.strip('"')]

    def _on_drop(self, event):
        """插件窗口拖入：EXE/目录自动作为游戏目录并触发引擎识别；JSON作为翻译文件。"""
        paths = self._parse_drop_paths(getattr(event, 'data', ''))
        if not paths:
            return
        # 优先处理第一个有效项目路径
        game_path = None
        json_path = None
        for raw in paths:
            path = os.path.normpath(raw)
            if not os.path.exists(path):
                continue
            if os.path.isfile(path) and path.lower().endswith('.json'):
                json_path = path
                continue
            if os.path.isfile(path) and path.lower().endswith('.exe'):
                game_path = os.path.dirname(os.path.abspath(path))
                self.msg(f"🎮 拖入游戏 EXE：{os.path.basename(path)}")
                break
            if os.path.isdir(path):
                game_path = os.path.abspath(path)
                self.msg(f"📁 拖入游戏目录：{game_path}")
                break

        if json_path:
            self.trans.set(json_path)
            self.msg(f"📄 已载入翻译 JSON：{json_path}")
            if game_path is None:
                d = os.path.dirname(json_path)
                # JSON 若位于游戏目录或常见输出目录中，尝试反推游戏目录
                if os.path.basename(d).lower() in {'_mtool_output', 'translation', 'translations'}:
                    game_path = os.path.dirname(d)
                else:
                    game_path = d

        if game_path:
            self.game.set(os.path.normpath(game_path))
            self.detect_status.set("正在扫描…")
            self.msg(f"🔍 自动识别游戏：{game_path}")
            self.autodetect()
        elif not json_path:
            self.msg("⚠️ 拖入内容不是有效的游戏 EXE、游戏目录或 JSON")

    def msg(self,s):
        self.status.set(s); self.log.insert(tk.END,s+"\n"); self.log.see(tk.END); self.win.update_idletasks()
    def check_unity_backends(self, silent=False):
        try:
            st=self.bridge.unity_backends.status()
            parts=[]
            parts.append("UnityPy ✓" if st["UnityPy"] else "UnityPy ✗")
            parts.append("AssetRipper ✓" if st["AssetRipper"] else "AssetRipper ✗")
            parts.append("TTGen ✓" if st.get("TTGen") else "TTGen ✗(MonoBehaviour 解析受限)")
            self.unity_backend_status.set(" | ".join(parts))
            if not silent:
                msg=(f"UnityPy：{'可用' if st['UnityPy'] else '不可用'}\n"
                     f"AssetRipper：{'可用' if st['AssetRipper'] else '未找到'}\n")
                if st.get('UnityPyError'): msg += f"\nUnityPy：{st['UnityPyError']}"
                if st.get('AssetRipperPath'): msg += f"\nAssetRipper：{st['AssetRipperPath']}"
                messagebox.showinfo("Unity 后端",msg,parent=self.win)
        except Exception as e:
            self.unity_backend_status.set("检测失败")
            if not silent: messagebox.showerror("检测失败",str(e),parent=self.win)

    def install_unitypy(self):
        """在当前 MTool Python 环境安装 UnityPy；不捆绑平台相关二进制依赖。"""
        if messagebox.askyesno("安装 UnityPy", "将使用当前 Python 解释器执行 pip install UnityPy。\n需要网络连接。\n\n继续？", parent=self.win):
            def worker():
                try:
                    cp=subprocess.run([sys.executable,"-m","pip","install","-U","UnityPy"],capture_output=True,text=True,encoding="utf-8",errors="replace",timeout=900)
                    if cp.returncode!=0: raise RuntimeError((cp.stderr or cp.stdout)[-1800:])
                    importlib.invalidate_caches()
                    self.bridge.unity_backends.unitypy=UnityPyBackend()
                    self.win.after(0,lambda:(self.check_unity_backends(silent=True),messagebox.showinfo("完成","UnityPy 安装完成。",parent=self.win)))
                except Exception as e:
                    self.win.after(0,lambda e=e:messagebox.showerror("安装失败",str(e),parent=self.win))
            threading.Thread(target=worker,daemon=True).start()

    def check_livemaker(self, silent=False):
        try:
            ok = self.bridge._livemaker_available()
            ver = ""
            if ok:
                try:
                    import livemaker
                    ver = " " + getattr(livemaker, "__version__", "")
                except Exception:
                    pass
            self.lm_status.set(("pylivemaker ✓" + ver) if ok else "pylivemaker ✗（未安装）")
            if not silent:
                messagebox.showinfo("LiveMaker 后端", "pylivemaker：%s" % ("已安装" + ver if ok else "未安装，点「安装 pylivemaker」自动安装"), parent=self.win)
        except Exception as e:
            self.lm_status.set("检测失败")
            if not silent: messagebox.showerror("检测失败", str(e), parent=self.win)

    def install_pylivemaker(self):
        """在当前 MTool Python 环境安装 pylivemaker（提供 lmlsb/lmar 供 .lsb 提取与写回）。"""
        if messagebox.askyesno("安装 pylivemaker", "将使用当前 Python 解释器执行 pip install pylivemaker。\n需要网络连接。\n\n继续？", parent=self.win):
            def worker():
                try:
                    cp=subprocess.run([sys.executable,"-m","pip","install","-U","pylivemaker"],capture_output=True,text=True,encoding="utf-8",errors="replace",timeout=900)
                    if cp.returncode!=0: raise RuntimeError((cp.stderr or cp.stdout)[-1800:])
                    importlib.invalidate_caches()
                    self.win.after(0,lambda:(self.check_livemaker(silent=True),messagebox.showinfo("完成","pylivemaker 安装完成。",parent=self.win)))
                except Exception as e:
                    self.win.after(0,lambda e=e:messagebox.showerror("安装失败",str(e),parent=self.win))
            threading.Thread(target=worker,daemon=True).start()

    def pick_assetripper(self):
        f=filedialog.askopenfilename(parent=self.win,title="选择 AssetRipper 可执行文件",filetypes=[("AssetRipper","*.exe"),("所有文件","*.*")])
        if f:
            self.bridge.unity_backends.assetripper.set_path(f)
            self.unity_backend.set("AssetRipper")
            self.check_unity_backends(silent=True)
    def autodetect(self):
        game=self.game.get().strip()
        if not os.path.isdir(game):
            messagebox.showwarning("提示","请选择游戏目录",parent=self.win); return
        self.detect_status.set("正在扫描…"); self.msg("开始自动识别游戏引擎…")
        def worker():
            try:
                def prog(i,total,name): self.win.after(0,lambda:self.detect_status.set(f"试探 {i}/{total}: {name}"))
                results=self.bridge.autodetect(game,top_n=6,probe_top=3,progress=prog)
                def done():
                    if not results:
                        self.detect_status.set("未找到明显匹配"); self.msg("未找到明显匹配的 SExtractor 引擎"); return
                    self.detect_status.set("已识别："+results[0]['engine']); self.engine.set(results[0]['engine']); self.load_rule()
                    lines=["自动识别结果：",""]
                    for i,r in enumerate(results,1): lines.append(f"{i}. {r['engine']}  分数={r['score']:.1f}  " + "；".join(r['reasons']))
                    self.msg("\n".join(lines))
                    messagebox.showinfo("自动识别完成","最可能引擎：%s\n\n%s" % (results[0]['engine'],"\n".join(f"{i}. {r['engine']} ({r['score']:.1f})" for i,r in enumerate(results[:5],1))),parent=self.win)
                self.win.after(0,done)
            except Exception as e:
                self.win.after(0,lambda:self.detect_status.set("失败")); self.win.after(0,lambda e=e:messagebox.showerror("识别失败",str(e),parent=self.win))
        threading.Thread(target=worker,daemon=True).start()
    def pick_game(self):
        d=filedialog.askdirectory(parent=self.win)
        if d:
            self.game.set(os.path.normpath(d)); self.autodetect()
    def pick_trans(self):
        f=filedialog.askopenfilename(parent=self.win,filetypes=[("JSON","*.json"),("所有文件","*.*")])
        if f:self.trans.set(f)
    def pick_out(self):
        d=filedialog.askdirectory(parent=self.win)
        if d:self.out.set(d)
    def load_rule(self):
        eng=self.selected()
        self.rule.delete("1.0",tk.END)
        if eng == "Unity":
            self.rule.insert(tk.END,"# Unity 后端：自动=UnityPy优先→AssetRipper兜底→传统扫描\n# UnityPy 负责 SerializedFile/AssetBundle 对象级提取与可写回\n# AssetRipper 负责复杂版本/Bundle 的项目导出兜底分析\n# 传统扫描仅作最后兜底，不承担 Unity 二进制安全写回。")
        elif eng == "LiveMaker":
            self.rule.insert(tk.END,"# LiveMaker / LiveNovel（.lsb 编译脚本）后端，依赖 pylivemaker。\n# 提取：lmlsb extractcsv 逐个 .lsb 导出文本；散装 .lsb 直接处理，打包在 exe/dat 里则先 lmar 解包。\n# 写回：lmlsb insertcsv 按 ID 原地改 .lsb。\n#   · 有散装 .lsb → 输出目录得到整套可直接运行的汉化包。\n#   · 文本打包在 exe/dat → 输出散装 .lsb 补丁，覆盖回游戏目录即可（LiveMaker 优先加载散装 .lsb，无需重打包 exe）。\n# 若未安装：点上方「安装 pylivemaker」。此后端不使用下方 SExtractor 规则。")
        elif eng:
            self.rule.insert(tk.END,self.bridge.meta(eng).get("sample",""))
    def refresh(self):
        self.bridge.reload()
        vals=["选择引擎", "Unity（深度提取）", "LiveMaker（LSB脚本）"]+[x[0] for x in self.bridge.list_engines()]
        self.ec["values"]=vals; self.engine.set(vals[0]); self.load_rule(); self.msg("已刷新 %d 个引擎" % (len(vals)-1))
    def selected(self):
        if self.engine.get()=="选择引擎": return ""
        if self.engine.get()=="Unity（深度提取）": return "Unity"
        if self.engine.get()=="LiveMaker（LSB脚本）": return "LiveMaker"
        return self.engine.get()
    def show_engines(self):
        lines=[f"SExtractor 原生引擎：{len(self.bridge.list_engines())} 个",""]
        for n,ft,p,rg in self.bridge.list_engines():
            lines.append(f"{n:24} file={ft:4} postfix={p!r} regex={'Y' if rg else 'N'}")
        w=tk.Toplevel(self.win); w.title("SExtractor引擎列表"); w.geometry("760x620")
        t=scrolledtext.ScrolledText(w); t.pack(fill=tk.BOTH,expand=True); t.insert(tk.END,"\n".join(lines)); t.configure(state=tk.DISABLED)
    def extract(self):
        game=self.game.get().strip(); eng=self.selected()
        if not os.path.isdir(game): messagebox.showwarning("提示","请选择游戏目录",parent=self.win); return
        if not eng: messagebox.showwarning("提示","请选择具体引擎",parent=self.win); return
        if eng == "Unity" and self.unity_backend.get() == "AssetRipper" and not self.bridge.unity_backends.assetripper.available():
            messagebox.showwarning("AssetRipper 未配置", "当前选择了 AssetRipper，但尚未找到可执行文件。\n\n请点击“选择 AssetRipper”配置；也可以切换到“自动”继续使用 UnityPy/传统扫描。", parent=self.win)
            return
        def worker():
            try:
                self.msg("开始提取："+eng)
                def prog(i,total,rel,n):
                    self.win.after(0,lambda:self.pb.configure(maximum=max(total,1),value=i))
                    self.win.after(0,lambda:self.msg(f"[{i}/{total}] {rel} -> {n}"))
                sample=self.rule.get("1.0",tk.END).strip()
                # 规则自检：SExtractor 会在每行文本开头追加 <code401>/<name> 这类控制段头部，
                # 因此 _skip/_search 规则必须带 < 才能命中。缺少 < 的规则会静默失效，
                # 导致脚本代码、注释等噪声全部被当成正文抽出。
                if sample and ("_skip" in sample or "_search" in sample) and "<" not in sample:
                    self.win.after(0, lambda: self.msg(
                        "⚠ 规则缺少 < 控制段标记：SExtractor 会给每行文本加 <code401> 之类的头部，"
                        "规则必须写成 ^<code401> 而非 ^code401。当前规则可能完全不生效，"
                        "建议点『刷新』重新载入引擎默认规则。"))
                self.bridge.unity_backend_mode=self.unity_backend.get()
                data,stat=self.bridge.extract(game,eng,sample_override=sample or None,encode_read=self.er.get(),progress=prog,filter_enabled=self.filter_enabled.get(),filter_mode=self.filter_mode.get())
                out=os.path.join(game,"ManualTransFile.json"); _save_translations(out,data)
                self.win.after(0,lambda:self.msg(f"完成：{stat['items']} 条有效文本，{stat['files']} 文件；过滤 {stat.get('filtered',0)} 条（{self.filter_mode.get() if self.filter_enabled.get() else '关闭过滤'}）；后端={stat.get('backend','')}"))
                for w in stat.get('warnings', []):
                    self.win.after(0,lambda w=w:self.msg("⚠ "+w))
                for e in stat["errors"][:20]: self.win.after(0,lambda e=e:self.msg("⚠ "+e))
                self.win.after(0,lambda:messagebox.showinfo("提取完成",f"{stat['items']} 条有效文本\n过滤：{stat.get('filtered',0)} 条\n输出：{out}",parent=self.win))
            except Exception as e:
                error_msg = str(e)
                traceback.print_exc()
                self.win.after(0, lambda error_msg=error_msg: messagebox.showerror("提取失败", error_msg, parent=self.win))
        threading.Thread(target=worker,daemon=True).start()
    def inject(self):
        game=self.game.get().strip(); eng=self.selected(); fp=self.trans.get().strip() or os.path.join(game,"ManualTransFile.json")
        if not os.path.isdir(game): messagebox.showwarning("提示","请选择游戏目录",parent=self.win); return
        if not eng: messagebox.showwarning("提示","请选择具体引擎",parent=self.win); return
        if eng == "Unity" and self.unity_backend.get() == "AssetRipper" and not self.bridge.unity_backends.assetripper.available():
            messagebox.showwarning("AssetRipper 未配置", "当前选择了 AssetRipper，但尚未找到可执行文件。\n\n请先点击“选择 AssetRipper”配置。", parent=self.win)
            return
        if not os.path.isfile(fp): messagebox.showwarning("提示","找不到翻译 JSON",parent=self.win); return
        try: trans=_load_trans(fp)
        except Exception as e: messagebox.showerror("错误",str(e),parent=self.win); return
        if not trans: messagebox.showwarning("提示","没有可用译文",parent=self.win); return
        out=self.out.get().strip() or os.path.join(game,"_mtool_output")
        def worker():
            try:
                self.msg(f"开始写回：{eng}，{len(trans)} 条")
                def prog(i,total,rel,n):
                    self.win.after(0,lambda:self.pb.configure(maximum=max(total,1),value=i))
                    self.win.after(0,lambda:self.msg(f"[{i}/{total}] {rel} -> {n}"))
                sample=self.rule.get("1.0",tk.END).strip()
                self.bridge.unity_backend_mode=self.unity_backend.get()
                stat=self.bridge.inject(game,eng,trans,out_dir=out,sample_override=sample or None,
                                        encode_read=self.er.get(),encode_write=self.ew.get(),progress=prog)
                if stat.get("dll_replaced") or stat.get("dll_skipped"):
                    self.msg(f"DLL 对话回填：{stat.get('dll_replaced',0)} 条；译文超长跳过 {stat.get('dll_skipped',0)} 条")
                    for s in stat.get("dll_skipped_samples", [])[:10]:
                        self.win.after(0,lambda s=s:self.msg("⚠ 译文超长未回填（可缩短译文后重试）: "+s))
                if stat.get("blob_replaced") or stat.get("blob_skipped"):
                    self.msg(f"无定位资源串原位覆写：{stat.get('blob_replaced',0)} 处；译文过长/标识符跳过 {stat.get('blob_skipped',0)} 处")
                    for s in stat.get("blob_skipped_samples", [])[:10]:
                        self.win.after(0,lambda s=s:self.msg("⚠ 资源串译文过长未覆写（可缩短译文后重试）: "+s))
                warns=stat.get("warnings",[])
                for w in warns:
                    self.win.after(0,lambda w=w:self.msg("⚠ "+w))
                tail=("\n\n⚠ "+"\n⚠ ".join(warns)) if warns else ""
                self.win.after(0,lambda tail=tail:messagebox.showinfo("写回完成",f"写回 {stat['replaced']} 处\n（DLL 对话 {stat.get('dll_replaced',0)} 条；无定位资源串原位覆写 {stat.get('blob_replaced',0)} 处）\n输出：{out}\n失败文件：{len(stat['errors'])}"+tail,parent=self.win))
            except Exception as e:
                traceback.print_exc(); self.win.after(0,lambda e=e:messagebox.showerror("写回失败",str(e),parent=self.win))
        threading.Thread(target=worker,daemon=True).start()

def _open():
    global _DIALOG
    try:
        if _DIALOG and _DIALOG.win.winfo_exists():
            _DIALOG.win.lift(); return
    except Exception: pass
    _DIALOG=UniversalDialog(_APP,_get_bridge())

def _get_bridge():
    """懒构造 SExtractorHeadless：其内部 UnityBackendManager→UnityPyBackend 会 import UnityPy
    （约 600ms），只有首次打开本工具时才需要，避免拖慢主程序启动。"""
    global _BRIDGE
    if _BRIDGE is None:
        _BRIDGE = SExtractorHeadless()
        try:
            if _APP is not None:
                _APP._sextractor_bridge = _BRIDGE
        except Exception:
            pass
    return _BRIDGE

def init_plugin(app):
    global _APP
    _APP=app
    try:
        app.tools_menu.add_separator()
        app.tools_menu.add_command(label=PLUGIN_NAME,command=_open)
    except Exception:
        pass
