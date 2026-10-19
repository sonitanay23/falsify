"""Live trace printed to the terminal (what judges watch) and kept for the report."""
from __future__ import annotations

import os
import time

if os.name == "nt":  # enable colours on Windows terminals
    os.system("")

TRACE: list[dict] = []

_C = {
    "stage": "\033[1;38;5;208m",
    "info": "\033[0m",
    "dim": "\033[2m",
    "warn": "\033[33m",
    "ok": "\033[32m",
    "bad": "\033[31m",
    "end": "\033[0m",
}


def _emit(kind: str, msg: str, stage: str = "") -> None:
    stamp = time.strftime("%H:%M:%S")
    TRACE.append({"time": stamp, "kind": kind, "stage": stage, "msg": msg})
    prefix = {"stage": "\n▶ ", "info": "  • ", "dim": "    ", "warn": "  ! ", "ok": "  ✓ ", "bad": "  ✗ "}[kind]
    print(f"{_C[kind]}{prefix}{msg}{_C['end']}", flush=True)


_current = {"stage": ""}


def stage(name: str) -> None:
    _current["stage"] = name
    _emit("stage", name.upper(), name)


def info(msg: str) -> None:
    _emit("info", msg, _current["stage"])


def detail(msg: str) -> None:
    _emit("dim", msg, _current["stage"])


def warn(msg: str) -> None:
    _emit("warn", msg, _current["stage"])


def ok(msg: str) -> None:
    _emit("ok", msg, _current["stage"])


def bad(msg: str) -> None:
    _emit("bad", msg, _current["stage"])
