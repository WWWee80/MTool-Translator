# -*- coding: utf-8 -*-
"""
audit_lost_entries.py —— 核对“翻译输出 JSON”相对“原始提取 JSON”丢了哪些条目

用途：定位/复核 _save_output 静默删键问题（输出文件比输入小很多、游戏里句子没被翻译）。

用法：
    python audit_lost_entries.py <原始文件.json> <翻译输出.json> [显示条数，默认30]

输出：
    - 条目数对比、丢失总数、丢失率
    - 丢失条目按“命中规则”分组统计（规则从同目录的 MTool-Translator.py 动态提取）
    - 丢失样例（优先展示含假名/汉字的，即最可能是真实游戏文本的）
"""
import json
import os
import re
import sys
import textwrap
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "MTool-Translator.py")

RULE_TEXT = {}


def load_skip_fn(script_path):
    """从 MTool-Translator.py 提取 _should_skip_translation，并给每个 return True 编号。"""
    if not os.path.exists(script_path):
        return None, None
    code = open(script_path, encoding="utf-8").read()
    try:
        i = code.index("    def _should_skip_translation(")
        j = code.index("    def _translate_one(", i)
    except ValueError:
        return None, None
    seg = textwrap.dedent(code[i:j])
    base_line = code[:i].count("\n") + 1

    lines, out, n = seg.split("\n"), [], 0
    for ln in lines:
        m = re.match(r"^(\s*)return True\s*$", ln)
        if m:
            n += 1
            out.append(m.group(1) + "_HITS.append(%d)" % n)
        out.append(ln)
    seg2 = "\n".join(out)

    # 规则号 -> 行号，便于回查到源码
    k = 0
    for idx, ln in enumerate(lines):
        if re.match(r"^\s*return True\s*$", ln):
            k += 1
            RULE_TEXT[k] = base_line + idx

    ns = {"re": re, "_HITS": []}
    exec(seg2, ns)

    class _Fake:
        _unity_skip_mode = False

    _Fake._should_skip_translation = ns["_should_skip_translation"]
    return ns, _Fake


def load_keys(path):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):
        return [k for k in data if k != "_mtool_meta"]
    return list(data)


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 1
    src_path, out_path = sys.argv[1], sys.argv[2]
    show = int(sys.argv[3]) if len(sys.argv) > 3 else 30

    src_keys = load_keys(src_path)
    out_keys = load_keys(out_path)
    out_set = set(out_keys)
    lost = [k for k in src_keys if k not in out_set]

    print("=" * 72)
    print("原始文件  : %s" % src_path)
    print("输出文件  : %s" % out_path)
    print("原始大小  : %s 字节" % os.path.getsize(src_path))
    print("输出大小  : %s 字节" % os.path.getsize(out_path))
    print("-" * 72)
    print("原始条目数: %d" % len(src_keys))
    print("输出条目数: %d" % len(out_keys))
    print("丢失条目数: %d  (丢失率 %.1f%%)"
          % (len(lost), len(lost) * 100.0 / max(1, len(src_keys))))
    print("=" * 72)

    if not lost:
        print("没有丢条目 ✓（输出包含全部原始键）")
        return 0

    ns, Fake = load_skip_fn(SCRIPT)
    group = Counter()
    if ns:
        app = Fake()
        for k in lost:
            ns["_HITS"].clear()
            try:
                app._should_skip_translation(k)
            except Exception:
                pass
            tag = "+".join(map(str, ns["_HITS"])) or "-"
            group[tag] += 1
        print("\n丢失原因分组（规则号 -> 源文件行号）:")
        for tag, cnt in group.most_common():
            lines = " / ".join("行%d" % RULE_TEXT.get(int(t), 0) for t in tag.split("+"))
            print("  规则%-8s %5d 条   %s" % (tag, cnt, lines))
    else:
        print("\n（未找到 MTool-Translator.py，跳过规则归因）")

    jp = [k for k in lost if re.search(r"[\u3040-\u30ff\u4e00-\u9fff]", k)]
    other = [k for k in lost if k not in set(jp)]
    print("\n其中含日文/汉字（最可能是真实游戏文本，被删即游戏内不翻译）: %d 条" % len(jp))
    print("其余（英文/符号/乱码）: %d 条" % len(other))
    print("\n丢失样例（含日文/汉字优先）:")
    for k in (jp + other)[:show]:
        print("   " + repr(k))
    if len(lost) > show:
        print("   ... 共 %d 条" % len(lost))
    return 0


if __name__ == "__main__":
    sys.exit(main())
