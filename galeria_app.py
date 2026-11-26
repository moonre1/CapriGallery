"""Capri: desktop photo organizer with optional, local face grouping."""
from __future__ import annotations

import os
import ctypes
from concurrent.futures import ThreadPoolExecutor
import queue
import re
import shutil
import sqlite3
import sys
import threading
import time
import urllib.request
import weakref
from pathlib import Path


def _enable_windows_dpi_awareness():
    if os.name != "nt":
        return

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    set_context = getattr(user32, "SetProcessDpiAwarenessContext", None)
    if set_context is not None:
        set_context.argtypes = (ctypes.c_void_p,)
        set_context.restype = ctypes.c_bool
        if set_context(ctypes.c_void_p(-4)):
            return
        if ctypes.get_last_error() == 5:
            return

    try:
        shcore = ctypes.WinDLL("shcore", use_last_error=True)
    except OSError:
        set_aware = user32.SetProcessDPIAware
        set_aware.argtypes = ()
        set_aware.restype = ctypes.c_bool
        ctypes.set_last_error(0)
        if not set_aware() and ctypes.get_last_error() != 5:
            raise ctypes.WinError(ctypes.get_last_error())
        return

    set_awareness = shcore.SetProcessDpiAwareness
    set_awareness.argtypes = (ctypes.c_int,)
    set_awareness.restype = ctypes.c_long
    status = set_awareness(2) & 0xFFFFFFFF
    if status not in (0, 0x80070005):
        raise OSError(f"SetProcessDpiAwareness failed: 0x{status:08X}")


_enable_windows_dpi_awareness()

import tkinter as tk
from tkinter import filedialog, font as tkfont, messagebox, simpledialog, ttk

try:
    from PIL import Image, ImageTk, ImageOps, ImageDraw, ImageFont, ImageFilter
except ImportError:
    Image = ImageTk = ImageOps = ImageDraw = ImageFont = ImageFilter = None

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except ImportError:
    pillow_heif = None

APP_NAME = "Capri"
_FILE_IO_LOCK = threading.RLock()
APP_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Galeria"
DB_PATH = APP_DIR / "galeria.db"
MODEL_DIR = APP_DIR / "models"
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".jpe", ".jfif", ".png", ".webp", ".bmp", ".tif", ".tiff", ".gif", ".heic", ".heif", ".avif"}
MODELS = {
    "yunet.onnx": "https://github.com/opencv/opencv_zoo/raw/refs/heads/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx",
    "sface.onnx": "https://github.com/opencv/opencv_zoo/raw/refs/heads/main/models/face_recognition_sface/face_recognition_sface_2021dec.onnx",
}
CAPRI_DARK_BG = "#11141b"
CAPRI_DARK_PANEL = "#1a2029"
CAPRI_TEXT = "#eff0ee"
CAPRI_MUTED = "#a9b0b9"
CAPRI_GREEN = "#759b82"
CAPRI_ACCENT = "#ff3d7f"
CAPRI_DARK_LINE = "#2d3540"
CAPRI_LIGHT_BG = "#f3f1ed"
CAPRI_LIGHT_PANEL = "#fbfaf7"
CAPRI_LIGHT_INK = "#27262d"
CAPRI_LIGHT_MUTED = "#6b6d74"
CAPRI_LIGHT_GREEN = "#56856a"
CAPRI_LIGHT_LINE = "#e1dfdc"
CAPRI_THEME_BG = "#12171f"
CAPRI_THEME_PANEL = "#1c2531"
CAPRI_THEME_TEXT = "#f6f4f7"
CAPRI_THEME_MUTED = "#b4c0cb"
CAPRI_THEME_GREEN = "#32d5a4"
CAPRI_THEME_LINE = "#34414f"
LEGACY_CAPRI_ACCENT = "#b5748b"
BG, PANEL, INK, MUTED, GREEN, LIME, LINE = (
    CAPRI_DARK_BG,
    CAPRI_DARK_PANEL,
    CAPRI_TEXT,
    CAPRI_MUTED,
    CAPRI_GREEN,
    CAPRI_ACCENT,
    CAPRI_DARK_LINE,
)


def _safe_resolve(path) -> str:
    try:
        return str(Path(path).resolve())
    except OSError:
        return str(Path(path))


def _move_file(source, target):
    with _FILE_IO_LOCK:
        return shutil.move(str(source), str(target))


def _safe_is_file(path) -> bool:
    try:
        return Path(path).is_file()
    except OSError:
        return False


def _hard_alpha(image, threshold=128):
    if image.mode != "RGBA":
        image = image.convert("RGBA")
    result = image.copy()
    result.putalpha(image.getchannel("A").point(lambda value: 255 if value >= threshold else 0))
    return result


class Store:
    def __init__(self):
        APP_DIR.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.db = sqlite3.connect(DB_PATH, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute("PRAGMA busy_timeout=30000")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS photos(path TEXT PRIMARY KEY, folder TEXT NOT NULL, added_at TEXT DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE IF NOT EXISTS albums(id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, face_group INTEGER NOT NULL DEFAULT 0, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE IF NOT EXISTS memberships(photo_path TEXT NOT NULL REFERENCES photos(path) ON DELETE CASCADE, album_id INTEGER NOT NULL REFERENCES albums(id) ON DELETE CASCADE, PRIMARY KEY(photo_path, album_id));
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)
        if "cover" not in {r[1] for r in self.db.execute("PRAGMA table_info(albums)")}:
            self.db.execute("ALTER TABLE albums ADD COLUMN cover TEXT")
        self.db.execute("CREATE INDEX IF NOT EXISTS idx_memberships_album ON memberships(album_id)")
        self.db.execute("CREATE INDEX IF NOT EXISTS idx_photos_folder ON photos(folder)")
        self.db.commit()

    def setting(self, key, default=""):
        with self._lock:
            row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return row[0] if row else default

    def set_setting(self, key, value):
        with self._lock:
            self.db.execute("INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
            self.db.commit()

    def set_settings(self, settings):
        with self._lock:
            try:
                self.db.executemany(
                    "INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    settings.items(),
                )
                self.db.commit()
            except sqlite3.Error:
                self.db.rollback()
                raise

    def duplicate_groups(self, paths=None, progress=None):
        """Find visually matching images with pHash, independent of file names."""
        paths = paths or self.photos()
        try:
            import cv2
            import numpy as np
        except ImportError as exc:
            raise RuntimeError("OpenCV não está disponível para comparar as imagens.") from exc

        records = []
        for i, path in enumerate(paths, 1):
            try:
                with _FILE_IO_LOCK:
                    raw = np.fromfile(str(path), dtype=np.uint8)
                    image = cv2.imdecode(raw, cv2.IMREAD_COLOR)
                    if image is None and Image is not None:
                        with Image.open(path) as opened:
                            decoded = ImageOps.exif_transpose(opened).convert("RGB")
                            image = cv2.cvtColor(np.asarray(decoded), cv2.COLOR_RGB2BGR)
                if image is None:
                    continue
                original_area = image.shape[0] * image.shape[1]
                image = cv2.resize(image, (32, 32), interpolation=cv2.INTER_AREA)
                gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY).astype(np.float32)
                low = cv2.dct(gray)[:8, :8]
                values = low.reshape(-1)[1:]
                median = float(np.median(values))
                bits = 0
                for value in values:
                    bits = (bits << 1) | int(value >= median)
                hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
                hist = cv2.calcHist([hsv], [0, 1], None, [8, 8], [0, 180, 0, 256])
                cv2.normalize(hist, hist)
                try:
                    stat = Path(path).stat()
                    quality = (original_area, stat.st_size, -i)
                except OSError:
                    quality = (0, 0, -i)
                records.append((str(path), bits, hist, quality))
            except (OSError, ValueError, cv2.error):
                pass
            if progress and (i % 25 == 0 or i == len(paths)):
                progress(i, len(paths))

        buckets = {}
        parent = list(range(len(records)))

        def root(index):
            while parent[index] != index:
                parent[index] = parent[parent[index]]
                index = parent[index]
            return index

        def join(first, second):
            a, b = root(first), root(second)
            if a != b:
                parent[b] = a

        for index, (_path, bits, hist, _quality) in enumerate(records):
            candidates = set()
            for shift in (0, 16, 32, 48):
                candidates.update(buckets.get((shift, (bits >> shift) & 0xFFFF), ()))
            for other in candidates:
                other_bits, other_hist = records[other][1], records[other][2]
                distance = (bits ^ other_bits).bit_count()
                if distance > 3:
                    continue
                similarity = 1.0 - distance / 63.0
                color_similarity = cv2.compareHist(hist, other_hist, cv2.HISTCMP_CORREL)
                if similarity >= 0.95 and color_similarity >= 0.90:
                    join(index, other)
            for shift in (0, 16, 32, 48):
                buckets.setdefault((shift, (bits >> shift) & 0xFFFF), []).append(index)

        grouped = {}
        for index, record in enumerate(records):
            grouped.setdefault(root(index), []).append(record)
        result = []
        for group in grouped.values():
            if len(group) > 1:
                group.sort(key=lambda record: record[3], reverse=True)
                result.append([record[0] for record in group])
        return result

    def remove_photos(self, paths):
        removed = 0
        with self._lock:
            for path in paths:
                try:
                    if not Path(path).exists():
                        self.db.execute("UPDATE albums SET cover=NULL WHERE cover=?", (path,))
                        self.db.execute("DELETE FROM photos WHERE path=?", (path,))
                        removed += 1
                        continue
                    if os.name != "nt":
                        continue

                    class SHFILEOPSTRUCTW(ctypes.Structure):
                        _fields_ = [
                            ("hwnd", ctypes.c_void_p),
                            ("wFunc", ctypes.c_uint),
                            ("pFrom", ctypes.c_wchar_p),
                            ("pTo", ctypes.c_wchar_p),
                            ("fFlags", ctypes.c_ushort),
                            ("fAnyOperationsAborted", ctypes.c_int),
                            ("hNameMappings", ctypes.c_void_p),
                            ("lpszProgressTitle", ctypes.c_wchar_p),
                        ]

                    operation = SHFILEOPSTRUCTW()
                    operation.wFunc = 3  # FO_DELETE
                    operation.pFrom = str(Path(path)) + "\0\0"
                    operation.fFlags = 0x40 | 0x10 | 0x04  # Recycle Bin, no prompt, quiet
                    with _FILE_IO_LOCK:
                        result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(operation))
                    if result == 0 and not operation.fAnyOperationsAborted:
                        self.db.execute("UPDATE albums SET cover=NULL WHERE cover=?", (path,))
                        self.db.execute("DELETE FROM photos WHERE path=?", (path,))
                        removed += 1
                except (OSError, AttributeError, ctypes.ArgumentError):
                    continue
            self.db.commit()
        return removed

    def move_to_album_folder(self, album_id, name, paths, destination):
        """Move files to the selected destination and rewrite their library paths."""
        if not destination:
            return 0, 0
        safe_name = "".join("_" if c in r'<>:"/\|?*' or ord(c) < 32 else c for c in name).strip(" .") or f"Album {album_id}"
        target_dir = Path(destination) / safe_name
        target_dir.mkdir(parents=True, exist_ok=True)
        moved = skipped = 0
        with self._lock:
            for old in list(paths):
                source = Path(old)
                if not source.exists():
                    continue
                other = self.db.execute(
                    "SELECT 1 FROM memberships WHERE photo_path=? AND album_id<>? LIMIT 1", (old, album_id)
                ).fetchone()
                if other:
                    skipped += 1
                    continue
                target = target_dir / source.name
                n = 2
                while target.exists() and _safe_resolve(target) != _safe_resolve(source):
                    target = target_dir / f"{source.stem} ({n}){source.suffix}"
                    n += 1
                if _safe_resolve(target) == _safe_resolve(source):
                    continue
                try:
                    _move_file(source, target)
                    new = _safe_resolve(target)
                    self.db.execute("INSERT OR IGNORE INTO photos(path,folder) VALUES (?,?)", (new, _safe_resolve(target_dir)))
                    self.db.execute(
                        "INSERT OR IGNORE INTO memberships(photo_path,album_id) SELECT ?,album_id FROM memberships WHERE photo_path=?",
                        (new, old),
                    )
                    self.db.execute("INSERT OR IGNORE INTO memberships(photo_path,album_id) VALUES (?,?)", (new, album_id))
                    self.db.execute("UPDATE albums SET cover=? WHERE cover=?", (new, old))
                    self.db.execute("DELETE FROM photos WHERE path=?", (old,))
                    self.db.commit()
                    moved += 1
                except (OSError, sqlite3.Error):
                    self.db.rollback()
                    skipped += 1
        return moved, skipped

    def folders(self):
        with self._lock:
            return [r[0] for r in self.db.execute("SELECT DISTINCT folder FROM photos ORDER BY folder COLLATE NOCASE")]

    def folder_count(self):
        with self._lock:
            return int(self.db.execute("SELECT COUNT(DISTINCT folder) FROM photos").fetchone()[0])

    def add_folder(self, folder):
        folder = _safe_resolve(folder)
        candidates = self.scan_folder_paths(folder)
        return self.import_paths(candidates)

    def add_photos(self, paths):
        added = 0
        with self._lock:
            for value in paths:
                p = Path(value)
                if p.suffix.lower() in IMAGE_EXTENSIONS and _safe_is_file(p):
                    folder = _safe_resolve(p.parent)
                    self.db.execute("INSERT OR IGNORE INTO photos(path, folder) VALUES (?, ?)", (_safe_resolve(p), folder))
                    added += self.db.execute("SELECT changes()").fetchone()[0]
            self.db.commit()
        return added

    @staticmethod
    def scan_folder_paths(folder, excluded_root=""):
        """Walk every subdirectory without blocking the Tk event loop."""
        root = Path(folder).resolve()
        excluded = Path(excluded_root).resolve() if excluded_root else None
        skip_names = {"$recycle.bin", "system volume information", "node_modules", ".git", "__pycache__", ".thumbnails"}
        paths = []
        for base, dirs, names in os.walk(root, onerror=lambda _error: None):
            current = Path(base)
            if excluded and (current == excluded or excluded in current.parents):
                dirs[:] = []
                continue
            dirs[:] = [name for name in dirs if name.casefold() not in skip_names]
            for name in names:
                if Path(name).suffix.casefold() not in IMAGE_EXTENSIONS:
                    continue
                candidate = current / name
                try:
                    if candidate.is_file():
                        paths.append(_safe_resolve(candidate))
                except OSError:
                    continue
        return paths

    def import_paths(self, paths):
        """Index discovered images and relink old missing records by unique name."""
        candidates = []
        for value in paths:
            try:
                item = Path(value)
                if item.suffix.casefold() in IMAGE_EXTENSIONS and item.is_file():
                    candidates.append(_safe_resolve(item))
            except OSError:
                continue
        recovered = self.relink_missing_files(candidates)
        with self._lock:
            before = self.db.total_changes
            self.db.executemany(
                "INSERT OR IGNORE INTO photos(path,folder) VALUES (?,?)",
                [(path, str(Path(path).parent)) for path in candidates],
            )
            added = self.db.total_changes - before
            self.db.commit()
        return added, recovered

    def index_paths(self, paths):
        with self._lock:
            before = self.db.total_changes
            self.db.executemany(
                "INSERT OR IGNORE INTO photos(path,folder) VALUES (?,?)",
                [(_safe_resolve(path), str(Path(_safe_resolve(path)).parent)) for path in paths],
            )
            added = self.db.total_changes - before
            self.db.commit()
        return added

    def move_unassigned_to_inbox(self, paths, destination):
        """Move unassigned library photos to the shared incoming folder."""
        inbox = Path(destination) / "Biblioteca"
        inbox.mkdir(parents=True, exist_ok=True)
        results = []
        inbox = inbox.resolve()
        for old in paths:
            source = Path(old)
            try:
                if not source.is_file():
                    continue
                if source.resolve().parent == inbox:
                    continue
                target = inbox / source.name
                suffix = 2
                while target.exists():
                    target = inbox / f"{source.stem} ({suffix}){source.suffix}"
                    suffix += 1
                _move_file(source, target)
                results.append((_safe_resolve(source), _safe_resolve(target)))
            except OSError:
                continue
        return results

    @staticmethod
    def remove_album_photos_to_inbox(album_id, paths, destination, progress=None):
        """Remove album membership and return selected files to Biblioteca."""
        inbox = (Path(destination) / "Biblioteca").resolve()
        inbox.mkdir(parents=True, exist_ok=True)
        unique_paths = list(dict.fromkeys(str(path) for path in paths))
        moved = []
        new_for_old = {}
        missing = 0
        db = None

        try:
            for index, old in enumerate(unique_paths, 1):
                source = Path(old)
                if not source.is_file():
                    missing += 1
                elif source.resolve().parent != inbox:
                    target = inbox / source.name
                    suffix = 2
                    while target.exists():
                        target = inbox / f"{source.stem} ({suffix}){source.suffix}"
                        suffix += 1
                    _move_file(source, target)
                    new = _safe_resolve(target)
                    moved.append((old, new))
                    new_for_old[old] = new
                if progress and (index == 1 or index % 5 == 0 or index == len(unique_paths)):
                    progress(index, len(unique_paths))

            db = sqlite3.connect(DB_PATH, timeout=30)
            db.execute("PRAGMA busy_timeout=30000")
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE")
            for old in unique_paths:
                new = new_for_old.get(old)
                if new:
                    db.execute("INSERT OR IGNORE INTO photos(path,folder) VALUES (?,?)", (new, str(inbox)))
                    db.execute(
                        "INSERT OR IGNORE INTO memberships(photo_path,album_id) "
                        "SELECT ?,album_id FROM memberships WHERE photo_path=?",
                        (new, old),
                    )
                    db.execute("UPDATE albums SET cover=? WHERE cover=?", (new, old))
                    db.execute("DELETE FROM photos WHERE path=?", (old,))
                db.execute("DELETE FROM memberships WHERE album_id=? AND photo_path=?", (album_id, new or old))
            db.commit()
        except Exception:
            if db is not None:
                try:
                    db.rollback()
                except sqlite3.Error:
                    pass
            for old, new in reversed(moved):
                try:
                    if Path(new).is_file() and not Path(old).exists():
                        Path(old).parent.mkdir(parents=True, exist_ok=True)
                        _move_file(new, old)
                except OSError:
                    pass
            raise
        finally:
            if db is not None:
                db.close()

        return len(moved), missing

    def update_moved_paths(self, moved):
        """Rewrite the catalog atomically after files have been moved on disk."""
        if not moved:
            return
        with self._lock:
            try:
                self.db.execute("CREATE TEMP TABLE IF NOT EXISTS move_paths(old TEXT PRIMARY KEY,new TEXT NOT NULL,folder TEXT NOT NULL)")
                self.db.execute("DELETE FROM move_paths")
                self.db.executemany(
                    "INSERT OR REPLACE INTO move_paths(old,new,folder) VALUES (?,?,?)",
                    [(old, new, str(Path(new).parent)) for old, new in moved],
                )
                self.db.execute("INSERT OR IGNORE INTO photos(path,folder) SELECT new,folder FROM move_paths")
                self.db.execute(
                    "INSERT OR IGNORE INTO memberships(photo_path,album_id) SELECT p.new,m.album_id FROM memberships m JOIN move_paths p ON m.photo_path=p.old"
                )
                self.db.execute(
                    "UPDATE albums SET cover=(SELECT p.new FROM move_paths p WHERE p.old=albums.cover) WHERE cover IN (SELECT old FROM move_paths)"
                )
                self.db.execute("DELETE FROM photos WHERE path IN (SELECT old FROM move_paths)")
                self.db.execute("DELETE FROM move_paths")
                self.db.commit()
            except sqlite3.Error:
                self.db.rollback()
                raise

    def relink_missing_files(self, candidates):
        """Reconnect a missing catalog entry only when both names are unique."""
        candidate_map = {}
        ambiguous_candidates = set()
        for value in candidates:
            candidate = Path(value)
            if not _safe_is_file(candidate):
                continue
            key = candidate.name.casefold()
            resolved = _safe_resolve(candidate)
            previous = candidate_map.get(key)
            if previous and os.path.normcase(previous) != os.path.normcase(resolved):
                ambiguous_candidates.add(key)
            else:
                candidate_map[key] = resolved
        for key in ambiguous_candidates:
            candidate_map.pop(key, None)
        if not candidate_map:
            return 0
        wanted = set(candidate_map)
        missing_map = {}
        ambiguous_missing = set()
        with self._lock:
            for (old,) in self.db.execute("SELECT path FROM photos"):
                if _safe_is_file(old):
                    continue
                key = Path(old).name.casefold()
                if key not in wanted:
                    continue
                if key in missing_map:
                    ambiguous_missing.add(key)
                else:
                    missing_map[key] = old
            recovered = 0
            for key in wanted - ambiguous_missing:
                old = missing_map.get(key)
                new = candidate_map.get(key)
                if not old or not new:
                    continue
                try:
                    self.db.execute("INSERT OR IGNORE INTO photos(path,folder) VALUES (?,?)", (new, str(Path(new).parent)))
                    self.db.execute(
                        "INSERT OR IGNORE INTO memberships(photo_path,album_id) "
                        "SELECT ?,album_id FROM memberships WHERE photo_path=?",
                        (new, old),
                    )
                    self.db.execute("UPDATE albums SET cover=? WHERE cover=?", (new, old))
                    self.db.execute("DELETE FROM photos WHERE path=?", (old,))
                    recovered += 1
                except sqlite3.Error:
                    continue
            self.db.commit()
        return recovered

    def photos(self, folder=None, album_id=None, unassigned=False):
        sql = "SELECT p.path FROM photos p"
        where, args = [], []
        if album_id is not None:
            sql += " JOIN memberships m ON m.photo_path=p.path"
            where.append("m.album_id=?")
            args.append(album_id)
        if folder:
            where.append("p.folder=?")
            args.append(folder)
        if unassigned:
            where.append("NOT EXISTS(SELECT 1 FROM memberships m2 WHERE m2.photo_path=p.path)")
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY p.path COLLATE NOCASE"
        with self._lock:
            return [r[0] for r in self.db.execute(sql, args)]

    def photo_counts(self):
        with self._lock:
            total, unassigned = self.db.execute(
                "SELECT COUNT(*), SUM(CASE WHEN NOT EXISTS("
                "SELECT 1 FROM memberships m WHERE m.photo_path=p.path) THEN 1 ELSE 0 END) "
                "FROM photos p"
            ).fetchone()
            return int(total or 0), int(unassigned or 0)

    def albums(self):
        with self._lock:
            return self.db.execute(
                "SELECT a.id,a.name,a.face_group,COUNT(m.photo_path),a.cover,a.created_at "
                "FROM albums a LEFT JOIN memberships m ON m.album_id=a.id GROUP BY a.id ORDER BY a.name COLLATE NOCASE"
            ).fetchall()

    def album_photo_paths(self):
        with self._lock:
            return self.db.execute("SELECT album_id,photo_path FROM memberships ORDER BY album_id,photo_path COLLATE NOCASE").fetchall()

    def create_album(self, name, paths=(), face_group=False):
        with self._lock:
            cur = self.db.execute("INSERT INTO albums(name,face_group) VALUES (?,?)", (name, int(face_group)))
            album_id = cur.lastrowid
            self.db.executemany("INSERT OR IGNORE INTO memberships(photo_path,album_id) VALUES (?,?)", [(p, album_id) for p in paths])
            self.db.commit()
            return album_id

    def assign(self, album_id, paths):
        with self._lock:
            self.db.executemany("INSERT OR IGNORE INTO memberships(photo_path,album_id) VALUES (?,?)", [(p, album_id) for p in paths])
            self.db.commit()

    def unassign(self, album_id, paths):
        with self._lock:
            self.db.executemany("DELETE FROM memberships WHERE album_id=? AND photo_path=?", [(album_id, p) for p in paths])
            self.db.commit()

    def membership_map(self, paths):
        result = {}
        paths = list(paths)
        with self._lock:
            for start in range(0, len(paths), 500):
                batch = paths[start:start + 500]
                if not batch:
                    continue
                marks = ",".join("?" for _ in batch)
                for path, name in self.db.execute(
                    f"SELECT m.photo_path,a.name FROM memberships m JOIN albums a ON a.id=m.album_id WHERE m.photo_path IN ({marks})",
                    batch,
                ):
                    result.setdefault(path, []).append(name)
        return result


class SlimScrollbar(tk.Canvas):
    """Small rounded scrollbar that appears while its scroll area is hovered."""

    def __init__(self, master, command):
        super().__init__(master, width=8, highlightthickness=0, bd=0, bg=BG, cursor="")
        self._app = RoundedButton._find_app(master)
        if self._app is not None:
            if not hasattr(self._app, "_wallpaper_alpha_widgets"):
                self._app._wallpaper_alpha_widgets = weakref.WeakSet()
            self._app._wallpaper_alpha_widgets.add(self)
        self.command = command
        self.first, self.last = 0.0, 1.0
        self.visible = False
        self._hide_id = None
        self._drag_offset = None
        self.bind("<Enter>", self.show)
        self.bind("<Leave>", self.hide_later)
        self.bind("<Button-1>", self._press)
        self.bind("<B1-Motion>", self._drag)
        self.bind("<ButtonRelease-1>", lambda _event: setattr(self, "_drag_offset", None))
        self.bind("<Configure>", lambda _event: self._draw())

    def set(self, first, last):
        self.first, self.last = float(first), float(last)
        self._draw()

    def show(self, _event=None):
        changed = not self.visible
        if self._hide_id:
            try:
                self.after_cancel(self._hide_id)
            except tk.TclError:
                pass
            self._hide_id = None
        self.visible = True
        if changed:
            self._draw()

    def hide_later(self, _event=None):
        if self._hide_id:
            try:
                self.after_cancel(self._hide_id)
            except tk.TclError:
                pass
        self._hide_id = self.after(650, self._hide)

    def _hide(self):
        self._hide_id = None
        self.visible = False
        self._draw()

    def _draw(self):
        self.delete("all")
        height = max(1, self.winfo_height())
        if not self.visible or self.last - self.first >= 0.999:
            return
        top = max(0, min(height - 1, round(self.first * height)))
        thumb_height = max(30, round((self.last - self.first) * height))
        thumb_height = min(height - top, thumb_height)
        if thumb_height < 1 or Image is None:
            return
        scale = 6
        width = max(1, self.winfo_width())
        art = Image.new("RGBA", (width * scale, thumb_height * scale), (0, 0, 0, 0))
        ImageDraw.Draw(art).rounded_rectangle(
            (scale, 0, (width - 1) * scale, thumb_height * scale - 1),
            radius=max(1, width * scale // 2),
            fill=MUTED,
        )
        art = art.resize((width, thumb_height), Image.Resampling.LANCZOS)
        if getattr(self._app, "_wallpaper_transparent", False):
            art = _hard_alpha(art)
        self._thumb_ref = ImageTk.PhotoImage(art, master=self)
        self.create_image(width // 2, top, image=self._thumb_ref, anchor="n")

    def refresh_wallpaper_surface(self):
        self._draw()

    def _press(self, event):
        height = max(1, self.winfo_height())
        top, bottom = self.first * height, self.last * height
        if top <= event.y <= bottom:
            self._drag_offset = event.y - top
        else:
            self.command("moveto", event.y / height)

    def _drag(self, event):
        if self._drag_offset is None:
            return
        height = max(1, self.winfo_height())
        self.command("moveto", max(0, min(1, (event.y - self._drag_offset) / height)))


class RoundedButton(tk.Canvas):
    """Canvas button with an antialiased 14px shape and nonblocking color transitions."""

    def __init__(self, master, *, text="", command=None, width=140, height=40, primary=False, state="normal", style=None):
        self._surface_bg = self._surface_color(master)
        super().__init__(master, width=width, height=height, bg=self._surface_bg, bd=0, highlightthickness=0, cursor="hand2", takefocus=1)
        self._text = str(text)
        self._command = command
        self._primary = bool(primary)
        self._state = state
        self._style_name = style or ""
        self._app = self._find_app(master)
        self._hovered = False
        self._pressed = False
        self._current_palette = self._target_palette()
        self._transition_after_id = None
        self._redraw_after_id = None
        self._icon_source = None
        self._icon_image = None
        self._icon_photo = None
        self._image_compound = "left"
        if self._app is not None:
            if not hasattr(self._app, "_rounded_buttons"):
                self._app._rounded_buttons = weakref.WeakSet()
            self._app._rounded_buttons.add(self)
        self.bind("<Configure>", self._queue_redraw, add="+")
        self.bind("<Enter>", self._on_enter, add="+")
        self.bind("<Leave>", self._on_leave, add="+")
        self.bind("<ButtonPress-1>", self._on_press, add="+")
        self.bind("<ButtonRelease-1>", self._on_release, add="+")
        self.bind("<FocusOut>", self._on_focus_out, add="+")
        self.bind("<Return>", self._on_keyboard_activate, add="+")
        self.bind("<space>", self._on_keyboard_activate, add="+")
        self.bind("<Destroy>", self._cancel_transition, add="+")
        self.after_idle(self._draw_button)

    @staticmethod
    def _surface_color(widget):
        current = widget
        while current is not None:
            try:
                color = current.cget("background")
            except (AttributeError, tk.TclError):
                color = ""
            if color:
                return color
            current = getattr(current, "master", None)
        return BG

    @staticmethod
    def _find_app(widget):
        current = widget
        while current is not None:
            if hasattr(current, "accent") and hasattr(current, "store"):
                return current
            current = getattr(current, "master", None)
        return None

    @staticmethod
    def _mix_color(first, second, amount):
        first_rgb = tuple(int(first[index:index + 2], 16) for index in (1, 3, 5))
        second_rgb = tuple(int(second[index:index + 2], 16) for index in (1, 3, 5))
        mixed = tuple(round(a + (b - a) * amount) for a, b in zip(first_rgb, second_rgb))
        return "#{:02x}{:02x}{:02x}".format(*mixed)

    def _target_palette(self):
        accent = getattr(self._app, "accent", LIME)
        if self._primary:
            top = accent
            bottom = self._mix_color(accent, "#ffffff", 0.12)
            border = self._mix_color(accent, "#ffffff", 0.22)
        elif self._style_name == "Sidebar.Active.TButton":
            top = bottom = self._mix_color(PANEL, accent, 0.22)
            border = self._mix_color(PANEL, accent, 0.42)
        else:
            top = bottom = PANEL
            border = LINE

        if self._state == "disabled":
            top = self._mix_color(top, BG, 0.42)
            bottom = self._mix_color(bottom, BG, 0.42)
            border = self._mix_color(border, BG, 0.5)
        elif self._pressed:
            top = self._mix_color(top, "#000000", 0.12)
            bottom = self._mix_color(bottom, "#000000", 0.12)
            border = self._mix_color(border, "#000000", 0.12)
        elif self._hovered:
            target = accent if self._style_name.startswith("Sidebar") else "#ffffff"
            top = self._mix_color(top, target, 0.08)
            bottom = self._mix_color(bottom, target, 0.08)
            border = self._mix_color(border, target, 0.12)
        return top, bottom, border

    def _queue_redraw(self, _event=None):
        if self._redraw_after_id:
            try:
                self.after_cancel(self._redraw_after_id)
            except tk.TclError:
                pass
        self._redraw_after_id = self.after(40, self._draw_button)

    def _draw_button(self):
        self._redraw_after_id = None
        try:
            width = max(1, self.winfo_width(), self.winfo_reqwidth())
            height = max(1, self.winfo_height(), self.winfo_reqheight())
            if width <= 1 or height <= 1 or Image is None:
                return
            scale = 4
            shape_size = (width * scale, height * scale)
            base = Image.new("RGB", shape_size)
            draw = ImageDraw.Draw(base)
            top, bottom, border = self._current_palette
            top_rgb = tuple(int(top[index:index + 2], 16) for index in (1, 3, 5))
            bottom_rgb = tuple(int(bottom[index:index + 2], 16) for index in (1, 3, 5))
            if top_rgb == bottom_rgb:
                draw.rectangle((0, 0, shape_size[0] - 1, shape_size[1] - 1), fill=top)
            else:
                for y in range(shape_size[1]):
                    ratio = y / max(1, shape_size[1] - 1)
                    color = tuple(round(a + (b - a) * ratio) for a, b in zip(top_rgb, bottom_rgb))
                    draw.line((0, y, shape_size[0] - 1, y), fill=color)
            mask = Image.new("L", shape_size, 0)
            mask_draw = ImageDraw.Draw(mask)
            radius = min(14 * scale, shape_size[0] // 2, shape_size[1] // 2)
            box = (0, 0, shape_size[0] - 1, shape_size[1] - 1)
            mask_draw.rounded_rectangle(box, radius=radius, fill=255)
            art = Image.new("RGBA", shape_size, (0, 0, 0, 0))
            art.paste(base, (0, 0))
            art.putalpha(mask)
            ImageDraw.Draw(art).rounded_rectangle(box, radius=radius, outline=border, width=scale)
            art = art.resize((width, height), Image.Resampling.LANCZOS)
            if getattr(self._app, "_wallpaper_transparent", False):
                art = _hard_alpha(art)
            self._button_photo = ImageTk.PhotoImage(art, master=self._app or self)
            self.delete("all")
            self.create_image(0, 0, image=self._button_photo, anchor="nw")

            foreground = MUTED if self._state == "disabled" else "#15131a" if self._primary else INK
            is_sidebar = self._style_name.startswith("Sidebar")
            text = self._text
            text_x = width / 2
            text_anchor = "center"
            if self._icon_photo is not None and self._image_compound == "left":
                icon_x = 15 if is_sidebar else max(12, (width - len(text) * 7 - 34) / 2)
                self.create_image(icon_x, height / 2, image=self._icon_photo, anchor="w")
                text_x = icon_x + 34
                text_anchor = "w"
            elif is_sidebar:
                text_x = 13
                text_anchor = "w"
            font_size = 11 if width <= 58 else 9
            available_text_width = max(1, width - 24)
            if (self._icon_photo is not None and self._image_compound == "left") or is_sidebar:
                available_text_width = max(1, width - text_x - 8)
            while font_size > 7 and tkfont.Font(
                root=self,
                family="Inter",
                size=font_size,
                weight="bold",
            ).measure(text) > available_text_width:
                font_size -= 1
            self.create_text(
                text_x,
                height / 2,
                text=text,
                fill=foreground,
                font=("Inter", font_size, "bold"),
                anchor=text_anchor,
            )
        except tk.TclError:
            return

    def _cancel_transition(self, event=None):
        if event is not None and event.widget is not self:
            return
        if self._transition_after_id:
            try:
                self.after_cancel(self._transition_after_id)
            except tk.TclError:
                pass
            self._transition_after_id = None

    def _animate_to_state(self):
        self._cancel_transition()
        start = self._current_palette
        target = self._target_palette()
        started = time.monotonic()

        def advance():
            try:
                if not self.winfo_exists():
                    self._transition_after_id = None
                    return
                progress = min(1.0, (time.monotonic() - started) / 0.15)
                eased = 1 - (1 - progress) ** 3
                palette = []
                for start_color, end_color in zip(start, target):
                    start_rgb = tuple(int(start_color[index:index + 2], 16) for index in (1, 3, 5))
                    end_rgb = tuple(int(end_color[index:index + 2], 16) for index in (1, 3, 5))
                    color = tuple(round(a + (b - a) * eased) for a, b in zip(start_rgb, end_rgb))
                    palette.append("#{0:02x}{1:02x}{2:02x}".format(*color))
                self._current_palette = tuple(palette)
                self._draw_button()
                if progress < 1:
                    self._transition_after_id = self.after(16, advance)
                else:
                    self._transition_after_id = None
            except tk.TclError:
                self._transition_after_id = None

        self._transition_after_id = self.after(16, advance)

    def _on_enter(self, _event=None):
        if self._state != "disabled":
            self._hovered = True
            self._animate_to_state()

    def _on_leave(self, _event=None):
        self._hovered = False
        self._pressed = False
        self._animate_to_state()

    def _on_focus_out(self, _event=None):
        self._hovered = False
        self._pressed = False
        self._animate_to_state()

    def _on_press(self, event):
        if self._state != "disabled" and event.num == 1:
            self._pressed = True
            self.focus_set()
            self._animate_to_state()

    def _on_release(self, event):
        was_pressed = self._pressed
        self._pressed = False
        self._animate_to_state()
        if was_pressed and 0 <= event.x < self.winfo_width() and 0 <= event.y < self.winfo_height():
            self.invoke()

    def _on_keyboard_activate(self, _event=None):
        self.invoke()
        return "break"

    def invoke(self):
        if self._state != "disabled" and callable(self._command):
            return self._command()
        return None

    def refresh_theme(self):
        if self._transition_after_id:
            self._cancel_transition()
        self._current_palette = self._target_palette()
        if not getattr(self._app, "_wallpaper_enabled", False):
            self._surface_bg = self._surface_color(self.master)
            self.configure(bg=self._surface_bg)
        self._refresh_icon_photo()
        self._draw_button()

    def _refresh_icon_photo(self):
        if self._icon_image is None:
            self._icon_photo = None
            return
        icon = self._icon_image.copy()
        if getattr(self._app, "_wallpaper_transparent", False):
            icon = _hard_alpha(icon)
        self._icon_photo = ImageTk.PhotoImage(icon, master=self._app or self)

    def configure(self, cnf=None, **kwargs):
        if cnf is None and not kwargs:
            return super().configure()
        if isinstance(cnf, str) and not kwargs:
            return super().configure(cnf)
        if isinstance(cnf, dict):
            kwargs = {**cnf, **kwargs}
            cnf = None
        text = kwargs.pop("text", None)
        command = kwargs.pop("command", None)
        state = kwargs.pop("state", None)
        style = kwargs.pop("style", None)
        image = kwargs.pop("image", None)
        compound = kwargs.pop("compound", None)
        if text is not None:
            self._text = str(text)
        if command is not None:
            self._command = command
        if state is not None:
            self._state = str(state)
            kwargs["cursor"] = "" if self._state == "disabled" else "hand2"
        if style is not None:
            self._style_name = str(style)
        if image is not None:
            self._icon_source = image
            try:
                icon = ImageTk.getimage(image).convert("RGBA")
                icon.thumbnail((28, 28), Image.Resampling.LANCZOS)
                self._icon_image = icon
            except (AttributeError, tk.TclError, TypeError, ValueError):
                self._icon_image = None
                self._icon_photo = None
        if compound is not None:
            self._image_compound = str(compound)
        result = super().configure(cnf, **kwargs)
        if text is not None or state is not None or style is not None or image is not None or compound is not None:
            self.refresh_theme()
        return result

    config = configure

    def cget(self, key):
        if key == "text":
            return self._text
        if key == "state":
            return self._state
        if key == "style":
            return self._style_name
        return super().cget(key)


class ModernScale(tk.Canvas):
    def __init__(self, parent, *, variable, from_, to, command=None, step=None):
        self._surface_bg = RoundedButton._surface_color(parent)
        super().__init__(parent, height=28, bg=self._surface_bg, highlightthickness=0, bd=0, cursor="hand2", takefocus=1)
        self.variable = variable
        self.minimum = float(from_)
        self.maximum = float(to)
        self.command = command
        self.step = float(step) if step is not None else (self.maximum - self.minimum) / 100
        self.accent = self._find_accent(parent)
        self._redraw_after_id = None
        self._trace_id = variable.trace_add("write", self._queue_redraw)
        self.bind("<Configure>", self._queue_redraw, add="+")
        self.bind("<Button-1>", self._set_from_event)
        self.bind("<B1-Motion>", self._set_from_event)
        self.bind("<Left>", lambda _event: self._step_by(-1))
        self.bind("<Right>", lambda _event: self._step_by(1))
        self.bind("<FocusIn>", self._queue_redraw, add="+")
        self.bind("<FocusOut>", self._queue_redraw, add="+")
        self.bind("<Destroy>", self._cleanup, add="+")
        self.after_idle(self._draw)

    @staticmethod
    def _find_accent(widget):
        current = widget
        while current is not None:
            if hasattr(current, "accent"):
                return current.accent
            current = getattr(current, "master", None)
        return LIME

    def _queue_redraw(self, *_args):
        if self._redraw_after_id is None:
            try:
                self._redraw_after_id = self.after_idle(self._draw)
            except tk.TclError:
                self._redraw_after_id = None

    def _cleanup(self, event=None):
        if event is not None and event.widget is not self:
            return
        if self._trace_id:
            try:
                self.variable.trace_remove("write", self._trace_id)
            except tk.TclError:
                pass
            self._trace_id = None
        if self._redraw_after_id:
            try:
                self.after_cancel(self._redraw_after_id)
            except tk.TclError:
                pass
            self._redraw_after_id = None

    def _current_value(self):
        try:
            return max(self.minimum, min(self.maximum, float(self.variable.get())))
        except (TypeError, ValueError, tk.TclError):
            return self.minimum

    def _draw(self):
        self._redraw_after_id = None
        try:
            if not self.winfo_exists():
                return
            width = max(1, self.winfo_width())
            height = max(1, self.winfo_height())
            left, right = 10, max(11, width - 10)
            center_y = height // 2
            ratio = (self._current_value() - self.minimum) / max(1e-9, self.maximum - self.minimum)
            thumb_x = left + (right - left) * ratio
            self.delete("all")
            self.create_line(left, center_y, right, center_y, fill=LINE, width=4, capstyle="round")
            accent = self._find_accent(self)
            if thumb_x > left:
                self.create_line(left, center_y, thumb_x, center_y, fill=accent, width=4, capstyle="round")
            if self.focus_get() is self:
                self.create_oval(thumb_x - 10, center_y - 10, thumb_x + 10, center_y + 10, outline=accent, width=1)
            self.create_oval(thumb_x - 7, center_y - 7, thumb_x + 7, center_y + 7, fill=accent, outline=PANEL, width=2)
        except tk.TclError:
            return

    def refresh_theme(self):
        self._surface_bg = RoundedButton._surface_color(self.master)
        self.configure(bg=self._surface_bg)
        self._draw()

    def _set_value(self, value):
        value = max(self.minimum, min(self.maximum, value))
        if self.step > 0:
            value = self.minimum + round((value - self.minimum) / self.step) * self.step
        value = max(self.minimum, min(self.maximum, value))
        self.variable.set(round(value, 3))
        if self.command:
            self.command(str(value))
        return "break"

    def _set_from_event(self, event):
        self.focus_set()
        left, right = 10, max(11, self.winfo_width() - 10)
        ratio = max(0.0, min(1.0, (event.x - left) / (right - left)))
        return self._set_value(self.minimum + ratio * (self.maximum - self.minimum))

    def _step_by(self, direction):
        return self._set_value(self._current_value() + direction * max(self.step, 0.01))


class RoundedSwitch(tk.Frame):
    def __init__(self, parent, *, text, variable):
        self._surface_bg = RoundedButton._surface_color(parent)
        super().__init__(parent, bg=self._surface_bg, cursor="hand2", takefocus=1)
        self._app = RoundedButton._find_app(parent)
        if self._app is not None:
            if not hasattr(self._app, "_wallpaper_alpha_widgets"):
                self._app._wallpaper_alpha_widgets = weakref.WeakSet()
            self._app._wallpaper_alpha_widgets.add(self)
        self._galeria_keep_surface = True
        self.variable = variable
        self.switch = tk.Canvas(self, width=48, height=28, bg=self._surface_bg, highlightthickness=0, bd=0, cursor="hand2")
        self.switch._galeria_keep_surface = True
        self.switch.pack(side="left", padx=(0, 9))
        self.label = tk.Label(self, text=text, bg=self._surface_bg, fg=INK, font=("Inter", 9), cursor="hand2")
        self.label.pack(side="left", anchor="w")
        self._trace_id = variable.trace_add("write", self._draw)
        for widget in (self, self.switch, self.label):
            widget.bind("<Button-1>", self._toggle, add="+")
            widget.bind("<Return>", self._toggle, add="+")
            widget.bind("<space>", self._toggle, add="+")
            widget.bind("<Destroy>", self._cleanup, add="+")
        self.bind("<FocusIn>", self._draw, add="+")
        self.bind("<FocusOut>", self._draw, add="+")
        self.after_idle(self._draw)

    def _cleanup(self, event=None):
        if event is not None and event.widget is not self:
            return
        if self._trace_id:
            try:
                self.variable.trace_remove("write", self._trace_id)
            except tk.TclError:
                pass
            self._trace_id = None

    def _toggle(self, _event=None):
        self.focus_set()
        self.variable.set(not bool(self.variable.get()))
        return "break"

    def refresh_theme(self):
        self._draw()

    def _draw(self, *_args):
        try:
            surface = RoundedButton._surface_color(self.master)
            self.configure(bg=surface)
            self.switch.configure(bg=surface)
            self.label.configure(bg=surface)
            self.switch.delete("all")
            active = bool(self.variable.get())
            accent = ModernScale._find_accent(self)
            scale = 4
            width, height = 48, 28
            art = Image.new("RGBA", (width * scale, height * scale), (0, 0, 0, 0))
            draw = ImageDraw.Draw(art)
            track = accent if active else LINE
            draw.rounded_rectangle(
                (2 * scale, 4 * scale, 46 * scale, 24 * scale),
                radius=10 * scale,
                fill=track,
            )
            if self.focus_get() is self:
                draw.rounded_rectangle(
                    (1 * scale, 3 * scale, 47 * scale, 25 * scale),
                    radius=11 * scale,
                    outline=accent,
                    width=scale,
                )
            knob_x = 34 if active else 14
            draw.ellipse(
                ((knob_x - 7) * scale, 7 * scale, (knob_x + 7) * scale, 21 * scale),
                fill="#ffffff" if active else MUTED,
            )
            art = art.resize((width, height), Image.Resampling.LANCZOS)
            if getattr(self._app, "_wallpaper_transparent", False):
                art = _hard_alpha(art)
            self._switch_photo = ImageTk.PhotoImage(art, master=self)
            self.switch.create_image(0, 0, image=self._switch_photo, anchor="nw")
        except tk.TclError:
            return

    def refresh_wallpaper_surface(self):
        self._draw()


class RoundedControl(RoundedButton):
    """Rounded canvas buttons and dropdowns with a shared visual style."""

    def __init__(
        self,
        parent,
        *,
        kind="button",
        text="",
        command=None,
        values=None,
        textvariable=None,
        width=140,
        primary=False,
        state="normal",
        style=None,
    ):
        self.kind = kind
        self._dropdown_values = list(values or ())
        self._dropdown_variable = textvariable
        self._dropdown_popup = None
        self._dropdown_trace = None
        self._dropdown_initial_text = text
        if kind == "dropdown":
            if self._dropdown_variable is None:
                self._dropdown_variable = tk.StringVar(master=parent, value="")
            pixel_width = max(110, min(420, round(width * 8 + 32)))
            super().__init__(
                parent,
                text="",
                command=self._open_dropdown,
                width=pixel_width,
                height=38,
                primary=primary,
                state="disabled" if state == "disabled" else "normal",
                style=style or "Dropdown.TButton",
            )
            self._dropdown_trace = self._dropdown_variable.trace_add("write", self._sync_dropdown_caption)
            self._sync_dropdown_caption()
            self.bind("<Down>", self._open_dropdown)
            self.bind("<Alt-Down>", self._open_dropdown)
            self.bind("<FocusOut>", self._dropdown_focus_left, add="+")
            self.bind("<Destroy>", self._cleanup_dropdown, add="+")
        else:
            super().__init__(
                parent,
                text=text,
                command=command,
                width=width,
                height=40,
                primary=primary,
                state=state,
                style=style or ("Accent.TButton" if primary else "TButton"),
            )

    def _cleanup_dropdown(self, event=None):
        if event is not None and event.widget is not self:
            return
        self._close_dropdown()
        if self._dropdown_trace and self._dropdown_variable is not None:
            try:
                self._dropdown_variable.trace_remove("write", self._dropdown_trace)
            except (tk.TclError, AttributeError):
                pass
            self._dropdown_trace = None

    def _sync_dropdown_caption(self, *_args):
        if self._dropdown_variable is None:
            return
        value = self._dropdown_variable.get()
        shown = value if value else self._dropdown_initial_text
        self.configure(text=f"{shown}  ▾" if shown else "Escolher  ▾")

    def _set_dropdown_value(self, value, *, emit=True):
        if self._dropdown_variable is None:
            return
        self._dropdown_variable.set(value)
        if emit:
            try:
                self.event_generate("<<ComboboxSelected>>", when="tail")
            except tk.TclError:
                pass
        self._close_dropdown()

    def get(self):
        if self.kind != "dropdown":
            raise AttributeError("get() is available on dropdown controls")
        return self._dropdown_variable.get()

    def set(self, value):
        if self.kind != "dropdown":
            raise AttributeError("set() is available on dropdown controls")
        self._dropdown_variable.set(value)

    def current(self, index=None):
        if self.kind != "dropdown":
            raise AttributeError("current() is available on dropdown controls")
        if index is None:
            try:
                return self._dropdown_values.index(self.get())
            except ValueError:
                return -1
        if 0 <= int(index) < len(self._dropdown_values):
            self._set_dropdown_value(self._dropdown_values[int(index)])

    @staticmethod
    def _rounded_panel(canvas, width, height, radius=14):
        radius = max(1, min(radius, width // 2, height // 2))
        points = [
            radius, 0, width - radius, 0, width, 0,
            width, radius, width, height - radius, width, height,
            width - radius, height, radius, height, 0, height,
            0, height - radius, 0, radius, 0, 0,
        ]
        return canvas.create_polygon(points, smooth=True, splinesteps=24, fill=PANEL, outline=LINE)

    def configure(self, cnf=None, **kw):
        if self.kind == "dropdown" and kw:
            values = kw.pop("values", None)
            variable = kw.pop("textvariable", None)
            state = kw.get("state")
            if values is not None:
                self._dropdown_values = list(values)
            if variable is not None and variable is not self._dropdown_variable:
                if self._dropdown_trace:
                    try:
                        self._dropdown_variable.trace_remove("write", self._dropdown_trace)
                    except (tk.TclError, AttributeError):
                        pass
                self._dropdown_variable = variable
                self._dropdown_trace = variable.trace_add("write", self._sync_dropdown_caption)
                self._sync_dropdown_caption()
            if state == "readonly":
                kw["state"] = "normal"
            if values is not None or variable is not None:
                if not kw and cnf is None:
                    return None
        return super().configure(cnf, **kw)

    config = configure

    def _open_dropdown(self, _event=None):
        if self.kind != "dropdown" or str(self.cget("state")) == "disabled":
            return "break"
        self._close_dropdown()
        values = list(self._dropdown_values)
        if not values:
            return "break"
        self.update_idletasks()
        row_height = 32
        visible_rows = min(8, len(values))
        menu_height = visible_rows * row_height + 4
        screen_width = self.winfo_screenwidth()
        screen_height = self.winfo_screenheight()
        estimated_width = max(self.winfo_width(), max(len(str(value)) for value in values) * 7 + 48)
        menu_width = max(150, min(screen_width - 16, estimated_width))
        x = max(8, min(self.winfo_rootx(), screen_width - menu_width - 8))
        y = self.winfo_rooty() + self.winfo_height()
        if y + menu_height > screen_height - 8:
            y = max(8, self.winfo_rooty() - menu_height)

        popup = tk.Toplevel(self.winfo_toplevel())
        self._dropdown_popup = popup
        popup.withdraw()
        popup.overrideredirect(True)
        popup.configure(bg=LINE)
        popup.transient(self.winfo_toplevel())
        outer = tk.Frame(popup, bg=PANEL, highlightbackground=LINE, highlightthickness=1, bd=0)
        outer.pack(fill="both", expand=True)
        canvas = tk.Canvas(outer, width=menu_width - 2, height=menu_height - 2, bg=PANEL, highlightthickness=0, bd=0)
        canvas.pack(fill="both", expand=True, padx=1, pady=1)
        inner = tk.Frame(canvas, bg=PANEL)
        canvas.create_window((1, 1), window=inner, anchor="nw", width=max(1, menu_width - 4))
        option_buttons = []
        selected_index = {"value": self.current() if self.current() >= 0 else 0}
        owner = self.winfo_toplevel()
        theme_owner = owner
        while not hasattr(theme_owner, "_blend_color") and getattr(theme_owner, "master", None) is not None:
            theme_owner = theme_owner.master
        blend_color = getattr(theme_owner, "_blend_color", None)
        hover_color = blend_color(PANEL, getattr(theme_owner, "accent", INK), 0.18) if blend_color else PANEL

        def move_option(delta):
            selected_index["value"] = (selected_index["value"] + delta) % len(option_buttons)
            option_buttons[selected_index["value"]].focus_set()
            return "break"

        def invoke_option(_event=None):
            option_buttons[selected_index["value"]].invoke()
            return "break"

        for index, value in enumerate(values):
            option = tk.Button(
                inner,
                text=str(value),
                command=lambda item=value: self._set_dropdown_value(item),
                bg=PANEL,
                fg=INK,
                activebackground=hover_color,
                activeforeground=INK,
                disabledforeground=MUTED,
                relief="flat",
                borderwidth=0,
                highlightthickness=0,
                anchor="w",
                padx=12,
                pady=6,
                font=("Inter", 9, "bold"),
                cursor="hand2",
                takefocus=True,
            )
            option.pack(fill="x", padx=1, pady=1)
            option_buttons.append(option)
            option.bind("<Up>", lambda _event: move_option(-1))
            option.bind("<Down>", lambda _event: move_option(1))
            option.bind("<Return>", invoke_option)
            option.bind("<space>", invoke_option)
            option.bind("<MouseWheel>", lambda event, area=canvas: (area.yview_scroll(-1 if event.delta > 0 else 1, "units"), "break"))
            option.bind("<Escape>", lambda _event: self._close_dropdown())
            if index == selected_index["value"]:
                option.focus_set()
        inner.update_idletasks()
        canvas.configure(
            scrollregion=(0, 0, menu_width - 4, max(menu_height - 4, inner.winfo_reqheight() + 2))
        )
        canvas.bind("<MouseWheel>", lambda event: (canvas.yview_scroll(-1 if event.delta > 0 else 1, "units"), "break"))
        popup.bind("<Escape>", lambda _event: self._close_dropdown())
        popup.bind("<FocusOut>", self._dropdown_focus_left, add="+")
        popup.geometry(f"{menu_width}x{menu_height}+{x}+{y}")
        fade = getattr(owner, "_fade_in_window", None)
        if fade:
            fade(popup)
        else:
            popup.deiconify()
        popup.lift()
        option_buttons[selected_index["value"]].focus_force()
        return "break"

    def _dropdown_focus_left(self, _event=None):
        popup = self._dropdown_popup
        if popup is None:
            return
        try:
            def close_if_focus_is_outside(target=popup):
                if not target.winfo_exists():
                    return
                focus = target.focus_get()
                try:
                    if focus is None or focus.winfo_toplevel() is not target:
                        self._close_dropdown()
                except (AttributeError, tk.TclError):
                    self._close_dropdown()

            popup.after_idle(close_if_focus_is_outside)
        except tk.TclError:
            self._close_dropdown()

    def _close_dropdown(self):
        popup, self._dropdown_popup = self._dropdown_popup, None
        if popup is not None:
            try:
                popup.destroy()
            except tk.TclError:
                pass


class GalleryApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self._base_tk_scaling = float(self.tk.call("tk", "scaling"))
        self._private_fonts = self._load_private_fonts()
        self.title("Capri")
        self.geometry("1240x820")
        self.minsize(900, 620)
        self.configure(bg=BG)
        if Image is None:
            messagebox.showerror("Dependência ausente", "A biblioteca Pillow não está instalada. Abra iniciar_galeria.bat para preparar o aplicativo.")
            self.destroy()
            return
        self._capri_brand_original = None
        self._capri_logo_photo = None
        try:
            app_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
            with Image.open(app_root / "assets" / "capri-logo.jpg") as logo_source:
                self._capri_brand_original = logo_source.convert("RGB")
            window_icon = self._capri_brand_original.resize((64, 64), Image.Resampling.LANCZOS)
            self._capri_logo_photo = ImageTk.PhotoImage(window_icon, master=self)
            self.iconphoto(True, self._capri_logo_photo)
        except (OSError, tk.TclError):
            self._capri_brand_original = None
            self._capri_logo_photo = None
        self._install_themed_dialogs()
        self.store = Store()
        self._restore_window_geometry()
        self.theme = self.store.setting("theme", "dark")
        if self.theme not in ("dark", "light", "capri"):
            self.theme = "dark"
        stored_accent = self.store.setting("accent", "")
        self.accent = stored_accent or LIME
        if self.accent.lower() == LEGACY_CAPRI_ACCENT:
            self.accent = CAPRI_ACCENT
            self.store.set_setting("accent", self.accent)
        self.destination = self.store.setting("album_destination", "")
        self.ui_scale_percent = self._normalize_ui_scale(self.store.setting("ui_scale", "100"))
        self.sidebar_width = self._normalize_sidebar_width(self.store.setting("sidebar_width", "282"))
        self.gallery_density = self._normalize_gallery_density(self.store.setting("gallery_density", "comfortable"))
        self.photo_corner_radius = self._normalize_photo_corner_radius(self.store.setting("photo_corner_radius", "11"))
        self._apply_ui_scale(self.ui_scale_percent)
        prompt_for_destination = (
            not self.destination
            and self.store.setting("initial_setup_complete", "0") != "1"
        )
        self.active_view = "photos"
        self.view_mode = self.store.setting("view_mode", "grid")
        self.sort_by = self.store.setting("sort_by", "Nome")
        self.sort_desc = self.store.setting("sort_desc", "0") == "1"
        self.active_folder = None
        self.active_album = None
        self.active_album_name = ""
        self.active_filter = "all"
        self.visible_limit = 100
        self.sidebar_album_mode = self.store.setting("sidebar_album_view", "compact_grid")
        self.selected = set()
        self.last_clicked = None
        self.cards = {}
        self._album_drop_targets = weakref.WeakSet()
        self.photo_refs = []
        self.album_refs = []
        self.sidebar_album_refs = []
        self.album_art_cache = {}
        self.thumb_cache = {}
        self.thumb_mask_cache = {}
        self._thumb_cache_lock = threading.RLock()
        self._thumbnail_lock = threading.RLock()
        self._thumbnail_futures = {}
        self._wallpaper_thumbnail_widgets = weakref.WeakSet()
        self._thumbnail_scope_generations = {}
        self.thumbnail_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="Galeria-miniaturas")
        self.background_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="Galeria-fundo")
        self.photo_viewer_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="Capri-visualizador")
        self._photo_viewer = None
        self._photo_viewer_future = None
        self.review_refs = []
        self.worker_queue = queue.Queue()
        self._photo_render_generation = 0
        self._photo_render_after_id = None
        self._photo_drag_state = None
        self._import_busy = False
        self._scan_busy = False
        self._library_move_busy = False
        self._close_after_library_move = False
        self._status_after_id = None
        self._wallpaper_image_cache = None
        self._adaptive_wallpaper_path = None
        self._adaptive_accent = None
        self._background_after_id = None
        self._wallpaper_transparent = False
        self._wallpaper_enabled = False
        self._wallpaper_originals = {}
        self._wallpaper_text_styles = {}
        self._background_configure_id = None
        self._build_styles()
        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self.close)
        if self.theme in ("light", "capri"):
            self.apply_theme()
        else:
            self.refresh_all()
            self._apply_background()
        self._start_missing_photo_recovery()
        self.after(150, self._poll_worker)
        self.after_idle(self._apply_dark_titlebar)
        if prompt_for_destination:
            self.after(700, self._prompt_initial_album_destination)

    @staticmethod
    def _normalize_ui_scale(value):
        try:
            scale = int(value)
        except (TypeError, ValueError):
            return 100
        return min((90, 100, 110, 120), key=lambda option: abs(option - scale))

    @staticmethod
    def _normalize_sidebar_width(value):
        try:
            width = int(round(float(value) / 10) * 10)
        except (TypeError, ValueError):
            return 282
        return max(220, min(340, width))

    @staticmethod
    def _normalize_gallery_density(value):
        return value if value in ("compact", "comfortable", "spacious") else "comfortable"

    @staticmethod
    def _normalize_photo_corner_radius(value):
        try:
            radius = int(round(float(value)))
        except (TypeError, ValueError):
            return 11
        return max(0, min(20, radius))

    def _apply_ui_scale(self, percent):
        self.ui_scale_percent = self._normalize_ui_scale(percent)
        self.tk.call("tk", "scaling", self._base_tk_scaling * self.ui_scale_percent / 100)
        self.update_idletasks()

    def _apply_sidebar_width(self, width):
        self.sidebar_width = self._normalize_sidebar_width(width)
        if hasattr(self, "side"):
            self.side.configure(width=self.sidebar_width)
        if hasattr(self, "shell"):
            self.shell.sash_place(0, self.sidebar_width, 0)

    def _persist_sidebar_sash(self, _event=None):
        try:
            width = self._normalize_sidebar_width(self.shell.sash_coord(0)[0])
            self._apply_sidebar_width(width)
            if self.store.setting("sidebar_width", "") != str(width):
                self.store.set_setting("sidebar_width", str(width))
        except (IndexError, tk.TclError, sqlite3.Error) as exc:
            if isinstance(exc, sqlite3.Error):
                messagebox.showerror("Falha ao salvar a barra lateral", str(exc), parent=self)

    def _queue_sidebar_responsive_layout(self, _event=None):
        pending = getattr(self, "_sidebar_responsive_after_id", None)
        if pending:
            try:
                self.after_cancel(pending)
            except tk.TclError:
                pass
        self._sidebar_responsive_after_id = self.after(80, self._apply_sidebar_responsive_layout)

    def _apply_sidebar_responsive_layout(self):
        self._sidebar_responsive_after_id = None
        if not hasattr(self, "side") or not self.side.winfo_exists():
            return
        width = self.side.winfo_width()
        narrow = width < 260
        if hasattr(self, "brand"):
            self.brand.pack_configure(padx=14 if narrow else 20, pady=(18, 24) if narrow else (24, 34))
            self.brand_copy.pack_configure(padx=(8 if narrow else 12, 0))
            self.brand_title.configure(font=("Cyberpunk", 20 if narrow else 28))
        if hasattr(self, "privacy_text"):
            self.privacy_text.configure(wraplength=max(120, width - 54))
        if hasattr(self, "sidebar_album_control"):
            self.sidebar_album_control.configure(width=18 if narrow else 22)
        if width <= 1:
            width = self.sidebar_width
        for button in tuple(getattr(self, "_rounded_buttons", ())):
            try:
                if not button.winfo_exists():
                    continue
                ancestor = button.master
                while ancestor is not None and ancestor is not self.side:
                    ancestor = getattr(ancestor, "master", None)
                if ancestor is not self.side:
                    continue
                parent_width = button.master.winfo_width()
                if parent_width <= 1:
                    parent_width = width
                try:
                    padding = button.pack_info().get("padx", 0)
                    padding_parts = padding if isinstance(padding, (tuple, list)) else str(padding).split()
                    padding_total = sum(max(0, int(float(value))) for value in padding_parts)
                except (tk.TclError, TypeError, ValueError):
                    padding_total = 0
                button_width = max(110, parent_width - padding_total - 4)
                if button.winfo_reqwidth() != button_width:
                    button.configure(width=button_width)
                    button.refresh_theme()
            except tk.TclError:
                continue
        style = ttk.Style(self)
        button_font = ("Inter", 9 if narrow else 10, "bold")
        style.configure("Sidebar.TButton", font=button_font)
        style.configure("Sidebar.Active.TButton", font=button_font)

    @staticmethod
    def _make_window_nonactivating(window):
        if os.name != "nt":
            return
        user32 = ctypes.windll.user32
        get_window_long = getattr(user32, "GetWindowLongPtrW", user32.GetWindowLongW)
        set_window_long = getattr(user32, "SetWindowLongPtrW", user32.SetWindowLongW)
        get_window_long.argtypes = (ctypes.c_void_p, ctypes.c_int)
        get_window_long.restype = ctypes.c_ssize_t
        set_window_long.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t)
        set_window_long.restype = ctypes.c_ssize_t
        set_window_pos = user32.SetWindowPos
        set_window_pos.argtypes = (
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint,
        )
        set_window_pos.restype = ctypes.c_int

        hwnd = window.winfo_id()
        style_index = -20
        no_activate = 0x08000000
        style = get_window_long(hwnd, style_index)
        if style & no_activate:
            return
        ctypes.set_last_error(0)
        previous = set_window_long(hwnd, style_index, style | no_activate)
        if previous == 0 and ctypes.get_last_error():
            raise ctypes.WinError(ctypes.get_last_error())
        flags = 0x0001 | 0x0002 | 0x0004 | 0x0010 | 0x0020
        if not set_window_pos(hwnd, None, 0, 0, 0, 0, flags):
            raise ctypes.WinError(ctypes.get_last_error())

    def _apply_dark_titlebar(self, window=None):
        if os.name != "nt":
            return False
        window = window or self
        try:
            hwnd = window.winfo_id()
            user32 = ctypes.WinDLL("user32")
            dwmapi = ctypes.WinDLL("dwmapi")
            set_attribute = dwmapi.DwmSetWindowAttribute
            get_ancestor = user32.GetAncestor
        except (AttributeError, OSError, tk.TclError):
            return False
        get_ancestor.argtypes = (ctypes.c_void_p, ctypes.c_uint)
        get_ancestor.restype = ctypes.c_void_p
        hwnd = get_ancestor(ctypes.c_void_p(hwnd), 2)
        if not hwnd:
            return False
        set_attribute.argtypes = (ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint)
        set_attribute.restype = ctypes.c_long
        dark = ctypes.c_int(getattr(self, "theme", "dark") != "light")
        for attribute in (20, 19):
            result = set_attribute(
                hwnd,
                attribute,
                ctypes.byref(dark),
                ctypes.sizeof(dark),
            )
            if result == 0:
                return True
        return False

    def _restore_window_geometry(self):
        saved = self.store.setting("window_geometry", "")
        match = re.fullmatch(r"(\d+)x(\d+)([+-]\d+)([+-]\d+)", saved.strip())
        if match:
            width, height, left, top = map(int, match.groups())
            screen_width, screen_height = self.winfo_screenwidth(), self.winfo_screenheight()
            width = max(900, min(width, screen_width))
            height = max(620, min(height, screen_height))
            left = max(0, min(left, screen_width - width))
            top = max(0, min(top, screen_height - height))
            self.geometry(f"{width}x{height}+{left}+{top}")
        if self.store.setting("window_state", "normal") == "zoomed":
            self.state("zoomed")

    def _add_tooltip(self, widget, text, delay=450):
        state = {"after": None, "window": None}

        def hide(_event=None):
            if state["after"] is not None:
                try:
                    widget.after_cancel(state["after"])
                except tk.TclError:
                    pass
                state["after"] = None
            if state["window"] is not None:
                try:
                    state["window"].destroy()
                except tk.TclError:
                    pass
                state["window"] = None

        def show():
            state["after"] = None
            if not widget.winfo_exists():
                return
            tip = tk.Toplevel(self)
            tip.wm_overrideredirect(True)
            tip.configure(bg=LINE)
            label = tk.Label(tip, text=text, bg=PANEL, fg=INK, font=("Inter", 9), padx=10, pady=6)
            label.pack(padx=1, pady=1)
            tip.update_idletasks()
            x = widget.winfo_rootx() + max(0, (widget.winfo_width() - tip.winfo_reqwidth()) // 2)
            y = widget.winfo_rooty() + widget.winfo_height() + 7
            tip.geometry(f"+{x}+{y}")
            state["window"] = tip

        def schedule(_event=None):
            if state["after"] is None:
                state["after"] = widget.after(delay, show)

        widget.bind("<Enter>", schedule, add="+")
        widget.bind("<Leave>", hide, add="+")
        widget.bind("<ButtonPress-1>", hide, add="+")
        return state

    def _fade_in_window(self, window, frames=6):
        """Fade a small surface in asynchronously so it never blocks Tk."""
        try:
            window.attributes("-alpha", 0.0)
            if window.state() == "withdrawn":
                window.deiconify()
        except tk.TclError:
            try:
                window.deiconify()
            except tk.TclError:
                pass
            return

        def reveal(frame=1):
            try:
                if not window.winfo_exists():
                    return
                window.attributes("-alpha", min(1.0, frame / frames))
                if frame < frames:
                    window.after(14, reveal, frame + 1)
                else:
                    window.attributes("-alpha", 1.0)
            except tk.TclError:
                return

        window.after_idle(reveal)

    @staticmethod
    def _is_text_entry(widget):
        try:
            return widget.winfo_class() in {"Entry", "TEntry", "Text", "Listbox", "Spinbox", "TCombobox"}
        except (AttributeError, tk.TclError):
            return False

    def _shortcut_select_all(self, event):
        if event.widget.winfo_toplevel() is not self or self._is_text_entry(event.widget):
            return None
        self.select_all()
        return "break"

    def _shortcut_delete(self, event):
        if event.widget.winfo_toplevel() is not self or self._is_text_entry(event.widget):
            return None
        if not self.selected:
            return None
        if self.active_album is not None:
            self._remove_photos_from_current_album(list(self.selected))
        elif self.active_view == "photos":
            self._delete_photos_with_confirmation(list(self.selected))
        return "break"

    def _shortcut_escape(self, event):
        if event.widget.winfo_toplevel() is not self:
            return None
        if self.selected:
            self.clear_selection()
            return "break"
        return None

    @staticmethod
    def _load_private_fonts():
        """Load bundled app fonts privately for this process; never install system-wide."""
        if os.name != "nt":
            return []
        root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
        loaded = []
        try:
            for name in ("Inter-Regular.ttf", "Inter-Bold.ttf", "Cyberpunk-Regular.ttf"):
                path = root / "assets" / "fonts" / name
                if path.is_file() and ctypes.windll.gdi32.AddFontResourceExW(str(path), 0x10, 0):
                    loaded.append(str(path))
        except (AttributeError, OSError):
            return []
        return loaded

    def _build_styles(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TFrame", background=BG)
        style.configure("TLabel", background=BG, foreground=INK, font=("Inter", 10))
        style.configure("TButton", font=("Inter", 9, "bold"), padding=(14, 8), background=PANEL, foreground=INK, borderwidth=0)
        sidebar_font_size = 9 if getattr(self, "sidebar_width", 282) < 260 else 10
        style.configure("Sidebar.TButton", font=("Inter", sidebar_font_size, "bold"), padding=(13, 9), background=PANEL, foreground=INK, borderwidth=0, anchor="w")
        style.configure(
            "Sidebar.Active.TButton",
            font=("Inter", sidebar_font_size, "bold"),
            padding=(13, 9),
            background=self._blend_color(PANEL, self.accent, 0.22),
            foreground=INK,
            borderwidth=0,
            anchor="w",
        )
        style.map("Sidebar.TButton", foreground=[("disabled", MUTED)])
        style.map("Sidebar.Active.TButton", foreground=[("disabled", MUTED)])
        style.configure("Dropdown.TButton", padding=(11, 8))
        style.map(
            "TButton",
            background=[("active", self._blend_color(PANEL, INK, 0.12)), ("pressed", self._blend_color(PANEL, INK, 0.18))],
            foreground=[("disabled", MUTED)],
        )
        style.configure("Accent.TButton", background=GREEN, foreground="#101010")
        style.map("Accent.TButton", background=[("active", self._blend_color(GREEN, INK, 0.12))])
        try:
            if not getattr(self, "_rounded_button_images", None):
                self._rounded_button_images = {}
            for key, colors in {
                "regular": (PANEL, self._blend_color(PANEL, INK, 0.12), self._blend_color(PANEL, INK, 0.18)),
                "accent": (GREEN, self._blend_color(GREEN, INK, 0.12), self._blend_color(GREEN, INK, 0.18)),
                "sidebar": (
                    self._blend_color(PANEL, self.accent, 0.09),
                    self._blend_color(PANEL, self.accent, 0.15),
                    self._blend_color(PANEL, self.accent, 0.2),
                ),
                "sidebar_active": (
                    self._blend_color(PANEL, self.accent, 0.22),
                    self._blend_color(PANEL, self.accent, 0.3),
                    self._blend_color(PANEL, self.accent, 0.36),
                ),
            }.items():
                state_colors = (colors[0], colors[1], colors[2], self._blend_color(colors[0], BG, 0.42))
                images = self._rounded_button_images.get(key)
                if images is None:
                    images = [ImageTk.PhotoImage(Image.new("RGBA", (48, 36)), master=self) for _ in state_colors]
                    self._rounded_button_images[key] = images
                for image, color in zip(images, state_colors):
                    scale = 6
                    art = Image.new("RGBA", (48 * scale, 36 * scale), (0, 0, 0, 0))
                    drawing = ImageDraw.Draw(art)
                    drawing.rounded_rectangle(
                        (0, 0, 48 * scale - 1, 36 * scale - 1),
                        radius=13 * scale,
                        fill=color,
                    )
                    art = art.resize((48, 36), Image.Resampling.LANCZOS)
                    image.paste(art)
                element = f"Galeria.{key}.button.border"
                if element not in style.element_names():
                    style.element_create(
                        element,
                        "image",
                        images[0],
                        ("disabled", images[3]),
                        ("focus", images[0]),
                        ("pressed", images[2]),
                        ("active", images[1]),
                        border=(13, 13, 13, 13),
                        sticky="nsew",
                    )
                layout = [(element, {"sticky": "nsew", "children": [("Button.label", {"sticky": "nsew"})]})]
                if key in {"regular", "accent"}:
                    style.layout("TButton" if key == "regular" else "Accent.TButton", layout)
                    if key == "regular":
                        style.layout("Dropdown.TButton", layout)
                    compact_style = "Icon.TButton" if key == "regular" else "Accent.Icon.TButton"
                    style.configure(compact_style, padding=(3, 6))
                    style.layout(compact_style, layout)
                else:
                    style_name = "Sidebar.TButton" if key == "sidebar" else "Sidebar.Active.TButton"
                    style.layout(style_name, layout)
        except (tk.TclError, AttributeError, TypeError):
            pass
        style.configure("TCheckbutton", background=PANEL, foreground=INK, font=("Inter", 9))
        style.configure(
            "TCombobox",
            fieldbackground=PANEL,
            background=PANEL,
            foreground=INK,
            arrowcolor=INK,
            bordercolor=LINE,
            lightcolor=LINE,
            darkcolor=LINE,
            padding=7,
        )
        style.configure("TEntry", fieldbackground=PANEL, foreground=INK, insertcolor=INK, padding=8, borderwidth=0)
        style.configure(
            "TScale",
            background=BG,
            troughcolor=LINE,
            sliderlength=18,
            sliderthickness=16,
            borderwidth=0,
            lightcolor=self.accent,
            darkcolor=self.accent,
            foreground=self.accent,
        )
        style.configure(
            "Gallery.Horizontal.TProgressbar",
            background=self.accent,
            troughcolor=PANEL,
            bordercolor=PANEL,
            lightcolor=self.accent,
            darkcolor=self.accent,
            thickness=4,
        )
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", PANEL)],
            foreground=[("readonly", INK)],
            selectbackground=[("readonly", PANEL)],
            selectforeground=[("readonly", INK)],
        )
        try:
            if not getattr(self, "_rounded_combo_images", None):
                self._rounded_combo_images = [ImageTk.PhotoImage(Image.new("RGBA", (64, 36), BG), master=self) for _ in range(2)]
            for image, color in zip(self._rounded_combo_images, (PANEL, self._blend_color(PANEL, INK, 0.05))):
                scale = 6
                art = Image.new("RGBA", (64 * scale, 36 * scale), BG)
                ImageDraw.Draw(art).rounded_rectangle(
                    (0, 0, 64 * scale - 1, 36 * scale - 1),
                    radius=11 * scale,
                    fill=color,
                    outline=LINE,
                    width=scale,
                )
                art = art.resize((64, 36), Image.Resampling.LANCZOS)
                image.paste(art)
            combo_element = "Galeria.rounded.combobox.field"
            if combo_element not in style.element_names():
                style.element_create(
                    combo_element,
                    "image",
                    self._rounded_combo_images[0],
                    ("focus", self._rounded_combo_images[1]),
                    border=(11, 11, 11, 11),
                    sticky="nsew",
                )
            style.layout(
                "TCombobox",
                [
                    ("Combobox.downarrow", {"side": "right", "sticky": "ns"}),
                    (combo_element, {"sticky": "nswe", "children": [("Combobox.textarea", {"sticky": "nswe"})]}),
                ],
            )
            entry_element = "Galeria.rounded.entry.field"
            if entry_element not in style.element_names():
                style.element_create(
                    entry_element,
                    "image",
                    self._rounded_combo_images[0],
                    ("focus", self._rounded_combo_images[1]),
                    border=(11, 11, 11, 11),
                    sticky="nsew",
                )
            style.layout("TEntry", [(entry_element, {"sticky": "nswe", "children": [("Entry.textarea", {"sticky": "nswe"})]})])
        except (tk.TclError, AttributeError, TypeError):
            pass
        style.configure("Vertical.TScrollbar", background=PANEL, troughcolor=BG, bordercolor=BG, arrowcolor=MUTED)
        style.configure("Horizontal.TScrollbar", background=PANEL, troughcolor=BG, bordercolor=BG, arrowcolor=MUTED)

    @staticmethod
    def _blend_color(first, second, amount):
        def rgb(value):
            return tuple(int(value[index:index + 2], 16) for index in (1, 3, 5))

        a, b = rgb(first), rgb(second)
        return "#" + "".join(f"{round(x * (1 - amount) + y * amount):02x}" for x, y in zip(a, b))

    @staticmethod
    def _photo_count(count):
        return f"{count} foto" + ("s" if count != 1 else "")

    @staticmethod
    def _selected_count(count):
        return f"{count} foto selecionada" + ("s" if count != 1 else "")

    def _build_ui(self):
        self._scroll_canvases = {}
        self._auto_scrollbars = []
        self._scroll_motion_bound = False
        self._scroll_wheel_bound = False
        shell = tk.PanedWindow(
            self,
            orient="horizontal",
            bg=LINE,
            bd=0,
            relief="flat",
            sashwidth=6,
            sashpad=1,
            sashrelief="flat",
            showhandle=True,
            handlesize=12,
            handlepad=5,
            opaqueresize=True,
        )
        shell.pack(fill="both", expand=True)
        self.shell = shell
        shell.bind("<ButtonRelease-1>", self._persist_sidebar_sash, add="+")
        side = tk.Frame(shell, bg=PANEL, width=self.sidebar_width, highlightthickness=0, bd=0)
        self.side = side

        brand = tk.Frame(side, bg=PANEL)
        self.brand = brand
        brand.pack(fill="x", padx=20, pady=(24, 34))
        mark = tk.Label(brand, image=self._capri_logo_photo, bg=PANEL, bd=0)
        mark.pack(side="left", anchor="center")
        brand_copy = tk.Frame(brand, bg=PANEL)
        self.brand_copy = brand_copy
        brand_copy.pack(side="left", padx=12, fill="both", expand=True)
        self.brand_title = tk.Label(
            brand_copy,
            text="Capri",
            bg=PANEL,
            fg=INK,
            font=("Cyberpunk", 28),
            anchor="w",
        )
        self.brand_title.pack(fill="both", expand=True)

        self._side_label(side, "BIBLIOTECA")
        self.all_button = self._side_button(side, "Todas as fotos", self.show_all)
        self.unassigned_button = self._side_button(side, "Sem álbum", self.show_unassigned)
        self.album_browser_button = self._side_button(side, "Álbuns", self.show_albums)
        self._set_sidebar_active(self.all_button)
        self._side_label(side, "SEUS ÁLBUNS", 22)
        sidebar_modes = {
            "compact_grid": "Grade compacta",
            "grid": "Grade padrão",
            "compact_list": "Lista compacta",
            "list": "Lista padrão",
        }
        if self.sidebar_album_mode not in sidebar_modes:
            self.sidebar_album_mode = "compact_grid"
        self.sidebar_album_view = tk.StringVar(value=sidebar_modes[self.sidebar_album_mode])
        sidebar_view = self._rounded_dropdown(side, textvariable=self.sidebar_album_view, values=list(sidebar_modes.values()), width=22)
        self.sidebar_album_control = sidebar_view
        sidebar_view.pack(fill="x", padx=17, pady=(0, 7))
        sidebar_view.bind("<<ComboboxSelected>>", self.change_sidebar_album_view)
        album_wrap = tk.Frame(side, bg=PANEL)
        album_wrap.pack(fill="both", expand=True, padx=(10, 6), pady=(0, 2))
        self.album_canvas = tk.Canvas(album_wrap, bg=PANEL, highlightthickness=0)
        self.album_scroll = SlimScrollbar(album_wrap, command=self.album_canvas.yview)
        self.album_canvas._galeria_scrollbar = self.album_scroll
        self.album_scroll._galeria_area = self.album_canvas
        self.album_canvas.configure(yscrollcommand=self.album_scroll.set)
        self.album_canvas.pack(side="left", fill="both", expand=True)
        self.album_scroll.pack(side="right", fill="y")
        self.album_list = tk.Frame(self.album_canvas, bg=PANEL)
        self._album_list_window = self.album_canvas.create_window((0, 0), window=self.album_list, anchor="nw")
        self.album_list.bind("<Configure>", lambda _event: self.after_idle(self._update_album_list_scroll_region))
        self.album_canvas.bind("<Configure>", lambda event: self.album_canvas.itemconfigure(self._album_list_window, width=event.width))
        self.album_canvas.bind("<Configure>", self._queue_sidebar_layout_refresh, add="+")
        self._register_scroll_canvas(self, self.album_canvas)
        self._pill_button(side, "＋  Criar álbum", self.create_album, primary=True, width=236).pack(fill="x", padx=17, pady=(15, 5))
        self._pill_button(side, "Selecionar / excluir álbuns", self.delete_multiple_albums, width=236).pack(fill="x", padx=17, pady=4)
        privacy_note = tk.Frame(side, bg=PANEL)
        privacy_note.pack(side="bottom", fill="x", anchor="w", padx=19, pady=(0, 18))
        privacy_heading = tk.Frame(privacy_note, bg=PANEL)
        privacy_heading.pack(fill="x", anchor="w", pady=(0, 5))
        privacy_mark = tk.Canvas(privacy_heading, width=10, height=10, bg=PANEL, highlightthickness=0, bd=0)
        privacy_mark.pack(side="left", padx=(0, 7), pady=(1, 0))
        privacy_mark.create_oval(1, 1, 9, 9, fill=self.accent, outline="")
        tk.Label(privacy_heading, text="PRIVACIDADE", bg=PANEL, fg=INK, font=("Inter", 8, "bold"), anchor="w").pack(side="left")
        self.privacy_text = tk.Label(
            privacy_note,
            text="Suas fotos ficam neste computador.\nNenhum arquivo sai desta pasta.",
            bg=PANEL,
            fg=INK,
            justify="left",
            font=("Inter", 9),
            wraplength=228,
            anchor="w",
        )
        self.privacy_text.pack(fill="x", anchor="w", padx=(17, 0))

        main = tk.Frame(shell, bg=BG)
        self.main = main
        top = tk.Frame(main, bg=PANEL, height=76, highlightthickness=0, bd=0)
        top.pack(fill="x")
        top.pack_propagate(False)
        self.crumb = tk.Label(top, text="Biblioteca   /   Todas as fotos", bg=PANEL, fg=MUTED, font=("Inter", 10))
        self.crumb.pack(side="left", padx=32)
        self._pill_button(top, "+  Adicionar fotos", self.add_photos, width=145).pack(side="right", padx=(0, 24), pady=17)
        self._pill_button(top, "+  Adicionar pasta", self.add_folder, primary=True, width=172).pack(side="right", padx=10, pady=17)
        self._pill_button(top, "Aparência", self.preferences, width=116).pack(side="right", padx=10, pady=17)

        body = tk.Frame(main, bg=BG)
        body.pack(fill="both", expand=True, padx=38, pady=(30, 18))
        head = tk.Frame(body, bg=BG)
        head.pack(fill="x")
        head_left = tk.Frame(head, bg=BG)
        head_left.pack(side="left", fill="x", expand=True)
        self.heading = tk.Label(head_left, text="Todas as fotos", bg=BG, fg=INK, font=("Inter", 27, "bold"))
        self.heading.pack(anchor="w")
        subrow = tk.Frame(head_left, bg=BG)
        subrow.pack(anchor="w", pady=(5, 0))
        self.folder_label = tk.Label(subrow, text="Escolha uma pasta para começar.", bg=BG, fg=MUTED, font=("Inter", 10))
        self.folder_label.pack(side="left")
        self.activity_progress = ttk.Progressbar(subrow, mode="indeterminate", length=112, style="Gallery.Horizontal.TProgressbar")
        self.count = tk.Label(subrow, text="", bg=BG, fg=GREEN, font=("Inter", 10, "bold"))
        self.count.pack(side="left", padx=12)
        head_tools = tk.Frame(head, bg=BG)
        head_tools.pack(side="right", anchor="e")
        self.sort_var = tk.StringVar(value=self.sort_by)
        self.sort_control = self._rounded_dropdown(
            head_tools, textvariable=self.sort_var, values=["Nome", "Data modificada", "Tamanho"], width=17
        )
        self.sort_control.pack(side="right", padx=(8, 0))
        self.sort_control.bind("<<ComboboxSelected>>", self.change_sort)
        self.sort_direction = self._pill_button(head_tools, "↓" if self.sort_desc else "↑", self.toggle_sort_direction, width=42)
        self.sort_direction.pack(side="right", padx=6)
        self._add_tooltip(self.sort_direction, "Alternar ordem crescente ou decrescente")
        self.view_var = tk.StringVar(value=self.view_mode)
        mode_labels = {"grid": "Grid padrão", "compact_grid": "Grid compacto", "list": "Lista padrão", "compact_list": "Lista compacta"}
        self.view_var.set(mode_labels.get(self.view_mode, "Grid padrão"))
        self.view_control = self._rounded_dropdown(head_tools, textvariable=self.view_var, values=list(mode_labels.values()), width=18)
        self.view_control.pack(side="right")
        self.view_control.bind("<<ComboboxSelected>>", self.change_view_mode)
        toolbar = tk.Frame(body, bg=BG)
        toolbar.pack(fill="x", pady=(23, 14))
        self.toolbar = toolbar
        action_row = tk.Frame(toolbar, bg=BG)
        action_row.pack(fill="x")
        selection_group = tk.Frame(action_row, bg=BG)
        selection_group.pack(side="left", anchor="w")
        self.select_label = tk.Label(selection_group, text="0 selecionadas", bg=BG, fg=MUTED, font=("Inter", 9))
        self.select_label.pack(side="left", padx=(0, 12), pady=8)
        self.select_all_btn = self._pill_button(selection_group, "Selecionar todas", self.select_all, width=126)
        self.select_all_btn.pack(side="left", padx=(0, 6), pady=4)
        self.clear_btn = self._pill_button(selection_group, "Limpar", self.clear_selection, width=78)
        self.clear_btn.pack(side="left", pady=4)
        primary_tools = tk.Frame(action_row, bg=BG)
        primary_tools.pack(side="right")
        self._pill_button(primary_tools, "Criar álbum", self.create_album_from_selection, primary=True, width=132).pack(side="right", padx=(6, 0))
        tk.Frame(toolbar, bg=LINE, height=1).pack(fill="x", pady=(8, 8))
        utility_tools = tk.Frame(toolbar, bg=BG)
        utility_tools.pack(fill="x")
        self.add_to_album_btn = self._pill_button(utility_tools, "Adicionar a álbum", self.assign_selection, width=150)
        self.add_to_album_btn.pack(side="left", padx=(0, 8))
        self._pill_button(utility_tools, "Buscar por rosto", self.face_options, width=150).pack(side="left", padx=8)
        self._pill_button(utility_tools, "Encontrar duplicatas", self.find_duplicates, width=160).pack(side="left", padx=8)

        self.album_tools = tk.Frame(body, bg=BG)
        self.album_edit_btn = self._pill_button(self.album_tools, "Renomear álbum", self.rename_album, width=140)
        self.album_edit_btn.pack(side="left")
        self.album_remove_btn = self._pill_button(self.album_tools, "Devolver seleção à Biblioteca", self.remove_selection_from_album, width=214)
        self.album_remove_btn.pack(side="left", padx=7)
        self._pill_button(self.album_tools, "Excluir álbum…", self.delete_album, width=130).pack(side="left")
        self._pill_button(self.album_tools, "Definir capa…", self.set_album_cover, width=130).pack(side="left", padx=7)
        self.album_tools.pack_forget()

        grid_wrap = tk.Frame(body, bg=BG)
        self.grid_wrap = grid_wrap
        grid_wrap.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(grid_wrap, bg=BG, highlightthickness=0, yscrollincrement=16)
        self.scroll = SlimScrollbar(grid_wrap, command=self._scroll_canvas)
        self.canvas._galeria_scrollbar = self.scroll
        self.canvas.configure(yscrollcommand=self._set_scrollbar)
        self.scroll.pack(side="right", fill="y", padx=(8, 0))
        self.canvas.pack(side="left", fill="both", expand=True)
        self.grid_frame = tk.Frame(self.canvas, bg=BG)
        self.scroll._galeria_area = self.canvas
        self.canvas._galeria_scrollbar = self.scroll
        self.canvas._galeria_primary = True
        self.window_id = self.canvas.create_window((0, 0), window=self.grid_frame, anchor="nw")
        self.grid_frame.bind("<Configure>", lambda _e: self.after_idle(self._update_scroll_region))
        self.canvas.bind("<Configure>", self._resize_canvas)
        self._register_scroll_canvas(self, self.canvas)
        if not getattr(self, "_photo_drag_bindings", False):
            self.bind_all("<B1-Motion>", self._on_photo_drag_motion, add="+")
            self.bind_all("<ButtonRelease-1>", self._finish_photo_drag, add="+")
            self._photo_drag_bindings = True
        shell.add(side, minsize=220, stretch="never")
        shell.add(main, minsize=600, stretch="always")
        side.bind("<Configure>", self._queue_sidebar_responsive_layout, add="+")
        self.after_idle(self._apply_sidebar_responsive_layout)
        self.background_window = tk.Toplevel(self)
        self.background_window.overrideredirect(True)
        self.background_window.configure(bg=BG)
        self.background_window.withdraw()
        self.background_layer = tk.Label(self.background_window, bg=BG, bd=0, highlightthickness=0)
        self.background_layer.pack(fill="both", expand=True)
        if os.name == "nt":
            self._make_window_nonactivating(self.background_window)
        self.background_window.bind("<ButtonPress-1>", self._keep_background_behind, add="+")
        self.background_layer.bind("<ButtonPress-1>", self._keep_background_behind, add="+")
        self.after_idle(lambda: shell.sash_place(0, self.sidebar_width, 0))
        self.bind("<Configure>", self._queue_background_refresh, add="+")
        self.bind("<Map>", lambda _event: self._queue_background_refresh(), add="+")
        self.bind("<Unmap>", self._on_unmap, add="+")
        self.bind_all("<Control-a>", self._shortcut_select_all, add="+")
        self.bind_all("<Control-A>", self._shortcut_select_all, add="+")
        self.bind_all("<Delete>", self._shortcut_delete, add="+")
        self.bind_all("<Escape>", self._shortcut_escape, add="+")

    def _resize_canvas(self, event):
        self.canvas.itemconfigure(self.window_id, width=event.width)
        swap = getattr(self, "_grid_swap", None)
        if swap:
            self.canvas.itemconfigure(swap["old_id"], width=event.width)
        self.after_idle(self._update_scroll_region)
        previous_width = getattr(self, "_last_canvas_width", 0)
        if not previous_width or abs(event.width - previous_width) > 40:
            if getattr(self, "_resize_refresh_id", None):
                try:
                    self.after_cancel(self._resize_refresh_id)
                except tk.TclError:
                    pass
            self._resize_refresh_pending = True
            self._resize_refresh_id = self.after(320, self._refresh_after_resize)
        self._last_canvas_width = event.width

    def _keep_background_behind(self, _event=None):
        try:
            if self.background_window.winfo_exists():
                self.background_window.lower(self)
        except tk.TclError:
            pass

    def _queue_background_refresh(self, _event=None):
        if getattr(self, "_background_configure_id", None):
            try:
                self.after_cancel(self._background_configure_id)
            except tk.TclError:
                pass
        self._background_configure_id = self.after(140, self._apply_background)

    def _on_unmap(self, event=None):
        if event is not None and event.widget is not self:
            return
        if hasattr(self, "background_window"):
            try:
                if self.background_window.winfo_exists():
                    self.background_window.withdraw()
            except tk.TclError:
                pass
        self._wallpaper_enabled = False
        self._restore_wallpaper_surfaces()
        try:
            self.configure(bg=BG)
        except tk.TclError:
            pass

    def report_callback_exception(self, exc, value, tb):
        """Log callback failures so a broken action never silently hides the UI."""
        try:
            import traceback

            APP_DIR.mkdir(parents=True, exist_ok=True)
            with (APP_DIR / "galeria.log").open("a", encoding="utf-8") as log:
                log.write("\n--- erro de interface ---\n")
                log.writelines(traceback.format_exception(exc, value, tb))
        except OSError:
            pass
        if hasattr(self, "folder_label"):
            self._notify("Uma ação encontrou um erro. A janela continua aberta; detalhes em galeria.log.")

    def _notify(self, text, duration=5500):
        """Show routine results inline without triggering Windows message-box sounds."""
        if not hasattr(self, "folder_label"):
            return
        try:
            self._hide_progress()
            self.folder_label.configure(text=text)
            if self._status_after_id:
                self.after_cancel(self._status_after_id)
            self._status_after_id = self.after(duration, self._restore_page_status)
        except tk.TclError:
            pass

    def _show_progress(self, message, done=None, total=None):
        if not hasattr(self, "folder_label"):
            return
        self.folder_label.configure(text=message)
        bar = getattr(self, "activity_progress", None)
        if bar is None:
            return
        if not bar.winfo_manager():
            bar.pack(side="left", padx=(10, 0))
        bar.stop()
        if total is None:
            bar.configure(mode="indeterminate", maximum=100, value=0)
            bar.start(14)
        else:
            maximum = max(1, int(total))
            bar.configure(mode="determinate", maximum=maximum, value=max(0, min(maximum, int(done or 0))))

    def _hide_progress(self):
        bar = getattr(self, "activity_progress", None)
        if bar is None:
            return
        bar.stop()
        bar.pack_forget()

    def _info(self, _title, message, parent=None):
        """Use the in-window status line for routine notices instead of beeps."""
        self._notify(" ".join(str(message).split()))

    def _install_themed_dialogs(self):
        """Use in-app rounded dialogs instead of square native message boxes."""
        self._messagebox_originals = {
            name: getattr(messagebox, name) for name in ("askyesno", "showerror", "showwarning", "showinfo")
        }
        messagebox.askyesno = lambda title, message, **kw: self._confirm_dialog(title, message, kw.get("parent"))
        messagebox.showerror = lambda title, message, **kw: self._notice_dialog(title, message, kw.get("parent"), error=True)
        messagebox.showwarning = lambda title, message, **kw: self._notice_dialog(title, message, kw.get("parent"), warning=True)
        messagebox.showinfo = lambda title, message, **kw: self._notice_dialog(title, message, kw.get("parent"))

    def _dialog_window(self, title, parent, message):
        parent = parent if parent is not None and parent.winfo_exists() else self
        popup = tk.Toplevel(parent)
        popup.title(title)
        popup.configure(bg=BG)
        popup.transient(parent)
        popup.resizable(False, False)
        width = 500
        line_count = max(2, min(9, (len(str(message)) + 66) // 67))
        height = 170 + line_count * 20
        popup.geometry(f"{width}x{height}")
        popup.update_idletasks()
        px, py = parent.winfo_rootx(), parent.winfo_rooty()
        popup.geometry(
            f"{width}x{height}+{max(0, px + (parent.winfo_width() - width) // 2)}+{max(0, py + (parent.winfo_height() - height) // 2)}"
        )
        tk.Label(popup, text=title, bg=BG, fg=INK, font=("Inter", 15, "bold"), anchor="w").pack(fill="x", padx=22, pady=(20, 8))
        tk.Label(popup, text=str(message), bg=BG, fg=MUTED, font=("Inter", 10), justify="left", anchor="nw", wraplength=450).pack(
            fill="both", expand=True, padx=22, pady=(0, 12)
        )
        self._fade_in_window(popup)
        return popup

    def _confirm_dialog(self, title, message, parent=None):
        popup = self._dialog_window(title, parent, message)
        result = {"yes": False}
        footer = tk.Frame(popup, bg=BG)
        footer.pack(fill="x", padx=18, pady=(0, 18))

        def finish(value):
            result["yes"] = value
            popup.destroy()

        self._pill_button(footer, "Cancelar", lambda: finish(False), width=116).pack(side="right", padx=(8, 0))
        self._pill_button(footer, "Confirmar", lambda: finish(True), primary=True, width=128).pack(side="right")
        popup.protocol("WM_DELETE_WINDOW", lambda: finish(False))
        popup.bind("<Escape>", lambda _event: finish(False))
        popup.bind("<Return>", lambda _event: finish(True))
        popup.grab_set()
        popup.wait_window()
        return result["yes"]

    def _notice_dialog(self, title, message, parent=None, error=False, warning=False):
        popup = self._dialog_window(title, parent, message)
        button = "Entendi" if error or warning else "OK"
        self._pill_button(popup, button, popup.destroy, primary=True, width=112).pack(anchor="e", padx=22, pady=(0, 18))
        popup.bind("<Escape>", lambda _event: popup.destroy())
        popup.bind("<Return>", lambda _event: popup.destroy())
        popup.grab_set()
        popup.wait_window()

    def _ask_text(self, title, prompt, initial="", parent=None):
        parent = parent or self
        popup = tk.Toplevel(parent)
        popup.title(title)
        popup.configure(bg=BG)
        popup.transient(parent)
        popup.resizable(False, False)
        popup.geometry("500x220")
        popup.update_idletasks()
        popup.geometry(
            f"500x220+{max(0, parent.winfo_rootx() + (parent.winfo_width() - 500) // 2)}+{max(0, parent.winfo_rooty() + (parent.winfo_height() - 220) // 2)}"
        )
        tk.Label(popup, text=prompt, bg=BG, fg=INK, font=("Inter", 11, "bold"), wraplength=450, justify="left", anchor="w").pack(
            fill="x", padx=22, pady=(22, 12)
        )
        value = tk.StringVar(value=initial)
        entry = ttk.Entry(popup, textvariable=value, width=48)
        entry.pack(fill="x", padx=22, pady=(0, 18))
        footer = tk.Frame(popup, bg=BG)
        footer.pack(fill="x", padx=18, pady=(0, 18))
        result = {"value": None}

        def finish(save):
            if save:
                result["value"] = value.get()
            popup.destroy()

        self._pill_button(footer, "Cancelar", lambda: finish(False), width=116).pack(side="right", padx=(8, 0))
        self._pill_button(footer, "Confirmar", lambda: finish(True), primary=True, width=128).pack(side="right")
        popup.protocol("WM_DELETE_WINDOW", lambda: finish(False))
        popup.bind("<Escape>", lambda _event: finish(False))
        popup.bind("<Return>", lambda _event: finish(True))
        self._fade_in_window(popup)
        popup.grab_set()
        entry.focus_set()
        popup.wait_window()
        return result["value"]

    def _restore_page_status(self):
        self._status_after_id = None
        if not hasattr(self, "folder_label") or not self.winfo_exists():
            return
        self._hide_progress()
        paths = self._current_paths()
        folder_count = self.store.folder_count()
        message = (
            (Path(self.active_folder).name if self.active_folder else f"{folder_count} pastas conectadas · neste computador")
            if paths
            else "Adicione uma pasta ou fotos para preencher sua galeria."
        )
        self.folder_label.configure(text=message)

    def _refresh_after_resize(self):
        self._resize_refresh_pending = False
        self._resize_refresh_id = None
        if self.winfo_exists():
            self._refresh_photos()

    def _set_scrollbar(self, first, last):
        self.scroll.set(first, last)

    def _scroll_canvas(self, *args):
        self.canvas.yview(*args)
        self.after_idle(self._maybe_load_more_photos)

    def _update_scroll_region(self):
        if not self.canvas.winfo_exists():
            return
        if getattr(self, "_grid_swap", None):
            return
        width = max(1, self.canvas.winfo_width())
        self.canvas.itemconfigure(self.window_id, width=width)
        bounds = self.canvas.bbox(self.window_id)
        if bounds:
            self.canvas.configure(scrollregion=(0, 0, max(width, bounds[2]), max(self.canvas.winfo_height(), bounds[3])))
        else:
            self.canvas.configure(scrollregion=(0, 0, width, self.canvas.winfo_height()))

    def _discard_pending_grid_swap(self):
        swap = getattr(self, "_grid_swap", None)
        if not swap:
            return
        self._grid_swap = None
        try:
            self.canvas.delete(swap["new_id"])
            swap["new_frame"].destroy()
        except tk.TclError:
            pass
        self.grid_frame = swap["old_frame"]
        self.window_id = swap["old_id"]
        self.album_refs = swap["old_album_refs"]
        self.photo_refs = swap["old_photo_refs"]
        self.cards = swap["old_cards"]

    def _begin_grid_swap(self, generation):
        self._discard_pending_grid_swap()
        old_frame, old_id = self.grid_frame, self.window_id
        old_album_refs, old_photo_refs, old_cards = self.album_refs, self.photo_refs, self.cards
        frame = tk.Frame(self.canvas, bg=BG)
        frame.bind("<Configure>", lambda _event: self.after_idle(self._update_scroll_region))
        width = max(1, self.canvas.winfo_width())
        item_id = self.canvas.create_window((0, 0), window=frame, anchor="nw", width=width, state="hidden")
        self._grid_swap = {
            "generation": generation,
            "old_frame": old_frame,
            "old_id": old_id,
            "old_album_refs": old_album_refs,
            "old_photo_refs": old_photo_refs,
            "old_cards": old_cards,
            "new_frame": frame,
            "new_id": item_id,
        }
        self.grid_frame, self.window_id = frame, item_id
        self.album_refs, self.photo_refs, self.cards = [], [], {}
        if self._wallpaper_enabled:
            self._enable_wallpaper_surfaces((frame,))

    def _finish_grid_swap(self, generation):
        swap = getattr(self, "_grid_swap", None)
        if not swap or swap["generation"] != generation:
            return
        try:
            self.canvas.itemconfigure(swap["new_id"], state="normal", width=max(1, self.canvas.winfo_width()))
            self.canvas.delete(swap["old_id"])
            swap["old_frame"].destroy()
        except tk.TclError:
            pass
        self._grid_swap = None
        self._update_scroll_region()

    def _on_mousewheel(self, event):
        try:
            widget = self.nametowidget(str(event.widget))
        except (KeyError, tk.TclError, AttributeError, TypeError):
            try:
                widget = self.winfo_containing(event.x_root, event.y_root)
            except (tk.TclError, AttributeError):
                widget = None
        if widget is None:
            return None
        try:
            owner = widget.winfo_toplevel()
        except (tk.TclError, AttributeError):
            return None
        if owner is getattr(self, "background_window", None):
            owner = self
        canvases = getattr(self, "_scroll_canvases", {}).get(owner, ())
        canvas = None
        for candidate in canvases:
            area = getattr(candidate, "_galeria_area", candidate)
            node = widget
            while node is not None:
                if node is area:
                    canvas = candidate
                    break
                node = getattr(node, "master", None)
            if canvas is not None:
                break
            if widget is getattr(candidate, "_galeria_scrollbar", None):
                canvas = candidate
                break
        if canvas is None:
            canvas = next((item for item in canvases if getattr(item, "_galeria_primary", False)), canvases[0] if canvases else None)
        if canvas is None or not canvas.winfo_exists():
            return None
        if getattr(event, "num", None) == 4:
            distance = -150
        elif getattr(event, "num", None) == 5:
            distance = 150
        elif event.delta:
            distance = -float(event.delta) * 1.25
        else:
            return None
        self._animate_scroll(canvas, distance)
        return "break"

    def _animate_scroll(self, canvas, distance):
        if not hasattr(self, "_scroll_animation_jobs"):
            self._scroll_animation_jobs = {}
        previous = self._scroll_animation_jobs.pop(canvas, None)
        if previous is not None:
            try:
                canvas.after_cancel(previous["after"])
            except tk.TclError:
                pass
        bounds = canvas.bbox("all")
        if not bounds:
            return
        extent = max(1, bounds[3] - bounds[1])
        viewport = max(1, canvas.winfo_height())
        start = canvas.yview()[0] * extent
        proposed = (previous["target"] + distance) if previous else (start + distance)
        target = max(0.0, min(max(0.0, extent - viewport), proposed))
        if abs(target - start) < 1:
            if canvas is getattr(self, "canvas", None):
                self._maybe_load_more_photos()
            return
        state = {"step": 0, "steps": 9, "start": start, "target": target, "extent": extent, "after": None}

        def advance():
            if not canvas.winfo_exists():
                self._scroll_animation_jobs.pop(canvas, None)
                return
            state["step"] += 1
            progress = min(1.0, state["step"] / state["steps"])
            eased = 1 - (1 - progress) ** 3
            position = state["start"] + (state["target"] - state["start"]) * eased
            canvas.yview_moveto(position / state["extent"])
            if progress < 1:
                state["after"] = canvas.after(16, advance)
            else:
                self._scroll_animation_jobs.pop(canvas, None)
                if canvas is getattr(self, "canvas", None):
                    self._maybe_load_more_photos()

        state["after"] = canvas.after(0, advance)
        self._scroll_animation_jobs[canvas] = state

    def _maybe_load_more_photos(self):
        state = getattr(self, "_photo_render_state", None)
        if (
            self.active_view == "albums"
            or not state
            or state["generation"] != self._photo_render_generation
            or state["index"] < len(state["paths"])
            or len(state["paths"]) >= len(state["all_paths"])
        ):
            return
        try:
            if self.canvas.yview()[1] < 0.82:
                return
        except tk.TclError:
            return
        old_count = len(state["paths"])
        new_count = min(len(state["all_paths"]), old_count + 100)
        new_paths = state["all_paths"][old_count:new_count]
        if not new_paths:
            return
        state["paths"] = state["all_paths"][:new_count]
        state["memberships"].update(self.store.membership_map(new_paths))
        self.visible_limit = new_count
        generation = state["generation"]
        self._photo_render_after_id = self.after(1, lambda current=generation: self._render_photo_batch(current))

    def _register_scroll_canvas(self, window, canvas):
        """Register one scroll area and reveal its thumb over the whole viewport."""
        if not hasattr(self, "_scroll_canvases"):
            self._scroll_canvases = {}
        targets = self._scroll_canvases.setdefault(window, [])
        if canvas not in targets:
            targets.append(canvas)
        bar = getattr(canvas, "_galeria_scrollbar", None)
        if bar is not None:
            bar._galeria_canvas = canvas
            if not hasattr(self, "_auto_scrollbars"):
                self._auto_scrollbars = []
            if bar not in self._auto_scrollbars:
                self._auto_scrollbars.append(bar)
            if not getattr(self, "_scroll_motion_bound", False):
                self.bind_all("<Motion>", self._on_scroll_hover, add="+")
                self._scroll_motion_bound = True
        if not getattr(self, "_scroll_wheel_bound", False):
            self.bind_all("<MouseWheel>", self._on_mousewheel, add="+")
            self.bind_all("<Button-4>", self._on_mousewheel, add="+")
            self.bind_all("<Button-5>", self._on_mousewheel, add="+")
            self._scroll_wheel_bound = True
        window.bind(
            "<Destroy>",
            lambda event, owner=window: self._unregister_scroll_canvas(owner) if event.widget is owner else None,
            add="+",
        )

    def _unregister_scroll_canvas(self, owner):
        canvases = self._scroll_canvases.pop(owner, ())
        for canvas in canvases:
            getattr(self, "_scroll_wheel_remainders", {}).pop(canvas, None)
            pending = getattr(self, "_scroll_animation_jobs", {}).pop(canvas, None)
            if pending:
                try:
                    canvas.after_cancel(pending["after"])
                except tk.TclError:
                    pass
            bar = getattr(canvas, "_galeria_scrollbar", None)
            if bar in getattr(self, "_auto_scrollbars", []):
                self._auto_scrollbars.remove(bar)

    def _on_scroll_hover(self, event):
        try:
            widget = self.nametowidget(str(event.widget))
            owner = widget.winfo_toplevel()
        except (KeyError, tk.TclError, AttributeError, TypeError):
            return
        for bar in tuple(getattr(self, "_auto_scrollbars", ())):
            target = getattr(bar, "_galeria_area", None)
            belongs = False
            if event.widget is bar:
                belongs = True
            if target is not None and target.winfo_toplevel() is owner:
                node = widget
                while node is not None:
                    if node is target:
                        belongs = True
                        break
                    node = getattr(node, "master", None)
            if belongs:
                bar.show()
            elif getattr(bar, "visible", False) and not getattr(bar, "_hide_id", None):
                bar.hide_later()

    def _pill_button(self, parent, text, command, primary=False, width=140, state="normal", style=None):
        button = RoundedButton(
            parent,
            text=text,
            command=command,
            width=width,
            primary=primary,
            state=state,
            style=style,
        )
        if getattr(self, "_wallpaper_enabled", False) and button.winfo_toplevel() is self:
            self._enable_wallpaper_surfaces((button,))
        return button

    def _refresh_rounded_buttons(self):
        for button in tuple(getattr(self, "_rounded_buttons", ())):
            try:
                if button.winfo_exists():
                    button.refresh_theme()
            except tk.TclError:
                continue
        for widget in tuple(getattr(self, "_wallpaper_alpha_widgets", ())):
            try:
                if widget.winfo_exists():
                    widget.refresh_wallpaper_surface()
            except tk.TclError:
                continue

    def _wallpaper_photo_image(self, image, master=None):
        if self._wallpaper_transparent:
            image = _hard_alpha(image)
        return ImageTk.PhotoImage(image, master=master or self)

    def _track_wallpaper_thumbnail(self, widget, image, scope):
        widget._galeria_thumbnail_source = image
        widget._galeria_thumbnail_scope = scope
        self._wallpaper_thumbnail_widgets.add(widget)

    def _refresh_wallpaper_thumbnails(self):
        references_by_scope = {
            "photo_grid": self.photo_refs,
            "album_browser": self.album_refs,
            "sidebar": self.sidebar_album_refs,
            "review": self.review_refs,
        }
        for widget in tuple(getattr(self, "_wallpaper_thumbnail_widgets", ())):
            try:
                if not widget.winfo_exists():
                    continue
                fade_after_id = getattr(widget, "_galeria_fade_after_id", None)
                if fade_after_id:
                    try:
                        widget.after_cancel(fade_after_id)
                    except tk.TclError:
                        pass
                    widget._galeria_fade_after_id = None
                source = getattr(widget, "_galeria_thumbnail_source", None)
                scope = getattr(widget, "_galeria_thumbnail_scope", None)
                if source is None or scope is None:
                    continue
                old_photo = getattr(widget, "_galeria_photo_ref", None)
                for references in references_by_scope.values():
                    if old_photo is not None:
                        while old_photo in references:
                            references.remove(old_photo)
                photo = self._wallpaper_photo_image(source, master=self)
                if isinstance(widget, ttk.Button):
                    widget.configure(image=photo, compound="left")
                    widget._galeria_photo_ref = photo
                else:
                    self._set_thumbnail_widget_photo(widget, photo, scope)
                widget._galeria_photo_ref = photo
                references_by_scope.get(scope, self.review_refs).append(photo)
            except tk.TclError:
                continue

    def _rounded_dropdown(self, parent, *, textvariable, values, width=18, state="readonly"):
        return RoundedControl(parent, kind="dropdown", textvariable=textvariable, values=values, width=width, state=state)

    @staticmethod
    def _round_rect(canvas, x1, y1, x2, y2, r, fill, outline):
        points = [
            x1 + r, y1, x2 - r, y1, x2, y1,
            x2, y1 + r, x2, y2 - r, x2, y2,
            x2 - r, y2, x1 + r, y2, x1, y2,
            x1, y2 - r, x1, y1 + r, x1, y1,
        ]
        return canvas.create_polygon(points, smooth=True, fill=fill, outline=outline, splinesteps=24)

    def _side_label(self, parent, text, top=0):
        tk.Label(parent, text=text, bg=PANEL, fg=MUTED, font=("Inter", 8, "bold")).pack(anchor="w", padx=20, pady=(top, 8))

    def _side_button(self, parent, text, command):
        b = self._pill_button(parent, text, command, width=236, style="Sidebar.TButton")
        b.pack(fill="x", padx=13, pady=3)
        return b

    def _set_sidebar_active(self, button):
        for item in (
            getattr(self, "all_button", None),
            getattr(self, "unassigned_button", None),
            getattr(self, "album_browser_button", None),
        ):
            if item is not None:
                item.configure(style="Sidebar.Active.TButton" if item is button else "Sidebar.TButton")

    def refresh_all(self):
        self._refresh_albums()
        self._refresh_photos()

    def change_sidebar_album_view(self, _event=None):
        modes = {
            "Grade compacta": "compact_grid",
            "Grade padrão": "grid",
            "Lista compacta": "compact_list",
            "Lista padrão": "list",
        }
        self.sidebar_album_mode = modes.get(self.sidebar_album_view.get(), "compact_grid")
        self.store.set_setting("sidebar_album_view", self.sidebar_album_mode)
        self.album_canvas.yview_moveto(0)
        self._refresh_albums()

    def _queue_sidebar_layout_refresh(self, _event=None):
        if not self.sidebar_album_mode.endswith("grid"):
            return
        pending = getattr(self, "_sidebar_layout_after_id", None)
        if pending:
            try:
                self.after_cancel(pending)
            except tk.TclError:
                pass
        self._sidebar_layout_after_id = self.after(260, self._refresh_albums)

    def _update_album_list_scroll_region(self):
        if not self.album_canvas.winfo_exists():
            return
        bounds = self.album_canvas.bbox(self._album_list_window)
        if bounds:
            self.album_canvas.configure(
                scrollregion=(0, 0, max(self.album_canvas.winfo_width(), bounds[2]), max(self.album_canvas.winfo_height(), bounds[3]))
            )
        else:
            self.album_canvas.configure(scrollregion=(0, 0, self.album_canvas.winfo_width(), self.album_canvas.winfo_height()))

    def _resolve_album_covers(self, albums):
        """Prefer an explicitly selected cover, then a valid photo from that album."""
        covers = {}
        for album_id, _name, _face_group, _count, cover, _created in albums:
            if cover:
                try:
                    candidate = Path(cover).expanduser()
                    if candidate.is_file():
                        covers[album_id] = _safe_resolve(candidate)
                except OSError:
                    pass
        for album_id, path in self.store.album_photo_paths():
            if album_id in covers:
                continue
            try:
                candidate = Path(path)
                if candidate.is_file():
                    covers[album_id] = _safe_resolve(candidate)
            except OSError:
                continue
        return covers

    def _refresh_albums(self):
        self._cancel_thumbnail_scope("sidebar")
        self._sidebar_render_generation = getattr(self, "_sidebar_render_generation", 0) + 1
        sidebar_generation = self._sidebar_render_generation
        for w in self.album_list.winfo_children():
            w.destroy()
        self.sidebar_album_refs.clear()
        albums = self.store.albums()
        covers = self._resolve_album_covers(albums)
        mode = self.sidebar_album_mode
        compact = mode in ("compact_grid", "compact_list")
        is_grid = mode in ("compact_grid", "grid")
        if is_grid:
            available = max(1, self.album_canvas.winfo_width())
            gutter = 8
            min_cell = 76 if compact else 108
            columns = max(1, min(3, (available + gutter) // (min_cell + gutter)))
            cell_width = max(1, (available - gutter * (columns - 1)) // columns)
            thumb_size = max(40, min(96 if not compact else 72, cell_width - 24))
            previous_columns = getattr(self, "_sidebar_album_columns", 0)
            for column in range(max(previous_columns, columns)):
                self.album_list.grid_columnconfigure(
                    column,
                    weight=1 if column < columns else 0,
                    uniform="sidebar-albums" if column < columns else "",
                )
            self._sidebar_album_columns = columns
            for index, album in enumerate(albums):
                album_id, name, _face, _count, _cover, _created = album
                active = album_id == self.active_album
                tile_bg = PANEL
                tile = tk.Frame(
                    self.album_list,
                    bg=tile_bg,
                    padx=6,
                    pady=6,
                    highlightthickness=0,
                )
                tile.grid(row=index // columns, column=index % columns, sticky="nsew", padx=4, pady=4)
                self._set_album_drop_target(tile, album_id, name)
                path = covers.get(album_id)
                picture = tk.Frame(tile, bg=tile_bg, width=thumb_size, height=thumb_size)
                picture.pack(anchor="center")
                self._set_album_drop_target(picture, album_id, name)
                picture.pack_propagate(False)
                image_label = tk.Label(picture, text="…", bg=tile_bg, fg=MUTED, font=("Inter", 16, "bold"), cursor="hand2", bd=0)
                image_label.place(relx=0, rely=0, relwidth=1, relheight=1)
                self._set_album_drop_target(image_label, album_id, name)
                if path:
                    self._request_thumbnail(path, (thumb_size, thumb_size), image_label, "sidebar", sidebar_generation)
                else:
                    image = self._album_art(name, (thumb_size, thumb_size))
                    self._track_wallpaper_thumbnail(image_label, image, "sidebar")
                    ref = self._wallpaper_photo_image(image, master=self)
                    image_label.configure(image=ref)
                    image_label._galeria_photo_ref = ref
                    self.sidebar_album_refs.append(ref)
                max_name_chars = max(5, (cell_width - 16) // 7)
                shown_name = name if len(name) <= max_name_chars else name[:max_name_chars - 1] + "…"
                name_overlay = tk.Label(
                    picture,
                    text=shown_name,
                    bg="#11141b",
                    fg="#ffffff",
                    font=("Inter", 8 if compact else 9, "bold"),
                    anchor="w",
                    padx=5,
                    cursor="hand2",
                )
                name_overlay.place_forget()
                self._set_album_drop_target(name_overlay, album_id, name)
                hover_state = {"hide_after_id": None}

                def show_album_name(_event=None, overlay=name_overlay, state=hover_state, owner=tile):
                    pending = state["hide_after_id"]
                    if pending:
                        try:
                            owner.after_cancel(pending)
                        except tk.TclError:
                            pass
                        state["hide_after_id"] = None
                    overlay.place(x=0, rely=1, relwidth=1, anchor="sw")
                    overlay.lift()

                def schedule_album_name_hide(_event=None, overlay=name_overlay, state=hover_state, owner=tile):
                    def hide_if_outside():
                        state["hide_after_id"] = None
                        try:
                            pointer = self.winfo_containing(self.winfo_pointerx(), self.winfo_pointery())
                            while pointer is not None:
                                if pointer is owner:
                                    return
                                pointer = getattr(pointer, "master", None)
                            overlay.place_forget()
                        except tk.TclError:
                            return

                    pending = state["hide_after_id"]
                    if pending:
                        try:
                            owner.after_cancel(pending)
                        except tk.TclError:
                            pass
                    state["hide_after_id"] = owner.after_idle(hide_if_outside)

                command = lambda i=album_id, n=name: self.open_album(i, n)
                for widget in (tile, picture, image_label, name_overlay):
                    widget.bind("<Button-1>", lambda _event, fn=command: fn())
                    widget.bind("<Enter>", show_album_name, add="+")
                    widget.bind("<Leave>", schedule_album_name_hide, add="+")
        else:
            thumb_size = 28 if compact else 38
            for album_id, name, _face, count, _cover, _created in albums:
                preview = covers.get(album_id)
                button = self._pill_button(
                    self.album_list,
                    f"{name}   {count}",
                    lambda i=album_id, n=name: self.open_album(i, n),
                    width=max(120, min(236, len(name) * 7 + 62)),
                    style="Sidebar.Active.TButton" if album_id == self.active_album else "Sidebar.TButton",
                )
                self._set_album_drop_target(button, album_id, name)
                button.pack(fill="x", pady=1)
                if preview:
                    self._request_thumbnail(preview, (thumb_size, thumb_size), button, "sidebar", sidebar_generation)
                else:
                    image = self._album_art(name, (thumb_size, thumb_size))
                    self._track_wallpaper_thumbnail(button, image, "sidebar")
                    ref = self._wallpaper_photo_image(image, master=self)
                    button.configure(image=ref, compound="left")
                    button._galeria_photo_ref = ref
                    self.sidebar_album_refs.append(ref)
        total, unassigned = self.store.photo_counts()
        self.unassigned_button.configure(text=f"Sem álbum   {unassigned}")
        self.all_button.configure(text=f"Todas as fotos   {total}")
        self.after_idle(self._update_album_list_scroll_region)
        if getattr(self, "_wallpaper_enabled", False):
            self._enable_wallpaper_surfaces((self.album_list,))

    def _current_paths(self):
        if self.active_album is not None:
            paths = self.store.photos(self.active_folder, self.active_album)
        else:
            paths = self.store.photos(self.active_folder, unassigned=self.active_filter == "unassigned")
        if self.sort_by in ("Data modificada", "Tamanho"):
            attribute = "st_mtime" if self.sort_by == "Data modificada" else "st_size"

            def file_metric(path):
                try:
                    return getattr(Path(path).stat(), attribute)
                except OSError:
                    return 0

            paths.sort(key=file_metric, reverse=self.sort_desc)
        else:
            paths.sort(key=lambda p: Path(p).name.casefold(), reverse=self.sort_desc)
        return paths

    def change_sort(self, _event=None):
        self.sort_by = self.sort_var.get()
        self.store.set_setting("sort_by", self.sort_by)
        self.canvas.yview_moveto(0)
        self._refresh_photos()

    def toggle_sort_direction(self):
        self.sort_desc = not self.sort_desc
        self.store.set_setting("sort_desc", "1" if self.sort_desc else "0")
        self.sort_direction.configure(text="↓" if self.sort_desc else "↑")
        self.canvas.yview_moveto(0)
        self._refresh_photos()

    def change_view_mode(self, _event=None):
        labels = {"Grid padrão": "grid", "Grid compacto": "compact_grid", "Lista padrão": "list", "Lista compacta": "compact_list"}
        self.view_mode = labels.get(self.view_var.get(), "grid")
        self.store.set_setting("view_mode", self.view_mode)
        self.canvas.yview_moveto(0)
        self._refresh_photos()

    def show_albums(self):
        self.active_view = "albums"
        self.active_album = None
        self.active_folder = None
        self.selected.clear()
        self.canvas.yview_moveto(0)
        self.heading.configure(text="Álbuns")
        self.crumb.configure(text="Biblioteca   /   Álbuns")
        self._set_sidebar_active(self.album_browser_button)
        self._refresh_photos()

    def _make_thumbnail(self, path, size):
        size = tuple(int(value) for value in size)
        try:
            stat = Path(path).stat()
            signature = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            signature = (0, 0)
        corner_radius = self.photo_corner_radius
        key = (str(path), size, signature, corner_radius)
        with self._thumb_cache_lock:
            image = self.thumb_cache.get(key)
            if image is not None:
                return image
        source = self._open_thumbnail(path, size)
        scale = 6
        if source is None:
            width, height = size
            source = Image.new(
                "RGB",
                (max(1, width * scale), max(1, height * scale)),
                self._blend_color(BG, PANEL, 0.72),
            )
            draw = ImageDraw.Draw(source)
            color = self._blend_color(MUTED, BG, 0.25)
            cx, cy = width * scale // 2, height * scale // 2
            radius = max(9, min(width, height) // 9) * scale
            line_width = max(2, width // 70) * scale
            draw.rounded_rectangle(
                (cx - radius, cy - radius, cx + radius, cy + radius),
                radius=max(4 * scale, radius // 4),
                outline=color,
                width=line_width,
            )
            draw.ellipse(
                (cx - radius // 2, cy - radius // 2, cx + radius // 2, cy + radius // 2),
                outline=color,
                width=line_width,
            )
            draw.ellipse((cx + radius // 2, cy - radius // 2, cx + radius * 3 // 4, cy - radius // 4), fill=color)
            source = source.resize(size, Image.Resampling.LANCZOS)
        mask = self._thumbnail_mask(size, scale, corner_radius)
        source.putalpha(mask)
        with self._thumb_cache_lock:
            image = self.thumb_cache.get(key)
            if image is not None:
                return image
            self.thumb_cache[key] = source
            while len(self.thumb_cache) > 240:
                self.thumb_cache.pop(next(iter(self.thumb_cache)))
        return source

    def _thumbnail_mask(self, size, scale=6, corner_radius=None):
        corner_radius = self.photo_corner_radius if corner_radius is None else self._normalize_photo_corner_radius(corner_radius)
        key = (tuple(size), scale, corner_radius)
        with self._thumb_cache_lock:
            mask = self.thumb_mask_cache.get(key)
            if mask is not None:
                return mask
        dimensions = (size[0] * scale, size[1] * scale)
        if corner_radius == 0:
            mask = Image.new("L", dimensions, 255)
        else:
            mask = Image.new("L", dimensions, 0)
            scaled_radius = min(
                dimensions[0] // 2,
                dimensions[1] // 2,
                round(corner_radius * min(size) / 150 * scale),
            )
            ImageDraw.Draw(mask).rounded_rectangle(
                (0, 0, dimensions[0] - 1, dimensions[1] - 1),
                radius=scaled_radius,
                fill=255,
            )
        mask = mask.resize(size, Image.Resampling.LANCZOS)
        with self._thumb_cache_lock:
            cached = self.thumb_mask_cache.get(key)
            if cached is not None:
                return cached
            self.thumb_mask_cache[key] = mask
            while len(self.thumb_mask_cache) > 16:
                self.thumb_mask_cache.pop(next(iter(self.thumb_mask_cache)))
            return mask

    def _open_thumbnail(self, path, size):
        """Decode JPEGs near thumbnail size to avoid loading huge originals in full."""
        try:
            with _FILE_IO_LOCK:
                with Image.open(path) as opened:
                    try:
                        opened.draft("RGB", (max(1, size[0] * 2), max(1, size[1] * 2)))
                    except (AttributeError, OSError, ValueError):
                        pass
                    image = ImageOps.exif_transpose(opened).convert("RGB")
            return ImageOps.fit(image, size, method=Image.Resampling.LANCZOS)
        except Exception:
            image = self._open_image(path)
            return ImageOps.fit(image, size, method=Image.Resampling.LANCZOS) if image is not None else None

    @staticmethod
    def _decode_preview_image(path, size):
        """Load a small aspect-preserving source for the appearance preview."""
        try:
            with _FILE_IO_LOCK:
                with Image.open(path) as opened:
                    try:
                        opened.draft("RGB", size)
                    except (AttributeError, OSError, ValueError):
                        pass
                    image = ImageOps.exif_transpose(opened).convert("RGB")
            image.thumbnail(size, Image.Resampling.LANCZOS)
            return image.copy()
        except Exception:
            return None

    def _cancel_thumbnail_scope(self, scope):
        with self._thumbnail_lock:
            futures = tuple(self._thumbnail_futures.pop(scope, ()))
        for future in futures:
            future.cancel()

    def _request_thumbnail(self, path, size, widget, scope, generation=None, *, fade_in=False, background_color=BG):
        size = tuple(int(value) for value in size)
        try:
            future = self.thumbnail_executor.submit(self._make_thumbnail, str(path), size)
        except RuntimeError:
            return
        with self._thumbnail_lock:
            self._thumbnail_futures.setdefault(scope, set()).add(future)

        def deliver(done):
            with self._thumbnail_lock:
                active = self._thumbnail_futures.get(scope)
                if active is not None:
                    active.discard(done)
                    if not active:
                        self._thumbnail_futures.pop(scope, None)
            if done.cancelled():
                return
            try:
                image = done.result()
            except Exception:
                image = None
            frames = None
            if fade_in and image is not None:
                try:
                    background = Image.new("RGB", image.size, background_color)
                    source = image.convert("RGB")
                    source_alpha = image.getchannel("A")
                    fade_frames = []
                    for alpha in (0.25, 0.5, 0.75, 1.0):
                        frame = Image.blend(background, source, alpha).convert("RGBA")
                        frame.putalpha(source_alpha)
                        fade_frames.append(frame)
                    frames = tuple(fade_frames)
                except (OSError, ValueError):
                    frames = None
            self.worker_queue.put(("thumbnail", scope, generation, widget, image, frames))

        future.add_done_callback(deliver)

    @staticmethod
    def _open_image(path):
        """Decode a local image with EXIF orientation and a Unicode-safe fallback."""
        if not path or Image is None:
            return None
        try:
            with _FILE_IO_LOCK:
                with Image.open(path) as opened:
                    return ImageOps.exif_transpose(opened).convert("RGB")
        except Exception:
            try:
                import cv2
                import numpy as np

                with _FILE_IO_LOCK:
                    raw = np.fromfile(str(path), dtype=np.uint8)
                decoded = cv2.imdecode(raw, cv2.IMREAD_COLOR)
                if decoded is not None:
                    return Image.fromarray(cv2.cvtColor(decoded, cv2.COLOR_BGR2RGB))
            except Exception:
                pass
        return None

    def _start_missing_photo_recovery(self):
        """Reconnect uniquely named missing files inside already connected roots."""
        try:
            with self.store._lock:
                rows = self.store.db.execute("SELECT path,folder FROM photos").fetchall()
            missing = [(path, folder) for path, folder in rows if not _safe_is_file(path)]
        except sqlite3.Error:
            return
        if not missing:
            return
        names = {Path(path).name.casefold() for path, _folder in missing}
        roots = {str(Path(folder)) for _path, folder in missing if folder}
        destination = self.store.setting("album_destination", "")
        if destination:
            roots.add(destination)
        roots = [Path(root) for root in roots if Path(root).is_dir()]
        roots.sort(key=lambda item: len(item.parts))
        scan_roots = []
        for root in roots:
            try:
                if not any(root == existing or existing in root.parents for existing in scan_roots):
                    scan_roots.append(root)
            except OSError:
                continue
        if not scan_roots:
            return

        def scan():
            found = {}
            ambiguous = set()
            for root in scan_roots:
                for current, dirs, files in os.walk(root, onerror=lambda _error: None):
                    dirs[:] = [name for name in dirs if name.casefold() not in {"$recycle.bin", "system volume information", "node_modules", ".git"}]
                    for filename in files:
                        key = filename.casefold()
                        if key not in names or Path(filename).suffix.casefold() not in IMAGE_EXTENSIONS:
                            continue
                        candidate = _safe_resolve(Path(current, filename))
                        if key in found and os.path.normcase(found[key]) != os.path.normcase(candidate):
                            ambiguous.add(key)
                        else:
                            found[key] = candidate
            for key in ambiguous:
                found.pop(key, None)
            self.worker_queue.put(("relink", [(old, found[Path(old).name.casefold()]) for old, _folder in missing if Path(old).name.casefold() in found]))

        threading.Thread(target=scan, name="Galeria-localiza-fotos", daemon=True).start()

    def _render_album_browser(self):
        self._cancel_thumbnail_scope("album_browser")
        self._album_browser_generation = getattr(self, "_album_browser_generation", 0) + 1
        album_generation = self._album_browser_generation
        albums = list(self.store.albums())
        covers = self._resolve_album_covers(albums)
        self.folder_label.configure(text="Abra um álbum para conferir as fotos.")
        if self.sort_by == "Nome":
            albums.sort(key=lambda a: a[1].casefold(), reverse=self.sort_desc)
        elif self.sort_by == "Data modificada":
            albums.sort(key=lambda a: a[5] or "", reverse=not self.sort_desc)
        elif self.sort_by == "Tamanho":
            sizes = {album[0]: 0 for album in albums}
            for album_id, path in self.store.album_photo_paths():
                if album_id in sizes:
                    try:
                        sizes[album_id] += Path(path).stat().st_size
                    except OSError:
                        pass
            albums.sort(key=lambda album: sizes[album[0]], reverse=self.sort_desc)
        self.count.configure(text=f"{len(albums)} álbuns")
        if not albums:
            empty = tk.Frame(self.grid_frame, bg=BG)
            empty.pack(expand=True, pady=84)
            tk.Label(empty, text="Nenhum álbum criado ainda", bg=BG, fg=INK, font=("Inter", 18, "bold")).pack(pady=(0, 7))
            tk.Label(empty, text="Escolha algumas fotos e crie um álbum para começar.", bg=BG, fg=MUTED, font=("Inter", 10)).pack(pady=(0, 16))
            self._pill_button(empty, "Criar álbum", self.create_album, primary=True, width=142).pack()
            if getattr(self, "_wallpaper_enabled", False):
                self._enable_wallpaper_surfaces((self.grid_frame,))
            self._finish_grid_swap(self._photo_render_generation)
            return
        mode = self.view_mode
        compact = mode in ("compact_grid", "compact_list")
        is_list = mode in ("list", "compact_list")
        if is_list:
            for album in albums:
                album_id, name, face_group, count, _cover, created = album
                row = tk.Frame(
                    self.grid_frame,
                    bg=PANEL,
                    highlightbackground=LINE,
                    highlightthickness=1,
                    padx=10,
                    pady=8 if not compact else 4,
                    cursor="hand2",
                )
                self._bind_card_feedback(row, selected=album_id == self.active_album, outline=LINE, always_outline=True)
                row.pack(fill="x", pady=3 if compact else 5)
                self._set_album_drop_target(row, album_id, name)
                size = (48, 48) if compact else (72, 72)
                cover = covers.get(album_id)
                image = self._album_art(name, size)
                pic = tk.Label(row, bg=PANEL, cursor="hand2")
                self._track_wallpaper_thumbnail(pic, image, "album_browser")
                ref = self._wallpaper_photo_image(image, master=self)
                self.album_refs.append(ref)
                pic.configure(image=ref)
                pic.pack(side="left", padx=(0, 14))
                self._set_album_drop_target(pic, album_id, name)
                if cover:
                    self._request_thumbnail(cover, size, pic, "album_browser", album_generation)
                copy = tk.Frame(row, bg=PANEL)
                copy.pack(side="left", fill="x", expand=True)
                self._set_album_drop_target(copy, album_id, name)
                title = tk.Label(copy, text=name, bg=PANEL, fg=INK, font=("Inter", 11 if not compact else 9, "bold"), anchor="w")
                title.pack(anchor="w")
                self._set_album_drop_target(title, album_id, name)
                tk.Label(
                    copy,
                    text=f"{self._photo_count(count)}  ·  {'Grupo facial' if face_group else 'Álbum'}",
                    bg=PANEL,
                    fg=MUTED,
                    font=("Inter", 9),
                    anchor="w",
                ).pack(anchor="w", pady=(4, 0))
                for widget in (row, pic, copy, title):
                    widget.bind("<Button-1>", lambda _e, i=album_id, n=name: self.open_album(i, n))
        else:
            width = 180 if compact else 232
            cols = max(2, min(8, self.canvas.winfo_width() // (width + 18)))
            for col in range(cols):
                self.grid_frame.grid_columnconfigure(col, weight=1, uniform="albums")
            for i, album in enumerate(albums):
                album_id, name, face_group, count, _cover, created = album
                card = tk.Frame(
                    self.grid_frame,
                    bg=BG,
                    highlightthickness=0,
                    padx=10 if not compact else 7,
                    pady=10 if not compact else 7,
                    cursor="hand2",
                )
                card.grid(row=i // cols, column=i % cols, sticky="nsew", padx=(0, 12), pady=(0, 14 if not compact else 9))
                self._set_album_drop_target(card, album_id, name)
                thumb_size = (width - 20, width - 20) if not compact else (width - 14, width - 14)
                cover = covers.get(album_id)
                image = self._album_art(name, thumb_size)
                pic = tk.Label(card, bg=BG, cursor="hand2")
                self._track_wallpaper_thumbnail(pic, image, "album_browser")
                ref = self._wallpaper_photo_image(image, master=self)
                self.album_refs.append(ref)
                pic.configure(image=ref)
                pic.pack(fill="x")
                self._set_album_drop_target(pic, album_id, name)
                if cover:
                    self._request_thumbnail(cover, thumb_size, pic, "album_browser", album_generation)
                title = tk.Label(
                    pic,
                    text=name,
                    bg="#11141b",
                    fg="#ffffff",
                    font=("Inter", 9 if compact else 10, "bold"),
                    anchor="w",
                    padx=8,
                    pady=5,
                    bd=0,
                    cursor="hand2",
                )
                title.place_forget()
                self._set_album_drop_target(title, album_id, name)
                hover_state = {"hide_after_id": None}

                def show_album_name(_event=None, overlay=title, state=hover_state, owner=card):
                    pending = state["hide_after_id"]
                    if pending:
                        try:
                            owner.after_cancel(pending)
                        except tk.TclError:
                            pass
                        state["hide_after_id"] = None
                    overlay.place(x=0, rely=1, relwidth=1, anchor="sw")
                    overlay.lift()

                def schedule_album_name_hide(_event=None, overlay=title, state=hover_state, owner=card):
                    def hide_if_outside():
                        state["hide_after_id"] = None
                        try:
                            pointer = self.winfo_containing(self.winfo_pointerx(), self.winfo_pointery())
                            while pointer is not None:
                                if pointer is owner:
                                    return
                                pointer = getattr(pointer, "master", None)
                            overlay.place_forget()
                        except tk.TclError:
                            return

                    pending = state["hide_after_id"]
                    if pending:
                        try:
                            owner.after_cancel(pending)
                        except tk.TclError:
                            pass
                    state["hide_after_id"] = owner.after_idle(hide_if_outside)

                command = lambda i=album_id, n=name: self.open_album(i, n)
                for widget in (card, pic, title):
                    widget.bind("<Button-1>", lambda _e, fn=command: fn())
                    widget.bind("<Enter>", show_album_name, add="+")
                    widget.bind("<Leave>", schedule_album_name_hide, add="+")
        if getattr(self, "_wallpaper_enabled", False):
            self._enable_wallpaper_surfaces((self.grid_frame,))
        self._finish_grid_swap(self._photo_render_generation)

    def _album_art(self, name, size):
        cache_key = (name, tuple(size))
        cached = self.album_art_cache.get(cache_key)
        if cached is not None:
            return cached
        width, height = size
        scale = 4
        canvas_size = (max(1, width * scale), max(1, height * scale))
        seed = sum((index + 1) * ord(char) for index, char in enumerate(name))
        palette = ["#6a48d7", "#087f72", "#b45276", "#ba6d20", "#3e65b8", "#527b34"]
        color = palette[seed % len(palette)]
        image = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        mask = Image.new("L", canvas_size, 0)
        mask_draw = ImageDraw.Draw(mask)
        radius = max(12, min(width, height) // 9 * scale)
        box = (3, 3, canvas_size[0] - 4, canvas_size[1] - 4)
        mask_draw.rounded_rectangle(box, radius=radius, fill=255)
        draw.rounded_rectangle(
            box,
            radius=radius,
            fill=color,
        )
        draw.ellipse(
            (width * scale * 0.48, -height * scale * 0.18, width * scale * 1.22, height * scale * 0.56),
            fill=self._blend_color(color, "#ffffff", 0.22),
        )
        draw.ellipse(
            (-width * scale * 0.28, height * scale * 0.58, width * scale * 0.48, height * scale * 1.34),
            fill=self._blend_color(color, "#000000", 0.22),
        )
        initial = (name.strip()[:1] or "•").upper()
        font_path = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "segoeuib.ttf"
        try:
            font = ImageFont.truetype(str(font_path), max(20, int(min(width, height) * 0.46 * scale)))
        except OSError:
            font = ImageFont.load_default()
        box = draw.textbbox((0, 0), initial, font=font)
        x = (canvas_size[0] - (box[2] - box[0])) / 2 - box[0]
        y = (canvas_size[1] - (box[3] - box[1])) / 2 - box[1]
        draw.text((x, y), initial, font=font, fill="#ffffff")
        image.putalpha(mask)
        image = image.resize(size, Image.Resampling.LANCZOS)
        self.album_art_cache[cache_key] = image
        while len(self.album_art_cache) > 36:
            self.album_art_cache.pop(next(iter(self.album_art_cache)))
        return image

    def _refresh_photos(self):
        self._photo_render_generation += 1
        generation = self._photo_render_generation
        self._cancel_thumbnail_scope("photo_grid")
        self._cancel_thumbnail_scope("album_browser")
        if self._photo_render_after_id:
            try:
                self.after_cancel(self._photo_render_after_id)
            except tk.TclError:
                pass
            self._photo_render_after_id = None
        self._begin_grid_swap(generation)
        if self.active_view == "albums":
            self.toolbar.pack_forget()
            self.album_tools.pack_forget()
            self._render_album_browser()
            return
        if self.active_album is not None:
            if not self.album_tools.winfo_manager():
                self.album_tools.pack(fill="x", before=self.grid_wrap, pady=(0, 11))
        else:
            self.album_tools.pack_forget()
        if not self.toolbar.winfo_manager():
            sibling = self.album_tools if self.album_tools.winfo_manager() else self.grid_wrap
            self.toolbar.pack(fill="x", before=sibling, pady=(23, 14))
        paths = self._current_paths()
        visible_paths = paths[:self.visible_limit]
        membership_map = self.store.membership_map(visible_paths)
        self.selected.intersection_update(paths)
        self.count.configure(text=self._photo_count(len(paths)))
        self.select_label.configure(text=("1 selecionada" if len(self.selected) == 1 else f"{len(self.selected)} selecionadas"))
        self.crumb.configure(
            text="Biblioteca   /   "
            + (self.active_album_name if self.active_album is not None else "Sem álbum" if self.active_filter == "unassigned" else "Todas as fotos")
        )
        self.heading.configure(
            text=self.active_album_name if self.active_album is not None else "Sem álbum" if self.active_filter == "unassigned" else "Todas as fotos"
        )
        folder_count = self.store.folder_count()
        self.folder_label.configure(
            text=(Path(self.active_folder).name if self.active_folder else f"{folder_count} pastas conectadas · no seu computador")
            if paths
            else "Adicione uma pasta ou fotos para preencher sua galeria."
        )
        if not paths:
            self.scroll.pack_forget()
            empty = tk.Canvas(self.grid_frame, bg=BG, highlightthickness=0, width=590, height=330)
            empty.place(relx=0.5, rely=0.46, anchor="center", width=590, height=330)
            empty.create_oval(265, 38, 325, 98, fill=self._blend_color(PANEL, self.accent, 0.24), outline="")
            empty.create_rectangle(278, 54, 312, 82, outline=INK, width=2)
            empty.create_oval(302, 58, 307, 63, outline=INK, width=2)
            empty.create_polygon(281, 78, 290, 67, 298, 75, 304, 70, 310, 78, fill=INK, outline="")
            empty.create_text(
                291,
                137,
                text="Este álbum ainda está vazio" if self.active_album is not None else "Sua galeria começa aqui",
                fill="#ffffff",
                font=("Inter", 20, "bold"),
            )
            empty.create_text(
                291,
                176,
                text="Selecione fotos e use “Adicionar a álbum” para reuni-las aqui."
                if self.active_album is not None
                else "Adicione uma pasta ou escolha algumas fotos para montar seus álbuns.",
                fill="#ffffff",
                font=("Inter", 10, "bold"),
                width=460,
            )
            actions = tk.Frame(empty, bg=PANEL)
            self._pill_button(actions, "＋  Escolher pasta", self.add_folder, primary=True, width=164).pack(side="left", padx=5)
            self._pill_button(actions, "Adicionar fotos", self.add_photos, width=150).pack(side="left", padx=5)
            empty.create_window(291, 237, window=actions)
            if getattr(self, "_wallpaper_enabled", False):
                self._enable_wallpaper_surfaces((self.grid_frame,))
            self._finish_grid_swap(generation)
            return
        if not self.scroll.winfo_manager():
            self.scroll.pack(side="right", fill="y", padx=(8, 0))
        mode = self.view_mode
        compact = mode in ("compact_grid", "compact_list")
        is_list = mode in ("list", "compact_list")
        canvas_width = max(1, self.canvas.winfo_width())
        density_width = {"compact": 185, "comfortable": 250, "spacious": 330}[self.gallery_density]
        if compact:
            density_width = round(density_width * 0.8)
        columns = 1 if is_list else max(1, min(10, canvas_width // density_width))
        max_card_width = {"compact": 190, "comfortable": 220, "spacious": 320}[self.gallery_density]
        card_width = max(96, min(max_card_width, (canvas_width - 13 * (columns - 1)) // columns - 4))
        for c in range(columns):
            self.grid_frame.grid_columnconfigure(c, weight=1, uniform="photos")
        self._photo_render_state = {
            "generation": generation,
            "paths": visible_paths,
            "all_paths": paths,
            "memberships": membership_map,
            "columns": columns,
            "card_width": card_width,
            "mode": mode,
            "compact": compact,
            "is_list": is_list,
            "index": 0,
        }
        self._render_photo_batch(generation)

    def _render_photo_batch(self, generation):
        state = getattr(self, "_photo_render_state", None)
        if not state or generation != self._photo_render_generation or state["generation"] != generation:
            return
        visible_paths = state["paths"]
        paths = state["all_paths"]
        membership_map = state["memberships"]
        columns, mode = state["columns"], state["mode"]
        card_width = state["card_width"]
        compact, is_list = state["compact"], state["is_list"]
        start = state["index"]
        stop = min(start + 30, len(visible_paths))
        new_cards = []
        for i in range(start, stop):
            path = visible_paths[i]
            selected_bg = BG
            membership = membership_map.get(path, [])
            missing_file = not _safe_is_file(path)
            status = (
                "Arquivo não localizado"
                if missing_file
                else (" · ".join(membership[:2]) + (f" +{len(membership) - 2}" if len(membership) > 2 else ""))
                if membership
                else "Sem álbum"
            )
            card = tk.Frame(
                self.grid_frame,
                bg=selected_bg,
                highlightbackground=BG,
                highlightthickness=0,
                padx=0 if not is_list else 10,
                pady=0 if not is_list else 8,
                cursor="hand2",
            )
            card._galeria_photo_card = True
            card._galeria_photo_path = path
            self.cards[path] = card
            new_cards.append(card)
            self._bind_card_feedback(card, selected=path in self.selected, outline=BG, always_outline=True)
            if is_list:
                card.grid(row=i, column=0, sticky="ew", padx=2, pady=3 if compact else 5)
            else:
                card.grid(row=i // columns, column=i % columns, sticky="nsew", padx=(0, 13), pady=(0, 14))
            card.bind("<Double-Button-1>", lambda e, p=path: self._open_photo_viewer(p))
            card.bind("<Button-3>", lambda e, p=path: self._photo_context_menu(e, p))
            card.bind("<ButtonPress-1>", lambda e, p=path: self._begin_photo_drag(e, p), add="+")
            try:
                thumb_size = (
                    (56, 56)
                    if mode == "compact_list"
                    else (88, 88)
                    if mode == "list"
                    else (min(150, card_width),) * 2
                    if compact
                    else (card_width, card_width)
                )
                thumb_frame = tk.Frame(card, bg=selected_bg, width=thumb_size[0], height=thumb_size[1])
                thumb_frame.pack(side="left", padx=(0, 12)) if is_list else thumb_frame.pack(anchor="center")
                thumb_frame.pack_propagate(False)
                image_label = tk.Canvas(
                    thumb_frame,
                    bg=selected_bg,
                    width=thumb_size[0],
                    height=thumb_size[1],
                    highlightthickness=0,
                    bd=0,
                    cursor="hand2",
                )
                image_label.place(relx=0, rely=0, relwidth=1, relheight=1)
                image_label.bind("<Configure>", lambda _event, target=card: self._refresh_photo_card_overlay(target), add="+")
                image_label.bind("<Enter>", lambda _event, target=card: self._set_card_hover(target, True), add="+")
                image_label.bind("<Leave>", lambda _event, target=card: self._set_card_hover(target, False), add="+")
                image_label._galeria_loading_id = image_label.create_text(
                    thumb_size[0] / 2,
                    thumb_size[1] / 2,
                    text="Carregando…",
                    fill=MUTED,
                    font=("Inter", 8, "bold"),
                    width=max(32, thumb_size[0] - 8),
                    tags=("photo_loading",),
                )
                card._galeria_thumb_canvas = image_label
                self._refresh_photo_card_overlay(card)
                self._request_thumbnail(
                    path,
                    thumb_size,
                    image_label,
                    "photo_grid",
                    generation,
                    fade_in=i < 60,
                    background_color=selected_bg,
                )
                for widget in (thumb_frame, image_label):
                    widget.bind("<ButtonPress-1>", lambda e, p=path: self._begin_photo_drag(e, p), add="+")
                    widget.bind("<Double-Button-1>", lambda e, p=path: self._open_photo_viewer(p))
                    widget.bind("<Button-3>", lambda e, p=path: self._photo_context_menu(e, p))
            except Exception:
                image_label = tk.Label(card, text="Foto indisponível", bg=selected_bg, fg=MUTED, height=5)
                image_label.pack(side="left" if is_list else "top", fill="x")
                image_label.bind("<ButtonPress-1>", lambda e, p=path: self._begin_photo_drag(e, p), add="+")
            if not is_list:
                self._add_tooltip(image_label, status, delay=1000)
            if is_list:
                info = tk.Frame(card, bg=card.cget("bg"))
                info.pack(side="left", fill="both", expand=True, padx=8)
                status_label = tk.Label(
                    info,
                    text=status,
                    bg=card.cget("bg"),
                    fg=MUTED if missing_file else self.accent if membership else MUTED,
                    font=("Inter", 8 if compact else 9),
                    anchor="w",
                )
                status_label.pack(fill="x")
                status_label.bind("<ButtonPress-1>", lambda e, p=path: self._begin_photo_drag(e, p), add="+")
                status_label.bind("<Double-Button-1>", lambda e, p=path: self._open_photo_viewer(p))
                status_label.bind("<Button-3>", lambda e, p=path: self._photo_context_menu(e, p))
                info.bind("<ButtonPress-1>", lambda e, p=path: self._begin_photo_drag(e, p), add="+")
                info.bind("<Button-3>", lambda e, p=path: self._photo_context_menu(e, p))
        state["index"] = stop
        if getattr(self, "_wallpaper_enabled", False):
            self._enable_wallpaper_surfaces(new_cards)
        self._update_scroll_region()
        if stop < len(visible_paths):
            self._photo_render_after_id = self.after(1, lambda g=generation: self._render_photo_batch(g))
        else:
            self._photo_render_after_id = None
            if len(paths) > len(visible_paths):
                self._pill_button(
                    self.grid_frame,
                    f"Carregar mais fotos ({len(paths) - len(visible_paths)} restantes)",
                    self.load_more_photos,
                    width=260,
                ).grid(row=(len(visible_paths) + columns - 1) // columns, column=0, columnspan=columns, pady=12)
            self._finish_grid_swap(generation)

    def _open_photo_file(self, path):
        if not _safe_is_file(path):
            self._locate_missing_photo(path)
            return
        try:
            if hasattr(os, "startfile"):
                os.startfile(path)
        except OSError as exc:
            self._notify(f"Não foi possível abrir o arquivo: {exc}")

    def _open_photo_viewer(self, path):
        if not _safe_is_file(path):
            self._locate_missing_photo(path)
            return
        viewer = getattr(self, "_photo_viewer", None)
        if viewer is not None and viewer.winfo_exists():
            state = viewer._galeria_state
            paths = self._current_paths()
            state["paths"] = paths if path in paths else [path]
            state["index"] = state["paths"].index(path)
            viewer.deiconify()
            viewer.lift()
            self._load_photo_viewer_image(viewer)
            return

        paths = self._current_paths()
        if path not in paths:
            paths = [path]
        screen_width, screen_height = self.winfo_screenwidth(), self.winfo_screenheight()
        width = max(720, min(1200, round(screen_width * 0.82)))
        height = max(520, min(900, round(screen_height * 0.84)))
        viewer = tk.Toplevel(self)
        viewer.title(f"Capri — {Path(path).name}")
        viewer.configure(bg="#0b0d12")
        viewer.geometry(f"{width}x{height}")
        viewer.minsize(480, 360)
        viewer.transient(self)
        state = {
            "paths": paths,
            "index": paths.index(path),
            "zoom": 1.0,
            "generation": 0,
            "image_ref": None,
            "resize_after_id": None,
        }
        viewer._galeria_state = state
        self._photo_viewer = viewer

        toolbar = tk.Frame(viewer, bg="#151922", height=52)
        toolbar.pack(fill="x")
        toolbar.pack_propagate(False)
        self._pill_button(toolbar, "‹", lambda: self._navigate_photo_viewer(viewer, -1), width=42).pack(side="left", padx=(12, 6), pady=7)
        self._pill_button(toolbar, "›", lambda: self._navigate_photo_viewer(viewer, 1), width=42).pack(side="left", padx=(0, 12), pady=7)
        state["title"] = tk.Label(
            toolbar,
            text=Path(path).name,
            bg="#151922",
            fg="#f0f2f5",
            font=("Inter", 10, "bold"),
            anchor="w",
        )
        state["title"].pack(side="left", fill="x", expand=True)
        state["status"] = tk.Label(toolbar, text="100%", bg="#151922", fg="#a9b0b9", font=("Inter", 9))
        state["status"].pack(side="right", padx=12)

        image_canvas = tk.Canvas(viewer, bg="#0b0d12", highlightthickness=0, bd=0)
        image_canvas.pack(fill="both", expand=True)
        state["canvas"] = image_canvas
        image_canvas.bind("<Configure>", lambda _event: self._schedule_photo_viewer_render(viewer), add="+")

        footer = tk.Frame(viewer, bg="#151922", height=48)
        footer.pack(fill="x")
        footer.pack_propagate(False)
        self._pill_button(footer, "−", lambda: self._change_photo_viewer_zoom(viewer, -0.25), width=42).pack(side="right", padx=(6, 12), pady=6)
        self._pill_button(footer, "+", lambda: self._change_photo_viewer_zoom(viewer, 0.25), width=42).pack(side="right", padx=6, pady=6)
        tk.Label(
            footer,
            text="Setas: navegar   ·   Esc: fechar   ·   Zoom: + / −",
            bg="#151922",
            fg="#a9b0b9",
            font=("Inter", 8),
        ).pack(side="left", padx=14)
        viewer.bind("<Left>", lambda _event: self._navigate_photo_viewer(viewer, -1))
        viewer.bind("<Right>", lambda _event: self._navigate_photo_viewer(viewer, 1))
        viewer.bind("<Escape>", lambda _event: self._close_photo_viewer(viewer))
        viewer.bind("<plus>", lambda _event: self._change_photo_viewer_zoom(viewer, 0.25))
        viewer.bind("<equal>", lambda _event: self._change_photo_viewer_zoom(viewer, 0.25))
        viewer.bind("<minus>", lambda _event: self._change_photo_viewer_zoom(viewer, -0.25))
        viewer.protocol("WM_DELETE_WINDOW", lambda: self._close_photo_viewer(viewer))
        viewer.after_idle(lambda: self._apply_dark_titlebar(viewer))
        self._fade_in_window(viewer)
        viewer.after_idle(lambda: self._load_photo_viewer_image(viewer))

    @staticmethod
    def _decode_photo_viewer_image(path, viewport, zoom):
        try:
            base_width = max(1, round(viewport[0] * 0.88))
            base_height = max(1, round(viewport[1] * 0.88))
            target = (max(1, round(base_width * zoom)), max(1, round(base_height * zoom)))
            with _FILE_IO_LOCK:
                with Image.open(path) as opened:
                    try:
                        opened.draft("RGB", target)
                    except (AttributeError, OSError, ValueError):
                        pass
                    image = ImageOps.exif_transpose(opened).convert("RGB")
            original_size = image.size
            image.thumbnail(target, Image.Resampling.LANCZOS)
            if zoom > 1 and max(original_size) < max(base_width, base_height):
                enlarged = (
                    min(target[0], max(1, round(image.width * zoom))),
                    min(target[1], max(1, round(image.height * zoom))),
                )
                image = image.resize(enlarged, Image.Resampling.LANCZOS)
            return image.copy(), None
        except (OSError, ValueError, Image.DecompressionBombError) as exc:
            return None, str(exc)

    def _schedule_photo_viewer_render(self, viewer):
        try:
            if not viewer.winfo_exists():
                return
            state = viewer._galeria_state
            pending = state.get("resize_after_id")
            if pending:
                viewer.after_cancel(pending)
            state["resize_after_id"] = viewer.after(120, lambda: self._load_photo_viewer_image(viewer))
        except tk.TclError:
            return

    def _load_photo_viewer_image(self, viewer):
        try:
            if not viewer.winfo_exists():
                return
            state = viewer._galeria_state
            state["resize_after_id"] = None
            path = state["paths"][state["index"]]
            canvas = state["canvas"]
            viewport = (
                max(320, canvas.winfo_width() or viewer.winfo_width()),
                max(240, canvas.winfo_height() or viewer.winfo_height()),
            )
            state["generation"] += 1
            generation = state["generation"]
            state["title"].configure(text=Path(path).name)
            state["status"].configure(text=f"{state['zoom']:.0%} · Carregando…")
            previous = getattr(self, "_photo_viewer_future", None)
            if previous is not None:
                previous.cancel()
            future = self.photo_viewer_executor.submit(
                self._decode_photo_viewer_image,
                path,
                viewport,
                state["zoom"],
            )
            self._photo_viewer_future = future

            def deliver(done):
                if done.cancelled():
                    return
                try:
                    image, error = done.result()
                except Exception as exc:
                    image, error = None, str(exc)
                self.worker_queue.put(("photo_viewer_image", viewer, generation, image, error))

            future.add_done_callback(deliver)
        except (IndexError, RuntimeError, tk.TclError) as exc:
            self._notify(f"Não foi possível carregar a foto no visualizador: {exc}")

    def _navigate_photo_viewer(self, viewer, direction):
        try:
            if not viewer.winfo_exists():
                return
            state = viewer._galeria_state
            if not state["paths"]:
                return
            state["index"] = (state["index"] + direction) % len(state["paths"])
            self._load_photo_viewer_image(viewer)
        except tk.TclError:
            return

    def _change_photo_viewer_zoom(self, viewer, delta):
        try:
            if not viewer.winfo_exists():
                return
            state = viewer._galeria_state
            state["zoom"] = max(0.5, min(3.0, round(state["zoom"] + delta, 2)))
            self._load_photo_viewer_image(viewer)
        except tk.TclError:
            return

    def _close_photo_viewer(self, viewer):
        try:
            if not viewer.winfo_exists():
                return
            state = viewer._galeria_state
            state["generation"] += 1
            pending = state.get("resize_after_id")
            if pending:
                viewer.after_cancel(pending)
            if self._photo_viewer is viewer:
                self._photo_viewer = None
            future = getattr(self, "_photo_viewer_future", None)
            if future is not None:
                future.cancel()
                self._photo_viewer_future = None
            viewer.destroy()
        except tk.TclError:
            return

    def load_more_photos(self):
        self.visible_limit += 100
        self._refresh_photos()

    def _photo_context_menu(self, event, path):
        """Offer rounded file and album actions without changing selection."""
        paths = list(self.selected) if path in self.selected and self.selected else [path]
        actions = []
        if _safe_is_file(path):
            actions.append(("Abrir no programa padrão", lambda p=path: self._open_photo_file(p)))
        else:
            actions.append(("Localizar arquivo…", lambda p=path: self._locate_missing_photo(p)))
        albums = self.store.albums()
        if albums:
            actions.append((None, None))
            actions.append(
                ("Mover para álbum", lambda selected_paths=paths: self._choose_album_for_photos(selected_paths))
            )
        if self.active_album is not None:
            actions.append(("Devolver para a Biblioteca", lambda selected_paths=paths: self._remove_photos_from_current_album(selected_paths)))
        actions.append((None, None))
        actions.append((f"Excluir {len(paths)} foto(s)…", lambda selected_paths=paths: self._delete_photos_with_confirmation(selected_paths)))

        width = 292
        row_height = 36
        visible_rows = min(9, sum(label is not None for label, _action in actions) + 1)
        height = visible_rows * row_height + 18
        popup = tk.Toplevel(self)
        popup.withdraw()
        popup.overrideredirect(True)
        popup.configure(bg=BG)
        popup.transient(self)
        canvas = tk.Canvas(popup, width=width, height=height, bg=BG, highlightthickness=0, bd=0)
        canvas.pack(fill="both", expand=True)
        panel_id = RoundedControl._rounded_panel(canvas, width, height, radius=14)
        inner = tk.Frame(canvas, bg=PANEL)
        canvas.create_window((10, 10), window=inner, anchor="nw", width=width - 20)
        if len(paths) > 1:
            tk.Label(inner, text=f"{len(paths)} fotos selecionadas", bg=PANEL, fg=MUTED, font=("Inter", 8, "bold"), anchor="w").pack(
                fill="x", padx=5, pady=(2, 4)
            )

        def close_popup():
            try:
                if popup.winfo_exists():
                    popup.destroy()
            except tk.TclError:
                pass

        first_action = None
        for label, action in actions:
            if label is None:
                tk.Frame(inner, bg=LINE, height=1).pack(fill="x", padx=6, pady=4)
                continue
            button = RoundedButton(
                inner,
                text=label,
                command=lambda fn=action: (close_popup(), fn()),
                width=width - 26,
                height=row_height - 2,
            )
            button.pack(fill="x", padx=3, pady=1)
            button.bind("<MouseWheel>", lambda wheel, area=canvas: (area.yview_scroll(-1 if wheel.delta > 0 else 1, "units"), "break"))
            button.bind("<Escape>", lambda _event: close_popup())
            if first_action is None:
                first_action = button

        inner.update_idletasks()
        screen_width, screen_height = popup.winfo_screenwidth(), popup.winfo_screenheight()
        total_height = inner.winfo_reqheight() + 24
        height = min(total_height, max(80, screen_height - 16))
        canvas.configure(height=height)
        canvas.configure(scrollregion=(0, 0, width, max(height, total_height)))
        canvas.delete(panel_id)
        panel_id = RoundedControl._rounded_panel(canvas, width, height, radius=14)
        canvas.tag_lower(panel_id)
        canvas.bind("<MouseWheel>", lambda wheel: (canvas.yview_scroll(-1 if wheel.delta > 0 else 1, "units"), "break"))
        popup.bind("<Escape>", lambda _event: close_popup())

        def dismiss_when_focus_leaves(_event=None):
            def check_focus():
                try:
                    if not popup.winfo_exists():
                        return
                    focused = popup.focus_get()
                    if focused is None or focused.winfo_toplevel() is not popup:
                        close_popup()
                except (AttributeError, tk.TclError):
                    close_popup()

            try:
                popup.after_idle(check_focus)
            except tk.TclError:
                close_popup()

        popup.bind("<FocusOut>", dismiss_when_focus_leaves, add="+")
        x = max(8, min(event.x_root, screen_width - width - 8))
        y = event.y_root
        if y + height > screen_height - 8:
            y = max(8, y - height)
        popup.geometry(f"{width}x{height}+{x}+{y}")
        self._fade_in_window(popup)
        popup.lift()
        if first_action is not None:
            first_action.focus_force()
        return "break"

    def _choose_album_for_photos(self, paths):
        albums = self.store.albums()
        if not albums:
            self._info("Crie um álbum", "Crie um álbum primeiro para poder mover fotos.")
            return
        paths = list(dict.fromkeys(paths))
        options = [f"{name}  ({self._photo_count(count)})" for _album_id, name, _face, count, _cover, _created in albums]
        album_by_option = {option: album for option, album in zip(options, albums)}
        popup = tk.Toplevel(self)
        popup.title("Mover para álbum")
        popup.configure(bg=BG)
        popup.resizable(False, False)
        popup.transient(self)
        popup.grab_set()
        tk.Label(
            popup,
            text=f"Mover {self._selected_count(len(paths))} para:",
            bg=BG,
            fg=INK,
            font=("Inter", 11, "bold"),
        ).pack(anchor="w", padx=20, pady=(19, 10))
        choice = tk.StringVar(master=popup, value=options[0])
        control = self._rounded_dropdown(popup, textvariable=choice, values=options, width=38)
        control.pack(fill="x", padx=20, pady=5)

        def confirm():
            album = album_by_option.get(choice.get())
            if album is None:
                messagebox.showerror("Álbum indisponível", "Selecione um álbum da lista.", parent=popup)
                return
            album_id, name = album[0], album[1]
            remove_album_id = self.active_album if self.active_album is not None and self.active_album != album_id else None
            if self._start_album_file_move(album_id, name, paths, remove_album_id=remove_album_id):
                popup.destroy()

        buttons = tk.Frame(popup, bg=BG)
        buttons.pack(fill="x", padx=20, pady=(14, 17))
        self._pill_button(buttons, "Cancelar", popup.destroy, width=112).pack(side="right", padx=(8, 0))
        self._pill_button(buttons, "Mover fotos", confirm, primary=True, width=138).pack(side="right")
        popup.bind("<Return>", lambda _event: confirm())
        popup.bind("<Escape>", lambda _event: popup.destroy())
        self._fade_in_window(popup)

    def _locate_missing_photo(self, old_path):
        chosen = filedialog.askopenfilename(
            parent=self,
            title="Localize o arquivo desta foto",
            filetypes=[
                ("Imagens", "*.jpg *.jpeg *.jpe *.jfif *.png *.webp *.bmp *.tif *.tiff *.gif *.heic *.heif *.avif"),
                ("Todos os arquivos", "*.*"),
            ],
        )
        if not chosen:
            return
        if Path(chosen).name.casefold() != Path(old_path).name.casefold():
            if not messagebox.askyesno("Nome diferente", "O arquivo escolhido tem outro nome. Quer vincular mesmo assim?", parent=self):
                return
            resolved = _safe_resolve(chosen)
            with self.store._lock:
                try:
                    self.store.db.execute("INSERT OR IGNORE INTO photos(path,folder) VALUES (?,?)", (resolved, str(Path(resolved).parent)))
                    self.store.db.execute(
                        "INSERT OR IGNORE INTO memberships(photo_path,album_id) "
                        "SELECT ?,album_id FROM memberships WHERE photo_path=?",
                        (resolved, old_path),
                    )
                    self.store.db.execute("UPDATE albums SET cover=? WHERE cover=?", (resolved, old_path))
                    self.store.db.execute("DELETE FROM photos WHERE path=?", (old_path,))
                    self.store.db.commit()
                except sqlite3.Error as exc:
                    self.store.db.rollback()
                    messagebox.showerror("Não foi possível reconectar", str(exc), parent=self)
                    return
            recovered = 1
        else:
            recovered = self.store.relink_missing_files([chosen])
        if recovered:
            self.refresh_all()
            self._info("Foto reconectada", "O arquivo foi vinculado novamente e permanece na pasta escolhida.", parent=self)
        else:
            messagebox.showwarning(
                "Vínculo ambíguo",
                "Já existem vários registros com esse nome. Para evitar trocar a foto errada, nenhum vínculo foi alterado.",
                parent=self,
            )

    def _move_photos_to_album(self, paths, album_id, name):
        if self._library_move_busy:
            self._notify("Aguarde a movimentação de fotos terminar.")
            return
        paths = list(dict.fromkeys(paths))
        if not any(_safe_is_file(path) for path in paths):
            messagebox.showwarning("Fotos indisponíveis", "Os arquivos selecionados não estão mais nas pastas conectadas.", parent=self)
            return
        remove_album_id = self.active_album if self.active_album is not None and self.active_album != album_id else None
        self._start_album_file_move(album_id, name, paths, remove_album_id=remove_album_id)

    def _start_album_file_move(self, album_id, name, paths, *, remove_album_id=None, open_album=False, created=False):
        if self._library_move_busy:
            self._notify("Aguarde a movimentação de fotos terminar.")
            return False
        paths = list(dict.fromkeys(str(path) for path in paths))
        available = [path for path in paths if _safe_is_file(path)]
        missing = len(paths) - len(available)
        if missing:
            self._notify(f"{missing} arquivo(s) selecionado(s) não foram encontrados; eles não serão movidos.")
        paths = available
        if not paths:
            if open_album:
                self.open_album(album_id, name)
            return not missing
        if not self._ensure_library_destination():
            return False

        destination = self.destination
        self._library_move_busy = True
        self.configure(cursor="watch")
        self._show_progress(f"Movendo {len(paths)} foto(s) para a pasta do álbum “{name}”…")

        def move_files():
            try:
                if remove_album_id is not None and remove_album_id != album_id:
                    self.store.unassign(remove_album_id, paths)
                self.store.assign(album_id, paths)
                moved, skipped = self.store.move_to_album_folder(album_id, name, paths, destination)
                self.worker_queue.put(
                    ("album_move_finished", album_id, name, paths, moved, skipped, open_album, created, None)
                )
            except (OSError, sqlite3.Error, ValueError) as exc:
                self.worker_queue.put(
                    ("album_move_finished", album_id, name, paths, 0, 0, open_album, created, str(exc))
                )

        threading.Thread(target=move_files, name="Capri-move-fotos-album", daemon=True).start()
        return True

    def _start_existing_album_reorganization(self, albums, destination):
        if self._library_move_busy:
            self._notify("Aguarde a movimentação de fotos terminar.")
            return False
        self._library_move_busy = True
        self.configure(cursor="watch")
        self._show_progress("Organizando os arquivos dos álbuns…", 0, len(albums))

        def move_existing_files():
            moved = skipped = 0
            errors = []
            total = len(albums)
            for index, (album_id, name, _face_group, _count, _cover, _created) in enumerate(albums, 1):
                try:
                    album_moved, album_skipped = self.store.move_to_album_folder(
                        album_id,
                        name,
                        self.store.photos(album_id=album_id),
                        destination,
                    )
                    moved += album_moved
                    skipped += album_skipped
                except (OSError, sqlite3.Error, ValueError) as exc:
                    errors.append(f"{name}: {exc}")
                self.worker_queue.put(("album_reorganize_progress", index, total, name))
            self.worker_queue.put(("album_reorganize_finished", moved, skipped, errors, total))

        threading.Thread(target=move_existing_files, name="Capri-reorganiza-albuns", daemon=True).start()
        return True

    def _set_album_drop_target(self, widget, album_id, name):
        widget._galeria_album_target = (album_id, name)
        self._album_drop_targets.add(widget)

    def _begin_photo_drag(self, event, path):
        paths = list(self.selected) if path in self.selected and self.selected else [path]
        self._photo_drag_state = {
            "path": path,
            "paths": paths,
            "x": event.x_root,
            "y": event.y_root,
            "dragging": False,
            "preview": None,
        }

    def _create_photo_drag_preview(self, state, x_root, y_root):
        preview = tk.Toplevel(self)
        preview.overrideredirect(True)
        preview.configure(bg=LINE)
        try:
            preview.attributes("-topmost", True)
            preview.attributes("-alpha", 0.88)
        except tk.TclError:
            pass
        image_frame = tk.Frame(preview, bg=PANEL, padx=3, pady=3)
        image_frame.pack()
        card = self.cards.get(state["path"])
        thumb_canvas = getattr(card, "_galeria_thumb_canvas", None) if card else None
        source_photo = getattr(thumb_canvas, "_galeria_photo_ref", None) if thumb_canvas else None
        if source_photo is not None and Image is not None:
            try:
                image = ImageTk.getimage(source_photo).convert("RGBA")
                image.thumbnail((96, 96), Image.Resampling.LANCZOS)
                photo = ImageTk.PhotoImage(image, master=preview)
                image_label = tk.Label(image_frame, image=photo, bg=PANEL, bd=0)
                image_label.pack()
                preview._galeria_drag_photo = photo
            except (AttributeError, tk.TclError, TypeError, ValueError):
                source_photo = None
        if source_photo is None:
            tk.Label(
                image_frame,
                text=Path(state["path"]).name[:18],
                bg=PANEL,
                fg=INK,
                font=("Inter", 8, "bold"),
                padx=8,
                pady=12,
            ).pack()
        if len(state["paths"]) > 1:
            tk.Label(
                image_frame,
                text=f"+{len(state['paths']) - 1}",
                bg=self.accent,
                fg="#ffffff",
                font=("Inter", 8, "bold"),
                padx=5,
                pady=2,
            ).place(relx=1, rely=1, anchor="se")
        preview.update_idletasks()
        state["preview"] = preview
        self._move_photo_drag_preview(state, x_root, y_root)

    @staticmethod
    def _move_photo_drag_preview(state, x_root, y_root):
        preview = state.get("preview")
        if preview is None:
            return
        try:
            if not preview.winfo_exists():
                state["preview"] = None
                return
            width, height = preview.winfo_reqwidth(), preview.winfo_reqheight()
            x = max(0, min(x_root + 18, preview.winfo_screenwidth() - width))
            y = max(0, min(y_root + 18, preview.winfo_screenheight() - height))
            preview.geometry(f"+{x}+{y}")
        except tk.TclError:
            state["preview"] = None

    @staticmethod
    def _destroy_photo_drag_preview(state):
        preview, state["preview"] = state.get("preview"), None
        if preview is not None:
            try:
                preview.destroy()
            except tk.TclError:
                pass

    def _on_photo_drag_motion(self, event):
        state = getattr(self, "_photo_drag_state", None)
        if not state:
            return
        if not state["dragging"] and abs(event.x_root - state["x"]) + abs(event.y_root - state["y"]) >= 8:
            state["dragging"] = True
            try:
                self.configure(cursor="hand2")
            except tk.TclError:
                pass
            self._create_photo_drag_preview(state, event.x_root, event.y_root)
        if state["dragging"]:
            self._move_photo_drag_preview(state, event.x_root, event.y_root)

    def _album_target_at(self, x_root, y_root):
        try:
            widget = self.winfo_containing(x_root, y_root)
        except (tk.TclError, AttributeError):
            widget = None
        while widget is not None:
            target = getattr(widget, "_galeria_album_target", None)
            if target:
                return target
            widget = getattr(widget, "master", None)
        candidates = []
        for target_widget in tuple(self._album_drop_targets):
            try:
                if not target_widget.winfo_exists() or not target_widget.winfo_ismapped():
                    continue
                left, top = target_widget.winfo_rootx(), target_widget.winfo_rooty()
                right, bottom = left + target_widget.winfo_width(), top + target_widget.winfo_height()
                if left <= x_root < right and top <= y_root < bottom:
                    target = getattr(target_widget, "_galeria_album_target", None)
                    if target:
                        area = max(1, target_widget.winfo_width() * target_widget.winfo_height())
                        candidates.append((area, target))
            except tk.TclError:
                continue
        return min(candidates, key=lambda item: item[0])[1] if candidates else None

    def _finish_photo_drag(self, event):
        state = getattr(self, "_photo_drag_state", None)
        if not state:
            return
        self._photo_drag_state = None
        self._destroy_photo_drag_preview(state)
        try:
            self.configure(cursor="")
        except tk.TclError:
            pass
        if state["dragging"]:
            self.update_idletasks()
            target = self._album_target_at(event.x_root, event.y_root)
            if target:
                album_id, name = target
                self._move_photos_to_album(state["paths"], album_id, name)
            return "break"
        try:
            if event.widget.winfo_exists():
                self.select_by_click(event, state["path"])
        except (tk.TclError, AttributeError):
            pass
        return "break"

    def _remove_photos_from_current_album(self, paths):
        if self.active_album is None or not paths:
            return
        if self._library_move_busy:
            self._notify("Aguarde a movimentação de fotos terminar.")
            return
        paths = list(dict.fromkeys(paths))
        album_id = self.active_album
        if not self._ensure_library_destination():
            return
        destination = self.destination
        self._library_move_busy = True
        self.configure(cursor="watch")
        self._show_progress("Devolvendo fotos à pasta Biblioteca…", 0, len(paths))

        def move_files():
            try:
                moved, missing = Store.remove_album_photos_to_inbox(
                    album_id,
                    paths,
                    destination,
                    progress=lambda done, total: self.worker_queue.put(("album_remove_progress", done, total)),
                )
                self.worker_queue.put(("album_remove_finished", album_id, paths, moved, missing, None))
            except Exception as exc:
                self.worker_queue.put(("album_remove_finished", album_id, paths, 0, 0, str(exc)))

        threading.Thread(target=move_files, name="Capri-devolve-biblioteca", daemon=True).start()

    def _delete_photos_with_confirmation(self, paths):
        if self._library_move_busy:
            self._notify("Aguarde a movimentação de fotos terminar.")
            return
        paths = list(dict.fromkeys(paths))
        if not paths:
            return
        if not messagebox.askyesno(
            "Excluir fotos",
            f"Excluir {len(paths)} arquivo(s) do computador? Essa ação remove as fotos da Capri e não pode ser desfeita.",
            parent=self,
        ):
            return
        self._start_photo_deletion(paths)

    def _start_photo_deletion(self, paths, *, duplicate_groups=None):
        if self._library_move_busy:
            self._notify("Aguarde a operação atual terminar.")
            return False
        paths = list(dict.fromkeys(paths))
        if not paths:
            return False
        self._library_move_busy = True
        self.configure(cursor="watch")
        self._show_progress(f"Enviando {len(paths)} arquivo(s) para a Lixeira…")

        def delete_files():
            try:
                removed = self.store.remove_photos(paths)
                self.worker_queue.put(("photos_deleted", paths, removed, duplicate_groups, None))
            except sqlite3.Error as exc:
                self.worker_queue.put(("photos_deleted", paths, 0, duplicate_groups, str(exc)))

        threading.Thread(target=delete_files, name="Capri-exclui-fotos", daemon=True).start()
        return True

    def toggle_selection(self, path, value):
        if value:
            self.selected.add(path)
        else:
            self.selected.discard(path)
        self._update_selection_ui()

    def _update_selection_ui(self):
        self.select_label.configure(text=("1 selecionada" if len(self.selected) == 1 else f"{len(self.selected)} selecionadas"))
        for path, card in self.cards.items():
            chosen = path in self.selected
            card._galeria_selected = chosen
            self._refresh_card_feedback(card)

    def _bind_card_feedback(self, card, *, selected=False, outline=BG, always_outline=False):
        card._galeria_selected = selected
        card._galeria_hovered = False
        card._galeria_focused = False
        card._galeria_pressed = False
        card._galeria_base_outline = outline
        card._galeria_always_outline = always_outline
        card.bind("<Enter>", lambda _event, target=card: self._set_card_hover(target, True), add="+")
        card.bind("<Leave>", lambda _event, target=card: self._set_card_hover(target, False), add="+")
        card.bind("<FocusIn>", lambda _event, target=card: self._set_card_focus(target, True), add="+")
        card.bind("<FocusOut>", lambda _event, target=card: self._set_card_focus(target, False), add="+")
        card.bind("<ButtonPress-1>", lambda _event, target=card: self._set_card_pressed(target, True), add="+")
        card.bind("<ButtonRelease-1>", lambda _event, target=card: self._set_card_pressed(target, False), add="+")
        card.configure(takefocus=1)
        self._refresh_card_feedback(card)

    def _set_card_hover(self, card, active):
        if active:
            pending = getattr(card, "_galeria_hover_check_after_id", None)
            if pending:
                try:
                    card.after_cancel(pending)
                except tk.TclError:
                    pass
                card._galeria_hover_check_after_id = None
            if not getattr(card, "_galeria_hovered", False):
                card._galeria_hovered = True
                self._refresh_card_feedback(card)
            return
        pending = getattr(card, "_galeria_hover_check_after_id", None)
        if pending:
            try:
                card.after_cancel(pending)
            except tk.TclError:
                pass
        card._galeria_hover_check_after_id = card.after_idle(lambda target=card: self._finish_card_hover_check(target))

    def _finish_card_hover_check(self, card):
        card._galeria_hover_check_after_id = None
        try:
            if not card.winfo_exists():
                return
            pointer = self.winfo_containing(self.winfo_pointerx(), self.winfo_pointery())
            while pointer is not None:
                if pointer is card:
                    return
                pointer = getattr(pointer, "master", None)
        except (AttributeError, tk.TclError):
            pass
        card._galeria_hovered = False
        self._refresh_card_feedback(card)

    def _set_card_focus(self, card, active):
        card._galeria_focused = active
        self._refresh_card_feedback(card)

    def _set_card_pressed(self, card, active):
        card._galeria_pressed = active
        self._refresh_card_feedback(card)

    def _refresh_card_feedback(self, card):
        try:
            if not card.winfo_exists():
                return
            if getattr(card, "_galeria_photo_card", False):
                background = "#010203" if getattr(self, "_wallpaper_transparent", False) else BG
                card.configure(highlightthickness=0, bg=background)
                self._refresh_photo_card_overlay(card)
                return
            selected = getattr(card, "_galeria_selected", False)
            hovered = getattr(card, "_galeria_hovered", False)
            focused = getattr(card, "_galeria_focused", False)
            pressed = getattr(card, "_galeria_pressed", False)
            base = getattr(card, "_galeria_base_outline", BG)
            if selected or pressed:
                color, thickness = self.accent, 2
            elif hovered or focused:
                color, thickness = self._blend_color(base, self.accent, 0.65), 1
            else:
                color = base
                thickness = 1 if getattr(card, "_galeria_always_outline", False) else 0
                if (
                    thickness
                    and base == BG
                    and getattr(self, "_wallpaper_transparent", False)
                ):
                    color = "#010203"
            card.configure(highlightbackground=color, highlightthickness=thickness)
        except tk.TclError:
            pass

    def select_by_click(self, event, path):
        paths = self._current_paths()
        if event.state & 0x0001 and self.last_clicked in paths:
            start, end = paths.index(self.last_clicked), paths.index(path)
            self.selected.update(paths[min(start, end):max(start, end) + 1])
        else:
            if path in self.selected:
                self.selected.remove(path)
            else:
                self.selected.add(path)
        self.last_clicked = path
        self._update_selection_ui()

    def select_all(self):
        self.selected.update(self._current_paths())
        self._update_selection_ui()

    def clear_selection(self):
        self.selected.clear()
        self._update_selection_ui()

    def show_all(self):
        self.active_view = "photos"
        self.active_folder = None
        self.active_album = None
        self.active_filter = "all"
        self.visible_limit = 100
        self.selected.clear()
        self.canvas.yview_moveto(0)
        self._set_sidebar_active(self.all_button)
        self.refresh_all()

    def show_unassigned(self):
        self.active_view = "photos"
        self.active_folder = None
        self.active_album = None
        self.active_filter = "unassigned"
        self.visible_limit = 100
        self.selected.clear()
        self.canvas.yview_moveto(0)
        self._set_sidebar_active(self.unassigned_button)
        self.refresh_all()

    def open_album(self, album_id, name):
        self.active_view = "photos"
        self.active_folder = None
        self.active_album = album_id
        self.active_album_name = name
        self.active_filter = "all"
        self.visible_limit = 100
        self.selected.clear()
        self.canvas.yview_moveto(0)
        self._set_sidebar_active(self.album_browser_button)
        self.refresh_all()

    def _prompt_initial_album_destination(self):
        pictures = Path.home() / "Pictures"
        initial_dir = pictures if pictures.is_dir() else Path.home()
        dialog = tk.Toplevel(self)
        dialog.title("Bem-vindo à Capri")
        dialog.configure(bg=BG)
        dialog.resizable(False, False)
        dialog.transient(self)
        dialog.grab_set()
        self._fade_in_window(dialog)
        tk.Label(
            dialog,
            text="Escolha onde guardar suas fotos",
            bg=BG,
            fg=INK,
            font=("Inter", 15, "bold"),
        ).pack(anchor="w", padx=22, pady=(20, 8))
        tk.Label(
            dialog,
            text=(
                "Antes de começar, escolha uma pasta para a Capri organizar os álbuns e a Biblioteca. "
                "Os arquivos continuarão neste computador."
            ),
            bg=BG,
            fg=MUTED,
            font=("Inter", 10),
            justify="left",
            wraplength=390,
        ).pack(anchor="w", padx=22, pady=(0, 18))

        def choose_destination():
            chosen = filedialog.askdirectory(
                parent=dialog,
                title="Onde guardar os álbuns e a biblioteca de fotos?",
                initialdir=str(initial_dir),
                mustexist=False,
            )
            if not chosen:
                return
            destination = _safe_resolve(chosen)
            try:
                Path(destination).mkdir(parents=True, exist_ok=True)
                self.store.set_settings({
                    "album_destination": destination,
                    "initial_setup_complete": "1",
                })
            except (OSError, sqlite3.Error) as exc:
                messagebox.showerror(
                    "Pasta indisponível",
                    f"Não foi possível preparar a pasta escolhida:\n{exc}",
                    parent=dialog,
                )
                return
            self.destination = destination
            dialog.destroy()
            self._notify(f"Pasta dos álbuns definida: {destination}")

        footer = tk.Frame(dialog, bg=BG)
        footer.pack(fill="x", padx=22, pady=(0, 18))
        self._pill_button(footer, "Sair", self.close, width=92).pack(side="right", padx=(8, 0))
        self._pill_button(footer, "Escolher pasta…", choose_destination, primary=True, width=164).pack(side="right")
        dialog.protocol("WM_DELETE_WINDOW", self.close)

    def _ensure_library_destination(self):
        if not self.destination or not Path(self.destination).is_dir():
            chosen = filedialog.askdirectory(parent=self, title="Escolha onde guardar a biblioteca de fotos")
            if not chosen:
                self._notify("Escolha uma pasta de armazenamento nas opções de Aparência para importar fotos.")
                return False
            self.destination = _safe_resolve(chosen)
            self.store.set_setting("album_destination", self.destination)
        try:
            Path(self.destination).mkdir(parents=True, exist_ok=True)
            (Path(self.destination) / "Biblioteca").mkdir(parents=True, exist_ok=True)
            return True
        except OSError as exc:
            messagebox.showerror("Pasta indisponível", f"Não consegui preparar a pasta escolhida.\n\n{exc}", parent=self)
            return False

    def _begin_import(self, source, candidates):
        if self._import_busy:
            self._notify("Já estou organizando uma importação. Aguarde ela terminar.")
            return
        self._import_busy = True
        if not candidates:
            self._import_busy = False
            self.configure(cursor="")
            self.refresh_all()
            self._notify("Não encontrei fotos compatíveis nessa seleção.")
            return
        self.configure(cursor="watch")
        self._show_progress(f"Analisando {len(candidates):,} arquivos de imagem…", 0, len(candidates))
        try:
            recovered = self.store.relink_missing_files(candidates)
        except sqlite3.Error as exc:
            self._import_busy = False
            self.configure(cursor="")
            self._hide_progress()
            messagebox.showerror("Falha ao atualizar a biblioteca", str(exc), parent=self)
            return
        self._import_state = {
            "source": source,
            "paths": candidates,
            "index": 0,
            "added": 0,
            "recovered": recovered,
        }
        self._index_import_batch()

    def _index_import_batch(self):
        state = self._import_state
        start = state["index"]
        batch = state["paths"][start:start + 500]
        if batch:
            try:
                state["added"] += self.store.index_paths(batch)
            except (sqlite3.Error, OSError) as exc:
                self._import_busy = False
                self.configure(cursor="")
                self._hide_progress()
                messagebox.showerror("Falha ao atualizar a biblioteca", str(exc), parent=self)
                return
            state["index"] += len(batch)
            self._show_progress(
                f"Adicionando fotos à biblioteca {state['index']:,} de {len(state['paths']):,}…",
                state["index"],
                len(state["paths"]),
            )
            self.after(1, self._index_import_batch)
            return

        unassigned = self.store.photos(unassigned=True)
        destination = self.destination

        def move_files():
            moved = self.store.move_unassigned_to_inbox(unassigned, destination)
            self.worker_queue.put(("import_finished", state["source"], state["added"], state["recovered"], moved, len(state["paths"])))

        threading.Thread(target=move_files, name="Galeria-organiza-biblioteca", daemon=True).start()
        self._show_progress("Movendo fotos para a pasta central da biblioteca…")

    def add_folder(self):
        if self._library_move_busy:
            self._notify("Aguarde a movimentação de fotos terminar.")
            return
        if self._import_busy or self._scan_busy:
            self._notify("Aguarde a importação em andamento.")
            return
        if not self._ensure_library_destination():
            return
        path = filedialog.askdirectory(parent=self, title="Escolha uma pasta para analisar com todas as subpastas")
        if not path:
            return
        source = _safe_resolve(path)
        destination = self.destination
        self._scan_busy = True
        self.configure(cursor="watch")
        self._show_progress("Procurando fotos em todas as subpastas…")

        def scan():
            try:
                candidates = Store.scan_folder_paths(source, destination)
                self.worker_queue.put(("import_scanned", source, candidates))
            except Exception as exc:
                self.worker_queue.put(("import_error", str(exc)))

        threading.Thread(target=scan, name="Galeria-varredura-recursiva", daemon=True).start()

    def add_photos(self):
        if self._library_move_busy:
            self._notify("Aguarde a movimentação de fotos terminar.")
            return
        if self._import_busy or self._scan_busy:
            self._notify("Aguarde a importação em andamento.")
            return
        if not self._ensure_library_destination():
            return
        paths = filedialog.askopenfilenames(
            title="Selecione uma ou várias fotos",
            filetypes=[
                ("Imagens", "*.jpg *.jpeg *.jpe *.jfif *.png *.webp *.bmp *.tif *.tiff *.gif *.heic *.heif *.avif"),
                ("Todos os arquivos", "*.*"),
            ],
        )
        if not paths:
            return
        self.active_view = "photos"
        self.active_folder = None
        self.active_album = None
        self.active_filter = "all"
        self.selected.clear()
        self._begin_import(None, list(paths))

    def create_album(self):
        if self._library_move_busy:
            self._notify("Aguarde a movimentação de fotos terminar.")
            return
        name = self._ask_text("Criar álbum", "Nome da pessoa, viagem ou momento:", parent=self)
        if not name or not name.strip():
            return
        name = name.strip()
        selected_paths = list(self.selected)
        paths = [path for path in selected_paths if _safe_is_file(path)]
        if selected_paths and not paths:
            messagebox.showwarning("Fotos indisponíveis", "Os arquivos selecionados não estão mais nas pastas conectadas.", parent=self)
            return
        if paths and not self._ensure_library_destination():
            return
        try:
            album_id = self.store.create_album(name)
        except sqlite3.IntegrityError:
            messagebox.showerror("Nome já usado", "Já existe um álbum com esse nome.")
            return
        if paths:
            self._start_album_file_move(album_id, name, selected_paths, open_album=True, created=True)
        else:
            self.open_album(album_id, name)
            self._info("Álbum criado", f"“{name}” foi criado e aberto.")

    def create_album_from_selection(self):
        if self._library_move_busy:
            self._notify("Aguarde a movimentação de fotos terminar.")
            return
        if not self.selected:
            self._info("Selecione fotos", "Marque as fotos que quer reunir e clique de novo.")
            return
        name = self._ask_text(
            "Criar álbum com seleção",
            f"{self._selected_count(len(self.selected))}.\n"
            "Nome da pessoa, viagem ou momento:",
            parent=self,
        )
        if not name or not name.strip():
            return
        name = name.strip()
        selected_paths = list(self.selected)
        paths = [path for path in selected_paths if _safe_is_file(path)]
        if not paths:
            self._notify("Os arquivos selecionados não estão mais nas pastas conectadas.")
            return
        if not self._ensure_library_destination():
            return
        try:
            album_id = self.store.create_album(name)
        except sqlite3.IntegrityError:
            messagebox.showerror("Nome já usado", "Já existe um álbum com esse nome.")
            return
        self._start_album_file_move(album_id, name, selected_paths, open_album=True, created=True)

    def assign_selection(self):
        if self._library_move_busy:
            self._notify("Aguarde a movimentação de fotos terminar.")
            return
        if not self.selected:
            self._info("Selecione fotos", "Marque uma ou mais fotos primeiro.")
            return
        albums = self.store.albums()
        if not albums:
            self._info("Crie um álbum", "Crie um álbum primeiro, depois adicione as fotos selecionadas.")
            return
        options = [f"{a[1]}  ({self._photo_count(a[3])})" for a in albums]
        popup = tk.Toplevel(self)
        popup.title("Adicionar a álbum")
        popup.configure(bg=BG)
        popup.resizable(False, False)
        popup.transient(self)
        popup.grab_set()
        self._fade_in_window(popup)
        tk.Label(popup, text=f"Adicionar {len(self.selected)} fotos selecionadas a:", bg=BG, fg=INK, font=("Inter", 11, "bold")).pack(
            anchor="w", padx=20, pady=(19, 10)
        )
        choice = tk.StringVar(value=options[0])
        self._rounded_dropdown(popup, textvariable=choice, values=options, width=38).pack(padx=20, pady=5)

        def confirm():
            index = options.index(choice.get())
            album = albums[index]
            paths = list(self.selected)
            if self._start_album_file_move(album[0], album[1], paths):
                popup.destroy()

        self._pill_button(popup, "Adicionar fotos", confirm, primary=True, width=150).pack(anchor="e", padx=20, pady=17)

    def delete_multiple_albums(self):
        if self._library_move_busy:
            self._notify("Aguarde a movimentação de fotos terminar.")
            return
        albums = self.store.albums()
        if not albums:
            self._info("Sem álbuns", "Crie um álbum para poder selecionar ou excluir álbuns.")
            return
        popup = tk.Toplevel(self)
        popup.title("Selecionar álbuns")
        popup.configure(bg=BG)
        popup.geometry("420x430")
        popup.transient(self)
        popup.grab_set()
        self._fade_in_window(popup)
        tk.Label(popup, text="Selecione um ou mais álbuns", bg=BG, fg=INK, font=("Inter", 14, "bold")).pack(anchor="w", padx=18, pady=(18, 5))
        tk.Label(
            popup,
            text="Excluir um álbum remove só a organização; os arquivos das fotos ficam onde estão.",
            bg=BG,
            fg=MUTED,
            wraplength=380,
            justify="left",
        ).pack(anchor="w", padx=18, pady=(0, 12))
        list_area = tk.Frame(popup, bg=BG)
        list_area.pack(fill="both", expand=True, padx=18, pady=4)
        box = tk.Listbox(
            list_area,
            selectmode="extended",
            font=("Inter", 10),
            relief="flat",
            activestyle="none",
            exportselection=False,
            bg=PANEL,
            fg=INK,
            selectbackground=self.accent,
            selectforeground="#101010",
            highlightthickness=0,
            borderwidth=0,
            cursor="hand2",
        )
        scrollbar = SlimScrollbar(list_area, command=box.yview)
        box._galeria_scrollbar = scrollbar
        scrollbar._galeria_area = box
        box.configure(yscrollcommand=scrollbar.set)
        box.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self._register_scroll_canvas(popup, box)
        for album in albums:
            box.insert("end", f"{album[1]}   ·   {album[3]} fotos")

        def delete_selected():
            idx = list(box.curselection())
            if not idx:
                self._info("Selecione álbuns", "Use Ctrl ou Shift para selecionar vários.", parent=popup)
                return
            names = [albums[i][1] for i in idx]
            if not messagebox.askyesno(
                "Confirmar exclusão",
                f"Excluir {len(names)} álbum{'s' if len(names) != 1 else ''}?\n\n"
                + "\n".join(names[:8])
                + ("\n…" if len(names) > 8 else "")
                + "\n\nAs fotos originais não serão apagadas.",
                parent=popup,
            ):
                return
            with self.store._lock:
                self.store.db.executemany("DELETE FROM albums WHERE id=?", [(albums[i][0],) for i in idx])
                self.store.db.commit()
            if self.active_album in {albums[i][0] for i in idx}:
                self.active_album = None
            popup.destroy()
            self.refresh_all()

        self._pill_button(popup, "Excluir selecionados", delete_selected, width=180).pack(anchor="e", padx=18, pady=16)

    def find_duplicates(self):
        if self._library_move_busy:
            self._notify("Aguarde a movimentação de fotos terminar.")
            return
        paths = [path for path in self.store.photos() if _safe_is_file(path)]
        if not paths:
            self._info("Biblioteca vazia", "Adicione fotos antes de procurar duplicatas.")
            return
        self.configure(cursor="watch")
        self._show_progress(f"Comparando visualmente {len(paths):,} fotos…", 0, len(paths))

        def compare():
            try:
                groups = self.store.duplicate_groups(
                    paths,
                    progress=lambda done, total: self.worker_queue.put(("duplicates_progress", done, total)),
                )
                self.worker_queue.put(("duplicates", groups))
            except Exception as exc:
                self.worker_queue.put(("duplicates_error", str(exc)))

        threading.Thread(target=compare, name="Galeria-duplicatas-visuais", daemon=True).start()

    def review_duplicates(self, groups):
        self.configure(cursor="")
        self._hide_progress()
        if not groups:
            self.refresh_all()
            self._notify("Nenhuma imagem passou do limite de 95% de similaridade visual.")
            return
        remove = [path for group in groups for path in group[1:]]
        popup = tk.Toplevel(self)
        popup.title("Revisar duplicatas visuais")
        popup.configure(bg=BG)
        self._fade_in_window(popup)
        popup.geometry("900x660")
        popup.minsize(700, 480)
        popup.transient(self)
        popup.grab_set()
        tk.Label(popup, text=f"{len(groups)} grupos · {len(remove)} duplicatas", bg=BG, fg=INK, font=("Inter", 18, "bold")).pack(
            anchor="w", padx=20, pady=(18, 4)
        )
        tk.Label(
            popup,
            text="Similaridade visual acima de 95%. A melhor resolução fica como original; confira as prévias antes de confirmar.",
            bg=BG,
            fg=MUTED,
            wraplength=850,
            justify="left",
        ).pack(anchor="w", padx=20, pady=(0, 12))
        area = tk.Frame(popup, bg=BG)
        area.pack(fill="both", expand=True, padx=18)
        canvas = tk.Canvas(area, bg=BG, highlightthickness=0)
        scrollbar = SlimScrollbar(area, command=canvas.yview)
        inner = tk.Frame(canvas, bg=BG)
        scrollbar._galeria_area = canvas
        canvas._galeria_scrollbar = scrollbar
        window_id = canvas.create_window((0, 0), window=inner, anchor="nw")
        progress = {"index": 0, "loading": False, "scheduled": False}
        canvas.configure(
            yscrollcommand=lambda first, last: (
                scrollbar.set(first, last),
                request_more_groups(float(first), float(last)) if progress["index"] else None,
            )
        )
        self._register_scroll_canvas(popup, canvas)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        def update_group_scroll_region(_event=None):
            canvas.configure(scrollregion=canvas.bbox("all"))
            if progress["index"]:
                request_more_groups()

        inner.bind("<Configure>", update_group_scroll_region)
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window_id, width=event.width))
        thumbnail_scope = f"duplicate_review:{id(popup)}"
        popup.bind(
            "<Destroy>",
            lambda event: self._cancel_thumbnail_scope(thumbnail_scope) if event.widget is popup else None,
            add="+",
        )

        def request_more_groups(first=None, last=None):
            if (
                not popup.winfo_exists()
                or progress["loading"]
                or progress["scheduled"]
                or progress["index"] >= len(groups)
            ):
                return
            if first is None or last is None:
                first, last = canvas.yview()
            content_height = inner.winfo_reqheight()
            viewport_height = max(1, canvas.winfo_height())
            if content_height > viewport_height * 1.25 and last < 0.84:
                return
            progress["scheduled"] = True

            def scheduled_render():
                progress["scheduled"] = False
                render_groups()

            popup.after_idle(scheduled_render)

        def render_groups():
            if not popup.winfo_exists() or progress["loading"]:
                return
            start = progress["index"]
            progress["loading"] = True
            stop = min(start + 6, len(groups))
            try:
                for group_index, group in enumerate(groups[start:stop], start=start + 1):
                    row = tk.Frame(inner, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
                    row.pack(fill="x", pady=5)
                    tk.Label(row, text=f"Grupo {group_index}", bg=PANEL, fg=self.accent, font=("Inter", 9, "bold")).pack(
                        anchor="w", padx=12, pady=(8, 2)
                    )
                    previews = tk.Frame(row, bg=PANEL)
                    previews.pack(fill="x", padx=10, pady=(0, 9))
                    for item_index, path in enumerate(group[:8]):
                        tile = tk.Frame(previews, bg=PANEL)
                        tile.pack(side="left", padx=4)
                        preview = tk.Label(tile, text="…", bg=PANEL, fg=MUTED, width=12, height=4, bd=0)
                        preview.pack()
                        self._request_thumbnail(path, (72, 72), preview, thumbnail_scope)
                        label = "MANTER · original" if item_index == 0 else "Duplicata"
                        color = self.accent if item_index == 0 else MUTED
                        tk.Label(
                            tile,
                            text=label,
                            bg=PANEL,
                            fg=color,
                            font=("Inter", 8, "bold" if item_index == 0 else "normal"),
                        ).pack(anchor="w", pady=(4, 0))
                        tk.Label(tile, text=Path(path).name[:28], bg=PANEL, fg=INK, font=("Inter", 8), width=22, anchor="w").pack(
                            anchor="w"
                        )
                    if len(group) > 8:
                        tk.Label(
                            previews,
                            text=f"+ {len(group) - 8} arquivos nesta revisão",
                            bg=PANEL,
                            fg=MUTED,
                            font=("Inter", 8),
                        ).pack(side="left", padx=8)
                progress["index"] = stop
            finally:
                progress["loading"] = False
            popup.after_idle(request_more_groups)

        footer = tk.Frame(popup, bg=BG)
        footer.pack(fill="x", padx=18, pady=16)

        def confirm_removal():
            prompt = (
                f"Mover {len(remove)} arquivo(s) duplicado(s), em {len(groups)} grupos, para a Lixeira?\n\n"
                "A cópia com a maior resolução será mantida. A exclusão só acontece depois desta confirmação."
            )
            if not messagebox.askyesno("Confirmar exclusão", prompt, parent=popup):
                return
            if self._start_photo_deletion(remove, duplicate_groups=len(groups)):
                popup.destroy()

        self._pill_button(footer, "Fechar", popup.destroy, width=110).pack(side="right", padx=(8, 0))
        self._pill_button(footer, f"Mover {len(remove)} duplicatas para a Lixeira", confirm_removal, primary=True, width=250).pack(
            side="right"
        )
        popup.after_idle(render_groups)

    def _show_accent_palette(self, parent, variable):
        """Compact, themed color chooser without the native legacy dialog."""
        popup = tk.Toplevel(parent)
        popup.title("Cor de destaque")
        popup.configure(bg=BG)
        self._fade_in_window(popup)
        popup.resizable(False, False)
        popup.transient(parent)
        popup.grab_set()
        tk.Label(popup, text="Escolha uma cor", bg=BG, fg=INK, font=("Inter", 12, "bold")).pack(anchor="w", padx=16, pady=(14, 8))
        colors = [
            CAPRI_ACCENT, "#ff5b50", "#ff7b32", "#f5b52e", "#cbd83c",
            "#8edb42", "#42c878", "#19c4a4", "#19bbc0", "#24a9e8",
            "#4f8dff", "#746df5", "#a466ff", "#cf58e8", "#ff58bf",
            "#b4c0cb", "#8995a2", "#64717e", "#d2d5d8", "#f6f4f7",
        ]
        swatches = tk.Frame(popup, bg=BG)
        swatches.pack(padx=12, pady=4)
        for index, color in enumerate(colors):
            swatch = tk.Canvas(swatches, width=34, height=34, bg=BG, highlightthickness=0, cursor="hand2")
            swatch.grid(row=index // 5, column=index % 5, padx=3, pady=3)
            swatch.create_oval(3, 3, 31, 31, fill=color, outline=LINE, width=1)
            swatch.bind("<Button-1>", lambda _event, value=color: (variable.set(value), popup.destroy()))
        custom = tk.Frame(popup, bg=BG)
        custom.pack(fill="x", padx=16, pady=(10, 14))
        tk.Label(custom, text="Hex", bg=BG, fg=MUTED).pack(side="left", padx=(0, 8))
        value = tk.StringVar(value=variable.get())
        entry = ttk.Entry(custom, textvariable=value, width=12)
        entry.pack(side="left")

        def accept_custom():
            color = value.get().strip()
            if len(color) == 7 and color.startswith("#"):
                try:
                    int(color[1:], 16)
                except ValueError:
                    pass
                else:
                    variable.set(color.lower())
                    popup.destroy()
                    return
            self._info("Cor inválida", "Digite uma cor no formato #RRGGBB.", parent=popup)

        self._pill_button(custom, "Aplicar", accept_custom, primary=True, width=100).pack(side="right")
        popup.bind("<Return>", lambda _event: accept_custom())
        popup.bind("<Escape>", lambda _event: popup.destroy())

    def preferences(self):
        popup = tk.Toplevel(self)
        popup.title("Aparência")
        popup.configure(bg=BG)
        self._fade_in_window(popup)
        popup.resizable(True, True)
        popup.transient(self)
        popup.grab_set()
        window_width = min(760, max(600, self.winfo_screenwidth() - 80))
        window_height = min(900, max(600, self.winfo_screenheight() - 96))
        popup.geometry(f"{window_width}x{window_height}")
        popup.minsize(min(620, window_width), min(600, window_height))
        original_theme = self.theme
        original_accent = self.accent
        original_ui_scale = self.ui_scale_percent
        original_sidebar_width = self.sidebar_width
        original_gallery_density = self.gallery_density
        original_photo_corner_radius = self.photo_corner_radius
        modern_scales = []
        modern_switches = []
        gallery_preview_after = {"id": None}

        header = tk.Frame(popup, bg=BG)
        header.pack(fill="x", padx=22, pady=(18, 8))
        tk.Label(header, text="Aparência", bg=BG, fg=INK, font=("Inter", 18, "bold")).pack(anchor="w")
        tk.Label(
            header,
            text="Personalize o Capri. As alterações visuais aparecem na prévia; use Cancelar para desfazê-las.",
            bg=BG,
            fg=MUTED,
            wraplength=window_width - 64,
            justify="left",
        ).pack(anchor="w", pady=(3, 0))

        content = tk.Frame(popup, bg=BG)
        content.pack(fill="both", expand=True, padx=(0, 6), pady=(4, 0))
        canvas = tk.Canvas(content, bg=BG, highlightthickness=0)
        scrollbar = SlimScrollbar(content, command=canvas.yview)
        scrollbar._galeria_area = canvas
        canvas._galeria_scrollbar = scrollbar
        canvas.configure(yscrollcommand=scrollbar.set)
        self._register_scroll_canvas(popup, canvas)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        panel = tk.Frame(canvas, bg=BG)
        panel_id = canvas.create_window((0, 0), window=panel, anchor="nw")
        panel.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(panel_id, width=event.width))
        footer = tk.Frame(popup, bg=BG)
        footer.pack(fill="x", padx=22, pady=(4, 12))

        def create_section(title, description):
            card = tk.Frame(panel, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
            card.pack(fill="x", padx=18, pady=(0, 12))
            heading = tk.Frame(card, bg=PANEL)
            heading.pack(fill="x", padx=16, pady=(12, 4))
            tk.Label(heading, text=title, bg=PANEL, fg=INK, font=("Inter", 11, "bold")).pack(anchor="w")
            if description:
                tk.Label(
                    heading,
                    text=description,
                    bg=PANEL,
                    fg=MUTED,
                    wraplength=max(320, window_width - 100),
                    justify="left",
                ).pack(anchor="w", pady=(3, 0))
            body = tk.Frame(card, bg=PANEL)
            body.pack(fill="x", padx=16, pady=(4, 14))
            return body

        def refresh_appearance_controls():
            for control in tuple(modern_scales):
                if control.winfo_exists():
                    control.refresh_theme()
            for control in tuple(modern_switches):
                if control.winfo_exists():
                    control.refresh_theme()

        def schedule_gallery_preview():
            if gallery_preview_after["id"]:
                try:
                    self.after_cancel(gallery_preview_after["id"])
                except tk.TclError:
                    pass
            gallery_preview_after["id"] = self.after(240, refresh_gallery_preview)

        def refresh_gallery_preview():
            gallery_preview_after["id"] = None
            self._refresh_photos()

        def cancel_gallery_preview():
            after_id = gallery_preview_after["id"]
            gallery_preview_after["id"] = None
            if after_id:
                try:
                    self.after_cancel(after_id)
                except tk.TclError:
                    pass

        theme_section = create_section("Tema e cores", "Escolha o modo Capri, claro ou escuro e personalize os destaques.")
        theme_row = tk.Frame(theme_section, bg=PANEL)
        theme_row.pack(fill="x", pady=(0, 12))
        tk.Label(theme_row, text="Tema", bg=PANEL, fg=MUTED).pack(side="left")
        theme_labels = {"light": "Claro", "dark": "Escuro", "capri": "Capri"}
        theme = tk.StringVar(value=theme_labels[self.theme])
        self._rounded_dropdown(theme_row, textvariable=theme, values=["Claro", "Escuro", "Capri"], width=14).pack(side="right")

        accent = tk.StringVar(value=self.store.setting("accent", LIME))
        adaptive = tk.BooleanVar(value=self.store.setting("adaptive_colors", "0") == "1")

        accent_row = tk.Frame(theme_section, bg=PANEL)
        accent_row.pack(fill="x", pady=(0, 8))
        tk.Label(accent_row, text="Cor manual", bg=PANEL, fg=MUTED).pack(side="left")
        accent_swatch = tk.Canvas(accent_row, width=28, height=28, bg=PANEL, highlightthickness=0)
        accent_swatch.pack(side="right", padx=(8, 0))
        accent_swatch_id = accent_swatch.create_oval(3, 3, 25, 25, fill=accent.get(), outline=LINE, width=1)
        accent_value = tk.Label(accent_row, text=accent.get().upper(), bg=PANEL, fg=MUTED, font=("Inter", 8))
        accent_value.pack(side="right")

        def choose_accent():
            self._show_accent_palette(popup, accent)

        accent_button = self._pill_button(accent_row, "Escolher cor…", choose_accent, width=142)
        accent_button.pack(side="right", padx=(0, 10))
        adaptive_switch = RoundedSwitch(
            theme_section,
            text="Adaptar a cor de destaque à imagem de fundo",
            variable=adaptive,
        )
        adaptive_switch.pack(anchor="w")
        modern_switches.append(adaptive_switch)
        accent_hint = tk.Label(theme_section, bg=PANEL, fg=MUTED, font=("Inter", 8), justify="left", wraplength=500)
        accent_hint.pack(anchor="w", pady=(3, 0))

        def sync_accent_mode(*_args):
            automatic = adaptive.get()
            accent_button.configure(state="disabled" if automatic else "normal")
            accent_hint.configure(
                text=(
                    "A cor automática prevalece. Desative esta opção para escolher uma cor manual."
                    if automatic
                    else "A cor escolhida será usada nos destaques."
                )
            )
            accent_value.configure(fg=MUTED if automatic else INK)

        sync_accent_mode()

        layout_section = create_section(
            "Layout da interface",
            "Ajuste o tamanho dos textos e o espaço da barra lateral.",
        )
        scale_row = tk.Frame(layout_section, bg=PANEL)
        scale_row.pack(fill="x", pady=(0, 10))
        tk.Label(scale_row, text="Tamanho dos textos", bg=PANEL, fg=MUTED).pack(side="left")
        ui_scale = tk.StringVar(value=f"{self.ui_scale_percent}%")
        ui_scale_control = self._rounded_dropdown(
            scale_row,
            textvariable=ui_scale,
            values=["90%", "100%", "110%", "120%"],
            width=10,
        )
        ui_scale_control.pack(side="right")

        def preview_ui_scale(_event=None):
            self._apply_ui_scale(ui_scale.get().rstrip("%"))

        ui_scale_control.bind("<<ComboboxSelected>>", preview_ui_scale)

        sidebar_row = tk.Frame(layout_section, bg=PANEL)
        sidebar_row.pack(fill="x", pady=(2, 0))
        tk.Label(sidebar_row, text="Largura da barra lateral", bg=PANEL, fg=MUTED).pack(side="left")
        sidebar_width_value = tk.Label(
            sidebar_row,
            text=f"{self.sidebar_width} px",
            bg=PANEL,
            fg=INK,
            width=8,
            anchor="e",
        )
        sidebar_width_value.pack(side="right")
        sidebar_width = tk.DoubleVar(value=self.sidebar_width)

        def preview_sidebar_width(value):
            width = self._normalize_sidebar_width(value)
            sidebar_width_value.configure(text=f"{width} px")
            self._apply_sidebar_width(width)

        sidebar_scale = ModernScale(
            layout_section,
            variable=sidebar_width,
            from_=220,
            to=340,
            command=preview_sidebar_width,
            step=10,
        )
        modern_scales.append(sidebar_scale)
        sidebar_scale.pack(fill="x", pady=(0, 4))

        gallery_section = create_section(
            "Galeria e cartões",
            "Defina quantas fotos cabem na tela e o arredondamento das miniaturas.",
        )
        density_row = tk.Frame(gallery_section, bg=PANEL)
        density_row.pack(fill="x", pady=(0, 10))
        tk.Label(density_row, text="Densidade da galeria", bg=PANEL, fg=MUTED).pack(side="left")
        density_labels = {"compact": "Compacta", "comfortable": "Equilibrada", "spacious": "Ampla"}
        density_values = {label: key for key, label in density_labels.items()}
        density = tk.StringVar(value=density_labels[self.gallery_density])
        density_control = self._rounded_dropdown(
            density_row,
            textvariable=density,
            values=list(density_values),
            width=15,
        )
        density_control.pack(side="right")

        def preview_density(_event=None):
            selected = density_values.get(density.get(), "comfortable")
            if selected != self.gallery_density:
                self.gallery_density = selected
                schedule_gallery_preview()

        density_control.bind("<<ComboboxSelected>>", preview_density)
        radius_row = tk.Frame(gallery_section, bg=PANEL)
        radius_row.pack(fill="x", pady=(2, 0))
        tk.Label(radius_row, text="Arredondamento das miniaturas", bg=PANEL, fg=MUTED).pack(side="left")
        radius_value = tk.Label(
            radius_row,
            text=f"{self.photo_corner_radius} px",
            bg=PANEL,
            fg=INK,
            width=7,
            anchor="e",
        )
        radius_value.pack(side="right")
        radius = tk.DoubleVar(value=self.photo_corner_radius)

        def preview_corner_radius(value):
            self.photo_corner_radius = self._normalize_photo_corner_radius(value)
            radius_value.configure(text=f"{self.photo_corner_radius} px")
            schedule_gallery_preview()

        radius_scale = ModernScale(
            gallery_section,
            variable=radius,
            from_=0,
            to=20,
            command=preview_corner_radius,
            step=1,
        )
        modern_scales.append(radius_scale)
        radius_scale.pack(fill="x", pady=(0, 4))

        wallpaper_section = create_section(
            "Imagem de fundo",
            "Escolha uma imagem e ajuste o resultado. Arraste a prévia para mover; use a roda do mouse para aproximar.",
        )
        bg_path = tk.StringVar(value=self.store.setting("background", ""))
        wallpaper_info = tk.StringVar()
        wallpaper_name = tk.Label(
            wallpaper_section,
            textvariable=wallpaper_info,
            bg=PANEL,
            fg=INK,
            anchor="w",
            justify="left",
            wraplength=500,
            font=("Inter", 9),
        )
        wallpaper_name.pack(fill="x", pady=(0, 8))

        wallpaper_row = tk.Frame(wallpaper_section, bg=PANEL)
        wallpaper_row.pack(fill="x")

        def choose_background():
            chosen = filedialog.askopenfilename(
                parent=popup,
                title="Escolha uma imagem de fundo",
                filetypes=[
                    ("Imagens suportadas", " ".join(f"*{extension}" for extension in sorted(IMAGE_EXTENSIONS))),
                    ("Todos os arquivos", "*.*"),
                ],
            )
            if chosen:
                bg_path.set(chosen)

        self._pill_button(wallpaper_row, "Escolher imagem…", choose_background, width=174).pack(side="left")
        self._pill_button(wallpaper_row, "Remover", lambda: bg_path.set(""), width=104).pack(side="left", padx=(8, 0))

        background_preview = tk.Canvas(wallpaper_section, height=156, bg=BG, highlightthickness=0, cursor="fleur")
        background_preview.pack(fill="x", pady=(12, 10))
        preview_state = {"path": None, "source": None, "generation": 0, "drag": None}
        background_preview._galeria_preview_loading = False
        background_preview._galeria_preview_error = False

        def stored_number(key, default, minimum, maximum, integer=False):
            try:
                value = float(self.store.setting(key, str(default)))
            except (TypeError, ValueError):
                value = float(default)
            value = max(minimum, min(maximum, value))
            return int(value) if integer else value

        opacity = tk.DoubleVar(value=stored_number("background_opacity", 0.42, 0, 1))
        blur = tk.DoubleVar(value=stored_number("background_blur", 8, 0, 32))
        zoom = tk.DoubleVar(value=stored_number("background_zoom", 100, 50, 300))
        pan_x = tk.DoubleVar(value=stored_number("background_pan_x", 0, -100, 100))
        pan_y = tk.DoubleVar(value=stored_number("background_pan_y", 0, -100, 100))

        value_labels = {}

        def add_slider(parent, title, variable, start, end, formatter, step=None):
            row = tk.Frame(parent, bg=PANEL)
            row.pack(fill="x", pady=(5, 0))
            tk.Label(row, text=title, bg=PANEL, fg=MUTED).pack(side="left")
            value = tk.Label(row, text=formatter(), bg=PANEL, fg=INK, width=7, anchor="e")
            value.pack(side="right")
            scale = ModernScale(parent, from_=start, to=end, variable=variable, step=step)
            modern_scales.append(scale)
            scale.pack(fill="x", pady=(0, 4))
            value_labels[title] = (value, formatter)
            return value

        add_slider(wallpaper_section, "Opacidade", opacity, 0, 1, lambda: f"{round(opacity.get() * 100)}%", 0.01)
        add_slider(wallpaper_section, "Desfoque", blur, 0, 32, lambda: f"{round(blur.get())} px", 1)
        add_slider(wallpaper_section, "Zoom", zoom, 50, 300, lambda: f"{round(zoom.get())}%", 5)
        add_slider(wallpaper_section, "Deslocamento horizontal", pan_x, -100, 100, lambda: f"{round(pan_x.get())}%", 1)
        add_slider(wallpaper_section, "Deslocamento vertical", pan_y, -100, 100, lambda: f"{round(pan_y.get())}%", 1)

        position_buttons = tk.Frame(wallpaper_section, bg=PANEL)
        position_buttons.pack(fill="x", pady=(8, 0))

        def paint_position_preview():
            if not background_preview.winfo_exists():
                return
            width = max(1, background_preview.winfo_width())
            height = max(1, background_preview.winfo_height())
            source = preview_state["source"]
            background_preview.delete("all")
            if source is None:
                path = bg_path.get().strip()
                if not path:
                    message = "Escolha uma imagem para ver a prévia."
                elif not _safe_is_file(path):
                    message = "A imagem selecionada não foi encontrada."
                elif getattr(background_preview, "_galeria_preview_loading", False):
                    message = "Carregando prévia…"
                else:
                    detail = getattr(background_preview, "_galeria_preview_error_message", None)
                    message = f"Falha ao carregar imagem: {detail}" if detail else "Não foi possível carregar esta imagem. Escolha outra."
                background_preview.create_text(
                    width // 2,
                    height // 2,
                    text=message,
                    fill=MUTED,
                    font=("Inter", 9),
                    width=max(100, width - 32),
                    justify="center",
                )
                return
            fitted = ImageOps.contain(source, (width, height), method=Image.Resampling.LANCZOS)
            scale = max(0.5, min(3.0, zoom.get() / 100))
            scaled = fitted.resize(
                (max(1, round(fitted.width * scale)), max(1, round(fitted.height * scale))),
                Image.Resampling.LANCZOS,
            )
            image = Image.new("RGB", (width, height), BG)
            x = round((width - scaled.width) / 2 - pan_x.get() * width / 200)
            y = round((height - scaled.height) / 2 - pan_y.get() * height / 200)
            image.paste(scaled, (x, y))
            base_rgb = tuple(int(BG[index:index + 2], 16) for index in (1, 3, 5))
            image = Image.blend(Image.new("RGB", (width, height), base_rgb), image, max(0.0, min(1.0, opacity.get())))
            blur_now = max(0, min(32, round(blur.get())))
            if blur_now:
                image = image.filter(ImageFilter.GaussianBlur(blur_now))
            ref = ImageTk.PhotoImage(image, master=background_preview)
            background_preview._galeria_preview_ref = ref
            background_preview.create_image(0, 0, image=ref, anchor="nw")
            background_preview.create_text(10, 10, text="Arraste para reposicionar", fill="#ffffff", anchor="nw", font=("Inter", 8, "bold"))

        def request_position_preview():
            path = bg_path.get().strip()
            if path == preview_state["path"]:
                return
            preview_state["path"] = path
            preview_state["generation"] += 1
            generation = preview_state["generation"]
            background_preview._galeria_preview_generation = generation
            preview_state["source"] = None
            background_preview._galeria_preview_error = False
            background_preview._galeria_preview_error_message = None
            background_preview._galeria_preview_loading = False
            if not path or not _safe_is_file(path):
                return
            background_preview._galeria_preview_loading = True
            try:
                future = self.background_executor.submit(self._decode_preview_image, path, (900, 270))
            except RuntimeError:
                background_preview._galeria_preview_loading = False
                background_preview._galeria_preview_error = True
                return

            def deliver_preview(done, target=background_preview, gen=generation):
                try:
                    image = done.result()
                except Exception as exc:
                    self.worker_queue.put(("background_preview_error", target, gen, str(exc)))
                else:
                    self.worker_queue.put(("background_preview", target, gen, image))

            future.add_done_callback(deliver_preview)

        def begin_preview_drag(event):
            preview_state["drag"] = (event.x, event.y)

        def move_preview_drag(event):
            previous = preview_state.get("drag")
            if previous is None:
                return
            width = max(1, background_preview.winfo_width())
            height = max(1, background_preview.winfo_height())
            pan_x.set(max(-100, min(100, pan_x.get() - (event.x - previous[0]) * 200 / width)))
            pan_y.set(max(-100, min(100, pan_y.get() - (event.y - previous[1]) * 200 / height)))
            preview_state["drag"] = (event.x, event.y)

        def preview_zoom(event):
            zoom.set(max(50, min(300, zoom.get() + (5 if event.delta > 0 else -5))))
            return "break"

        background_preview.bind("<ButtonPress-1>", begin_preview_drag)
        background_preview.bind("<B1-Motion>", move_preview_drag)
        background_preview.bind("<ButtonRelease-1>", lambda _event: preview_state.update(drag=None))
        background_preview.bind("<MouseWheel>", preview_zoom)
        background_preview.bind("<Configure>", lambda _event: paint_position_preview())

        def redraw_loaded_preview():
            preview_state["source"] = getattr(background_preview, "_galeria_source_image", None)
            paint_position_preview()

        background_preview._galeria_redraw_preview = redraw_loaded_preview

        def update_wallpaper_info():
            path = bg_path.get().strip()
            if not path:
                wallpaper_info.set("Nenhuma imagem selecionada")
                return
            selected = Path(path)
            wallpaper_info.set(f"{selected.name}\n{selected.parent}")

        def preview_background(*_args):
            for value, formatter in value_labels.values():
                value.configure(text=formatter())
            update_wallpaper_info()
            self._preview_background_path = bg_path.get().strip()
            self._preview_adaptive_colors = adaptive.get()
            self._preview_accent = accent.get()
            preview_options = {
                "opacity": max(0.0, min(1.0, opacity.get())),
                "blur": max(0, min(32, round(blur.get()))),
                "zoom": max(50, min(300, zoom.get())),
                "pan_x": max(-100, min(100, pan_x.get())),
                "pan_y": max(-100, min(100, pan_y.get())),
            }
            self._preview_background_options = preview_options
            request_position_preview()
            paint_position_preview()
            self._queue_background_preview(**preview_options)

        def center_background():
            pan_x.set(0)
            pan_y.set(0)

        def fit_background():
            zoom.set(100)
            pan_x.set(0)
            pan_y.set(0)

        self._pill_button(position_buttons, "Centralizar", center_background, width=126).pack(side="left")
        self._pill_button(position_buttons, "Ajustar à janela", fit_background, width=160).pack(side="left", padx=(8, 0))

        def preview_theme(*_args):
            selected_theme = {"Claro": "light", "Escuro": "dark", "Capri": "capri"}.get(theme.get(), "dark")
            if self.theme != selected_theme:
                self.theme = selected_theme
                self.apply_theme()
            refresh_appearance_controls()
            preview_background()

        def preview_accent(*_args):
            try:
                accent_swatch.itemconfigure(accent_swatch_id, fill=accent.get())
            except tk.TclError:
                pass
            accent_value.configure(text=accent.get().upper())
            if adaptive.get():
                return
            if self.accent != accent.get():
                self.accent = accent.get()
                self.apply_theme()
            refresh_appearance_controls()
            preview_background()

        def preview_adaptive(*_args):
            sync_accent_mode()
            if not adaptive.get() and self.accent != accent.get():
                self.accent = accent.get()
                self.apply_theme()
            refresh_appearance_controls()
            preview_background()

        for variable in (theme, accent, adaptive, bg_path, opacity, blur, zoom, pan_x, pan_y):
            callback = preview_theme if variable is theme else preview_accent if variable is accent else preview_adaptive if variable is adaptive else preview_background
            variable.trace_add("write", callback)

        def cancel_background_preview():
            self._cancel_background_preview()
            self._preview_background_path = None
            self._preview_adaptive_colors = None
            self._preview_accent = None
            self._preview_background_options = None

        dest_section = create_section(
            "Pasta dos álbuns",
            "Define onde os arquivos dos álbuns serão organizados. Fotos existentes só são movidas após sua confirmação.",
        )
        dest = tk.StringVar(value=self.destination)
        dest_info = tk.StringVar(value=dest.get() or "Nenhuma pasta selecionada")
        tk.Label(
            dest_section,
            textvariable=dest_info,
            bg=PANEL,
            fg=INK,
            anchor="w",
            justify="left",
            wraplength=500,
            font=("Inter", 9),
        ).pack(fill="x", pady=(0, 8))
        dest_row = tk.Frame(dest_section, bg=PANEL)
        dest_row.pack(fill="x")

        def update_destination_info(*_args):
            dest_info.set(dest.get().strip() or "Nenhuma pasta selecionada")

        def choose_destination():
            chosen = filedialog.askdirectory(parent=popup, title="Escolha a pasta dos álbuns")
            if chosen:
                dest.set(chosen)

        self._pill_button(dest_row, "Escolher pasta…", choose_destination, width=164).pack(side="left")
        self._pill_button(dest_row, "Limpar", lambda: dest.set(""), width=94).pack(side="left", padx=(8, 0))
        dest.trace_add("write", update_destination_info)

        def save():
            if self._library_move_busy:
                self._notify("Aguarde a movimentação de fotos terminar antes de salvar as preferências.")
                return
            selected_background = bg_path.get().strip()
            if selected_background:
                if not _safe_is_file(selected_background):
                    messagebox.showerror(
                        "Imagem indisponível",
                        "A imagem selecionada não foi encontrada. Escolha outra imagem ou remova o fundo.",
                        parent=popup,
                    )
                    return
                if getattr(background_preview, "_galeria_preview_loading", False):
                    messagebox.showwarning("Prévia em andamento", "Aguarde a imagem terminar de carregar antes de salvar.", parent=popup)
                    return
                if getattr(background_preview, "_galeria_preview_error", False) or preview_state["source"] is None:
                    messagebox.showerror(
                        "Imagem incompatível",
                        "Não foi possível carregar esta imagem. Escolha outro arquivo antes de salvar.",
                        parent=popup,
                    )
                    return

            selected_destination = dest.get().strip()
            previous_destination = self.destination
            albums = self.store.albums() if selected_destination else []
            if selected_destination:
                try:
                    Path(selected_destination).mkdir(parents=True, exist_ok=True)
                except OSError as exc:
                    messagebox.showerror(
                        "Pasta indisponível",
                        f"Não foi possível criar ou acessar a pasta escolhida:\n{exc}",
                        parent=popup,
                    )
                    return

            move_existing = False
            if selected_destination and previous_destination != selected_destination and albums:
                move_existing = messagebox.askyesno(
                    "Mover álbuns existentes?",
                    "Deseja mover agora as fotos dos álbuns existentes para a nova pasta? "
                    "Fotos que pertencem a mais de um álbum permanecem no local atual.",
                    parent=popup,
                )

            if selected_destination and not move_existing:
                try:
                    for album_id, name, _face_group, _count, _cover, _created in albums:
                        safe_name = "".join("_" if c in '<>:"/\\|?*' or ord(c) < 32 else c for c in name).strip(" .")
                        safe_name = safe_name or f"Album {album_id}"
                        (Path(selected_destination) / safe_name).mkdir(parents=True, exist_ok=True)
                except OSError as exc:
                    messagebox.showerror(
                        "Falha ao preparar a pasta",
                        f"Não foi possível preparar as pastas dos álbuns:\n{exc}",
                        parent=popup,
                    )
                    return

            selected_theme = {"Claro": "light", "Escuro": "dark", "Capri": "capri"}.get(theme.get(), "dark")
            selected_accent = accent.get()
            selected_ui_scale = self._normalize_ui_scale(ui_scale.get().rstrip("%"))
            selected_sidebar_width = self._normalize_sidebar_width(sidebar_width.get())
            selected_gallery_density = self._normalize_gallery_density(density_values.get(density.get(), "comfortable"))
            selected_photo_corner_radius = self._normalize_photo_corner_radius(radius.get())
            settings = {
                "theme": selected_theme,
                "accent": selected_accent,
                "album_destination": selected_destination,
                "initial_setup_complete": "1",
                "ui_scale": str(selected_ui_scale),
                "sidebar_width": str(selected_sidebar_width),
                "gallery_density": selected_gallery_density,
                "photo_corner_radius": str(selected_photo_corner_radius),
                "background": selected_background,
                "background_opacity": f"{max(0.0, min(1.0, opacity.get())):.2f}",
                "background_blur": str(max(0, min(32, round(blur.get())))),
                "background_zoom": str(max(50, min(300, round(zoom.get())))),
                "background_pan_x": str(max(-100, min(100, round(pan_x.get())))),
                "background_pan_y": str(max(-100, min(100, round(pan_y.get())))),
                "adaptive_colors": "1" if adaptive.get() else "0",
            }
            try:
                self.store.set_settings(settings)
            except sqlite3.Error as exc:
                messagebox.showerror("Falha ao salvar preferências", str(exc), parent=popup)
                return

            self.theme = selected_theme
            self.destination = selected_destination
            self._apply_ui_scale(selected_ui_scale)
            self._apply_sidebar_width(selected_sidebar_width)
            gallery_changed = (
                original_gallery_density != selected_gallery_density
                or original_photo_corner_radius != selected_photo_corner_radius
            )
            self.gallery_density = selected_gallery_density
            self.photo_corner_radius = selected_photo_corner_radius
            if not adaptive.get():
                self.accent = selected_accent

            cancel_background_preview()
            cancel_gallery_preview()
            popup.destroy()
            self._apply_background()
            if gallery_changed:
                self._refresh_photos()
            if move_existing:
                self._start_existing_album_reorganization(albums, selected_destination)
            else:
                self._info("Preferências salvas", "Tema, fundo e pasta de destino foram atualizados.")

        def cancel_preferences(_event=None):
            cancel_background_preview()
            cancel_gallery_preview()
            self.theme = original_theme
            self.accent = original_accent
            self._apply_ui_scale(original_ui_scale)
            self._apply_sidebar_width(original_sidebar_width)
            gallery_changed = (
                self.gallery_density != original_gallery_density
                or self.photo_corner_radius != original_photo_corner_radius
            )
            self.gallery_density = original_gallery_density
            self.photo_corner_radius = original_photo_corner_radius
            popup.destroy()
            self.apply_theme()
            self._apply_background()
            if gallery_changed:
                self._refresh_photos()

        self._pill_button(footer, "Cancelar", cancel_preferences, width=112).pack(side="right", padx=(8, 22), pady=16)
        self._pill_button(footer, "Salvar alterações", save, primary=True, width=174).pack(side="right", pady=16)
        popup.protocol("WM_DELETE_WINDOW", cancel_preferences)
        popup.bind("<Escape>", cancel_preferences)
        popup.bind("<Control-Return>", lambda _event: save())
        preview_background()


    def apply_theme(self):
        self._restore_wallpaper_surfaces()
        global BG, PANEL, INK, MUTED, GREEN, LIME, LINE
        previous = (BG, PANEL, INK, MUTED, GREEN, LIME, LINE)
        palettes = {
            "dark": (
                CAPRI_DARK_BG,
                CAPRI_DARK_PANEL,
                CAPRI_TEXT,
                CAPRI_MUTED,
                CAPRI_GREEN,
                self.accent,
                CAPRI_DARK_LINE,
            ),
            "light": (
                CAPRI_LIGHT_BG,
                CAPRI_LIGHT_PANEL,
                CAPRI_LIGHT_INK,
                CAPRI_LIGHT_MUTED,
                CAPRI_LIGHT_GREEN,
                self.accent,
                CAPRI_LIGHT_LINE,
            ),
            "capri": (
                CAPRI_THEME_BG,
                CAPRI_THEME_PANEL,
                CAPRI_THEME_TEXT,
                CAPRI_THEME_MUTED,
                CAPRI_THEME_GREEN,
                self.accent,
                CAPRI_THEME_LINE,
            ),
        }
        colors = palettes.get(self.theme, palettes["dark"])
        old_map = dict(zip(previous, colors))
        old_map.update({
            "#242424": colors[0],
            "#292929": colors[0],
            "#333333": colors[1],
            "#202b24": self._blend_color(colors[1], colors[5], 0.22),
            "#e9ecdf": colors[1],
            "#edf0e9": colors[1],
            "#edf0e6": colors[1],
            "#bdd071": self.accent,
        })
        last_accent = getattr(self, "_last_applied_accent", None)
        if last_accent and last_accent not in old_map:
            old_map[last_accent] = self.accent
        BG, PANEL, INK, MUTED, GREEN, LIME, LINE = colors
        self._last_applied_accent = self.accent

        def recolor(widget):
            for option in ("background", "bg", "foreground", "fg", "highlightbackground", "activebackground"):
                try:
                    value = widget.cget(option)
                    if value in old_map:
                        widget.configure(**{option: old_map[value]})
                except (tk.TclError, TypeError):
                    pass
            for child in widget.winfo_children():
                recolor(child)

        recolor(self)
        self.configure(bg=BG)
        self._build_styles()
        self._refresh_rounded_buttons()
        self.after_idle(self._apply_dark_titlebar)
        self.refresh_all()
        pending = getattr(self, "_theme_background_after_id", None)
        if pending:
            try:
                self.after_cancel(pending)
            except tk.TclError:
                pass
        self._theme_background_after_id = self.after(100, self._finish_theme_redraw)

    def _finish_theme_redraw(self):
        self._theme_background_after_id = None
        try:
            self.update_idletasks()
            self._apply_background()
        except tk.TclError:
            pass

    def _queue_background_preview(self, opacity, blur, zoom=100, pan_x=0, pan_y=0):
        if getattr(self, "_background_preview_id", None):
            try:
                self.after_cancel(self._background_preview_id)
            except tk.TclError:
                pass

        def apply_preview():
            self._background_preview_id = None
            self._apply_background(
                opacity_override=opacity,
                blur_override=blur,
                zoom_override=zoom,
                pan_x_override=pan_x,
                pan_y_override=pan_y,
            )

        self._background_preview_id = self.after(120, apply_preview)

    def _cancel_background_preview(self):
        preview_id = getattr(self, "_background_preview_id", None)
        if preview_id is None:
            return
        try:
            self.after_cancel(preview_id)
        except tk.TclError:
            pass
        self._background_preview_id = None

    def _restore_wallpaper_surfaces(self):
        was_transparent = self._wallpaper_transparent
        for widget, original in list(getattr(self, "_wallpaper_originals", {}).items()):
            try:
                if widget.winfo_exists():
                    widget.configure(bg=original)
            except tk.TclError:
                pass
        self._wallpaper_originals.clear()
        for widget, (foreground, font) in list(self._wallpaper_text_styles.items()):
            try:
                if widget.winfo_exists():
                    widget.configure(fg=foreground, font=font)
            except tk.TclError:
                pass
        self._wallpaper_text_styles.clear()
        self._wallpaper_surface_tree_initialized = False
        if getattr(self, "_wallpaper_transparent", False):
            try:
                self.wm_attributes("-transparentcolor", "")
            except tk.TclError:
                pass
        self._wallpaper_transparent = False
        if was_transparent:
            self._refresh_rounded_buttons()
            self._refresh_wallpaper_thumbnails()

    def _enable_wallpaper_surfaces(self, roots=None):
        if not getattr(self, "_wallpaper_enabled", False):
            return
        was_transparent = self._wallpaper_transparent
        full_scan = roots is None
        if full_scan:
            if getattr(self, "_wallpaper_surface_tree_initialized", False):
                return
            roots = (self.shell,)
        else:
            roots = tuple(roots)
        if not hasattr(self, "_wallpaper_text_styles"):
            self._wallpaper_text_styles = {}
        if full_scan:
            for widget in tuple(self._wallpaper_originals):
                try:
                    if not widget.winfo_exists():
                        self._wallpaper_originals.pop(widget, None)
                except tk.TclError:
                    self._wallpaper_originals.pop(widget, None)
            for widget in tuple(self._wallpaper_text_styles):
                try:
                    if not widget.winfo_exists():
                        self._wallpaper_text_styles.pop(widget, None)
                except tk.TclError:
                    self._wallpaper_text_styles.pop(widget, None)
        transparent = "#010203"
        try:
            self.wm_attributes("-transparentcolor", transparent)
        except tk.TclError:
            return
        if full_scan:
            self._wallpaper_surface_tree_initialized = True
        self._wallpaper_transparent = True
        self.configure(bg=transparent)
        surfaces = {BG, PANEL}

        def visit(widget, keep_surface=False):
            keep_surface = keep_surface or getattr(widget, "_galeria_keep_surface", False)
            try:
                widget_class = widget.winfo_class()
                if not keep_surface:
                    if widget_class == "Label":
                        self._wallpaper_text_styles.setdefault(
                            widget, (widget.cget("fg"), widget.cget("font"))
                        )
                        font = tkfont.Font(root=self, font=widget.cget("font"))
                        widget.configure(
                            fg="#ffffff",
                            font=(font.actual("family"), font.actual("size"), "bold"),
                        )
                    if widget_class in {"Frame", "Label", "Canvas"}:
                        current = widget.cget("bg")
                        if current in surfaces and current != transparent:
                            self._wallpaper_originals[widget] = current
                            widget.configure(bg=transparent)
            except tk.TclError:
                pass
            for child in widget.winfo_children():
                visit(child, keep_surface)

        for root in roots:
            if root is not None:
                visit(root)
        cards_to_refresh = tuple(getattr(self, "cards", {}).values()) if full_scan else roots
        for card in cards_to_refresh:
            if getattr(card, "_galeria_always_outline", False):
                self._refresh_card_feedback(card)
        if not was_transparent:
            self._refresh_rounded_buttons()
            self._refresh_wallpaper_thumbnails()

    def _wallpaper_accent(self, image):
        try:
            sample = image.convert("RGB").resize((48, 48), Image.Resampling.BILINEAR).quantize(colors=8).convert("RGB")
            colors = sample.getcolors(48 * 48) or []
            candidates = []
            for count, color in colors:
                high, low = max(color), min(color)
                saturation = (high - low) / max(1, high)
                luminance = (color[0] * 299 + color[1] * 587 + color[2] * 114) / 1000
                if saturation >= 0.24 and 45 <= luminance <= 235:
                    candidates.append((count * saturation, color))
            color = max(candidates, default=(0, (30, 215, 96)), key=lambda item: item[0])[1]
            average = max(1, sum(color) / 3)
            factor = max(0.72, min(1.38, 184 / average))
            return "#" + "".join(f"{max(0, min(255, round(channel * factor))):02x}" for channel in color)
        except Exception:
            return self.store.setting("accent", LIME)

    @staticmethod
    def _compose_wallpaper(source, width, height, opacity, blur, zoom, pan_x, pan_y, left_width, base_color):
        fitted = ImageOps.contain(source, (width, height), method=Image.Resampling.LANCZOS)
        scaled = fitted.resize(
            (max(1, round(fitted.width * zoom)), max(1, round(fitted.height * zoom))),
            Image.Resampling.LANCZOS,
        )
        positioned = Image.new("RGB", (width, height), base_color)
        x = round((width - scaled.width) / 2 - pan_x * width / 200)
        y = round((height - scaled.height) / 2 - pan_y * height / 200)
        positioned.paste(scaled, (x, y))
        base = Image.new("RGB", (width, height), base_color)
        composed = Image.blend(base, positioned, opacity)
        if blur:
            composed = composed.filter(ImageFilter.GaussianBlur(blur))
        left_width = max(0, min(width, left_width))
        if left_width:
            sidebar = Image.blend(base.crop((0, 0, left_width, height)), positioned.crop((0, 0, left_width, height)), opacity)
            sidebar = sidebar.filter(ImageFilter.GaussianBlur(min(40, blur + 9)))
            sidebar = Image.blend(sidebar, Image.new("RGB", sidebar.size, (16, 19, 24)), 0.22)
            composed.paste(sidebar, (0, 0))
        return composed

    def _show_wallpaper_image(self, render_key, composed):
        try:
            if not self.winfo_exists() or self.state() == "iconic":
                return
            self.background_ref = ImageTk.PhotoImage(composed, master=self.background_layer)
            self.background_layer.configure(image=self.background_ref, bg="#010203")
            width, height = render_key[1], render_key[2]
            x, y = self.winfo_rootx(), self.winfo_rooty()
            self.background_window.geometry(f"{width}x{height}+{x}+{y}")
            self.background_window.deiconify()
            self.background_window.lower(self)
            self._wallpaper_enabled = True
            self._enable_wallpaper_surfaces()
            self._wallpaper_render_key = render_key
        except (OSError, ValueError, tk.TclError):
            self._wallpaper_enabled = False
            self._wallpaper_render_key = None
            self._restore_wallpaper_surfaces()
            if hasattr(self, "background_window"):
                try:
                    self.background_window.withdraw()
                except tk.TclError:
                    pass

    def _apply_background(
        self,
        path_override=None,
        opacity_override=None,
        blur_override=None,
        zoom_override=None,
        pan_x_override=None,
        pan_y_override=None,
    ):
        if not hasattr(self, "store") or not hasattr(self, "shell"):
            return
        preview_path = getattr(self, "_preview_background_path", None)
        path = path_override if path_override is not None else (
            preview_path if preview_path is not None else self.store.setting("background", "")
        )
        adaptive_preview = getattr(self, "_preview_adaptive_colors", None)
        adaptive = adaptive_preview if adaptive_preview is not None else (self.store.setting("adaptive_colors", "0") == "1")
        if not path or not _safe_is_file(path):
            self._adaptive_wallpaper_path = ("", adaptive)
            self._wallpaper_render_key = None
            self._wallpaper_pending_key = None
            self._wallpaper_load_key = None
            for attr in ("_wallpaper_future", "_wallpaper_load_future"):
                future = getattr(self, attr, None)
                if future is not None:
                    future.cancel()
            preview_accent = getattr(self, "_preview_accent", None)
            base_accent = preview_accent if preview_accent is not None else self.store.setting("accent", LIME)
            if self.accent != base_accent:
                self.accent = base_accent
                self.apply_theme()
                return
            self._wallpaper_enabled = False
            self._restore_wallpaper_surfaces()
            if hasattr(self, "background_window"):
                try:
                    if self.background_window.winfo_exists():
                        self.background_window.withdraw()
                except tk.TclError:
                    pass
            self.configure(bg=BG)
            return
        try:
            self.update_idletasks()
            width = max(1, self.winfo_width())
            height = max(1, self.winfo_height())
            preview_options = getattr(self, "_preview_background_options", None) or {}
            try:
                opacity = float(
                    opacity_override
                    if opacity_override is not None
                    else preview_options.get("opacity", self.store.setting("background_opacity", "0.42"))
                )
            except (TypeError, ValueError):
                opacity = 0.42
            try:
                blur = int(
                    float(
                        blur_override
                        if blur_override is not None
                        else preview_options.get("blur", self.store.setting("background_blur", "8"))
                    )
                )
            except (TypeError, ValueError):
                blur = 8
            try:
                zoom = float(
                    zoom_override
                    if zoom_override is not None
                    else preview_options.get("zoom", self.store.setting("background_zoom", "100"))
                ) / 100
            except (TypeError, ValueError):
                zoom = 1.0
            try:
                pan_x = float(
                    pan_x_override
                    if pan_x_override is not None
                    else preview_options.get("pan_x", self.store.setting("background_pan_x", "0"))
                )
            except (TypeError, ValueError):
                pan_x = 0.0
            try:
                pan_y = float(
                    pan_y_override
                    if pan_y_override is not None
                    else preview_options.get("pan_y", self.store.setting("background_pan_y", "0"))
                )
            except (TypeError, ValueError):
                pan_y = 0.0
            opacity = max(0.0, min(1.0, opacity))
            blur = max(0, min(32, blur))
            zoom = max(0.5, min(3.0, zoom))
            pan_x = max(-100, min(100, pan_x))
            pan_y = max(-100, min(100, pan_y))
            stat = Path(path).stat()
            signature = (_safe_resolve(path), stat.st_mtime_ns, stat.st_size)
            cache = self._wallpaper_image_cache
            if cache and cache[0] == signature:
                source = cache[1]
            else:
                if getattr(self, "_wallpaper_load_key", None) != signature:
                    old_future = getattr(self, "_wallpaper_load_future", None)
                    if old_future is not None:
                        old_future.cancel()
                    self._wallpaper_load_key = signature
                    future = self.background_executor.submit(self._open_image, path)
                    self._wallpaper_load_future = future

                    def deliver_source(done, key=signature):
                        try:
                            image = done.result()
                        except Exception:
                            image = None
                        self.worker_queue.put(("wallpaper_source", key, image))

                    future.add_done_callback(deliver_source)
                return
            adaptive_key = (_safe_resolve(path), adaptive)
            preview_accent = getattr(self, "_preview_accent", None)
            manual_accent = preview_accent if preview_accent is not None else self.store.setting("accent", LIME)
            target_accent = self._wallpaper_accent(source) if adaptive else manual_accent
            if adaptive_key != self._adaptive_wallpaper_path or self.accent != target_accent:
                self._adaptive_wallpaper_path = adaptive_key
                if self.accent != target_accent:
                    self.accent = target_accent
                    self.apply_theme()
                    return
            if self.state() == "iconic":
                self._wallpaper_enabled = False
                self._restore_wallpaper_surfaces()
                self.background_window.withdraw()
                self.configure(bg=BG)
                return
            left_width = max(0, min(width, self.main.winfo_x()))
            render_key = (signature, width, height, opacity, blur, zoom, pan_x, pan_y, BG, left_width)
            if getattr(self, "_wallpaper_render_key", None) == render_key and self._wallpaper_enabled:
                x, y = self.winfo_rootx(), self.winfo_rooty()
                self.background_window.geometry(f"{width}x{height}+{x}+{y}")
                self.background_window.deiconify()
                self.background_window.lower(self)
                self._enable_wallpaper_surfaces()
                return
            if getattr(self, "_wallpaper_pending_key", None) == render_key:
                return
            previous_future = getattr(self, "_wallpaper_future", None)
            if previous_future is not None:
                previous_future.cancel()
            self._wallpaper_pending_key = render_key
            base_hex = BG.lstrip("#")
            base_color = tuple(int(base_hex[i:i + 2], 16) for i in (0, 2, 4))
            future = self.background_executor.submit(
                self._compose_wallpaper,
                source.copy(),
                width,
                height,
                opacity,
                blur,
                zoom,
                pan_x,
                pan_y,
                left_width,
                base_color,
            )
            self._wallpaper_future = future

            def deliver_render(done, key=render_key):
                try:
                    image = done.result()
                except Exception:
                    image = None
                self.worker_queue.put(("wallpaper_render", key, image))

            future.add_done_callback(deliver_render)
        except (OSError, ValueError, tk.TclError) as exc:
            self._wallpaper_enabled = False
            self._wallpaper_render_key = None
            self._restore_wallpaper_surfaces()
            if hasattr(self, "background_window"):
                try:
                    if self.background_window.winfo_exists():
                        self.background_window.withdraw()
                except tk.TclError:
                    pass
            if getattr(self, "_last_background_error", None) != str(exc):
                self._last_background_error = str(exc)
                self.folder_label.configure(text="Não foi possível abrir a imagem de fundo. Escolha outra em Aparência.")

    def set_album_cover(self):
        if self.active_album is None:
            return
        path = filedialog.askopenfilename(
            parent=self,
            title="Escolha a capa deste álbum",
            filetypes=[("Imagens", "*.jpg *.jpeg *.png *.webp *.bmp *.tif *.tiff *.heic *.heif *.avif")],
        )
        if not path:
            return
        resolved = _safe_resolve(path)
        if not _safe_is_file(resolved):
            self._notify("O arquivo escolhido para a capa não está mais disponível.")
            return
        with self.store._lock:
            self.store.db.execute("UPDATE albums SET cover=? WHERE id=?", (resolved, self.active_album))
            self.store.db.commit()
        self.refresh_all()
        self._info("Capa atualizada", "A capa deste álbum foi salva.")

    def face_options(self):
        albums = self.store.albums()
        reference_albums = [album for album in albums if album[3] > 0]
        if not albums:
            self._info("Crie um álbum primeiro", "Crie o álbum que receberá as fotos antes de iniciar a busca facial.", parent=self)
            return
        popup = tk.Toplevel(self)
        popup.title("Buscar por rosto")
        popup.configure(bg=BG)
        self._fade_in_window(popup)
        popup.geometry("650x620")
        popup.minsize(580, 560)
        popup.transient(self)
        popup.grab_set()
        tk.Label(popup, text="Buscar fotos de uma pessoa", bg=BG, fg=INK, font=("Inter", 18, "bold")).pack(anchor="w", padx=24, pady=(22, 4))
        tk.Label(
            popup,
            text="A análise roda neste computador. Você verá e aprovará cada foto antes de adicionar ao álbum.",
            bg=BG,
            fg=MUTED,
            wraplength=530,
            justify="left",
        ).pack(anchor="w", padx=24, pady=(0, 16))

        ref_mode = tk.StringVar(value="Foto de amostra")
        tk.Label(popup, text="Usar como referência", bg=BG, fg=MUTED).pack(anchor="w", padx=24)
        mode = self._rounded_dropdown(
            popup,
            textvariable=ref_mode,
            values=["Foto de amostra", "Fotos da biblioteca", "Álbum de referência"],
            width=40,
        )
        mode.pack(anchor="w", padx=24, pady=(4, 12))

        ref_names = [f"{a[1]} ({a[3]} fotos)" for a in reference_albums]
        ref_album = tk.StringVar(value=ref_names[0] if ref_names else "")
        tk.Label(popup, text="Álbum com fotos de referência", bg=BG, fg=MUTED).pack(anchor="w", padx=24)
        ref_combo = self._rounded_dropdown(popup, textvariable=ref_album, values=ref_names, state="disabled", width=40)
        ref_combo.pack(anchor="w", padx=24, pady=(4, 10))

        sample_path = tk.StringVar(value="Nenhuma foto selecionada")
        sample_row = tk.Frame(popup, bg=BG)
        sample_row.pack(fill="x", padx=24, pady=4)

        def choose_sample():
            chosen = filedialog.askopenfilename(
                parent=popup,
                title="Escolha uma foto de referência",
                filetypes=[("Imagens", "*.jpg *.jpeg *.png *.webp *.bmp *.tif *.tiff")],
            )
            if chosen:
                sample_path.set(chosen)

        sample_button = self._pill_button(sample_row, "Escolher foto…", choose_sample, width=148)
        sample_button.pack(side="left")
        tk.Label(sample_row, textvariable=sample_path, bg=BG, fg=MUTED, wraplength=345, anchor="w").pack(side="left", padx=10)

        library_refs = []
        library_row = tk.Frame(popup, bg=BG)
        library_row.pack(fill="x", padx=24, pady=(2, 4))
        library_button = self._pill_button(library_row, "Escolher fotos da biblioteca…", lambda: None, width=230, state="disabled")
        library_button.pack(side="left")
        library_label = tk.Label(library_row, text="Nenhuma foto selecionada", bg=BG, fg=MUTED, wraplength=345, anchor="w")
        library_label.pack(side="left", padx=10)

        def choose_library_photos():
            self._choose_reference_photos(
                popup,
                library_refs,
                lambda chosen: (library_refs.__setitem__(slice(None), chosen), library_label.configure(text=f"{len(chosen)} fotos selecionadas")),
            )

        library_button.configure(command=choose_library_photos)

        target_options = [f"{a[1]} ({a[3]} fotos)" for a in albums]
        target_name = tk.StringVar(value=target_options[0])
        tk.Label(popup, text="Adicionar resultados aprovados a", bg=BG, fg=MUTED).pack(anchor="w", padx=24, pady=(18, 4))
        self._rounded_dropdown(popup, textvariable=target_name, values=target_options, width=40).pack(anchor="w", padx=24)

        def update_reference_widgets(*_args):
            use_album = ref_mode.get() == "Álbum de referência"
            use_library = ref_mode.get() == "Fotos da biblioteca"
            ref_combo.configure(state="readonly" if use_album and reference_albums else "disabled")
            sample_button_state = "normal" if not use_album and not use_library else "disabled"
            sample_button.configure(state=sample_button_state)
            library_button.configure(state="normal" if use_library else "disabled")

        ref_mode.trace_add("write", update_reference_widgets)

        def start():
            if ref_mode.get() == "Foto de amostra":
                if sample_path.get() == "Nenhuma foto selecionada":
                    self._info("Escolha uma foto", "Escolha a foto que será usada como amostra.", parent=popup)
                    return
                refs = [sample_path.get()]
                label = Path(refs[0]).stem
            elif ref_mode.get() == "Fotos da biblioteca":
                refs = [path for path in library_refs if _safe_is_file(path)]
                if not refs:
                    self._info("Escolha fotos", "Selecione uma ou mais fotos da biblioteca para usar como referência.", parent=popup)
                    return
                label = f"{len(refs)} fotos da biblioteca"
            else:
                if not reference_albums:
                    self._info("Sem álbum de referência", "Crie um álbum com pelo menos uma foto para usá-lo como referência.", parent=popup)
                    return
                ref_album_item = reference_albums[ref_names.index(ref_album.get())]
                refs = self.store.photos(album_id=ref_album_item[0])
                label = ref_album_item[1]
            target = albums[target_options.index(target_name.get())]
            if not messagebox.askyesno(
                "Permitir análise facial?",
                "Vou comparar os rostos localmente. Nenhuma foto será enviada. Nenhum álbum será criado e nada será adicionado até você aprovar os resultados. Continuar?",
                parent=popup,
            ):
                return
            popup.destroy()
            self.start_reference_scan(refs, label, target[0], target[1])

        self._pill_button(popup, "Buscar correspondências", start, primary=True, width=214).pack(anchor="e", padx=24, pady=22)
        popup.bind("<Escape>", lambda _e: popup.destroy())

    def _choose_reference_photos(self, parent, initial_paths, on_confirm):
        all_paths = [str(path) for path in self.store.photos() if _safe_is_file(path)]
        if not all_paths:
            self._info("Biblioteca vazia", "Adicione fotos à biblioteca antes de escolhê-las como referência.", parent=parent)
            return

        picker = tk.Toplevel(parent)
        picker.title("Escolher referências da biblioteca")
        picker.configure(bg=BG)
        self._fade_in_window(picker)
        picker.geometry("1040x760")
        picker.minsize(640, 540)
        picker.transient(parent)
        picker.grab_set()

        heading = tk.Frame(picker, bg=BG)
        heading.pack(fill="x", padx=22, pady=(18, 8))
        tk.Label(heading, text="Escolha fotos de referência", bg=BG, fg=INK, font=("Inter", 18, "bold")).pack(anchor="w")
        tk.Label(
            heading,
            text="Selecione uma ou várias fotos da sua biblioteca. A grade acompanha o tamanho desta janela.",
            bg=BG,
            fg=MUTED,
            font=("Inter", 9),
            wraplength=950,
            justify="left",
        ).pack(anchor="w", pady=(4, 0))

        tools = tk.Frame(picker, bg=BG)
        tools.pack(fill="x", padx=22, pady=(0, 10))
        query = tk.StringVar()
        tk.Label(tools, text="Buscar", bg=BG, fg=MUTED, font=("Inter", 9)).pack(side="left", padx=(0, 7))
        search = ttk.Entry(tools, textvariable=query, width=34)
        search.pack(side="left", fill="x", expand=True, padx=(0, 8))
        count = tk.Label(tools, text="0 selecionadas", bg=BG, fg=self.accent, font=("Inter", 9, "bold"))
        count.pack(side="left", padx=10)
        self._pill_button(tools, "Selecionar visíveis", lambda: select_visible(), width=142).pack(side="right", padx=4)
        self._pill_button(tools, "Limpar", lambda: clear_selected(), width=82).pack(side="right", padx=4)

        body = tk.Frame(picker, bg=BG)
        body.pack(fill="both", expand=True, padx=22)
        canvas = tk.Canvas(body, bg=BG, highlightthickness=0)
        scrollbar = SlimScrollbar(body, command=canvas.yview)
        scrollbar._galeria_area = canvas
        canvas._galeria_scrollbar = scrollbar
        canvas.configure(yscrollcommand=scrollbar.set)
        self._register_scroll_canvas(picker, canvas)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        inner = tk.Frame(canvas, bg=BG)
        window_id = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox(window_id)))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window_id, width=event.width))

        footer = tk.Frame(picker, bg=BG)
        footer.pack(fill="x", padx=22, pady=14)
        load_more_btn = self._pill_button(footer, "Carregar mais", lambda: load_more(), width=138)
        load_more_btn.pack(side="left")
        self._pill_button(footer, "Cancelar", picker.destroy, width=112).pack(side="right", padx=4)

        selected = set(initial_paths)
        state = {
            "filtered": all_paths,
            "limit": 100,
            "index": 0,
            "cards": {},
            "scope": f"reference_picker:{id(picker)}",
            "generation": 1,
            "thumb_size": None,
            "columns": 1,
            "layout_after": None,
            "empty": None,
            "building": False,
            "build_generation": 0,
            "filter_after": None,
        }
        self._thumbnail_scope_generations[state["scope"]] = state["generation"]

        def update_count():
            count.configure(text=f"{len(selected)} selecionadas")

        def style_card(path):
            record = state["cards"].get(path)
            if not record:
                return
            approved = path in selected
            record["card"]._galeria_selected = approved
            self._refresh_card_feedback(record["card"])
            record["card"].configure(bg=self._blend_color(PANEL, self.accent, 0.12) if approved else PANEL)
            record["status"].configure(
                text="✓ Selecionada" if approved else Path(path).stem[:26],
                fg=self.accent if approved else MUTED,
            )

        def toggle(path):
            if path in selected:
                selected.remove(path)
            else:
                selected.add(path)
            style_card(path)
            update_count()

        def clear_selected():
            selected.clear()
            for path in state["cards"]:
                style_card(path)
            update_count()

        def select_visible():
            selected.update(state["cards"])
            for path in state["cards"]:
                style_card(path)
            update_count()

        def dimensions():
            width = max(1, canvas.winfo_width())
            columns = max(1, min(7, width // 220))
            card_width = max(148, (width - (columns - 1) * 12) // columns)
            thumb_size = max(116, min(204, card_width - 18))
            return columns, card_width, thumb_size

        def apply_layout(reload_thumbnails=False):
            if not picker.winfo_exists():
                return
            columns, card_width, thumb_size = dimensions()
            changed_size = state["thumb_size"] is not None and thumb_size != state["thumb_size"]
            if changed_size:
                state["generation"] += 1
                self._thumbnail_scope_generations[state["scope"]] = state["generation"]
                self._cancel_thumbnail_scope(state["scope"])
                reload_thumbnails = True
            state["columns"] = columns
            for column in range(columns):
                inner.grid_columnconfigure(column, weight=1, uniform="reference-picker")
            for index, (path, record) in enumerate(state["cards"].items()):
                record["card"].grid(row=index // columns, column=index % columns, sticky="nsew", padx=(0, 12), pady=(0, 12))
                record["thumb_frame"].configure(width=thumb_size, height=thumb_size)
                record["caption"].configure(wraplength=card_width - 16)
                if reload_thumbnails or changed_size:
                    self._request_thumbnail(path, (thumb_size, thumb_size), record["image"], state["scope"], state["generation"])
            state["thumb_size"] = thumb_size
            inner.update_idletasks()
            bounds = canvas.bbox(window_id)
            canvas.configure(
                scrollregion=bounds if bounds else (0, 0, max(1, canvas.winfo_width()), max(1, canvas.winfo_height()))
            )

        def schedule_layout(_event=None):
            pending = state.get("layout_after")
            if pending:
                try:
                    picker.after_cancel(pending)
                except tk.TclError:
                    pass
            state["layout_after"] = picker.after(140, apply_layout)

        def create_card(path, index):
            columns, _card_width, thumb_size = dimensions()
            card = tk.Frame(inner, bg=PANEL, highlightbackground=LINE, highlightthickness=1, padx=8, pady=8, cursor="hand2")
            self._bind_card_feedback(card, outline=LINE, always_outline=True)
            card.grid(row=index // columns, column=index % columns, sticky="nsew", padx=(0, 12), pady=(0, 12))
            thumb_frame = tk.Frame(card, bg=PANEL, width=thumb_size, height=thumb_size)
            thumb_frame.pack(anchor="center")
            thumb_frame.pack_propagate(False)
            image = tk.Label(thumb_frame, text="…", bg=PANEL, fg=MUTED, bd=0, cursor="hand2", padx=0, pady=0)
            image.place(relx=0, rely=0, relwidth=1, relheight=1)
            caption = tk.Label(card, text=Path(path).stem[:34], bg=PANEL, fg=INK, font=("Inter", 9, "bold"), anchor="w", justify="left")
            caption.pack(fill="x", pady=(8, 2))
            status = tk.Label(card, text="", bg=PANEL, fg=MUTED, font=("Inter", 8), anchor="w")
            status.pack(fill="x")
            record = {"card": card, "thumb_frame": thumb_frame, "image": image, "caption": caption, "status": status}
            state["cards"][path] = record
            for widget in (card, thumb_frame, image, caption, status):
                widget.bind("<Button-1>", lambda _event, p=path: toggle(p))
            style_card(path)
            self._request_thumbnail(path, (thumb_size, thumb_size), image, state["scope"], state["generation"])

        def build_batch():
            if not picker.winfo_exists():
                return
            build_generation = state["build_generation"]
            state["building"] = True
            load_more_btn.configure(state="disabled")
            start = state["index"]
            visible = state["filtered"][:state["limit"]]
            stop = min(start + 24, len(visible))
            for index in range(start, stop):
                create_card(visible[index], index)
            state["index"] = stop
            apply_layout()
            update_count()
            if stop < len(visible):
                picker.after_idle(
                    lambda generation=build_generation: build_batch() if generation == state["build_generation"] else None
                )
            else:
                state["building"] = False
                load_more_btn.configure(
                    state="normal" if stop < len(state["filtered"]) else "disabled",
                    text=f"Carregar mais ({max(0, len(state['filtered']) - stop)})",
                )

        def rebuild(*_args):
            self._cancel_thumbnail_scope(state["scope"])
            state["generation"] += 1
            self._thumbnail_scope_generations[state["scope"]] = state["generation"]
            for child in inner.winfo_children():
                child.destroy()
            state["cards"].clear()
            if state["empty"] is not None:
                try:
                    state["empty"].destroy()
                except tk.TclError:
                    pass
                state["empty"] = None
            state["thumb_size"] = None
            state["index"] = 0
            state["building"] = False
            state["build_generation"] += 1
            search_text = query.get().strip().casefold()
            state["filtered"] = [path for path in all_paths if not search_text or search_text in Path(path).name.casefold()]
            state["limit"] = 100
            if not state["filtered"]:
                state["empty"] = tk.Label(inner, text="Nenhuma foto corresponde à busca.", bg=BG, fg=MUTED, font=("Inter", 11))
                state["empty"].grid(row=0, column=0, pady=45, sticky="nsew")
            build_batch()

        def schedule_rebuild(*_args):
            pending = state.get("filter_after")
            if pending:
                try:
                    picker.after_cancel(pending)
                except tk.TclError:
                    pass
            state["filter_after"] = picker.after(160, rebuild)

        def load_more():
            if state["building"] or state["index"] >= len(state["filtered"]):
                return
            state["limit"] += 100
            build_batch()

        def confirm():
            chosen = [path for path in all_paths if path in selected and _safe_is_file(path)]
            on_confirm(chosen)
            picker.destroy()

        query.trace_add("write", schedule_rebuild)
        canvas.bind("<Configure>", schedule_layout, add="+")
        picker.bind(
            "<Destroy>",
            lambda event: (
                self._cancel_thumbnail_scope(state["scope"]),
                self._thumbnail_scope_generations.pop(state["scope"], None),
            )
            if event.widget is picker
            else None,
            add="+",
        )
        self._pill_button(footer, "Usar fotos selecionadas", confirm, primary=True, width=210).pack(side="right", padx=4)
        update_count()
        build_batch()
        search.focus_set()

    def start_reference_scan(self, refs, label, target_id, target_name):
        if self._library_move_busy:
            self._notify("Aguarde a movimentação de fotos terminar.")
            return
        paths = self.store.photos()
        if not refs or not paths:
            self._info("Fotos insuficientes", "Adicione fotos à biblioteca e escolha uma referência válida.", parent=self)
            return
        self.configure(cursor="watch")
        self._show_progress("Preparando o reconhecimento facial…")
        threading.Thread(
            target=self._reference_worker,
            args=(paths, refs, label, target_id, target_name),
            daemon=True,
        ).start()

    def _reference_worker(self, paths, refs, label, target_id, target_name):
        try:
            import cv2
            import numpy as np
            from urllib.parse import urlparse

            MODEL_DIR.mkdir(parents=True, exist_ok=True)
            for name, url in MODELS.items():
                target = MODEL_DIR / name
                if target.exists() and target.stat().st_size > 1024:
                    continue
                parsed = urlparse(url)
                if parsed.scheme != "https" or parsed.hostname != "github.com":
                    raise RuntimeError("A origem do modelo facial não passou pela validação HTTPS.")
                temp = target.with_suffix(".download")
                request = urllib.request.Request(url, headers={"User-Agent": "Capri/1.0"})
                written = 0
                try:
                    with urllib.request.urlopen(request, timeout=30) as response, temp.open("wb") as output:
                        while chunk := response.read(1024 * 1024):
                            written += len(chunk)
                            if written > 100 * 1024 * 1024:
                                raise RuntimeError("O modelo facial excedeu o tamanho permitido de 100 MB.")
                            output.write(chunk)
                    if written < 1024:
                        raise RuntimeError("O download do modelo facial parece incompleto.")
                    temp.replace(target)
                finally:
                    if temp.exists():
                        temp.unlink()
            detector = cv2.FaceDetectorYN.create(str(MODEL_DIR / "yunet.onnx"), "", (320, 320), 0.75, 0.3, 5000)
            recognizer = cv2.FaceRecognizerSF.create(str(MODEL_DIR / "sface.onnx"), "")

            def features(path):
                try:
                    with _FILE_IO_LOCK:
                        encoded = np.fromfile(str(path), dtype=np.uint8)
                    image = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
                except (OSError, ValueError):
                    image = None
                if image is None:
                    return []
                h, w = image.shape[:2]
                if max(h, w) > 1800:
                    scale = 1800 / max(h, w)
                    image = cv2.resize(image, (int(w * scale), int(h * scale)))
                    h, w = image.shape[:2]
                detector.setInputSize((w, h))
                found = detector.detect(image)[1]
                if found is None:
                    return []
                out = []
                for face in found:
                    if float(face[-1]) < 0.8:
                        continue
                    feature = recognizer.feature(recognizer.alignCrop(image, face)).reshape(-1).astype("float32")
                    norm = float((feature @ feature) ** 0.5)
                    if norm:
                        out.append(feature / norm)
                return out

            reference = [f for p in refs for f in features(p)]
            matched = []
            for index, p in enumerate(paths, 1):
                current = features(p)
                if reference and any(max(float(ref @ face) for ref in reference) >= 0.42 for face in current):
                    matched.append(p)
                if index == 1 or index % 5 == 0 or index == len(paths):
                    self.worker_queue.put(("reference_progress", index, len(paths)))
            self.worker_queue.put(("reference_done", matched, label, target_id, target_name))
        except Exception as exc:
            self.worker_queue.put(("error", str(exc)))

    def review_face_matches(self, paths, target_id, target_name):
        paths = [path for path in paths if _safe_is_file(path)]
        if not paths:
            self._info("Nenhuma correspondência", "Não encontrei fotos com um rosto suficientemente parecido com a referência.", parent=self)
            return
        popup = tk.Toplevel(self)
        popup.title("Revisar fotos encontradas")
        popup.configure(bg=BG)
        self._fade_in_window(popup)
        popup.geometry("1100x780")
        popup.minsize(660, 540)
        popup.transient(self)
        popup.grab_set()
        heading = tk.Frame(popup, bg=BG)
        heading.pack(fill="x", padx=22, pady=(18, 6))
        tk.Label(heading, text=f"Revisar {len(paths)} fotos", bg=BG, fg=INK, font=("Inter", 19, "bold")).pack(anchor="w")
        tk.Label(
            heading,
            text=f"Clique nas imagens para aprovar ou rejeitar. Destino: {target_name}.",
            bg=BG,
            fg=MUTED,
            wraplength=1000,
            justify="left",
        ).pack(anchor="w", pady=(4, 0))

        tools = tk.Frame(popup, bg=BG)
        tools.pack(fill="x", padx=22, pady=(4, 10))
        selected = set()
        cards = {}
        count_label = tk.Label(tools, text="0 aprovadas", bg=BG, fg=self.accent, font=("Inter", 10, "bold"))
        count_label.pack(side="left")
        self._pill_button(tools, "Rejeitar todas", lambda: set_all(False), width=146).pack(side="right", padx=4)
        self._pill_button(tools, "Aprovar todas", lambda: set_all(True), primary=True, width=146).pack(side="right", padx=4)

        scroll_frame = tk.Frame(popup, bg=BG)
        scroll_frame.pack(fill="both", expand=True, padx=22)
        canvas = tk.Canvas(scroll_frame, bg=BG, highlightthickness=0)
        inner = tk.Frame(canvas, bg=BG)
        bar = SlimScrollbar(scroll_frame, command=canvas.yview)
        bar._galeria_area = canvas
        canvas._galeria_scrollbar = bar
        canvas.configure(yscrollcommand=bar.set)
        self._register_scroll_canvas(popup, canvas)
        bar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        window_id = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox(window_id)))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window_id, width=event.width))

        thumbnail_scope = f"face_review:{id(popup)}"
        render = {
            "generation": 1,
            "limit": 120,
            "index": 0,
            "thumb_size": None,
            "layout_after": None,
            "load_button": None,
            "building": False,
            "build_generation": 0,
        }
        self._thumbnail_scope_generations[thumbnail_scope] = render["generation"]

        def refresh_card(path):
            record = cards.get(path)
            if not record:
                return
            approved = path in selected
            card_bg = self._blend_color(PANEL, self.accent, 0.12) if approved else PANEL
            record["card"]._galeria_selected = approved
            self._refresh_card_feedback(record["card"])
            record["card"].configure(bg=card_bg)
            record["image_wrap"].configure(bg=card_bg)
            record["caption"].configure(bg=card_bg)
            record["status"].configure(
                bg=card_bg,
                fg=self.accent if approved else MUTED,
                text="Aprovada · clique para rejeitar" if approved else "Clique para aprovar",
            )
            record["badge"].configure(
                text="✓" if approved else "",
                bg=self.accent if approved else card_bg,
                fg="#101010" if approved else card_bg,
            )
            if approved:
                record["badge"].place(relx=1, x=-7, y=7, anchor="ne")
            else:
                record["badge"].place_forget()

        def toggle(path):
            if path in selected:
                selected.remove(path)
            else:
                selected.add(path)
            refresh_card(path)
            count_label.configure(text=f"{len(selected)} aprovadas")

        def set_all(value):
            selected.clear()
            if value:
                selected.update(paths)
            for path in tuple(cards):
                refresh_card(path)
            count_label.configure(text=f"{len(selected)} aprovadas")

        def dimensions():
            width = max(1, canvas.winfo_width())
            columns = max(1, min(7, width // 216))
            card_width = max(150, (width - (columns - 1) * 12) // columns)
            thumb_size = max(116, min(210, card_width - 20))
            return columns, card_width, thumb_size

        def layout_cards(reload_thumbnails=False):
            if not popup.winfo_exists():
                return
            columns, card_width, thumb_size = dimensions()
            changed = render["thumb_size"] is not None and render["thumb_size"] != thumb_size
            if changed:
                render["generation"] += 1
                self._thumbnail_scope_generations[thumbnail_scope] = render["generation"]
                self._cancel_thumbnail_scope(thumbnail_scope)
                reload_thumbnails = True
            for column in range(columns):
                inner.grid_columnconfigure(column, weight=1, uniform="face-review")
            for index, (path, record) in enumerate(cards.items()):
                record["card"].grid(row=index // columns, column=index % columns, sticky="nsew", padx=(0, 12), pady=(0, 12))
                record["image_wrap"].configure(width=thumb_size, height=thumb_size)
                record["caption"].configure(wraplength=card_width - 18)
                if reload_thumbnails or changed:
                    self._request_thumbnail(path, (thumb_size, thumb_size), record["image"], thumbnail_scope, render["generation"])
            render["thumb_size"] = thumb_size
            load_btn = render.get("load_button")
            if load_btn is not None:
                remaining = len(paths) - len(cards)
                if remaining > 0:
                    load_btn.configure(text=f"Carregar mais fotos ({remaining} restantes)")
                    load_btn.grid(row=(len(cards) + columns - 1) // columns, column=0, columnspan=columns, pady=12)
                else:
                    load_btn.grid_remove()
            inner.update_idletasks()
            bounds = canvas.bbox(window_id)
            canvas.configure(
                scrollregion=bounds if bounds else (0, 0, max(1, canvas.winfo_width()), max(1, canvas.winfo_height()))
            )

        def schedule_layout(_event=None):
            pending = render.get("layout_after")
            if pending:
                try:
                    popup.after_cancel(pending)
                except tk.TclError:
                    pass
            render["layout_after"] = popup.after(140, layout_cards)

        def create_card(path, index):
            columns, card_width, thumb_size = dimensions()
            card = tk.Frame(inner, bg=PANEL, highlightbackground=LINE, highlightthickness=1, padx=8, pady=8, cursor="hand2")
            self._bind_card_feedback(card, outline=LINE, always_outline=True)
            card.grid(row=index // columns, column=index % columns, sticky="nsew", padx=(0, 12), pady=(0, 12))
            image_wrap = tk.Frame(card, bg=PANEL, width=thumb_size, height=thumb_size)
            image_wrap.pack(anchor="center")
            image_wrap.pack_propagate(False)
            image_label = tk.Label(image_wrap, text="…", bg=PANEL, fg=MUTED, bd=0, cursor="hand2", padx=0, pady=0)
            image_label.place(relx=0, rely=0, relwidth=1, relheight=1)
            badge = tk.Label(image_wrap, text="", font=("Inter", 9, "bold"), padx=6, pady=2)
            caption = tk.Label(
                card, text=Path(path).name[:36], bg=PANEL, fg=INK, font=("Inter", 9, "bold"), anchor="w", justify="left", wraplength=card_width - 18
            )
            caption.pack(fill="x", pady=(8, 2))
            status = tk.Label(card, text="", bg=PANEL, fg=MUTED, font=("Inter", 8), anchor="w")
            status.pack(fill="x")
            record = {
                "card": card,
                "image_wrap": image_wrap,
                "image": image_label,
                "badge": badge,
                "caption": caption,
                "status": status,
            }
            cards[path] = record
            for widget in (card, image_wrap, image_label, badge, caption, status):
                widget.bind("<Button-1>", lambda _event, p=path: toggle(p))
            refresh_card(path)
            self._request_thumbnail(path, (thumb_size, thumb_size), image_label, thumbnail_scope, render["generation"])

        def load_more():
            if render["building"] or render["index"] >= len(paths):
                return
            render["limit"] += 120
            build_batch()

        render["load_button"] = self._pill_button(inner, "", load_more, width=260)

        def build_batch():
            if not popup.winfo_exists():
                return
            build_generation = render["build_generation"]
            render["building"] = True
            start = render["index"]
            stop = min(start + 24, render["limit"], len(paths))
            for index in range(start, stop):
                create_card(paths[index], index)
            render["index"] = stop
            layout_cards()
            if stop < min(render["limit"], len(paths)):
                popup.after_idle(
                    lambda generation=build_generation: build_batch() if generation == render["build_generation"] else None
                )
            else:
                render["building"] = False

        def confirm():
            approved = [path for path in paths if path in selected and _safe_is_file(path)]
            self.store.assign(target_id, approved)
            popup.destroy()
            self.refresh_all()
            self._info("Álbum atualizado", f"{len(approved)} fotos aprovadas foram adicionadas a “{target_name}”.")

        footer = tk.Frame(popup, bg=BG)
        footer.pack(fill="x", padx=22, pady=16)
        self._pill_button(footer, "Cancelar", popup.destroy, width=116).pack(side="right", padx=6)
        self._pill_button(footer, "Adicionar aprovadas", confirm, primary=True, width=196).pack(side="right", padx=6)
        canvas.bind("<Configure>", schedule_layout, add="+")
        popup.bind(
            "<Destroy>",
            lambda event: (
                self._cancel_thumbnail_scope(thumbnail_scope),
                self._thumbnail_scope_generations.pop(thumbnail_scope, None),
            )
            if event.widget is popup
            else None,
            add="+",
        )
        build_batch()

    def rename_album(self):
        if self.active_album is None:
            return
        name = self._ask_text("Renomear álbum", "Novo nome para este álbum:", initial=self.active_album_name, parent=self)
        if not name or not name.strip():
            return
        try:
            with self.store._lock:
                self.store.db.execute("UPDATE albums SET name=? WHERE id=?", (name.strip(), self.active_album))
                self.store.db.commit()
        except sqlite3.IntegrityError:
            messagebox.showerror("Nome já usado", "Já existe outro álbum com esse nome.")
            return
        self.active_album_name = name.strip()
        self.refresh_all()

    def remove_selection_from_album(self):
        if self.active_album is None or not self.selected:
            return
        self._remove_photos_from_current_album(list(self.selected))

    def _animate_thumbnail_fade(self, widget, frames, index, scope, generation):
        if not frames:
            return
        if scope == "photo_grid" and generation != self._photo_render_generation:
            return
        if scope == "album_browser" and generation != getattr(self, "_album_browser_generation", 0):
            return
        if scope == "sidebar" and generation != getattr(self, "_sidebar_render_generation", 0):
            return
        try:
            if not widget.winfo_exists():
                return
            widget._galeria_fade_after_id = None
            photo = self._wallpaper_photo_image(frames[index], master=self)
            self._set_thumbnail_widget_photo(widget, photo, scope)
            if index == len(frames) - 1:
                if scope == "photo_grid":
                    self.photo_refs.append(photo)
                elif scope == "album_browser":
                    self.album_refs.append(photo)
                elif scope == "sidebar":
                    self.sidebar_album_refs.append(photo)
                else:
                    self.review_refs.append(photo)
            else:
                widget._galeria_fade_after_id = widget.after(
                    16,
                    lambda: self._animate_thumbnail_fade(widget, frames, index + 1, scope, generation),
                )
        except tk.TclError:
            return

    def _set_thumbnail_widget_photo(self, widget, photo, scope):
        if scope == "photo_grid" and isinstance(widget, tk.Canvas):
            widget.delete("photo_loading")
            widget.delete("photo_error")
            items = widget.find_withtag("photo_thumbnail")
            if items:
                widget.itemconfigure(items[0], image=photo)
            else:
                image_id = widget.create_image(0, 0, image=photo, anchor="nw", tags=("photo_thumbnail",))
                widget.tag_lower(image_id)
            widget._galeria_photo_ref = photo
            card = widget.master.master
            if getattr(card, "_galeria_photo_card", False):
                self._refresh_photo_card_overlay(card)
            return
        widget.configure(image=photo, text="", width=0, height=0)
        widget._galeria_photo_ref = photo

    def _refresh_photo_card_overlay(self, card):
        canvas = getattr(card, "_galeria_thumb_canvas", None)
        if canvas is None:
            return
        try:
            if not canvas.winfo_exists():
                return
            width = max(1, canvas.winfo_width(), canvas.winfo_reqwidth())
            height = max(1, canvas.winfo_height(), canvas.winfo_reqheight())
            items = getattr(canvas, "_galeria_overlay_items", None)
            if items is None:
                items = {
                    "shade": canvas.create_rectangle(0, 0, width, height, fill="#11141b", outline="", state="hidden", tags=("photo_overlay",)),
                    "name": canvas.create_text(8, height - 6, anchor="sw", fill="#ffffff", font=("Inter", 8, "bold"), state="hidden", tags=("photo_overlay",)),
                    "marker": canvas.create_oval(width - 27, 5, width - 7, 25, fill="#11141b", outline="#ffffff", width=1, state="hidden", tags=("photo_overlay",)),
                    "check": canvas.create_text(width - 17, 15, text="✓", fill="#101010", font=("Inter", 9, "bold"), state="hidden", tags=("photo_overlay",)),
                    "ring": self._round_rect(canvas, 1, 1, width - 1, height - 1, 14, "", self.accent),
                }
                canvas._galeria_overlay_items = items
                canvas.tag_lower("photo_thumbnail")
                canvas.tag_raise("photo_overlay")
                canvas.tag_raise(items["ring"])
            canvas.coords(items["shade"], 0, max(0, height - 27), width, height)
            canvas.coords(items["name"], 8, height - 6)
            canvas.coords(items["marker"], width - 27, 5, width - 7, 25)
            canvas.coords(items["check"], width - 17, 15)
            canvas.coords(
                items["ring"],
                *[
                    coord
                    for point in (
                        (15, 1),
                        (width - 15, 1),
                        (width - 1, 1),
                        (width - 1, 15),
                        (width - 1, height - 15),
                        (width - 1, height - 1),
                        (width - 15, height - 1),
                        (15, height - 1),
                        (1, height - 1),
                        (1, height - 15),
                        (1, 15),
                        (1, 1),
                    )
                    for coord in point
                ],
            )
            filename = Path(card._galeria_photo_path).name
            max_chars = max(8, (width - 16) // 6)
            if len(filename) > max_chars:
                filename = filename[:max_chars - 1] + "…"
            canvas.itemconfigure(items["name"], text=filename)
            selected = getattr(card, "_galeria_selected", False)
            hovered = getattr(card, "_galeria_hovered", False)
            pressed = getattr(card, "_galeria_pressed", False)
            canvas.itemconfigure(items["name"], state="normal" if hovered else "hidden")
            canvas.itemconfigure(items["shade"], state="normal" if hovered else "hidden")
            canvas.itemconfigure(items["marker"], state="normal" if selected or hovered else "hidden")
            canvas.itemconfigure(items["check"], state="normal" if selected else "hidden")
            canvas.itemconfigure(
                items["marker"],
                fill=self.accent if selected else "#11141b",
                outline=self.accent if selected else "#ffffff",
            )
            canvas.itemconfigure(
                items["ring"],
                state="normal" if selected or pressed else "hidden",
                outline=self.accent if selected or pressed else self._blend_color(BG, self.accent, 0.7),
                width=2 if selected else 1,
            )
        except tk.TclError:
            return

    def delete_album(self):
        if self.active_album is None:
            return
        if not messagebox.askyesno(
            "Excluir álbum",
            f"Excluir o álbum “{self.active_album_name}”? As fotos originais no seu computador não serão apagadas.",
        ):
            return
        with self.store._lock:
            self.store.db.execute("DELETE FROM albums WHERE id=?", (self.active_album,))
            self.store.db.commit()
        self.active_album = None
        self.show_all()

    def _poll_worker(self):
        try:
            while True:
                msg = self.worker_queue.get_nowait()
                kind = msg[0]
                if kind == "thumbnail":
                    _kind, scope, generation, widget, image, fade_frames = msg
                    if scope == "photo_grid" and generation != self._photo_render_generation:
                        continue
                    if scope == "album_browser" and generation != getattr(self, "_album_browser_generation", 0):
                        continue
                    if scope == "sidebar" and generation != getattr(self, "_sidebar_render_generation", 0):
                        continue
                    active_generation = getattr(self, "_thumbnail_scope_generations", {}).get(scope)
                    if active_generation is not None and generation is not None and generation != active_generation:
                        continue
                    try:
                        if not widget.winfo_exists():
                            continue
                        if image is None:
                            if scope == "photo_grid" and isinstance(widget, tk.Canvas):
                                widget.delete("photo_loading")
                                widget.create_text(
                                    widget.winfo_reqwidth() / 2,
                                    widget.winfo_reqheight() / 2,
                                    text="Foto indisponível",
                                    fill=MUTED,
                                    font=("Inter", 8, "bold"),
                                    width=max(32, widget.winfo_reqwidth() - 8),
                                    tags=("photo_error",),
                                )
                            else:
                                widget.configure(text="Foto indisponível")
                            continue
                        self._track_wallpaper_thumbnail(widget, image, scope)
                        if fade_frames:
                            self._animate_thumbnail_fade(widget, fade_frames, 0, scope, generation)
                            continue
                        photo = self._wallpaper_photo_image(image, master=self)
                        if isinstance(widget, ttk.Button):
                            widget.configure(image=photo, compound="left")
                            widget._galeria_photo_ref = photo
                            self.sidebar_album_refs.append(photo)
                        elif scope == "photo_grid":
                            self._set_thumbnail_widget_photo(widget, photo, scope)
                            self.photo_refs.append(photo)
                        elif scope == "album_browser":
                            widget.configure(image=photo, text="", width=0, height=0)
                            widget._galeria_photo_ref = photo
                            self.album_refs.append(photo)
                        elif scope == "sidebar":
                            widget.configure(image=photo, text="", width=0, height=0)
                            widget._galeria_photo_ref = photo
                            self.sidebar_album_refs.append(photo)
                        else:
                            widget.configure(image=photo, text="", width=0, height=0)
                            widget._galeria_photo_ref = photo
                            self.review_refs.append(photo)
                    except tk.TclError:
                        pass
                elif kind == "photo_viewer_image":
                    _kind, viewer, generation, image, error = msg
                    try:
                        if not viewer.winfo_exists():
                            continue
                        state = viewer._galeria_state
                        if generation != state["generation"]:
                            continue
                        canvas = state["canvas"]
                        canvas.delete("all")
                        if image is None:
                            canvas.create_text(
                                max(1, canvas.winfo_width()) / 2,
                                max(1, canvas.winfo_height()) / 2,
                                text="Não foi possível carregar esta foto.",
                                fill="#f0f2f5",
                                font=("Inter", 12, "bold"),
                                width=max(240, canvas.winfo_width() - 48),
                            )
                            state["status"].configure(text=error or "Arquivo indisponível")
                            continue
                        photo = ImageTk.PhotoImage(image, master=viewer)
                        state["image_ref"] = photo
                        canvas.create_image(
                            canvas.winfo_width() / 2,
                            canvas.winfo_height() / 2,
                            image=photo,
                            tags=("viewer_image",),
                        )
                        state["status"].configure(
                            text=f"{state['index'] + 1} / {len(state['paths'])} · {state['zoom']:.0%}"
                        )
                    except (AttributeError, tk.TclError):
                        continue
                elif kind == "wallpaper_source":
                    _kind, key, image = msg
                    if getattr(self, "_wallpaper_load_key", None) != key:
                        continue
                    self._wallpaper_load_future = None
                    if image is not None:
                        self._wallpaper_image_cache = (key, image)
                        if self.winfo_exists():
                            self._apply_background()
                    else:
                        self._wallpaper_enabled = False
                        self._restore_wallpaper_surfaces()
                        if hasattr(self, "background_window"):
                            self.background_window.withdraw()
                        self.configure(bg=BG)
                        self._notify("Não foi possível carregar a imagem de fundo. Escolha outra em Aparência.")
                elif kind == "wallpaper_render":
                    _kind, key, composed = msg
                    if getattr(self, "_wallpaper_pending_key", None) != key:
                        continue
                    self._wallpaper_pending_key = None
                    self._wallpaper_future = None
                    if composed is not None and self.winfo_exists():
                        self._show_wallpaper_image(key, composed)
                elif kind == "background_preview":
                    _kind, target, generation, image = msg
                    try:
                        if target.winfo_exists() and getattr(target, "_galeria_preview_generation", None) == generation:
                            target._galeria_source_image = image
                            target._galeria_preview_loading = False
                            target._galeria_preview_error = image is None
                            target._galeria_preview_error_message = None
                            redraw = getattr(target, "_galeria_redraw_preview", None)
                            if callable(redraw):
                                redraw()
                    except tk.TclError:
                        pass
                elif kind == "background_preview_error":
                    _kind, target, generation, error = msg
                    try:
                        if target.winfo_exists() and getattr(target, "_galeria_preview_generation", None) == generation:
                            target._galeria_source_image = None
                            target._galeria_preview_loading = False
                            target._galeria_preview_error = True
                            target._galeria_preview_error_message = error
                            redraw = getattr(target, "_galeria_redraw_preview", None)
                            if callable(redraw):
                                redraw()
                    except tk.TclError:
                        pass
                elif kind == "relink":
                    pairs = msg[1]
                    if pairs:
                        try:
                            self.store.update_moved_paths(pairs)
                            self.refresh_all()
                            self._notify(f"{len(pairs)} fotos foram reconectadas automaticamente após serem movidas.")
                        except sqlite3.Error:
                            pass
                elif kind == "import_scanned":
                    _kind, source, candidates = msg
                    self._scan_busy = False
                    self._begin_import(source, candidates)
                elif kind == "import_error":
                    _kind, message = msg
                    self._scan_busy = False
                    self._import_busy = False
                    self.configure(cursor="")
                    self._hide_progress()
                    messagebox.showerror("Não foi possível ler a pasta", message, parent=self)
                elif kind == "import_finished":
                    _kind, source, added, recovered, moved, total = msg
                    self._import_busy = False
                    self._scan_busy = False
                    self.configure(cursor="")
                    self._hide_progress()
                    try:
                        if moved:
                            self.store.update_moved_paths(moved)
                    except sqlite3.Error as exc:
                        messagebox.showerror("Falha ao atualizar a biblioteca", str(exc), parent=self)
                        self.refresh_all()
                        continue
                    if source:
                        self.active_folder = None
                    self.refresh_all()
                    if not total:
                        self._notify("Não encontrei fotos compatíveis nessa seleção.")
                    else:
                        parts = [
                            f"{added} adicionadas à biblioteca" if added else "nenhuma foto nova",
                            f"{len(moved)} movidas para {self.destination}\\Biblioteca" if moved else None,
                            f"{recovered} reconectadas" if recovered else None,
                        ]
                        self._info("Biblioteca atualizada", " · ".join(part for part in parts if part))
                elif kind == "album_remove_progress":
                    _kind, done, total = msg
                    self._show_progress(f"Devolvendo fotos à pasta Biblioteca {done:,} de {total:,}…", done, total)
                elif kind == "album_move_finished":
                    _kind, album_id, name, paths, moved, skipped, open_album, created, error = msg
                    self._library_move_busy = False
                    self.configure(cursor="")
                    self._hide_progress()
                    if open_album:
                        self.open_album(album_id, name)
                    else:
                        if moved:
                            self.active_folder = None
                        self.refresh_all()
                    if error:
                        messagebox.showerror("Falha ao mover fotos para o álbum", error, parent=self)
                        continue
                    self.selected.difference_update(paths)
                    title = "Álbum pronto" if created else "Fotos organizadas"
                    prefix = (
                        f"O álbum “{name}” foi criado e aberto com {self._photo_count(len(paths))}."
                        if created
                        else f"{len(paths)} foto(s) adicionadas ao álbum “{name}”."
                    )
                    details = []
                    if moved:
                        details.append(
                            f"{moved} arquivo(s) foram movidos da pasta de origem para a pasta do álbum."
                        )
                    if skipped:
                        details.append(
                            f"{skipped} arquivo(s) ficaram na origem porque não puderam ser movidos ou continuam vinculados a outro álbum."
                        )
                    unchanged = len(paths) - moved - skipped
                    if unchanged > 0:
                        details.append(f"{unchanged} arquivo(s) já estavam no destino.")
                    self._info(title, " ".join([prefix] + details))
                elif kind == "album_reorganize_progress":
                    _kind, done, total, name = msg
                    self._show_progress(f"Organizando álbuns {done:,} de {total:,}: {name}", done, total)
                elif kind == "album_reorganize_finished":
                    _kind, moved, skipped, errors, total = msg
                    self._library_move_busy = False
                    self.configure(cursor="")
                    self._hide_progress()
                    self.refresh_all()
                    details = [f"{moved} arquivo(s) movidos para a nova pasta."]
                    if skipped:
                        details.append(f"{skipped} arquivo(s) permaneceram na origem.")
                    if errors:
                        details.append("Falhas: " + " · ".join(errors[:3]))
                    if errors or skipped:
                        messagebox.showwarning(
                            "Preferências salvas com avisos",
                            " ".join(details),
                            parent=self,
                        )
                    else:
                        self._info(
                            "Preferências salvas",
                            f"Fotos dos {total} álbum(s) foram organizadas na nova pasta. {details[0]}",
                        )
                elif kind == "photos_deleted":
                    _kind, paths, removed, duplicate_groups, error = msg
                    self._library_move_busy = False
                    self.configure(cursor="")
                    self._hide_progress()
                    if error:
                        self.refresh_all()
                        messagebox.showerror("Falha ao atualizar a biblioteca", error, parent=self)
                        continue
                    self.selected.difference_update(paths)
                    self.refresh_all()
                    if duplicate_groups is None:
                        self._info(
                            "Fotos excluídas",
                            f"{removed} de {len(paths)} arquivo(s) foram enviados à Lixeira.",
                        )
                    else:
                        self._info(
                            "Duplicatas removidas",
                            f"{removed} duplicata(s) foram enviadas à Lixeira; "
                            f"{duplicate_groups} grupo(s) e os arquivos originais foram mantidos.",
                        )
                elif kind == "album_remove_finished":
                    _kind, album_id, paths, moved, missing, error = msg
                    self._library_move_busy = False
                    self.configure(cursor="")
                    self._hide_progress()
                    if error:
                        messagebox.showerror("Falha ao devolver fotos", error, parent=self)
                        self.refresh_all()
                        continue
                    self.selected.difference_update(paths)
                    self.refresh_all()
                    detail = f" {missing} arquivo(s) não foram encontrados no disco e tiveram apenas o vínculo removido." if missing else ""
                    self._info(
                        "Fotos devolvidas à Biblioteca",
                        f"{len(paths)} foto(s) removidas do álbum e {moved} arquivo(s) movidos para {self.destination}\\Biblioteca.{detail}",
                    )
                elif kind == "duplicates_progress":
                    _kind, done, total = msg
                    self._show_progress(f"Comparando visualmente {done:,} de {total:,} fotos…", done, total)
                elif kind == "duplicates_error":
                    _kind, message = msg
                    self.configure(cursor="")
                    self._hide_progress()
                    self._info("Comparação indisponível", message)
                elif kind == "reference_progress":
                    _kind, done, total = msg
                    self._show_progress(f"Analisando rostos {done:,} de {total:,} fotos…", done, total)
                elif kind == "reference_done":
                    _kind, matched, label, target_id, target_name = msg
                    self.configure(cursor="")
                    self._hide_progress()
                    self.review_face_matches(matched, target_id, target_name)
                elif kind == "duplicates":
                    self.review_duplicates(msg[1])
                elif kind == "error":
                    self.configure(cursor="")
                    self._hide_progress()
                    self._info("Busca facial interrompida", msg[1])
        except queue.Empty:
            pass
        if self._close_after_library_move and not self._library_move_busy:
            self._close_after_library_move = False
            self.after_idle(self.close)
        else:
            self.after(150, self._poll_worker)

    def close(self):
        if getattr(self, "_library_move_busy", False):
            self._close_after_library_move = True
            self._notify("Aguarde a operação em andamento; a Capri será fechada assim que ela terminar.")
            return
        viewer = getattr(self, "_photo_viewer", None)
        if viewer is not None:
            self._close_photo_viewer(viewer)
        try:
            if self.state() != "iconic":
                self.store.set_setting("window_geometry", self.geometry())
            self.store.set_setting("window_state", "zoomed" if self.state() == "zoomed" else "normal")
        except tk.TclError:
            pass
        if hasattr(self, "background_window"):
            try:
                if self.background_window.winfo_exists():
                    self.background_window.destroy()
            except tk.TclError:
                pass
        for executor in (
            getattr(self, "thumbnail_executor", None),
            getattr(self, "background_executor", None),
            getattr(self, "photo_viewer_executor", None),
        ):
            if executor is not None:
                try:
                    executor.shutdown(wait=False, cancel_futures=True)
                except TypeError:
                    executor.shutdown(wait=False)
        for path in getattr(self, "_private_fonts", ()):
            try:
                if os.name == "nt":
                    ctypes.windll.gdi32.RemoveFontResourceExW(path, 0x10, 0)
            except (AttributeError, OSError):
                pass
        try:
            self.store.db.close()
        except sqlite3.Error:
            pass
        self.destroy()


if __name__ == "__main__":
    app = GalleryApp()
    if app.winfo_exists():
        app.mainloop()
