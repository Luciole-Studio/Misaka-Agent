"""Isolated GUI server: real routes, real ChatHost IPC, no network model or user home."""
import json
import os
import sys
import tempfile
from pathlib import Path


def main():
    """Set the disposable home before imports and clean it on every exit path."""
    with tempfile.TemporaryDirectory(prefix="misaka-gui-acceptance-") as directory:
        root = Path(directory)
        os.environ["MISAKA_HOME"] = str(root / "home")
        os.environ["MISAKA_OFFLINE"] = "1"

        from misaka.config import current_config
        from misaka.ui.gui.server import Bridge, GUIServer
        from misaka.ui.gui.chats import ChatManager

        projects = [root / "项目甲", root / "项目乙"]
        for project in projects:
            project.mkdir()
            (project / "资料").mkdir()
            (project / "资料" / "中文 笔记.md").write_text("# 浏览器验收\n\n文件预览保留在右侧，主对话可以继续。", encoding="utf-8")
            (project / "example.js").write_text("const answer = 42;", encoding="utf-8")
        profile = Path(current_config()["profiles_root"]) / "10086"
        profile.mkdir(parents=True)
        (profile / "DESCRIBE.md").write_text("验收用 Sister", encoding="utf-8")
        bridge = Bridge(str(projects[0]))
        bridge.projects.add([str(p) for p in projects])
        bridge.chats = ChatManager(str(projects[0]), program=[sys.executable, "-u", "-X", "utf8", str(Path(__file__).with_name("workspace_chat_runner.py"))])
        def no_daemon(*args, **kwargs):
            raise ConnectionError("Isolated fixture")
        bridge.request = no_daemon
        server = GUIServer(("127.0.0.1", 0), bridge)
        print(json.dumps({"url": server.origin + "/#token=" + server.token, "root": str(root)}, ensure_ascii=False), flush=True)
        try:
            server.serve_forever(poll_interval=.3)
        finally:
            bridge.chats.close_all()
            bridge.jobs.close_all()
            server.server_close()


if __name__ == "__main__":
    main()
