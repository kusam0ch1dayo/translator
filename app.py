import csv
import ctypes
import ctypes.wintypes
import json
import os
import queue
import re
import sqlite3
import sys
import threading
import time
import tkinter as tk
from datetime import datetime
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageGrab

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # 座標を物理ピクセルに揃える
except Exception:
    pass

# 表示名: (Google翻訳, Windows OCR, MyMemory)
LANGS = {
    "English": ("en", "en", "en-US"),
    "日本語": ("ja", "ja", "ja-JP"),
    "中国語(簡体)": ("zh-CN", "zh-Hans-CN", "zh-CN"),
    "韓国語": ("ko", "ko", "ko-KR"),
    "フランス語": ("fr", "fr", "fr-FR"),
    "ドイツ語": ("de", "de", "de-DE"),
    "スペイン語": ("es", "es", "es-ES"),
}

DATA_DIR = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "ScreenTranslator")
SETTINGS_PATH = os.path.join(DATA_DIR, "settings.json")
DB_PATH = os.path.join(DATA_DIR, "data.db")

DEFAULTS = {
    "src": "English",
    "dst": "日本語",
    "hotkey_select": "Ctrl+Alt+T",
    "hotkey_repeat": "Ctrl+Alt+R",
    "popup": True,
    "popup_sec": 8,  # 0ならクリックするまで表示
}

MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x4000
WM_HOTKEY, WM_QUIT = 0x0312, 0x0012
HOTKEY_IDS = {1: "hotkey_select", 2: "hotkey_repeat"}


# ---------- 設定 ----------
def load_settings():
    settings = dict(DEFAULTS)
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            settings.update(json.load(f))
    except Exception:
        pass
    for key in ("src", "dst"):
        if settings[key] not in LANGS:
            settings[key] = DEFAULTS[key]
    return settings


def save_settings(settings):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)


# ---------- 履歴・キャッシュ ----------
class Store:
    def __init__(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        self.run("""CREATE TABLE IF NOT EXISTS cache(
            src TEXT, dst TEXT, text TEXT, result TEXT, PRIMARY KEY(src, dst, text))""")
        self.run("""CREATE TABLE IF NOT EXISTS history(
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, src TEXT, dst TEXT,
            original TEXT, translated TEXT)""")

    def run(self, sql, args=()):
        db = sqlite3.connect(DB_PATH)
        try:
            with db:
                return db.execute(sql, args).fetchall()
        finally:
            db.close()

    def cached(self, src, dst, text):
        rows = self.run("SELECT result FROM cache WHERE src=? AND dst=? AND text=?", (src, dst, text))
        return rows[0][0] if rows else None

    def put_cache(self, src, dst, text, result):
        self.run("INSERT OR REPLACE INTO cache VALUES(?,?,?,?)", (src, dst, text, result))

    def add_history(self, src, dst, original, translated):
        self.run("INSERT INTO history(ts, src, dst, original, translated) VALUES(?,?,?,?,?)",
                 (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), src, dst, original, translated))

    def search_history(self, query=""):
        like = f"%{query}%"
        return self.run("""SELECT id, ts, src, dst, original, translated FROM history
            WHERE original LIKE ? OR translated LIKE ? ORDER BY id DESC LIMIT 1000""", (like, like))

    def clear_history(self):
        self.run("DELETE FROM history")


# ---------- OCR・翻訳 ----------
CJK = "　-鿿＀-￯"


def ocr(img, lang):
    import winocr
    try:
        text = winocr.recognize_pil_sync(img, LANGS[lang][1])["text"]
    except Exception as e:
        raise RuntimeError(
            f"{lang}の文字認識を使えません。Windowsの言語設定で{lang}の言語パックを追加してください ({e})")
    text = " ".join(text.split())  # 改行を除いて文として扱う
    return re.sub(f"(?<=[{CJK}]) (?=[{CJK}])", "", text)  # 日本語・中国語の文字間の空白を除く


def translate(store, text, src, dst):
    from deep_translator import GoogleTranslator, MyMemoryTranslator
    hit = store.cached(src, dst, text)
    if hit:
        return hit
    result = None
    for wait in (0, 1.5):  # Googleを最大2回試す
        time.sleep(wait)
        try:
            result = GoogleTranslator(source=LANGS[src][0], target=LANGS[dst][0]).translate(text)
            if result:
                break
        except Exception:
            pass
    if not result:  # 制限された場合は別サービスに切り替える (1回500文字まで)
        tr = MyMemoryTranslator(source=LANGS[src][2], target=LANGS[dst][2])
        result = "".join(tr.translate(text[i:i + 450]) for i in range(0, len(text), 450))
    store.put_cache(src, dst, text, result)
    return result


# ---------- ホットキー ----------
def parse_hotkey(text):
    *mod_names, key = text.split("+")
    mods = MOD_NOREPEAT
    for name in mod_names:
        mods |= {"Ctrl": MOD_CONTROL, "Alt": MOD_ALT, "Shift": MOD_SHIFT}[name]
    vk = ord(key) if len(key) == 1 else 0x70 + int(key[1:]) - 1  # F1=0x70
    return mods, vk


class HotkeyThread(threading.Thread):
    """グローバルホットキーを登録し、押されたらキューに通知する。"""

    def __init__(self, specs, events):
        super().__init__(daemon=True)
        self.specs, self.events = specs, events
        self.tid = None
        self.ready = threading.Event()

    def run(self):
        user32 = ctypes.windll.user32
        msg = ctypes.wintypes.MSG()
        user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)  # メッセージキューを作る
        self.tid = ctypes.windll.kernel32.GetCurrentThreadId()
        self.ready.set()
        for hid, mods, vk in self.specs:
            if not user32.RegisterHotKey(None, hid, mods, vk):
                self.events.put(("failed", hid))
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY:
                self.events.put(("hotkey", msg.wParam))
        for hid, _, _ in self.specs:
            user32.UnregisterHotKey(None, hid)

    def stop(self):
        if self.ready.wait(1):
            ctypes.windll.user32.PostThreadMessageW(self.tid, WM_QUIT, 0, 0)
        self.join(1)


# ---------- 画面部品 ----------
class Selector(tk.Toplevel):
    """画面全体を覆い、ドラッグで矩形を選ぶ。"""

    def __init__(self, master, on_done):
        super().__init__(master)
        self.on_done = on_done
        self.attributes("-fullscreen", True, "-topmost", True, "-alpha", 0.3)
        self.configure(bg="black", cursor="crosshair")
        self.canvas = tk.Canvas(self, bg="black", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.start = None
        self.rect = None
        self.canvas.bind("<ButtonPress-1>", self.press)
        self.canvas.bind("<B1-Motion>", self.drag)
        self.canvas.bind("<ButtonRelease-1>", self.release)
        self.bind("<Escape>", lambda e: self.finish(None))
        self.focus_force()

    def press(self, e):
        self.start = (e.x_root, e.y_root)
        self.rect = self.canvas.create_rectangle(e.x, e.y, e.x, e.y, outline="red", width=3)

    def drag(self, e):
        x0 = self.start[0] - self.winfo_rootx()
        y0 = self.start[1] - self.winfo_rooty()
        self.canvas.coords(self.rect, x0, y0, e.x, e.y)

    def release(self, e):
        x0, y0 = self.start
        box = (min(x0, e.x_root), min(y0, e.y_root), max(x0, e.x_root), max(y0, e.y_root))
        self.finish(box if box[2] - box[0] > 5 and box[3] - box[1] > 5 else None)

    def finish(self, box):
        self.destroy()
        self.on_done(box)


class Popup(tk.Toplevel):
    """選択範囲のすぐ下(入らなければ上)に訳文を表示する。クリックで閉じる。"""

    def __init__(self, master, box, text, seconds):
        super().__init__(master)
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.configure(bg="#333333")
        wrap = max(300, min(box[2] - box[0], 700))
        label = tk.Label(self, text=text, wraplength=wrap, justify="left", bg="#fffbe6",
                         fg="#111111", font=("Meiryo UI", 11), padx=10, pady=6)
        label.pack(padx=1, pady=1)
        self.update_idletasks()
        width, height = self.winfo_reqwidth(), self.winfo_reqheight()
        screen_w, screen_h = self.winfo_screenwidth(), self.winfo_screenheight()
        x = max(0, min(box[0], screen_w - width))
        y = box[3] + 8
        if y + height > screen_h:
            y = max(0, box[1] - height - 8)
        self.geometry(f"+{x}+{y}")
        for widget in (self, label):
            widget.bind("<Button-1>", lambda e: self.destroy())
        if seconds > 0:
            self.after(seconds * 1000, self.destroy)


class HistoryWindow(tk.Toplevel):
    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.title("翻訳履歴")
        self.geometry("820x420")
        top = ttk.Frame(self, padding=6)
        top.pack(fill="x")
        ttk.Label(top, text="検索:").pack(side="left")
        self.query = tk.StringVar()
        self.query.trace_add("write", lambda *a: self.refresh())
        ttk.Entry(top, textvariable=self.query).pack(side="left", fill="x", expand=True, padx=6)

        cols = (("ts", "日時", 130), ("lang", "言語", 130), ("original", "原文", 270), ("translated", "訳文", 270))
        self.tree = ttk.Treeview(self, columns=[c[0] for c in cols], show="headings", selectmode="browse")
        for key, title, width in cols:
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width)
        scroll = ttk.Scrollbar(self, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        bottom = ttk.Frame(self, padding=6)
        bottom.pack(side="bottom", fill="x")
        scroll.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True)
        for text, cmd in (("訳文をコピー", lambda: self.copy(3)), ("原文をコピー", lambda: self.copy(2)),
                          ("CSV出力", self.export), ("履歴を全削除", self.clear)):
            ttk.Button(bottom, text=text, command=cmd).pack(side="left", padx=4)
        self.refresh()

    def refresh(self):
        self.tree.delete(*self.tree.get_children())
        for id_, ts, src, dst, original, translated in self.app.store.search_history(self.query.get()):
            self.tree.insert("", "end", iid=str(id_), values=(ts, f"{src}→{dst}", original, translated))

    def copy(self, index):
        selected = self.tree.selection()
        if selected:
            self.clipboard_clear()
            self.clipboard_append(self.tree.item(selected[0], "values")[index])

    def export(self):
        path = filedialog.asksaveasfilename(parent=self, defaultextension=".csv",
                                            filetypes=[("CSV", "*.csv")], initialfile="translation_history.csv")
        if not path:
            return
        with open(path, "w", newline="", encoding="utf-8-sig") as f:  # Excelで文字化けしない
            writer = csv.writer(f)
            writer.writerow(["日時", "翻訳元", "翻訳先", "原文", "訳文"])
            for _, ts, src, dst, original, translated in self.app.store.search_history(self.query.get()):
                writer.writerow([ts, src, dst, original, translated])
        messagebox.showinfo("CSV出力", "保存しました。", parent=self)

    def clear(self):
        if messagebox.askyesno("確認", "履歴をすべて削除しますか?", parent=self):
            self.app.store.clear_history()
            self.refresh()


class SettingsDialog(tk.Toplevel):
    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.title("設定")
        self.resizable(False, False)
        self.transient(app)
        s = app.settings
        self.src = tk.StringVar(value=s["src"])
        self.dst = tk.StringVar(value=s["dst"])
        self.hk_select = tk.StringVar(value=s["hotkey_select"])
        self.hk_repeat = tk.StringVar(value=s["hotkey_repeat"])
        self.popup = tk.BooleanVar(value=s["popup"])
        self.popup_sec = tk.IntVar(value=s["popup_sec"])

        frame = ttk.Frame(self, padding=12)
        frame.pack()
        names = list(LANGS)
        ttk.Label(frame, text="翻訳元").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Combobox(frame, textvariable=self.src, values=names, state="readonly", width=14).grid(row=0, column=1)
        ttk.Button(frame, text="⇄", width=3, command=self.swap).grid(row=0, column=2, padx=6)
        ttk.Label(frame, text="翻訳先").grid(row=0, column=3, sticky="w")
        ttk.Combobox(frame, textvariable=self.dst, values=names, state="readonly", width=14).grid(row=0, column=4)

        for row, (label, var) in enumerate((("範囲選択のショートカット", self.hk_select),
                                            ("前回範囲を再翻訳のショートカット", self.hk_repeat)), start=1):
            ttk.Label(frame, text=label).grid(row=row, column=0, columnspan=2, sticky="w", pady=4)
            entry = ttk.Entry(frame, textvariable=var, width=16, justify="center")
            entry.grid(row=row, column=3, columnspan=2, sticky="w")
            entry.bind("<KeyPress>", lambda e, v=var: self.capture(e, v))
        ttk.Label(frame, text="※ 欄をクリックして、割り当てたいキーを押します (Ctrl か Alt を含める)",
                  foreground="gray").grid(row=3, column=0, columnspan=5, sticky="w")

        ttk.Checkbutton(frame, text="訳文を選択範囲の近くにポップアップ表示する", variable=self.popup).grid(
            row=4, column=0, columnspan=5, sticky="w", pady=(10, 2))
        ttk.Label(frame, text="表示秒数 (0=クリックするまで)").grid(row=5, column=0, columnspan=3, sticky="w")
        ttk.Spinbox(frame, from_=0, to=60, textvariable=self.popup_sec, width=5).grid(row=5, column=3, sticky="w")

        buttons = ttk.Frame(frame)
        buttons.grid(row=6, column=0, columnspan=5, pady=(14, 0))
        ttk.Button(buttons, text="保存", command=self.save).pack(side="left", padx=6)
        ttk.Button(buttons, text="キャンセル", command=self.destroy).pack(side="left", padx=6)
        self.grab_set()

    def swap(self):
        src, dst = self.src.get(), self.dst.get()
        self.src.set(dst)
        self.dst.set(src)

    @staticmethod
    def capture(event, var):
        key = event.keysym
        if len(key) == 1 and key.isalnum():
            key = key.upper()
        elif not re.fullmatch(r"F([1-9]|1[0-2])", key):
            return "break"
        mods = [name for name, bit in (("Ctrl", 0x4), ("Alt", 0x20000), ("Shift", 0x1)) if event.state & bit]
        if "Ctrl" in mods or "Alt" in mods:
            var.set("+".join(mods + [key]))
        return "break"

    def save(self):
        if self.hk_select.get() == self.hk_repeat.get():
            messagebox.showerror("設定", "2つのショートカットは別のキーにしてください。", parent=self)
            return
        try:
            seconds = max(0, min(60, int(self.popup_sec.get())))
        except (tk.TclError, ValueError):
            seconds = DEFAULTS["popup_sec"]
        self.app.apply_settings({
            "src": self.src.get(), "dst": self.dst.get(),
            "hotkey_select": self.hk_select.get(), "hotkey_repeat": self.hk_repeat.get(),
            "popup": self.popup.get(), "popup_sec": seconds,
        })
        self.destroy()


# ---------- メインウィンドウ ----------
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("画面翻訳")
        self.geometry("560x500")
        self.attributes("-topmost", True)
        self.settings = load_settings()
        self.store = Store()
        self.events = queue.Queue()
        self.hotkey_thread = None
        self.busy = False
        self.last_box = None
        self.prev_state = "normal"
        self.popup = None
        self.history_win = None

        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=8, pady=8)
        ttk.Button(bar, text="範囲を選択して翻訳", command=self.select).pack(side="left", fill="x", expand=True)
        for text, cmd in (("前回の範囲", self.repeat), ("履歴", self.open_history), ("設定", self.open_settings)):
            ttk.Button(bar, text=text, command=cmd).pack(side="left", padx=(6, 0))
        self.info = ttk.Label(self)
        self.info.pack(anchor="w", padx=8)
        self.status = ttk.Label(self, foreground="gray")
        self.status.pack(anchor="w", padx=8)
        self.src_box = self.make_box("認識した文字")
        self.dst_box = self.make_box("翻訳結果")

        self.refresh_info()
        self.start_hotkeys()
        self.after(100, self.poll)

    def make_box(self, title):
        frame = ttk.LabelFrame(self, text=title)
        frame.pack(fill="both", expand=True, padx=8, pady=4)
        box = tk.Text(frame, wrap="word", height=6, font=("Meiryo UI", 11))
        box.pack(fill="both", expand=True)
        return box

    def set_text(self, box, text):
        box.delete("1.0", "end")
        box.insert("1.0", text)

    def refresh_info(self):
        s = self.settings
        self.info.config(text=f"{s['src']} → {s['dst']}　　"
                              f"選択: {s['hotkey_select']} / 再翻訳: {s['hotkey_repeat']}")

    # --- 設定・ホットキー ---
    def open_settings(self):
        SettingsDialog(self)

    def apply_settings(self, new):
        self.settings.update(new)
        save_settings(self.settings)
        self.refresh_info()
        self.start_hotkeys()
        self.status.config(text="設定を保存しました")

    def start_hotkeys(self):
        if self.hotkey_thread:
            self.hotkey_thread.stop()
            self.hotkey_thread = None
        if sys.platform != "win32":
            return
        specs = []
        for hid, name in HOTKEY_IDS.items():
            try:
                specs.append((hid, *parse_hotkey(self.settings[name])))
            except Exception:
                pass
        self.hotkey_thread = HotkeyThread(specs, self.events)
        self.hotkey_thread.start()

    def poll(self):
        try:
            while True:
                kind, *args = self.events.get_nowait()
                if kind == "hotkey":
                    (self.select if args[0] == 1 else self.repeat)()
                elif kind == "failed":
                    key = self.settings[HOTKEY_IDS[args[0]]]
                    self.status.config(text=f"ショートカット {key} を登録できません (他のアプリが使用中)")
                elif kind == "result":
                    self.show(*args)
        except queue.Empty:
            pass
        self.after(100, self.poll)

    # --- 履歴 ---
    def open_history(self):
        if self.history_win and self.history_win.winfo_exists():
            self.history_win.refresh()
            self.history_win.lift()
        else:
            self.history_win = HistoryWindow(self)

    # --- 撮影 → OCR → 翻訳 ---
    def begin_capture(self):
        self.busy = True
        self.prev_state = self.state()
        self.withdraw()
        if self.popup and self.popup.winfo_exists():
            self.popup.destroy()

    def end_capture(self):
        self.busy = False
        if self.prev_state == "normal":
            self.deiconify()
        elif self.prev_state == "iconic":
            self.iconify()

    def select(self):
        if self.busy:
            return
        self.begin_capture()
        self.after(200, lambda: Selector(self, self.selected))

    def selected(self, box):
        if not box:
            self.end_capture()
            return
        self.last_box = box
        self.after(300, self.grab, box)  # オーバーレイが消えるのを待つ

    def repeat(self):
        if self.busy:
            return
        if not self.last_box:
            self.status.config(text="先に範囲を選択して翻訳してください")
            return
        self.begin_capture()
        self.after(250, self.grab, self.last_box)

    def grab(self, box):
        img = ImageGrab.grab(bbox=box, all_screens=True)
        self.end_capture()
        if img.height < 100:  # 小さい文字はOCRしにくいので拡大
            scale = 100 / img.height
            img = img.resize((int(img.width * scale), 100), Image.LANCZOS)
        self.status.config(text="認識・翻訳中...")
        threading.Thread(target=self.work, args=(img, box, self.settings["src"], self.settings["dst"]),
                         daemon=True).start()

    def work(self, img, box, src, dst):
        try:
            text = ocr(img, src)
            if not text:
                self.events.put(("result", "", "(文字を認識できませんでした)", "認識失敗", box))
                return
            result = translate(self.store, text, src, dst)
            self.store.add_history(src, dst, text, result)
            self.events.put(("result", text, result, "完了", box))
        except Exception as e:
            self.events.put(("result", "", f"エラー: {e}", "失敗", box))

    def show(self, text, result, status, box):
        self.set_text(self.src_box, text)
        self.set_text(self.dst_box, result)
        self.status.config(text=status)
        if self.settings["popup"]:
            self.popup = Popup(self, box, result, self.settings["popup_sec"])
        if self.history_win and self.history_win.winfo_exists():
            self.history_win.refresh()

    def destroy(self):
        if self.hotkey_thread:
            self.hotkey_thread.stop()
        super().destroy()


if __name__ == "__main__":
    App().mainloop()
