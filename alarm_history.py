# Session history of BMS conditions for the JK and JBD monitors: alarm bits, red thresholds and link errors.
# Kept in memory only; a condition seen in a single frame stays in the list after it clears.
import time
import tkinter as tk
from dataclasses import dataclass
from tkinter import ttk

ALARM, THRESHOLD, LINK = "alarm", "threshold", "link"
LINK_DOWN = "Link down"
LIMIT = 1000
MERGE_SECONDS = 30


@dataclass
class Entry:
    kind: str
    name: str
    detail: str
    start: float
    end: float = None
    last_seen: float = None
    cut: bool = False
    count: int = 1


class AlarmHistory:
    """An entry opens when a condition appears and closes at the first frame without it.

    A condition that returns within MERGE_SECONDS reopens its entry and raises its count instead of adding a new one.
    """

    def __init__(self):
        self.entries = []
        self._open = {}
        self._closed = {}

    def _add(self, entry: Entry):
        self.entries.append(entry)
        if len(self.entries) > LIMIT:
            old = self.entries.pop(next(i for i, e in enumerate(self.entries) if e.end is not None))
            if self._closed.get(old.name) is old:
                del self._closed[old.name]

    def observe(self, active: dict, now: float) -> bool:
        """Apply one frame: active maps a condition name to (kind, detail). Returns True if the list changed."""
        changed = False
        for name, (kind, detail) in active.items():
            entry = self._open.get(name)
            if entry:
                entry.last_seen = now
                continue
            entry = self._closed.get(name)
            if entry and not entry.cut and now - entry.end <= MERGE_SECONDS:
                entry.end, entry.last_seen = None, now
                entry.count += 1
                self._open[name] = entry
            else:
                entry = self._open[name] = Entry(kind, name, detail, now, last_seen=now)
                self._add(entry)
            changed = True
        for name, entry in list(self._open.items()):
            if entry.kind != LINK and name not in active:
                entry.end = now
                del self._open[name]
                self._closed[name] = entry
                changed = True
        return changed

    def cut(self, now: float) -> bool:
        """No more frames (link lost, paused, scanning): close frame conditions at the last frame that had them."""
        changed = False
        for name, entry in list(self._open.items()):
            if entry.kind != LINK:
                entry.end, entry.cut = entry.last_seen, True
                del self._open[name]
                changed = True
        return changed

    def link_down(self, detail: str, now: float) -> bool:
        self.cut(now)
        entry = self._open.get(LINK_DOWN)
        if entry:
            entry.count += 1
        else:
            self._open[LINK_DOWN] = entry = Entry(LINK, LINK_DOWN, detail, now)
            self._add(entry)
        return True

    def link_up(self, now: float) -> bool:
        entry = self._open.pop(LINK_DOWN, None)
        if entry:
            entry.end = now
        return entry is not None

    def note(self, kind: str, name: str, detail: str, now: float) -> bool:
        self._add(Entry(kind, name, detail, now, end=now))
        return True

    def active_count(self) -> int:
        return len(self._open)


def clock(t: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(t))


def span(seconds: float) -> str:
    s = round(seconds)
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} min {s % 60} s"
    return f"{s // 3600} h {s % 3600 // 60} min"


def row(e: Entry) -> tuple:
    if e.end is None:
        end, length = "active", ""
    else:
        end = clock(e.end) + (" (link lost)" if e.cut else "")
        length = span(e.end - e.start) if e.end > e.start else ""
    detail = e.detail + (f" (x{e.count})" if e.count > 1 else "")
    return clock(e.start), end, length, e.kind, e.name, detail


class HistoryWindow:
    COLUMNS = (("Start", 70), ("End", 130), ("Duration", 90), ("Kind", 70), ("Condition", 190), ("Detail", 300))

    def __init__(self, parent, history: AlarmHistory, title: str):
        self.history = history
        self.top = tk.Toplevel(parent)
        self.top.title(title)
        frame = ttk.Frame(self.top, padding=8)
        frame.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(frame, columns=[c for c, _w in self.COLUMNS], show="headings", height=16)
        for name, width in self.COLUMNS:
            self.tree.heading(name, text=name, anchor="w")
            self.tree.column(name, width=width, anchor="w")
        self.tree.tag_configure("active", foreground="#9b0f1f")
        bar = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=bar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        self.refresh()

    def alive(self) -> bool:
        return bool(self.top.winfo_exists())

    def show(self):
        self.top.deiconify()
        self.top.lift()

    def refresh(self):
        self.tree.delete(*self.tree.get_children())
        for e in reversed(self.history.entries):
            self.tree.insert("", "end", values=row(e), tags=("active",) if e.end is None else ())
