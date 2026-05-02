"""
mes.py
======
MES data layer — tracks production targets and live counters for each pen type.

Used by mes_hmi.py (production threads write via record_*; UI reads for display).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto
from typing import Dict

from prod import PenType


class LineStatus(Enum):
    IDLE    = "■ Idle"
    RUNNING = "● Running"
    DONE    = "✓ Done"
    STOPPED = "■ Stopped"


@dataclass
class LineData:
    target:   int        = 0
    produced: int        = 0
    rejected: int        = 0
    status:   LineStatus = LineStatus.IDLE

    def yield_pct(self) -> float:
        total = self.produced + self.rejected
        return round(self.produced / total * 100, 1) if total else 0.0

    def target_met(self) -> bool:
        return self.target > 0 and self.produced >= self.target


class MES:
    """Central MES state — written from production threads, read from UI thread."""

    def __init__(self) -> None:
        self.lines: Dict[PenType, LineData] = {
            PenType.STANDARD: LineData(),
            PenType.PREMIUM:  LineData(),
        }

    # ── Configuration ─────────────────────────────────────────────────────────

    def set_targets(self, standard: int, premium: int) -> None:
        self.lines[PenType.STANDARD].target = standard
        self.lines[PenType.PREMIUM].target  = premium

    def set_target(self, pen_type: PenType, target: int) -> None:
        self.lines[pen_type].target = target

    def reset(self) -> None:
        for d in self.lines.values():
            d.produced = 0
            d.rejected = 0
            d.status   = LineStatus.IDLE

    def reset_line(self, pen_type: PenType) -> None:
        d = self.lines[pen_type]
        d.produced = 0
        d.rejected = 0
        d.status   = LineStatus.IDLE

    # ── Called from production threads ────────────────────────────────────────

    def record_produced(self, pen_type: PenType) -> bool:
        """Increment produced count.  Returns True if target is now met."""
        d = self.lines[pen_type]
        d.produced += 1
        if d.target_met():
            d.status = LineStatus.DONE
            return True
        return False

    def record_rejected(self, pen_type: PenType) -> None:
        self.lines[pen_type].rejected += 1

    def set_status(self, pen_type: PenType, status: LineStatus) -> None:
        self.lines[pen_type].status = status

    # ── Convenience reads ─────────────────────────────────────────────────────

    def data(self, pen_type: PenType) -> LineData:
        return self.lines[pen_type]
