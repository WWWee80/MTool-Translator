# MTool-Translator

[简体中文](README.md) | English

> An open-source batch translation tool for game localization.

MTool-Translator is designed to process large amounts of JSON text from games and visual novels, bringing **LLM APIs, local models, and translation services** into a unified translation workflow.

---

## Features

* OpenAI-compatible APIs
* Google Gemini
* Ollama / Local Models
* Multiple translation services
* Concurrent translation
* Resumable translation
* Pause / Resume
* Context-aware translation
* Automatic retry
* Cost estimation
* Translation statistics
* Batch JSON translation
* Plugin compatibility

---

## Game Localization

MTool-Translator is mainly designed for:

* Game localization
* Visual novel localization
* Large RPG / ADV text projects
* JSON game text
* LLM-based game translation
* Local model translation

Basic workflow:

```text
Game Text
   ↓
JSON
   ↓
MTool-Translator
   ↓
LLM / Local Model / Translation Service
   ↓
Translated JSON
```

---

## Translation Engines

Supports multiple translation methods:

| Type                   | Support |
| ---------------------- | ------- |
| OpenAI-compatible APIs | Yes     |
| Google Gemini          | Yes     |
| Ollama / Local Models  | Yes     |
| Google Translate       | Yes     |
| DeepL                  | Yes     |
| Baidu / Youdao         | Yes     |
| Microsoft Azure        | Yes     |
| Tencent / Alibaba      | Yes     |
| MyMemory / IBM Watson  | Yes     |

OpenAI-compatible APIs can be used to connect different compatible services and models.

---

## Large-Scale Translation

MTool-Translator is designed with large game translation projects in mind.

**Speed**

Concurrent processing improves translation throughput.

**Stability**

Resumable translation and automatic retry reduce the impact of interruptions during long-running tasks.

**Context**

Context-aware translation helps LLMs understand continuous dialogue and story content.

**Cost**

Cost estimation is provided, with ongoing work focused on improving caching, duplicate-text handling, and token efficiency.

---

## Maintained Open Source Project

The project is actively developed and maintained, with ongoing improvements to features, bug fixes, and compatibility.

Current development focuses on:

* Translation cache optimization
* Glossary management and matching
* Translation consistency
* Game plugin compatibility
* Translation performance
* Support for additional models and APIs

---

## Contributing

Contributions are welcome:

* Report bugs
* Open issues
* Suggest features
* Improve translation engine compatibility
* Submit pull requests

If MTool-Translator is useful for your localization work, consider giving the project a Star.

---

**MTool-Translator**

*Open Source · Game Localization · AI Translation*
