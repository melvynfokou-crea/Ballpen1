"""
mes_hmi.py
==========
MES HMI — tracks Standard (regular tip) and Premium (premium tip) pen lines.

Features:
  - Input fields to set the production target for each pen type
  - Two animated pipeline diagrams, one per line
  - Live counters: produced / target, rejected, yield %
  - Shared rejection log with pen-type column
  - START runs both lines in parallel; STOP halts both

Run:
    python mes_hmi.py
"""

from __future__ import annotations

import os, queue, sys, threading, time, tkinter as tk
from tkinter import ttk, messagebox

sys.path.insert(0, os.path.dirname(__file__))
from prod import ProductionLine, PenType, Quality
from mes  import MES, LineStatus

# ── Colour palette ─────────────────────────────────────────────────────────────
BG     = "#1a2332"
PANEL  = "#243044"
ACCENT = "#00b4d8"
GREEN  = "#2dc653"
RED    = "#e63946"
AMBER  = "#f4a261"
PURPLE = "#9b59b6"
FG     = "#e0e8f0"
FG_DIM = "#7a8fa8"
HEADER = "#0d1b2a"

TYPE_ACCENT = {PenType.STANDARD: ACCENT, PenType.PREMIUM: PURPLE}
TYPE_LABEL  = {
    PenType.STANDARD: "STANDARD PEN  —  Regular Tip",
    PenType.PREMIUM:  "PREMIUM PEN   —  Premium Tip",
}

COMP_COLORS = {
    "barrel":    "#3498db",
    "tip":       "#9b59b6",
    "cartridge": "#2ecc71",
    "cap":       "#e74c3c",
    "pen":       "#f39c12",
}

# ── Pipeline geometry (mirrors hmi.py) ─────────────────────────────────────────
_CW,  _CH    = 650, 168
_BHW, _BHH   = 30,  13
_LANE_Y = {"barrel": 26, "tip": 68, "cartridge": 110, "cap": 152}
_CTR_Y  = 89
_X_MAKE = 50;  _X_QC = 145;  _X_BIN = 240
_X_ASSM = 345; _X_FQC = 435; _X_PACK = 520; _X_SHIP = 610

STEP_DELAY = 0.40   # seconds per stage (same as hmi.py)

# ── Event types ────────────────────────────────────────────────────────────────
EVT_ASSEMBLED = "assembled"
EVT_REJECTED  = "rejected"
EVT_STAGE     = "stage"
EVT_STATUS    = "status"
EVT_STOPPED   = "stopped"


# ── Instrumented production line ───────────────────────────────────────────────

class InstrumentedLine(ProductionLine):
    """ProductionLine that posts UI events, stops when the target is met."""

    def __init__(self, pen_type: PenType, target: int,
                 event_q: queue.Queue, stop_event: threading.Event, mes: MES):
        super().__init__(pen_type=pen_type)
        self._target = target
        self._q      = event_q
        self._stop   = stop_event
        self._mes    = mes

    # helpers ──────────────────────────────────────────────────────────────────

    def _stage(self, key: str, comp: str) -> None:
        if not self._stop.is_set():
            self._q.put({"type": EVT_STAGE, "pen_type": self.pen_type,
                         "stage": key, "component": comp})
            time.sleep(STEP_DELAY)

    def _refill_bins(self) -> None:
        for name in self.COMPONENT_NAMES:
            while not self.bins[name]:
                if self._stop.is_set():
                    return
                self._stage(f"make_{name}", name)
                raw = self.makers[name].process()

                self._stage(f"qc_{name}", name)
                inspected = self.qcs[name].process(raw)

                if inspected is None:
                    self._mes.record_rejected(self.pen_type)
                    self._q.put({
                        "type":     EVT_REJECTED,
                        "pen_type": self.pen_type,
                        "item":     f"{name.capitalize()} #{raw.serial}",
                        "station":  f"QC – {name}",
                        "reason":   "Defective component",
                    })
                else:
                    self._stage(f"bin_{name}", name)
                    self.bins[name].append(inspected)

    def _assemble_one(self):
        if self._stop.is_set():
            return None

        self._stage("assembly", "pen")
        parts  = {n: self.bins[n].popleft() for n in self.COMPONENT_NAMES}
        pen    = self.assembly.process(parts, self.pen_type)

        self._stage("final_qc", "pen")
        pen_ok = self.final_qc.process(pen)
        if pen_ok is None:
            self._mes.record_rejected(self.pen_type)
            self._q.put({
                "type":     EVT_REJECTED,
                "pen_type": self.pen_type,
                "item":     f"{self.pen_type.value} Pen #{pen.serial}",
                "station":  "Final QC",
                "reason":   "Assembly defect",
            })
            return None

        self._stage("packaging", "pen")
        result = self.packaging.process(pen_ok)
        target_met = self._mes.record_produced(self.pen_type)
        self._q.put({"type": EVT_ASSEMBLED, "pen_type": self.pen_type,
                     "serial": result.serial})

        self._stage("shipped", "pen")
        return result, target_met

    # main loop ────────────────────────────────────────────────────────────────

    def run_until_done(self) -> None:
        self._mes.set_status(self.pen_type, LineStatus.RUNNING)
        self._q.put({"type": EVT_STATUS, "pen_type": self.pen_type,
                     "msg": LineStatus.RUNNING.value})

        while not self._stop.is_set() and len(self.shipped) < self._target:
            self._refill_bins()
            if self._stop.is_set():
                break
            result = self._assemble_one()
            if result is not None:
                pen, target_met = result
                self.shipped.append(pen)
                if target_met:
                    break   # target reached — exit loop cleanly

        if len(self.shipped) >= self._target:
            final_status = LineStatus.DONE
        else:
            final_status = LineStatus.STOPPED
            self._mes.set_status(self.pen_type, final_status)

        self._q.put({"type": EVT_STATUS, "pen_type": self.pen_type,
                     "msg": f"{final_status.value}  —  {len(self.shipped)} shipped"})
        self._q.put({"type": EVT_STOPPED, "pen_type": self.pen_type})


# ── Single-line panel ──────────────────────────────────────────────────────────

class LinePanel(tk.LabelFrame):
    """Displays one production line: target input, pipeline, live stats."""

    def __init__(self, parent: tk.Widget, pen_type: PenType,
                 shared_q: queue.Queue, mes: MES):
        accent = TYPE_ACCENT[pen_type]
        super().__init__(
            parent,
            text=f"  {TYPE_LABEL[pen_type]}  ",
            bg=BG, fg=accent,
            font=("Courier New", 11, "bold"),
            bd=2, relief="ridge", labelanchor="n",
            padx=8, pady=6,
        )
        self._pen_type = pen_type
        self._mes      = mes
        self._shared_q = shared_q

        self._stop_event:   threading.Event        = threading.Event()
        self._thread:       threading.Thread | None = None
        self._active_stage: str | None             = None
        self._stage_items:  dict[str, tuple]       = {}

        self._build()

    # ── UI construction ────────────────────────────────────────────────────────

    def _build(self) -> None:
        accent = TYPE_ACCENT[self._pen_type]

        # Target input row
        row = tk.Frame(self, bg=BG)
        row.pack(fill="x", pady=(0, 6))

        tk.Label(row, text="Target:", bg=BG, fg=FG,
                 font=("Courier New", 10)).pack(side="left")

        self._target_var = tk.StringVar(value="10")
        self._target_entry = tk.Entry(
            row, textvariable=self._target_var, width=6,
            bg=PANEL, fg=accent, insertbackground=accent,
            font=("Courier New", 12, "bold"),
            relief="flat", bd=4, justify="center",
        )
        self._target_entry.pack(side="left", padx=6)

        tk.Label(row, text="pens", bg=BG, fg=FG_DIM,
                 font=("Courier New", 10)).pack(side="left")

        # Pipeline canvas
        self._build_pipeline()

        # Stats bar
        stats = tk.Frame(self, bg=PANEL, padx=10, pady=6)
        stats.pack(fill="x", pady=(4, 0))

        self._status_var   = tk.StringVar(value=LineStatus.IDLE.value)
        self._progress_var = tk.StringVar(value="Produced:  0 / 0")
        self._rejected_var = tk.StringVar(value="Rejected: 0")
        self._yield_var    = tk.StringVar(value="Yield: 0.0 %")

        tk.Label(stats, textvariable=self._status_var,
                 bg=PANEL, fg=AMBER, font=("Courier New", 9, "bold"),
                 width=22, anchor="w").grid(row=0, column=0, padx=(0, 10))

        tk.Label(stats, textvariable=self._progress_var,
                 bg=PANEL, fg=GREEN, font=("Courier New", 9)).grid(row=0, column=1, padx=6)

        tk.Label(stats, textvariable=self._rejected_var,
                 bg=PANEL, fg=RED, font=("Courier New", 9)).grid(row=0, column=2, padx=6)

        tk.Label(stats, textvariable=self._yield_var,
                 bg=PANEL, fg=accent, font=("Courier New", 9)).grid(row=0, column=3, padx=6)

        # Per-line start / stop buttons
        btn_row = tk.Frame(self, bg=BG, pady=6)
        btn_row.pack()

        self._btn_start = tk.Button(
            btn_row, text="▶  START",
            bg=GREEN, fg="#000", activebackground="#22a844",
            font=("Courier New", 10, "bold"),
            width=10, relief="flat", cursor="hand2",
            command=self.start,
        )
        self._btn_start.pack(side="left", padx=6)

        self._btn_stop = tk.Button(
            btn_row, text="■  STOP",
            bg=RED, fg="#fff", activebackground="#c0303a",
            font=("Courier New", 10, "bold"),
            width=10, relief="flat", cursor="hand2",
            state="disabled",
            command=self.stop,
        )
        self._btn_stop.pack(side="left", padx=6)

    def _build_pipeline(self) -> None:
        c = tk.Canvas(self, bg=PANEL, width=_CW, height=_CH, highlightthickness=0)
        c.pack()
        self._canvas = c

        abbr = {"barrel": "BAR", "tip": "TIP", "cartridge": "CART", "cap": "CAP"}

        def box(cx, cy, label, key):
            x0, y0 = cx - _BHW, cy - _BHH
            x1, y1 = cx + _BHW, cy + _BHH
            r = c.create_rectangle(x0, y0, x1, y1, fill=PANEL, outline=FG_DIM, width=1)
            t = c.create_text(cx, cy, text=label, fill=FG_DIM,
                              font=("Courier New", 7, "bold"), justify="center")
            self._stage_items[key] = (r, t)

        def arrow(x1, y1, x2, y2):
            c.create_line(x1, y1, x2, y2, fill=FG_DIM, width=1,
                          arrow=tk.LAST, arrowshape=(5, 7, 3))

        for name, y in _LANE_Y.items():
            s = abbr[name]
            box(_X_MAKE, y, f"MAKE\n{s}",  f"make_{name}")
            box(_X_QC,   y, f"QC\n{s}",    f"qc_{name}")
            box(_X_BIN,  y, f"BIN\n{s}",   f"bin_{name}")
            arrow(_X_MAKE + _BHW, y,   _X_QC  - _BHW, y)
            arrow(_X_QC  + _BHW, y,   _X_BIN - _BHW, y)
            arrow(_X_BIN + _BHW, y,   _X_ASSM - _BHW, _CTR_Y)

        box(_X_ASSM, _CTR_Y, "ASSM",       "assembly")
        box(_X_FQC,  _CTR_Y, "FINAL\nQC",  "final_qc")
        box(_X_PACK, _CTR_Y, "PACK",       "packaging")
        box(_X_SHIP, _CTR_Y, "SHIP",       "shipped")

        arrow(_X_ASSM + _BHW, _CTR_Y, _X_FQC  - _BHW, _CTR_Y)
        arrow(_X_FQC  + _BHW, _CTR_Y, _X_PACK - _BHW, _CTR_Y)
        arrow(_X_PACK + _BHW, _CTR_Y, _X_SHIP - _BHW, _CTR_Y)

    # ── Per-line controls ─────────────────────────────────────────────────────

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        try:
            target = self.get_target()
            if target <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("Invalid input",
                                 "Target must be a positive integer.", parent=self)
            return

        self._mes.reset_line(self._pen_type)
        self._mes.set_target(self._pen_type, target)
        self._stop_event.clear()
        self.reset_pipeline()
        self.refresh()
        self.lock_input(True)

        line = InstrumentedLine(
            self._pen_type, target,
            self._shared_q, self._stop_event, self._mes,
        )
        self._thread = threading.Thread(target=line.run_until_done, daemon=True)
        self._thread.start()
        self._btn_start.config(state="disabled")
        self._btn_stop.config(state="normal")

    def stop(self) -> None:
        self._stop_event.set()
        self._btn_stop.config(state="disabled")

    def on_stopped(self) -> None:
        self.reset_pipeline()
        self.refresh()
        self.lock_input(False)
        self._btn_start.config(state="normal")
        self._btn_stop.config(state="disabled")

    # ── Stage highlighting ─────────────────────────────────────────────────────

    def highlight_stage(self, stage: str, component: str) -> None:
        if self._active_stage and self._active_stage in self._stage_items:
            r, t = self._stage_items[self._active_stage]
            self._canvas.itemconfig(r, fill=PANEL, outline=FG_DIM)
            self._canvas.itemconfig(t, fill=FG_DIM)
        self._active_stage = stage
        if stage not in self._stage_items:
            return
        color = COMP_COLORS.get(component, ACCENT)
        r, t = self._stage_items[stage]
        self._canvas.itemconfig(r, fill=color, outline=color)
        self._canvas.itemconfig(t, fill="#111111")

    def reset_pipeline(self) -> None:
        for r, t in self._stage_items.values():
            self._canvas.itemconfig(r, fill=PANEL, outline=FG_DIM)
            self._canvas.itemconfig(t, fill=FG_DIM)
        self._active_stage = None

    # ── Live display update ────────────────────────────────────────────────────

    def refresh(self) -> None:
        d = self._mes.data(self._pen_type)
        self._status_var.set(d.status.value)
        self._progress_var.set(f"Produced:  {d.produced} / {d.target}")
        self._rejected_var.set(f"Rejected: {d.rejected}")
        self._yield_var.set(f"Yield: {d.yield_pct()} %")

    def set_status_text(self, msg: str) -> None:
        self._status_var.set(msg)

    # ── Target input ──────────────────────────────────────────────────────────

    def get_target(self) -> int:
        return int(self._target_var.get())

    def lock_input(self, locked: bool) -> None:
        self._target_entry.config(state="disabled" if locked else "normal")


# ── Main MES HMI window ────────────────────────────────────────────────────────

class MesHMI(tk.Tk):

    def __init__(self):
        super().__init__()
        self.title("Ball Pen MES — Manufacturing Execution System")
        self.configure(bg=BG)
        self.resizable(False, False)

        self._mes      = MES()
        self._shared_q = queue.Queue()

        self._build_ui()
        self._poll()

    # ── UI construction ────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        # Header
        hdr = tk.Frame(self, bg=HEADER, pady=10)
        hdr.pack(fill="x")
        tk.Label(
            hdr,
            text="BALL PEN MES  —  Manufacturing Execution System",
            bg=HEADER, fg=ACCENT,
            font=("Courier New", 14, "bold"),
        ).pack()

        # Line panels
        body = tk.Frame(self, bg=BG, padx=14, pady=10)
        body.pack(fill="x")

        self._std_panel = LinePanel(body, PenType.STANDARD, self._shared_q, self._mes)
        self._std_panel.pack(fill="x", pady=(0, 10))

        self._prm_panel = LinePanel(body, PenType.PREMIUM, self._shared_q, self._mes)
        self._prm_panel.pack(fill="x")

        self._panels = {
            PenType.STANDARD: self._std_panel,
            PenType.PREMIUM:  self._prm_panel,
        }

        # Divider
        tk.Frame(self, bg=ACCENT, height=2).pack(fill="x")

        # Rejection log
        self._build_log()

    def _build_log(self) -> None:
        log_frame = tk.Frame(self, bg=BG, padx=14, pady=8)
        log_frame.pack(fill="both", expand=True)

        tk.Label(
            log_frame, text="REJECTION LOG",
            bg=BG, fg=FG_DIM, font=("Courier New", 9, "bold"),
        ).pack(anchor="w", pady=(0, 4))

        style = ttk.Style(self)
        style.theme_use("default")
        style.configure("MES.Treeview",
                        background=PANEL, foreground=FG,
                        fieldbackground=PANEL, rowheight=22,
                        font=("Courier New", 9))
        style.configure("MES.Treeview.Heading",
                        background=HEADER, foreground=ACCENT,
                        font=("Courier New", 9, "bold"), relief="flat")
        style.map("MES.Treeview",
                  background=[("selected", ACCENT)],
                  foreground=[("selected", "#000")])

        cols = ("Pen Type", "Item", "Station", "Reason")
        wrap = tk.Frame(log_frame, bg=BG)
        wrap.pack(fill="both", expand=True)

        self._tree = ttk.Treeview(
            wrap, columns=cols, show="headings",
            style="MES.Treeview", height=8,
        )
        for col, w in zip(cols, (110, 160, 180, 240)):
            self._tree.heading(col, text=col)
            self._tree.column(col, width=w, anchor="w")

        sb = ttk.Scrollbar(wrap, orient="vertical", command=self._tree.yview)
        self._tree.configure(yscrollcommand=sb.set)
        self._tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        tk.Frame(self, bg=BG, height=8).pack()

    # ── Event pump (UI thread, every 50 ms) ───────────────────────────────────

    def _poll(self) -> None:
        try:
            while True:
                evt      = self._shared_q.get_nowait()
                t        = evt["type"]
                pen_type = evt.get("pen_type")
                panel    = self._panels.get(pen_type)

                if t == EVT_STAGE and panel:
                    panel.highlight_stage(evt["stage"], evt["component"])

                elif t == EVT_STATUS and panel:
                    panel.set_status_text(evt["msg"])

                elif t == EVT_ASSEMBLED and panel:
                    panel.refresh()

                elif t == EVT_REJECTED:
                    if panel:
                        panel.refresh()
                    self._tree.insert("", "end", values=(
                        pen_type.value if pen_type else "?",
                        evt["item"], evt["station"], evt["reason"],
                    ))
                    self._tree.yview_moveto(1.0)

                elif t == EVT_STOPPED:
                    if panel:
                        panel.on_stopped()

        except queue.Empty:
            pass
        self.after(50, self._poll)


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    MesHMI().mainloop()
