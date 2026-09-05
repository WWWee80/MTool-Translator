# -*- coding: utf-8 -*-
"""
游戏文本提取插件 - 多引擎文本提取
零外部依赖（可选 SExtractor 适配层扩展引擎），纯标准库实现

支持:
  - Unity (StreamingAssets / Resources / *_Data 核心资产二进制 / 深度扫描)
  - RPG Maker MV/MZ (www/data 事件指令结构解析)
  - Ren'Py (.rpy 明文 / .rpa 归档解包 + unrpyc 反编译)
  - SExtractor 全部引擎（通过同级 sextractor_adapter.py 适配层）
"""

import os
import re
import json
import glob
import threading
import queue
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext

# ==================== 核心提取引擎 ====================


class TextExtractorCore:
    """文本提取核心，纯静态方法，无GUI依赖"""

    @staticmethod
    def detect_engine(game_dir):
        """检测游戏引擎。

        重点：Unity 优先使用强特征，不能被一个普通 package.json 误判成 RPG Maker。
        同时保留手动引擎选择作为最终兜底。
        """
        if not os.path.isdir(game_dir):
            return None, None

        # ---------- Unity：强特征优先 ----------
        # Unity 游戏经常同时存在 package.json（尤其解包/导出的工程或附带文件），
        # 旧逻辑把 package.json 当成 RPG Maker 的充分条件，会直接误判。
        root_names = set()
        try:
            root_names = {n.lower() for n in os.listdir(game_dir)}
        except OSError:
            pass

        unity_data = glob.glob(os.path.join(game_dir, "*_Data"))
        unity_strong = (
            bool(unity_data) or
            "unityplayer.dll" in root_names or
            "gameassembly.dll" in root_names or
            any(n.startswith("unitycrashhandler") for n in root_names) or
            os.path.isdir(os.path.join(game_dir, "MonoBleedingEdge")) or
            os.path.isdir(os.path.join(game_dir, "il2cpp_data")) or
            os.path.isfile(os.path.join(game_dir, "globalgamemanagers")) or
            bool(glob.glob(os.path.join(game_dir, "*_Data", "globalgamemanagers")))
        )
        if unity_strong:
            return "Unity", "unity"

        # 只有 .assets/.bundle/.unity3d 等资产时，也倾向 Unity；
        # 不再用递归扫描所有文件夹作为 RPG Maker 的判断依据。
        unity_assets = glob.glob(os.path.join(game_dir, "**", "*.assets"), recursive=True)[:3]
        unity_bundles = glob.glob(os.path.join(game_dir, "**", "*.unity3d"), recursive=True)[:3]
        if unity_assets or unity_bundles:
            return "Unity", "unity"

        # ---------- RPG Maker MV/MZ：要求 www/data 结构 ----------
        # package.json 单独存在不够，因为 Unity/其他网页组件也可能带 package.json。
        rpg_data = os.path.join(game_dir, "www", "data")
        if (os.path.isdir(rpg_data) or
                bool(glob.glob(os.path.join(rpg_data, "Map*.json")))):
            return "RPG Maker MV/MZ", "rpgmaker"

        # 某些 MV/MZ 打包目录会把 data 放在 www 下，但目录检查不足时再看 package.json
        # 只有 package.json + data/Map*.json 这类组合才认为是 RPG Maker。
        www_dir = os.path.join(game_dir, "www")
        if (os.path.isfile(os.path.join(game_dir, "package.json")) and
                os.path.isdir(www_dir) and
                (os.path.isfile(os.path.join(www_dir, "data", "System.json")) or
                 bool(glob.glob(os.path.join(www_dir, "data", "Map*.json"))))):
            return "RPG Maker MV/MZ", "rpgmaker"

        return None, None

    @staticmethod
    def is_valid_text(text, min_len=2):
        """判断文本是否值得提取"""
        if not text or not isinstance(text, str):
            return False
        text = text.strip()
        if not text or len(text) < min_len:
            return False

        # 硬控制字符 = 二进制垃圾（\x00-\x08, \x0b\x0c, \x0e-\x1f, \x7f）
        for c in text:
            o = ord(c)
            if o < 0x09 or (0x0b <= o <= 0x0c) or (0x0e <= o <= 0x1f) or o == 0x7f:
                return False

        # 短中文乱码（从提取时就过滤）：CJK/假名 ≤2 且长度≤20，且（含 ASCII 字母数字 且 含半角符号）
        # = 二进制解码乱码。覆盖 "cZ0絶~" / "丄p" / "}:z乆" / "Ӑ郜od" / "}L愺iV⯘G" 等
        # 注意：纯日文+全角标点（"はい。" "あっ…" "なに？"）是真实短对话，必须保留；
        #       全角标点（。？！…）不在乱码特征内——乱码核心是 中日文与ASCII字母数字混排
        if len(re.findall(r'[\u4e00-\u9fff\u3040-\u30ff]', text)) <= 2 and len(text) <= 20 and \
           (re.search(r'[A-Za-z0-9]', text) and
            re.search(r'[?@#$%^&*()\[\]{}<>~|;,_:\.`\\+\-/]', text)):
            return False

        # 含换行/制表符的短文本多为二进制误判（真实多行字典文本通常较长）
        # 但日文对话常为多行短文本（如 "えっ？\r\nあっ、あっ……。" Unity TextMesh 两行），
        # 必须保留——只对非中日韩文本生效, 避免误杀真实多行对话
        if ('\t' in text or '\n' in text or '\r' in text) and len(text) < 15 \
                and not re.search(r'[\u3040-\u30ff\u4e00-\u9fff]', text):
            return False

        # 单个字符占比过高 = 二进制重复字节误判（如 "ccccccc...d\rd"）
        # 仅对非中日韩文本生效，避免误杀 "哈哈哈哈""ふふふ" 等日文重复台词
        if len(text) > 6 and not any('\u3040' <= c <= '\u9fff' or '\uac00' <= c <= '\ud7a3' for c in text):
            max_ratio = max(text.count(c) for c in set(text)) / len(text)
            if max_ratio > 0.55:
                return False

        # 纯 ASCII 随机符号乱码（Unity 二进制可打印段误判，如 "n>3Q44Qb" "535#5!" "'(#'"）：
        # 无 CJK/假名时，字母占比过低 = 随机符号垃圾；长文本还需含连续 3+ 字母段
        if not any(ord(c) > 0x7f for c in text):
            letters = sum(1 for c in text if c.isalpha())
            if letters / len(text) < 0.5:
                return False
            if len(text) >= 6 and not re.search(r'[A-Za-z]{3,}', text):
                return False

        # 乱码字符集检查（确定性, 零误杀）：
        # 日文文本只由 假名/汉字/日文标点/ASCII可打印/常见装饰符号 组成。
        # 一旦出现 谚文/西里尔/拉丁扩展/组合音标/阿拉伯/希伯来 等非日文字符 → 二进制解码乱码, 整条过滤。
        # 覆盖 삺AڊJ / H3tFǨ / 5HRĎW / }L愺iV⯘G 等海量 Unity 二进制可读串（占提取量70%+）
        if re.search(r'[^\u0009\u000a\u000d\u0020-\u007e\u3000-\u303f\u3040-\u30ff\u31f0-\u31ff'
                     r'\uff01-\uff5e\uff65-\uff9f\u4e00-\u9fff\u3400-\u4dbf'
                     r'\u2010-\u2027\u2030-\u205e\u2190-\u21ff\u2500-\u257f\u25a0-\u25ff\u2600-\u26ff\u3007\u00d7\u00f7]',
                text):
            return False

        # Unity 引擎内部资产（确定性, 零误杀）：shader/资源/类名路径 与 输入映射名
        # 均为引擎元数据, 100% 非游戏文本。日文文本不用 半角'/'（用全角）, 故纯ASCII含'/'必为路径
        if re.match(r'^(Hidden|Custom|Legacy|Mobile|Effects|Nature|Particles|Skybox|Standard|Autodesk|Utage|UnityEngine|UI|Water|TransparentFX|Ignore Raycast)/', text):
            return False
        if '.dll' in text or text.startswith('UnityEngine.'):
            return False
        if re.match(r'^(mouse|joystick button|left ctrl|right ctrl|left alt|right alt|left shift|right shift|Mouse X|Mouse Y|Mouse ScrollWheel|keyboard)($|\s)', text):
            return False
        if all(ord(c) < 128 for c in text) and '/' in text:
            return False

        # 过滤路径
        if text.startswith('/') or ':/' in text or ':\\' in text:
            return False

        # 过滤文件扩展名
        if re.match(r'^[\w\-]+\.(png|jpg|jpeg|gif|bmp|webp|mp3|wav|ogg|mp4|avi|json|txt|js|css|html|xml|csv|tsv|log|ini|cfg)$', text, re.I):
            return False

        # 过滤纯数字
        if text.replace('.', '').replace(',', '').replace('-', '').replace(' ', '').isdigit():
            return False

        # 过滤URL
        if text.startswith('http') or '://' in text or text.startswith('www.'):
            return False

        # 过滤邮箱
        if re.match(r'^[\w\.-]+@[\w\.-]+\.\w+$', text):
            return False

        # 过滤HTML/XML标签
        if text.startswith('<') and text.endswith('>') and '<' not in text[1:-1]:
            return False

        # 过滤代码标识符（仅"含下划线或数字"的才像代码变量, 如 my_var_2/renpy_version;
        # 纯英文单词如菜单按钮 Kitchen/Save/Load 是真实文本, 必须保留——否则单英文单词菜单全漏）
        if len(text) < 25 and ' ' not in text and ('_' in text or re.search(r'\d', text)) \
                and re.match(r'^[a-zA-Z0-9_]+$', text):
            return False

        # 过滤代码开头
        code_starts = ['function ', 'var ', 'let ', 'const ', 'if ', 'for ', 'while ',
                      'return ', 'import ', 'export ', 'class ', 'def ', '#include',
                      'using ', 'public ', 'private ', 'protected ']
        if any(text.startswith(s) for s in code_starts):
            return False

        if re.search(r'[=;{}()\[\]]{3,}', text):
            return False

        # 过滤Unity内部路径
        if text.startswith('Assets/') or text.startswith('Packages/'):
            return False

        # 过滤Unity内置资源/签名（二进制扫描误判的固定字符串, 旧提取结果中常见）
        if (text.startswith('Library/') or
                text.startswith('Resources/unity') or
                text.startswith('unity default resources') or
                text.startswith('unity_builtin_extra') or
                text in ('globalgamemanagers.assets', 'globalgamemanagers') or
                text.startswith('public.app-category.') or
                re.match(r'^[A-Za-z0-9_\-\.]+\.assets$', text) or
                text.endswith('.assets.resS') or
                re.match(r'^[A-Za-z0-9_\-\.]+\.resS$', text)):
            return False

        if re.match(r'^[a-f0-9]{8,}$', text, re.I):
            return False

        return True

    @classmethod
    def extract(cls, game_dir, engine_id, min_len=2, skip_translated=True, unity_deep=False, progress_queue=None):
        """主提取入口，返回 {原文: ""} 字典

        Args:
            progress_queue: 可选，queue.Queue 对象，用于报告进度
                发送格式: {"type": "progress", "current": int, "total": int, "file": str}
                          {"type": "file", "file": str}
                          {"type": "done", "count": int}
                          {"type": "error", "msg": str}
        """
        result = {}

        extractors = {
            "unity": cls._extract_unity,
            "rpgmaker": cls._extract_rpgmaker_mv,
        }

        if engine_id in extractors:
            extractors[engine_id](game_dir, result, min_len, skip_translated, unity_deep, progress_queue)

        # 空结果不发 done（worker 会针对空结果报具体错误，避免"成功0条"与错误弹窗重复）
        if progress_queue is not None and result:
            progress_queue.put({"type": "done", "count": len(result)})

        return result

    @classmethod
    def extract_structured(cls, game_dir, engine_id, min_len=2, skip_translated=True,
                           unity_deep=False, progress_queue=None):
        """结构化提取（对齐 SExtractor 三数据源），返回:
        {
          "lines":      {逐行原文: ""},
          "items":      [{name?, message}, ...]   # Ren'Py 对话有 name 配对
          "paragraphs": {合并段落文本: ""}          # 兼容 extract() 返回
        }
        Ren'Py 一次性扫 .rpy/.rpa 出 items(避免 extract() 二次扫描), 段落由 items 派生;
        Unity/RPGMV 无 name 概念, items 退化为 [{message}];
        sextractor:X 直接走适配层 extract_structured。
        """
        # unity / rpgmaker: 无 name 概念, items 退化为 [{message}]
        paragraphs = cls.extract(game_dir, engine_id, min_len, skip_translated,
                                 unity_deep, progress_queue)
        items = [{"message": m} for m in paragraphs]
        return {"lines": dict(paragraphs), "items": items, "paragraphs": paragraphs}

    @classmethod
    def _report_progress(cls, progress_queue, current, total, filepath=""):
        """向进度队列发送进度更新"""
        if progress_queue is not None:
            try:
                progress_queue.put({
                    "type": "progress",
                    "current": current,
                    "total": total,
                    "file": os.path.basename(filepath) if filepath else ""
                }, block=False)
            except:
                pass

    @classmethod
    def _report_file(cls, progress_queue, filepath):
        """报告当前正在处理的文件"""
        if progress_queue is not None:
            try:
                progress_queue.put({
                    "type": "file",
                    "file": os.path.basename(filepath)
                }, block=False)
            except:
                pass

    @staticmethod
    def _decode_bytes(raw, encodings=('utf-8-sig', 'utf-8', 'cp932', 'shift-jis', 'euc-jp', 'gbk')):
        """严格模式尝试解码 bytes，返回(文本, 编码)；全部失败返回(None, None)。
        用 strict 而非 replace——replace 会让非法字节被吞掉、编码探测永远命中第一个，
        导致日文 Shift-JIS 游戏提取出乱码。
        """
        if raw.startswith(b'\xff\xfe'):
            return raw.decode('utf-16-le', 'strict'), 'utf-16-le'
        if raw.startswith(b'\xfe\xff'):
            return raw.decode('utf-16-be', 'strict'), 'utf-16-be'
        for enc in encodings:
            try:
                return raw.decode(enc, 'strict'), enc
            except (UnicodeDecodeError, ValueError, LookupError):
                continue
        return None, None

    @classmethod
    def _read_text(cls, filepath):
        """读取文本文件，自动识别编码。返回文本；异常返回 None"""
        try:
            with open(filepath, 'rb') as f:
                raw = f.read()
        except Exception:
            return None
        text, enc = cls._decode_bytes(raw)
        if text is None:
            text = raw.decode('utf-8', 'replace')  # 兜底
        return text

    @classmethod
    def _add(cls, result, text, min_len):
        if cls.is_valid_text(text, min_len):
            result[text] = ""

    @classmethod
    def _extract_rpgmaker_mv(cls, game_dir, result, min_len, skip_translated, unity_deep, progress_queue=None):
        """RPG Maker MV/MZ 结构解析提取：按事件指令码精确提取对话文本。
        借鉴 SExtractor 的 EVENT_COMMAND_CODES 思路——只取玩家可见文本
        （显示文字/说话人/选项/滚动文字/角色名/昵称），注释与脚本代码天然跳过，
        不含文件名、图块、地图信息等编辑器数据。
        """

        # 事件指令码（借鉴官方/SExtractor 方案）
        # 101 显示文字属性(说话人) / 102 显示选项 / 320 更改角色名 / 324 更改昵称
        # 401 显示文字 / 405 显示滚动文字
        # 108/408 注释、355/655 脚本等不提取
        SHOW_CODES = {101, 401, 405}
        CHOICES_CODES = {102}
        NAME_CODES = {320, 324}

        def walk(node):
            if isinstance(node, dict):
                if "code" in node and "parameters" in node:
                    code = node.get("code")
                    params = node.get("parameters") or []
                    if code in SHOW_CODES and params and isinstance(params[0], str):
                        cls._add(result, params[0], min_len)
                    elif code in CHOICES_CODES and params and isinstance(params[0], list):
                        for c in params[0]:
                            if isinstance(c, str):
                                cls._add(result, c, min_len)
                    elif code in NAME_CODES and params and isinstance(params[0], str):
                        cls._add(result, params[0], min_len)
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)

        # 数据目录：www/data（MV/MZ 标准），退化为游戏根目录
        data_dirs = []
        www_data = os.path.join(game_dir, "www", "data")
        if os.path.isdir(www_data):
            data_dirs.append(www_data)
        else:
            data_dirs.append(game_dir)

        # 事件类文件：按事件指令码抽（脚本 code355/655、注释 code108/408 天然不在
        # SHOW/CHOICES/NAME 集合里，因此不会抽出 JS 代码与注释）
        event_files = []
        # 数据库文件：没有事件指令结构，必须按字段抽（角色名/技能名/物品名/敌人名…
        # 这些玩家天天可见，漏掉会导致 UI 上仍是日文）
        db_files = []
        # Animations.json 的 name 是作者给特效起的名字，恰好常与技能名同名，
        # 可作为技能名翻译的对照；其中的音效/SE 资源名(Slash6/Damage8 等)是纯 ASCII，
        # 会被 is_valid_text 与噪声规则滤掉，不会污染结果。
        DB_NAMES = ("Actors.json", "Classes.json", "Skills.json", "Items.json",
                    "Weapons.json", "Armors.json", "Enemies.json", "States.json",
                    "MapInfos.json", "System.json", "Tilesets.json", "Animations.json")
        for d in data_dirs:
            if not os.path.isdir(d):
                continue
            event_files.extend(sorted(glob.glob(os.path.join(d, "Map*.json"))))
            for name in ("CommonEvents.json", "Troops.json"):
                p = os.path.join(d, name)
                if os.path.isfile(p):
                    event_files.append(p)
            for name in DB_NAMES:
                p = os.path.join(d, name)
                if os.path.isfile(p):
                    db_files.append(p)

        # 数据库字段白名单：只取面向玩家的文本，不碰 iconIndex/animationId 等数值与资源名
        DB_FIELDS = ("name", "description", "nickname", "profile",
                     "message1", "message2", "message3", "message4")

        def walk_db(node):
            if isinstance(node, dict):
                for k, v in node.items():
                    if k in DB_FIELDS and isinstance(v, str):
                        cls._add(result, v, min_len)
                    else:
                        walk_db(v)
            elif isinstance(node, list):
                for v in node:
                    walk_db(v)

        files = event_files + db_files
        db_set = set(db_files)
        total = len(files)
        for i, fp in enumerate(files):
            cls._report_file(progress_queue, fp)
            cls._report_progress(progress_queue, i, total, fp)
            try:
                with open(fp, "r", encoding="utf-8-sig") as f:
                    data = json.load(f)
                if fp in db_set:
                    walk_db(data)
                else:
                    walk(data)
            except Exception:
                continue

    @classmethod
    def _extract_unity(cls, game_dir, result, min_len, skip_translated, unity_deep, progress_queue=None):
        all_files = []
        seen = set()

        def add_file(f):
            if not f:
                return
            key = os.path.normcase(os.path.abspath(f))
            if key not in seen:
                seen.add(key)
                all_files.append(f)

        # StreamingAssets
        streaming = os.path.join(game_dir, "StreamingAssets")
        if os.path.exists(streaming):
            for ext in ["*.txt", "*.json", "*.csv", "*.tsv", "*.xml"]:
                for f in glob.glob(os.path.join(streaming, "**", ext), recursive=True):
                    add_file(f)

        # Resources
        for res_dir in glob.glob(os.path.join(game_dir, "**", "Resources"), recursive=True):
            if os.path.isdir(res_dir):
                for ext in ["*.txt", "*.json", "*.csv", "*.tsv", "*.bytes", "*.asset"]:
                    for f in glob.glob(os.path.join(res_dir, "**", ext), recursive=True):
                        add_file(f)

        # 默认扫描 *_Data 根目录的核心资产二进制：
        # globalgamemanagers / level* / sharedassets* / *.assets —— 文本高度集中且体积小，扫描快
        # 数百MB 的 .resS/.resource 大文件留给「Unity深度扫描」
        for data_dir in glob.glob(os.path.join(game_dir, "*_Data")):
            if not os.path.isdir(data_dir):
                continue
            try:
                names = os.listdir(data_dir)
            except Exception:
                continue
            for name in names:
                full = os.path.join(data_dir, name)
                if not os.path.isfile(full):
                    continue
                low = name.lower()
                if (low == 'globalgamemanagers' or
                        low.startswith('globalgamemanagers.assets') or
                        low.startswith('sharedassets') or
                        low.startswith('level') or
                        low.endswith('.assets')):
                    # 跳过超大资源文件（resS/resource 由深度扫描处理）
                    try:
                        if os.path.getsize(full) > 50 * 1024 * 1024:
                            continue
                    except Exception:
                        continue
                    add_file(full)

        # 深度扫描
        if unity_deep:
            for ext in ["*.txt", "*.json", "*.csv", "*.tsv", "*.xml"]:
                files = glob.glob(os.path.join(game_dir, "**", ext), recursive=True)
                files = [f for f in files if not any(x in f.lower() for x in ["output_log", "crash", "debug", "settings", "config", "save"])]
                for f in files:
                    add_file(f)

            # 二进制字符串（.resS/.resource 等；多为音频/纹理数据，超大文件跳过避免卡死）
            binary_exts = ["*.resS", "*.resource", "*.sharedAssets"]
            for ext in binary_exts:
                for f in glob.glob(os.path.join(game_dir, "**", ext), recursive=True):
                    try:
                        if os.path.getsize(f) > 50 * 1024 * 1024:
                            continue  # 数百MB的音频/纹理资源里基本没有文本
                    except Exception:
                        continue
                    add_file(f)

        total = len(all_files)
        for idx, filepath in enumerate(all_files):
            cls._report_progress(progress_queue, idx + 1, total, filepath)
            ext = os.path.splitext(filepath)[1].lower()
            low_base = os.path.basename(filepath).lower()

            # level0/globalgamemanagers 等无扩展名的 Unity 核心文件是二进制（序列化数据），按二进制扫描
            is_unity_bin = (ext in ['.assets', '.ress', '.resource', '.level', '.sharedassets'] or
                            low_base == 'globalgamemanagers' or
                            low_base.startswith('globalgamemanagers.assets') or
                            low_base.startswith('level'))

            if is_unity_bin:
                try:
                    with open(filepath, 'rb') as f:
                        data = f.read()
                    cls._extract_binary_strings(data, result, min_len)
                except:
                    pass
            else:
                cls._extract_text_file(filepath, result, min_len)

    @classmethod
    def _extract_text_file(cls, filepath, result, min_len):
        try:
            with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
                content = f.read()
            ext = os.path.splitext(filepath)[1].lower()
            if ext == '.json':
                try:
                    cls._extract_recursive(json.loads(content), result, min_len)
                except:
                    for line in content.splitlines():
                        cls._add(result, line, min_len)
            elif ext in ['.csv', '.tsv']:
                delim = '\t' if ext == '.tsv' else ','
                for line in content.splitlines():
                    for part in line.split(delim):
                        cls._add(result, part.strip().strip('"').strip("'"), min_len)
            else:
                for line in content.splitlines():
                    cls._add(result, line.strip(), min_len)
        except:
            pass

    @classmethod
    def _extract_binary_strings(cls, data, result, min_len):
        min_str_len = max(min_len, 3)
        i = 0
        n = len(data)
        while i < n:
            # Unity 序列化字符串: [4字节LE长度前缀][UTF-8文本]（后面紧跟其他二进制字段）
            # 用长度前缀精确切分，避免把后续字段拼进文本
            if i + 4 <= n:
                ln = data[i] | (data[i+1] << 8) | (data[i+2] << 16) | (data[i+3] << 24)
                if 2 <= ln <= 2000 and i + 4 + ln <= n:
                    chunk = bytes(data[i+4:i+4+ln])
                    if b'\x00' not in chunk:  # Unity 字符串纯UTF-8，无内嵌null
                        try:
                            s = chunk.decode('utf-8')
                            printable = sum(1 for c in s
                                            if c.isprintable() or c in '\n\r\t' or ord(c) > 0x7f)
                            if len(s) >= min_len and printable / max(1, len(s)) > 0.9 \
                                    and cls.is_valid_text(s, min_len):
                                cls._add(result, s, min_len)
                                i += 4 + ln
                                continue
                        except (UnicodeDecodeError, ValueError):
                            pass
            if data[i] < 0x20 and data[i] not in (0x09, 0x0a, 0x0d):
                i += 1
                continue
            start = i
            text_bytes = bytearray()
            while i < len(data):
                b = data[i]
                if 0x20 <= b <= 0x7e:
                    text_bytes.append(b); i += 1
                elif b in (0x09, 0x0a, 0x0d):
                    text_bytes.append(b); i += 1
                elif b >= 0xc0:
                    if b >= 0xf0 and i + 3 < len(data):
                        seq = data[i:i+4]
                        if all(0x80 <= seq[j] <= 0xbf for j in range(1, 4)):
                            text_bytes.extend(seq); i += 4; continue
                    elif b >= 0xe0 and i + 2 < len(data):
                        seq = data[i:i+3]
                        if all(0x80 <= seq[j] <= 0xbf for j in range(1, 3)):
                            text_bytes.extend(seq); i += 3; continue
                    elif i + 1 < len(data):
                        seq = data[i:i+2]
                        if 0x80 <= seq[1] <= 0xbf:
                            text_bytes.extend(seq); i += 2; continue
                    i += 1
                else:
                    break
            if len(text_bytes) >= min_str_len * 2:
                try:
                    text = text_bytes.decode('utf-8', errors='ignore').strip()
                    if len(text) >= min_len and cls.is_valid_text(text, min_len):
                        if not text.startswith('Assets/') and not text.startswith('Packages/'):
                            cls._add(result, text, min_len)
                except:
                    pass
            else:
                i = start + 1

    @classmethod
    def _extract_recursive(cls, obj, result, min_len, max_depth=12, depth=0):
        if depth > max_depth:
            return
        if isinstance(obj, str):
            cls._add(result, obj, min_len)
        elif isinstance(obj, list):
            for item in obj:
                cls._extract_recursive(item, result, min_len, max_depth, depth + 1)
        elif isinstance(obj, dict):
            for key, value in obj.items():
                if isinstance(key, str):
                    cls._add(result, key, min_len)
                cls._extract_recursive(value, result, min_len, max_depth, depth + 1)


# ==================== GUI 提取对话框 ====================


class ExtractorDialog:
    """提取对话框 - 支持后台线程 + 进度显示"""

    def __init__(self, parent, app, initial_path=None):
        self.app = app
        self.result = None
        self._worker_thread = None
        self._stop_event = threading.Event()
        self._progress_queue = queue.Queue()
        # 必须早于 _setup_dnd()/_log()：这些会在控件创建前被调用
        self._closed = False
        self._poll_id = None
        self.log_text = None
        self._pending_logs = []

        self.win = tk.Toplevel(parent)
        self.win.title("从游戏提取文本")
        self.win.geometry("680x680")
        self.win.grab_set()
        self.win.resizable(True, True)
        self.win.update_idletasks()
        try:
            self.win.minsize(620, 600)
        except Exception:
            pass

        # 设置拖拽支持
        self._setup_dnd()

        # 如果有初始路径（从主窗口拖入），自动处理
        if initial_path:
            self.win.after(100, lambda: self._handle_dropped_path(initial_path))

        # 游戏目录
        dir_frame = ttk.LabelFrame(self.win, text="游戏目录", padding=8)
        dir_frame.pack(fill=tk.X, padx=12, pady=(12, 6))

        self.dir_var = tk.StringVar()
        ttk.Entry(dir_frame, textvariable=self.dir_var, width=50).pack(side=tk.LEFT, padx=5)
        ttk.Button(dir_frame, text="浏览...", command=self._browse_dir).pack(side=tk.LEFT, padx=3)
        ttk.Button(dir_frame, text="自动检测", command=self._auto_detect).pack(side=tk.LEFT, padx=3)

        # 检测结果显示
        self.detect_var = tk.StringVar(value="未选择目录")
        ttk.Label(dir_frame, textvariable=self.detect_var, foreground="gray", font=("微软雅黑", 8)).pack(anchor=tk.W, padx=5, pady=(4, 0))

        # 引擎选择
        engine_frame = ttk.LabelFrame(self.win, text="引擎类型 (自动检测或手动选择)", padding=8)
        engine_frame.pack(fill=tk.X, padx=12, pady=6)

        self.engine_var = tk.StringVar(value="auto")
        self.engine_var.trace_add("write", self._on_engine_write)

        engines = [
            ("自动检测", "auto"), ("Unity", "unity"),
            ("RPG Maker MV/MZ", "rpgmaker"),
        ]
        for idx, (text, val) in enumerate(engines):
            ttk.Radiobutton(engine_frame, text=text, variable=self.engine_var, value=val).grid(
                row=idx // 3, column=idx % 3, sticky=tk.W, padx=8, pady=2)


        # 选项
        opt_frame = ttk.LabelFrame(self.win, text="选项", padding=8)
        opt_frame.pack(fill=tk.X, padx=12, pady=6)

        self.skip_tl_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(opt_frame, text="跳过已翻译目录 (tl/, translate/)", variable=self.skip_tl_var).grid(row=0, column=0, sticky=tk.W, padx=5)

        self.unity_deep_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(opt_frame, text="Unity深度扫描 (扫描二进制文件，较慢)", variable=self.unity_deep_var).grid(row=0, column=1, sticky=tk.W, padx=5)

        ttk.Label(opt_frame, text="最小长度:").grid(row=1, column=0, sticky=tk.W, padx=5, pady=(6, 0))
        self.min_len_var = tk.IntVar(value=2)
        ttk.Spinbox(opt_frame, from_=1, to=10, textvariable=self.min_len_var, width=6).grid(row=1, column=1, sticky=tk.W, padx=5, pady=(6, 0))

        # 输出
        out_frame = ttk.LabelFrame(self.win, text="输出", padding=8)
        out_frame.pack(fill=tk.X, padx=12, pady=6)

        ttk.Label(out_frame, text="文件名:").grid(row=0, column=0, sticky=tk.W, padx=5)
        self.out_var = tk.StringVar(value="ManualTransFile.json")
        ttk.Entry(out_frame, textvariable=self.out_var, width=45).grid(row=0, column=1, padx=5)
        ttk.Button(out_frame, text="浏览...", command=self._browse_output).grid(row=0, column=2, padx=3)

        # 导出格式（对齐 SExtractor 8 种导出格式；默认 json字典 带换行文本=段落, 兼容 MTool 主流程）
        ttk.Label(out_frame, text="导出格式:").grid(row=1, column=0, sticky=tk.W, padx=5, pady=(6, 0))
        self.format_var = tk.StringVar(value="json_dict_para")
        self.format_combo = ttk.Combobox(
            out_frame, textvariable=self.format_var, state="readonly", width=42,
            values=[
                "json字典 {文本:''}（逐行，SExtractor fmt0）",
                "json字典 {文本:文本}（值复制原文）",
                "json列表 [{name,message}]（GalTransl，带名字）",
                "json字典 {带换行文本:''}（段落，MTool兼容·默认）",
                "json字典 {带换行文本:带换行文本}（段落copy）",
                "txt文档 {文本}（每行一条）",
                "txt文档 [带换行文本]（message列表）",
                "json列表 [带换行文本]",
            ]
        )
        self.format_combo.grid(row=1, column=1, columnspan=2, sticky=tk.W, padx=5, pady=(6, 0))
        self.format_combo.current(3)

        # ===== 进度条区域 =====
        progress_frame = ttk.LabelFrame(self.win, text="提取进度", padding=8)
        progress_frame.pack(fill=tk.X, padx=12, pady=6)

        # 进度条
        self.progress_var = tk.DoubleVar(value=0)
        self.progress_bar = ttk.Progressbar(progress_frame, variable=self.progress_var, maximum=100, mode='determinate', length=500)
        self.progress_bar.pack(fill=tk.X, padx=5, pady=3)

        # 进度文本
        self.progress_text_var = tk.StringVar(value="等待开始...")
        ttk.Label(progress_frame, textvariable=self.progress_text_var, font=("微软雅黑", 9), foreground="#0066cc").pack(anchor=tk.W, padx=5)

        # 当前文件
        self.current_file_var = tk.StringVar(value="")
        ttk.Label(progress_frame, textvariable=self.current_file_var, font=("Consolas", 8), foreground="gray").pack(anchor=tk.W, padx=5)

        # 统计信息
        self.stats_var = tk.StringVar(value="已提取: 0 条")
        ttk.Label(progress_frame, textvariable=self.stats_var, font=("微软雅黑", 8), foreground="#228822").pack(anchor=tk.W, padx=5)

        # 日志
        log_frame = ttk.LabelFrame(self.win, text="提取日志", padding=5)
        log_frame.pack(fill=tk.BOTH, expand=True, padx=12, pady=6)

        self.log_text = scrolledtext.ScrolledText(log_frame, height=4, font=("Consolas", 9), wrap=tk.WORD)
        self.log_text.pack(fill=tk.BOTH, expand=True)
        self.log_text.config(state=tk.DISABLED)

        # 按钮（放大更醒目，确保不会被遮挡）
        btn_frame = ttk.Frame(self.win)
        btn_frame.pack(pady=10)
        self.start_btn = ttk.Button(btn_frame, text="▶ 开始提取并导出", command=self._start, width=20)
        self.start_btn.pack(side=tk.LEFT, padx=8)
        self.cancel_btn = ttk.Button(btn_frame, text="❌ 取消", command=self._cancel, width=12)
        self.cancel_btn.pack(side=tk.LEFT, padx=8)

        # 提示信息
        ttk.Label(self.win, text="选择游戏目录 → 点击「开始提取并导出」，提取结果会保存为JSON文件，可直接用作MTool的输入文件",
                  foreground="#0066cc", font=("微软雅黑", 8)).pack(pady=(0, 8))

        # 启动进度轮询
        self.win.protocol("WM_DELETE_WINDOW", self.destroy)
        self._poll_progress()

    def _alive(self):
        """窗口与日志控件是否仍存在（模态弹窗期间窗口可能已被销毁）"""
        if self._closed:
            return False
        try:
            return bool(self.win.winfo_exists())
        except Exception:
            return False

    def _poll_progress(self):
        """轮询进度队列，更新UI"""
        self._poll_id = None
        if not self._alive():
            return
        try:
            while True:
                msg = self._progress_queue.get_nowait()
                self._handle_progress_msg(msg)
                # 处理 done/error 时会弹模态框并可能销毁窗口，需立即停止
                if not self._alive():
                    return
        except queue.Empty:
            pass
        # 每100ms轮询一次
        try:
            self._poll_id = self.win.after(100, self._poll_progress)
        except Exception:
            self._poll_id = None

    def _handle_progress_msg(self, msg):
        """处理进度消息"""
        msg_type = msg.get("type", "")

        if msg_type == "progress":
            current = msg.get("current", 0)
            total = msg.get("total", 1)
            filename = msg.get("file", "")
            pct = (current / total * 100) if total > 0 else 0
            self.progress_var.set(pct)
            self.progress_text_var.set(f"处理中... {current}/{total} ({pct:.1f}%)")
            if filename:
                self.current_file_var.set(f"当前: {filename}")

        elif msg_type == "file":
            filename = msg.get("file", "")
            if filename:
                self.current_file_var.set(f"当前: {filename}")

        elif msg_type == "stats":
            count = msg.get("count", 0)
            self.stats_var.set(f"已提取: {count} 条")

        elif msg_type == "done":
            count = msg.get("count", 0)
            self.progress_var.set(100)
            self.progress_text_var.set(f"✅ 提取完成！共 {count} 条文本")
            self.current_file_var.set("")
            self.stats_var.set(f"已提取: {count} 条")
            self._on_extraction_done(count)

        elif msg_type == "error":
            self.progress_text_var.set(f"❌ 错误: {msg.get('msg', '')}")
            self._on_extraction_error(msg.get('msg', ''))

        elif msg_type == "cancelled":
            self.progress_text_var.set("⏹ 已取消")
            self.current_file_var.set("")
            self._reset_ui()

    def _setup_dnd(self):
        """设置拖拽支持 - 拖入exe或目录自动处理"""
        try:
            import tkinterdnd2 as _d
            _d.TkinterDnD._require(self.app.root)
            from tkinterdnd2 import DND_FILES
            self.win.drop_target_register(DND_FILES)
            self.win.dnd_bind('<<Drop>>', self._on_drop)
            self._log("💡 提示: 可直接拖拽游戏exe或游戏文件夹到本窗口")
        except ImportError:
            self._log("💡 提示: 安装 tkinterdnd2 后可支持拖拽 (pip install tkinterdnd2)")
        except Exception as e:
            pass

    def _on_drop(self, event):
        """处理拖入事件"""
        path = event.data.strip()
        path = path.strip('{}').strip('"').strip("'")
        if ' ' in path and not os.path.exists(path):
            for p in path.split():
                p = p.strip('{}').strip('"').strip("'")
                if os.path.exists(p):
                    path = p
                    break
        self._handle_dropped_path(path)

    def _handle_dropped_path(self, path):
        """处理拖入的路径（exe或目录）"""
        if not path or not os.path.exists(path):
            self._log(f"⚠️ 无效路径: {path}")
            return

        self._log(f"📥 接收到: {path}")

        if os.path.isfile(path):
            if path.lower().endswith('.exe'):
                game_dir = os.path.dirname(path)
                exe_name = os.path.basename(path)
                self._log(f"🎮 检测到游戏exe: {exe_name}")
                self.dir_var.set(game_dir)
                self._auto_detect()
                game_name = os.path.splitext(exe_name)[0]
                self.out_var.set(f"{game_name}_ManualTransFile.json")
                self._log(f"📄 建议输出: {self.out_var.get()}")
            else:
                self._log(f"⚠️ 请拖入游戏exe文件或游戏文件夹")

        elif os.path.isdir(path):
            self.dir_var.set(path)
            self._log(f"📁 已选择游戏目录")
            self._auto_detect()
            dir_name = os.path.basename(path)
            if dir_name:
                self.out_var.set(f"{dir_name}_ManualTransFile.json")

    def _log(self, msg):
        # _setup_dnd() 在 log_text 创建前就会调用本方法；窗口销毁后也可能被回调触发
        if getattr(self, 'log_text', None) is None or not self._alive():
            self._pending_logs = getattr(self, '_pending_logs', [])
            self._pending_logs.append(msg)
            return
        try:
            # 补发早于控件创建的日志
            pending = getattr(self, '_pending_logs', None)
            if pending:
                self._pending_logs = []
                for m in pending:
                    self.log_text.config(state=tk.NORMAL)
                    self.log_text.insert(tk.END, f"{m}\n")
                    self.log_text.config(state=tk.DISABLED)
            self.log_text.config(state=tk.NORMAL)
            self.log_text.insert(tk.END, f"{msg}\n")
            self.log_text.see(tk.END)
            self.log_text.config(state=tk.DISABLED)
            self.win.update_idletasks()
        except tk.TclError:
            # 控件已随窗口销毁，静默丢弃
            pass

    def _browse_dir(self):
        d = filedialog.askdirectory(title="选择游戏根目录")
        if d:
            self.dir_var.set(d)
            self._auto_detect()

    def _browse_output(self):
        f = filedialog.asksaveasfilename(defaultextension=".json", filetypes=[("JSON", "*.json")])
        if f:
            self.out_var.set(os.path.basename(f))

    def _on_engine_write(self, *args):
        """引擎联动导出格式: Unity→逐行(fmt0, XUnity注入需逐行精确匹配原文),
        RPG Maker→段落(MTool兼容, 写回需要)"""
        try:
            combo = getattr(self, "format_combo", None)
            if combo is None:
                return
            eid = self.engine_var.get()
            vals = list(combo["values"])
            if eid == "unity":
                target = next((v for v in vals if "逐行" in v and "fmt0" in v), vals[0])
            elif eid == "rpgmaker":
                target = next((v for v in vals if "MTool" in v), vals[3] if len(vals) > 3 else vals[0])
            else:
                return
            self.format_var.set(target)
        except Exception:
            pass


    def _auto_detect(self):
        d = self.dir_var.get()
        if not d or not os.path.isdir(d):
            return
        name, eid = TextExtractorCore.detect_engine(d)
        if eid:
            self.engine_var.set(eid)
            self.detect_var.set(f"检测到: {name}")
            self._log(f"自动检测到引擎: {name}")
        else:
            self.detect_var.set("未能识别，请手动选择")

    def _start(self):
        """启动后台提取线程"""
        game_dir = self.dir_var.get()
        if not game_dir or not os.path.isdir(game_dir):
            messagebox.showwarning("提示", "请先选择游戏目录", parent=self.win)
            return

        engine = self.engine_var.get()
        if engine == "auto":
            name, engine = TextExtractorCore.detect_engine(game_dir)
            if not engine:
                messagebox.showwarning("提示", "无法识别引擎，请手动选择", parent=self.win)
                return
            self._log(f"自动检测到: {name}")

        out_name = self.out_var.get()
        out_path = os.path.join(os.path.dirname(game_dir) if os.path.dirname(game_dir) else game_dir, out_name)

        # 重置进度
        self._stop_event.clear()
        self.progress_var.set(0)
        self.progress_text_var.set("准备提取...")
        self.current_file_var.set("")
        self.stats_var.set("已提取: 0 条")

        self._log("=" * 40)
        self._log(f"开始提取 | 引擎: {engine}")
        self._log(f"目录: {game_dir}")

        # 禁用开始按钮，启用取消按钮
        self.start_btn.config(state=tk.DISABLED)
        self.cancel_btn.config(text="⏹ 停止", command=self._cancel)

        # 启动后台线程
        self._worker_thread = threading.Thread(
            target=self._extraction_worker,
            args=(game_dir, engine, out_path),
            daemon=True
        )
        self._worker_thread.start()

    def _extraction_worker(self, game_dir, engine, out_path):
        """后台工作线程 - 执行提取"""
        try:
            structured = TextExtractorCore.extract_structured(
                game_dir, engine,
                min_len=self.min_len_var.get(),
                skip_translated=self.skip_tl_var.get(),
                unity_deep=self.unity_deep_var.get(),
                progress_queue=self._progress_queue
            )

            if self._stop_event.is_set():
                self._progress_queue.put({"type": "cancelled"})
                return

            paragraphs = dict(structured.get("paragraphs") or {})
            if not paragraphs:
                self._progress_queue.put({"type": "error", "msg": "未提取到任何文本，请检查目录或引擎选择"})
                return

            # 保存（Unity 引擎写入特征码，供主程序识别后启用 Unity 专属乱码跳过）
            if engine == 'unity':
                paragraphs['_mtool_meta'] = {"engine": "unity"}

            # 按导出格式序列化（对齐 SExtractor 8 种导出格式）
            self._write_export(structured, paragraphs, out_path)

            count = len(paragraphs) - (1 if '_mtool_meta' in paragraphs else 0)
            self._progress_queue.put({"type": "done", "count": count, "path": out_path})

        except Exception as e:
            self._progress_queue.put({"type": "error", "msg": str(e)})

    def _write_export(self, structured, paragraphs, out_path):
        """按选中的导出格式写文件（对齐 SExtractor 8 种导出格式）。
        structured = {"lines": {逐行:""}, "items": [{name?,message}], "paragraphs": {段落:""}}
        """
        fmt = self.format_var.get()
        lines = structured.get("lines") or {}
        items = structured.get("items") or []
        data = dict(paragraphs)
        meta = data.pop("_mtool_meta", None)
        if not lines:
            lines = data

        if fmt.startswith("json字典 {文本:文本}"):
            # fmt1: json字典 {文本:文本}（值复制原文, 逐行）
            payload = {k: k for k in lines}
        elif fmt.startswith("json列表 [{name,message}]"):
            # fmt10/11: json列表 [{name,message}]（GalTransl 兼容, 带名字）
            payload = []
            for it in items:
                if "name" in it and it.get("name"):
                    payload.append({"name": it["name"], "message": it.get("message", "")})
                else:
                    payload.append({"message": it.get("message", "")})
        elif fmt.startswith("json字典 {带换行文本:带换行文本}"):
            # fmt4: json字典 {带换行文本:带换行文本}（段落copy）
            payload = {k: k for k in data}
        elif fmt.startswith("json字典 {带换行文本"):
            # fmt3: json字典 {带换行文本:''}（段落, MTool 兼容·默认）
            payload = data
        elif fmt.startswith("txt文档 [带换行文本]"):
            # fmt6: txt文档 [带换行文本]（message 列表）
            with open(out_path, "w", encoding="utf-8") as f:
                f.write("\n".join(it.get("message", "") for it in items))
            self._write_meta_sidecar(meta, out_path)
            return
        elif fmt.startswith("json列表 [带换行文本]"):
            # fmt7: json列表 [带换行文本]
            payload = [it.get("message", "") for it in items]
        elif fmt.startswith("txt文档"):
            # fmt5: txt文档 {文本}（每行一条, 逐行）
            with open(out_path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines.keys()))
            self._write_meta_sidecar(meta, out_path)
            return
        elif fmt.startswith("json列表"):
            # 其他 json列表（逐行数组）
            payload = list(lines.keys())
        else:
            # fmt0: json字典 {文本:''}（逐行）
            payload = lines
        # 关键: json 导出也必须写回 _mtool_meta 特征码, 否则主程序无法识别 Unity → 乱码跳过模式不生效
        if isinstance(payload, dict) and meta is not None:
            payload["_mtool_meta"] = meta
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

    @staticmethod
    def _write_meta_sidecar(meta, out_path):
        """txt 类格式不支持附加 meta, 单独写 meta json"""
        if meta is None:
            return
        meta_path = os.path.splitext(out_path)[0] + "_meta.json"
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump({"_mtool_meta": meta}, f, ensure_ascii=False)

    def _on_extraction_done(self, count):
        """提取完成后的UI更新（主线程）"""
        out_path = os.path.join(
            os.path.dirname(self.dir_var.get()) if os.path.dirname(self.dir_var.get()) else self.dir_var.get(),
            self.out_var.get()
        )

        self._log(f"\n✅ 提取完成: {count} 条文本")
        self._log(f"📁 已保存: {out_path}")

        # 自动填入主应用
        if self.app:
            if hasattr(self.app, 'input_file'):
                self.app.input_file.set(os.path.abspath(out_path))
            if hasattr(self.app, 'output_file'):
                d = os.path.dirname(os.path.abspath(out_path))
                self.app.output_file.set(os.path.join(d, "ManualTransFile_translated.json"))
            if hasattr(self.app, 'progress_file'):
                d = os.path.dirname(os.path.abspath(out_path))
                self.app.progress_file.set(os.path.join(d, "trans_progress.json"))
            self._log("📋 已自动填入翻译工具")

        # 恢复UI
        self._reset_ui()

        messagebox.showinfo("完成", f"成功提取 {count} 条文本！\n\n已保存: {out_path}\n\n输入文件已自动填入，可直接开始翻译。", parent=self.win)
        # 走 destroy() 而非 win.destroy()：需同时取消挂起的进度轮询回调
        self.destroy()

    def _on_extraction_error(self, msg):
        """提取错误处理"""
        self._log(f"\n❌ 错误: {msg}")
        self._reset_ui()
        if self._alive():
            messagebox.showerror("错误", f"提取失败:\n{msg}", parent=self.win)

    def _cancel(self):
        """取消/停止提取"""
        if self._worker_thread and self._worker_thread.is_alive():
            self._stop_event.set()
            self.progress_text_var.set("⏹ 正在停止...")
            self._log("⏹ 用户请求停止...")
            # 等待线程结束（最多2秒）
            self._worker_thread.join(timeout=2.0)
            if self._worker_thread.is_alive():
                self._log("⚠️ 线程未能及时停止")
        self._reset_ui()

    def _reset_ui(self):
        """恢复UI状态"""
        self._worker_thread = None
        if not self._alive():
            return
        try:
            self.start_btn.config(state=tk.NORMAL)
            self.cancel_btn.config(text="❌ 取消", command=self._cancel)
        except tk.TclError:
            pass

    def destroy(self):
        """销毁窗口时确保线程停止"""
        if self._closed:
            return
        self._cancel()
        self._closed = True
        # 取消挂起的轮询回调，否则窗口销毁后回调仍会触发 TclError
        if self._poll_id is not None:
            try:
                self.win.after_cancel(self._poll_id)
            except Exception:
                pass
            self._poll_id = None
        try:
            self.win.destroy()
        except tk.TclError:
            pass


# ==================== 主程序集成钩子 ====================


def init_plugin(app):
    """MTool 插件初始化入口 - 在基本设置页添加"从游戏提取"按钮，并支持主窗口拖拽"""

    # ========== 主窗口拖拽支持 ==========
    def setup_main_dnd():
        """设置主窗口拖拽 - 拖入exe自动打开提取对话框"""
        try:
            from tkinterdnd2 import DND_FILES

            def on_main_drop(event):
                path = event.data.strip().strip('{}').strip('"').strip("'")

                if ' ' in path and not os.path.exists(path):
                    for p in path.split():
                        p = p.strip('{}').strip('"').strip("'")
                        if os.path.exists(p):
                            path = p
                            break

                if not os.path.exists(path):
                    # 交给主程序默认拖拽处理（如JSON填入输入路径）
                    if hasattr(app, '_on_drop'):
                        try:
                            app._on_drop(event)
                        except Exception:
                            pass
                    return

                if os.path.isfile(path) and path.lower().endswith('.exe'):
                    ExtractorDialog(app.root, app, initial_path=path)
                elif os.path.isdir(path):
                    ExtractorDialog(app.root, app, initial_path=path)
                elif hasattr(app, '_on_drop'):
                    # 其他文件（如JSON）交给主程序默认拖拽处理，不覆盖主程序填入路径功能
                    try:
                        app._on_drop(event)
                    except Exception:
                        pass

            app.root.drop_target_register(DND_FILES)
            app.root.dnd_bind('<<Drop>>', on_main_drop)

            def register_recursive(widget):
                try:
                    widget.drop_target_register(DND_FILES)
                    widget.dnd_bind('<<Drop>>', on_main_drop)
                except:
                    pass
                for child in widget.winfo_children():
                    register_recursive(child)

            app.root.after(1000, lambda: register_recursive(app.root))

            print("[游戏文本提取器] ✅ 主窗口拖拽已启用（拖入exe或文件夹自动提取）")

        except ImportError:
            print("[游戏文本提取器] 提示: 安装 tkinterdnd2 后支持主窗口拖拽 (pip install tkinterdnd2)")
        except Exception as e:
            print(f"[游戏文本提取器] 主窗口拖拽初始化失败: {e}")

    app.root.after(300, setup_main_dnd)

    # ========== 注入"从游戏提取"按钮 ==========
    def inject_button():
        """在"输入文件"行旁边注入"从游戏提取"按钮"""
        try:
            target_entry = None

            if hasattr(app, 'input_file_entry'):
                target_entry = app.input_file_entry
                print(f"[游戏文本提取器] 找到 input_file_entry: {target_entry}")

            if not target_entry:
                def find_entry(widget, depth=0):
                    if depth > 15:
                        return None
                    if isinstance(widget, ttk.Entry):
                        try:
                            var = widget.cget('textvariable')
                            if var and hasattr(app, 'input_file'):
                                if str(var) == str(app.input_file):
                                    return widget
                        except:
                            pass
                    for child in widget.winfo_children():
                        result = find_entry(child, depth + 1)
                        if result:
                            return result
                    return None

                target_entry = find_entry(app.root)

            if not target_entry:
                file_frame = None
                for widget in app.root.winfo_children():
                    for child in widget.winfo_children():
                        if isinstance(child, ttk.LabelFrame):
                            try:
                                if '文件' in str(child.cget('text')):
                                    file_frame = child
                                    break
                            except:
                                pass
                    if file_frame:
                        break

                if file_frame:
                    for child in file_frame.winfo_children():
                        if isinstance(child, ttk.Label) and '输入' in str(child.cget('text')):
                            info = child.grid_info()
                            row = int(info.get('row', 0))
                            new_btn = ttk.Button(file_frame, text="📁 从游戏提取",
                                                command=lambda: ExtractorDialog(app.root, app))
                            new_btn.grid(row=row, column=3, padx=5, pady=2, sticky=tk.W)
                            print("[游戏文本提取器] ✅ 已注入按钮到文件设置框架")
                            return

            if target_entry:
                parent = target_entry.winfo_parent()
                frame = target_entry.nametowidget(parent) if parent else target_entry.master

                entry_info = target_entry.grid_info()
                row = int(entry_info.get('row', 0))

                existing_cols = []
                for child in frame.winfo_children():
                    info = child.grid_info()
                    if int(info.get('row', -1)) == row:
                        existing_cols.append(int(info.get('column', 0)))

                next_col = max(existing_cols) + 1 if existing_cols else 3

                new_btn = ttk.Button(frame, text="📁 从游戏提取",
                                    command=lambda: ExtractorDialog(app.root, app))
                new_btn.grid(row=row, column=next_col, padx=5, pady=2, sticky=tk.W)
                print(f"[游戏文本提取器] ✅ 按钮已注入到 row={row}, col={next_col}")
                return

            print("[游戏文本提取器] 未找到合适的注入位置，回退到菜单模式")
            _register_to_menu(app)

        except Exception as e:
            print(f"[游戏文本提取器] GUI注入失败: {e}")
            _register_to_menu(app)

    def _register_to_menu(app):
        """回退：注册到工具菜单"""
        try:
            menubar = app.root.nametowidget(app.root.cget('menu'))
            for i in range(menubar.index('end') + 1):
                try:
                    label = menubar.entrycget(i, 'label')
                    if '工具' in label:
                        menu_path = menubar.entrycget(i, 'menu')
                        tools_menu = menubar.nametowidget(menu_path)
                        tools_menu.add_separator()
                        tools_menu.add_command(label="提取游戏文本...",
                                              command=lambda: ExtractorDialog(app.root, app))
                        print("[游戏文本提取器] ✅ 已注册到工具菜单")
                        return
                except:
                    continue
        except Exception as e:
            print(f"[游戏文本提取器] 菜单注册也失败了: {e}")

    app.root.after(500, inject_button)

    return True


# 兼容旧版翻译引擎接口（主程序 _load_plugins 会跳过带 init_plugin 模块内的类，
# 除非类声明 plugin_is_engine=True——此处刻意不声明，避免被误注册为翻译引擎）
class GameTextExtractor:
    name = "游戏文本提取器"

    def __init__(self, **kwargs):
        pass

    def translate(self, text, source_lang, target_lang):
        return text
