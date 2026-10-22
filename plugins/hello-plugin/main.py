"""示例插件：演示 register(ctx) 入口与工具注册。"""


def register(ctx):
    """插件入口：注册一个示例工具。"""

    def HelloPlugin(name: str = "world") -> str:
        """示例插件工具：返回问候语。

        Args:
            name: 问候对象名
        """
        from chat_client.tools.base import _xml_response
        return _xml_response("done", f"Hello, {name}! 来自 hello-plugin 插件。")

    ctx.register_tool(HelloPlugin, name_cn="插件问候", risk_level="low", timeout=10)
