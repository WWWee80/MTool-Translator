# -*- coding: utf-8 -*-
"""
内嵌汉化插件(GUI版) - 把译文写回游戏, 游戏本体变中文, 无需MTool常驻
原理: 翻译字典{原文:译文} → 按引擎写回:
  - RPG Maker MV/MZ: 递归替换 www/data/*.json 字符串, 原件备份到 _data_backup 可恢复
  - Unity: 整合 XUnity AutoTranslator 懒人包, 一键部署 BepInEx 框架 + 注入翻译 + 启动
           (winhttp/doorstop 启动注入, 游戏本体除 BepInEx 外零修改)
入口: 工具菜单 → 内嵌汉化(译文写回游戏)
借鉴: LinguaGacha 的写回与字段过滤思路(安全写回/编辑器数据黑名单)
"""
import os
import json
import re
import shutil
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext
PLUGIN_NAME = "内嵌汉化"
BACKUP_DIR = "_data_backup"
SKIP_DIRS = {'_data_backup', '_font_backup', 'save', 'saves', 'savedata',
             'node_modules', '__pycache__', 'thumbnails'}
# ==================== 引擎定义 ====================
ENGINES = [
    ("自动检测", "auto"),
    ("RPG Maker MV/MZ", "rpgmaker"),
    ("Unity (XUnity注入)", "unity"),
]
ENGINE_DESC = {
    "rpgmaker": "RPG Maker MV/MZ (JSON明文数据, 可内嵌)",
    "unity": "Unity (XUnity AutoTranslator 注入: 部署框架+注入翻译+启动)",
}
# ===== 字段级黑名单(借鉴 LinguaGacha rpgmaker-processor) =====
# RPG Maker 的编辑器数据/资源字段, 写回成中文会造成误替换甚至损坏游戏:
# 地图事件名、图块集名、地图信息名、动画名、公共事件名、文件名、MZ 插件命令
_RPG_BLACKLIST_ADDRESS = (
    re.compile(r'/events/\d+/name$', re.I),
    re.compile(r'/Tilesets/\d+/name$', re.I),
    re.compile(r'/MapInfos/\d+/name$', re.I),
    re.compile(r'/Animations/\d+/name$', re.I),
    re.compile(r'/CommonEvents/\d+/name$', re.I),
    re.compile(r'/filename$', re.I),
    re.compile(r'/name$', re.I),
)
_RPG_BLACKLIST_VALUE = (
    re.compile(r'^MZ Plugin Command', re.I),
)
def _is_skip_path(path, value):
    """字段级黑名单判断: 命中则不对该字符串做替换(原文保留)"""
    for pat in _RPG_BLACKLIST_ADDRESS:
        if pat.search(path):
            return True
    for pat in _RPG_BLACKLIST_VALUE:
        if pat.match(value):
            return True
    return False
# ==================== 工具函数 ====================
def _guess_game_root(app):
    inf = app.input_file.get()
    if inf and os.path.exists(inf):
        d = os.path.dirname(os.path.abspath(inf))
        if os.path.basename(d).lower() == 'www':  # RPG MV/MZ
            return os.path.dirname(d)
        return d
    return ""
def _guess_trans_file(app, root):
    for p in (app.output_file.get(), app.input_file.get()):
        if p and os.path.exists(p):
            return p
    if root:
        for rel in ('ManualTransFile.json', os.path.join('www', 'ManualTransFile.json')):
            p = os.path.join(root, rel)
            if os.path.exists(p):
                return p
    return ""
def _detect_engine(root):
    """返回 (引擎id, 描述); 未知返回 ('', 描述)"""
    if not root or not os.path.isdir(root):
        return "", "未知"
    # RPG Maker MV/MZ
    if os.path.isfile(os.path.join(root, 'package.json')) or os.path.isdir(os.path.join(root, 'www')):
        return "rpgmaker", ENGINE_DESC["rpgmaker"]
    # Unity
    if (glob_any(os.path.join(root, '*_Data')) or
            glob_any(os.path.join(root, '**', '*.assets'))[:1]):
        return "unity", ENGINE_DESC["unity"]
    return "", "通用引擎(仅文本存于JSON的引擎可内嵌)"
def glob_any(pattern):
    import glob
    return glob.glob(pattern, recursive=True)
def _load_trans_dict(path, split_lines):
    """读取{原文:译文} → 返回(替换字典, 总条目, 已翻译条目)。
    split_lines=True 时额外按行拆分配对(多行条目原文第i行→译文第i行),
    提高逐行存储引擎(RPG MV)的命中率; 完整条目优先于分行条目。"""
    with open(path, 'r', encoding='utf-8-sig') as f:
        raw = json.load(f)
    full, line_map = {}, {}
    total = done = 0
    for k, v in raw.items():
        if not isinstance(k, str) or not isinstance(v, str):
            continue
        total += 1
        if v != k:
            done += 1
            full[k] = v
            if split_lines and '\n' in k and '\n' in v:
                ls, lt = k.split('\n'), v.split('\n')
                if len(ls) == len(lt):
                    for a, b in zip(ls, lt):
                        if a and b and a != b:
                            line_map.setdefault(a, b)
    trans = dict(line_map)
    trans.update(full)  # 完整条目覆盖分行条目
    return trans, total, done
# ==================== RPG Maker 写回 ====================
def _walk(obj, trans, replace=False, matched=None, key_path=""):
    """递归遍历JSON结构; 只替换字符串值(不动键名)。
    replace=False 仅计数, True 则替换。返回替换/命中次数。
    key_path 为字段路径(如 "Map001/events/0/name"), 用于字段级黑名单过滤。"""
    n = 0
    if isinstance(obj, dict):
        for k in list(obj.keys()):
            v = obj[k]
            path = f"{key_path}/{k}"
            if isinstance(v, str):
                if v in trans and not _is_skip_path(path, v):
                    if matched is not None:
                        matched.add(v)
                    if replace:
                        obj[k] = trans[v]
                    n += 1
            else:
                n += _walk(v, trans, replace, matched, path)
    elif isinstance(obj, list):
        for i in range(len(obj)):
            v = obj[i]
            path = f"{key_path}/{i}"
            if isinstance(v, str):
                if v in trans and not _is_skip_path(path, v):
                    if matched is not None:
                        matched.add(v)
                    if replace:
                        obj[i] = trans[v]
                    n += 1
            else:
                n += _walk(v, trans, replace, matched, path)
    return n
def _scan_json_files(root):
    files = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d.lower() not in SKIP_DIRS and not d.startswith('.')]
        for fn in filenames:
            if fn.lower().endswith('.json'):
                full = os.path.join(dirpath, fn)
                files.append((os.path.relpath(full, root), full))
    files.sort()
    return files
def _source_of(root, rel, full):
    """若已有原始备份则从备份读(支持反复重新嵌入), 否则读当前文件"""
    bak = os.path.join(root, BACKUP_DIR, rel)
    return bak if os.path.exists(bak) else full
def _embed_rpgmaker_file(root, rel, full, trans):
    """嵌入单个 RPG Maker 数据文件: 从原始来源读取 → 替换 → 备份原件(仅首次) → 写回。返回替换次数"""
    src = _source_of(root, rel, full)
    with open(src, 'r', encoding='utf-8-sig') as f:
        data = json.load(f)
    n = _walk(data, trans, replace=True)
    if n == 0:
        return 0
    bak = os.path.join(root, BACKUP_DIR, rel)
    if not os.path.exists(bak):
        os.makedirs(os.path.dirname(bak), exist_ok=True)
        shutil.copy2(full, bak)
    with open(full, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, separators=(',', ':'))
    return n
def _restore_rpgmaker(root):
    br, n = os.path.join(root, BACKUP_DIR), 0
    if not os.path.isdir(br):
        return 0
    for dirpath, _, files in os.walk(br):
        for fn in files:
            rel = os.path.relpath(os.path.join(dirpath, fn), br)
            try:
                shutil.copy2(os.path.join(br, rel), os.path.join(root, rel))
                n += 1
            except Exception:
                pass
    return n
# ==================== Unity 写回 (XUnity 注入) ====================
# 整合自「XUnity懒人包」: 把 BepInEx + XUnity.AutoTranslator 框架部署到游戏目录
# (winhttp/doorstop 启动注入), 再把 MTool 翻译 JSON 转成
# BepInEx/Translation/zh/Text/_ManualTranslations.txt, 启动游戏即内嵌汉化,
# 游戏本体除 BepInEx 目录外零修改。删除 BepInEx + winhttp.dll + doorstop_config.ini 即卸载。
if getattr(sys, "frozen", False):
    _XU_BASE = os.path.dirname(sys.executable)
else:
    _XU_BASE = os.path.dirname(os.path.abspath(__file__))
# 模板目录多候选查找：exe目录 -> 插件文件目录 -> 插件目录上一级（主脚本目录）
_XU_CANDIDATES = [_XU_BASE,
                  os.path.dirname(os.path.abspath(__file__)),
                  os.path.dirname(os.path.dirname(os.path.abspath(__file__)))]
XUNITY_TEMPLATE_DIR = next(
    (os.path.join(b, "xunity_template") for b in _XU_CANDIDATES
     if os.path.isdir(os.path.join(b, "xunity_template"))),
    os.path.join(_XU_BASE, "xunity_template"))
XUNITY_LANG = "zh"          # 目标语言目录
XUNITY_ROOT_FILES = ["winhttp.dll", "doorstop_config.ini", ".doorstop_version"]
XUNITY_EXCLUDE_EXE = ["unitycrashhandler64", "unitycrashhandler", "install", "unins",
                      "setup", "updater", "crash", "patch and run", "报错"]


def find_game_exe(game_dir):
    """在游戏目录找主可执行文件（排除崩溃处理/安装器等）"""
    if not os.path.isdir(game_dir):
        return None
    for f in sorted(os.listdir(game_dir)):
        if f.lower().endswith(".exe"):
            base = os.path.splitext(f)[0].lower()
            if any(x in base for x in XUNITY_EXCLUDE_EXE):
                continue
            return os.path.join(game_dir, f)
    return None


def has_non_ascii(path):
    """路径是否含非 ASCII（中文/日文等）字符"""
    try:
        path.encode("ascii")
        return False
    except UnicodeEncodeError:
        return True


def ensure_ascii_game_dir(game_dir, log=None):
    """若游戏路径含非 ASCII，自动建英文 junction 兼容入口并返回它。
    不改系统设置、不移动文件——仅在同盘符英文路径建目录连接，
    之后所有部署/注入/启动都走该入口，解决 Mono 在中文/日文路径下的编码崩溃。"""
    def out(msg):
        if log:
            log(msg)
    if not game_dir or not os.path.isdir(game_dir):
        return game_dir
    game_dir = os.path.normpath(game_dir)
    if not has_non_ascii(game_dir):
        return game_dir
    # 候选联接根目录: 同盘符英文路径 -> 本地应用数据（必须纯 ASCII 才有意义）
    candidates = []
    drive = os.path.splitdrive(game_dir)[0]
    if drive:
        candidates.append(os.path.join(drive + os.sep, "XUnityJunctions"))
    local = os.environ.get("LOCALAPPDATA", "")
    if local and not has_non_ascii(local):
        candidates.append(os.path.join(local, "XUnityJunctions"))
    junc_root = None
    for cand in candidates:
        try:
            os.makedirs(cand, exist_ok=True)
            junc_root = cand
            break
        except Exception:
            continue
    if junc_root is None:
        out("[警告] 无法创建英文联接目录，将按原路径继续（中日文路径会导致 XUnity 钩子全部失效）")
        return game_dir
    import hashlib as _hl
    h = _hl.md5(game_dir.encode("utf-8")).hexdigest()[:10]
    base_link = os.path.normpath(os.path.join(junc_root, "game_" + h))
    link = base_link
    if os.path.isdir(link) or os.path.islink(link):
        try:
            if os.path.realpath(link).lower() == os.path.realpath(game_dir).lower():
                return link  # 已存在且指向一致, 静默复用
        except Exception:
            pass
        link = None
        for i in range(2, 100):
            cand = os.path.normpath(os.path.join(junc_root, "game_" + h + "_" + str(i)))
            if not os.path.exists(cand):
                link = cand
                break
        if link is None:
            out("[警告] 联接入口已占用且无法复用: " + base_link)
            return game_dir
    # 优先 _winapi（无窗口无闪动），失败再用 mklink /J（隐藏窗口）
    created = False
    try:
        import _winapi
        _winapi.CreateJunction(game_dir, link)
        created = os.path.isdir(link)
    except Exception:
        created = False
    if not created:
        try:
            flags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
            r = subprocess.run(["cmd", "/c", "mklink", "/J", link, game_dir],
                               capture_output=True, text=True, creationflags=flags)
            created = os.path.isdir(link)
        except Exception as e:
            out(f"[警告] 创建兼容入口失败: {e}")
            return game_dir
        if not created:
            out("[警告] 创建兼容入口失败，将按原路径继续（中日文路径会导致 XUnity 钩子全部失效）")
            return game_dir
    out(f"[兼容] 检测到中文/日文路径，已自动创建兼容入口: {link}")
    out("       后续部署/注入/启动将使用该入口（原目录文件不动）。")
    return link


def check_installed(game_dir):
    """检查游戏是否已部署 XUnity 框架。返回 (是否已装, 说明)"""
    if not os.path.isdir(game_dir):
        return False, "目录无效"
    has_bep = os.path.isdir(os.path.join(game_dir, "BepInEx", "core"))
    has_at = os.path.isdir(os.path.join(game_dir, "BepInEx", "plugins", "XUnity.AutoTranslator"))
    has_wh = os.path.exists(os.path.join(game_dir, "winhttp.dll"))
    has_ds = os.path.exists(os.path.join(game_dir, "doorstop_config.ini"))
    if has_bep and has_at and has_wh:
        return True, "已安装 XUnity 框架"
    return False, "未安装框架(BepInEx/winhttp 缺失)"


def deploy_framework(game_dir, log=None):
    """部署 XUnity 框架到游戏目录"""
    def out(msg):
        if log:
            log(msg)
    if not os.path.isdir(XUNITY_TEMPLATE_DIR):
        out("[错误] 找不到模板目录: " + XUNITY_TEMPLATE_DIR)
        out("      请确认 xunity_template 文件夹与插件在同一目录")
        return False
    exe = find_game_exe(game_dir)
    if not exe:
        out("[错误] 游戏目录下没找到主 .exe，请确认选择的是游戏根目录")
        return False
    out("开始部署 XUnity 框架...")
    src_be = os.path.join(XUNITY_TEMPLATE_DIR, "BepInEx")
    dst_be = os.path.join(game_dir, "BepInEx")
    try:
        if os.path.isdir(src_be):
            shutil.copytree(src_be, dst_be, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns("LogOutput.log", "cache"))
            out(f"  BepInEx 框架 -> {dst_be}")
    except Exception as e:
        out(f"[错误] 复制 BepInEx 失败: {e}")
        return False
    for rf in XUNITY_ROOT_FILES:
        s = os.path.join(XUNITY_TEMPLATE_DIR, rf)
        d = os.path.join(game_dir, rf)
        if os.path.exists(s):
            try:
                shutil.copy2(s, d)
                out(f"  {rf}")
            except Exception as e:
                out(f"[错误] 复制 {rf} 失败: {e}")
                return False
    out("部署完成！启动游戏即加载 XUnity AutoTranslator。")
    return True


def inject_translation(game_dir, json_path, log=None):
    """把 MTool 翻译 JSON 转成 XUnity 翻译文件并注入"""
    def out(msg):
        if log:
            log(msg)
    if not json_path or not os.path.exists(json_path):
        out("[错误] 请选择 MTool 翻译后的 JSON 文件")
        return False
    if not os.path.isdir(game_dir):
        out("[错误] 请先选择游戏目录")
        return False
    try:
        # utf-8-sig 兼容带 BOM 的 MTool 导出 JSON
        with open(json_path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except Exception as e:
        out(f"[错误] 读取 JSON 失败: {e}")
        return False
    if not isinstance(data, dict):
        out("[错误] JSON 不是 {原文: 译文} 字典（请用 MTool 翻译后的文件）")
        return False
    lines = []
    skip = {"empty": 0, "same": 0, "long": 0, "meta": 0}
    for src, dst in data.items():
        if src.startswith("_mtool_"):
            skip["meta"] += 1
            continue
        if isinstance(dst, dict):
            dst = dst.get("translation") or dst.get("译文") or dst.get("translatedText") or ""
        if not isinstance(dst, str):
            dst = str(dst)
        src = src.replace("\r", "").replace("\n", r"\n").strip()
        dst = dst.replace("\r", "").replace("\n", r"\n").strip()
        if not src or not dst:
            skip["empty"] += 1
            continue
        if src == dst:
            skip["same"] += 1
            continue
        if len(src) > 950 or len(dst) > 950:
            skip["long"] += 1
            continue
        lines.append(f"{src}={dst}")
    text_dir = os.path.join(game_dir, "BepInEx", "Translation", XUNITY_LANG, "Text")
    os.makedirs(text_dir, exist_ok=True)
    out_path = os.path.join(text_dir, "_ManualTranslations.txt")
    with open(out_path, "w", encoding="utf-8-sig") as f:
        f.write("\n".join(lines))
    out(f"注入完成！共 {len(lines)} 条 ->")
    out(f"  {out_path}")
    if any(skip.values()):
        out(f"（跳过: 空 {skip['empty']} | 原文译文相同 {skip['same']} | 超长 {skip['long']} | 元数据 {skip['meta']}）")
    return True


def launch_game(game_dir, log=None):
    def out(msg):
        if log:
            log(msg)
    exe = find_game_exe(game_dir)
    if not exe:
        out("[错误] 找不到游戏主程序")
        return
    try:
        subprocess.Popen([exe], cwd=game_dir)
        out(f"已启动: {os.path.basename(exe)}")
    except Exception as e:
        out(f"[错误] 启动失败: {e}")


class EmbedDialog:
    def __init__(self, app):
        self.app = app
        self.items = {}        # iid -> [rel, full, matches]  (rpgmaker 用)
        self.checked = set()
        self.trans, self.trans_total, self.trans_done = {}, 0, 0
        self.engine_id = ""
        self.top = tk.Toplevel(app.root)
        self.top.title(f"{PLUGIN_NAME} - 译文写回游戏文件(无需MTool常驻)")
        self.top.geometry("780x620")
        self.top.transient(app.root)
        body = ttk.Frame(self.top, padding=10)
        body.pack(fill=tk.BOTH, expand=True)
        # 游戏根目录
        r1 = ttk.Frame(body); r1.pack(fill=tk.X, pady=(0, 3))
        ttk.Label(r1, text="游戏根目录:").pack(side=tk.LEFT)
        self.root_var = tk.StringVar(value=_guess_game_root(app))
        ttk.Entry(r1, textvariable=self.root_var).pack(side=tk.LEFT, padx=5, fill=tk.X, expand=True)
        ttk.Button(r1, text="浏览…", command=self.pick_root).pack(side=tk.LEFT)
        self.eng_var = tk.StringVar(value="引擎: 未检测")
        ttk.Label(body, textvariable=self.eng_var, foreground="#0066cc").pack(anchor=tk.W)
        # 引擎手动选择
        r_eng = ttk.Frame(body); r_eng.pack(fill=tk.X, pady=(2, 3))
        ttk.Label(r_eng, text="引擎:").pack(side=tk.LEFT)
        self.engine_sel = ttk.Combobox(r_eng, values=[t for t, _ in ENGINES],
                                       state="readonly", width=24)
        self.engine_sel.current(0)
        self.engine_sel.pack(side=tk.LEFT, padx=5)
        self.engine_sel.bind("<<ComboboxSelected>>", lambda e: self.refresh_engine_ui())
        ttk.Button(r_eng, text="重新检测", command=self.refresh_engine).pack(side=tk.LEFT, padx=5)
        # 翻译文件
        r2 = ttk.Frame(body); r2.pack(fill=tk.X, pady=(6, 3))
        ttk.Label(r2, text="翻译文件:").pack(side=tk.LEFT)
        self.trans_file_var = tk.StringVar(value=_guess_trans_file(app, self.root_var.get()))
        ttk.Entry(r2, textvariable=self.trans_file_var).pack(side=tk.LEFT, padx=5, fill=tk.X, expand=True)
        ttk.Button(r2, text="浏览…", command=self.pick_trans).pack(side=tk.LEFT)
        ttk.Button(r2, text="加载翻译", command=self.load_trans).pack(side=tk.LEFT, padx=4)
        self.split_lines = tk.BooleanVar(value=True)
        ttk.Checkbutton(body, text="分行匹配(多行条目按行配对, RPG MV/MZ建议开启)",
                        variable=self.split_lines).pack(anchor=tk.W)
        self.dict_var = tk.StringVar(value="（未加载翻译字典）")
        self.dict_label = ttk.Label(body, textvariable=self.dict_var, foreground="gray")
        self.dict_label.pack(anchor=tk.W)
        ttk.Label(body, foreground="gray", wraplength=740, justify=tk.LEFT, text=(
            "RPG Maker: 译文写回 www/data/*.json(字段过滤: 事件名/图块集名/filename/插件命令等编辑器数据不替换), "
            "原件备份 _data_backup 可恢复。\n"
            "Unity: 选择游戏根目录后切换「Unity (XUnity注入)」引擎, 自动部署 XUnity 框架 + 注入翻译 + 启动游戏, "
            "游戏本体除 BepInEx 外零修改。")).pack(anchor=tk.W, pady=(3, 6))
        # 文件列表(RPG Maker 用)
        self.list_frame = ttk.LabelFrame(body, text="游戏数据文件(仅RPG Maker, 有匹配的才列出)", padding=4)
        self.list_frame.pack(fill=tk.BOTH, expand=True)
        self.tv = ttk.Treeview(self.list_frame, columns=("path", "matches", "status"),
                               show="tree headings", selectmode="none")
        self.tv.heading("#0", text="选")
        self.tv.column("#0", width=40, stretch=False, anchor="center")
        self.tv.heading("path", text="游戏数据文件")
        self.tv.column("path", width=430)
        self.tv.heading("matches", text="替换处数")
        self.tv.column("matches", width=90, anchor="center")
        self.tv.heading("status", text="状态")
        self.tv.column("status", width=120, anchor="center")
        sb = ttk.Scrollbar(self.list_frame, orient="vertical", command=self.tv.yview)
        self.tv.configure(yscrollcommand=sb.set)
        self.tv.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.tv.bind("<Button-1>", self.on_click)
        self.r3_frame = ttk.Frame(body); self.r3_frame.pack(fill=tk.X, pady=4)
        ttk.Button(self.r3_frame, text="全选", command=lambda: self.set_all(True)).pack(side=tk.LEFT, padx=3)
        ttk.Button(self.r3_frame, text="全不选", command=lambda: self.set_all(False)).pack(side=tk.LEFT, padx=3)
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(self.r3_frame, textvariable=self.status_var, foreground="gray").pack(side=tk.RIGHT)
        self.progress = ttk.Progressbar(body, mode="indeterminate")
        self.progress.pack(fill=tk.X, pady=4)
        self.r4_frame = ttk.Frame(body); self.r4_frame.pack(fill=tk.X)
        self.btn_scan = ttk.Button(self.r4_frame, text="扫描匹配", command=self.scan)
        self.btn_scan.pack(side=tk.LEFT, padx=3)
        self.btn_embed = ttk.Button(self.r4_frame, text="嵌入选中文件", command=self.embed)
        self.btn_embed.pack(side=tk.LEFT, padx=3)
        self.btn_restore = ttk.Button(self.r4_frame, text="恢复原文件", command=self.restore)
        self.btn_restore.pack(side=tk.LEFT, padx=3)
        ttk.Button(self.r4_frame, text="关闭", command=self.top.destroy).pack(side=tk.RIGHT, padx=3)
        # ---- XUnity 面板 (Unity 注入) ----
        self.xu_frame = ttk.LabelFrame(body, text="Unity - XUnity AutoTranslator 注入 (部署框架 + 注入翻译 + 启动)", padding=6)
        self.xu_log = scrolledtext.ScrolledText(self.xu_frame, height=12, font=("Consolas", 9),
                                                wrap=tk.WORD, bg="#1e1e1e", fg="#d4d4d4")
        self.xu_log.pack(fill=tk.BOTH, expand=True)
        xu_btns = ttk.Frame(self.xu_frame); xu_btns.pack(fill=tk.X, pady=4)
        ttk.Button(xu_btns, text="部署 XUnity 框架", command=self.xu_deploy).pack(side=tk.LEFT, padx=3)
        ttk.Button(xu_btns, text="注入翻译(当前JSON)", command=self.xu_inject).pack(side=tk.LEFT, padx=3)
        ttk.Button(xu_btns, text="▶ 启动游戏", command=self.xu_launch).pack(side=tk.LEFT, padx=3)
        ttk.Button(xu_btns, text="部署+注入+启动", command=self.xu_all).pack(side=tk.LEFT, padx=3)
        self.xu_state_var = tk.StringVar(value="XUnity 面板就绪")
        ttk.Label(xu_btns, textvariable=self.xu_state_var, foreground="#0a7d32",
                  font=("微软雅黑", 9)).pack(side=tk.RIGHT)
        ttk.Label(self.xu_frame, foreground="gray", wraplength=740, justify=tk.LEFT, text=(
            "Unity: 自动把 BepInEx + XUnity AutoTranslator 部署到游戏目录(winhttp/doorstop 注入), "
            "再把翻译 JSON 转为 _ManualTranslations.txt, 启动游戏即内嵌汉化。\n"
            "翻译 JSON 用上方「翻译文件」选择 MTool 翻译后的文件(译文非空才会注入); "
            "游戏内 Alt+0 打开翻译面板; 删除游戏目录 BepInEx + winhttp.dll 即卸载。"
            "游戏路径含中文/日文时自动创建英文入口(junction), 无需移动文件。")).pack(anchor=tk.W, pady=(2, 0))
        # 拖拽支持: 拖入游戏exe/目录/翻译JSON
        self._setup_dnd()
        # 自动加载并扫描
        self.refresh_engine()
        if self.trans_file_var.get() and os.path.exists(self.trans_file_var.get()):
            self.load_trans()
        elif self.root_var.get():
            self.status_var.set("请选择翻译文件(输出文件/ManualTransFile.json)")
    # ---------- 工具 ----------
    def _busy(self, on):
        if on:
            self.progress.start(12)
        else:
            self.progress.stop()
        state = "disabled" if on else "normal"
        for b in (self.btn_scan, self.btn_embed, self.btn_restore):
            b.config(state=state)
    def _log(self, msg):
        try:
            self.app._log(msg)
        except Exception:
            pass
    def refresh_engine(self):
        root = self.root_var.get().strip()
        eid, desc = _detect_engine(root)
        self.engine_id = eid
        self.eng_var.set("引擎: " + (desc or "未识别, 请手动选择"))
        # 同步下拉
        for i, (t, v) in enumerate(ENGINES):
            if v == eid:
                self.engine_sel.current(i)
                break
        self.refresh_engine_ui()
    def refresh_engine_ui(self):
        """按当前引擎调整按钮/列表: RPG Maker 显示文件列表, Unity 显示 XUnity 面板"""
        eid = self.selected_engine()
        is_rpg = (eid == "rpgmaker")
        is_unity = (eid == "unity")
        if is_rpg:
            self.list_frame.pack(fill=tk.BOTH, expand=True)
            self.r3_frame.pack(fill=tk.X, pady=4)
            self.progress.pack(fill=tk.X, pady=4)
            self.r4_frame.pack(fill=tk.X)
            self.xu_frame.pack_forget()
            self.btn_scan.config(text="扫描匹配")
            self.btn_embed.config(text="嵌入选中文件")
            self.btn_restore.config(text="恢复原文件")
        elif is_unity:
            self.list_frame.pack_forget()
            self.r3_frame.pack_forget()
            self.progress.pack_forget()
            self.r4_frame.pack_forget()
            self.xu_frame.pack(fill=tk.BOTH, expand=True)
            self.xu_update_state()
        else:
            self.list_frame.pack(fill=tk.BOTH, expand=True)
            self.r3_frame.pack(fill=tk.X, pady=4)
            self.progress.pack(fill=tk.X, pady=4)
            self.r4_frame.pack(fill=tk.X)
            self.xu_frame.pack_forget()
            self.btn_scan.config(text="扫描匹配")
            self.btn_embed.config(text="嵌入选中文件")
            self.btn_restore.config(text="恢复原文件")
    def selected_engine(self):
        t = self.engine_sel.get()
        for name, v in ENGINES:
            if name == t:
                return v
        return "auto"
    # ---------- 交互 ----------
    def pick_root(self):
        d = filedialog.askdirectory(parent=self.top)
        if d:
            self.root_var.set(os.path.normpath(d))
            self.refresh_engine()
            if self.engine_id == "rpgmaker" and self.trans:
                self.scan()
    def pick_trans(self):
        f = filedialog.askopenfilename(parent=self.top,
            filetypes=[("JSON文件", "*.json"), ("所有文件", "*.*")])
        if f:
            self.trans_file_var.set(f)
            self.load_trans()
    def load_trans(self):
        p = self.trans_file_var.get().strip()
        if not p or not os.path.exists(p):
            messagebox.showwarning("提示", "翻译文件不存在", parent=self.top)
            return
        try:
            self.trans, self.trans_total, self.trans_done = \
                _load_trans_dict(p, self.split_lines.get())
        except Exception as e:
            messagebox.showerror("错误", f"读取翻译文件失败:\n{e}", parent=self.top)
            return
        if not self.trans:
            self.dict_var.set("字典为空: 文件里没有已翻译条目(值≠原文)的项")
            self.dict_label.config(foreground="#d32f2f")
            return
        self.dict_var.set(f"字典已加载: 已翻译 {self.trans_done}/{self.trans_total} 条, "
                          f"可用替换规则 {len(self.trans)} 条(含分行配对)")
        self.dict_label.config(foreground="#4CAF50")
        self._log(f"[{PLUGIN_NAME}] 加载字典: {self.trans_done} 条译文")
        if self.engine_id == "rpgmaker":
            self.scan()
    # ---------- 列表(RPG Maker) ----------
    def on_click(self, event):
        iid = self.tv.identify_row(event.y)
        if iid:
            self.toggle(iid)
    def toggle(self, iid):
        if iid in self.checked:
            self.checked.discard(iid)
            self.tv.item(iid, text="☐")
        else:
            self.checked.add(iid)
            self.tv.item(iid, text="☑")
    def set_all(self, on):
        for iid in self.items:
            if on:
                self.checked.add(iid); self.tv.item(iid, text="☑")
            else:
                self.checked.discard(iid); self.tv.item(iid, text="☐")
    def _status_of(self, root, rel):
        return "已嵌入(有备份)" if os.path.exists(os.path.join(root, BACKUP_DIR, rel)) else "原始"
    def _refresh_status(self):
        root = self.root_var.get().strip()
        for iid, (rel, _, _) in self.items.items():
            vals = list(self.tv.item(iid, "values"))
            if len(vals) >= 3:
                vals[2] = self._status_of(root, rel)
                self.tv.item(iid, values=vals)
    # ---------- 动作 ----------
    def scan(self):
        eid = self.selected_engine()
        root = self.root_var.get().strip()
        if not root or not os.path.isdir(root):
            messagebox.showwarning("提示", "请先选择游戏根目录", parent=self.top)
            return
        # RPG Maker / auto
        if not self.trans:
            self.status_var.set("请先加载翻译字典")
            return
        self.eng_var.set("引擎: " + (ENGINE_DESC.get(self.engine_id) or "通用引擎"))
        self.status_var.set("正在扫描…")
        import queue as _queue
        self._busy(True)
        trans_abs = os.path.abspath(self.trans_file_var.get())
        _q = _queue.Queue()
        def w():
            rows, matched, errors = [], set(), 0
            for rel, full in _scan_json_files(root):
                if os.path.abspath(full) == trans_abs:
                    continue
                src = _source_of(root, rel, full)
                try:
                    with open(src, 'r', encoding='utf-8-sig') as f:
                        data = json.load(f)
                    n = _walk(data, self.trans, False, matched)
                except Exception:
                    errors += 1
                    continue
                if n > 0:
                    rows.append((rel, full, n))
            _q.put((rows, matched, errors))
        def poll():
            # 工作线程不能直接调 tkinter after(main thread is not in main loop),
            # 用队列把结果传回主线程轮询
            try:
                rows, matched, errors = _q.get_nowait()
            except _queue.Empty:
                self.top.after(60, poll)
                return
            self._busy(False)
            self.tv.delete(*self.tv.get_children())
            self.items.clear(); self.checked.clear()
            for rel, full, n in rows:
                iid = self.tv.insert("", "end", text="☑",
                    values=(rel, n, self._status_of(root, rel)))
                self.items[iid] = [rel, full, n]
                self.checked.add(iid)
            if not rows:
                self.status_var.set("无匹配")
                messagebox.showinfo("未找到匹配", (
                    "游戏JSON数据里没有任何字符串与字典原文完全匹配, 可能原因:\n"
                    "· 不是RPG MV/MZ或文本不在JSON里(见引擎检测结果);\n"
                    "· 翻译文件是MTool多行合并导出 → 确认勾选「分行匹配」后重新加载翻译;\n"
                    "· 选错了游戏目录或翻译文件。"), parent=self.top)
                return
            total_n = sum(n for _, _, n in rows)
            cov = len(matched) * 100 // max(1, len(self.trans))
            self.status_var.set(
                f"命中 {len(rows)} 个文件 / {total_n} 处替换 | "
                f"字典覆盖 {len(matched)}/{len(self.trans)} ({cov}%)"
                + (f" | {errors} 个文件读取失败已跳过" if errors else ""))
            self._log(f"[{PLUGIN_NAME}] 扫描: {len(rows)}文件/{total_n}处, 字典覆盖{cov}%")
        threading.Thread(target=w, daemon=True).start()
        self.top.after(60, poll)
    def embed(self):
        eid = self.selected_engine()
        root = self.root_var.get().strip()
        if not root or not os.path.isdir(root):
            messagebox.showwarning("提示", "游戏目录无效", parent=self.top); return
        if not self.trans:
            messagebox.showwarning("提示", "请先加载翻译字典", parent=self.top); return
        # RPG Maker / auto: 写回选中文件
        sel = [tuple(v) for iid, v in self.items.items() if iid in self.checked]
        if not sel:
            messagebox.showwarning("提示", "请先扫描并勾选要嵌入的文件", parent=self.top); return
        if not messagebox.askyesno("确认",
                f"将译文写入 {len(sel)} 个游戏数据文件(原件备份到 {BACKUP_DIR}), 继续?\n"
                f"嵌入后游戏本体即为中文, 建议关闭MTool运行时翻译再进游戏验证。", parent=self.top):
            return
        import queue as _queue
        self._busy(True)
        self.status_var.set("正在嵌入…")
        _q = _queue.Queue()
        def w():
            total_n, ok_files, failed = 0, 0, []
            for rel, full, _ in sel:
                try:
                    n = _embed_rpgmaker_file(root, rel, full, self.trans)
                    total_n += n
                    if n > 0:
                        ok_files += 1
                except Exception as e:
                    failed.append((rel, str(e)))
            _q.put((total_n, ok_files, failed))
        def poll():
            try:
                total_n, ok_files, failed = _q.get_nowait()
            except _queue.Empty:
                self.top.after(60, poll)
                return
            self._busy(False)
            self._refresh_status()
            msg = (f"嵌入完成: {ok_files} 个文件, 共替换 {total_n} 处文本\n"
                   f"备份目录: {os.path.join(root, BACKUP_DIR)}\n\n"
                   f"现在可以直接启动游戏(无需MTool)验证效果;\n"
                   f"译文更新后再点一次「嵌入选中文件」即可重写。")
            if failed:
                msg += "\n失败:\n" + "\n".join(f"{r}: {e}" for r, e in failed[:10])
            self.status_var.set(f"嵌入完成: {ok_files} 文件 / {total_n} 处")
            self._log(f"[{PLUGIN_NAME}] 嵌入完成: {ok_files}文件/{total_n}处")
            messagebox.showinfo("完成", msg, parent=self.top)
        threading.Thread(target=w, daemon=True).start()
        self.top.after(60, poll)
    def restore(self):
        eid = self.selected_engine()
        root = self.root_var.get().strip()
        if eid != "rpgmaker":
            messagebox.showinfo("提示", "恢复仅适用于 RPG Maker (Unity 为 XUnity 注入, 无备份覆盖)", parent=self.top)
            return
        if not root or not os.path.isdir(root):
            messagebox.showwarning("提示", "请先选择游戏根目录", parent=self.top); return
        if not os.path.isdir(os.path.join(root, BACKUP_DIR)):
            messagebox.showinfo("提示", "没有找到备份目录, 无需恢复", parent=self.top); return
        if not messagebox.askyesno("确认", "把游戏数据文件恢复为嵌入前的原始版本?", parent=self.top):
            return
        import queue as _queue
        self._busy(True)
        self.status_var.set("正在恢复…")
        _q = _queue.Queue()
        def w():
            _q.put(_restore_rpgmaker(root))
        def poll():
            try:
                n = _q.get_nowait()
            except _queue.Empty:
                self.top.after(60, poll)
                return
            self._busy(False)
            self._refresh_status()
            self.status_var.set(f"已恢复 {n} 个原始文件")
            self._log(f"[{PLUGIN_NAME}] 已恢复 {n} 个原始数据文件")
            messagebox.showinfo("恢复", f"已恢复 {n} 个原始数据文件", parent=self.top)
            self.scan()
        threading.Thread(target=w, daemon=True).start()
        self.top.after(60, poll)
    # ---------- XUnity (Unity 注入) ----------
    def _xu_log(self, msg):
        self.xu_log.config(state=tk.NORMAL)
        self.xu_log.insert(tk.END, msg + "\n")
        self.xu_log.see(tk.END)
        self.xu_log.config(state=tk.DISABLED)
        self.top.update_idletasks()
    def _xu_workdir(self):
        d = self.root_var.get().strip()
        if not d or not os.path.isdir(d):
            messagebox.showwarning("提示", "请先选择游戏根目录", parent=self.top)
            return None
        return ensure_ascii_game_dir(d, self._xu_log)
    def xu_update_state(self):
        d = self.root_var.get().strip()
        if not os.path.isdir(d):
            self.xu_state_var.set("未选择游戏目录")
            return
        exe = find_game_exe(d)
        installed, note = check_installed(d)
        self.xu_state_var.set(f"exe: {os.path.basename(exe) if exe else '?'} | {note}")
    def xu_deploy(self):
        d = self._xu_workdir()
        if not d:
            return
        self._xu_log("=" * 40)
        threading.Thread(target=deploy_framework, args=(d, self._xu_log), daemon=True).start()
    def xu_inject(self):
        d = self._xu_workdir()
        if not d:
            return
        jp = self.trans_file_var.get().strip()
        if not jp or not os.path.exists(jp):
            jp = filedialog.askopenfilename(parent=self.top, title="选择 MTool 翻译后的 JSON",
                                            filetypes=[("JSON", "*.json")])
            if not jp:
                return
            self.trans_file_var.set(jp)
        self._xu_log("=" * 40)
        threading.Thread(target=inject_translation, args=(d, jp, self._xu_log), daemon=True).start()
    def xu_launch(self):
        d = self._xu_workdir()
        if not d:
            return
        self._xu_log("=" * 40)
        launch_game(d, self._xu_log)
    def xu_all(self):
        d = self._xu_workdir()
        if not d:
            return
        installed, _ = check_installed(d)
        jp = self.trans_file_var.get().strip()
        if not jp or not os.path.exists(jp):
            jp = filedialog.askopenfilename(parent=self.top, title="选择 MTool 翻译后的 JSON（可取消仅部署）",
                                            filetypes=[("JSON", "*.json")])
            if jp:
                self.trans_file_var.set(jp)
        self._xu_log("=" * 40)
        self._xu_log("一条龙：部署 + 注入 + 启动")
        if not installed:
            deploy_framework(d, self._xu_log)
        else:
            self._xu_log("框架已安装，跳过部署")
        if jp:
            inject_translation(d, jp, self._xu_log)
        launch_game(d, self._xu_log)
    # ---------- 拖拽支持 (拖入 exe/目录/翻译JSON) ----------
    def _setup_dnd(self):
        """注册拖拽目标: 拖入游戏exe自动识别目录+引擎, 拖入目录直接选择, 拖入JSON设为翻译文件"""
        try:
            import tkinterdnd2 as _d
            _d.TkinterDnD._require(self.app.root)
            self.top.drop_target_register(_d.DND_FILES)
            self.top.dnd_bind("<<Drop>>", self._on_drop)
            self.status_var.set("就绪 (可直接拖入游戏exe/目录/翻译JSON)")
        except Exception:
            pass

    def _on_drop(self, event):
        path = (event.data or "").strip()
        path = path.strip("{}").strip('"').strip("'")
        if not path:
            return
        if " " in path and not os.path.exists(path):
            for p in path.split():
                p = p.strip("{}").strip('"').strip("'")
                if os.path.exists(p):
                    path = p
                    break
        self._handle_dropped_path(path)

    def _handle_dropped_path(self, path):
        if not path or not os.path.exists(path):
            self.status_var.set("无效路径: " + str(path))
            return
        low = path.lower()
        if os.path.isfile(path) and low.endswith(".exe"):
            self.root_var.set(os.path.dirname(path))
            self.status_var.set(f"已识别游戏exe: {os.path.basename(path)}")
            self.refresh_engine()
        elif os.path.isdir(path):
            self.root_var.set(path)
            self.status_var.set(f"已选择游戏目录: {os.path.basename(path)}")
            self.refresh_engine()
        elif os.path.isfile(path) and low.endswith(".json"):
            self.trans_file_var.set(path)
            self.status_var.set(f"已选择翻译文件: {os.path.basename(path)}")
        else:
            self.status_var.set("请拖入游戏exe / 游戏目录 / 翻译JSON")


# ==================== 插件入口 ====================
def _open_dialog(app):
    dlg = getattr(app, "_embed_translate_dlg", None)
    try:
        if dlg is not None and dlg.top.winfo_exists():
            dlg.top.lift(); dlg.top.focus_force()
            return
    except Exception:
        pass
    app._embed_translate_dlg = EmbedDialog(app)
def init_plugin(app):
    app.tools_menu.add_separator()
    app.tools_menu.add_command(label="内嵌汉化(译文写回游戏)", command=lambda: _open_dialog(app))
    app._log(f"[{PLUGIN_NAME}] 插件已加载: 工具菜单 → 内嵌汉化(RPG Maker + Unity/XUnity)")
