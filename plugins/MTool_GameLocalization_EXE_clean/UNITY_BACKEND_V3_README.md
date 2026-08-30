# UnityPy 主引擎 + AssetRipper 兜底

自动模式：UnityPy → AssetRipper CLI-compatible → 传统扫描。

## UnityPy
UnityPy 是主后端。结构化读取 SerializedFile / AssetBundle 中的 TextAsset、MonoBehaviour、ScriptableObject 以及名称中带 Localization / StringTable 的对象，并记录 file/path_id/type/field 定位元数据。UnityPy 官方文档支持 `parse_as_dict` / `parse_as_object`、`patch()` 和保存修改后的文件。

本包没有硬塞平台相关 UnityPy 二进制依赖；GUI 提供“安装 UnityPy”，执行当前 Python 环境的 `python -m pip install -U UnityPy`。

## AssetRipper
作为 UnityPy 失败或不可用时的兜底分析后端。它把游戏导出为可分析的 Unity Project，再由 MTool 扫描导出的 YAML/TextAsset 等文本。当前插件支持 CLI-compatible AssetRipper 构建；官方 AssetRipper 项目本身也提供 Unity Project / Primary Content 导出能力。

## 写回边界
UnityPy：对直接可写对象尝试 `patch()` 后保存游戏副本。
AssetRipper：主要用于复杂 Bundle/版本的提取兜底；本插件不会把 AssetRipper 导出项目冒充成“任意原始 Bundle 可原样重建”。对复杂 AssetBundle 的最终回包仍需要专用回包工具。

输出默认到游戏目录 `_mtool_output`，不覆盖源游戏。
