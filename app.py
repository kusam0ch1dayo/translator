import ctypes
import threading
import tkinter as tk
from tkinter import ttk

from PIL import ImageGrab

SRC_LANG = "en"  # OCR・翻訳元
DST_LANG = "ja"  # 翻訳先

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # 座標を物理ピクセルに揃える
except Exception:
    pass


def ocr(img):
    import winocr
    return winocr.recognize_pil_sync(img, SRC_LANG)["text"]


def translate(text):
    from deep_translator import GoogleTranslator
    return GoogleTranslator(source=SRC_LANG, target=DST_LANG).translate(text)


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
        self.rect = self.canvas.create_rectangle(
            e.x, e.y, e.x, e.y, outline="red", width=3)

    def drag(self, e):
        x0 = self.start[0] - self.winfo_rootx()
        y0 = self.start[1] - self.winfo_rooty()
        self.canvas.coords(self.rect, x0, y0, e.x, e.y)

    def release(self, e):
        x0, y0 = self.start
        box = (min(x0, e.x_root), min(y0, e.y_root),
               max(x0, e.x_root), max(y0, e.y_root))
        self.finish(box if box[2] - box[0] > 5 and box[3] - box[1] > 5 else None)

    def finish(self, box):
        self.destroy()
        self.on_done(box)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("画面翻訳 (英語→日本語)")
        self.geometry("520x460")
        self.attributes("-topmost", True)
        ttk.Button(self, text="範囲を選択して翻訳", command=self.select).pack(
            fill="x", padx=8, pady=8)
        self.status = ttk.Label(self, text="ボタンを押して、翻訳したい範囲をドラッグしてください (Escで中止)")
        self.status.pack(anchor="w", padx=8)
        self.src = self.make_box("認識した文字")
        self.dst = self.make_box("翻訳結果")

    def make_box(self, title):
        frame = ttk.LabelFrame(self, text=title)
        frame.pack(fill="both", expand=True, padx=8, pady=4)
        box = tk.Text(frame, wrap="word", height=6, font=("Meiryo UI", 11))
        box.pack(fill="both", expand=True)
        return box

    def set_text(self, box, text):
        box.delete("1.0", "end")
        box.insert("1.0", text)

    def select(self):
        self.withdraw()
        self.after(200, lambda: Selector(self, self.captured))

    def captured(self, box):
        self.deiconify()
        if not box:
            return
        img = ImageGrab.grab(bbox=box, all_screens=True)
        self.status.config(text="認識・翻訳中...")
        threading.Thread(target=self.work, args=(img,), daemon=True).start()

    def work(self, img):
        try:
            text = ocr(img).strip()
            result = translate(text) if text else "(文字を認識できませんでした)"
            self.after(0, self.show, text, result, "完了")
        except Exception as e:
            self.after(0, self.show, "", f"エラー: {e}", "失敗")

    def show(self, text, result, status):
        self.set_text(self.src, text)
        self.set_text(self.dst, result)
        self.status.config(text=status)


if __name__ == "__main__":
    App().mainloop()
