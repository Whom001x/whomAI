import base64
import ctypes
from ctypes import wintypes
import json
import mimetypes
import os
import queue
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path
import tkinter as tk
from tkinter import colorchooser, filedialog, messagebox, scrolledtext
from PIL import ImageGrab


APP_NAME = "Floating AI Chat"
APP_DIR = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
CONFIG_PATH = APP_DIR / "app_config.json"
TEMP_DIR = APP_DIR / "temp"
MAX_IMAGE_BYTES = 20 * 1024 * 1024
RESIZE_BORDER = 6
TRANSPARENT_COLOR = "#010203"
SHORT_SYSTEM_PROMPT = "你是一个中文桌面 AI 助手。默认用 1-3 句回答，只说结论和最关键步骤；除非用户要求详细说明，否则不要展开长列表。"
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
PM_REMOVE = 0x0001
HOTKEY_ID = 1001
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("message", wintypes.UINT),
        ("wParam", wintypes.WPARAM),
        ("lParam", wintypes.LPARAM),
        ("time", wintypes.DWORD),
        ("pt", POINT),
    ]


DEFAULT_CONFIG = {
    "api_key": "",
    "model": "gpt-5.5",
    "base_url": "https://api.openai.com/v1/responses",
    "system_prompt": SHORT_SYSTEM_PROMPT,
    "opacity": 1.0,
    "transparent_opacity": 0.55,
    "input_color": "#f9fafb",
    "transparent_input_color": "#111827",
    "max_output_tokens": 1200,
    "pinned": True,
    "transparent_mode": False,
    "hotkey": "Ctrl+Alt+A",
}


def load_config():
    config = DEFAULT_CONFIG.copy()
    if CONFIG_PATH.exists():
        try:
            saved = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                config.update(saved)
        except Exception:
            pass
    return config


def save_config(config):
    CONFIG_PATH.write_text(
        json.dumps(config, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def extract_response_text(payload):
    if isinstance(payload, dict):
        text = payload.get("output_text")
        if isinstance(text, str) and text.strip():
            return text.strip()

        parts = []
        for item in payload.get("output", []) or []:
            for content in item.get("content", []) or []:
                if isinstance(content, dict):
                    value = content.get("text") or content.get("output_text")
                    if isinstance(value, str):
                        parts.append(value)
        if parts:
            return "\n".join(parts).strip()
    return json.dumps(payload, ensure_ascii=False, indent=2)


class FloatingAIChat:
    def __init__(self, root):
        self.root = root
        self.config = load_config()
        self.messages = []
        self.result_queue = queue.Queue()
        self.drag_start = None
        self.sending = False
        self.pinned = bool(self.config.get("pinned", True))
        self.transparent_mode = bool(self.config.get("transparent_mode", False))
        self.selected_image_path = None
        self.resize_start = None
        self.color_pick_target = None
        self.hotkey_registered = False
        self.hotkey_thread = None
        self.hotkey_thread_id = None

        self.root.title(APP_NAME)
        self.root.geometry("430x580+980+150")
        self.root.minsize(280, 300)
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", self.pinned)
        self.root.attributes("-alpha", float(self.config.get("opacity", 1.0)))
        self.root.configure(bg="#111827")
        self.root.bind("<Map>", self.restore_borderless)
        self.root.bind("<Motion>", self.update_resize_cursor)
        self.root.bind("<ButtonPress-1>", self.start_resize)
        self.root.bind("<B1-Motion>", self.resize_window)
        self.root.bind("<ButtonRelease-1>", self.stop_resize)

        self.build_ui()
        self.update_pin_ui()
        self.apply_transparent_mode()
        self.register_hotkey()
        self.root.after(120, self.poll_queue)
        self.root.after(800, self.enforce_pin)
        self.root.protocol("WM_DELETE_WINDOW", self.close_app)

    def build_ui(self):
        self.shell = tk.Frame(self.root, bg="#111827")
        self.shell.pack(fill=tk.BOTH, expand=True, padx=RESIZE_BORDER, pady=RESIZE_BORDER)

        self.header = tk.Frame(self.shell, bg="#111827", height=38)
        self.header.pack(fill=tk.X)
        self.header.bind("<ButtonPress-1>", self.start_drag)
        self.header.bind("<B1-Motion>", self.drag)

        self.title_label = tk.Label(
            self.header,
            text="悬浮 AI",
            bg="#111827",
            fg="#f9fafb",
            font=("Microsoft YaHei UI", 11, "bold"),
        )
        self.title_label.pack(side=tk.LEFT, padx=(12, 4))
        self.title_label.bind("<ButtonPress-1>", self.start_drag)
        self.title_label.bind("<B1-Motion>", self.drag)

        self.status = tk.Label(
            self.header,
            text="就绪",
            bg="#111827",
            fg="#9ca3af",
            font=("Microsoft YaHei UI", 9),
        )
        self.status.pack(side=tk.LEFT, padx=8)

        self.menu_btn = tk.Button(
            self.header,
            text="菜单",
            command=self.open_menu,
            bg="#1f2937",
            fg="#f9fafb",
            bd=0,
            width=4,
        )
        self.menu_btn.pack(side=tk.RIGHT, padx=(0, 8), pady=6)

        self.body = tk.Frame(self.shell, bg="#0f172a")
        self.body.pack(fill=tk.BOTH, expand=True, padx=1, pady=(0, 1))
        self.body.columnconfigure(0, weight=1)
        self.body.rowconfigure(0, weight=1)
        self.body.rowconfigure(1, weight=0)

        self.chat = scrolledtext.ScrolledText(
            self.body,
            wrap=tk.WORD,
            state=tk.DISABLED,
            bg="#111827",
            fg="#e5e7eb",
            insertbackground="#ffffff",
            relief=tk.FLAT,
            font=("Microsoft YaHei UI", 10),
            padx=10,
            pady=10,
        )
        self.chat.grid(row=0, column=0, sticky="nsew", padx=10, pady=(10, 8))

        self.bottom_bar = tk.Frame(self.body, bg="#0f172a")
        self.bottom_bar.grid(row=1, column=0, sticky="ew", padx=10, pady=(0, 10))
        self.bottom_bar.columnconfigure(0, weight=1)

        self.attach_bar = tk.Frame(self.bottom_bar, bg="#0f172a")
        self.attach_bar.pack(fill=tk.X, pady=(0, 8))

        self.image_label = tk.Label(
            self.attach_bar,
            text="未选择图片",
            bg="#0f172a",
            fg="#9ca3af",
            anchor="w",
            font=("Microsoft YaHei UI", 9),
        )
        self.image_label.pack(side=tk.LEFT, fill=tk.X, expand=True)

        self.image_btn = tk.Button(
            self.attach_bar,
            text="图片",
            command=self.choose_image,
            bg="#374151",
            fg="#ffffff",
            bd=0,
            width=5,
        )
        self.image_btn.pack(side=tk.RIGHT, padx=(6, 0))

        self.clear_image_btn = tk.Button(
            self.attach_bar,
            text="清图",
            command=self.clear_image,
            bg="#4b5563",
            fg="#ffffff",
            bd=0,
            width=5,
        )
        self.clear_image_btn.pack(side=tk.RIGHT, padx=(6, 0))

        self.input_bar = tk.Frame(self.bottom_bar, bg="#0f172a")
        self.input_bar.pack(fill=tk.X)

        self.input = tk.Text(
            self.input_bar,
            height=1,
            wrap=tk.WORD,
            bg=self.get_input_color(),
            fg="#111827",
            insertbackground="#111827",
            relief=tk.FLAT,
            font=("Microsoft YaHei UI", 10),
            padx=8,
            pady=7,
        )
        self.input.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.input.bind("<Control-Return>", lambda _event: self.send_message())
        self.input.bind("<Control-v>", self.paste_from_clipboard)
        self.input.bind("<Control-V>", self.paste_from_clipboard)

        self.send_btn = tk.Button(
            self.input_bar,
            text="发送",
            command=self.send_message,
            bg="#2563eb",
            fg="#ffffff",
            activebackground="#1d4ed8",
            activeforeground="#ffffff",
            bd=0,
            width=6,
            font=("Microsoft YaHei UI", 10, "bold"),
        )
        self.send_btn.pack(side=tk.RIGHT, fill=tk.Y, padx=(8, 0))

        self.append_chat(
            "系统",
            "默认已固定在其他窗口上方。可选择图片后发送，按 Ctrl+Enter 发送文字。",
        )

    def pick_screen_color_for_entry(self, entry):
        self.color_pick_target = entry
        win = tk.Toplevel(self.root)
        win.title("屏幕取色")
        win.attributes("-fullscreen", True)
        win.attributes("-topmost", True)
        win.attributes("-alpha", 0.12)
        win.configure(bg="#000000", cursor="crosshair")
        tk.Label(
            win,
            text="点击屏幕任意位置取色，按 Esc 取消",
            bg="#000000",
            fg="#ffffff",
            font=("Microsoft YaHei UI", 16, "bold"),
        ).pack(pady=40)

        def close_picker():
            self.color_pick_target = None
            win.destroy()

        def pick(event):
            try:
                x, y = event.x_root, event.y_root
                win.withdraw()
                self.root.update()
                image = ImageGrab.grab(bbox=(x, y, x + 1, y + 1))
                pixel = image.getpixel((0, 0))
                r, g, b = pixel[:3]
                color = f"#{r:02x}{g:02x}{b:02x}"
                entry.delete(0, tk.END)
                entry.insert(0, color)
            except Exception as exc:
                messagebox.showerror("取色失败", str(exc), parent=self.root)
            finally:
                close_picker()

        win.bind("<Button-1>", pick)
        win.bind("<Escape>", lambda _event: close_picker())

    def append_chat(self, speaker, text):
        self.chat.configure(state=tk.NORMAL)
        self.chat.insert(tk.END, f"{speaker}：\n{text.strip()}\n\n")
        self.chat.configure(state=tk.DISABLED)
        self.chat.see(tk.END)

    def clear_chat(self):
        self.messages.clear()
        self.chat.configure(state=tk.NORMAL)
        self.chat.delete("1.0", tk.END)
        self.chat.configure(state=tk.DISABLED)
        self.set_busy(False)

    def start_drag(self, event):
        if self.pinned:
            return
        self.drag_start = (event.x_root, event.y_root, self.root.winfo_x(), self.root.winfo_y())

    def drag(self, event):
        if self.pinned or not self.drag_start:
            return
        start_x, start_y, win_x, win_y = self.drag_start
        self.root.geometry(f"+{win_x + event.x_root - start_x}+{win_y + event.y_root - start_y}")

    def get_resize_edges(self, event):
        width = self.root.winfo_width()
        height = self.root.winfo_height()
        edges = []
        if event.x <= RESIZE_BORDER:
            edges.append("left")
        elif event.x >= width - RESIZE_BORDER:
            edges.append("right")
        if event.y <= RESIZE_BORDER:
            edges.append("top")
        elif event.y >= height - RESIZE_BORDER:
            edges.append("bottom")
        return "+".join(edges)

    def update_resize_cursor(self, event):
        edge = self.get_resize_edges(event)
        cursors = {
            "left": "size_we",
            "right": "size_we",
            "top": "size_ns",
            "bottom": "size_ns",
            "left+top": "size_nw_se",
            "right+bottom": "size_nw_se",
            "right+top": "size_ne_sw",
            "left+bottom": "size_ne_sw",
        }
        self.root.configure(cursor=cursors.get(edge, ""))

    def start_resize(self, event):
        edge = self.get_resize_edges(event)
        if not edge:
            self.resize_start = None
            return
        self.resize_start = {
            "edge": edge,
            "x_root": event.x_root,
            "y_root": event.y_root,
            "win_x": self.root.winfo_x(),
            "win_y": self.root.winfo_y(),
            "width": self.root.winfo_width(),
            "height": self.root.winfo_height(),
        }

    def resize_window(self, event):
        if not self.resize_start:
            return

        dx = event.x_root - self.resize_start["x_root"]
        dy = event.y_root - self.resize_start["y_root"]
        edge = self.resize_start["edge"]
        min_width = 280
        min_height = 300
        x = self.resize_start["win_x"]
        y = self.resize_start["win_y"]
        width = self.resize_start["width"]
        height = self.resize_start["height"]

        if "right" in edge:
            width = max(min_width, self.resize_start["width"] + dx)
        if "bottom" in edge:
            height = max(min_height, self.resize_start["height"] + dy)
        if "left" in edge:
            width = max(min_width, self.resize_start["width"] - dx)
            if width > min_width:
                x = self.resize_start["win_x"] + dx
        if "top" in edge:
            height = max(min_height, self.resize_start["height"] - dy)
            if height > min_height:
                y = self.resize_start["win_y"] + dy

        self.root.geometry(f"{int(width)}x{int(height)}+{int(x)}+{int(y)}")

    def stop_resize(self, _event):
        self.resize_start = None

    def minimize_window(self):
        self.root.overrideredirect(False)
        self.root.iconify()

    def restore_borderless(self, _event=None):
        self.root.after(10, lambda: self.root.overrideredirect(True))

    def toggle_visibility(self):
        if self.root.state() in {"withdrawn", "iconic"}:
            self.root.deiconify()
            self.restore_borderless()
            self.root.attributes("-topmost", self.pinned)
            self.root.lift()
            self.input.focus_set()
        else:
            self.minimize_window()

    def close_app(self):
        self.unregister_hotkey()
        self.root.destroy()

    def parse_hotkey(self, hotkey):
        parts = [part.strip().lower() for part in hotkey.replace(" ", "").split("+") if part.strip()]
        if not parts:
            raise ValueError("快捷键不能为空。")

        modifiers = MOD_NOREPEAT
        key = None
        for part in parts:
            if part in {"ctrl", "control"}:
                modifiers |= MOD_CONTROL
            elif part == "alt":
                modifiers |= MOD_ALT
            elif part == "shift":
                modifiers |= MOD_SHIFT
            elif part in {"win", "windows"}:
                modifiers |= MOD_WIN
            else:
                key = part.upper()

        if not key:
            raise ValueError("快捷键必须包含一个主键，例如 Ctrl+Alt+A。")
        if len(key) == 1 and key.isalnum():
            vk = ord(key)
        elif key.startswith("F") and key[1:].isdigit() and 1 <= int(key[1:]) <= 12:
            vk = 0x70 + int(key[1:]) - 1
        else:
            raise ValueError("主键只支持 A-Z、0-9、F1-F12。")
        return modifiers, vk

    def register_hotkey(self):
        self.unregister_hotkey()
        hotkey = self.config.get("hotkey", "Ctrl+Alt+A").strip()
        if not hotkey:
            return True
        try:
            modifiers, vk = self.parse_hotkey(hotkey)
        except ValueError as exc:
            self.append_chat("系统", f"快捷键设置无效：{exc}")
            return False

        ready = threading.Event()
        result = {"ok": False}

        def hotkey_loop():
            self.hotkey_thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
            ok = ctypes.windll.user32.RegisterHotKey(None, HOTKEY_ID, modifiers, vk)
            result["ok"] = bool(ok)
            ready.set()
            if not ok:
                return
            msg = MSG()
            try:
                while ctypes.windll.user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
                    if msg.message == WM_HOTKEY and msg.wParam == HOTKEY_ID:
                        self.result_queue.put(("hotkey", ""))
            finally:
                ctypes.windll.user32.UnregisterHotKey(None, HOTKEY_ID)

        self.hotkey_thread = threading.Thread(target=hotkey_loop, daemon=True)
        self.hotkey_thread.start()
        ready.wait(1.0)
        self.hotkey_registered = result["ok"]
        if not result["ok"]:
            self.append_chat("系统", f"快捷键 {hotkey} 注册失败，可能已被其他软件占用。")
            return False
        return True

    def unregister_hotkey(self):
        if self.hotkey_registered:
            if self.hotkey_thread_id:
                ctypes.windll.user32.PostThreadMessageW(self.hotkey_thread_id, WM_QUIT, 0, 0)
            if self.hotkey_thread and self.hotkey_thread.is_alive():
                self.hotkey_thread.join(timeout=1.0)
            self.hotkey_registered = False
            self.hotkey_thread = None
            self.hotkey_thread_id = None

    def poll_hotkey(self):
        msg = MSG()
        while ctypes.windll.user32.PeekMessageW(ctypes.byref(msg), None, WM_HOTKEY, WM_HOTKEY, PM_REMOVE):
            if msg.message == WM_HOTKEY and msg.wParam == HOTKEY_ID:
                self.toggle_visibility()
        self.root.after(80, self.poll_hotkey)

    def toggle_pin(self):
        self.pinned = not self.pinned
        self.config["pinned"] = self.pinned
        save_config(self.config)
        self.root.attributes("-topmost", self.pinned)
        if self.pinned:
            self.root.lift()
        self.update_pin_ui()

    def update_pin_ui(self):
        if self.pinned:
            self.status.configure(text="已固定")
        else:
            self.status.configure(text="就绪")

    def enforce_pin(self):
        if self.pinned:
            self.root.attributes("-topmost", True)
            self.root.lift()
        self.root.after(800, self.enforce_pin)

    def open_menu(self):
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(
            label="取消固定" if self.pinned else "钉住",
            command=self.toggle_pin,
        )
        menu.add_command(label="粘贴图片", command=self.paste_image_from_clipboard)
        menu.add_command(label="清空聊天", command=self.clear_chat)
        menu.add_command(label="短回答模式", command=self.set_short_reply_mode)
        menu.add_command(
            label="关闭透明模式" if self.transparent_mode else "透明模式",
            command=self.toggle_transparent_mode,
        )
        menu.add_command(label="最小化", command=self.minimize_window)
        menu.add_separator()
        menu.add_command(label="设置", command=self.open_settings)
        menu.add_separator()
        menu.add_command(label="退出", command=self.close_app)
        x = self.menu_btn.winfo_rootx()
        y = self.menu_btn.winfo_rooty() + self.menu_btn.winfo_height()
        menu.tk_popup(x, y)

    def choose_image(self):
        path = filedialog.askopenfilename(
            title="选择要发送的图片",
            filetypes=[
                ("图片文件", "*.png *.jpg *.jpeg *.webp *.gif"),
                ("PNG", "*.png"),
                ("JPEG", "*.jpg *.jpeg"),
                ("WebP", "*.webp"),
                ("GIF", "*.gif"),
                ("所有文件", "*.*"),
            ],
            parent=self.root,
        )
        if not path:
            return

        image_path = Path(path)
        if not image_path.exists():
            messagebox.showerror("图片错误", "图片文件不存在。")
            return
        if image_path.stat().st_size > MAX_IMAGE_BYTES:
            messagebox.showerror("图片过大", "请选择 20MB 以内的图片。")
            return

        mime_type = mimetypes.guess_type(str(image_path))[0]
        if mime_type not in {"image/png", "image/jpeg", "image/webp", "image/gif"}:
            messagebox.showerror("格式不支持", "请选择 PNG、JPG、WebP 或 GIF 图片。")
            return

        self.selected_image_path = image_path
        self.image_label.configure(text=f"已选：{image_path.name}", fg="#d1d5db")

    def paste_from_clipboard(self, _event=None):
        if self.paste_image_from_clipboard(show_error=False):
            return "break"
        return None

    def paste_image_from_clipboard(self, show_error=True):
        try:
            image = ImageGrab.grabclipboard()
        except Exception as exc:
            if show_error:
                messagebox.showerror("剪贴板读取失败", str(exc))
            return False

        if image is None:
            if show_error:
                messagebox.showinfo("没有图片", "剪贴板里没有可用图片。")
            return False

        if isinstance(image, list):
            for item in image:
                path = Path(item)
                if path.exists() and path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
                    self.set_selected_image(path)
                    return True
            if show_error:
                messagebox.showinfo("没有图片", "剪贴板里的文件不是支持的图片格式。")
            return False

        if not hasattr(image, "save"):
            if show_error:
                messagebox.showinfo("没有图片", "剪贴板里没有可用图片。")
            return False

        TEMP_DIR.mkdir(parents=True, exist_ok=True)
        image_path = TEMP_DIR / "clipboard_image.png"
        image.save(image_path, "PNG")
        self.set_selected_image(image_path)
        return True

    def set_selected_image(self, image_path):
        if image_path.stat().st_size > MAX_IMAGE_BYTES:
            messagebox.showerror("图片过大", "请选择 20MB 以内的图片。")
            return
        self.selected_image_path = image_path
        self.image_label.configure(text=f"已选：{image_path.name}", fg="#ffffff" if self.transparent_mode else "#d1d5db")

    def set_short_reply_mode(self):
        self.config["system_prompt"] = SHORT_SYSTEM_PROMPT
        save_config(self.config)
        self.append_chat("系统", "已切换为短回答模式。")

    def toggle_transparent_mode(self):
        self.transparent_mode = not self.transparent_mode
        self.config["transparent_mode"] = self.transparent_mode
        save_config(self.config)
        self.apply_transparent_mode()

    def apply_transparent_mode(self):
        input_color = self.get_input_color()
        if self.transparent_mode:
            self.root.attributes("-alpha", 1.0)
            try:
                self.root.attributes("-transparentcolor", TRANSPARENT_COLOR)
            except tk.TclError:
                pass
            panel_color = TRANSPARENT_COLOR
            transparent_widgets = [
                self.root,
                self.shell,
                self.header,
                self.title_label,
                self.status,
                self.body,
                self.bottom_bar,
                self.attach_bar,
                self.image_label,
                self.input_bar,
            ]
            for widget in transparent_widgets:
                widget.configure(bg=panel_color)
            self.chat.configure(
                bg=panel_color,
                fg="#ffffff",
                insertbackground="#ffffff",
                relief=tk.FLAT,
                borderwidth=0,
                highlightthickness=0,
            )
            self.input.configure(
                bg=input_color,
                fg="#ffffff",
                insertbackground="#ffffff",
                relief=tk.FLAT,
                borderwidth=0,
                highlightthickness=1,
                highlightbackground="#374151",
                highlightcolor="#60a5fa",
            )
            self.image_label.configure(fg="#ffffff")
            self.menu_btn.configure(bg="#111827", fg="#ffffff")
        else:
            try:
                self.root.attributes("-transparentcolor", "")
            except tk.TclError:
                pass
            self.root.attributes("-alpha", float(self.config.get("opacity", 1.0)))
            self.root.configure(bg="#111827")
            self.shell.configure(bg="#111827")
            self.header.configure(bg="#111827")
            self.title_label.configure(bg="#111827", fg="#f9fafb")
            self.status.configure(bg="#111827", fg="#9ca3af")
            self.body.configure(bg="#0f172a")
            self.bottom_bar.configure(bg="#0f172a")
            self.attach_bar.configure(bg="#0f172a")
            self.image_label.configure(bg="#0f172a", fg="#9ca3af")
            self.input_bar.configure(bg="#0f172a")
            self.chat.configure(
                bg="#111827",
                fg="#e5e7eb",
                insertbackground="#ffffff",
                relief=tk.FLAT,
                borderwidth=0,
                highlightthickness=0,
            )
            self.input.configure(
                bg=input_color,
                fg="#111827",
                insertbackground="#111827",
                relief=tk.FLAT,
                borderwidth=0,
                highlightthickness=0,
            )
            self.menu_btn.configure(bg="#1f2937", fg="#f9fafb")

    def get_input_color(self):
        return (
            self.config.get("input_color")
            or self.config.get("transparent_input_color")
            or "#f9fafb"
        )

    def clear_image(self):
        self.selected_image_path = None
        self.image_label.configure(text="未选择图片", fg="#ffffff" if self.transparent_mode else "#9ca3af")

    def build_user_content(self, text):
        content = []
        if text:
            content.append({"type": "input_text", "text": text})

        if self.selected_image_path:
            image_path = self.selected_image_path
            mime_type = mimetypes.guess_type(str(image_path))[0] or "image/png"
            encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
            content.append(
                {
                    "type": "input_image",
                    "image_url": f"data:{mime_type};base64,{encoded}",
                }
            )

        return content

    def open_settings(self):
        win = tk.Toplevel(self.root)
        win.title("设置")
        win.geometry("540x540")
        win.configure(bg="#f3f4f6")
        win.transient(self.root)
        win.grab_set()

        fields = {}

        def add_field(label, key, row, show=None):
            tk.Label(win, text=label, bg="#f3f4f6", anchor="w").grid(
                row=row, column=0, sticky="w", padx=14, pady=(12, 4)
            )
            entry = tk.Entry(win, show=show)
            entry.insert(0, str(self.config.get(key, "")))
            entry.grid(row=row, column=1, sticky="ew", padx=14, pady=(12, 4))
            fields[key] = entry

        def record_hotkey(event):
            key = event.keysym
            if key in {"Control_L", "Control_R", "Shift_L", "Shift_R", "Alt_L", "Alt_R", "Win_L", "Win_R"}:
                return "break"
            modifiers = []
            if event.state & 0x0004:
                modifiers.append("Ctrl")
            if event.state & 0x0001:
                modifiers.append("Shift")
            if event.state & 0x0008 or event.state & 0x0080:
                modifiers.append("Alt")
            key_name = key.upper() if len(key) == 1 else key
            if key_name.startswith("F") and key_name[1:].isdigit():
                pass
            elif len(key_name) == 1 and key_name.isalnum():
                pass
            else:
                return "break"
            value = "+".join(modifiers + [key_name])
            fields["hotkey"].delete(0, tk.END)
            fields["hotkey"].insert(0, value)
            return "break"

        win.columnconfigure(1, weight=1)
        add_field("API Key", "api_key", 0, show="*")
        add_field("模型", "model", 1)
        add_field("接口地址", "base_url", 2)
        add_field("最大输出 tokens", "max_output_tokens", 3)
        add_field("普通透明度 0.01-1.00", "opacity", 4)
        add_field("全局快捷键", "hotkey", 5)
        fields["hotkey"].bind("<KeyPress>", record_hotkey)
        if "input_color" not in self.config and "transparent_input_color" in self.config:
            self.config["input_color"] = self.config["transparent_input_color"]
        add_field("输入框颜色", "input_color", 6)

        def choose_input_color():
            current = fields["input_color"].get().strip() or self.get_input_color()
            _rgb, color = colorchooser.askcolor(
                color=current,
                title="选择输入框颜色",
                parent=win,
            )
            if color:
                fields["input_color"].delete(0, tk.END)
                fields["input_color"].insert(0, color)

        tk.Label(win, text="系统提示词", bg="#f3f4f6", anchor="w").grid(
            row=7, column=0, sticky="nw", padx=14, pady=(12, 4)
        )
        prompt = tk.Text(win, height=5, wrap=tk.WORD)
        prompt.insert("1.0", self.config.get("system_prompt", ""))
        prompt.grid(row=7, column=1, sticky="nsew", padx=14, pady=(12, 4))
        win.rowconfigure(7, weight=1)

        tk.Button(
            win,
            text="选择颜色",
            command=choose_input_color,
            bg="#374151",
            fg="#ffffff",
        ).grid(row=6, column=1, sticky="e", padx=(14, 100), pady=(2, 4))

        tk.Button(
            win,
            text="屏幕取色",
            command=lambda: self.pick_screen_color_for_entry(fields["input_color"]),
            bg="#047857",
            fg="#ffffff",
        ).grid(row=6, column=1, sticky="e", padx=14, pady=(2, 4))

        note = tk.Label(
            win,
            text="快捷键示例：Ctrl+Alt+A、Ctrl+Shift+F8。API Key 也可以改用 OPENAI_API_KEY。",
            bg="#f3f4f6",
            fg="#4b5563",
        )
        note.grid(row=8, column=0, columnspan=2, sticky="w", padx=14, pady=(8, 0))

        def on_save():
            try:
                opacity = max(0.01, min(1.0, float(fields["opacity"].get().strip())))
                max_tokens = int(fields["max_output_tokens"].get().strip())
            except ValueError:
                messagebox.showerror("设置错误", "透明度或 tokens 必须是数字。", parent=win)
                return

            for key, entry in fields.items():
                self.config[key] = entry.get().strip()
            self.config["opacity"] = opacity
            self.config["max_output_tokens"] = max_tokens
            self.config["input_color"] = fields["input_color"].get().strip() or self.get_input_color()
            self.config["transparent_input_color"] = self.config["input_color"]
            self.config["system_prompt"] = prompt.get("1.0", tk.END).strip()
            self.config["pinned"] = self.pinned
            self.config["transparent_mode"] = self.transparent_mode
            save_config(self.config)
            self.apply_transparent_mode()
            if not self.register_hotkey():
                messagebox.showwarning(
                    "快捷键不可用",
                    "这个快捷键注册失败，可能已被其他软件占用。请换一个组合，例如 Ctrl+Alt+A 或 Ctrl+Shift+F8。",
                    parent=win,
                )
                return
            win.destroy()

        buttons = tk.Frame(win, bg="#f3f4f6")
        buttons.grid(row=9, column=0, columnspan=2, sticky="e", padx=14, pady=14)
        tk.Button(buttons, text="取消", command=win.destroy, width=10).pack(side=tk.RIGHT, padx=(8, 0))
        tk.Button(buttons, text="保存", command=on_save, width=10, bg="#2563eb", fg="#ffffff").pack(side=tk.RIGHT)

    def send_message(self):
        if self.sending:
            return
        text = self.input.get("1.0", tk.END).strip()
        image_path = self.selected_image_path
        if not text and not image_path:
            return

        api_key = self.config.get("api_key") or os.environ.get("OPENAI_API_KEY", "")
        if not api_key:
            messagebox.showwarning("缺少 API Key", "请在设置中填写 API Key，或设置 OPENAI_API_KEY 环境变量。")
            return

        self.input.delete("1.0", tk.END)
        if not text:
            text = "请描述并分析这张图片。"
        display = text
        if image_path:
            display = f"{display}\n[图片：{image_path.name}]"

        try:
            content = self.build_user_content(text)
        except Exception as exc:
            messagebox.showerror("图片读取失败", str(exc))
            return

        self.append_chat("你", display)
        self.messages.append({"role": "user", "content": content if image_path else text})
        self.clear_image()
        self.set_busy(True)

        thread = threading.Thread(target=self.call_api, args=(api_key,), daemon=True)
        thread.start()

    def set_busy(self, busy):
        self.sending = busy
        if busy:
            self.status.configure(text="思考中...")
        else:
            self.status.configure(text="已固定" if self.pinned else "就绪")
        self.send_btn.configure(state=tk.DISABLED if busy else tk.NORMAL)

    def call_api(self, api_key):
        payload = {
            "model": self.config.get("model", DEFAULT_CONFIG["model"]),
            "instructions": self.config.get("system_prompt", ""),
            "input": self.messages[-20:],
            "max_output_tokens": int(self.config.get("max_output_tokens", 1200)),
        }
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.config.get("base_url", DEFAULT_CONFIG["base_url"]),
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=90) as response:
                result = json.loads(response.read().decode("utf-8"))
            answer = extract_response_text(result)
            self.result_queue.put(("ok", answer))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            self.result_queue.put(("error", f"HTTP {exc.code}\n{detail}"))
        except Exception as exc:
            self.result_queue.put(("error", str(exc)))

    def poll_queue(self):
        try:
            kind, text = self.result_queue.get_nowait()
        except queue.Empty:
            self.root.after(120, self.poll_queue)
            return

        if kind == "hotkey":
            self.toggle_visibility()
        elif kind == "ok":
            self.messages.append({"role": "assistant", "content": text})
            self.append_chat("AI", text)
            self.set_busy(False)
        elif kind == "error":
            self.append_chat("错误", text)
            self.set_busy(False)
        self.root.after(120, self.poll_queue)


def main():
    root = tk.Tk()
    FloatingAIChat(root)
    root.mainloop()


if __name__ == "__main__":
    main()
