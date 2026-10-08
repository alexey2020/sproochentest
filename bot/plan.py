"""The learner's state and plan schedule, ported from index.html.

The state dict has the same shape the site stores and syncs, so the bot and the
site can share one sync code. Keep these functions in step with their JS
counterparts (normalize, ensureCfg, mergeState, setFlag, buildSchedule, ...).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date, timedelta

SETS = ("done", "known", "days")


def now_ms() -> int:
    return int(time.time() * 1000)


def iso(d: date) -> str:
    return d.isoformat()


def parse_iso(s: str) -> date:
    return date.fromisoformat(s)


# ---------- state ----------

def normalize(s: dict | None, legacy_cfg: dict) -> dict:
    s = dict(s or {})
    s.setdefault("cfg", None)
    for k in ("done", "known", "days", "ts", "listenBy", "listenDec"):
        s.setdefault(k, {})
    s.setdefault("quizBest", 0)
    s.setdefault("sims", [])
    s.pop("listen", None)
    return ensure_cfg(s, legacy_cfg)


def ensure_cfg(s: dict, legacy_cfg: dict) -> dict:
    """Progress saved before profiles existed belongs to the one learner with a fixed exam date."""
    used = s["done"] or s["known"] or s["days"] or s["sims"]
    if not s.get("cfg") and used:
        s["cfg"] = dict(legacy_cfg)
    return s


def merge(a: dict, b: dict) -> dict:
    ca, cb = a.get("cfg"), b.get("cfg")
    o = {
        "cfg": ca if ((ca or {}).get("ts") or 0) >= ((cb or {}).get("ts") or 0) else cb,
        "done": {}, "known": {}, "days": {}, "ts": {}, "listenBy": {}, "listenDec": {},
        "quizBest": max(a.get("quizBest") or 0, b.get("quizBest") or 0),
        "sims": [],
    }
    for f in SETS:
        ids = set()
        for s in (a, b):
            ids.update(s[f])
            ids.update(i[len(f) + 1:] for i in s["ts"] if i.startswith(f + ":"))
        for k in ids:
            key = f + ":" + k
            ta, tb = a["ts"].get(key, 0), b["ts"].get(key, 0)
            on = bool(a[f].get(k)) if ta > tb else bool(b[f].get(k)) if tb > ta else bool(a[f].get(k) or b[f].get(k))
            if on:
                o[f][k] = 1
            if ta or tb:
                o["ts"][key] = max(ta, tb)
    for f in ("listenBy", "listenDec"):
        for s in (a, b):
            for dev, n in s[f].items():
                o[f][dev] = max(o[f].get(dev, 0), n)
    sims = {}
    for x in a["sims"] + b["sims"]:
        sims[x.get("id")] = x
    o["sims"] = [sims[k] for k in sorted(sims, key=str)][-60:]
    return o


def set_flag(s: dict, f: str, k: str, on: bool) -> None:
    if on:
        s[f][k] = 1
    else:
        s[f].pop(k, None)
    s["ts"][f + ":" + k] = now_ms()


# ---------- schedule ----------

@dataclass
class Slot:
    i: int
    start: date | None
    end: date | None
    mods: list[int]
    review: bool


@dataclass
class Schedule:
    mode: str
    slots: list[Slot]


def monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def plan_mods(cfg: dict, data: dict) -> list[int]:
    skip = set(data["plan"]["level_skip"].get(cfg.get("level"), [])) | set(cfg.get("skip") or [])
    return [m["n"] for m in data["modules"] if m["n"] not in skip]


def spread(items: list[int], k: int) -> list:
    """k consecutive groups whose sizes differ by at most one; an empty group becomes a review week."""
    out, i = [], 0
    for j in range(k):
        n = -(-(len(items) - i) // (k - j))
        out.append(items[i : i + n] if n else "R")
        i += n
    return out


def build_schedule(cfg: dict, data: dict) -> Schedule:
    act = plan_mods(cfg, data)
    if not cfg.get("exam"):
        return Schedule("pace", [Slot(i, None, None, [n], False) for i, n in enumerate(act)])
    S, E0 = parse_iso(cfg["start"]), parse_iso(cfg["exam"])
    E = S if E0 < S else E0
    bounds, a = [], S
    while True:
        end = monday(a) + timedelta(days=6)
        if end >= E:
            bounds.append((a, E))
            break
        bounds.append((a, end))
        a = end + timedelta(days=1)
    K, R = len(bounds), "R"
    head = [[n] for n in act if n == 0]
    last = [[n] for n in act if n == 20]
    mid = [n for n in act if 1 <= n <= 16]
    tail = [n for n in act if 17 <= n <= 19]
    fixed = len(head) + len(last)
    if K >= fixed + len(mid) + len(tail):
        mode = "full"
        extra = K - fixed - len(mid) - len(tail)
        body: list = [R] * extra if not mid else []
        for i, n in enumerate(mid):
            body.append([n])
            body += [R] * ((i + 1) * extra // len(mid) - i * extra // len(mid))
        groups = head + body + [[n] for n in tail] + last
    elif K >= 8:
        mode = "compressed"
        groups = head + spread(mid, K - fixed - len(tail)) + [[n] for n in tail] + last
    else:
        mode = "express"
        if K == 1:
            groups = [last[0] if last else R]
        else:
            tc = max(0, min(3, (K - fixed) // 2))
            if tc >= len(tail):
                t = tail
            elif tc == 2:
                t = [tail[0], tail[-1]]
            elif tc == 1:
                t = [tail[-1]]
            else:
                t = []
            express = [n for n in mid if n in data["plan"]["express_mid"]]
            groups = head + spread(express, K - fixed - len(t)) + [[n] for n in t] + last
    return Schedule(
        mode,
        [Slot(i, bounds[i][0], bounds[i][1], [] if g == R else g, g == R) for i, g in enumerate(groups)],
    )


def slot_keys(sl: Slot, data: dict) -> list[str]:
    if sl.review:
        return [f"rv{iso(sl.start)}-{ti}" for ti in range(len(data["plan"]["review"]["tasks"]))]
    return [f"w{n}-{ti}" for n in sl.mods for ti in range(len(data["modules"][n]["tasks"]))]


def slot_done(sl: Slot, state: dict, data: dict) -> bool:
    keys = slot_keys(sl, data)
    return bool(keys) and all(state["done"].get(k) for k in keys)


def slot_mod(sched: Schedule, sl: Slot) -> int:
    """The module that stands for a week: its first one, or for a review week the last one before it."""
    if sl.mods:
        return sl.mods[0]
    for i in range(sl.i - 1, -1, -1):
        if sched.slots[i].mods:
            return sched.slots[i].mods[-1]
    return 0


def current_slot(sched: Schedule, state: dict, data: dict, today: date) -> int:
    sl = sched.slots
    if sched.mode == "pace":
        return next((x.i for x in sl if not slot_done(x, state, data)), len(sl) - 1)
    if today < sl[0].start:
        return 0
    return next((x.i for x in sl if x.start <= today <= x.end), len(sl) - 1)


def mod_slot(sched: Schedule) -> dict[int, int]:
    return {n: x.i for x in sched.slots for n in x.mods}
