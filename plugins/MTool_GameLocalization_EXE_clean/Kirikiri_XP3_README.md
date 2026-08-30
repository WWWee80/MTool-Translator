# Kirikiri XP3 一键处理说明

本插件在 SExtractor 原生引擎基础上增加了 Kirikiri XP3 容器处理：

1. 扫描游戏目录中的 `.xp3`。
2. 对标准未加密 XP3 使用内置 Python 后端解包。
3. 以 SExtractor `Krkr_Reg` 引擎提取内部 `.ks/.txt` 等文本。
4. MTool 翻译后，将译文安全写回解包目录。
5. 使用内置 XP3 writer 重新打包，输出到 `_mtool_output`，不会覆盖原游戏。
6. 如果 XP3 含已知/自定义加密，内置后端会停止直接改包；若游戏目录里存在 `krkrxp3.exe`，插件会尝试调用它处理归档。

## 重要限制

- 加密 XP3 是否能直接重包取决于游戏的加密方案。未知方案不会强行改写，避免损坏游戏。
- `Krkr_Reg` 的编码必须与实际脚本匹配。将日文 CP932 脚本直接写入中文字符时，若编码不兼容，建议把“写入编码”改为 UTF-8，并先对游戏做测试副本。
- 本插件始终优先输出到 `_mtool_output`，原始游戏文件保留不动。

## 外部工具

如果游戏为加密 XP3，可在对应游戏目录放置 `krkrxp3.exe`。插件会识别并尝试使用：

`krkrxp3.exe -m extract input.xp3 output_dir`

`krkrxp3.exe -m repack input_dir output.xp3`

若工具/加密方案不匹配，插件会保留原 XP3 到输出目录并在日志中报告失败原因。
