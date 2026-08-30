# MTool Unity 深度提取说明

本版本把 Unity 文本提取分成“专用文本载体 + Unity 序列化字符串 + AssetBundle/二进制原始字符串 + 多编码扫描”多个通道。

覆盖的常见载体：
- *_Data 下的 globalgamemanagers / level / sharedassets / *.assets
- .resS / .resource / .bundle / .unity3d / .dat / .data
- StreamingAssets / Resources
- txt/json/csv/tsv/xml/yaml/yml/ini/cfg/po/loc/lang/bytes/asset/lua/js/ts/html 等文本文件
- UTF-8 / UTF-16 LE/BE / CP932 / Shift-JIS / GBK / Big5
- Unity 常见 4 字节小端长度前缀字符串

过滤：沿用 MTool 的 严格 / 标准 / 激进 三档。二进制裸字符串扫描比专用文本/长度前缀更保守，避免把随机 ASCII 碎片送去翻译。

限制：本模块不是完整 Unity SerializedFile / AssetBundle 解包器。压缩、加密、LZ4/LZMA 资产包中的文本如果没有可直接发现的字符串字节，仍可能需要专门的 Unity 资产解析后端（如 AssetRipper/UABEA/UnityPy 一类工具）。

因此“尽可能不漏文本”的策略是：能安全识别的尽量扫描；不能可靠解析的只报告，不伪造文本。
