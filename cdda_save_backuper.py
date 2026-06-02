#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CDDA Save Backuper
Локальный менеджер сохранений Cataclysm: Dark Days Ahead / Bright Nights.
Зависимости: только стандартная библиотека Python 3.10+.
"""

from __future__ import annotations

import csv
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

APP_NAME = "CDDA Save Backuper"
APP_VERSION = "2026-06-02-r6"


def get_app_dir() -> Path:
    """Папка, рядом с которой лежит программа.

    Для .py это папка скрипта, для собранного .exe — папка exe.
    Все настройки программы хранятся здесь, а не в профиле пользователя.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


APP_DIR = get_app_dir()
CONFIG_DIR = APP_DIR / "_cdda_save_backuper_data"
CONFIG_PATH = CONFIG_DIR / "config.json"
OLD_CONFIG_DIR = Path.home() / ".cdda_save_backuper"
OLD_CONFIG_PATH = OLD_CONFIG_DIR / "config.json"
NOTES_FILENAME = "_cdda_save_notes.json"
DELETED_DIRNAME = "_deleted_saves_by_backuper"
DEFAULT_BACKUP_DIRNAME = "_cdda_save_backups"
DEFAULT_GAME_PROCESSES = "cataclysm-tiles.exe, cataclysm.exe, cataclysm-tiles, cataclysm"

IGNORED_SAVE_DIRS = {
    DEFAULT_BACKUP_DIRNAME.lower(),
    DELETED_DIRNAME.lower(),
    "cddabackups",
    "backups",
    ".git",
    "graveyard",
    "memorial",
}


# ---------- common helpers ----------

def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def human_size(num_bytes: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(num_bytes)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{num_bytes} B"


def safe_name(name: str) -> str:
    forbidden = '<>:"/\\|?*\n\r\t'
    clean = "".join("_" if ch in forbidden else ch for ch in name).strip().strip(".")
    return clean or "save"


def get_dir_size_and_mtime(path: Path) -> Tuple[int, float, int]:
    total = 0
    latest = path.stat().st_mtime if path.exists() else 0.0
    files_count = 0
    for root, dirs, filenames in os.walk(path):
        dirs[:] = [d for d in dirs if d.lower() not in IGNORED_SAVE_DIRS]
        for filename in filenames:
            fp = Path(root) / filename
            try:
                st = fp.stat()
            except OSError:
                continue
            total += st.st_size
            latest = max(latest, st.st_mtime)
            files_count += 1
    return total, latest, files_count


def wait_until_stable(path: Path, stable_seconds: float = 3.0, timeout: float = 45.0) -> bool:
    """Ждём, пока размер/mtime перестанут меняться, чтобы не снять полузаписанный save."""
    start = time.time()
    last: Optional[Tuple[int, float, int]] = None
    stable_since: Optional[float] = None
    while time.time() - start <= timeout:
        try:
            current = get_dir_size_and_mtime(path)
        except OSError:
            return False
        if current == last:
            if stable_since is None:
                stable_since = time.time()
            if time.time() - stable_since >= stable_seconds:
                return True
        else:
            last = current
            stable_since = None
        time.sleep(0.5)
    return False


def zip_directory(src_dir: Path, zip_path: Path, meta: Dict[str, Any]) -> None:
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_zip = zip_path.with_suffix(zip_path.suffix + ".tmp")
    if tmp_zip.exists():
        tmp_zip.unlink()
    with zipfile.ZipFile(tmp_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        zf.writestr("__cdda_backup_meta.json", json.dumps(meta, ensure_ascii=False, indent=2))
        for root, dirs, filenames in os.walk(src_dir):
            dirs[:] = [d for d in dirs if d.lower() not in IGNORED_SAVE_DIRS]
            for fn in filenames:
                fp = Path(root) / fn
                try:
                    arc = fp.relative_to(src_dir).as_posix()
                    zf.write(fp, arc)
                except OSError:
                    # Игра могла изменить файл после проверки стабильности.
                    continue
    tmp_zip.replace(zip_path)


def copy_tree_stable(src: Path, dst: Path) -> None:
    if dst.exists():
        raise FileExistsError(f"Папка уже существует: {dst}")
    if not wait_until_stable(src):
        raise RuntimeError("Сохранение не стало стабильным: возможно, игра сейчас активно пишет файлы.")
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("*.tmp", "*.temp"))


def candidate_save_roots() -> List[Path]:
    candidates: List[Path] = []

    def add(p: Path) -> None:
        try:
            p = p.expanduser().resolve()
        except OSError:
            return
        if p.exists() and p.is_dir() and p not in candidates:
            candidates.append(p)

    home = Path.home()
    cwd = Path.cwd()
    env = os.environ

    direct = [
        cwd / "save",
        cwd / "userdata" / "save",
        home / ".local" / "share" / "cataclysm-dda" / "save",
        home / ".local" / "share" / "cataclysm-bn" / "save",
        home / "Cataclysm-DDA" / "save",
        home / "Cataclysm-BN" / "save",
        home / "Documents" / "Cataclysm-DDA" / "save",
        home / "Documents" / "Cataclysm-BN" / "save",
    ]
    for p in direct:
        add(p)

    program_roots = []
    for key in ("PROGRAMFILES(X86)", "PROGRAMFILES", "PROGRAMW6432"):
        val = env.get(key)
        if val:
            program_roots.append(Path(val))
    for root in program_roots:
        for rel in (
            Path("Steam/steamapps/common/Cataclysm Dark Days Ahead/save"),
            Path("Steam/steamapps/common/Cataclysm Bright Nights/save"),
            Path("Steam/steamapps/common/Cataclysm DDA/save"),
        ):
            add(root / rel)

    likely_roots = [home / "Games", home / "Desktop", home / "Downloads", home / "Documents"]
    if sys.platform.startswith("win"):
        for drive in ("C:/", "D:/", "E:/"):
            likely_roots.append(Path(drive) / "Games")
            likely_roots.append(Path(drive) / "Cataclysm")
            likely_roots.append(Path(drive) / "CDDA Game Launcher")

    for root in likely_roots:
        if not root.exists() or not root.is_dir():
            continue
        try:
            for p in root.rglob("save"):
                if len(candidates) >= 30:
                    return candidates
                if not p.is_dir():
                    continue
                parts = " ".join(part.lower() for part in p.parts[-5:])
                if any(token in parts for token in ("cataclysm", "cdda", "dda", "launcher", "userdata")):
                    add(p)
        except OSError:
            continue
    return candidates



def candidate_game_executables(save_root: Optional[Path] = None) -> List[Path]:
    """Ищем вероятный исполняемый файл Cataclysm рядом с выбранной папкой save."""
    exe_names = [
        "cataclysm-tiles.exe",
        "cataclysm.exe",
        "Cataclysm.exe",
        "cataclysm-tiles",
        "cataclysm",
    ]
    candidates: List[Path] = []

    def add(path: Path) -> None:
        try:
            path = path.expanduser().resolve()
        except OSError:
            return
        if path.exists() and path.is_file() and path not in candidates:
            candidates.append(path)

    roots: List[Path] = []
    if save_root:
        try:
            sr = save_root.expanduser().resolve()
            for parent in [sr, *sr.parents[:6]]:
                if parent not in roots:
                    roots.append(parent)
        except OSError:
            pass

    for base in [Path.cwd(), Path.home() / "Games", Path.home() / "Downloads", Path.home() / "Desktop", Path.home() / "Documents"]:
        if base not in roots:
            roots.append(base)

    if sys.platform.startswith("win"):
        for drive in ("C:/", "D:/", "E:/"):
            for rel in ("Games", "Cataclysm", "CDDA Game Launcher", "Steam/steamapps/common"):
                base = Path(drive) / rel
                if base not in roots:
                    roots.append(base)

    # Сначала проверяем типичные места без глубокого поиска.
    for root in roots:
        if not root.exists() or not root.is_dir():
            continue
        for name in exe_names:
            add(root / name)
            add(root / "bin" / name)
            add(root / "cataclysm" / name)

    # Потом ограниченный поиск по вероятным корням.
    for root in roots:
        if len(candidates) >= 20:
            break
        if not root.exists() or not root.is_dir():
            continue
        try:
            for name in exe_names:
                for found in root.rglob(name):
                    add(found)
                    if len(candidates) >= 20:
                        break
                if len(candidates) >= 20:
                    break
        except OSError:
            continue
    return candidates

def split_process_names(text: str) -> List[str]:
    result: List[str] = []
    for part in text.replace(";", ",").split(","):
        name = part.strip().strip('"').lower()
        if name and name not in result:
            result.append(name)
    return result


def is_game_process_running(process_names: List[str]) -> Tuple[bool, List[str]]:
    """Возвращает, найден ли процесс игры. Без внешних зависимостей."""
    wanted = {n.lower() for n in process_names if n.strip()}
    if not wanted:
        return False, []
    matched: List[str] = []

    if sys.platform.startswith("win"):
        try:
            proc = subprocess.run(
                ["tasklist", "/fo", "csv", "/nh"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                timeout=10,
            )
            for row in csv.reader(proc.stdout.splitlines()):
                if not row:
                    continue
                image = row[0].strip().lower()
                image_base = Path(image).name.lower()
                if image in wanted or image_base in wanted:
                    matched.append(row[0])
        except Exception:
            return False, []
    else:
        try:
            proc = subprocess.run(
                ["ps", "-eo", "comm,args"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
            )
            for line in proc.stdout.splitlines()[1:]:
                low = line.lower()
                first = low.split(maxsplit=1)[0] if low.split() else ""
                first_base = Path(first).name.lower()
                if first in wanted or first_base in wanted or any(f"/{w}" in low or f" {w}" in low for w in wanted):
                    matched.append(line.strip())
        except Exception:
            return False, []
    return bool(matched), matched


@dataclass
class SaveInfo:
    name: str
    path: Path
    size: int
    mtime: float
    files: int


@dataclass
class BackupInfo:
    name: str
    path: Path
    size: int
    mtime: float
    note: str
    reason: str


class ScrolledFrame(ttk.Frame):
    """Вертикально прокручиваемый контейнер для блоков управления."""

    def __init__(self, parent, height: int = 220):
        super().__init__(parent)
        self.canvas = tk.Canvas(self, highlightthickness=0, height=height)
        self.vbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.inner = ttk.Frame(self.canvas)
        self.window_id = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=self.vbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.vbar.pack(side="right", fill="y")
        self.inner.bind("<Configure>", self._on_inner_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.canvas.bind_all("<MouseWheel>", self._on_mousewheel, add="+")
        self.canvas.bind_all("<Button-4>", self._on_mousewheel, add="+")
        self.canvas.bind_all("<Button-5>", self._on_mousewheel, add="+")

    def _on_inner_configure(self, _event=None) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas_configure(self, event) -> None:
        self.canvas.itemconfigure(self.window_id, width=event.width)

    def _on_mousewheel(self, event) -> None:
        # Прокручиваем только если курсор над этим контейнером.
        try:
            widget = self.winfo_containing(event.x_root, event.y_root)
        except Exception:
            widget = None
        if not widget:
            return
        parent = widget
        inside = False
        while parent is not None:
            if parent == self:
                inside = True
                break
            parent = parent.master
        if not inside:
            return
        if getattr(event, "num", None) == 4:
            self.canvas.yview_scroll(-3, "units")
        elif getattr(event, "num", None) == 5:
            self.canvas.yview_scroll(3, "units")
        else:
            delta = int(-1 * (event.delta / 120)) if event.delta else 0
            self.canvas.yview_scroll(delta * 3, "units")


class CDDAApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title(f"{APP_NAME} {APP_VERSION}")
        self.root.geometry("1280x820")
        self.root.minsize(850, 520)

        self.log_queue: "queue.Queue[str]" = queue.Queue()
        self.auto_thread: Optional[threading.Thread] = None
        self.auto_stop = threading.Event()
        self.last_auto_signature: Dict[str, Tuple[float, int]] = {}

        self.rollback_thread: Optional[threading.Thread] = None
        self.rollback_stop = threading.Event()
        self.rollback_enabled = False
        self.rollback_save_path: Optional[Path] = None
        self.rollback_backup_path: Optional[Path] = None
        self.rollback_seen_running = False

        self.notes: Dict[str, str] = {}
        self.current_save_name: Optional[str] = None

        self.config = self.load_config()
        self.save_root_var = tk.StringVar(value=self.config.get("save_root", ""))
        self.backup_root_var = tk.StringVar(value=self.config.get("backup_root", ""))
        self.game_exe_var = tk.StringVar(value=self.config.get("game_exe", ""))
        self.interval_value = int(self.config.get("auto_interval_sec", 60))
        self.max_backups_value = int(self.config.get("max_backups_per_save", 20))
        self.interval_var = tk.IntVar(value=self.interval_value)
        self.max_backups_var = tk.IntVar(value=self.max_backups_value)
        self.processes_var = tk.StringVar(value=self.config.get("game_processes", DEFAULT_GAME_PROCESSES))
        self.keep_final_before_rollback_var = tk.BooleanVar(value=bool(self.config.get("keep_final_before_rollback", True)))
        self.status_var = tk.StringVar(value="Выбери папку save или нажми Автонайти.")
        self.rollback_status_var = tk.StringVar(value="Откат выключен.")

        self._build_ui()
        self.load_notes()
        self.refresh_all()
        self.root.after(200, self._drain_log_queue)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    # ---------- config/notes ----------
    def load_config(self) -> Dict[str, Any]:
        # 1) новый локальный конфиг рядом с программой;
        # 2) старый конфиг из профиля пользователя — только для миграции.
        for path in (CONFIG_PATH, OLD_CONFIG_PATH):
            try:
                if path.exists():
                    data = json.loads(path.read_text(encoding="utf-8"))
                    if path == OLD_CONFIG_PATH:
                        try:
                            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
                            CONFIG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                        except Exception:
                            pass
                    return data
            except Exception:
                pass
        return {}

    def save_config(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        try:
            self.interval_value = int(self.interval_var.get())
        except Exception:
            self.interval_value = 60
        try:
            self.max_backups_value = int(self.max_backups_var.get())
        except Exception:
            self.max_backups_value = 20
        data = {
            "save_root": self.save_root_var.get(),
            "backup_root": self.backup_root_var.get(),
            "game_exe": self.game_exe_var.get(),
            "auto_interval_sec": self.interval_value,
            "max_backups_per_save": self.max_backups_value,
            "game_processes": self.processes_var.get(),
            "keep_final_before_rollback": bool(self.keep_final_before_rollback_var.get()),
        }
        CONFIG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def notes_path(self) -> Path:
        # Заметки программы теперь всегда рядом с программой.
        return CONFIG_DIR / NOTES_FILENAME

    def load_notes(self) -> None:
        candidates: List[Path] = [self.notes_path()]

        # Старые места хранения — только для чтения/миграции.
        try:
            if self.backup_root_var.get():
                candidates.append(Path(self.backup_root_var.get()).expanduser() / NOTES_FILENAME)
        except Exception:
            pass
        candidates.append(OLD_CONFIG_DIR / NOTES_FILENAME)

        self.notes = {}
        for np in candidates:
            try:
                if np.exists():
                    data = json.loads(np.read_text(encoding="utf-8"))
                    self.notes = {str(k): str(v) for k, v in data.items()}
                    if np != self.notes_path():
                        self.save_notes()
                    return
            except Exception:
                continue

    def save_notes(self) -> None:
        try:
            path = self.notes_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(self.notes, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            self.log(f"Не удалось сохранить заметки: {exc}")

    # ---------- UI ----------
    def _build_ui(self) -> None:
        main = ttk.Frame(self.root, padding=8)
        main.pack(fill="both", expand=True)
        main.rowconfigure(1, weight=1)
        main.columnconfigure(0, weight=1)

        path_frame = ttk.LabelFrame(main, text="Папки")
        path_frame.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        path_frame.columnconfigure(1, weight=1)

        ttk.Label(path_frame, text="CDDA save:").grid(row=0, column=0, sticky="w", padx=6, pady=4)
        ttk.Entry(path_frame, textvariable=self.save_root_var).grid(row=0, column=1, sticky="ew", padx=6, pady=4)
        ttk.Button(path_frame, text="Обзор", command=self.choose_save_root).grid(row=0, column=2, padx=3, pady=4)
        ttk.Button(path_frame, text="Автонайти", command=self.autofind_save_root).grid(row=0, column=3, padx=3, pady=4)

        ttk.Label(path_frame, text="Backups:").grid(row=1, column=0, sticky="w", padx=6, pady=4)
        ttk.Entry(path_frame, textvariable=self.backup_root_var).grid(row=1, column=1, sticky="ew", padx=6, pady=4)
        ttk.Button(path_frame, text="Обзор", command=self.choose_backup_root).grid(row=1, column=2, padx=3, pady=4)
        ttk.Button(path_frame, text="По умолчанию", command=self.default_backup_root).grid(row=1, column=3, padx=3, pady=4)

        ttk.Label(path_frame, text="Игра:").grid(row=2, column=0, sticky="w", padx=6, pady=4)
        ttk.Entry(path_frame, textvariable=self.game_exe_var).grid(row=2, column=1, sticky="ew", padx=6, pady=4)
        ttk.Button(path_frame, text="Обзор", command=self.choose_game_exe).grid(row=2, column=2, padx=3, pady=4)
        ttk.Button(path_frame, text="Автонайти", command=self.autofind_game_exe).grid(row=2, column=3, padx=3, pady=4)

        body = ttk.PanedWindow(main, orient="horizontal")
        body.grid(row=1, column=0, sticky="nsew")

        left = ttk.LabelFrame(body, text="Сохранения / миры")
        right = ttk.LabelFrame(body, text="Бэкапы выбранного сохранения / заметка / лог")
        body.add(left, weight=3)
        body.add(right, weight=4)

        left.rowconfigure(0, weight=1)
        left.columnconfigure(0, weight=1)
        right.rowconfigure(0, weight=1)
        right.columnconfigure(0, weight=1)

        # Saves tree with explicit headers, note column and scrollbars.
        self.saves_tree = ttk.Treeview(
            left,
            columns=("name", "mtime", "size", "files", "note"),
            show="headings",
            height=12,
        )
        for col, title, width, anchor in (
            ("name", "Сохранение", 220, "w"),
            ("mtime", "Изменено", 150, "w"),
            ("size", "Размер", 90, "e"),
            ("files", "Файлов", 70, "e"),
            ("note", "Заметка", 260, "w"),
        ):
            self.saves_tree.heading(col, text=title)
            self.saves_tree.column(col, width=width, minwidth=50, anchor=anchor, stretch=True)
        saves_y = ttk.Scrollbar(left, orient="vertical", command=self.saves_tree.yview)
        saves_x = ttk.Scrollbar(left, orient="horizontal", command=self.saves_tree.xview)
        self.saves_tree.configure(yscrollcommand=saves_y.set, xscrollcommand=saves_x.set)
        self.saves_tree.grid(row=0, column=0, sticky="nsew", padx=(6, 0), pady=(6, 0))
        saves_y.grid(row=0, column=1, sticky="ns", pady=(6, 0))
        saves_x.grid(row=1, column=0, sticky="ew", padx=(6, 0), pady=(0, 8))
        self.saves_tree.bind("<<TreeviewSelect>>", self.on_save_select)

        controls_scroller = ScrolledFrame(left, height=245)
        controls_scroller.grid(row=2, column=0, columnspan=2, sticky="ew", padx=6, pady=(0, 6))
        controls = controls_scroller.inner

        save_btns = ttk.LabelFrame(controls, text="Действия с сохранением")
        save_btns.pack(fill="x", pady=(0, 6))
        for i in range(4):
            save_btns.columnconfigure(i, weight=1)
        ttk.Button(save_btns, text="Обновить", command=self.refresh_all).grid(row=0, column=0, sticky="ew", padx=2, pady=2)
        ttk.Button(save_btns, text="Бэкап активного", command=self.backup_active).grid(row=0, column=1, sticky="ew", padx=2, pady=2)
        ttk.Button(save_btns, text="Бэкап выбранного", command=self.backup_selected).grid(row=0, column=2, sticky="ew", padx=2, pady=2)
        ttk.Button(save_btns, text="Копировать", command=self.copy_selected_save).grid(row=0, column=3, sticky="ew", padx=2, pady=2)
        ttk.Button(save_btns, text="Переименовать", command=self.rename_selected_save).grid(row=1, column=0, sticky="ew", padx=2, pady=2)
        ttk.Button(save_btns, text="Удалить", command=self.delete_selected_save).grid(row=1, column=1, sticky="ew", padx=2, pady=2)
        ttk.Button(save_btns, text="Открыть save", command=self.open_save_folder).grid(row=1, column=2, sticky="ew", padx=2, pady=2)
        ttk.Button(save_btns, text="Открыть backups", command=self.open_backup_folder).grid(row=1, column=3, sticky="ew", padx=2, pady=2)
        ttk.Button(save_btns, text="Запустить игру", command=self.launch_game).grid(row=2, column=0, columnspan=4, sticky="ew", padx=2, pady=2)

        auto_frame = ttk.LabelFrame(controls, text="Автобэкап активного сохранения")
        auto_frame.pack(fill="x", pady=(0, 6))
        for i in range(7):
            auto_frame.columnconfigure(i, weight=0)
        auto_frame.columnconfigure(6, weight=1)
        ttk.Label(auto_frame, text="Интервал, сек:").grid(row=0, column=0, padx=4, pady=4, sticky="w")
        ttk.Spinbox(auto_frame, from_=10, to=3600, textvariable=self.interval_var, width=8).grid(row=0, column=1, padx=4, pady=4, sticky="w")
        ttk.Label(auto_frame, text="Хранить на save:").grid(row=0, column=2, padx=4, pady=4, sticky="w")
        ttk.Spinbox(auto_frame, from_=0, to=9999, textvariable=self.max_backups_var, width=8).grid(row=0, column=3, padx=4, pady=4, sticky="w")
        ttk.Button(auto_frame, text="Старт", width=12, command=self.start_auto_backup).grid(row=0, column=4, padx=4, pady=4, sticky="w")
        ttk.Button(auto_frame, text="Стоп", width=12, command=self.stop_auto_backup).grid(row=0, column=5, padx=4, pady=4, sticky="w")

        rollback_frame = ttk.LabelFrame(controls, text="Тест модов: откат после закрытия игры")
        rollback_frame.pack(fill="x", pady=(0, 6))
        rollback_frame.columnconfigure(0, weight=0)
        rollback_frame.columnconfigure(1, weight=1)
        ttk.Label(rollback_frame, text="Процессы игры:").grid(row=0, column=0, padx=4, pady=4, sticky="w")
        ttk.Entry(rollback_frame, textvariable=self.processes_var).grid(row=0, column=1, sticky="ew", padx=4, pady=4)
        ttk.Checkbutton(
            rollback_frame,
            text="Сохранить финальное состояние перед откатом",
            variable=self.keep_final_before_rollback_var,
        ).grid(row=1, column=0, columnspan=2, sticky="w", padx=4, pady=2)

        rollback_buttons = ttk.Frame(rollback_frame)
        rollback_buttons.grid(row=2, column=0, columnspan=2, sticky="w", padx=2, pady=2)
        ttk.Button(rollback_buttons, text="Включить", width=13, command=self.enable_rollback_selected).pack(side="left", padx=2, pady=2)
        ttk.Button(rollback_buttons, text="Откатить сейчас", width=16, command=self.rollback_now).pack(side="left", padx=2, pady=2)
        ttk.Button(rollback_buttons, text="Выключить", width=13, command=self.disable_rollback).pack(side="left", padx=2, pady=2)
        ttk.Button(rollback_buttons, text="Проверить", width=13, command=self.check_game_process).pack(side="left", padx=2, pady=2)
        ttk.Button(rollback_buttons, text="Запустить игру", width=16, command=self.launch_game).pack(side="left", padx=2, pady=2)
        ttk.Label(rollback_frame, textvariable=self.rollback_status_var, wraplength=620).grid(row=3, column=0, columnspan=2, sticky="ew", padx=4, pady=4)

        # Right side: vertical panes. Backups, note and log are all on the right as requested.
        right_panes = ttk.PanedWindow(right, orient="vertical")
        right_panes.grid(row=0, column=0, sticky="nsew", padx=6, pady=6)

        backups_frame = ttk.Frame(right_panes)
        note_frame = ttk.LabelFrame(right_panes, text="Заметка к выбранному сохранению")
        log_frame = ttk.LabelFrame(right_panes, text="Лог")
        right_panes.add(backups_frame, weight=5)
        right_panes.add(note_frame, weight=2)
        right_panes.add(log_frame, weight=2)

        backups_frame.rowconfigure(0, weight=1)
        backups_frame.columnconfigure(0, weight=1)
        self.backups_tree = ttk.Treeview(
            backups_frame,
            columns=("name", "mtime", "size", "reason", "note"),
            show="headings",
            height=10,
        )
        for col, title, width, anchor in (
            ("name", "Файл", 280, "w"),
            ("mtime", "Создан", 150, "w"),
            ("size", "Размер", 90, "e"),
            ("reason", "Причина", 130, "w"),
            ("note", "Заметка", 280, "w"),
        ):
            self.backups_tree.heading(col, text=title)
            self.backups_tree.column(col, width=width, minwidth=50, anchor=anchor, stretch=True)
        backups_y = ttk.Scrollbar(backups_frame, orient="vertical", command=self.backups_tree.yview)
        backups_x = ttk.Scrollbar(backups_frame, orient="horizontal", command=self.backups_tree.xview)
        self.backups_tree.configure(yscrollcommand=backups_y.set, xscrollcommand=backups_x.set)
        self.backups_tree.grid(row=0, column=0, sticky="nsew")
        backups_y.grid(row=0, column=1, sticky="ns")
        backups_x.grid(row=1, column=0, sticky="ew")
        self.backups_tree.bind("<<TreeviewSelect>>", self.on_backup_select)

        backup_btns = ttk.Frame(backups_frame)
        backup_btns.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        for i in range(3):
            backup_btns.columnconfigure(i, weight=1)
        ttk.Button(backup_btns, text="Восстановить", command=self.restore_selected_backup).grid(row=0, column=0, sticky="ew", padx=2, pady=2)
        ttk.Button(backup_btns, text="Удалить backup", command=self.delete_selected_backup).grid(row=0, column=1, sticky="ew", padx=2, pady=2)
        ttk.Button(backup_btns, text="Открыть backup", command=self.open_selected_backup).grid(row=0, column=2, sticky="ew", padx=2, pady=2)

        note_frame.rowconfigure(0, weight=1)
        note_frame.columnconfigure(0, weight=1)
        self.note_text = tk.Text(note_frame, height=5, wrap="word", undo=True)
        note_y = ttk.Scrollbar(note_frame, orient="vertical", command=self.note_text.yview)
        self.note_text.configure(yscrollcommand=note_y.set)
        self.note_text.grid(row=0, column=0, sticky="nsew", padx=(4, 0), pady=4)
        note_y.grid(row=0, column=1, sticky="ns", pady=4)
        ttk.Button(note_frame, text="Сохранить заметку", command=self.save_current_note).grid(row=1, column=0, columnspan=2, sticky="e", padx=4, pady=(0, 4))

        log_frame.rowconfigure(0, weight=1)
        log_frame.columnconfigure(0, weight=1)
        self.log_text = tk.Text(log_frame, height=7, wrap="word", state="normal")
        log_y = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_y.set)
        self.log_text.grid(row=0, column=0, sticky="nsew", padx=(4, 0), pady=4)
        log_y.grid(row=0, column=1, sticky="ns", pady=4)

        status = ttk.Label(main, textvariable=self.status_var, anchor="w")
        status.grid(row=2, column=0, sticky="ew", pady=(4, 0))

        self.log(f"Готово. Версия {APP_VERSION}. Данные программы: {CONFIG_DIR}")
        self.log("Перед восстановлением, переименованием или ручным откатом лучше закрыть игру.")

    # ---------- helpers ----------
    def log(self, text: str) -> None:
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {text}"
        try:
            self.log_queue.put_nowait(line)
        except Exception:
            pass

    def _drain_log_queue(self) -> None:
        while True:
            try:
                line = self.log_queue.get_nowait()
            except queue.Empty:
                break
            self.log_text.insert("end", line + "\n")
            self.log_text.see("end")
            self.status_var.set(line)
        self.root.after(200, self._drain_log_queue)

    def set_rollback_status(self, text: str, add_log: bool = True) -> None:
        if add_log:
            self.log(f"Откат: {text}")
        try:
            self.root.after(0, self.rollback_status_var.set, text)
        except Exception:
            pass

    def sync_process_name_from_game_exe(self, exe: Optional[Path]) -> None:
        if not exe:
            return
        name = exe.name.strip()
        if not name:
            return
        existing = split_process_names(self.processes_var.get())
        if name.lower() not in existing:
            current = self.processes_var.get().strip()
            self.processes_var.set((current + ", " if current else "") + name)

    def save_root(self) -> Optional[Path]:
        value = self.save_root_var.get().strip()
        if not value:
            return None
        return Path(value).expanduser()

    def backup_root(self) -> Optional[Path]:
        value = self.backup_root_var.get().strip()
        if not value:
            sr = self.save_root()
            if sr:
                p = sr.parent / DEFAULT_BACKUP_DIRNAME
                self.backup_root_var.set(str(p))
                return p
            return None
        return Path(value).expanduser()

    def selected_save(self) -> Optional[Path]:
        sel = self.saves_tree.selection()
        if not sel:
            return None
        sr = self.save_root()
        if sr is None:
            return None
        return sr / sel[0]

    def selected_backup(self) -> Optional[Path]:
        sel = self.backups_tree.selection()
        if not sel:
            return None
        return Path(sel[0])

    def list_saves(self) -> List[SaveInfo]:
        sr = self.save_root()
        if sr is None or not sr.exists():
            return []
        result: List[SaveInfo] = []
        br = self.backup_root()
        br_resolved = None
        try:
            br_resolved = br.resolve() if br else None
        except OSError:
            pass
        for p in sr.iterdir():
            if not p.is_dir():
                continue
            if p.name.lower() in IGNORED_SAVE_DIRS:
                continue
            try:
                if br_resolved and p.resolve() == br_resolved:
                    continue
            except OSError:
                pass
            try:
                size, mt, files = get_dir_size_and_mtime(p)
            except OSError:
                continue
            result.append(SaveInfo(p.name, p, size, mt, files))
        result.sort(key=lambda x: x.mtime, reverse=True)
        return result

    def active_save(self) -> Optional[SaveInfo]:
        saves = self.list_saves()
        return saves[0] if saves else None

    def list_backups_for(self, save_name: str) -> List[BackupInfo]:
        br = self.backup_root()
        if br is None:
            return []
        folder = br / save_name
        if not folder.exists():
            return []
        result: List[BackupInfo] = []
        for p in folder.glob("*.zip"):
            try:
                st = p.stat()
            except OSError:
                continue
            note = ""
            reason = ""
            try:
                with zipfile.ZipFile(p, "r") as zf:
                    if "__cdda_backup_meta.json" in zf.namelist():
                        meta = json.loads(zf.read("__cdda_backup_meta.json").decode("utf-8"))
                        note = str(meta.get("note", ""))
                        reason = str(meta.get("reason", ""))
            except Exception:
                pass
            result.append(BackupInfo(p.name, p, st.st_size, st.st_mtime, note, reason))
        result.sort(key=lambda x: x.mtime, reverse=True)
        return result

    def run_worker(self, target, *args, refresh: bool = True) -> None:
        def wrapper() -> None:
            try:
                target(*args)
            except Exception as exc:
                self.log(f"Ошибка: {exc}")
            finally:
                if refresh:
                    self.root.after(0, self.refresh_all)
        threading.Thread(target=wrapper, daemon=True).start()

    # ---------- folder/actions ----------
    def choose_save_root(self) -> None:
        initial = self.save_root_var.get() or str(Path.home())
        d = filedialog.askdirectory(title="Выбери папку save Cataclysm", initialdir=initial)
        if d:
            self.save_root_var.set(d)
            if not self.backup_root_var.get().strip():
                self.default_backup_root()
            self.save_config()
            self.load_notes()
            self.refresh_all()

    def choose_backup_root(self) -> None:
        initial = self.backup_root_var.get() or str(Path.home())
        d = filedialog.askdirectory(title="Выбери папку для backups", initialdir=initial)
        if d:
            self.backup_root_var.set(d)
            self.save_config()
            self.load_notes()
            self.refresh_all()

    def default_backup_root(self) -> None:
        sr = self.save_root()
        if sr is None:
            messagebox.showwarning(APP_NAME, "Сначала выбери папку save.")
            return
        self.backup_root_var.set(str(sr.parent / DEFAULT_BACKUP_DIRNAME))
        self.save_config()
        self.load_notes()

    def choose_game_exe(self) -> None:
        initial = self.game_exe_var.get().strip()
        initialdir = str(Path(initial).parent) if initial else str(Path.home())
        filetypes = [
            ("Cataclysm executable", "cataclysm*"),
            ("Executable", "*.exe"),
            ("All files", "*"),
        ] if sys.platform.startswith("win") else [
            ("Cataclysm executable", "cataclysm*"),
            ("All files", "*"),
        ]
        f = filedialog.askopenfilename(title="Выбери исполняемый файл Cataclysm", initialdir=initialdir, filetypes=filetypes)
        if f:
            self.game_exe_var.set(f)
            self.sync_process_name_from_game_exe(Path(f))
            self.save_config()
            self.log(f"Выбран запуск игры: {f}")

    def autofind_game_exe(self) -> None:
        self.log("Ищу исполняемый файл Cataclysm...")
        candidates = candidate_game_executables(self.save_root())
        if not candidates:
            messagebox.showinfo(APP_NAME, "Не нашёл cataclysm/cataclysm-tiles рядом с save. Укажи файл игры вручную.")
            return
        if len(candidates) == 1:
            chosen = candidates[0]
        else:
            chooser = tk.Toplevel(self.root)
            chooser.title("Найденные файлы игры")
            chooser.geometry("850x390")
            ttk.Label(chooser, text="Выбери файл запуска Cataclysm:").pack(anchor="w", padx=8, pady=6)
            frame = ttk.Frame(chooser)
            frame.pack(fill="both", expand=True, padx=8, pady=6)
            lb = tk.Listbox(frame)
            yb = ttk.Scrollbar(frame, orient="vertical", command=lb.yview)
            xb = ttk.Scrollbar(frame, orient="horizontal", command=lb.xview)
            lb.configure(yscrollcommand=yb.set, xscrollcommand=xb.set)
            lb.grid(row=0, column=0, sticky="nsew")
            yb.grid(row=0, column=1, sticky="ns")
            xb.grid(row=1, column=0, sticky="ew")
            frame.rowconfigure(0, weight=1)
            frame.columnconfigure(0, weight=1)
            for c in candidates:
                lb.insert("end", str(c))
            chosen_path: Dict[str, Optional[Path]] = {"value": None}

            def ok() -> None:
                sel = lb.curselection()
                if sel:
                    chosen_path["value"] = candidates[sel[0]]
                    chooser.destroy()

            ttk.Button(chooser, text="Выбрать", command=ok).pack(anchor="e", padx=8, pady=8)
            lb.selection_set(0)
            chooser.grab_set()
            self.root.wait_window(chooser)
            if not chosen_path["value"]:
                return
            chosen = chosen_path["value"]
        self.game_exe_var.set(str(chosen))
        self.sync_process_name_from_game_exe(chosen)
        self.save_config()
        self.log(f"Найден файл запуска игры: {chosen}")

    def launch_game(self) -> None:
        value = self.game_exe_var.get().strip().strip('"')
        exe: Optional[Path] = Path(value).expanduser() if value else None
        if not exe or not exe.exists() or not exe.is_file():
            candidates = candidate_game_executables(self.save_root())
            if candidates:
                exe = candidates[0]
                self.game_exe_var.set(str(exe))
                self.sync_process_name_from_game_exe(exe)
                self.save_config()
                self.log(f"Автоматически выбран файл запуска: {exe}")
            else:
                messagebox.showwarning(APP_NAME, "Файл игры не выбран или не найден. Выбери cataclysm-tiles.exe / cataclysm-tiles.")
                self.choose_game_exe()
                value = self.game_exe_var.get().strip().strip('"')
                exe = Path(value).expanduser() if value else None
                if not exe or not exe.exists() or not exe.is_file():
                    return
        try:
            self.sync_process_name_from_game_exe(exe)
            self.save_config()
            kwargs: Dict[str, Any] = {"cwd": str(exe.parent)}
            if sys.platform.startswith("win"):
                kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            subprocess.Popen([str(exe)], **kwargs)
            self.log(f"Игра запущена: {exe}")
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Не удалось запустить игру: {exc}")
            self.log(f"Не удалось запустить игру: {exc}")

    def autofind_save_root(self) -> None:
        self.log("Ищу вероятные папки save...")
        candidates = candidate_save_roots()
        if not candidates:
            messagebox.showinfo(APP_NAME, "Не нашёл автоматически. Укажи папку save вручную.")
            return
        if len(candidates) == 1:
            chosen = candidates[0]
        else:
            chooser = tk.Toplevel(self.root)
            chooser.title("Найденные папки save")
            chooser.geometry("850x390")
            ttk.Label(chooser, text="Выбери нужную папку save:").pack(anchor="w", padx=8, pady=6)
            frame = ttk.Frame(chooser)
            frame.pack(fill="both", expand=True, padx=8, pady=6)
            lb = tk.Listbox(frame)
            yb = ttk.Scrollbar(frame, orient="vertical", command=lb.yview)
            xb = ttk.Scrollbar(frame, orient="horizontal", command=lb.xview)
            lb.configure(yscrollcommand=yb.set, xscrollcommand=xb.set)
            lb.grid(row=0, column=0, sticky="nsew")
            yb.grid(row=0, column=1, sticky="ns")
            xb.grid(row=1, column=0, sticky="ew")
            frame.rowconfigure(0, weight=1)
            frame.columnconfigure(0, weight=1)
            for c in candidates:
                lb.insert("end", str(c))
            chosen_path: Dict[str, Optional[Path]] = {"value": None}

            def ok() -> None:
                sel = lb.curselection()
                if sel:
                    chosen_path["value"] = candidates[sel[0]]
                    chooser.destroy()

            ttk.Button(chooser, text="Выбрать", command=ok).pack(anchor="e", padx=8, pady=8)
            lb.selection_set(0)
            chooser.grab_set()
            self.root.wait_window(chooser)
            if not chosen_path["value"]:
                return
            chosen = chosen_path["value"]
        self.save_root_var.set(str(chosen))
        self.default_backup_root()
        self.save_config()
        self.load_notes()
        self.refresh_all()
        self.log(f"Выбрана папка save: {chosen}")

    def refresh_all(self) -> None:
        self.save_config()
        # Важно сохранить заметку перед перерисовкой, иначе правка может потеряться.
        if self.current_save_name and hasattr(self, "note_text"):
            self.notes[self.current_save_name] = self.note_text.get("1.0", "end").rstrip("\n")
            self.save_notes()

        selected = self.current_save_name
        for tree in (self.saves_tree, self.backups_tree):
            for item in tree.get_children():
                tree.delete(item)
        saves = self.list_saves()
        for s in saves:
            note = self.notes.get(s.name, "").replace("\n", " ")[:220]
            self.saves_tree.insert("", "end", iid=s.name, values=(
                s.name,
                datetime.fromtimestamp(s.mtime).strftime("%Y-%m-%d %H:%M:%S"),
                human_size(s.size),
                s.files,
                note,
            ))
        save_names = [s.name for s in saves]
        if saves:
            active = saves[0]
            self.status_var.set(f"Активное/последнее изменённое сохранение: {active.name}")
        else:
            sr = self.save_root()
            if sr and sr.exists():
                self.status_var.set("В папке save не найдено папок миров.")
            else:
                self.status_var.set("Папка save не выбрана или не существует.")
        if selected and selected in save_names:
            try:
                self.saves_tree.selection_set(selected)
                self.saves_tree.see(selected)
                self.current_save_name = selected
                self.refresh_backups(selected)
            except tk.TclError:
                pass

    def refresh_backups(self, save_name: str) -> None:
        for item in self.backups_tree.get_children():
            self.backups_tree.delete(item)
        for b in self.list_backups_for(save_name):
            self.backups_tree.insert("", "end", iid=str(b.path), values=(
                b.name,
                datetime.fromtimestamp(b.mtime).strftime("%Y-%m-%d %H:%M:%S"),
                human_size(b.size),
                b.reason,
                b.note.replace("\n", " ")[:200],
            ))

    def on_save_select(self, _event=None) -> None:
        sel = self.saves_tree.selection()
        if not sel:
            return
        if self.current_save_name and self.current_save_name != sel[0]:
            self.notes[self.current_save_name] = self.note_text.get("1.0", "end").rstrip("\n")
            self.save_notes()
        self.current_save_name = sel[0]
        self.note_text.delete("1.0", "end")
        self.note_text.insert("1.0", self.notes.get(self.current_save_name, ""))
        self.refresh_backups(self.current_save_name)

    def on_backup_select(self, _event=None) -> None:
        pass

    def save_current_note(self) -> None:
        if not self.current_save_name:
            messagebox.showwarning(APP_NAME, "Сначала выбери сохранение.")
            return
        self.notes[self.current_save_name] = self.note_text.get("1.0", "end").rstrip("\n")
        self.save_notes()
        self.refresh_all()
        self.log(f"Заметка сохранена: {self.current_save_name}")

    def save_current_note_silent(self) -> None:
        if self.current_save_name:
            self.notes[self.current_save_name] = self.note_text.get("1.0", "end").rstrip("\n")
            self.save_notes()

    def make_backup(self, save_path: Path, reason: str = "manual") -> Path:
        br = self.backup_root()
        if br is None:
            raise RuntimeError("Папка backups не выбрана.")
        if not save_path.exists() or not save_path.is_dir():
            raise RuntimeError("Сохранение не найдено.")
        note = self.notes.get(save_path.name, "")
        self.log(f"Жду стабильности файлов: {save_path.name}")
        if not wait_until_stable(save_path):
            raise RuntimeError("Сохранение не стало стабильным. Закрой игру или повтори позже.")
        backup_dir = br / save_path.name
        suffix = ""
        if reason == "rollback_baseline":
            suffix = "__ROLLBACK_BASELINE"
        elif reason == "final_before_rollback":
            suffix = "__before_auto_rollback"
        zip_path = backup_dir / f"{safe_name(save_path.name)}__{now_stamp()}{suffix}.zip"
        size, mt, files = get_dir_size_and_mtime(save_path)
        meta = {
            "app": APP_NAME,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "save_name": save_path.name,
            "save_path": str(save_path),
            "reason": reason,
            "note": note,
            "source_size_bytes": size,
            "source_latest_mtime": mt,
            "source_files": files,
        }
        self.log(f"Создаю backup: {zip_path.name}")
        zip_directory(save_path, zip_path, meta)
        if reason not in {"rollback_baseline", "final_before_rollback"}:
            self.prune_backups(save_path.name)
        self.log(f"Backup готов: {zip_path}")
        return zip_path

    def prune_backups(self, save_name: str) -> None:
        try:
            max_n = int(self.max_backups_value)
        except Exception:
            max_n = 20
        if max_n <= 0:
            return
        backups = [b for b in self.list_backups_for(save_name) if b.reason not in {"rollback_baseline"}]
        for b in backups[max_n:]:
            try:
                b.path.unlink()
                self.log(f"Удалён старый backup по лимиту: {b.name}")
            except OSError as exc:
                self.log(f"Не удалось удалить старый backup {b.name}: {exc}")

    def backup_active(self) -> None:
        active = self.active_save()
        if not active:
            messagebox.showwarning(APP_NAME, "Не найдено активное/последнее изменённое сохранение.")
            return
        self.save_current_note_silent()
        self.save_config()
        self.run_worker(self.make_backup, active.path, "manual_active")

    def backup_selected(self) -> None:
        sp = self.selected_save()
        if not sp:
            messagebox.showwarning(APP_NAME, "Сначала выбери сохранение.")
            return
        self.save_current_note_silent()
        self.save_config()
        self.run_worker(self.make_backup, sp, "manual_selected")

    def copy_selected_save(self) -> None:
        sp = self.selected_save()
        if not sp:
            messagebox.showwarning(APP_NAME, "Сначала выбери сохранение.")
            return
        new_name = simpledialog.askstring(APP_NAME, "Имя копии:", initialvalue=f"{sp.name}_copy")
        if not new_name:
            return
        dst = sp.parent / safe_name(new_name)
        if dst.exists():
            messagebox.showerror(APP_NAME, "Папка с таким именем уже существует.")
            return
        self.run_worker(self._copy_save_worker, sp, dst)

    def _copy_save_worker(self, src: Path, dst: Path) -> None:
        self.log(f"Копирую сохранение {src.name} -> {dst.name}")
        copy_tree_stable(src, dst)
        if src.name in self.notes:
            self.notes[dst.name] = self.notes[src.name]
            self.save_notes()
        self.log(f"Копия готова: {dst}")

    def rename_selected_save(self) -> None:
        sp = self.selected_save()
        if not sp:
            messagebox.showwarning(APP_NAME, "Сначала выбери сохранение.")
            return
        new_name = simpledialog.askstring(APP_NAME, "Новое имя папки сохранения:", initialvalue=sp.name)
        if not new_name or new_name == sp.name:
            return
        dst = sp.parent / safe_name(new_name)
        if dst.exists():
            messagebox.showerror(APP_NAME, "Папка с таким именем уже существует.")
            return
        if not messagebox.askyesno(APP_NAME, "Переименование лучше делать при закрытой игре. Продолжить?"):
            return
        self.run_worker(self._rename_save_worker, sp, dst)

    def _rename_save_worker(self, src: Path, dst: Path) -> None:
        if not wait_until_stable(src):
            raise RuntimeError("Сохранение не стало стабильным. Закрой игру и повтори.")
        self.log(f"Переименовываю {src.name} -> {dst.name}")
        src.rename(dst)
        if src.name in self.notes:
            self.notes[dst.name] = self.notes.pop(src.name)
            self.save_notes()
        self.current_save_name = dst.name
        self.log("Переименование готово.")

    def delete_selected_save(self) -> None:
        sp = self.selected_save()
        if not sp:
            messagebox.showwarning(APP_NAME, "Сначала выбери сохранение.")
            return
        if not messagebox.askyesno(APP_NAME, f"Удалить сохранение '{sp.name}'? Оно будет перемещено в {DELETED_DIRNAME}, не стёрто окончательно."):
            return
        self.run_worker(self._delete_save_worker, sp)

    def _delete_save_worker(self, sp: Path) -> None:
        if not wait_until_stable(sp):
            raise RuntimeError("Сохранение не стало стабильным. Закрой игру и повтори.")
        trash = sp.parent / DELETED_DIRNAME
        trash.mkdir(exist_ok=True)
        dst = trash / f"{sp.name}__deleted_{now_stamp()}"
        self.log(f"Перемещаю в корзину программы: {dst}")
        shutil.move(str(sp), str(dst))
        self.log("Удаление готово. Папку можно восстановить вручную из _deleted_saves_by_backuper.")

    # ---------- restore ----------
    def restore_selected_backup(self) -> None:
        bp = self.selected_backup()
        sel_save = self.selected_save()
        if not bp:
            messagebox.showwarning(APP_NAME, "Сначала выбери backup.")
            return
        if not sel_save:
            messagebox.showwarning(APP_NAME, "Сначала выбери сохранение, в которое восстанавливать backup.")
            return
        if not messagebox.askyesno(APP_NAME, "Восстановление заменит текущую папку сохранения. Перед этим будет создан аварийный backup текущей версии. Продолжить?"):
            return
        self.run_worker(self._restore_worker, bp, sel_save, True)

    def _restore_worker(self, bp: Path, save_path: Path, make_pre_backup: bool = True) -> None:
        self._restore_backup_to_save(bp, save_path, make_pre_backup=make_pre_backup)

    def _restore_backup_to_save(self, bp: Path, save_path: Path, make_pre_backup: bool = True) -> None:
        if not bp.exists():
            raise RuntimeError(f"Backup не найден: {bp}")
        if make_pre_backup and save_path.exists():
            self.log(f"Перед восстановлением создаю аварийный backup текущего save: {save_path.name}")
            self.make_backup(save_path, "before_restore")
        trash = save_path.parent / DELETED_DIRNAME
        trash.mkdir(exist_ok=True)
        moved_current = None
        if save_path.exists():
            moved_current = trash / f"{save_path.name}__before_restore_{now_stamp()}"
            shutil.move(str(save_path), str(moved_current))
        tmp_dir = Path(tempfile.mkdtemp(prefix="cdda_restore_"))
        try:
            with zipfile.ZipFile(bp, "r") as zf:
                for member in zf.namelist():
                    if member == "__cdda_backup_meta.json":
                        continue
                    member_path = Path(member)
                    if member_path.is_absolute() or ".." in member_path.parts:
                        raise RuntimeError(f"Небезопасный путь в архиве: {member}")
                    zf.extract(member, tmp_dir)
            save_path.mkdir(parents=True, exist_ok=False)
            entries = [p for p in tmp_dir.iterdir() if p.name != "__MACOSX"]
            if len(entries) == 1 and entries[0].is_dir() and entries[0].name == save_path.name:
                src_root = entries[0]
            else:
                src_root = tmp_dir
            for item in src_root.iterdir():
                shutil.move(str(item), str(save_path / item.name))
            self.log(f"Восстановлено из backup: {bp.name}")
        except Exception:
            if save_path.exists():
                shutil.rmtree(save_path, ignore_errors=True)
            if moved_current and moved_current.exists():
                shutil.move(str(moved_current), str(save_path))
            raise
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def delete_selected_backup(self) -> None:
        bp = self.selected_backup()
        if not bp:
            messagebox.showwarning(APP_NAME, "Сначала выбери backup.")
            return
        if not messagebox.askyesno(APP_NAME, f"Удалить backup '{bp.name}'?"):
            return
        try:
            bp.unlink()
            self.log(f"Backup удалён: {bp.name}")
            if self.current_save_name:
                self.refresh_backups(self.current_save_name)
        except OSError as exc:
            messagebox.showerror(APP_NAME, str(exc))

    # ---------- rollback mode ----------
    def enable_rollback_selected(self) -> None:
        sp = self.selected_save()
        if not sp:
            messagebox.showwarning(APP_NAME, "Сначала выбери сохранение.")
            return
        if self.rollback_enabled:
            if not messagebox.askyesno(APP_NAME, "Режим отката уже включён. Перезаписать исходное состояние выбранным сохранением?"):
                return
            self.disable_rollback(show_log=False)
        self.save_current_note_silent()
        self.save_config()
        process_names = split_process_names(self.processes_var.get())
        keep_final = bool(self.keep_final_before_rollback_var.get())
        self.run_worker(self._enable_rollback_worker, sp, process_names, keep_final, refresh=True)

    def _enable_rollback_worker(self, sp: Path, process_names: List[str], keep_final: bool) -> None:
        self.set_rollback_status(f"Создаю исходный backup для {sp.name}...")
        baseline = self.make_backup(sp, "rollback_baseline")
        self.rollback_stop.clear()
        self.rollback_enabled = True
        self.rollback_save_path = sp
        self.rollback_backup_path = baseline
        self.rollback_seen_running = False
        self.set_rollback_status(f"Включено для '{sp.name}'. Жду запуск/закрытие игры. Исходник: {baseline.name}")
        self.rollback_thread = threading.Thread(
            target=self._rollback_watch_loop,
            args=(sp, baseline, process_names, keep_final),
            daemon=True,
        )
        self.rollback_thread.start()

    def _rollback_watch_loop(self, save_path: Path, baseline: Path, process_names: List[str], keep_final: bool) -> None:
        while not self.rollback_stop.is_set():
            running, matches = is_game_process_running(process_names)
            if running:
                if not self.rollback_seen_running:
                    self.rollback_seen_running = True
                    self.set_rollback_status("Игра найдена. Жду закрытия процесса.")
                self.rollback_stop.wait(3)
                continue

            if self.rollback_seen_running:
                self.set_rollback_status("Игра закрыта. Жду стабильности save перед откатом...")
                time.sleep(2)
                try:
                    if save_path.exists() and not wait_until_stable(save_path):
                        raise RuntimeError("Сохранение не стало стабильным после закрытия игры.")
                    if save_path.exists() and keep_final:
                        self.make_backup(save_path, "final_before_rollback")
                    self._restore_backup_to_save(baseline, save_path, make_pre_backup=False)
                    self.set_rollback_status("Откат выполнен. Режим остаётся включённым; жду следующий запуск игры.")
                    self.rollback_seen_running = False
                    self.root.after(0, self.refresh_all)
                except Exception as exc:
                    self.set_rollback_status(f"Ошибка отката: {exc}")
                    self.rollback_seen_running = False
                self.rollback_stop.wait(5)
                continue

            self.rollback_stop.wait(3)

    def rollback_now(self) -> None:
        if not self.rollback_enabled or not self.rollback_backup_path or not self.rollback_save_path:
            messagebox.showwarning(APP_NAME, "Сначала включи режим отката для выбранного сохранения.")
            return
        if not messagebox.askyesno(APP_NAME, "Сейчас заменить выбранное сохранение исходным состоянием режима отката?"):
            return
        keep_final = bool(self.keep_final_before_rollback_var.get())
        self.run_worker(self._rollback_now_worker, self.rollback_save_path, self.rollback_backup_path, keep_final)

    def _rollback_now_worker(self, save_path: Path, baseline: Path, keep_final: bool) -> None:
        self.set_rollback_status("Ручной откат запущен...")
        if save_path.exists() and not wait_until_stable(save_path):
            raise RuntimeError("Сохранение не стало стабильным. Закрой игру и повтори.")
        if save_path.exists() and keep_final:
            self.make_backup(save_path, "final_before_rollback")
        self._restore_backup_to_save(baseline, save_path, make_pre_backup=False)
        self.set_rollback_status("Ручной откат выполнен. Режим остаётся включённым.")

    def disable_rollback(self, show_log: bool = True) -> None:
        self.rollback_stop.set()
        self.rollback_enabled = False
        self.rollback_save_path = None
        self.rollback_backup_path = None
        self.rollback_seen_running = False
        self.set_rollback_status("Откат выключен.", add_log=show_log)

    def check_game_process(self) -> None:
        process_names = split_process_names(self.processes_var.get())
        running, matches = is_game_process_running(process_names)
        if running:
            msg = "Процесс найден: " + ", ".join(matches[:5])
            self.log(msg)
            messagebox.showinfo(APP_NAME, msg)
        else:
            msg = "Процесс игры не найден. Проверь имя .exe/процесса в Диспетчере задач и впиши его в поле процессов."
            self.log(msg)
            messagebox.showwarning(APP_NAME, msg)

    # ---------- auto backup ----------
    def start_auto_backup(self) -> None:
        if self.auto_thread and self.auto_thread.is_alive():
            self.log("Автобэкап уже запущен.")
            return
        self.save_current_note_silent()
        self.save_config()
        self.auto_stop.clear()
        self.last_auto_signature.clear()
        self.auto_thread = threading.Thread(target=self._auto_loop, daemon=True)
        self.auto_thread.start()
        self.log("Автобэкап запущен. Активный save — самая недавно изменённая папка мира.")

    def stop_auto_backup(self) -> None:
        self.auto_stop.set()
        self.log("Автобэкап остановлен.")

    def _auto_loop(self) -> None:
        while not self.auto_stop.is_set():
            try:
                active = self.active_save()
                if active:
                    size, mt, _files = get_dir_size_and_mtime(active.path)
                    sig = (mt, size)
                    last = self.last_auto_signature.get(active.name)
                    if last is None:
                        self.last_auto_signature[active.name] = sig
                    elif sig != last:
                        self.log(f"Обнаружены изменения активного save: {active.name}")
                        self.make_backup(active.path, "auto_active_changed")
                        size2, mt2, _ = get_dir_size_and_mtime(active.path)
                        self.last_auto_signature[active.name] = (mt2, size2)
            except Exception as exc:
                self.log(f"Автобэкап: {exc}")
            interval = max(10, int(self.interval_value))
            self.auto_stop.wait(interval)

    # ---------- open folders ----------
    def open_path(self, p: Path) -> None:
        try:
            if sys.platform.startswith("win"):
                os.startfile(str(p))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(p)])
            else:
                subprocess.Popen(["xdg-open", str(p)])
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Не удалось открыть папку: {exc}")

    def open_save_folder(self) -> None:
        sp = self.selected_save() or self.save_root()
        if sp and sp.exists():
            self.open_path(sp)

    def open_backup_folder(self) -> None:
        br = self.backup_root()
        if br:
            br.mkdir(parents=True, exist_ok=True)
            self.open_path(br)

    def open_selected_backup(self) -> None:
        bp = self.selected_backup()
        if bp:
            self.open_path(bp.parent)

    def on_close(self) -> None:
        self.auto_stop.set()
        self.rollback_stop.set()
        self.save_current_note_silent()
        self.save_config()
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    try:
        style = ttk.Style(root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        elif "clam" in style.theme_names():
            style.theme_use("clam")
    except Exception:
        pass
    CDDAApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
