"""pixel_clau — a pixel Claude that floats on top of Windows and talks to Claude Code.

click       : pixel prompt box (click again / Esc to close; long press opens it without the wait)
double click: chat panel, popped out of the sprite (double click again to close)
drag        : move the sprite (open popups ride along); dragging the chat moves the sprite too
right click : session picker, new session, run-at-login, quit
"""
import ctypes, json, os, queue, re, shutil, struct, subprocess, sys, threading, time, traceback, winreg, winsound
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, font as tkfont, ttk

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass

NAME, OLD_NAME = 'pixel_clau', 'ClaudeFloat'  # OLD_NAME: what v1.0-1.1 were called (settings / autostart)
APPDATA = Path(os.environ.get('APPDATA') or Path.home() / 'AppData' / 'Roaming')
CFG_PATH = APPDATA / NAME / 'config.json'  # not next to the script: a packaged exe unpacks to a temp dir
PROJECTS = Path(os.environ.get('CLAUDE_CONFIG_DIR') or Path.home() / '.claude') / 'projects'
DESKTOP = APPDATA / 'Claude' / 'claude-code-sessions'  # the desktop app's session list
PIXEL_DIR = APPDATA / NAME / 'pixel-claude'  # the pixel's own sessions work here, away from your projects
RUN_KEY = r'Software\Microsoft\Windows\CurrentVersion\Run'
APPROVED_KEY = r'Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run'
ICON = APPDATA / NAME / 'pixel_clau.ico'
LOG = APPDATA / NAME / 'error.log'
ORANGE, BG, BG2, FG, DIM, KEY = '#D97757', '#262624', '#30302E', '#ECEBE6', '#9A9890', '#010203'
RING = {'busy': '#FFB454', 'done': '#5BB974', 'error': '#E5534B'}
PIX = {'B': ORANGE, 'L': '#E8A183', 'D': '#B9553A', 'E': '#2E2019'}
BODY = tuple((y, 1, 14) for y in range(1, 12))  # (row, x, width) — a flat, angular block
PERMS = ['default', 'acceptEdits', 'plan', 'bypassPermissions']
LONG_PRESS_MS = 450
TAIL = 7  # speech-bubble tail length, in frame pixels
WRAPPERS = re.compile(r'<(system-reminder|command-[\w-]+|local-command-[\w-]+|[\w-]*hook[\w-]*)>.*?</\1>', re.S)
NO_CLI = 'claude CLI를 찾을 수 없어요. Claude Code를 설치하고 터미널에서 `claude`로 한 번 로그인해 주세요.'


# ---- reading Claude Code's session files -----------------------------------------------
def msg_text(d):
    """(role, text) for a displayable user/assistant record, else None."""
    role = d.get('type')
    if role not in ('user', 'assistant') or d.get('isMeta') or d.get('isSidechain'):
        return None
    c = (d.get('message') or {}).get('content')
    if isinstance(c, list):
        c = '\n'.join(x.get('text', '') for x in c if isinstance(x, dict) and x.get('type') == 'text')
    c = WRAPPERS.sub('', c or '').strip().lstrip('﻿')  # desktop-app prompts carry injected reminder blocks
    if not c or c.startswith('<'):
        return None
    return role, c


def tool_line(x):
    inp = x.get('input') or {}
    path = inp.get('file_path') or inp.get('path')
    arg = (inp.get('description') or (Path(path).name if isinstance(path, str) else '')
           or next((v for v in inp.values() if isinstance(v, str)), ''))
    return f"⚙ {x.get('name')}  {arg.strip().splitlines()[0][:70] if arg.strip() else ''}"


def parse_node(line):
    """(uuid, parent uuid, display items) for one transcript line, or None."""
    try:
        d = json.loads(line)
    except ValueError:
        return None
    if not d.get('uuid') or d.get('isSidechain'):
        return None
    items = [m] if (m := msg_text(d)) else []
    if d.get('type') == 'assistant':
        items += [('tool', tool_line(x)) for x in (d.get('message') or {}).get('content') or []
                  if isinstance(x, dict) and x.get('type') == 'tool_use']
    a = d.get('attachment') or {}
    if a.get('type') == 'queued_command' and isinstance(a.get('prompt'), str) \
            and not a['prompt'].lstrip().startswith('<'):  # messages typed while Claude was busy
        items.append(('user', a['prompt']))
    return d['uuid'], d.get('parentUuid') or d.get('logicalParentUuid'), items


class Transcript:
    """A session file parsed incrementally: each refresh reads only the bytes appended since the last,
    and keeps just (uuid, parent, items) per record — not the full JSON — so big sessions stay cheap."""

    def __init__(self, f):
        self.f, self.pos, self.nodes = f, 0, []

    def refresh(self):
        if self.f.stat().st_size < self.pos:  # rewritten rather than appended: start over
            self.pos, self.nodes = 0, []
        with open(self.f, 'rb') as fh:  # streamed line by line: never holds the whole file in memory
            fh.seek(self.pos)
            for raw in fh:
                if not raw.endswith(b'\n'):  # half-written last line: picked up on the next refresh
                    break
                self.pos += len(raw)
                if b'"uuid"' in raw and (node := parse_node(raw)):
                    self.nodes.append(node)

    def items(self):
        """(tag, text) on the active branch: walk parents back from the newest record, skipping turns
        another client appended off to the side — the same way Claude Code resolves a conversation."""
        parent = {u: p for u, p, _ in self.nodes}
        chain, u = set(), self.nodes[-1][0] if self.nodes else None
        while u in parent and u not in chain:
            chain.add(u)
            u = parent[u]
        return [x for u, _, its in self.nodes if u in chain for x in its]


def tail_records(f, nbytes=300_000):
    """Parsed records from the last `nbytes` of a session file, newest first."""
    with open(f, 'rb') as fh:
        fh.seek(max(0, f.stat().st_size - nbytes))
        lines = fh.read().decode('utf-8', 'replace').splitlines()
    for line in reversed(lines):
        try:
            yield json.loads(line)
        except ValueError:
            continue  # the chunk's cut-off first line, or a half-written last one


def head_info(f):
    """(first real prompt, first cwd), reading from the top only as far as needed."""
    first = cwd = None
    with open(f, encoding='utf-8', errors='replace') as fh:
        for line in fh:
            if '"cwd"' in line or '"type":"user"' in line:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                cwd = cwd or d.get('cwd')
                if (m := msg_text(d)) and m[0] == 'user':
                    return m[1], cwd
    return first, cwd


def session_file(sid):
    return next(PROJECTS.glob(f'*/{sid}.jsonl'), None) if sid else None


def session_cwd(f):
    """Latest cwd recorded in a session file — sessions can move folders mid-way."""
    return next((d['cwd'] for d in tail_records(f) if d.get('cwd')), None)


def turn_state(f):
    """'done' if the newest message is a finished assistant turn, else 'busy'."""
    for d in tail_records(f):
        if d.get('type') in ('user', 'assistant') and not d.get('isSidechain'):
            return 'done' if (d.get('message') or {}).get('stop_reason') == 'end_turn' else 'busy'
    return 'done'


def desktop_sessions():
    """cliSessionId -> Claude desktop app metadata (sidebar title, cwd, isArchived)."""
    out = {}
    for f in DESKTOP.rglob('local_*.json'):
        try:
            d = json.loads(f.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        if d.get('cliSessionId'):
            out[d['cliSessionId']] = d
    return out


def list_sessions(limit=60):
    """Recent sessions, newest first. Reads each file's tail (title, latest prompt, cwd) and only
    goes to the top for a CLI session that has no title yet."""
    desk = desktop_sessions()
    files = [f for f in PROJECTS.glob('*/*.jsonl') if 'observer-sessions' not in f.parent.name]
    files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
    snip = lambda s: WRAPPERS.sub('', s or '').strip().replace('\n', ' ')[:70]
    out = []
    for f in files[:limit]:
        meta = desk.get(f.stem, {})
        if meta.get('isArchived'):
            continue
        title, last, cwd = meta.get('title'), None, meta.get('cwd')
        for d in tail_records(f):
            cwd = cwd or d.get('cwd')
            title = title or (d.get('customTitle') if d.get('type') == 'custom-title' else None)
            if last is None:
                if d.get('type') == 'last-prompt':
                    last = d.get('lastPrompt')
                elif (m := msg_text(d)) and m[0] == 'user':
                    last = m[1]
            if cwd and title and last:
                break
        if not (title and cwd):
            first, cwd0 = head_info(f)
            title, cwd = title or snip(first), cwd or cwd0
        if cwd:
            out.append({'id': f.stem, 'cwd': cwd, 'title': title or f.stem, 'last': snip(last),
                        'mtime': f.stat().st_mtime, 'desktop': bool(meta)})
    return out


# ---- look & feel ------------------------------------------------------------------------
def sprite_rects(dy=0, blink=False, step=0, mood='idle', look=0, mouth=False):
    """Pixel Claude as (x, y, w, h, color) cells on an 18x18 grid — shared by the button and the icon."""
    out = []

    def px(x, y, w=1, h=1, col='B'):
        out.append((x + 1 + look, y + dy + 2, w, h, PIX[col]))  # whole body leans toward the prompt box

    for i, x in enumerate((2, 6, 9, 13)):  # legs
        px(x, 12, 2, 3 if i % 2 == step else 2, 'D')
    for y, x, w in BODY:
        px(x, y, w, 1)
    px(1, 1, 1, 9, 'L')                      # left highlight
    px(1, 11, 14, 1, 'D')                    # bottom shade
    lx, rx = 3 + look, 9 + look  # eyes
    if blink:
        px(lx, 6, 3, 1, 'E')
        px(rx, 6, 3, 1, 'E')
    elif mood == 'done':  # ^ ^
        px(lx, 6, 3, 1, 'E')
        px(lx + 1, 5, 1, 1, 'E')
        px(rx, 6, 3, 1, 'E')
        px(rx + 1, 5, 1, 1, 'E')
    else:
        px(lx, 5, 3, 3, 'E')
        px(rx, 5, 3, 3, 'E')
    if mouth:  # munch
        px(6 + look, 8, 4, 2, 'E')
    return out


def pixel_font(_cache=[]):
    """First installed bitmap-ish font, else Consolas."""
    if not _cache:
        have = set(tkfont.families())
        _cache.append(next((f for f in ('Galmuri11', 'DungGeunMo', 'Neo둥근모', 'Press Start 2P',
                                        'Consolas') if f in have), 'Consolas'))
    return _cache[0]


# ---- launching: run at login, Start menu / desktop icons (per-user, no admin needed) --------
def launch_target():
    """(program, arguments) that start this copy of pixel_clau."""
    if getattr(sys, 'frozen', False):  # packaged exe
        return sys.executable, ''
    return str(Path(sys.executable).with_name('pythonw.exe')), f'"{Path(__file__).resolve()}"'


def autostart_cmd():
    prog, args = launch_target()
    return f'"{prog}" {args}'.strip()


def autostart(on=None):
    """Read (on=None) or set whether pixel_clau starts at login."""
    hk = winreg.HKEY_CURRENT_USER
    with winreg.OpenKey(hk, RUN_KEY, 0, winreg.KEY_READ | winreg.KEY_SET_VALUE) as k, \
            winreg.CreateKey(hk, APPROVED_KEY) as a:
        if on is None:
            try:
                enabled = winreg.QueryValueEx(a, NAME)[0][0] % 2 == 0  # odd first byte = disabled in Task Manager
            except (FileNotFoundError, IndexError):
                enabled = False
            try:
                return enabled and winreg.QueryValueEx(k, NAME)[0] == autostart_cmd()
            except FileNotFoundError:
                return False
        if on:
            winreg.SetValueEx(k, NAME, 0, winreg.REG_SZ, autostart_cmd())
            # Windows skips a Run entry that has no "enabled" record here (seen on this Win 11 build)
            winreg.SetValueEx(a, NAME, 0, winreg.REG_BINARY, b'\x02' + bytes(11))
        for key, names in ((k, (OLD_NAME,) if on else (NAME, OLD_NAME)), (a, () if on else (NAME,))):
            for name in names:  # never leave the old version launching too
                try:
                    winreg.DeleteValue(key, name)
                except FileNotFoundError:
                    pass


def write_icon(path, k=4):
    """The idle sprite as a .ico (32-bit BMP payload) — stdlib only, for the script version's shortcuts."""
    n = 18 * k
    px = [[b'\0\0\0\0'] * n for _ in range(n)]
    for x, y, w, h, col in sprite_rects():
        bgra = bytes((int(col[5:7], 16), int(col[3:5], 16), int(col[1:3], 16), 255))
        for yy in range(y * k, (y + h) * k):
            for xx in range(x * k, (x + w) * k):
                px[yy][xx] = bgra
    pixels = b''.join(b''.join(row) for row in reversed(px))  # BMP rows go bottom-up
    mask = bytes((n + 31) // 32 * 4 * n)
    dib = struct.pack('<IiiHHIIiiII', 40, n, 2 * n, 1, 32, 0, len(pixels) + len(mask), 0, 0, 0, 0) + pixels + mask
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(struct.pack('<HHHBBBBHHII', 0, 1, 1, n, n, 0, 0, 1, 32, len(dib), 22) + dib)


def make_shortcut(where):
    """Create a pixel_clau shortcut in 'Programs' (Start menu) or 'Desktop' — the real, possibly
    OneDrive-redirected folder, which is why Windows is asked for it rather than guessing."""
    prog, args = launch_target()
    icon = prog if getattr(sys, 'frozen', False) else str(ICON)
    if icon == str(ICON):
        write_icon(ICON)
    ps = ("$l = Join-Path ([Environment]::GetFolderPath($env:PC_WHERE)) ($env:PC_NAME + '.lnk');"
          "$s = (New-Object -ComObject WScript.Shell).CreateShortcut($l);"
          "$s.TargetPath = $env:PC_PROG; $s.Arguments = $env:PC_ARGS; $s.IconLocation = $env:PC_ICON;"
          "$s.WorkingDirectory = Split-Path $env:PC_PROG; $s.Description = 'pixel Claude for Claude Code'; $s.Save()")
    env = dict(os.environ, PC_WHERE=where, PC_NAME=NAME, PC_PROG=prog, PC_ARGS=args, PC_ICON=icon)
    subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command', ps], env=env,
                   creationflags=subprocess.CREATE_NO_WINDOW, timeout=30)


def log_error(*exc):
    """pythonw has no console: keep tracebacks where they can be found."""
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, 'a', encoding='utf-8') as fh:
        fh.write(f'--- {datetime.now():%Y-%m-%d %H:%M:%S}\n' + ''.join(traceback.format_exception(*exc)))


class App:
    def __init__(self):
        self.cfg = {}
        for p in (CFG_PATH, APPDATA / OLD_NAME / 'config.json'):  # carry settings over from v1.x
            try:
                self.cfg = json.loads(p.read_text(encoding='utf-8'))
                break
            except Exception:
                pass
        self.cfg.setdefault('perm', 'default')
        self.to_pixel_session()  # every launch starts fresh in the pixel's own session
        self.state, self.tick, self.proc, self.q = 'idle', 0, None, queue.Queue()
        self.drawn = self.click_job = self.handed = None
        self.pending = []  # prompts sent while a run was still finishing
        self.present = self.dbl = self.nod = self.munch = 0

        r = self.root = tk.Tk()
        r.title(NAME)
        self.S = int(64 * r.winfo_fpixels('1i') / 96)
        icon = tk.PhotoImage(width=18, height=18)  # title-bar icon for the picker / dialogs
        for x, y, w, h, col in sprite_rects():
            icon.put(col, to=(x, y, x + w, y + h))
        self.icon = icon.zoom(2)
        r.iconphoto(True, self.icon)
        W, H = r.winfo_screenwidth(), r.winfo_screenheight()
        r.overrideredirect(True)
        r.attributes('-topmost', True)
        r.attributes('-transparentcolor', KEY)
        r.configure(bg=KEY)
        r.geometry(f"{self.S}x{self.S}+{self.cfg.get('x', W - self.S - 40)}+{self.cfg.get('y', H // 2)}")
        cv = self.cv = tk.Canvas(r, width=self.S, height=self.S, bg=KEY, highlightthickness=0, cursor='hand2')
        cv.pack()
        cv.bind('<ButtonPress-1>', self._press)
        cv.bind('<B1-Motion>', self._motion)
        cv.bind('<ButtonRelease-1>', self._release)
        cv.bind('<Double-Button-1>', self._double)
        cv.bind('<Button-3>', self._menu)

        self.chat = Chat(self)
        self.quick = Quick(self)
        self._loop()

    # ---- pixel Claude ----------------------------------------------------
    def _frame(self):
        """Animation params for this tick: (bob, blink, walk-step, mood, eye-look, mouth-open)."""
        t, s = self.tick, self.state
        look = self.quick.side if self.quick.win.state() == 'normal' else 0  # eyes follow the prompt box
        if self.present:  # ducking down while it hands you the chat window
            self.present -= 1
            return (3 if self.present > 4 else 1), False, 0, s, look, False
        if self.munch:  # chomps the message you just sent
            self.munch -= 1
            return 0, False, 0, s, self.quick.side, (self.munch // 2) % 2 == 0
        if self.nod:  # bounces along while you type
            self.nod -= 1
            return (-2 if self.nod > 1 else -1), False, 0, s, look, False
        if s == 'busy':  # trotting
            return (t // 3) % 2, False, (t // 3) % 2, 'busy', 0, False
        if s == 'done':  # happy hop
            return (0, -2, -3, -1)[(t // 3) % 4], False, 0, 'done', 0, False
        if s == 'error':
            return 0, False, 0, 'error', 0, False
        return (t // 14) % 2, t % 90 < 4, 0, 'idle', look, False  # breathing + blink every ~4.5s

    def _draw(self):
        key = self._frame()
        if key == self.drawn:  # redraw only when the frame actually changes
            return
        self.drawn = key
        c, S, p = self.cv, self.S, self.S / 18  # 18-unit grid: headroom for the hop
        c.delete('all')
        for x, y, w, h, col in sprite_rects(*key):
            c.create_rectangle(x * p, y * p, (x + w) * p, (y + h) * p, fill=col, width=0)
        mood, step = key[3], key[2]
        # status badge: pulses while busy, blinks when done / failed
        if mood != 'idle' and not (mood != 'busy' and (self.tick // 8) % 2):
            r, cx = (2.8 + 0.5 * step) * p, S - 3.2 * p
            c.create_oval(cx - r, cx - r, cx + r, cx + r, fill=RING[mood], outline='#FFF3EB',
                          width=max(1, p * 0.25))

    def set_state(self, s):
        self.state, self.tick, self.drawn = s, 0, None

    def _press(self, e):
        self.drag = (e.x_root, e.y_root, self.root.winfo_x(), self.root.winfo_y())
        # open popups ride along with the sprite (keeps the bubble tail pointing at it)
        self.follow = [(w, w.winfo_x(), w.winfo_y()) for w in (self.chat.win, self.quick.win)
                       if w.state() == 'normal']
        # remember now: the box's focus-out auto-hide fires before the click is handled
        self.quick_open = self.quick.win.state() == 'normal'
        self.moved = self.long = False
        self.lp_job = self.root.after(LONG_PRESS_MS, self._long_press)

    def _motion(self, e):
        x0, y0, wx, wy = self.drag
        dx, dy = e.x_root - x0, e.y_root - y0
        if self.long or (not self.moved and abs(dx) + abs(dy) < 6):
            return
        self.moved = True
        self.root.after_cancel(self.lp_job)
        self.root.geometry(f'+{wx + dx}+{wy + dy}')
        for w, x, y in self.follow:
            w.geometry(f'+{x + dx}+{y + dy}')

    def _release(self, e):
        self.root.after_cancel(self.lp_job)
        if self.moved:
            self.save(x=self.root.winfo_x(), y=self.root.winfo_y())
        elif self.dbl:
            self.dbl = 0
        elif not self.long:  # wait out the double-click window before acting
            self.click_job = self.root.after(260, self._click)

    def _click(self):
        self.click_job = None
        if self.state in ('done', 'error'):
            self.set_state('idle')
        if self.quick_open:
            self.quick.win.withdraw()
        else:
            self.quick.show()

    def _double(self, e):
        self.dbl = 1
        for job in (self.click_job, self.lp_job):
            if job:
                self.root.after_cancel(job)
        self.click_job = None
        if self.state in ('done', 'error'):
            self.set_state('idle')
        self.quick.win.withdraw()
        self.chat.toggle()

    def _long_press(self):
        self.long = True
        self._click()

    def _menu(self, e):
        m = tk.Menu(self.root, tearoff=0)
        m.add_command(label=f"세션: {self.cfg.get('title') or '(새 세션)'}"[:60], state='disabled')
        m.add_command(label=f"폴더: {self.session_dir()}"[:60], state='disabled')
        m.add_separator()
        m.add_command(label='픽셀 전용 세션 (새 대화)', command=self.new_pixel_session)
        m.add_command(label='설정 / 세션 선택…', command=lambda: Settings(self))
        m.add_command(label='폴더 골라서 새 세션…', command=self.new_session)
        m.add_separator()
        self.auto_var = tk.BooleanVar(value=autostart())
        m.add_checkbutton(label='윈도우 시작 시 자동 실행', variable=self.auto_var,
                          command=lambda: autostart(self.auto_var.get()))
        m.add_command(label='바탕화면에 아이콘 만들기',
                      command=lambda: threading.Thread(target=make_shortcut, args=('Desktop',), daemon=True).start())
        m.add_command(label='종료', command=self.quit)
        m.tk_popup(e.x_root, e.y_root)

    def place_near(self, win, w, h):
        bx, by, S = self.root.winfo_x(), self.root.winfo_y(), self.S
        W, H = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        x = bx - w - 8 if bx + S / 2 > W / 2 else bx + S + 8
        y = max(0, min(by + S // 2 - h // 2, H - h - 48))
        win.geometry(f'{w}x{h}+{x}+{y}')
        return x, y

    # ---- session / config ------------------------------------------------
    def save(self, **kw):
        self.cfg.update(kw)
        CFG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CFG_PATH.write_text(json.dumps(self.cfg, ensure_ascii=False, indent=1), encoding='utf-8')

    def session_dir(self):
        """Folder the selected session lives in now (falls back to the new-session folder)."""
        f = session_file(self.cfg.get('session'))
        return (f and session_cwd(f)) or self.cfg.get('cwd') or str(PIXEL_DIR)

    def to_pixel_session(self):
        """Point at a fresh pixel-only session; claude creates it on the first question."""
        PIXEL_DIR.mkdir(parents=True, exist_ok=True)
        self.cfg.update(session=None, cwd=str(PIXEL_DIR), title=f'픽셀 클로드 {datetime.now():%m-%d %H:%M}')

    def new_pixel_session(self):
        self.to_pixel_session()
        self.save()
        self.chat.reload()

    def new_session(self):
        d = filedialog.askdirectory(parent=self.root, title='새 세션 작업 폴더',
                                    initialdir=self.cfg.get('cwd') or str(Path.home()))
        if d:
            self.save(session=None, cwd=str(Path(d)), title=None)
            self.chat.reload()

    # ---- claude process --------------------------------------------------
    def run(self, prompt):
        if self.proc:  # previous answer still streaming, or its stop hooks still running: send right after
            self.pending.append(prompt)
            self.chat.add('tool', '⏳ 앞 작업이 끝나면 이어서 보냅니다')
            return
        sid = self.cfg.get('session')
        if sid:
            # the desktop app never re-reads its open conversations, and nothing outside it can
            # post into one — so hand the prompt over to the app instead of writing behind its back
            local = desktop_sessions().get(sid, {}).get('sessionId')
            if local:
                return self.handoff(prompt, local)
        claude = shutil.which('claude')  # searches the PATH this process started with
        if not claude:
            self.chat.add('err', NO_CLI)
            return self.set_state('error')
        cwd = self.session_dir()
        if not Path(cwd).is_dir():
            cwd = str(Path.home())
        args = [claude, '-p', '--output-format', 'stream-json', '--verbose', '--permission-mode', self.cfg['perm']]
        if sid:
            args += ['--resume', sid]
        self.chat.add('user', prompt)
        env = {k: v for k, v in os.environ.items() if k not in ('CLAUDECODE', 'CLAUDE_CODE_ENTRYPOINT')}
        try:
            self.proc = subprocess.Popen(args, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.STDOUT, text=True, encoding='utf-8', env=env,
                                         errors='replace', creationflags=subprocess.CREATE_NO_WINDOW)
        except OSError as ex:
            self.chat.add('err', f'claude 실행 실패: {ex}')
            return self.set_state('error')
        self.set_state('busy')
        self.chat.busy(True)
        threading.Thread(target=self._reader, args=(self.proc, prompt), daemon=True).start()

    def handoff(self, prompt, local):
        """Prompt -> clipboard, open the session in the desktop app; the user pastes and sends there.
        The chat bubble keeps mirroring the session file, and the sprite tracks the app's turn."""
        self.root.clipboard_clear()
        self.root.clipboard_append(prompt)
        os.startfile(f'claude://code/continue?session={local}')
        self.chat.add('tool', '↪ 앱으로 넘겼어요 — 앱 입력창에서 Ctrl+V, Enter')
        self.handed = (self.cfg['session'], time.time())

    def _check_handoff(self):
        """busy once the pasted prompt lands in the session file, done when the app's turn ends."""
        sid, since = self.handed
        f = session_file(sid)
        if not f or time.time() - since > 1800:  # never pasted: stop watching after 30 min
            self.handed = None
            return
        if f.stat().st_mtime <= since:
            return
        st = turn_state(f)
        if st == 'busy' and self.state != 'busy':
            self.set_state('busy')
        elif st == 'done' and self.state == 'busy':
            self.handed = None
            self.set_state('done')
            winsound.MessageBeep(winsound.MB_ICONASTERISK)

    def _reader(self, proc, prompt):
        proc.stdin.write(prompt)  # prompt via stdin: no shell-escaping issues
        proc.stdin.close()
        for line in proc.stdout:
            try:
                self.q.put(('json', json.loads(line)))
            except ValueError:
                if line.strip():
                    self.q.put(('err', line.strip()))
        self.q.put(('exit', proc.wait()))

    def stop(self):
        self.pending.clear()
        if self.proc:
            self.proc.kill()
            self.chat.add('err', '(중지됨)')

    def _handle(self, kind, d):
        if kind == 'err':
            return self.chat.add('err', d)
        if kind == 'exit':
            self.proc = None
            self.chat.busy(False)
            if self.state == 'busy':
                self.set_state('error' if d else 'done')
            if self.state in ('done', 'error'):
                winsound.MessageBeep(winsound.MB_ICONASTERISK if self.state == 'done' else winsound.MB_ICONHAND)
            if self.pending:
                self.run(self.pending.pop(0))
            return
        t = d.get('type')
        if d.get('session_id') and d['session_id'] != self.cfg.get('session'):
            owned = self.cfg.get('owned', [])
            if not self.cfg.get('session'):  # born here: tag it 픽셀 in the picker
                owned = (owned + [d['session_id']])[-200:]
            self.save(session=d['session_id'], owned=owned)
        if t == 'assistant':
            for x in d['message'].get('content', []):
                if x.get('type') == 'text' and x['text'].strip():
                    self.chat.add('assistant', x['text'])
                elif x.get('type') == 'tool_use':
                    self.chat.add('tool', tool_line(x))
        elif t == 'result':  # the answer is complete; the process may linger a bit for stop hooks
            if d.get('is_error'):
                self.chat.add('err', str(d.get('result') or d.get('subtype')))
            self.set_state('error' if d.get('is_error') else 'done')
            self.chat.busy(False)

    def _loop(self):
        while True:
            try:
                self._handle(*self.q.get_nowait())
            except queue.Empty:
                break
        self.tick += 1
        if self.handed and self.tick % 20 == 0:
            self._check_handoff()
        self._draw()
        self.root.after(50, self._loop)

    def quit(self):
        self.stop()
        self.root.destroy()


class Chat:
    """Chat panel drawn as a pixel speech bubble whose tail points at the sprite."""

    def __init__(self, app):
        self.app, self.last_role, self.mtime, self.tr, self.shown = app, None, None, None, None
        W, H = app.root.winfo_screenwidth(), app.root.winfo_screenheight()
        self.size = int(W * 0.3125), int(H * 0.9)  # FHD: 600x972 ≈ 28% of screen
        self.b = max(3, app.S // 16)               # one frame "pixel"
        w = self.win = tk.Toplevel(app.root)
        w.withdraw()
        w.overrideredirect(True)
        w.attributes('-transparentcolor', KEY)
        w.configure(bg=KEY)
        self.cv = tk.Canvas(w, bg=KEY, highlightthickness=0)
        self.cv.place(x=0, y=0, relwidth=1, relheight=1)
        self.box = tk.Frame(w, bg=BG)

        # closes only by double-clicking the sprite again (no ✕ / Esc, by request)
        self.head = tk.Label(self.box, bg=BG2, fg=DIM, anchor='w', padx=12, pady=8, font=(pixel_font(), 9),
                             cursor='fleur')
        self.head.pack(fill='x')
        for wid in (self.head, self.cv):  # drag the bubble by its header / frame; the sprite rides along
            wid.bind('<ButtonPress-1>', self._grab)
            wid.bind('<B1-Motion>', self._drag)
            wid.bind('<ButtonRelease-1>', lambda e: app.save(x=app.root.winfo_x(), y=app.root.winfo_y()))
        bottom = tk.Frame(self.box, bg=BG, padx=10, pady=10)
        bottom.pack(side='bottom', fill='x')
        self.inp = tk.Text(bottom, height=3, bg=BG2, fg=FG, insertbackground=FG, bd=0, wrap='word',
                           font=('Segoe UI', 10), padx=8, pady=6)
        self.inp.pack(side='left', fill='x', expand=True)
        self.inp.bind('<Return>', self._enter)
        self.btn = tk.Button(bottom, text='전송', command=self.send, bg=ORANGE, fg='white', bd=0,
                             activebackground='#C4633F', font=('Segoe UI', 10, 'bold'), padx=14)
        self.btn.pack(side='left', fill='y', padx=(8, 0))

        # no scrollbar: the light system one clashes with the pixel frame; mouse wheel scrolls
        self.log = tk.Text(self.box, bg=BG, fg=FG, bd=0, wrap='word', padx=14, pady=10, state='disabled',
                           font=('Segoe UI', 10), spacing3=2, highlightthickness=0)
        self.log.pack(fill='both', expand=True)
        self.log.tag_config('h_user', foreground=ORANGE, font=('Segoe UI', 9, 'bold'), spacing1=12)
        self.log.tag_config('h_assistant', foreground=DIM, font=('Segoe UI', 9, 'bold'), spacing1=12)
        self.log.tag_config('tool', foreground=DIM, font=('Consolas', 9))
        self.log.tag_config('err', foreground=RING['error'])
        self.reload()
        self._watch()

    def toggle(self):
        if self.win.state() == 'normal':
            return self.win.withdraw()
        if not self.app.proc:
            self.reload()  # pick up turns made elsewhere (desktop app / terminal)
        w, h = self.size
        w += TAIL * self.b  # room for the tail
        x, y = self.app.place_near(self.win, w, h)
        self.app.present = 10  # Claude ducks down as the window pops out of it
        self.box.place_forget()
        self.win.deiconify()
        self.win.lift()
        self.win.attributes('-topmost', True)
        self.win.after(400, lambda: self.win.attributes('-topmost', False))
        self._grow(0, x, y, w, h)

    def _grow(self, i, x, y, w, h, n=8):
        """Inflate the bubble out of the sprite, then draw the full pixel frame."""
        k = 1 - (1 - (i + 1) / n) ** 3
        bx = self.app.root.winfo_x() + self.app.S / 2
        by = self.app.root.winfo_y() + self.app.S / 2
        cw, ch = max(48, int(w * k)), max(32, int(h * k))
        self.win.geometry(f'{cw}x{ch}+{int(bx + (x + w / 2 - bx) * k - cw / 2)}'
                          f'+{int(by + (y + h / 2 - by) * k - ch / 2)}')
        if i + 1 < n:
            b = self.b
            self.cv.delete('all')
            self.cv.create_rectangle(0, 0, cw, ch, fill=ORANGE, width=0)
            self.cv.create_rectangle(b, b, cw - b, ch - b, fill=BG, width=0)
            self.win.after(16, self._grow, i + 1, x, y, w, h, n)
        else:
            self.win.geometry(f'{w}x{h}+{x}+{y}')
            self._bubble(w, h, tail_right=x < self.app.root.winfo_x(), tail_y=by - y)
            self.inp.focus_force()

    def _bubble(self, W, H, tail_right, tail_y):
        """Pixel speech bubble: chunky border, notched corners, stepped tail."""
        c, b = self.cv, self.b
        t = TAIL * b
        ox = 0 if tail_right else t  # bubble body x-offset
        w = W - t
        c.delete('all')
        c.create_rectangle(ox, 0, ox + w, H, fill=ORANGE, width=0)
        c.create_rectangle(ox + b, b, ox + w - b, H - b, fill=BG, width=0)
        for x, y in ((ox, 0), (ox + w - b, 0), (ox, H - b), (ox + w - b, H - b)):  # notch corners
            c.create_rectangle(x, y, x + b, y + b, fill=KEY, width=0)
        for x, y in ((ox + b, b), (ox + w - 2 * b, b), (ox + b, H - 2 * b), (ox + w - 2 * b, H - 2 * b)):
            c.create_rectangle(x, y, x + b, y + b, fill=ORANGE, width=0)
        # stepped tail pointing at the sprite: orange outline, then a hollow that opens into the bubble
        ty = max(6 * b, min(H - 6 * b, tail_y))
        col_x = lambda i: ox + w + i * b if tail_right else ox - (i + 1) * b  # i = -1 is the border column
        for i, hh in enumerate((7, 7, 5, 5, 3, 3, 1)):
            c.create_rectangle(col_x(i), ty - hh * b / 2, col_x(i) + b, ty + hh * b / 2, fill=ORANGE, width=0)
        for i, hh in enumerate((5, 5, 5, 3, 3, 1, 1), start=-1):
            c.create_rectangle(col_x(i), ty - hh * b / 2, col_x(i) + b, ty + hh * b / 2, fill=BG, width=0)
        self.box.place(x=ox + 2 * b, y=2 * b, width=w - 4 * b, height=H - 4 * b)

    def _grab(self, e):
        r = self.app.root
        self.grab = (e.x_root, e.y_root, self.win.winfo_x(), self.win.winfo_y(), r.winfo_x(), r.winfo_y())

    def _drag(self, e):
        x0, y0, wx, wy, rx, ry = self.grab
        dx, dy = e.x_root - x0, e.y_root - y0
        self.win.geometry(f'+{wx + dx}+{wy + dy}')
        self.app.root.geometry(f'+{rx + dx}+{ry + dy}')

    def _watch(self):
        """While open, reload when the session file changes (turns from the desktop app / terminal)."""
        try:
            if self.win.state() == 'normal' and not self.app.proc:
                f = session_file(self.app.cfg.get('session'))
                if f and f.stat().st_mtime != self.mtime:
                    self.reload()
        except Exception:
            pass  # a file mid-move; try again next tick
        finally:  # always reschedule, or live sync silently stops for good
            self.win.after(1500, self._watch)

    def reload(self):
        cfg = self.app.cfg
        self.head.config(text=f"{cfg.get('title') or '(새 세션)'}   ·   {self.app.session_dir()}")
        f = session_file(cfg.get('session'))
        if not f:
            self.tr = None
        elif not self.tr or self.tr.f != f:
            self.tr = Transcript(f)
        items = []
        if self.tr:
            self.mtime = f.stat().st_mtime
            self.tr.refresh()  # reads only what was appended since last time
            items = self.tr.items()[-80:]
        if (f, items) == self.shown:  # only bookkeeping records changed: leave the view alone
            return
        self.shown = (f, items)
        L = self.log
        at_end, top = L.yview()[1] > 0.98, L.yview()[0]
        L.config(state='normal')
        L.delete('1.0', 'end')
        L.config(state='disabled')
        self.last_role = None
        for role, text in items:
            self.add(role, text, scroll=False)
        if at_end:
            L.see('end')
        else:  # you scrolled up to read: stay there
            L.yview_moveto(top)

    def add(self, tag, text, scroll=True):
        L = self.log
        L.config(state='normal')
        role = 'user' if tag == 'user' else 'assistant'
        if role != self.last_role:
            L.insert('end', ('\n' if self.last_role else '') + ('나' if role == 'user' else 'Claude') + '\n',
                     'h_' + role)
            self.last_role = role
        L.insert('end', text.strip() + '\n', () if tag in ('user', 'assistant') else tag)
        L.config(state='disabled')
        if scroll:
            L.see('end')

    def _enter(self, e):
        if not e.state & 0x1:  # Shift+Enter = newline
            self.send()
            return 'break'

    def send(self):
        if self.app.proc and self.app.state == 'busy':  # the button reads 중지 only while answering
            return self.app.stop()
        text = self.inp.get('1.0', 'end').strip()
        if text:
            self.inp.delete('1.0', 'end')
            self.app.run(text)

    def busy(self, on):
        self.btn.config(text='중지' if on else '전송', bg='#6B6A65' if on else ORANGE)


class Quick:
    """One-line prompt in a chunky pixel frame."""

    def __init__(self, app):
        self.app = app
        w = self.win = tk.Toplevel(app.root)
        w.withdraw()
        w.overrideredirect(True)
        w.attributes('-topmost', True)
        w.attributes('-transparentcolor', KEY)
        w.configure(bg=KEY)
        self.cv = tk.Canvas(w, bg=KEY, highlightthickness=0)
        self.cv.pack()
        self.e = tk.Entry(w, bg='#1B1A18', fg=FG, insertbackground=ORANGE, bd=0,
                          font=(pixel_font(), 13), highlightthickness=0)
        self.side = 1  # which side of the sprite the box sits on (for its eyes)
        self.e.bind('<Return>', self.send)
        self.e.bind('<Escape>', lambda e: w.withdraw())
        self.e.bind('<FocusOut>', lambda e: w.after(200, self._hide_if_unfocused))
        self.e.bind('<Key>', lambda e: setattr(app, 'nod', 3), add='+')

    def show(self):
        W = int(self.app.root.winfo_screenwidth() * 0.26)
        H = int(self.app.S * 0.95)
        b = max(2, self.app.S // 16)  # one "pixel" of the frame
        c = self.cv
        c.config(width=W, height=H)
        c.delete('all')
        c.create_rectangle(0, 0, W, H, fill=ORANGE, width=0)
        c.create_rectangle(b, b, W - b, H - b, fill='#1B1A18', width=0)
        for x, y in ((0, 0), (W - b, 0), (0, H - b), (W - b, H - b)):  # cut corners
            c.create_rectangle(x, y, x + b, y + b, fill=KEY, width=0)
        for x, y in ((b, b), (W - 2 * b, b), (b, H - 2 * b), (W - 2 * b, H - 2 * b)):
            c.create_rectangle(x, y, x + b, y + b, fill=ORANGE, width=0)
        c.create_text(2.5 * b, H / 2, text='>', fill=ORANGE, anchor='w',
                      font=(pixel_font(), 13, 'bold'))
        self.e.place(x=4 * b, y=2 * b, width=W - 6 * b, height=H - 4 * b)
        x, _ = self.app.place_near(self.win, W, H)
        self.side = 1 if x > self.app.root.winfo_x() else -1
        self.app.nod = 5  # "oh!" hop as the box appears
        self.win.deiconify()
        self.win.lift()
        self.e.focus_force()

    def _hide_if_unfocused(self):
        try:
            if self.win.focus_get() is not self.e:
                self.win.withdraw()
        except KeyError:
            self.win.withdraw()

    def send(self, e=None):
        text = self.e.get().strip()
        if text:  # if Claude is still busy, run() queues it
            self.e.delete(0, 'end')
            self.win.withdraw()
            self.app.munch = 12  # gobbles the message, then trots off to work
            self.app.run(text)


class Settings:
    def __init__(self, app):
        self.app = app
        w = self.win = tk.Toplevel(app.root)
        w.title(f'{NAME} 설정')
        w.configure(bg=BG, padx=12, pady=12)
        w.attributes('-topmost', True)
        k = app.S / 64  # DPI scale
        w.geometry(f'{int(980 * k)}x{int(620 * k)}')

        top = tk.Frame(w, bg=BG)
        top.pack(fill='x')
        tk.Label(top, text='검색', bg=BG, fg=FG, font=('Segoe UI', 10, 'bold')).pack(side='left')
        self.q = tk.StringVar()
        ent = tk.Entry(top, textvariable=self.q, bg=BG2, fg=FG, insertbackground=FG, bd=0,
                       font=('Segoe UI', 11), highlightthickness=1, highlightcolor=ORANGE)
        ent.pack(side='left', fill='x', expand=True, padx=8, ipady=4)
        tk.Label(top, text='폴더별 · 더블클릭으로 선택 · ● 진행중', bg=BG, fg=DIM).pack(side='right')

        row = tk.Frame(w, bg=BG)
        row.pack(side='bottom', fill='x', pady=(6, 0))

        st = ttk.Style(w)
        st.theme_use('clam')
        st.configure('Treeview', background=BG2, fieldbackground=BG2, foreground=FG, borderwidth=0,
                     rowheight=int(26 * k), font=('Segoe UI', 10))
        st.configure('Treeview.Heading', background=BG, foreground=DIM, borderwidth=0)
        st.map('Treeview', background=[('selected', ORANGE)], foreground=[('selected', 'white')])
        t = self.tree = ttk.Treeview(w, columns=('last', 'src', 'when'), show='tree headings')
        for col, text, width in (('#0', '세션', 330), ('last', '최근 질문 / 경로', 380),
                                 ('src', '출처', 70), ('when', '시간', 100)):
            t.heading(col, text=text, anchor='w')
            t.column(col, width=int(width * k), anchor='w', stretch=col in ('#0', 'last'))
        t.tag_configure('folder', font=('Segoe UI', 10, 'bold'), foreground=ORANGE)
        t.pack(fill='both', expand=True, pady=6)
        t.bind('<Double-Button-1>', lambda e: self.ok() if t.focus() in self.by_id else None)
        ent.bind('<Return>', lambda e: self.ok())

        self.sessions = list_sessions()
        self.by_id = {s['id']: s for s in self.sessions}
        self.q.trace_add('write', lambda *a: self.fill())
        self.fill()
        ent.focus_set()
        tk.Label(row, text='권한 모드', bg=BG, fg=FG).pack(side='left')
        self.perm = ttk.Combobox(row, values=PERMS, state='readonly', width=18)
        self.perm.set(app.cfg['perm'])
        self.perm.pack(side='left', padx=8)
        for label, cmd in (('취소', w.destroy), ('선택', self.ok), ('폴더 골라서 새 세션…', self.new)):
            tk.Button(row, text=label, command=cmd, bg=BG2, fg=FG, bd=0, padx=14, pady=4).pack(side='right', padx=4)

    def fill(self):
        """Sessions grouped under their folder, newest folder first; search filters and expands."""
        t, q, now, cur = self.tree, self.q.get().strip().lower(), time.time(), self.app.cfg.get('session')
        owned = set(self.app.cfg.get('owned', []))
        t.delete(*t.get_children())
        groups = {}  # dict keeps insertion order = recency, since sessions come newest first
        for s in self.sessions:
            if not q or q in f"{s['title']} {s['last']} {s['cwd']}".lower():
                groups.setdefault(s['cwd'], []).append(s)
        for i, (cwd, ss) in enumerate(groups.items()):
            live = any(now - s['mtime'] < 120 for s in ss)
            node = t.insert('', 'end', text=f"{Path(cwd).name}  ({len(ss)}){'  ●' if live else ''}",
                            values=(cwd, '', ''), tags=('folder',),
                            open=bool(q) or i < 2 or any(s['id'] == cur for s in ss))
            for s in ss:
                t.insert(node, 'end', iid=s['id'],
                         text=('● ' if now - s['mtime'] < 120 else '    ') + s['title'],
                         values=(s['last'] if s['last'] != s['title'] else '',
                                 '픽셀' if s['id'] in owned else '데스크톱' if s['desktop'] else 'CLI',
                                 datetime.fromtimestamp(s['mtime']).strftime('%m-%d %H:%M')))
        if cur in self.by_id and t.exists(cur):
            t.selection_set(cur)
            t.focus(cur)
            t.see(t.parent(cur))  # keep its folder header in view too
            t.see(cur)
        elif q:  # first hit, so Enter picks it
            first = next((c for n in t.get_children() for c in t.get_children(n)), None)
            if first:
                t.selection_set(first)
                t.focus(first)

    def ok(self):
        self.app.save(perm=self.perm.get())
        s = self.by_id.get(self.tree.focus())
        if s:
            self.app.save(session=s['id'], cwd=s['cwd'], title=s['title'])
            self.app.chat.reload()
        self.win.destroy()

    def new(self):
        self.app.save(perm=self.perm.get())
        self.win.destroy()
        self.app.new_session()


if __name__ == '__main__':
    k32 = ctypes.WinDLL('kernel32', use_last_error=True)
    k32.CreateMutexW(None, False, f'Local\\{NAME}')
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS: one sprite is plenty
        sys.exit()
    sys.excepthook = log_error
    programs = Path(os.environ.get('APPDATA', '')) / 'Microsoft' / 'Windows' / 'Start Menu' / 'Programs'
    if not (programs / f'{NAME}.lnk').exists():  # so it can always be found in the Start menu
        threading.Thread(target=make_shortcut, args=('Programs',), daemon=True).start()
    app = App()
    app.root.report_callback_exception = log_error
    app.root.mainloop()
