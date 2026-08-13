"""扩展注册的启动屏资源区——与内置的 [Skills]/[Extensions] 同一渲染路径。

扩展调用 register("MCPs", collapsed_fn, expanded_fn)，文本可以是字符串或**可调用对象**；
可调用的会在每次渲染时求值，所以异步加载的资源（如 MCP server）能先显示"连接中"，
连上后再自动变成真实列表。
"""

SECTIONS: list[dict] = []


def register(name: str, collapsed, expanded=None) -> None:
    for s in SECTIONS:
        if s["name"] == name:
            s.update(collapsed=collapsed, expanded=expanded)
            return
    SECTIONS.append({"name": name, "collapsed": collapsed, "expanded": expanded})


def unregister(name: str) -> None:
    SECTIONS[:] = [s for s in SECTIONS if s["name"] != name]


def resolve(value) -> str:
    if callable(value):
        try:
            return str(value() or "")
        except Exception:  # noqa: BLE001  区块坏了不该毁掉启动屏
            return ""
    return str(value or "")
