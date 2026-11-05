import re
import sys
import os
import time
import threading
import tkinter as tk
from tkinter import ttk, messagebox

import cv2
import numpy as np
import pyautogui
import pytesseract
from pytesseract import Output
import keyboard
import pygetwindow as gw


# If Tesseract is installed in a different folder, change this path.
TESSERACT_CANDIDATES = [
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
]

TARGETS = {
    "GOTOWY": ("GOTOWY",),
    "ZAGRAJ PONOWNIE": ("ZAGRAJ PONOWNIE",),
}

SCAN_INTERVAL = 0.65
CLICK_COOLDOWN = 4.0
MIN_CONFIDENCE = 55.0

pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.08


def configure_tesseract():
    root = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    bundled_dir = os.path.join(root, "Tesseract-OCR")
    bundled_exe = os.path.join(bundled_dir, "tesseract.exe")
    if os.path.isfile(bundled_exe):
        pytesseract.pytesseract.tesseract_cmd = bundled_exe
        os.environ["PATH"] = bundled_dir + os.pathsep + os.environ.get("PATH", "")
        os.environ["TESSDATA_PREFIX"] = os.path.join(bundled_dir, "tessdata")
        if hasattr(os, "add_dll_directory"):
            try:
                os.add_dll_directory(bundled_dir)
            except OSError:
                pass
        return True
    for candidate in TESSERACT_CANDIDATES:
        if os.path.isfile(candidate):
            pytesseract.pytesseract.tesseract_cmd = candidate
            return True
    # Also accept Tesseract if it is already on PATH.
    try:
        pytesseract.get_tesseract_version()
        return True
    except Exception:
        return False


def normalize(s):
    s = (s or "").upper()
    s = s.replace("Ł", "L")
    s = re.sub(r"[^A-ZĄĆĘŁŃÓŚŹŻ ]", " ", s)
    return " ".join(s.split())


def find_target(data):
    """Return (target, center_x, center_y, confidence) for a matching OCR line."""
    lines = {}
    n = len(data["text"])
    for i in range(n):
        word = (data["text"][i] or "").strip()
        if not word:
            continue
        try:
            conf = float(data["conf"][i])
        except (ValueError, TypeError):
            continue
        if conf < MIN_CONFIDENCE:
            continue

        key = (
            data["block_num"][i],
            data["par_num"][i],
            data["line_num"][i],
        )
        x, y = int(data["left"][i]), int(data["top"][i])
        w, h = int(data["width"][i]), int(data["height"][i])
        lines.setdefault(key, []).append((x, y, w, h, word, conf))

    for words in lines.values():
        words.sort(key=lambda item: item[0])
        phrase = normalize(" ".join(item[4] for item in words))
        for target in TARGETS:
            if phrase == target or target in phrase:
                x1 = min(item[0] for item in words)
                y1 = min(item[1] for item in words)
                x2 = max(item[0] + item[2] for item in words)
                y2 = max(item[1] + item[3] for item in words)
                confidence = sum(item[5] for item in words) / len(words)
                return target, (x1 + x2) // 2, (y1 + y2) // 2, confidence
    return None


class ClickerApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Fortnite Button Clicker")
        self.root.resizable(False, False)
        self.enabled = False
        self.running = True
        self.last_click = 0.0
        self.last_target = None
        self.target_missing_since = time.time()
        self.worker = None

        frame = ttk.Frame(root, padding=16)
        frame.grid()

        ttk.Label(
            frame, text="Fortnite Button Clicker",
            font=("Segoe UI", 15, "bold")
        ).grid(row=0, column=0, columnspan=2, pady=(0, 10))

        self.status = tk.StringVar(value="Status: WYŁĄCZONY")
        ttk.Label(frame, textvariable=self.status).grid(
            row=1, column=0, columnspan=2, sticky="w", pady=4
        )

        self.detected = tk.StringVar(value="Ostatnie wykrycie: —")
        ttk.Label(frame, textvariable=self.detected).grid(
            row=2, column=0, columnspan=2, sticky="w", pady=4
        )

        self.toggle_button = ttk.Button(
            frame, text="Włącz (F6)", command=self.toggle
        )
        self.toggle_button.grid(row=3, column=0, sticky="ew", padx=(0, 5), pady=10)

        ttk.Button(
            frame, text="Zamknij (F7)", command=self.close
        ).grid(row=3, column=1, sticky="ew", padx=(5, 0), pady=10)

        ttk.Label(
            frame,
            text="Działa tylko, gdy aktywne jest okno Fortnite.\n"
                 "Najlepiej ustaw grę w trybie okienkowym bez ramek.",
            justify="left"
        ).grid(row=4, column=0, columnspan=2, sticky="w", pady=(4, 0))

        self.root.protocol("WM_DELETE_WINDOW", self.close)
        keyboard.add_hotkey("F6", lambda: self.root.after(0, self.toggle))
        keyboard.add_hotkey("F7", lambda: self.root.after(0, self.close))

        self.worker = threading.Thread(target=self.scan_loop, daemon=True)
        self.worker.start()

    def toggle(self):
        self.enabled = not self.enabled
        self.status.set("Status: " + ("AKTYWNY" if self.enabled else "WYŁĄCZONY"))
        self.toggle_button.configure(
            text="Wyłącz (F6)" if self.enabled else "Włącz (F6)"
        )
        if not self.enabled:
            self.last_target = None

    def fortnite_is_active(self):
        try:
            window = gw.getActiveWindow()
            return bool(window and "fortnite" in (window.title or "").lower())
        except Exception:
            return False

    def scan_loop(self):
        while self.running:
            if not self.enabled or not self.fortnite_is_active():
                time.sleep(0.25)
                continue

            try:
                screenshot = pyautogui.screenshot()
                frame = cv2.cvtColor(np.array(screenshot), cv2.COLOR_RGB2BGR)
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                # Upscale slightly to improve OCR on small UI labels.
                gray = cv2.resize(gray, None, fx=1.35, fy=1.35,
                                  interpolation=cv2.INTER_CUBIC)
                data = pytesseract.image_to_data(
                    gray, lang="eng", config="--oem 3 --psm 11",
                    output_type=Output.DICT
                )
                match = find_target(data)

                if match:
                    target, x, y, confidence = match
                    # Convert OCR coordinates back to original screen coordinates.
                    x = int(x / 1.35)
                    y = int(y / 1.35)
                    now = time.time()

                    if target != self.last_target and now - self.last_click >= CLICK_COOLDOWN:
                        pyautogui.click(x, y)
                        self.last_click = now
                        self.last_target = target
                        self.root.after(
                            0,
                            lambda t=target, c=confidence:
                                self.detected.set(
                                    f"Ostatnie wykrycie: {t} ({c:.0f}%)"
                                )
                        )
                    self.target_missing_since = now
                else:
                    if time.time() - self.target_missing_since > 1.2:
                        self.last_target = None

            except pyautogui.FailSafeException:
                self.root.after(0, self.toggle)
            except Exception as exc:
                self.root.after(
                    0, lambda e=str(exc):
                        self.detected.set("Błąd: " + e[:90])
                )

            time.sleep(SCAN_INTERVAL)

    def close(self):
        self.running = False
        self.enabled = False
        try:
            keyboard.unhook_all_hotkeys()
        except Exception:
            pass
        self.root.destroy()


def main():
    if not configure_tesseract():
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(
            "Brak Tesseract OCR",
            "Nie znaleziono Tesseract OCR.\n\n"
            "Zainstaluj Tesseract OCR dla Windows, a następnie uruchom program ponownie.\n"
            "Domyślna ścieżka: C:\\Program Files\\Tesseract-OCR\\tesseract.exe"
        )
        return

    try:
        pytesseract.get_languages(config="")
        # Both language packs are needed for Polish and English Fortnite UI.
        langs = pytesseract.get_languages(config="")
        if "eng" not in langs:
            raise RuntimeError(
                "Brakuje pakietu językowego eng w instalacji Tesseract."
            )
    except Exception as exc:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(
            "Problem z OCR",
            str(exc) + "\n\nDołóż pakiety językowe pol i eng do Tesseract."
        )
        return

    root = tk.Tk()
    ClickerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
