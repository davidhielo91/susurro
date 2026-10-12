"""Susurro: dictado por voz local con Whisper (estilo Wispr Flow).

Tecla de dictado (por defecto Ctrl derecho, configurable):
  - Tocar y soltar: empieza a grabar; otro toque termina y transcribe.
  - Mantener presionada: graba mientras está abajo; al soltar, transcribe.
Esc cancela la grabación sin pegar nada.

El texto se transcribe en tu computadora (GPU NVIDIA, o CPU si no hay), se
copia al portapapeles y se pega donde esté el cursor. La ventana principal
(Historial, Vocabulario, Ajustes) se abre desde el ícono de la bandeja o
abriendo el acceso directo otra vez.

Opciones para el instalador:
  --crear-accesos      crea los accesos directos (escritorio e inicio de Windows)
  --descargar-modelo   descarga el modelo de Whisper por adelantado
"""

import json
import os
import queue
import re
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

# --- Configuración fija (lo demás se cambia desde la ventana de Configuración)
# Modelos probados. turbo: ~3 s por 38 s de audio; large-v3: ~13 s, sin ganar precisión
# en dictado. Se cambia editando "model" en config.json.
MODELS = {
    "large-v3-turbo": "Rápido (large-v3-turbo)",
    "large-v3": "Máxima precisión (large-v3)",
}
# Idiomas del dictado (código de Whisper -> nombre en Ajustes); "auto" = detectar
LANGUAGES = {
    "es": "Español",
    "en": "Inglés",
    "pt": "Portugués",
    "fr": "Francés",
    "it": "Italiano",
    "de": "Alemán",
    "auto": "Automático",
}
HOLD_SECONDS = 0.35            # cuánto sostener la tecla para que cuente como "mantener"
SAMPLE_RATE = 16000
MIN_SECONDS = 0.4              # grabaciones más cortas se ignoran
DEFAULTS = {
    "hotkey": "right ctrl",
    "auto_paste": True,
    "sounds": True,
    "overlay": True,
    "save_history": True,
    "mic": "",                 # "" = el micrófono predeterminado de Windows
    "model": "large-v3-turbo",
    "language": "es",
    # Palabras que Whisper debe reconocer (nombres, marcas, términos técnicos)
    # (ejemplos de uso común; cada quien agrega los suyos en la pestaña Vocabulario)
    "vocab": ["WhatsApp", "Gmail", "YouTube", "ChatGPT", "IA", "Excel", "PowerPoint", "PDF"],
    # Correcciones después de transcribir: [lo que escribe, lo correcto]
    "replacements": [["chat gpt", "ChatGPT"], ["whats app", "WhatsApp"]],
}
HISTORY_MAX = 1000             # transcripciones guardadas (~1 mes y medio, ~0.5 MB)
HISTORY_MENU = 10              # cuántas aparecen en el menú del ícono
# -----------------------------------------------------------------------------

BASE = Path(__file__).resolve().parent
SCRIPT = BASE / "susurro.py"
PYTHONW = BASE / ".venv" / "Scripts" / "pythonw.exe"
# Si cambian los colores del ícono, cambiar también el nombre: Windows guarda en caché
# los íconos por nombre de archivo y seguiría mostrando el anterior.
ICON_FILE = BASE / "susurro-marca.ico"
CONFIG_FILE = BASE / "config.json"
HISTORY_FILE = BASE / "historial.json"
LOG_FILE = BASE / "susurro.log"
STARTUP_LNK = Path(os.environ["APPDATA"]) / r"Microsoft\Windows\Start Menu\Programs\Startup\Susurro.lnk"
INSTANCE_PORT = 47813
APP_ID = "DavidHielo.Susurro"
WEBSITE = "https://susurro.hieloco.de"
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")


def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def add_cuda_dll_dirs():
    """Hace visibles las DLL de cuBLAS/cuDNN instaladas con pip."""
    import site
    for sp in site.getsitepackages():
        for sub in ("cublas", "cudnn", "cuda_nvrtc"):
            d = Path(sp) / "nvidia" / sub / "bin"
            if d.is_dir():
                os.add_dll_directory(str(d))
                os.environ["PATH"] = str(d) + os.pathsep + os.environ["PATH"]


add_cuda_dll_dirs()

import ctypes  # noqa: E402
import ctypes.wintypes as wt  # noqa: E402
import tkinter as tk  # noqa: E402
from tkinter import ttk  # noqa: E402

import customtkinter as ctk  # noqa: E402

import keyboard  # noqa: E402
import numpy as np  # noqa: E402
import pyperclip  # noqa: E402
import pystray  # noqa: E402
import sounddevice as sd  # noqa: E402
from faster_whisper import WhisperModel  # noqa: E402
from PIL import Image, ImageDraw, ImageTk  # noqa: E402

user32 = ctypes.windll.user32


# --- Configuración guardada ---------------------------------------------------
def load_config():
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
    except FileNotFoundError:
        pass
    except Exception as e:
        log(f"config.json inválido, usando valores por defecto: {e}")
    return cfg


def save_config(cfg):
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")


# --- Vocabulario --------------------------------------------------------------
def apply_replacements(text, replacements):
    """Reemplaza palabras/frases completas sin importar mayúsculas."""
    for wrong, right in sorted(replacements, key=lambda r: -len(r[0])):
        if wrong.strip():
            pattern = r"(?<!\w)" + re.escape(wrong.strip()) + r"(?!\w)"
            text = re.sub(pattern, lambda m: right, text, flags=re.IGNORECASE)
    return text


def clean_words(lines):
    """Quita vacíos y repetidos, conservando el orden."""
    seen, out = set(), []
    for w in (l.strip() for l in lines):
        if w and w.lower() not in seen:
            seen.add(w.lower())
            out.append(w)
    return out


# --- Frases fantasma de Whisper -----------------------------------------------
# Whisper aprendió de subtítulos de internet y, con silencio o ruido, a veces
# "inventa" estas frases. Las de STRICT nunca son dictado real: se quitan siempre.
HALLUCINATION_STRICT = re.compile(
    r"amara\.org|subt[ií]tul(?:os|ado)\s+(?:realizados?\s+)?por\s+la\s+comunidad|"
    r"subt[ií]tulos\s+realizados\s+por", re.IGNORECASE)
# Estas sí podrían ser dictado real (p. ej. un guion de YouTube), así que solo se
# quitan cuando Whisper mismo indica que probablemente no había voz.
HALLUCINATION_SOFT = re.compile(
    r"gracias\s+por\s+(?:ver|mirar)|suscr[ií]bete|no\s+olvides\s+suscribirte|"
    r"dale\s+(?:a\s+)?like|^\W*(?:m[uú]sica|aplausos|risas)\W*$|♪|"
    r"thanks?\s+(?:you\s+)?for\s+watching|(?:please\s+|like\s+and\s+)subscribe|"
    r"obrigad[oa]\s+por\s+assistir|inscreva-se|^\W*(?:music|applause)\W*$", re.IGNORECASE)


def clean_segment(text, no_speech_prob, avg_logprob):
    """Devuelve el texto del segmento sin frases fantasma ('' si todo era fantasma)."""
    if no_speech_prob > 0.6 and avg_logprob < -1.0:
        return ""                     # casi seguro no había voz
    doubtful = no_speech_prob > 0.4 or avg_logprob < -0.8
    sentences = re.split(r"(?<=[.!?…])\s+", text.strip())
    kept = [s for s in sentences
            if not HALLUCINATION_STRICT.search(s)
            and not (doubtful and HALLUCINATION_SOFT.search(s))]
    return " ".join(kept).strip()


DEFAULT_MIC_LABEL = "Predeterminado de Windows"


def refresh_audio_devices():
    """PortAudio guarda la lista de dispositivos al iniciar; esto la vuelve a leer
    (~40 ms) para notar micrófonos conectados o un cambio de predeterminado en Windows."""
    try:
        sd._terminate()
        sd._initialize()
    except Exception as e:
        log(f"No se pudo actualizar la lista de micrófonos: {e}")


def list_mics():
    """Nombres de los micrófonos disponibles (sin el 'asignador' genérico de Windows)."""
    api = sd.default.hostapi
    names = []
    for d in sd.query_devices():
        name = d["name"]
        if (d["max_input_channels"] > 0 and d["hostapi"] == api and name not in names
                and not re.search(r"asignador|mapper|controlador primario|primary", name, re.I)):
            names.append(name)
    return names


def find_mic(name):
    """Índice del micrófono con ese nombre, o None para usar el predeterminado."""
    if not name:
        return None
    api = sd.default.hostapi
    for i, d in enumerate(sd.query_devices()):
        if d["name"] == name and d["max_input_channels"] > 0 and d["hostapi"] == api:
            return i
    return None


def trim_log(max_bytes=300_000, keep_lines=2000):
    """Evita que susurro.log crezca sin límite: conserva solo lo más reciente."""
    try:
        if LOG_FILE.stat().st_size > max_bytes:
            lines = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
            LOG_FILE.write_text("\n".join(lines[-keep_lines:]) + "\n", encoding="utf-8")
    except FileNotFoundError:
        pass


# --- Historial ----------------------------------------------------------------
class History:
    """Transcripciones recientes, la más nueva primero. Se guardan en historial.json."""

    def __init__(self):
        self.lock = threading.Lock()
        try:
            self.items = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self.items = []
        except Exception as e:
            log(f"historial.json inválido, empezando de cero: {e}")
            self.items = []

    def _save(self):
        HISTORY_FILE.write_text(json.dumps(self.items, indent=1, ensure_ascii=False), encoding="utf-8")

    def add(self, text):
        with self.lock:
            self.items.insert(0, {"t": time.time(), "text": text})
            del self.items[HISTORY_MAX:]
            self._save()

    def update(self, t, text):
        with self.lock:
            for i in self.items:
                if i["t"] == t:
                    i["text"] = text
            self._save()

    def remove(self, t):
        with self.lock:
            self.items = [i for i in self.items if i["t"] != t]
            self._save()

    def clear(self):
        with self.lock:
            self.items = []
            self._save()

    def snapshot(self):
        with self.lock:
            return list(self.items)


def when_label(t):
    """'14:10', 'ayer 14:10' o '28/09 14:10'."""
    lt, today = time.localtime(t), time.localtime()
    hhmm = time.strftime("%H:%M", lt)
    days = (time.mktime(today[:3] + (0, 0, 0, 0, 0, -1)) -
            time.mktime(lt[:3] + (0, 0, 0, 0, 0, -1))) // 86400
    if days <= 0:
        return hhmm
    if days == 1:
        return f"ayer {hhmm}"
    return time.strftime("%d/%m ", lt) + hhmm


def snippet(text, n=50):
    text = " ".join(text.split())
    return text if len(text) <= n else text[:n - 1].rstrip() + "…"


# --- Teclas -------------------------------------------------------------------
KEY_LABELS = {
    "right ctrl": "Ctrl derecho", "left ctrl": "Ctrl izquierdo",
    "right alt": "Alt derecho (AltGr)", "alt gr": "Alt derecho (AltGr)",
    "right shift": "Shift derecho", "caps lock": "Bloq Mayús",
    "scroll lock": "Bloq Despl", "pause": "Pausa", "insert": "Insert",
    "menu": "Tecla Menú", "apps": "Tecla Menú", "num lock": "Bloq Num",
    "space": "Espacio", "enter": "Enter", "backspace": "Retroceso", "delete": "Supr",
    "left windows": "Windows", "right windows": "Windows", "left alt": "Alt izquierdo",
    "left shift": "Shift izquierdo", "up": "Flecha", "down": "Flecha", "left": "Flecha",
    "right": "Flecha", "page up": "Re Pág", "page down": "Av Pág", "home": "Inicio", "end": "Fin",
}
ALTGR_NAMES = ("right alt", "alt gr")
# Teclas modificadoras: se dejan pasar para que sus combinaciones sigan funcionando.
MODIFIER_KEYS = {"right ctrl", "left ctrl", "right alt", "alt gr", "right shift"}
# Teclas que no conviene usar: escriben texto o tienen funciones esenciales.
BLOCKED_KEYS = {"space", "enter", "tab", "backspace", "esc", "delete", "shift",
                "left shift", "alt", "left alt", "ctrl", "left windows", "right windows",
                "windows", "up", "down", "left", "right", "home", "end",
                "page up", "page down"}


def key_label(name):
    if name in KEY_LABELS:
        return KEY_LABELS[name]
    if name and name.startswith("f") and name[1:].isdigit():
        return name.upper()
    return (name or "?").title()


def key_problem(name):
    """Devuelve por qué una tecla no sirve, o None si está bien."""
    if not name:
        return "No reconocí esa tecla."
    if name in BLOCKED_KEYS or len(name) == 1:
        return f"«{key_label(name)}» se usa para escribir o navegar; elige otra."
    return None


def key_matches(name, hotkey):
    if hotkey in ALTGR_NAMES:
        return name in ALTGR_NAMES
    return name == hotkey


# --- Colores de la marca ------------------------------------------------------
NAVY = "#101827"        # Azul noche
LAVENDER = "#9B8CF2"    # Lavanda
TURQUOISE = "#64D8CF"   # Turquesa
SMOKE = "#F3F5FA"       # Blanco humo
GRAY = "#64748B"        # Gris suave

# Tonos derivados para la interfaz oscura (variaciones de la paleta)
SURFACE = "#182235"     # tarjetas sobre el fondo azul noche
SURFACE_2 = "#212D45"   # campos de texto y botones secundarios
BORDER = "#2C3954"
MUTED = "#8C98AD"       # gris suave aclarado: se lee bien sobre azul noche
LAVENDER_HOVER = "#AFA3F5"
ERROR = "#F28B82"       # solo para mensajes de error


def apply_theme(root):
    """Tema oscuro de la marca para CustomTkinter y para las tablas (ttk.Treeview)."""
    ctk.set_appearance_mode("dark")
    st = ttk.Style(root)
    st.theme_use("clam")
    st.configure("Treeview", background=SURFACE, fieldbackground=SURFACE, foreground=SMOKE,
                 rowheight=30, borderwidth=0, relief="flat", font=("Segoe UI", 10))
    st.map("Treeview", background=[("selected", LAVENDER)], foreground=[("selected", NAVY)])
    st.configure("Treeview.Heading", background=SURFACE_2, foreground=MUTED, relief="flat",
                 borderwidth=0, font=("Segoe UI Semibold", 9), padding=(8, 6))
    st.map("Treeview.Heading", background=[("active", BORDER)])
    st.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])  # sin borde


def font(size=13, weight="normal"):
    return ctk.CTkFont(family="Segoe UI", size=size, weight=weight)


def primary_button(master, text, command, **kw):
    return ctk.CTkButton(master, text=text, command=command, fg_color=LAVENDER,
                         hover_color=LAVENDER_HOVER, text_color=NAVY, corner_radius=10,
                         height=36, font=font(13, "bold"), **kw)


def secondary_button(master, text, command, **kw):
    return ctk.CTkButton(master, text=text, command=command, fg_color=SURFACE_2,
                         hover_color=BORDER, text_color=SMOKE, corner_radius=10,
                         height=36, font=font(13), **kw)


def ghost_button(master, text, command, color=MUTED, **kw):
    return ctk.CTkButton(master, text=text, command=command, fg_color="transparent",
                         hover_color=SURFACE_2, text_color=color, corner_radius=10,
                         height=36, font=font(13), **kw)


def entry(master, **kw):
    return ctk.CTkEntry(master, fg_color=SURFACE_2, border_color=BORDER, border_width=1,
                        text_color=SMOKE, placeholder_text_color=GRAY, corner_radius=8,
                        height=34, font=font(13), **kw)


def option_menu(master, variable, values, width=330):
    return ctk.CTkOptionMenu(master, variable=variable, values=values,
                             width=width, height=34, corner_radius=8, fg_color=SURFACE_2,
                             button_color=BORDER, button_hover_color=GRAY, text_color=SMOKE,
                             font=font(13), dropdown_font=font(13), dropdown_fg_color=SURFACE_2,
                             dropdown_hover_color=BORDER, dropdown_text_color=SMOKE,
                             dynamic_resizing=False)


def card(master):
    return ctk.CTkFrame(master, fg_color=SURFACE, corner_radius=14, border_width=0)


def label(master, text="", size=13, color=SMOKE, weight="normal", **kw):
    return ctk.CTkLabel(master, text=text, text_color=color, font=font(size, weight),
                        anchor="w", justify="left", **kw)


def new_window(app, title, w, h, resizable=False):
    """Ventana de la app con el ícono y fondo de la marca, centrada en pantalla."""
    win = ctk.CTkToplevel(app.root, fg_color=NAVY)
    win.title(title)
    win.resizable(resizable, resizable)
    x = (win.winfo_screenwidth() - w) // 2
    y = max(20, (win.winfo_screenheight() - h) // 3)
    win.geometry(f"{w}x{h}+{x}+{y}")
    # CustomTkinter pone su propio ícono a los 200 ms; lo reemplazamos después
    win.after(250, lambda: (win.iconbitmap(str(ICON_FILE)), style_titlebar(win)))
    return win


def style_titlebar(win):
    """Barra de título azul noche (Windows 11), en vez del color de acento del sistema."""
    def colorref(hex_color):
        r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
        return ctypes.c_int(r | g << 8 | b << 16)
    try:
        hwnd = user32.GetParent(win.winfo_id()) or win.winfo_id()
        for attr, color in ((34, NAVY), (35, NAVY), (36, SMOKE)):  # borde, fondo, texto
            c = colorref(color)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(c), 4)
    except Exception:
        pass


def header(master, title, subtitle):
    row = ctk.CTkFrame(master, fg_color="transparent")
    logo = ctk.CTkImage(light_image=make_icon(LAVENDER, size=96),
                        dark_image=make_icon(LAVENDER, size=96), size=(38, 38))
    ctk.CTkLabel(row, text="", image=logo).pack(side="left")
    text = ctk.CTkFrame(row, fg_color="transparent")
    text.pack(side="left", padx=(12, 0))
    label(text, title, 20, SMOKE, "bold").pack(anchor="w")
    label(text, subtitle, 12, MUTED).pack(anchor="w")
    return row


def bring_to_front(win):
    win.deiconify()
    win.attributes("-topmost", True)
    win.lift()
    win.focus_force()
    win.after(200, lambda: win.attributes("-topmost", False))


def confirm(app, title, message, ok_text="Aceptar"):
    """Diálogo Sí/No con el estilo de la marca. Devuelve True si acepta."""
    d = new_window(app, title, 380, 170)
    d.transient(app.root)
    result = {"ok": False}
    body = ctk.CTkFrame(d, fg_color="transparent")
    body.pack(fill="both", expand=True, padx=22, pady=20)
    label(body, message, 14, wraplength=330).pack(anchor="w")
    buttons = ctk.CTkFrame(body, fg_color="transparent")
    buttons.pack(side="bottom", fill="x")

    def done(ok):
        result["ok"] = ok
        d.destroy()

    primary_button(buttons, ok_text, lambda: done(True), width=110).pack(side="right")
    ghost_button(buttons, "Cancelar", lambda: done(False), width=100).pack(side="right", padx=(0, 8))
    d.bind("<Escape>", lambda e: done(False))
    bring_to_front(d)
    d.grab_set()
    d.wait_window()
    return result["ok"]


# --- Ícono, sonidos y accesos directos ----------------------------------------
def make_icon(bg, fg=SMOKE, size=64):
    """Micrófono sobre un círculo. Se dibuja 4x más grande y se reduce para suavizar bordes."""
    big = size * 4
    s = big / 64
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((2 * s, 2 * s, 62 * s, 62 * s), fill=bg)
    d.rounded_rectangle((24 * s, 13 * s, 40 * s, 38 * s), radius=8 * s, fill=fg)
    d.arc((17 * s, 22 * s, 47 * s, 46 * s), start=0, end=180, fill=fg, width=round(3.5 * s))
    d.line((32 * s, 46 * s, 32 * s, 52 * s), fill=fg, width=round(3.5 * s))
    d.line((25 * s, 52 * s, 39 * s, 52 * s), fill=fg, width=round(3.5 * s))
    return img.resize((size, size), Image.LANCZOS)


ICONS = {
    "loading": make_icon(GRAY),
    "idle": make_icon(LAVENDER),
    "recording": make_icon(TURQUOISE, NAVY),
    "busy": make_icon(NAVY, TURQUOISE),
}


def ensure_icon_file():
    # Se regenera siempre para que el .ico siga los colores de la marca
    make_icon(LAVENDER, size=256).save(ICON_FILE, sizes=[(16, 16), (24, 24), (32, 32),
                                                         (48, 48), (64, 64), (256, 256)])


def make_shortcut(lnk):
    ensure_icon_file()
    ps = (f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{lnk}');"
          f"$s.TargetPath='{PYTHONW}';$s.Arguments='\"{SCRIPT}\"';"
          f"$s.WorkingDirectory='{BASE}';$s.IconLocation='{ICON_FILE},0';"
          f"$s.Description='Dictado por voz con Whisper';$s.Save()")
    subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                   creationflags=0x08000000, check=True)  # sin ventana de consola


def beep(freqs, seconds=0.12, volume=0.4):
    """Uno o varios tonos cortos seguidos por la salida de audio predeterminada."""
    rate = 44100
    t = np.arange(int(rate * seconds)) / rate
    fade = np.minimum(1, np.minimum(t, seconds - t) / 0.01)  # evita chasquidos
    gap = np.zeros(int(rate * 0.04))
    parts = []
    for f in (freqs if isinstance(freqs, (list, tuple)) else [freqs]):
        parts += [volume * np.sin(2 * np.pi * f * t) * fade, gap]
    try:
        sd.play(np.concatenate(parts).astype(np.float32), rate)
    except Exception as e:
        log(f"No se pudo reproducir el sonido: {e}")


# --- Barrita flotante ---------------------------------------------------------
class Overlay:
    """Píldora discreta, centrada justo arriba de la barra de tareas.
    Tamaño fijo y sin robar el foco de la ventana donde escribes."""

    KEY = "#010203"     # color que Windows vuelve transparente (fuera de la píldora)
    BARS = 5
    MARGIN = 14         # separación sobre la barra de tareas

    def __init__(self, win, get_level):
        self.win = win
        self.get_level = get_level
        self.visible = False
        self.mode = None
        self.key = ""
        self.started = 0.0
        self.s = win.winfo_fpixels("1i") / 96          # escala de pantalla (100% = 1.0)
        self.W, self.H = round(96 * self.s), round(30 * self.s)

        win.overrideredirect(True)
        win.configure(bg=self.KEY)
        win.attributes("-topmost", True)
        win.attributes("-transparentcolor", self.KEY)
        win.attributes("-alpha", 0.93)
        win.geometry(f"{self.W}x{self.H}+0+0")
        self.canvas = tk.Canvas(win, width=self.W, height=self.H, bg=self.KEY,
                                highlightthickness=0, borderwidth=0)
        self.canvas.pack()
        self.pill = ImageTk.PhotoImage(self.make_pill())
        win.update_idletasks()

        self.hwnd = user32.GetParent(win.winfo_id()) or win.winfo_id()
        GWL_EXSTYLE = -20
        WS_EX_TOPMOST, WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE = 0x8, 0x80, 0x08000000
        style = user32.GetWindowLongW(self.hwnd, GWL_EXSTYLE)
        user32.SetWindowLongW(self.hwnd, GWL_EXSTYLE,
                              style | WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)
        user32.ShowWindow(self.hwnd, 0)
        self.tick()

    def make_pill(self):
        """Azul noche con borde lavanda, redondeado y suavizado (dibujado 4x y reducido)."""
        k = 4
        img = Image.new("RGB", (self.W * k, self.H * k), self.KEY)
        ImageDraw.Draw(img).rounded_rectangle((0, 0, self.W * k - 1, self.H * k - 1),
                                              radius=self.H * k // 2, fill=NAVY,
                                              outline=LAVENDER, width=round(1.5 * self.s * k))
        return img.resize((self.W, self.H), Image.LANCZOS)

    def show(self, mode, key):
        """mode: 'toggle', 'hold' o 'busy'."""
        if mode != "busy" and self.mode not in ("toggle", "hold"):
            self.started = time.time()
        self.mode, self.key = mode, key
        self.render()
        if not self.visible:
            self.place()
            SW_SHOWNOACTIVATE = 4
            user32.ShowWindow(self.hwnd, SW_SHOWNOACTIVATE)
            self.visible = True

    def hide(self):
        self.mode = None
        if self.visible:
            user32.ShowWindow(self.hwnd, 0)
            self.visible = False

    def place(self):
        """Centrada justo arriba de la barra de tareas, en el monitor donde está el mouse."""
        pt = wt.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        mon = user32.MonitorFromPoint(pt, 2)

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT),
                        ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]

        mi = MONITORINFO(cbSize=ctypes.sizeof(MONITORINFO))
        user32.GetMonitorInfoW(mon, ctypes.byref(mi))
        x = (mi.rcWork.left + mi.rcWork.right - self.W) // 2
        y = mi.rcWork.bottom - self.H - round(self.MARGIN * self.s)
        # Tk recuerda esta posición, así no la regresa a la esquina al redibujar
        self.win.geometry(f"{self.W}x{self.H}+{x}+{y}")
        self.win.update_idletasks()
        HWND_TOPMOST, SWP_NOACTIVATE, SWP_NOMOVE, SWP_NOSIZE = -1, 0x10, 0x2, 0x1
        user32.SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                            SWP_NOACTIVATE | SWP_NOMOVE | SWP_NOSIZE)

    def render(self):
        c, s = self.canvas, self.s
        c.delete("all")
        c.create_image(0, 0, image=self.pill, anchor="nw")
        mid = self.H / 2
        if self.mode == "busy":   # puntitos que avanzan
            phase = int(time.time() * 6) % self.BARS
            gap, r = 11 * s, 2.6 * s
            x0 = self.W / 2 - gap * (self.BARS - 1) / 2
            for i in range(self.BARS):
                cx = x0 + i * gap
                c.create_oval(cx - r, mid - r, cx + r, mid + r, outline="",
                              fill=LAVENDER if i == phase else GRAY)
            return
        # barras de volumen + tiempo
        level = min(1.0, self.get_level() * 12)  # rms típico de voz ~0.02-0.1
        bw, gap, x = 3 * s, 2.5 * s, 16 * s
        for i in range(self.BARS):
            h = (5 + i * 2.2) * s
            c.create_rectangle(x, mid - h / 2, x + bw, mid + h / 2, outline="",
                               fill=TURQUOISE if level > (i + 0.5) / self.BARS else GRAY)
            x += bw + gap
        secs = int(time.time() - self.started)
        c.create_text(self.W - 16 * s, mid, text=f"{secs // 60}:{secs % 60:02d}", anchor="e",
                      fill=SMOKE, font=("Segoe UI Semibold", 9))

    def tick(self):
        if self.visible:
            self.render()
        self.win.after(80, self.tick)


# --- Ventana de configuración -------------------------------------------------
class MainWindow:
    """Ventana principal de Susurro: pestañas Historial, Vocabulario y Ajustes."""
    OPTIONS = (("auto_paste", "Pegar automáticamente", "El texto aparece donde está el cursor"),
               ("sounds", "Sonidos", "Un tono al empezar y otro al terminar"),
               ("overlay", "Barrita al grabar", "Píldora discreta arriba de la barra de tareas"),
               ("save_history", "Guardar historial", "Tus transcripciones, en la pestaña Historial"))

    def __init__(self, app, tab="Historial"):
        self.app = app
        self.hotkey = app.cfg["hotkey"]
        w = self.win = new_window(app, "Susurro", 540, 930)
        w.protocol("WM_DELETE_WINDOW", self.close)

        header(w, "Susurro", "Dictado por voz").pack(fill="x", padx=24, pady=(20, 8))

        self.tabs = ctk.CTkTabview(
            w, fg_color="transparent", corner_radius=12, anchor="w",
            segmented_button_fg_color=SURFACE, segmented_button_selected_color=LAVENDER,
            segmented_button_selected_hover_color=LAVENDER_HOVER,
            segmented_button_unselected_color=SURFACE,
            segmented_button_unselected_hover_color=SURFACE_2,
            text_color=SMOKE, text_color_disabled=MUTED)
        self.tabs.pack(fill="both", expand=True, padx=16)
        # Ordenadas de más a menos uso
        hist = self.tabs.add("Historial")
        vocab = self.tabs.add("Vocabulario")
        general = self.tabs.add("Ajustes")
        self.tabs._segmented_button.configure(font=font(13, "bold"))
        self.tabs.configure(command=self.tab_changed)
        self.build_general(general)
        self.build_vocab(vocab)
        self.history = HistoryPanel(app, hist, w)

        # Guardar/Cancelar solo aplica a Vocabulario y Ajustes
        self.footer = ctk.CTkFrame(w, fg_color="transparent")
        primary_button(self.footer, "Guardar", self.save, width=120).pack(side="right")
        ghost_button(self.footer, "Cancelar", self.close, width=100).pack(side="right", padx=(0, 8))
        self.show_tab(tab)
        bring_to_front(w)

    def show_tab(self, name):
        self.tabs.set(name)
        self.tab_changed()

    def tab_changed(self):
        current = self.tabs.get()
        # Texto azul noche en la pestaña activa (fondo lavanda) y claro en las demás
        for name, btn in self.tabs._segmented_button._buttons_dict.items():
            btn.configure(text_color=NAVY if name == current else MUTED)
        if current == "Historial":
            self.footer.pack_forget()
            self.history.refresh()
            self.win.after(100, self.history.search.focus_set)
        else:
            self.footer.pack(fill="x", padx=24, pady=(4, 20))

    # --- Pestaña Ajustes -----------------------------------------------------
    def build_general(self, tab):
        key_card = card(tab)
        key_card.pack(fill="x", pady=(6, 12))
        label(key_card, "Tecla para dictar", 14, weight="bold").pack(anchor="w", padx=18, pady=(14, 8))
        row = ctk.CTkFrame(key_card, fg_color="transparent")
        row.pack(fill="x", padx=18)
        self.key_box = ctk.CTkLabel(row, text=key_label(self.hotkey), width=220, height=36,
                                    fg_color=SURFACE_2, corner_radius=8, text_color=SMOKE,
                                    font=font(14, "bold"))
        self.key_box.pack(side="left")
        self.change_btn = secondary_button(row, "Cambiar", self.start_capture, width=100)
        self.change_btn.pack(side="left", padx=(10, 0))
        self.msg = label(key_card, "Tócala para empezar y terminar, o mantenla presionada mientras hablas.",
                         12, MUTED, wraplength=420)
        self.msg.pack(anchor="w", padx=18, pady=(8, 4))

        label(key_card, "Micrófono", 14, weight="bold").pack(anchor="w", padx=18, pady=(10, 8))
        if self.app.stream is None:      # no tocar el audio si está grabando
            refresh_audio_devices()
        mics = list_mics()
        current = self.app.cfg["mic"]
        if current and current not in mics:
            mics.append(current)         # el elegido, aunque ahora esté desconectado
        self.mic_var = tk.StringVar(value=current or DEFAULT_MIC_LABEL)
        option_menu(key_card, self.mic_var, [DEFAULT_MIC_LABEL] + mics).pack(anchor="w", padx=18)
        label(key_card, "«Predeterminado» sigue al que elijas en Windows, aunque lo cambies después.",
              12, MUTED, wraplength=420).pack(anchor="w", padx=18, pady=(6, 14))

        opts = card(tab)
        opts.pack(fill="x")
        label(opts, "Opciones", 14, weight="bold").pack(anchor="w", padx=18, pady=(14, 4))
        self.lang_var = tk.StringVar(value=LANGUAGES[self.app.cfg["language"]])
        row = self.option_row(opts, "Idioma", "El que hablas; «Automático» si alternas")
        option_menu(row, self.lang_var, list(LANGUAGES.values()), width=140).pack(side="right")
        self.vars = {}
        rows = [(k, t, d, self.app.cfg[k]) for k, t, d in self.OPTIONS]
        rows.append(("startup", "Iniciar con Windows", "Se abre solo al prender la computadora",
                     STARTUP_LNK.exists()))
        for key, title, desc, value in rows:
            self.vars[key] = self.switch_row(opts, title, desc, value)
        ctk.CTkFrame(opts, fg_color="transparent", height=8).pack()

    def option_row(self, master, title, desc):
        """Fila con título y descripción a la izquierda; el control va a la derecha."""
        row = ctk.CTkFrame(master, fg_color="transparent")
        row.pack(fill="x", padx=18, pady=5)
        text = ctk.CTkFrame(row, fg_color="transparent")
        text.pack(side="left", fill="x", expand=True)
        label(text, title, 13).pack(anchor="w")
        label(text, desc, 11, MUTED).pack(anchor="w")
        return row

    def switch_row(self, master, title, desc, value):
        var = tk.BooleanVar(value=value)
        row = self.option_row(master, title, desc)
        ctk.CTkSwitch(row, text="", variable=var, width=46, switch_width=42, switch_height=22,
                      progress_color=TURQUOISE, fg_color=BORDER, button_color=SMOKE,
                      button_hover_color=SMOKE).pack(side="right")
        return var

    # --- Pestaña Vocabulario -------------------------------------------------
    def build_vocab(self, tab):
        words_card = card(tab)
        words_card.pack(fill="x", pady=(6, 12))
        label(words_card, "Palabras que usas seguido", 14, weight="bold").pack(anchor="w", padx=18, pady=(14, 0))
        label(words_card, "Una por línea: nombres, marcas, términos técnicos.", 12, MUTED
              ).pack(anchor="w", padx=18, pady=(2, 8))
        self.words = ctk.CTkTextbox(words_card, height=120, fg_color=SURFACE_2, border_color=BORDER,
                                    border_width=1, corner_radius=8, text_color=SMOKE,
                                    font=font(13), scrollbar_button_color=BORDER, wrap="none")
        self.words.pack(fill="x", padx=18, pady=(0, 16))

        reps_card = card(tab)
        reps_card.pack(fill="both", expand=True)
        label(reps_card, "Reemplazos", 14, weight="bold").pack(anchor="w", padx=18, pady=(14, 0))
        label(reps_card, "Si aparece la palabra de la izquierda, se cambia por la de la derecha.",
              12, MUTED).pack(anchor="w", padx=18, pady=(2, 8))
        table = ctk.CTkFrame(reps_card, fg_color=SURFACE, corner_radius=0)
        table.pack(fill="x", padx=18)
        self.reps = ttk.Treeview(table, columns=("wrong", "right"), show="headings",
                                 height=8, selectmode="browse")
        self.reps.heading("wrong", text="SI ESCRIBE", anchor="w")
        self.reps.heading("right", text="CAMBIAR POR", anchor="w")
        self.reps.column("wrong", width=190)
        self.reps.column("right", width=190)
        sb = ctk.CTkScrollbar(table, command=self.reps.yview, button_color=BORDER, width=12)
        self.reps.configure(yscrollcommand=sb.set)
        self.reps.pack(side="left", fill="x", expand=True)
        sb.pack(side="right", fill="y")
        self.reps.bind("<<TreeviewSelect>>", lambda e: self.load_selected_rep())

        row = ctk.CTkFrame(reps_card, fg_color="transparent")
        row.pack(fill="x", padx=18, pady=(10, 16))
        self.wrong_entry = entry(row, placeholder_text="Si escribe…", width=130)
        self.wrong_entry.pack(side="left")
        label(row, "→", 14, MUTED).pack(side="left", padx=6)
        self.right_entry = entry(row, placeholder_text="Cambiar por…", width=130)
        self.right_entry.pack(side="left")
        self.right_entry.bind("<Return>", lambda e: self.add_rep())
        ghost_button(row, "Quitar", self.remove_rep, width=64).pack(side="right")
        secondary_button(row, "Agregar", self.add_rep, width=80).pack(side="right", padx=(0, 4))
        self.load_vocab(self.app.cfg)

    def load_vocab(self, cfg):
        self.words.delete("1.0", "end")
        self.words.insert("1.0", "\n".join(cfg["vocab"]))
        self.reps.delete(*self.reps.get_children())
        for wrong, right in cfg["replacements"]:
            self.reps.insert("", "end", values=(wrong, right))

    def set_entries(self, wrong, right):
        for e, v in ((self.wrong_entry, wrong), (self.right_entry, right)):
            e.delete(0, "end")
            if v:
                e.insert(0, v)
        if not wrong and not right:
            self.win.focus_set()   # para que vuelvan a mostrarse los textos de ejemplo

    def load_selected_rep(self):
        sel = self.reps.selection()
        if sel:
            wrong, right = self.reps.item(sel[0], "values")
            self.set_entries(str(wrong), str(right))

    def add_rep(self):
        wrong, right = self.wrong_entry.get().strip(), self.right_entry.get().strip()
        if not wrong or not right:
            return
        for iid in self.reps.get_children():  # si ya existe, se actualiza
            if str(self.reps.item(iid, "values")[0]).lower() == wrong.lower():
                self.reps.item(iid, values=(wrong, right))
                break
        else:
            self.reps.insert("", "end", values=(wrong, right))
        self.set_entries("", "")

    def remove_rep(self):
        for iid in self.reps.selection():
            self.reps.delete(iid)
        self.set_entries("", "")

    # --- Captura de tecla y guardado -----------------------------------------
    def focus(self):
        bring_to_front(self.win)

    def set_msg(self, text, color=MUTED):
        self.msg.configure(text=text, text_color=color)

    def start_capture(self):
        self.key_box.configure(text="Presiona una tecla…", text_color=LAVENDER)
        self.set_msg("Esc para cancelar. Recomendadas: Ctrl derecho, AltGr, F9, Pausa.")
        self.change_btn.configure(state="disabled")
        self.app.capture = True

    def captured(self, name):
        self.change_btn.configure(state="normal")
        self.key_box.configure(text_color=SMOKE)
        if name == "esc":
            self.key_box.configure(text=key_label(self.hotkey))
            self.set_msg("Sin cambios.")
            return
        problem = key_problem(name)
        if problem:
            self.key_box.configure(text=key_label(self.hotkey))
            self.set_msg(problem, ERROR)
            return
        self.hotkey = "alt gr" if name in ALTGR_NAMES else name
        self.key_box.configure(text=key_label(self.hotkey))
        self.set_msg("Presiona Guardar para usar esta tecla.", TURQUOISE)

    def save(self):
        self.add_rep()  # por si escribió un reemplazo y no presionó Agregar
        values = {k: v.get() for k, v in self.vars.items()}
        startup = values.pop("startup")
        cfg = dict(self.app.cfg, hotkey=self.hotkey, **values)
        mic = self.mic_var.get()
        cfg["mic"] = "" if mic == DEFAULT_MIC_LABEL else mic
        cfg["language"] = next(code for code, name in LANGUAGES.items()
                               if name == self.lang_var.get())
        cfg["vocab"] = clean_words(self.words.get("1.0", "end").splitlines())
        cfg["replacements"] = [[str(v) for v in self.reps.item(i, "values")]
                               for i in self.reps.get_children()]
        try:
            if startup and not STARTUP_LNK.exists():
                make_shortcut(STARTUP_LNK)
            elif not startup and STARTUP_LNK.exists():
                STARTUP_LNK.unlink()
        except Exception as e:
            log(f"No se pudo cambiar el inicio con Windows: {e}")
        self.app.apply_config(cfg)
        self.close()

    def close(self):
        self.app.capture = False
        self.app.settings = None
        self.win.destroy()


# --- Pestaña Historial --------------------------------------------------------
class HistoryPanel:
    def __init__(self, app, tab, win):
        self.app = app
        self.win = win
        self.items = []

        self.search = entry(tab, placeholder_text="Buscar en tus transcripciones…")
        self.search.pack(fill="x", pady=(6, 10))
        self.search.bind("<KeyRelease>", lambda e: self.refresh())

        list_card = card(tab)
        list_card.pack(fill="both", expand=True)
        table = ctk.CTkFrame(list_card, fg_color=SURFACE, corner_radius=0)
        table.pack(fill="both", expand=True, padx=12, pady=12)
        self.tree = ttk.Treeview(table, columns=("when", "text"), show="headings",
                                 selectmode="browse")
        self.tree.heading("when", text="CUÁNDO", anchor="w")
        self.tree.heading("text", text="TEXTO", anchor="w")
        self.tree.column("when", width=100, stretch=False)
        self.tree.column("text", width=360)
        sb = ctk.CTkScrollbar(table, command=self.tree.yview, button_color=BORDER, width=12)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", lambda e: self.show_selected())
        self.tree.bind("<Double-1>", lambda e: self.copy())

        self.preview = ctk.CTkTextbox(tab, height=130, fg_color=SURFACE, border_width=0,
                                      corner_radius=14, text_color=SMOKE, font=font(13),
                                      wrap="word", scrollbar_button_color=BORDER)
        self.preview.pack(fill="x", pady=(12, 0))
        # Se puede seleccionar y copiar, pero no editar
        self.preview.bind("<Key>", lambda e: None if (e.state & 0x4) else "break")

        bottom = ctk.CTkFrame(tab, fg_color="transparent")
        bottom.pack(fill="x", pady=(12, 4))
        self.status = label(bottom, "", 12, TURQUOISE)
        self.status.pack(side="left")
        primary_button(bottom, "Copiar", self.copy, width=96).pack(side="right")
        secondary_button(bottom, "Corregir…", self.correct, width=96).pack(side="right", padx=(0, 8))
        ghost_button(bottom, "Eliminar", self.delete, color=ERROR, width=84).pack(side="right", padx=(0, 4))
        label(tab, "Tip: selecciona en el texto la palabra mal escrita y presiona Corregir…", 11, MUTED
              ).pack(anchor="w", pady=(2, 4))
        self.refresh()

    def refresh(self):
        q = self.search.get().strip().lower()
        self.items = [i for i in self.app.history.snapshot() if q in i["text"].lower()]
        self.tree.delete(*self.tree.get_children())
        for n, item in enumerate(self.items):
            self.tree.insert("", "end", iid=str(n),
                             values=(when_label(item["t"]), snippet(item["text"], 90)))
        if self.items:
            self.tree.selection_set("0")
        else:
            self.set_preview("Todavía no hay transcripciones." if not q else "Sin resultados.")

    def selected(self):
        sel = self.tree.selection()
        return self.items[int(sel[0])] if sel else None

    def set_preview(self, text):
        self.preview.delete("1.0", "end")
        self.preview.insert("1.0", text)

    def show_selected(self):
        item = self.selected()
        if item:
            self.set_preview(item["text"])
            self.status.configure(text="")

    def copy(self):
        item = self.selected()
        if item:
            pyperclip.copy(item["text"])
            self.status.configure(text="Copiado al portapapeles ✓", text_color=TURQUOISE)

    def correct(self):
        item = self.selected()
        if not item:
            return
        try:
            picked = self.preview.get("sel.first", "sel.last").strip()
        except tk.TclError:
            picked = ""

        d = new_window(self.app, "Corregir palabra", 420, 300)
        d.transient(self.win)
        body = ctk.CTkFrame(d, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=22, pady=18)
        label(body, "Corregir palabra", 16, weight="bold").pack(anchor="w")
        label(body, "Se corrige esta transcripción y las próximas.", 12, MUTED).pack(anchor="w", pady=(0, 12))
        label(body, "Whisper escribió", 12, MUTED).pack(anchor="w")
        e1 = entry(body)
        e1.pack(fill="x", pady=(2, 8))
        label(body, "Lo correcto es", 12, MUTED).pack(anchor="w")
        e2 = entry(body)
        e2.pack(fill="x", pady=(2, 10))
        for e in (e1, e2):
            if picked:
                e.insert(0, picked)
        add_word = tk.BooleanVar(value=True)
        ctk.CTkCheckBox(body, text="Agregar a «Palabras que usas seguido»", variable=add_word,
                        font=font(12), text_color=SMOKE, fg_color=LAVENDER,
                        hover_color=LAVENDER_HOVER, checkmark_color=NAVY, border_color=MUTED,
                        corner_radius=5, checkbox_width=20, checkbox_height=20).pack(anchor="w")

        def ok(*_):
            w, r = e1.get().strip(), e2.get().strip()
            if not w or not r or w == r:
                d.destroy()
                return
            self.app.add_correction(w, r, add_word.get())
            self.app.history.update(item["t"], apply_replacements(item["text"], [[w, r]]))
            d.destroy()
            self.refresh()
            self.status.configure(text=f"«{w}» → «{r}» guardado ✓", text_color=TURQUOISE)

        buttons = ctk.CTkFrame(body, fg_color="transparent")
        buttons.pack(side="bottom", fill="x")
        primary_button(buttons, "Guardar", ok, width=110).pack(side="right")
        ghost_button(buttons, "Cancelar", d.destroy, width=100).pack(side="right", padx=(0, 8))
        d.bind("<Return>", ok)
        d.bind("<Escape>", lambda e: d.destroy())
        bring_to_front(d)
        target = e2 if picked else e1
        d.after(300, lambda: (target.focus_set(), target.select_range(0, "end")))
        d.grab_set()

    def delete(self):
        item = self.selected()
        if item:
            self.app.history.remove(item["t"])
            self.app.tray.update_menu()
            self.refresh()
            self.status.configure(text="Eliminado", text_color=MUTED)


# --- Aplicación ---------------------------------------------------------------
class Susurro:
    def __init__(self):
        self.cfg = load_config()
        self.model = None
        self.model_name = None
        self.loading_model = None
        if self.cfg["model"] not in MODELS:
            self.cfg["model"] = DEFAULTS["model"]
        if self.cfg["language"] not in LANGUAGES:
            self.cfg["language"] = DEFAULTS["language"]
        self.state = "idle"        # idle | pending | hold | toggle
        self.frames = []
        self.stream = None
        self.level = 0.0
        self.busy_jobs = 0
        self.press_id = 0
        self.stop_armed = False    # en modo toggle, el siguiente toque limpio detiene
        self.hotkey_down = False
        self.hotkey_clean = False
        self.capture = False       # la ventana de configuración espera una tecla
        self.events = queue.Queue()   # eventos del teclado -> hilo de control
        self.ui = queue.Queue()       # acciones para las ventanas (hilo de tkinter)
        self.root = None
        self.overlay = None
        self.settings = None
        self.history = History()
        self.tray = pystray.Icon(
            "susurro", ICONS["loading"], "Susurro: cargando modelo...",
            menu=pystray.Menu(
                pystray.MenuItem("Abrir Susurro", lambda: self.ui.put(("open", None)),
                                 default=True),
                pystray.MenuItem(lambda item: f"Grabar / detener ({self.key_name})",
                                 lambda: self.events.put(("menu", 0))),
                pystray.MenuItem("Historial", pystray.Menu(self.history_menu)),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Sitio web de Susurro", lambda: webbrowser.open(WEBSITE)),
                pystray.MenuItem("Salir", self.quit),
            ),
        )

    def history_menu(self):
        items = self.history.snapshot()[:HISTORY_MENU]
        if not items:
            yield pystray.MenuItem("(vacío)", None, enabled=False)
        for item in items:
            # En los menús de Windows '&' marca un atajo; '&&' lo muestra tal cual.
            label = f"{when_label(item['t'])}   {snippet(item['text'])}".replace("&", "&&")
            yield pystray.MenuItem(label, self.copy_action(item["text"]))
        yield pystray.Menu.SEPARATOR
        yield pystray.MenuItem("Ver todo el historial…", lambda: self.ui.put(("open", "Historial")))
        yield pystray.MenuItem("Borrar historial…", lambda: self.ui.put(("clear_history", None)),
                               enabled=bool(items))

    def copy_action(self, text):
        return lambda: self.copy_from_menu(text)

    def copy_from_menu(self, text):
        pyperclip.copy(text)
        try:
            self.tray.notify(snippet(text, 120), "Copiado al portapapeles")
        except Exception:
            pass

    def whisper_language(self):
        """Código para Whisper; None le pide que detecte el idioma."""
        lang = self.cfg["language"]
        return None if lang == "auto" else lang

    @property
    def key_name(self):
        return key_label(self.cfg["hotkey"])

    def apply_config(self, cfg):
        changed_key = cfg["hotkey"] != self.cfg["hotkey"]
        self.cfg = cfg
        save_config(cfg)
        self.hotkey_down = False
        log(f"Configuración guardada ({len(cfg['vocab'])} palabras, "
            f"{len(cfg['replacements'])} reemplazos)")
        if changed_key:
            log(f"Nueva tecla para dictar: {self.key_name}")
        if (cfg["model"] not in (self.model_name, self.loading_model)
                and self.model is not None):
            threading.Thread(target=self.load_model, args=(cfg["model"],), daemon=True).start()
        self.refresh_ui()
        self.tray.update_menu()

    def add_correction(self, wrong, right, add_word):
        """Guarda un reemplazo (y opcionalmente la palabra) desde el historial."""
        reps = [r for r in self.cfg["replacements"] if r[0].lower() != wrong.lower()]
        cfg = dict(self.cfg, replacements=reps + [[wrong, right]])
        if add_word:
            cfg["vocab"] = clean_words(self.cfg["vocab"] + [right])
        self.apply_config(cfg)
        if self.settings:  # mantener al día la pestaña Vocabulario si está abierta
            self.settings.load_vocab(cfg)

    def beep(self, freqs):
        if self.cfg["sounds"]:
            beep(freqs)

    # --- Estado visible ------------------------------------------------------
    def set_tray(self, state, title):
        self.tray.icon = ICONS[state]
        self.tray.title = f"Susurro: {title}"

    def refresh_ui(self):
        if self.model is None:
            return
        if self.state in ("hold", "toggle"):
            self.set_tray("recording", "grabando...")
            self.ui.put(("show", self.state))
        elif self.busy_jobs:
            self.set_tray("busy", "transcribiendo...")
            self.ui.put(("show", "busy"))
        else:
            self.set_tray("idle", f"listo ({self.key_name})")
            self.ui.put(("hide", None))

    # --- Modelo --------------------------------------------------------------
    def load_model(self, name):
        """Carga el modelo indicado. Si ya hay uno, se sigue usando hasta que el nuevo esté listo."""
        switching = self.model is not None
        self.loading_model = name
        try:
            self._load_model(name, switching)
        finally:
            self.loading_model = None

    def _load_model(self, name, switching):
        if switching:
            self.tray.title = f"Susurro: cargando {MODELS[name]}…"
            log(f"Cambiando a {name} (la primera vez se descarga)...")
        try:
            try:
                model = WhisperModel(name, device="cuda", compute_type="float16")
                log(f"Modelo {name} cargado en GPU")
            except Exception as e:
                log(f"No se pudo usar la GPU ({e}); usando CPU")
                model = WhisperModel(name, device="cpu", compute_type="int8")
            # Calentamiento para que la primera transcripción sea rápida
            list(model.transcribe(np.zeros(SAMPLE_RATE, dtype=np.float32),
                                  language=self.whisper_language())[0])
        except Exception as e:
            log(f"No se pudo cargar {name}: {e}")
            if switching:
                self.tray.notify(f"No se pudo cargar {MODELS[name]}. Sigo con el modelo anterior.", "Susurro")
                self.refresh_ui()
            return
        self.model, self.model_name = model, name   # el anterior se libera solo
        self.refresh_ui()
        log(f"Listo. Toca o mantén {self.key_name} para dictar.")
        if switching:
            self.tray.notify(f"Ahora usando: {MODELS[name]}", "Susurro")

    # --- Audio ---------------------------------------------------------------
    def audio_callback(self, indata, frames, t, status):
        self.frames.append(indata.copy())
        self.level = float(np.sqrt(np.mean(indata ** 2)))

    def open_mic(self):
        self.frames = []
        self.level = 0.0
        refresh_audio_devices()   # nota micrófonos nuevos o cambios en Windows
        wanted = self.cfg["mic"]
        device = find_mic(wanted)
        if wanted and device is None:
            log(f"El micrófono «{wanted}» no está conectado; usando el predeterminado")
        try:
            self.stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1, device=device,
                                         dtype="float32", callback=self.audio_callback)
        except Exception as e:
            if device is None:
                raise
            log(f"No se pudo abrir «{wanted}» ({e}); usando el predeterminado")
            self.stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1,
                                         dtype="float32", callback=self.audio_callback)
        self.stream.start()

    def close_mic(self):
        if self.stream is not None:
            self.stream.stop()
            self.stream.close()
            self.stream = None
        self.level = 0.0
        frames, self.frames = self.frames, []
        return frames

    # --- Máquina de estados (solo corre en el hilo de control) ---------------
    def control_loop(self):
        while True:
            kind, arg = self.events.get()
            if kind == "quit":
                return
            if self.model is None:
                continue
            try:
                self.handle(kind, arg)
            except Exception as e:
                log(f"Error: {e}")
                self.close_mic()
                self.state = "idle"
                self.refresh_ui()

    def handle(self, kind, arg):
        if kind == "down":
            if self.state == "idle":
                # Abrimos el micrófono de inmediato para no perder el inicio,
                # pero aún no avisamos: puede ser una combinación como Ctrl+C.
                self.open_mic()
                self.state = "pending"
                self.press_id += 1
                pid = self.press_id
                threading.Timer(HOLD_SECONDS, lambda: self.events.put(("hold", pid))).start()
            elif self.state == "toggle":
                self.stop_armed = True

        elif kind == "hold" and self.state == "pending" and arg == self.press_id:
            self.state = "hold"
            self.start_feedback("mantener")

        elif kind == "combo":  # la tecla se usó junto con otra
            if self.state == "pending":
                self.close_mic()
                self.state = "idle"
            self.stop_armed = False

        elif kind == "up":
            if self.state == "pending":
                self.state = "toggle"
                self.stop_armed = False
                self.start_feedback("toque")
            elif self.state == "hold" or (self.state == "toggle" and self.stop_armed):
                self.finish()

        elif kind == "esc" and self.state in ("pending", "hold", "toggle"):
            was_pending = self.state == "pending"
            self.close_mic()
            self.state = "idle"
            if not was_pending:
                self.beep([440, 330])
                log("Grabación cancelada")
            self.refresh_ui()

        elif kind == "menu":
            if self.state == "idle":
                self.open_mic()
                self.state = "toggle"
                self.stop_armed = False
                self.start_feedback("menú")
            elif self.state in ("toggle", "hold"):
                self.finish()

    def start_feedback(self, how):
        self.beep(880)
        log(f"Grabando ({how})...")
        self.refresh_ui()

    def finish(self):
        frames = self.close_mic()
        self.state = "idle"
        self.stop_armed = False
        self.beep(440)
        log("Grabación detenida")
        self.busy_jobs += 1
        self.refresh_ui()
        threading.Thread(target=self.transcribe, args=(frames,), daemon=True).start()

    def transcribe(self, frames):
        try:
            if not frames:
                return
            audio = np.concatenate(frames).flatten()
            if len(audio) < SAMPLE_RATE * MIN_SECONDS:
                log("Grabación demasiado corta, ignorada")
                return
            peak = float(np.abs(audio).max())
            log(f"Audio: {len(audio) / SAMPLE_RATE:.1f}s, nivel máximo {peak:.3f}")
            if peak < 0.001:
                log("El micrófono no captó sonido (¿silenciado o micrófono equivocado?)")
                return
            audio = audio / peak * 0.9  # normaliza el volumen para micrófonos bajos
            start = time.time()
            vocab = self.cfg["vocab"]
            segments, _ = self.model.transcribe(
                audio, language=self.whisper_language(), beam_size=5, vad_filter=True,
                condition_on_previous_text=False,
                # Pistas de vocabulario; se aplican a cada tramo de 30 s del audio
                hotwords=", ".join(vocab) if vocab else None,
            )
            parts = []
            for seg in segments:
                kept = clean_segment(seg.text, seg.no_speech_prob, seg.avg_logprob)
                if kept != seg.text.strip():
                    log(f"   (frase fantasma descartada: {seg.text.strip()!r})")
                if kept:
                    parts.append(kept)
            raw = " ".join(parts).strip()
            text = apply_replacements(raw, self.cfg["replacements"])
            log(f"({time.time() - start:.1f}s) {text!r}")
            if text != raw:
                log(f"   (antes de reemplazos: {raw!r})")
            if not text:
                return
            if self.cfg["save_history"]:
                self.history.add(text)
                self.tray.update_menu()
                self.ui.put(("history_changed", None))
            pyperclip.copy(text)
            if self.cfg["auto_paste"]:
                time.sleep(0.1)
                keyboard.send("ctrl+v")
        except Exception as e:
            log(f"Error al transcribir: {e}")
        finally:
            self.busy_jobs -= 1
            self.refresh_ui()

    # --- Teclado (corre dentro del gancho de Windows: debe ser rapidísimo) ----
    def on_key(self, e):
        down = e.event_type == keyboard.KEY_DOWN
        name = e.name
        if self.capture:
            if down:
                self.capture = False
                self.ui.put(("captured", name))
            return False   # la tecla elegida no llega a otras apps
        hotkey = self.cfg["hotkey"]
        if name and key_matches(name, hotkey):
            if down and not self.hotkey_down:   # ignora la auto-repetición
                self.hotkey_down = True
                self.hotkey_clean = True
                self.events.put(("down", 0))
            elif not down and self.hotkey_down:
                self.hotkey_down = False
                if self.hotkey_clean:
                    self.events.put(("up", 0))
            # Ctrl/Alt/Shift pasan (para sus combinaciones); F9, Pausa, etc. se bloquean
            return hotkey in MODIFIER_KEYS
        if down and self.hotkey_down and self.hotkey_clean:
            if not (hotkey in ALTGR_NAMES and name in ("ctrl", "left ctrl")):  # AltGr simula un Ctrl
                self.hotkey_clean = False
                self.events.put(("combo", 0))
        if name == "esc" and self.state in ("pending", "hold", "toggle"):
            if down:
                self.events.put(("esc", 0))
            return False   # Esc se usa para cancelar; no llega a la otra app
        return True

    # --- Ventanas / ciclo de vida --------------------------------------------
    def pump_ui(self):
        try:
            while True:
                action, arg = self.ui.get_nowait()
                if action == "quit":
                    self.root.destroy()
                    return
                if action == "show" and self.cfg["overlay"]:
                    self.overlay.show(arg, self.key_name)
                elif action in ("show", "hide"):
                    self.overlay.hide()
                elif action == "open":   # arg: pestaña a mostrar, o None para dejar la actual
                    if self.settings:
                        if arg:
                            self.settings.show_tab(arg)
                        self.settings.focus()
                    else:
                        self.settings = MainWindow(self, arg or "Historial")
                elif action == "captured" and self.settings:
                    self.settings.captured(arg)
                elif action == "history_changed" and self.settings:
                    self.settings.history.refresh()
                elif action == "clear_history":
                    if confirm(self, "Borrar historial",
                               "¿Borrar todo el historial de transcripciones?", "Borrar"):
                        self.history.clear()
                        self.tray.update_menu()
                        if self.settings:
                            self.settings.history.refresh()
        except queue.Empty:
            pass
        self.root.after(30, self.pump_ui)

    def listen_for_second_instance(self, server):
        """Si abren el acceso directo otra vez, mostramos la configuración."""
        while True:
            try:
                conn, _ = server.accept()
                conn.close()
                self.ui.put(("open", None))
            except OSError:
                return

    def quit(self, *_):
        keyboard.unhook_all()
        self.events.put(("quit", None))
        self.ui.put(("quit", None))
        self.tray.stop()

    def run(self, server):
        trim_log()
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)  # texto nítido en pantallas con escala
        except Exception:
            pass
        ensure_icon_file()
        try:  # identidad propia en la barra de tareas (si no, aparece como Python)
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
        except Exception:
            pass
        self.root = ctk.CTk()
        self.root.iconbitmap(default=str(ICON_FILE))  # ícono de todas las ventanas
        apply_theme(self.root)
        self.root.withdraw()
        self.overlay = Overlay(tk.Toplevel(self.root), lambda: self.level)
        keyboard.hook(self.on_key, suppress=True)
        threading.Thread(target=self.load_model, args=(self.cfg["model"],), daemon=True).start()
        threading.Thread(target=self.control_loop, daemon=True).start()
        threading.Thread(target=self.listen_for_second_instance, args=(server,), daemon=True).start()
        self.tray.run_detached()
        self.pump_ui()
        self.root.mainloop()


def desktop_dir():
    buf = ctypes.create_unicode_buffer(260)
    ctypes.windll.shell32.SHGetFolderPathW(0, 0x10, 0, 0, buf)  # Escritorio (puede estar en OneDrive)
    return Path(buf.value)


def setup_cli():
    """Opciones para el instalador. Devuelve True si se usó alguna (y no hay que abrir la app)."""
    if "--crear-accesos" in sys.argv:
        for lnk in (desktop_dir() / "Susurro.lnk", STARTUP_LNK, BASE / "Susurro.lnk"):
            make_shortcut(lnk)
            print(f"Acceso directo creado: {lnk}")
        ctypes.windll.shell32.SHChangeNotify(0x08000000, 0, None, None)  # refresca íconos
        return True
    if "--descargar-modelo" in sys.argv:
        name = load_config()["model"]
        print(f"Descargando el modelo {name} (solo la primera vez)...")
        from faster_whisper.utils import download_model
        print(f"Modelo listo en: {download_model(name)}")
        return True
    return False


if __name__ == "__main__":
    if setup_cli():
        sys.exit(0)
    server = socket.socket()
    try:
        server.bind(("127.0.0.1", INSTANCE_PORT))
        server.listen()
    except OSError:
        # Ya está abierto: le pedimos que muestre la configuración y salimos.
        try:
            socket.create_connection(("127.0.0.1", INSTANCE_PORT), timeout=2).close()
        except OSError:
            pass
        sys.exit(0)
    Susurro().run(server)
