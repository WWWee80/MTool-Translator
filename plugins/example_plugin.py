# 自定义翻译引擎插件示例
# 复制此文件，修改类名和translate方法即可添加自定义引擎
# 插件会自动出现在引擎下拉列表中

class ExampleTranslator:
    name = "示例自定义引擎"
    need_api_key = False
    need_api_id = False
    need_model = False
    need_base_url = False
    need_email = False

    def __init__(self, api_key="", api_id="", model="", base_url="", email="", timeout=60):
        self.api_key = api_key
        self.timeout = timeout

    def translate(self, text, source_lang, target_lang):
        # 在这里实现你的翻译逻辑
        return text
