"""网络守护进程：御坂们住的地方。

进程模型（herdr 同款）：本进程常驻并独占所有格子（伪终端）；前台面板/命令行
都是瘦客户端，断开只是少一个观察者。协议是一行一条 JSON（请求带 id/method/params）。

卡片格子：`pane.run_card` 会走看板状态机——认领租约→开格子跑交互会话→
盯交卷/超时/退出→转 verifying / 打回 / 判失败。验收照旧由红队路径做，
守护进程只当进程属主，绝不自评验收（宪法⑥）。
"""
import asyncio
import base64
import fcntl
import json
import os
import re
import secrets
import signal
import socket
import struct
import subprocess
import sys
import termios
import time
import unicodedata

import psutil
import pyte

from misaka.config import CFG

# 线上协议版本（herdr 同款严格相等）。教训（2026-08-11）：**服务端行为**变了也必须 +1，
# 不只是方法/事件形状——上次只改行为没升号，旧守护进程带病通过版本闸继续服役。
PROTOCOL = 17   # 17：手敲识别名单改读 ~/.misaka/allies.json（env 旋钮删除，热重载）
RING_CAP = 256 * 1024          # 每格保留的输出尾巴
FRAME_SECONDS = 0.008          # 脏行广播的合帧窗口（~120fps，中间态不外泄）
SCROLLBACK_LINES = 2000        # 每格回看历史行数（herdr 走 ghostty 的 scrollback_limit）
IDLE_QUIET_SECONDS = 1.0       # 屏幕静止这么久＝闲着（光有转圈字符不算数）
CARD_POLL_SECONDS = 5.0        # 卡片格子盯梢间隔
DEFAULT_ROWS, DEFAULT_COLS = 32, 120
CARD_SHELL = [sys.executable, "-m", "misaka", "card-shell"]   # 测试可替换


def _expand(path):
    return os.path.expanduser(path)


# pyte 认不得会把序列漏成可见字符的现代终端家伙什，进屏前拆掉：
# kitty 键盘推/弹/查询、修饰键模式、同步刷新、超链接（格子里无意义）
_UNSUPPORTED = re.compile(
    rb"\x1b\[[<>=?][0-9;]*u"          # kitty 键盘协议
    rb"|\x1b\[>[0-9;]*[mn]"           # XTMODKEYS / modifyOtherKeys
    rb"|\x1b\[\?2026[hl]"             # 同步刷新
    rb"|\x1b\]8;[^\x07\x1b]*(?:\x07|\x1b\\)"   # OSC8 超链接
)
_ALTSCREEN = re.compile(rb"\x1b\[\?(?:1049|1047|47)[hl]")
# 格子里的应用会向"终端"发问询并等应答（pi 的 kitty 协商就靠 DA 哨兵收尾）。
# herdr 靠内嵌 ghostty 仿真器应答；我们在这儿替仿真屏作答，问询不上屏。
_QUERY = re.compile(
    rb"\x1b\[(?P<da1>0?c)"                       # 主设备属性 → 应答 VT220 系
    rb"|\x1b\[>(?P<da2>0?c)"                     # 次设备属性
    rb"|\x1b\[(?P<dsr>6n)"                       # 光标位置
    rb"|\x1b\](?P<osc>1[01]);\?(?:\x07|\x1b\\)"  # OSC 10/11 前景/背景色查询（pi 启动会问）
)


def _answer_queries(pane, data):
    """替仿真屏应答终端问询（herdr 用内嵌 ghostty 干的活）；问询本体不进屏。"""

    def reply(match):
        group = match.lastgroup
        if group == "da1":
            answer = b"\x1b[?62;22c"
        elif group == "da2":
            answer = b"\x1b[>1;10;0c"
        elif group == "dsr":
            answer = (f"\x1b[{pane.screen.cursor.y + 1};{pane.screen.cursor.x + 1}R").encode()
        else:
            # OSC 10/11 前景/背景色：跟随格子的主题变体（亮色终端里全屏应用查到白底）
            light = pane.theme == "light"
            if match.group("osc") == b"11":
                color = b"faf4/f4f4/f6f6" if light else b"1e1e/1e1e/1e1e"
            else:
                color = b"3320/2020/2828" if light else b"e6e6/e6e6/e6e6"
            answer = b"\x1b]" + match.group("osc") + b";rgb:" + color + b"\x07"
        try:
            if pane.fd is not None:
                os.write(pane.fd, answer)
        except OSError:
            pass
        return b""

    return _QUERY.sub(reply, data)
# 尾部可能是被截断的半条转义序列：留到下一笔再喂（最长 64 字节兜底）
_PARTIAL_ESC = re.compile(rb"\x1b(?:\[[0-9;<>=?]*|\][^\x07\x1b]*)?$")

_FG = {"black": 30, "red": 31, "green": 32, "brown": 33, "blue": 34, "magenta": 35,
       "cyan": 36, "white": 37, "brightblack": 90, "brightred": 91, "brightgreen": 92,
       "brightbrown": 93, "brightblue": 94, "brightmagenta": 95, "brightcyan": 96,
       "brightwhite": 97}


def _render_row(screen, row):
    """仿真屏一行 → 带颜色的 ANSI 字符串（属性变化才发 SGR，行尾复位）。"""
    line = screen.buffer[row]
    out, last, skip_stub = [], None, False
    for col in range(screen.columns):
        if skip_stub:      # 宽字符（中日韩等）占两列：pyte 在后一格留占位，跳过别当空格
            skip_stub = False
            continue
        ch = line[col]
        attrs = (ch.fg, ch.bg, ch.bold, ch.reverse, ch.underscore)
        if attrs != last:
            last = attrs
            sgr = ["0"]
            if ch.bold:
                sgr.append("1")
            if ch.underscore:
                sgr.append("4")
            if ch.reverse:
                sgr.append("7")
            if ch.fg in _FG:
                sgr.append(str(_FG[ch.fg]))
            elif len(str(ch.fg)) == 6:      # 十六进制真彩
                try:
                    r, g, b = (int(str(ch.fg)[i:i + 2], 16) for i in (0, 2, 4))
                    sgr.append(f"38;2;{r};{g};{b}")
                except ValueError:
                    pass
            if ch.bg in _FG:
                sgr.append(str(_FG[ch.bg] + 10))
            elif len(str(ch.bg)) == 6:
                try:
                    r, g, b = (int(str(ch.bg)[i:i + 2], 16) for i in (0, 2, 4))
                    sgr.append(f"48;2;{r};{g};{b}")
                except ValueError:
                    pass
            out.append("\x1b[" + ";".join(sgr) + "m")
        data = ch.data or " "
        out.append(data)
        if data and unicodedata.east_asian_width(data[0]) in ("W", "F"):
            skip_stub = True
    out.append("\x1b[0m")
    return "".join(out)


# 转圈帧：只认 braille／几何圆点。**绝不能放 |/-\ 这类 ASCII**——
# 路径里的 / 和随处可见的 - 会让整屏都判成"在转圈"（踩过）。
_SPINNER_CHARS = set("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏⣾⣽⣻⢿⡿⣟⣯⣷◐◓◑◒◴◷◶◵")
_SHELLS = {"sh", "bash", "zsh", "fish", "dash", "ksh", "csh", "tcsh", "-zsh", "-bash"}
# 你在格子的 shell 里手敲起来时，只有名单里那几家算"临时御坂"进名册——否则 vim/htop
# 也会混进去。名单唯一真源 ~/.misaka/allies.json（用户裁定：不设环境变量旋钮）：
# 首次判定落盘种子生成它，此后一切维护都在文件里，改文件即生效（mtime 热重载，
# 不用重启守护进程）。LO 起的协力者不受此限。
ALLY_SEED = ("claude", "codex")
_allies_cache = {"path": None, "mtime": None, "commands": frozenset(ALLY_SEED)}


def ally_commands():
    """手敲识别名单。文件不存在＝落盘种子生成它；能读＝照文件（空数组＝一家不认，尊重）；
    坏 JSON＝按种子行为但**不动用户的文件**（可能正编辑到一半，不许覆盖手笔）。"""
    path = _expand(CFG["allies"])
    try:
        mtime = os.stat(path).st_mtime_ns
    except OSError:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = f"{path}.{secrets.token_hex(4)}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"commands": list(ALLY_SEED)}, f, ensure_ascii=False, indent=1)
            os.replace(tmp, path)
            mtime = os.stat(path).st_mtime_ns
        except OSError:
            return frozenset(ALLY_SEED)   # 落不了盘（只读盘等）：按种子行为，别拦格子
    if _allies_cache["path"] == path and _allies_cache["mtime"] == mtime:
        return _allies_cache["commands"]
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        commands = frozenset(
            c.strip() for c in (data.get("commands") or [])
            if isinstance(c, str) and c.strip())
    except (OSError, ValueError, AttributeError):
        commands = frozenset(ALLY_SEED)
    _allies_cache.update(path=path, mtime=mtime, commands=commands)
    return commands


def _looks_like_command(name):
    """像个命令名吗？纯数字/版本号（"2.1.228"）不是——那是应用自己改的进程名。"""
    return bool(name) and not re.fullmatch(r"[\d.]+", name)


def _foreground(pane):
    """格子里**此刻前台跑的是什么**（进程名＋命令行）。
    LO 靠这个感知"用户在 shell 里手起了个 codex"——不维护任何厂商名单，
    如实报进程名，是不是 agent 由 LO 自己判断（零表设计）。"""
    if not pane.alive() or pane.fd is None:
        return None
    try:
        fg = os.tcgetpgrp(pane.fd)
        if fg <= 0:
            return None
        proc = psutil.Process(fg)
        name = proc.name()
        try:
            argv = proc.cmdline()
        except psutil.Error:
            argv = []
        # 进程名不一定是命令名：claude 这类 Node 应用把它设成版本号（"2.1.228"），
        # 拿去当代号显示就是一串怪数字（用户实测）。命令行第一段才是人认得的名字。
        shown = name
        if argv and not _looks_like_command(name):
            shown = os.path.basename(argv[0])
            if shown in ("node", "python", "python3", "bun", "deno") and len(argv) > 1:
                shown = os.path.basename(argv[1]) or shown   # 解释器＋脚本：取脚本名
        return {"name": shown, "proc_name": name, "pid": fg,
                "cmdline": " ".join(argv)[:200] if argv else name,
                "is_shell": shown.lstrip("-") in _SHELLS or name.lstrip("-") in _SHELLS}
    except (OSError, psutil.Error):
        return None


def _ally_name(pane):
    """这个格子里跑着的**第三方 agent** 叫什么（没有就 None）＝临时御坂的判据。
    两种来源：
    ① LO 用协力者工具起的（pane.ally 有代号）——什么命令都算，是它自己派的
    ② 用户在 misaka 格子的 shell 里手敲起来的——**只认 allies.json 名单里那几家**，
       否则 vim/htop/npm 也会混进御坂名册（名单见 ally_commands()，种子 claude/codex）
    排除：御坂的卡片格子、misaka 自己的 chat/card-shell 会话。"""
    if pane.ally:
        return pane.ally
    if pane.card or not pane.alive():
        return None
    if "misaka" in " ".join(pane.argv or []):   # misaka 自己的会话不是协力者
        return None
    fg = _foreground(pane)
    if not fg or fg["is_shell"]:
        return None
    return fg["name"] if fg["name"] in ally_commands() else None


def _pane_busy(pane):
    """这个格子此刻真的在忙吗？（herdr 的 agent 状态检测同思路，按格子类型分流）
    ① shell 格子：伪终端**前台进程不是 shell 自己** → 有命令在跑
       （herdr foreground_job 同款：只比 pid 不行——`sh -c cmd` 会 exec 掉自己）
    ② agent 格子（引擎等）：屏幕上有转圈帧 → 在思考/输出
       （herdr 的 screen manifest 同款；引擎的 spinner 是 braille，见 tui/loader.py）"""
    if not pane.alive():
        return False
    own = os.path.basename(pane.argv[0]) if pane.argv else ""
    if own.lstrip("-") in _SHELLS:
        try:
            fg = os.tcgetpgrp(pane.fd) if pane.fd is not None else -1
            if fg <= 0:
                return False
            name = psutil.Process(fg).name()
            return bool(name) and name.lstrip("-") not in _SHELLS
        except (OSError, psutil.Error):
            return False
    # agent 格子：转圈帧**且屏幕正在动**才算在跑——只有字符没有变化＝界面装饰，
    # 静止不动的东西不该让指示灯一直闪（用户实测反馈）
    if time.time() - pane.last_output > IDLE_QUIET_SECONDS:
        return False
    try:
        for line in pane.screen.display:
            if any(ch in _SPINNER_CHARS for ch in line):
                return True
    except Exception:  # noqa: BLE001
        pass
    return False


def _busy_reason(pane):
    """诊断：为什么判成在跑/闲着（herdr `agent explain` 同款用途）。"""
    if not pane.alive():
        return "格子已退出"
    own = os.path.basename(pane.argv[0]) if pane.argv else ""
    if own.lstrip("-") in _SHELLS:
        try:
            fg = os.tcgetpgrp(pane.fd) if pane.fd is not None else -1
            name = psutil.Process(fg).name() if fg > 0 else "?"
        except (OSError, psutil.Error):
            return f"shell 格子（{own}）：读不到前台进程 → 判闲"
        if name.lstrip("-") in _SHELLS:
            return f"shell 格子（{own}）：前台是 shell 自己（{name}）→ 闲着等输入"
        return f"shell 格子（{own}）：前台进程是 {name} → 有命令在跑"
    quiet = time.time() - pane.last_output
    if quiet > IDLE_QUIET_SECONDS:
        return f"agent 格子：屏幕已静止 {quiet:.1f}s（>{IDLE_QUIET_SECONDS}s）→ 判闲"
    spins = sorted({ch for line in pane.screen.display for ch in line
                    if ch in _SPINNER_CHARS})
    if spins:
        return f"agent 格子：屏幕在动且有转圈帧 {''.join(spins)} → 在思考/输出"
    return f"agent 格子：屏幕在动（{quiet:.1f}s 前有输出）但无转圈帧 → 判闲"


def _scroll_metrics(pane):
    """herdr ScrollMetrics（pane/terminal.rs:49-53）：回看位置/可回看总量/视口行数。
    pyte 的 HistoryScreen 把历史分 top/bottom 两个 deque：往回滚时行从 top 出、
    进 bottom；所以 offset_from_bottom＝bottom 里的行数。"""
    screen = pane.screen
    top = len(getattr(screen, "history", None).top) if hasattr(screen, "history") else 0
    bottom = len(screen.history.bottom) if hasattr(screen, "history") else 0
    return {"offset_from_bottom": bottom,
            "max_offset_from_bottom": top + bottom,
            "viewport_rows": screen.lines}


def _scroll_pane(pane, delta=0, to=None):
    """按行回看。pyte 的 prev_page/next_page 翻的是 ratio×lines 行，
    我们建屏时把 ratio 设成 1/lines，于是一次＝一行。"""
    screen = pane.screen
    if not hasattr(screen, "history"):
        return
    if to == "bottom":
        while screen.history.bottom:
            screen.next_page()
        return
    step = screen.next_page if delta > 0 else screen.prev_page
    for _ in range(abs(int(delta))):
        before = (len(screen.history.top), len(screen.history.bottom))
        step()
        if (len(screen.history.top), len(screen.history.bottom)) == before:
            break            # 到头了
    screen.dirty.update(range(screen.lines))


class Pane:
    __slots__ = ("id", "title", "argv", "cwd", "card", "claim_lock", "generation",
                 "deadline", "proc", "fd", "buf", "started_at", "exit_code", "submitted",
                 "seen_status", "screen", "stream", "carry", "alt_screen",
                 "last_output", "theme", "ally", "flush", "sent_cursor")

    def __init__(self, pane_id, title, argv, cwd, card=None):
        self.id, self.title, self.argv, self.cwd, self.card = pane_id, title, argv, cwd, card
        self.claim_lock = self.generation = self.deadline = None
        self.proc = self.fd = self.exit_code = None
        self.buf = bytearray()
        self.started_at = int(time.time())
        self.submitted = False
        self.seen_status = None       # 聚焦时看到的看板状态（herdr 的"完了没人看"语义）
        # HistoryScreen＝带回看历史（滚动条要它）；ratio=1/rows 让翻页粒度是「行」
        self.screen = pyte.HistoryScreen(DEFAULT_COLS, DEFAULT_ROWS,
                                         history=SCROLLBACK_LINES, ratio=1 / DEFAULT_ROWS)
        self.stream = pyte.ByteStream(self.screen)
        self.carry = b""              # 跨笔截断的半条转义序列
        self.alt_screen = False       # 备用屏中（全屏应用）＝不显示滚动条
        self.theme = "dark"           # 主题变体（面板 create 时按 env 定，OSC 应答跟随）
        self.ally = None              # 协力者代号（LO 起的第三方 agent 格子才有）
        self.flush = None            # 待发的合帧定时器
        self.sent_cursor = None      # 上次播出去的光标（位置＋显隐）
        self.last_output = 0.0        # 最后一次有输出的时刻（判"屏幕在不在动"）

    def alive(self):
        return self.proc is not None and self.proc.poll() is None


class Daemon:
    def __init__(self, sock_path=None, snapshot_path=None):
        self.sock_path = _expand(sock_path or CFG["net_sock"])
        self.snapshot_path = _expand(snapshot_path or CFG["net_snapshot"])
        self.panes: dict[str, Pane] = {}
        self._seq = 0
        self._theme = "dark"        # 会话主题变体；面板 create 带 MISAKA_THEME 时更新
        self.layout = []            # 标签页布局（每项是一棵分屏树）；守护进程持有
        self._con = None            # board 库连接，第一次跑卡才开
        self._attached: dict[asyncio.StreamWriter, str] = {}   # 订阅流：连接→格子
        self._clients: set[asyncio.StreamWriter] = set()
        self._stopping = asyncio.Event()

    # ── 格子 ──────────────────────────────────────────────

    def _spawn(self, pane: Pane, env=None):
        import pty

        def _become_session_leader():
            # 正规控制终端（tmux/herdr 同款）：没有它，^C/^Z 的信号通路是断的
            os.setsid()
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)

        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ,
                    struct.pack("HHHH", DEFAULT_ROWS, DEFAULT_COLS, 0, 0))
        pane.proc = subprocess.Popen(
            pane.argv, cwd=pane.cwd, stdin=slave, stdout=slave, stderr=slave,
            preexec_fn=_become_session_leader,
            env={**os.environ, **(env or {}), "TERM": "xterm-256color",
                 "MISAKA_NET_PANE": pane.id},
        )
        os.close(slave)
        os.set_blocking(master, False)
        pane.fd = master
        asyncio.get_running_loop().add_reader(master, self._pump, pane)

    def _pump(self, pane: Pane):
        try:
            chunk = os.read(pane.fd, 65536)
        except BlockingIOError:
            return
        except OSError:
            chunk = b""
        if not chunk:
            asyncio.get_running_loop().remove_reader(pane.fd)
            if pane.flush is not None:      # 别再往已退场的格子发合帧
                pane.flush.cancel()
                pane.flush = None
            os.close(pane.fd)
            pane.fd = None
            pane.exit_code = pane.proc.poll() if pane.proc else None
            self._broadcast(pane.id, {"event": "exited", "id": pane.id,
                                      "exit_code": pane.exit_code})
            return
        pane.buf += chunk
        pane.last_output = time.time()
        if len(pane.buf) > RING_CAP:
            del pane.buf[: len(pane.buf) - RING_CAP]
        data = pane.carry + chunk
        pane.carry = b""
        tail = _PARTIAL_ESC.search(data)
        if tail and len(data) - tail.start() <= 64:
            pane.carry, data = data[tail.start():], data[: tail.start()]
        data = _answer_queries(pane, data)
        data = _UNSUPPORTED.sub(b"", data)
        try:
            # 备用屏切换 pyte 不认识：每个切换点等价一次清屏（全屏应用随后会自己重画）；
            # 顺带记住是否在备用屏——herdr 在备用屏时不显示滚动条（panes.rs:37）
            pieces = _ALTSCREEN.split(data)
            for match in _ALTSCREEN.finditer(data):
                pane.alt_screen = match.group().endswith(b"h")
            for index, piece in enumerate(pieces):
                if index:
                    pane.screen.reset()
                if piece:
                    pane.stream.feed(piece)
        except Exception:  # noqa: BLE001 - 仿真屏消化不了的序列不拖累格子本体
            pass
        if self._attached and pane.flush is None:
            # 按帧合并，不是读到就发（herdr 服务器同样是渲染循环、不是逐字节推送）。
            # 应用画一屏会被 PTY 切成好几段，段间光标停在**行尾**这类中间位置；
            # 逐段推出去，客户端就会照着中间态摆光标——中文输入法的候选窗跟着
            # 飘到输入框右端（用户实证）。攒满一帧再发，发出去的就是这帧末的状态。
            pane.flush = asyncio.get_running_loop().call_later(
                FRAME_SECONDS, self._flush, pane)

    def _flush(self, pane: Pane):
        pane.flush = None
        if not self._attached:
            pane.screen.dirty.clear()
            return
        dirty = sorted(pane.screen.dirty)
        pane.screen.dirty.clear()
        cursor = ([pane.screen.cursor.x, pane.screen.cursor.y],
                  bool(pane.screen.cursor.hidden))
        # 光标动了也得播——pyte 的 dirty 只跟内容，挪光标不产生脏行。
        # 照 pi tui.js:1114「No changes - but still need to update hardware
        # cursor position if it moved」：内容没变但光标挪了，客户端也要跟上。
        if not dirty and cursor == pane.sent_cursor:
            return
        pane.sent_cursor = cursor
        # 带上滚动读数：clear 会清掉回看历史，面板得知道滚动条该收起来
        self._broadcast(pane.id, {
            "event": "screen", "id": pane.id,
            "rows": {str(r): _render_row(pane.screen, r)
                     for r in dirty if r < pane.screen.lines},
            "cursor": cursor[0],
            "cursor_hidden": cursor[1],
            "scroll": _scroll_metrics(pane),
            "alt_screen": pane.alt_screen,
        })

    def _broadcast(self, pane_id, payload):
        line = (json.dumps(payload, ensure_ascii=False) + "\n").encode()
        for writer, wanted in list(self._attached.items()):
            if wanted not in (pane_id, "*") or writer.transport.is_closing():
                continue
            try:
                writer.write(line)
            except Exception:  # noqa: BLE001 - 订阅者死了就摘掉
                self._attached.pop(writer, None)

    def create(self, argv, cwd, *, title="", card=None, env=None) -> Pane:
        self._seq += 1
        pane = Pane(f"p{self._seq}", title or (argv[0] if argv else ""), list(argv),
                    cwd or os.getcwd(), card=card)
        if env and env.get("MISAKA_THEME") in ("dark", "light"):
            pane.theme = self._theme = env["MISAKA_THEME"]   # 记住会话变体
        if env and env.get("MISAKA_ALLY"):
            pane.ally = env["MISAKA_ALLY"]      # 临时御坂：进名册，进程结束即消失
        self._spawn(pane, env=env)
        self.panes[pane.id] = pane
        self._save_snapshot()
        return pane

    def close(self, pane_id):
        pane = self.panes.pop(pane_id, None)
        if pane is None:
            raise ValueError(f"没有这个格子：{pane_id}")
        if pane.alive():
            # 收尾慢的应用（claude 这类 Node CLI）两次 wait 都可能超时——
            # 超时**绝不能抛出去**：进程组已经 KILL 过，内核会收走；抛异常会一路
            # 穿到面板变成"关不掉＋闪退"（用户实测，2026-08-12）
            try:
                os.killpg(pane.proc.pid, signal.SIGTERM)
            except OSError:
                pass
            try:
                pane.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(pane.proc.pid, signal.SIGKILL)
                except OSError:
                    pass
                try:
                    pane.proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass          # KILL 已发出，剩下交给内核；格子照样从表里除名
        if pane.fd is not None:
            try:
                asyncio.get_running_loop().remove_reader(pane.fd)
            except RuntimeError:   # 事件循环已收摊（进程收尾路径）
                pass
            if pane.flush is not None:
                pane.flush.cancel()
                pane.flush = None
            try:
                os.close(pane.fd)
            except OSError:
                pass
            pane.fd = None
        self._save_snapshot()
        return pane

    # ── 卡片格子（走看板状态机）──────────────────────────

    def _board(self):
        if self._con is None:
            from misaka.extensions.board import db
            self._con = db.connect(_expand(CFG["db"]))
        return self._con

    def run_card(self, task_id) -> Pane:
        from misaka.extensions.board import db
        from misaka.orchestration import processes as process_tree

        con = self._board()
        row = db.get(con, task_id)
        if row is None:
            raise ValueError(f"没有这张卡：{task_id}")
        if row["status"] != "ready":
            raise ValueError(f"卡 {task_id} 不是 ready（当前 {row['status']}）")
        # 谁来领这张活：executor 为空＝御坂（card-shell），否则＝协力者的第三方 CLI。
        # **分叉只在这一处**——认领/租约/交卷/验收/审计全部两者共用（board 是唯一总线）
        executor = json.loads(row["executor"]) if row["executor"] else None
        if executor is None:
            profile = os.path.join(_expand(CFG["profiles_root"]), row["assignee"])
            if not os.path.isdir(profile):
                raise ValueError(f"Sister {row['assignee']} 不在可启动名册")
        generation = int(row["generation"])
        lock = f"net:{socket.gethostname()}:{os.getpid()}:{secrets.token_hex(4)}"
        if not db.claim(con, task_id, lock,
                        ttl_seconds=max(1800, int(row["timeout_seconds"]) + 60),
                        generation=generation, pid=os.getpid()):
            raise ValueError(f"卡 {task_id} 已被别的调度器认领")
        workspace = os.path.join(CFG["workspaces_root"], task_id)
        try:
            os.makedirs(workspace, exist_ok=True)
            if not db.set_workspace(con, task_id, workspace,
                                    generation=generation, claim_lock=lock):
                raise RuntimeError("认领已失效")
            env = {
                "MISAKA_USAGE_DB": _expand(CFG["db"]),
                "MISAKA_USAGE_TASK_ID": task_id,
                "MISAKA_USAGE_GENERATION": str(generation),
                "MISAKA_USAGE_CLAIM_LOCK": lock,
                "MISAKA_USAGE_TOKEN_CAP": str(int(CFG.get("token_cap") or 0)),
                "MISAKA_THEME": self._theme,    # 卡格子跟随会话主题变体
                # 格子里的 misaka 命令（如协力者的 `misaka tell`）必须用**守护进程同一套库**，
                # 否则各写各的：信送进另一个 messages.db，编排官永远收不到
                "MISAKA_DB": _expand(CFG["db"]),
                "MISAKA_MESSAGES": _expand(CFG["messages_db"]),
                "MISAKA_WS": _expand(CFG["workspaces_root"]),
            }
            if executor is None:
                argv = [*CARD_SHELL, task_id]
            else:                               # 协力者：合同当提示词，非交互跑一轮
                from misaka.extensions.ally import runner as ally_runner
                argv = ally_runner.build_argv(executor, ally_runner.card_prompt(row))
                env["MISAKA_ALLY"] = row["assignee"]
            pane = self.create(argv, workspace,
                               title=f"{row['assignee']}·{task_id}", card=task_id,
                               env=env)
        except BaseException:
            db.back_to_ready(con, task_id, generation=generation, claim_lock=lock)
            raise
        pane.claim_lock, pane.generation = lock, generation
        pane.deadline = time.time() + int(row["timeout_seconds"])
        # 属主=格子进程组：守护进程崩了，reconcile 按组身份回收（child.py 同款标记）
        identity = process_tree.identity(pane.proc.pid)
        db.set_pid(con, task_id, pane.proc.pid,
                   worker_identity=f"process-group|{identity}" if identity else None,
                   generation=generation, claim_lock=lock)
        db.add_event(con, task_id, "claimed",
                     {"lock": lock, "workspace": workspace, "pane": pane.id},
                     generation=generation)
        self._save_snapshot()
        return pane

    async def _watch_cards(self):
        """盯所有卡片格子：交卷→verifying；超时→failed；退出→按产物对账。"""
        from misaka.extensions.board import db, worker

        while not self._stopping.is_set():
            for pane in [p for p in self.panes.values() if p.card and p.claim_lock]:
                con = self._board()
                row = db.get(con, pane.card)
                if row is None or int(row["generation"]) != pane.generation \
                        or row["claim_lock"] != pane.claim_lock:
                    pane.claim_lock = None          # 易主：只旁观，不再动状态
                    continue
                if not pane.alive():
                    if pane.ally:
                        # 协力者不会交卷也不会上报——退出时替它做这两件事，于是下面
                        # 「按产物对账」的逻辑对两种执行主体完全一致（board 是唯一总线）
                        from misaka.extensions.ally import runner as ally_runner
                        ally_runner.finish(
                            pane.cwd,
                            pane.exit_code if pane.exit_code is not None else -1,
                            pane.buf.decode("utf-8", errors="replace"),
                            assignee=pane.ally, task_id=pane.card)
                    ok, report = worker.check_report(pane.cwd, con=con, task_id=pane.card)
                    if db.reclaim_abandoned(
                        con, pane.card, generation=pane.generation,
                        claim_lock=row["claim_lock"], worker_pid=row["worker_pid"],
                        worker_identity=row["worker_identity"],
                        claim_expires=row["claim_expires"], submitted=bool(ok),
                    ):
                        db.add_event(con, pane.card,
                                     "submitted" if ok else "reclaimed",
                                     {"summary": report["summary"],
                                      "artifacts": report.get("artifacts", [])}
                                     if ok else {"reason": str(report)[:500]},
                                     generation=pane.generation)
                    pane.claim_lock = None
                elif not pane.submitted:
                    ok, report = worker.check_report(pane.cwd, con=con, task_id=pane.card)
                    if ok:
                        if db.mark_verifying(con, pane.card,
                                             generation=pane.generation,
                                             claim_lock=pane.claim_lock):
                            db.add_event(con, pane.card, "submitted",
                                         {"summary": report["summary"],
                                          "artifacts": report["artifacts"],
                                          "notes": report.get("notes", ""),
                                          "uncertain": report.get("uncertain", [])},
                                         generation=pane.generation)
                        pane.submitted = True   # 交卷后格子留着，人可以进去续聊
                    elif pane.deadline and time.time() > pane.deadline:
                        if db.add_event(con, pane.card, "failed",
                                        {"reason": "Sister timeout"},
                                        generation=pane.generation,
                                        claim_lock=pane.claim_lock) and db.mark_failed(
                                con, pane.card, generation=pane.generation,
                                claim_lock=pane.claim_lock):
                            pass
                        pane.claim_lock = None
                        try:
                            self.close(pane.id)
                        except ValueError:
                            pass
            try:
                await asyncio.wait_for(self._stopping.wait(), CARD_POLL_SECONDS)
            except asyncio.TimeoutError:
                pass

    # ── 快照（只记形状，不记进程）────────────────────────

    def _save_snapshot(self):
        # 布局（标签页＋页内分屏树）归**守护进程**所有——herdr 同款：客户端是瘦的，
        # 面板退了布局还在。以前布局活在面板内存里，一分离分屏就散成标签页（踩过）
        data = {"version": 1, "layout": self.layout, "panes": [
            {"id": p.id, "title": p.title, "argv": p.argv, "cwd": p.cwd,
             "card": p.card} for p in self.panes.values()]}
        tmp = self.snapshot_path + ".tmp"
        os.makedirs(os.path.dirname(self.snapshot_path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.snapshot_path)

    def restore_snapshot(self):
        """恢复＝重建普通格子（原目录开新进程）；卡片格子绝不自动复跑（花钱等点头）。"""
        try:
            with open(self.snapshot_path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return []
        skipped = []
        for item in data.get("panes", []):
            if item.get("card"):
                skipped.append(item["card"])
                continue
            if os.path.isdir(item.get("cwd") or ""):
                self.create(item["argv"], item["cwd"], title=item.get("title", ""))
        # 布局照旧恢复；里面指向的旧格子 id 由面板的 sync 清理（重建的格子是新 id）
        self.layout = data.get("layout") or []
        return skipped

    # ── 协议 ──────────────────────────────────────────────

    def _api(self, method, params):
        if method == "ping":
            return {"pong": True, "pid": os.getpid(), "panes": len(self.panes),
                    "proto": PROTOCOL}
        if method == "panes.list":
            status, mail = {}, {}
            cards = [p.card for p in self.panes.values() if p.card]
            if cards:
                from misaka.extensions.board import db
                con = self._board()
                for card in cards:
                    row = db.get(con, card)
                    status[card] = row["status"] if row else "?"
                try:
                    from misaka.extensions import messages
                    mcon = messages.connect()
                    mail = dict(mcon.execute(
                        "SELECT task_id, COUNT(*) FROM messages"
                        " WHERE delivered_at IS NULL AND task_id IS NOT NULL"
                        " GROUP BY task_id").fetchall())
                    mcon.close()
                except Exception:  # noqa: BLE001 - 信箱坏了不拖累面板
                    mail = {}
            return {"panes": [
                {"id": p.id, "title": p.title, "card": p.card, "cwd": p.cwd,
                 "alive": p.alive(), "exit_code": p.exit_code,
                 "status": status.get(p.card),
                 "busy": _pane_busy(p),
                 "foreground": _foreground(p),   # 前台跑的是什么（LO 感知用）
                 "ally": _ally_name(p),          # 非空＝这格里跑着第三方 agent
                 "mail": int(mail.get(p.card, 0)) if p.card else 0,
                 "unseen": bool(p.card
                                and status.get(p.card) in {"done", "failed", "stopped"}
                                and status.get(p.card) != p.seen_status),
                 "pid": p.proc.pid if p.proc else None} for p in self.panes.values()]}
        if method == "layout.get":      # 布局归守护进程：面板重进也不丢分屏
            return {"layout": self.layout}
        if method == "layout.set":
            self.layout = params.get("layout") or []
            self._save_snapshot()
            return {"ok": True}
        if method == "projects.list":
            # 课题总览：目录（真相）＋projects 表状态＋各自的卡。排序＝置顶在前
            # （后置顶的更靠前）→ 进行中 → 已归档；未分类卡挂在名字 None 下
            from misaka.extensions.board import project
            con = self._board()
            state = project.states(con)
            cards = {}
            for r in con.execute("SELECT id,status,title,assignee,workspace,project"
                                 " FROM tasks ORDER BY created_at"):
                ws = r["workspace"] or os.path.join(
                    _expand(CFG["workspaces_root"]), r["id"])
                sess = os.path.join(ws, "session")
                cards.setdefault(r["project"], []).append(
                    {"id": r["id"], "status": r["status"], "title": r["title"],
                     "assignee": r["assignee"],
                     "has_session": os.path.isdir(sess) and bool(os.listdir(sess))})
            out = []
            for name in project.listing():
                meta = state.get(name, {})
                out.append({"name": name,
                            "archived": bool(meta.get("archived")),
                            "pinned_at": meta.get("pinned_at"),
                            "cards": cards.pop(name, [])})
            unfiled = [c for key, rows in cards.items() if key is not None
                       for c in rows]           # 指向已删目录的卡也别隐身
            if cards.get(None) or unfiled:
                out.append({"name": None, "archived": False, "pinned_at": None,
                            "cards": unfiled + cards.get(None, [])})
            out.sort(key=lambda p: (p["archived"], -(p["pinned_at"] or 0),
                                    p["name"] is None, p["name"] or "~"))
            return {"projects": out}
        if method == "project.set":
            from misaka.extensions.board import project
            ok, msg = project.set_state(self._board(), params["name"],
                                        archived=params.get("archived"),
                                        pinned=params.get("pinned"))
            if not ok:
                raise ValueError(msg)
            return {"message": msg}
        if method == "project.delete":
            from misaka.extensions.board import project
            ok, msg = project.delete(self._board(), params["name"],
                                     with_cards=params.get("with_cards", False))
            if not ok:
                raise ValueError(msg)
            return {"message": msg}
        if method == "card.delete":
            # 在跑的卡还占着格子——先拒，让用户去 stop（面板走 card.stop）
            if any(p.card == params["task_id"] and p.alive()
                   for p in self.panes.values()):
                raise ValueError(f"卡 {params['task_id']} 还在格子里跑，先停再删")
            from misaka.extensions.board import db
            ok, msg = db.delete_task(self._board(), params["task_id"])
            if not ok:
                raise ValueError(msg)
            return {"message": msg}
        if method == "pane.explain":     # herdr `agent explain` 同款：为什么判在跑/闲
            pane = self.panes.get(params["id"])
            if pane is None:
                raise ValueError(f"没有这个格子：{params['id']}")
            return {"id": pane.id, "title": pane.title, "busy": _pane_busy(pane),
                    "why": _busy_reason(pane)}
        if method == "pane.focused":
            pane = self.panes.get(params["id"])
            if pane is None:
                raise ValueError(f"没有这个格子：{params['id']}")
            if pane.card:
                from misaka.extensions.board import db
                row = db.get(self._board(), pane.card)
                pane.seen_status = row["status"] if row else None
            return {"seen": True}
        if method == "card.stop":
            pane = next((p for p in self.panes.values()
                         if p.card == params["task_id"]), None)
            if pane is None:
                raise ValueError(f"卡 {params['task_id']} 不在任何格子里")
            from misaka.extensions.board import db
            con = self._board()
            if pane.claim_lock:
                db.add_event(con, pane.card, "stopped", {},
                             generation=pane.generation, claim_lock=pane.claim_lock)
                db.mark_stopped(con, pane.card,
                                generation=pane.generation, claim_lock=pane.claim_lock)
            pane.card = pane.claim_lock = None   # 摘牌再关：别让盯梢当崩溃对账
            self.close(pane.id)
            return {"stopped": True}
        if method == "pane.create":
            pane = self.create(params["argv"], params.get("cwd"),
                               title=params.get("title", ""),
                               env=params.get("env"))     # 面板透传主题变体等
            return {"pane_id": pane.id, "pid": pane.proc.pid}
        if method == "pane.run_card":
            pane = self.run_card(params["task_id"])
            return {"pane_id": pane.id, "pid": pane.proc.pid, "card": pane.card}
        if method == "pane.read":
            pane = self.panes.get(params["id"])
            if pane is None:
                raise ValueError(f"没有这个格子：{params['id']}")
            text = pane.buf.decode("utf-8", errors="replace")
            if params.get("strip"):
                import re
                text = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07\x1b]*(\x07|\x1b\\)|[\r\x00]", "", text)
            lines = params.get("lines")
            if lines:
                text = "\n".join(text.splitlines()[-int(lines):])
            return {"text": text, "alive": pane.alive()}
        if method == "pane.send":
            pane = self.panes.get(params.get("id") or "") or next(
                (p for p in self.panes.values()
                 if p.card and p.card == params.get("card")), None)
            if pane is None or pane.fd is None:
                raise ValueError(f"格子不在或已退出：{params.get('id') or params.get('card')}")
            os.write(pane.fd, params["text"].encode())
            if params.get("enter"):
                os.write(pane.fd, b"\r")
            return {"sent": True}
        if method == "pane.input":
            pane = self.panes.get(params["id"])
            if pane is None or pane.fd is None:
                raise ValueError(f"格子不在或已退出：{params['id']}")
            os.write(pane.fd, base64.b64decode(params["data"]))
            return {"sent": True}
        if method == "pane.resize":
            pane = self.panes.get(params["id"])
            if pane is None or pane.fd is None:
                raise ValueError(f"格子不在或已退出：{params['id']}")
            rows, cols = int(params["rows"]), int(params["cols"])
            fcntl.ioctl(pane.fd, termios.TIOCSWINSZ,
                        struct.pack("HHHH", rows, cols, 0, 0))
            pane.screen.resize(rows, cols)
            pane.screen.dirty.clear()
            try:
                os.killpg(pane.proc.pid, signal.SIGWINCH)
            except OSError:
                pass
            return {"resized": True}
        if method == "pane.screen":
            pane = self.panes.get(params["id"])
            if pane is None:
                raise ValueError(f"没有这个格子：{params['id']}")
            pane.screen.dirty.clear()
            return {"rows": [_render_row(pane.screen, r)
                             for r in range(pane.screen.lines)],
                    "cursor": [pane.screen.cursor.x, pane.screen.cursor.y],
                    "cursor_hidden": bool(pane.screen.cursor.hidden),
                    "size": [pane.screen.lines, pane.screen.columns],
                    "scroll": _scroll_metrics(pane),
                    "alt_screen": pane.alt_screen}
        if method == "pane.scroll":
            # herdr 的回看：delta<0 往回翻，>0 往下翻，"bottom" 直接回底
            pane = self.panes.get(params["id"])
            if pane is None:
                raise ValueError(f"没有这个格子：{params['id']}")
            _scroll_pane(pane, params.get("delta", 0), params.get("to"))
            return {"scroll": _scroll_metrics(pane),
                    "rows": [_render_row(pane.screen, r)
                             for r in range(pane.screen.lines)]}
        if method == "pane.close":
            self.close(params["id"])
            return {"closed": True}
        if method == "server.stop":
            self._stopping.set()
            return {"stopping": True}
        raise ValueError(f"未知方法：{method}")

    async def _serve_client(self, reader, writer):
        self._clients.add(writer)
        try:
            while not self._stopping.is_set():
                line = await reader.readline()
                if not line:
                    break
                try:
                    req = json.loads(line)
                    method = req.get("method", "")
                    if method == "pane.attach":   # 订阅流："*"=收全部格子的屏事件（平铺面板用）
                        wanted = (req.get("params") or {}).get("id", "")
                        if wanted != "*" and wanted not in self.panes:
                            raise ValueError("没有这个格子")
                        self._attached[writer] = wanted
                        result = {"attached": wanted}
                    else:
                        result = self._api(method, req.get("params") or {})
                    out = {"id": req.get("id"), "result": result}
                except Exception as error:  # noqa: BLE001 - 单条请求失败不拆连接
                    out = {"id": None, "error": f"{type(error).__name__}: {error}"}
                writer.write((json.dumps(out, ensure_ascii=False) + "\n").encode())
                try:
                    await writer.drain()
                except ConnectionError:
                    break
        finally:
            self._clients.discard(writer)
            self._attached.pop(writer, None)
            writer.close()

    async def run(self):
        # 单例：连得上=已有守护进程；连不上的陈尸套接字删掉重建
        if os.path.exists(self.sock_path):
            probe = socket.socket(socket.AF_UNIX)
            try:
                probe.connect(self.sock_path)
                probe.close()
                raise SystemExit("守护进程已在跑（套接字有人应答）")
            except (ConnectionRefusedError, FileNotFoundError, OSError):
                pass
            finally:
                probe.close()
            os.unlink(self.sock_path)
        os.makedirs(os.path.dirname(self.sock_path), exist_ok=True)
        server = await asyncio.start_unix_server(self._serve_client, self.sock_path)
        os.chmod(self.sock_path, 0o600)
        ally_commands()   # 播种：allies.json 从守护进程首跑就在盘上，用户随时可编
        skipped = self.restore_snapshot()
        if skipped:
            print(f"上次有 {len(skipped)} 张卡在跑（{', '.join(skipped)}）；不自动复跑，进面板续派", flush=True)
        watcher = asyncio.create_task(self._watch_cards())
        await self._stopping.wait()
        watcher.cancel()
        for pane_id in list(self.panes):
            try:
                self.close(pane_id)
            except ValueError:
                pass
        server.close()
        for writer in list(self._clients):   # 挂着的连接不收摊，wait_closed 会永等
            writer.close()
        await server.wait_closed()
        try:
            os.unlink(self.sock_path)
        except OSError:
            pass


def main():
    asyncio.run(Daemon().run())


if __name__ == "__main__":
    main()
