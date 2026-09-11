"""Claude Code floating button for Windows.  Run:  pythonw claude_float.pyw

click       : pixel prompt box (long press does the same, without the wait)
double click: chat panel, popped out of the sprite
drag        : move
right click : menu (session select / new session / quit)
"""
import ctypes, json, queue, re, shutil, subprocess, threading, time, winsound
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, font as tkfont, ttk

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass

CFG_PATH = Path(__file__).with_suffix('.json')
PROJECTS = Path.home() / '.claude' / 'projects'
CLAUDE = shutil.which('claude') or 'claude'
ORANGE, BG, BG2, FG, DIM, KEY = '#D97757', '#262624', '#30302E', '#ECEBE6', '#9A9890', '#010203'
RING = {'busy': '#FFB454', 'done': '#5BB974', 'error': '#E5534B'}
PIX = {'B': ORANGE, 'L': '#E8A183', 'D': '#B9553A', 'E': '#2E2019'}
BODY = tuple((y, 1, 14) for y in range(1, 12))  # (row, x, width) — a flat, angular block
PERMS = ['default', 'acceptEdits', 'plan', 'bypassPermissions']
LONG_PRESS_MS = 450
TAIL = 7  # speech-bubble tail length, in frame pixels
WRAPPERS = re.compile(r'<(system-reminder|command-[\w-]+|local-command-[\w-]+|[\w-]*hook[\w-]*)>.*?</\1>', re.S)


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


def pixel_font(_cache=[]):
    """First installed bitmap-ish font, else Consolas."""
    if not _cache:
        have = set(tkfont.families())
        _cache.append(next((f for f in ('Galmuri11', 'DungGeunMo', 'Neo둥근모', 'Press Start 2P',
                                        'Consolas') if f in have), 'Consolas'))
    return _cache[0]


def session_file(sid):
    return next(PROJECTS.glob(f'*/{sid}.jsonl'), None) if sid else None


def list_sessions(limit=40):
    files = [f for f in PROJECTS.glob('*/*.jsonl') if 'observer-sessions' not in f.parent.name]
    files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
    out = []
    for f in files[:limit]:
        title = cwd = first = last = None
        with open(f, encoding='utf-8', errors='replace') as fh:
            for line in fh:
                if not ('"custom-title"' in line or '"last-prompt"' in line or '"type":"user"' in line
                        or (cwd is None and '"cwd"' in line)):
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if d.get('type') == 'custom-title':
                    title = d.get('customTitle')
                elif d.get('type') == 'last-prompt':
                    last = d.get('lastPrompt')
                cwd = cwd or d.get('cwd')
                if (m := msg_text(d)) and m[0] == 'user':  # latest real prompt = what's in progress
                    first = first or m[1]
                    last = m[1]
        if cwd:
            snip = lambda s: WRAPPERS.sub('', s or '').strip().replace('\n', ' ')[:70]
            out.append({'id': f.stem, 'cwd': cwd, 'title': title or snip(first) or f.stem,
                        'last': snip(last), 'mtime': f.stat().st_mtime})
    return out


class App:
    def __init__(self):
        try:
            self.cfg = json.loads(CFG_PATH.read_text(encoding='utf-8'))
        except Exception:
            self.cfg = {}
        self.cfg.setdefault('perm', 'default')
        self.state, self.tick, self.proc, self.q = 'idle', 0, None, queue.Queue()
        self.drawn = self.click_job = None
        self.present = self.dbl = self.nod = self.munch = 0

        r = self.root = tk.Tk()
        self.S = int(64 * r.winfo_fpixels('1i') / 96)
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
        if key == self.drawn:
            return
        self.drawn = key
        dy, blink, step, mood, look, mouth = key
        c, S, p = self.cv, self.S, self.S / 18  # 18-unit grid: headroom for the hop
        c.delete('all')

        def px(x, y, w=1, h=1, col='B'):
            x, y = x + 1 + look, y + dy + 2  # whole body leans toward the prompt box
            c.create_rectangle(x * p, y * p, (x + w) * p, (y + h) * p, fill=PIX[col], width=0)

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

        # status badge: pulses while busy, blinks when done / failed
        if mood != 'idle' and not (mood != 'busy' and (self.tick // 8) % 2):
            r, cx = (2.8 + 0.5 * step) * p, S - 3.2 * p
            c.create_oval(cx - r, cx - r, cx + r, cx + r, fill=RING[mood], outline='#FFF3EB',
                          width=max(1, p * 0.25))

    def set_state(self, s):
        self.state, self.tick, self.drawn = s, 0, None

    def _press(self, e):
        self.drag = (e.x_root, e.y_root, self.root.winfo_x(), self.root.winfo_y())
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
        self.quick.show()

    def _menu(self, e):
        m = tk.Menu(self.root, tearoff=0)
        m.add_command(label=f"세션: {self.cfg.get('title') or '(새 세션)'}"[:60], state='disabled')
        m.add_command(label=f"폴더: {self.cfg.get('cwd') or Path.home()}"[:60], state='disabled')
        m.add_separator()
        m.add_command(label='설정 / 세션 선택…', command=lambda: Settings(self))
        m.add_command(label='새 세션…', command=self.new_session)
        m.add_separator()
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
        CFG_PATH.write_text(json.dumps(self.cfg, ensure_ascii=False, indent=1), encoding='utf-8')

    def new_session(self):
        d = filedialog.askdirectory(parent=self.root, title='새 세션 작업 폴더',
                                    initialdir=self.cfg.get('cwd') or str(Path.home()))
        if d:
            self.save(session=None, cwd=str(Path(d)), title=None)
            self.chat.reload()

    # ---- claude process --------------------------------------------------
    def run(self, prompt):
        if self.proc:
            return
        cwd = self.cfg.get('cwd')
        if not cwd or not Path(cwd).is_dir():
            cwd = str(Path.home())
        args = [CLAUDE, '-p', '--output-format', 'stream-json', '--verbose',
                '--permission-mode', self.cfg['perm']]
        if self.cfg.get('session'):
            args += ['--resume', self.cfg['session']]
        self.chat.add('user', prompt)
        try:
            self.proc = subprocess.Popen(args, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.STDOUT, text=True, encoding='utf-8',
                                         errors='replace', creationflags=subprocess.CREATE_NO_WINDOW)
        except OSError as ex:
            self.chat.add('err', f'claude 실행 실패: {ex}')
            return self.set_state('error')
        self.set_state('busy')
        self.chat.busy(True)
        threading.Thread(target=self._reader, args=(self.proc, prompt), daemon=True).start()

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
            return
        t = d.get('type')
        if d.get('session_id') and d['session_id'] != self.cfg.get('session'):
            self.save(session=d['session_id'])
        if t == 'assistant':
            for x in d['message'].get('content', []):
                if x.get('type') == 'text' and x['text'].strip():
                    self.chat.add('assistant', x['text'])
                elif x.get('type') == 'tool_use':
                    self.chat.add('tool', tool_line(x))
        elif t == 'result':
            if d.get('is_error'):
                self.chat.add('err', str(d.get('result') or d.get('subtype')))
            self.set_state('error' if d.get('is_error') else 'done')

    def _loop(self):
        while True:
            try:
                self._handle(*self.q.get_nowait())
            except queue.Empty:
                break
        self.tick += 1
        self._draw()
        self.root.after(50, self._loop)

    def quit(self):
        self.stop()
        self.root.destroy()


class Chat:
    """Chat panel drawn as a pixel speech bubble whose tail points at the sprite."""

    def __init__(self, app):
        self.app, self.last_role, self.mtime = app, None, None
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
        for wid in (self.head, self.cv):  # drag the bubble around by its header / frame
            wid.bind('<ButtonPress-1>', lambda e: setattr(self, 'grab', (e.x_root - w.winfo_x(), e.y_root - w.winfo_y())))
            wid.bind('<B1-Motion>', lambda e: w.geometry(f'+{e.x_root - self.grab[0]}+{e.y_root - self.grab[1]}'))
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

    def _watch(self):
        """While open, reload when the session file changes (turns from the desktop app / terminal)."""
        if self.win.state() == 'normal' and not self.app.proc:
            f = session_file(self.app.cfg.get('session'))
            if f and f.stat().st_mtime != self.mtime:
                self.reload()
        self.win.after(1500, self._watch)

    def reload(self):
        cfg = self.app.cfg
        self.head.config(text=f"{cfg.get('title') or '(새 세션)'}   ·   {cfg.get('cwd') or Path.home()}")
        self.log.config(state='normal')
        self.log.delete('1.0', 'end')
        self.log.config(state='disabled')
        self.last_role = None
        f = session_file(cfg.get('session'))
        if not f:
            return
        self.mtime = f.stat().st_mtime
        msgs = []
        with open(f, encoding='utf-8', errors='replace') as fh:
            for line in fh:
                if '"type":"user"' in line or '"type":"assistant"' in line:
                    try:
                        d = json.loads(line)
                    except ValueError:
                        continue
                    if m := msg_text(d):
                        msgs.append(m)
                    if d.get('type') == 'assistant' and not d.get('isSidechain'):
                        msgs += [('tool', tool_line(x)) for x in d['message'].get('content', [])
                                 if isinstance(x, dict) and x.get('type') == 'tool_use']
        for role, text in msgs[-80:]:
            self.add(role, text)

    def add(self, tag, text):
        L = self.log
        L.config(state='normal')
        role = 'user' if tag == 'user' else 'assistant'
        if role != self.last_role:
            L.insert('end', ('\n' if self.last_role else '') + ('나' if role == 'user' else 'Claude') + '\n',
                     'h_' + role)
            self.last_role = role
        L.insert('end', text.strip() + '\n', () if tag in ('user', 'assistant') else tag)
        L.config(state='disabled')
        L.see('end')

    def _enter(self, e):
        if not e.state & 0x1:  # Shift+Enter = newline
            self.send()
            return 'break'

    def send(self):
        if self.app.proc:
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
        if self.win.state() == 'normal':
            return self.win.withdraw()
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
        if text and not self.app.proc:
            self.e.delete(0, 'end')
            self.win.withdraw()
            self.app.munch = 12  # gobbles the message, then trots off to work
            self.app.run(text)


class Settings:
    def __init__(self, app):
        self.app = app
        w = self.win = tk.Toplevel(app.root)
        w.title('Claude Float 설정')
        w.configure(bg=BG, padx=12, pady=12)
        w.attributes('-topmost', True)
        tk.Label(w, text='사용할 세션 (최근 수정순, 더블클릭으로 선택)', bg=BG, fg=FG,
                 font=('Segoe UI', 10, 'bold')).pack(anchor='w')
        frm = tk.Frame(w, bg=BG)
        frm.pack(fill='both', expand=True, pady=6)
        sb = ttk.Scrollbar(frm)
        sb.pack(side='right', fill='y')
        self.lb = tk.Listbox(frm, width=100, height=20, bg=BG2, fg=FG, bd=0, highlightthickness=0,
                             selectbackground=ORANGE, font=('Segoe UI', 10), yscrollcommand=sb.set)
        self.lb.pack(fill='both', expand=True)
        sb.config(command=self.lb.yview)
        self.lb.bind('<Double-Button-1>', lambda e: self.ok())

        self.sessions = list_sessions()
        now = time.time()
        for i, s in enumerate(self.sessions):
            when = datetime.fromtimestamp(s['mtime']).strftime('%m-%d %H:%M')
            live = '● 진행중' if now - s['mtime'] < 120 else '       '
            recent = f"   ›  {s['last']}" if s['last'] and s['last'] != s['title'] else ''
            self.lb.insert('end', f"{live}  {when}   [{Path(s['cwd']).name}]   {s['title']}{recent}")
            if s['id'] == app.cfg.get('session'):
                self.lb.selection_set(i)
                self.lb.see(i)

        row = tk.Frame(w, bg=BG)
        row.pack(fill='x', pady=(6, 0))
        tk.Label(row, text='권한 모드', bg=BG, fg=FG).pack(side='left')
        self.perm = ttk.Combobox(row, values=PERMS, state='readonly', width=18)
        self.perm.set(app.cfg['perm'])
        self.perm.pack(side='left', padx=8)
        for label, cmd in (('취소', w.destroy), ('선택', self.ok), ('새 세션…', self.new)):
            tk.Button(row, text=label, command=cmd, bg=BG2, fg=FG, bd=0, padx=14, pady=4).pack(side='right', padx=4)

    def ok(self):
        sel = self.lb.curselection()
        self.app.save(perm=self.perm.get())
        if sel:
            s = self.sessions[sel[0]]
            self.app.save(session=s['id'], cwd=s['cwd'], title=s['title'])
            self.app.chat.reload()
        self.win.destroy()

    def new(self):
        self.app.save(perm=self.perm.get())
        self.win.destroy()
        self.app.new_session()


if __name__ == '__main__':
    App().root.mainloop()
