# MTool-Translator

[English](README_EN.md) | 简体中文

> 面向游戏本地化的开源批量翻译工具。

MTool-Translator 用于处理游戏、视觉小说等项目的大量 JSON 文本，将 **LLM API、本地模型与翻译服务**统一到一个翻译流程中。

---

## 功能特性

* OpenAI 兼容 API
* Google Gemini
* Ollama / 本地模型
* 多种翻译服务
* 并发翻译
* 断点续译
* 暂停 / 继续
* 上下文参考
* 自动重试
* 费用预估
* 翻译统计
* JSON 批量翻译
* 插件兼容

---

## 游戏本地化

MTool-Translator 主要面向：

* 游戏汉化
* 视觉小说本地化
* RPG / ADV 等大型文本项目
* JSON 游戏文本
* LLM 游戏翻译
* 本地模型翻译

基本工作流：

```text
游戏文本
   ↓
JSON
   ↓
MTool-Translator
   ↓
LLM / 本地模型 / 翻译服务
   ↓
翻译后的 JSON
```

---

## 翻译引擎

支持多种翻译方式：

| 类型                    | 支持 |
| --------------------- | -- |
| OpenAI 兼容 API         | 是  |
| Google Gemini         | 是  |
| Ollama / 本地模型         | 是  |
| Google Translate      | 是  |
| DeepL                 | 是  |
| 百度 / 有道               | 是  |
| Microsoft Azure       | 是  |
| 腾讯 / 阿里               | 是  |
| MyMemory / IBM Watson | 是  |

OpenAI 兼容 API 可以连接不同的兼容服务和模型。

---

## 大规模文本翻译

针对大型游戏文本任务，项目重点解决：

**速度**

通过并发处理提高翻译吞吐量。

**稳定性**

通过断点续译和自动重试降低长时间任务中断的影响。

**上下文**

通过上下文参考帮助 LLM 理解连续剧情和对话。

**成本**

提供费用预估，并持续优化缓存、重复文本处理和 Token 使用效率。

---

## 持续维护

项目持续进行功能开发、Bug 修复和兼容性维护。

当前主要开发方向：

* 翻译缓存优化
* 术语管理与匹配
* 翻译一致性
* 游戏插件兼容
* 翻译性能
* 更多模型与 API 支持

---

## 参与贡献

欢迎：

* 提交 Bug
* 提交 Issue
* 提出功能建议
* 改进翻译引擎兼容性
* 提交 Pull Request

如果项目对你的游戏本地化工作有帮助，欢迎 Star。

---

**MTool-Translator**

*开源 · 游戏本地化 · AI 翻译*
