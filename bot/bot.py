"""Telegram bot for Sproochentest preparation.

Uses the same material and plan rules as the web trainer (index.html), exported
to data.json by tools/extract_data.js. Each learner's profile and marks live in
the site's sync storage (store.py), so a sync code works in both places.

Runs in long-polling mode by default. If WEBHOOK_URL (or Render's
RENDER_EXTERNAL_URL) is set, it starts an aiohttp server on $PORT and
receives updates by webhook instead.
"""

import asyncio
import hashlib
import json
import logging
import os
import random
import re
from datetime import date, datetime
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    ErrorEvent,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from plan import build_schedule, current_slot, iso, mod_slot, now_ms, set_flag, slot_done, slot_keys, slot_mod
from store import CODE_RE, Store, StoreError, clean_code

DATA = json.loads((Path(__file__).parent / "data.json").read_text(encoding="utf-8"))
TOPICS = {t["key"]: t for t in DATA["topics"]}
GRAMMAR = {g["key"]: g for g in DATA["grammar"]}
MODS = DATA["modules"]
PLAN = DATA["plan"]
LEVELS = {lv["key"]: lv for lv in PLAN["levels"]}
TZ = ZoneInfo(os.environ.get("TZ_NAME", "Europe/Luxembourg"))
WD = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
MONTHS = ["янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]

# Flashcard groups: id -> (title, list of {lb, ru, tr})
CARD_GROUPS: dict[str, tuple[str, list[dict]]] = {
    "rescue": ("🆘 Фразы-спасатели", DATA["rescue"]),
    "photo": ("🖼 Описание фото", DATA["photo_words"]),
}
for i, v in enumerate(DATA["vocab"]):
    CARD_GROUPS[f"v{i}"] = (f"📖 {v['title']}", v["words"])
for t in DATA["topics"]:
    CARD_GROUPS[f"t:{t['key']}"] = (f"💬 {t['ru']}", t["phrases"])
CARD_GROUPS["all"] = ("🎲 Всё вперемешку", [c for _, cards in CARD_GROUPS.values() for c in cards])

router = Dispatcher()
store: Store  # set in main()


def kb(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows if row]
    )


def chunk(items: list, n: int) -> list[list]:
    return [items[i : i + n] for i in range(0, len(items), n)]


MENU = kb(
    [
        [("📅 Сегодня", "today"), ("📋 План", "plan")],
        [("🃏 Карточки", "cards"), ("🎤 Устная часть", "sim")],
        [("🖼 Описание фото", "pic"), ("❓ Квиз", "quiz")],
        [("📚 Грамматика", "gram"), ("💬 Темы", "topics")],
        [("📺 Курс RTL", "rtl"), ("🆘 Спасатели", "rescue")],
        [("⚙️ Настройки", "set")],
    ]
)
BACK = ("⬅️ Меню", "menu")


def phrase(p: dict) -> str:
    tr = f"\n<code>{escape(p['tr'])}</code>" if p.get("tr") else ""
    return f"<i>{escape(p['lb'])}</i>{tr}\n{escape(p['ru'])}"


def today() -> date:
    return datetime.now(TZ).date()


def fmt(d: date) -> str:
    return f"{d.day} {MONTHS[d.month - 1]}"


def fmt_long(d: date) -> str:
    return f"{WD[d.weekday()]}, {d:%d.%m.%Y}"


def plural(n: int, a: str, b: str, c: str) -> str:
    m, h = n % 10, n % 100
    return c if 11 <= h <= 14 else a if m == 1 else b if 2 <= m <= 4 else c


def phase_of(n: int) -> dict:
    return next(p for p in DATA["phases"] if n in p["weeks"])


async def send(m: Message, view: tuple[str, InlineKeyboardMarkup]) -> None:
    await m.answer(view[0], reply_markup=view[1], disable_web_page_preview=True)


async def show(c: CallbackQuery, text: str, markup: InlineKeyboardMarkup) -> None:
    """Replaces the message the button belongs to; Telegram refuses an edit that changes nothing."""
    try:
        await c.message.edit_text(text, reply_markup=markup, disable_web_page_preview=True)
    except TelegramBadRequest as e:
        if "not modified" not in str(e):
            raise


# ---------- the learner's plan ----------

class Plan:
    """The learner's state with their schedule laid out for today."""

    def __init__(self, state: dict):
        self.state = state
        self.cfg = state["cfg"]
        self.sched = build_schedule(self.cfg, DATA)
        self.today = today()
        self.cur = current_slot(self.sched, state, DATA, self.today)
        self.mslot = mod_slot(self.sched)
        self.exam = date.fromisoformat(self.cfg["exam"]) if self.cfg.get("exam") else None

    def done(self, key: str) -> bool:
        return bool(self.state["done"].get(key))

    def title(self, i: int) -> str:
        sl = self.sched.slots[i]
        return PLAN["review"]["title"] if sl.review else " + ".join(MODS[n]["title"] for n in sl.mods)

    def dates(self, i: int) -> str:
        sl = self.sched.slots[i]
        return f"{fmt(sl.start)} – {fmt(sl.end)}" if sl.start else ""

    def phase(self, i: int) -> dict:
        return phase_of(slot_mod(self.sched, self.sched.slots[i]))

    def week_topic(self, i: int) -> str | None:
        sl = self.sched.slots[i]
        return next((MODS[n]["topic"] for n in sl.mods if MODS[n]["topic"] != "all"), None)

    def head(self) -> str:
        n = len(self.sched.slots)
        if not self.exam:
            d = sum(slot_done(x, self.state, DATA) for x in self.sched.slots)
            return f"🧭 Свой темп · пройдено {d} из {n} {plural(n, 'недели', 'недель', 'недель')} плана"
        days = (self.exam - self.today).days
        when = fmt_long(self.exam)
        if days > 0:
            return f"🗓 До экзамена <b>{days}</b> {plural(days, 'день', 'дня', 'дней')} · {when}"
        return f"🗓 Экзамен сегодня! Говори просто, не молчи · {when}" if days == 0 else f"🎉 Экзамен позади · {when}"


async def load_plan(uid: int) -> Plan | None:
    st = await store.load(uid)
    return Plan(st) if st.get("cfg") else None


def focus_lines(w: dict, topic_text: str = "Все 18 тем — симуляции") -> list[str]:
    t = TOPICS.get(w["topic"])
    topic = f"{escape(t['lb'])} ({escape(t['ru'])})" if t else escape(topic_text)
    lines = []
    if w["grammar"] and w["grammar"] != "—":
        lines.append(f"📚 {escape(w['grammar'])}")
    lines.append(f"💬 {topic}")
    lines.append(f"🎧 {escape(w['listening'])}")
    return lines


def week_view(p: Plan, i: int) -> tuple[str, InlineKeyboardMarkup]:
    sl = p.sched.slots[i]
    n = len(p.sched.slots)
    now = " · 👉 сейчас" if i == p.cur else ""
    dates = f" · {p.dates(i)}" if sl.start else ""
    lines = [
        p.head(),
        "",
        f"<b>Неделя {i} из {n - 1}</b>{dates}{now}",
        f"{escape(PLAN['mode_name'][p.sched.mode])} · {escape(p.phase(i)['name'])}",
        "",
        f"<b>{escape(p.title(i))}</b>",
    ]
    if len(sl.mods) > 1:
        lines.append(f"<i>Сжатый план: {len(sl.mods)} {plural(len(sl.mods), 'тема', 'темы', 'тем')} за неделю. Иди по порядку.</i>")
    tasks: list[tuple[str, str]] = []  # (key, text)
    if sl.review:
        r = PLAN["review"]
        lines += [""] + focus_lines({"grammar": r["grammar"], "topic": "all", "listening": r["listening"]}, r["topic_text"])
        tasks = list(zip(slot_keys(sl, DATA), r["tasks"]))
        lines.append("")
        lines += [f"{'✅' if p.done(k) else '⬜'} {j}. {escape(t)}" for j, (k, t) in enumerate(tasks, 1)]
    else:
        for m in sl.mods:
            w = MODS[m]
            lines.append("")
            if len(sl.mods) > 1:
                lines.append(f"▸ <b>{escape(w['title'])}</b>")
            lines += focus_lines(w)
            for ti, t in enumerate(w["tasks"]):
                tasks.append((f"w{m}-{ti}", t))
                lines.append(f"{'✅' if p.done(tasks[-1][0]) else '⬜'} {len(tasks)}. {escape(t)}")
            llo = DATA["llo"]["weeks"].get(str(m))
            if llo:
                lines.append(f"{'✅' if p.done(f'llo-{m}') else '⬜'} 🖥 LLO.lu, 2–3 раза в будни: {escape(llo)}")
    if i == p.cur:
        r = PLAN["routine"][str(p.cfg["mins"])]
        lines += ["", f"⏱ {p.cfg['mins']} мин в день: аудио {r[0]} · карточки {r[1]} · говорение {r[2]} · грамматика {r[3]}"]
    lines.append("\nНажимай номер задачи, чтобы отметить. Отметки видны и на сайте, если подключён тот же код.")

    rows: list[list[tuple[str, str]]] = chunk(
        [(f"{'✅' if p.done(k) else '⬜'} {j}", f"tk:{i}:{k}") for j, (k, _) in enumerate(tasks, 1)], 5
    )
    llo_marks = [
        (f"{'✅' if p.done(f'llo-{m}') else '⬜'} LLO{'' if len(sl.mods) == 1 else ' ' + MODS[m]['title'][:12]}", f"tk:{i}:llo-{m}")
        for m in sl.mods
        if str(m) in DATA["llo"]["weeks"]
    ]
    rows += chunk(llo_marks, 2)
    extra = []
    topic = p.week_topic(i)
    if topic:
        extra.append(("💬 Тема недели", f"topic:{topic}"))
    if any(x["module"] in sl.mods for x in DATA["rtl"]):
        extra.append(("📺 RTL недели", f"rtlw:{i}"))
    rows.append(extra)
    if i == p.cur:
        k = iso(p.today)
        rows.append([("✅ Сегодня занимался(ась)" if p.state["days"].get(k) else "✔️ Отметить занятие сегодня", f"day:{i}")])
        if p.sched.mode == "pace" and i < n - 1:
            rows.append([("➡️ Неделя пройдена, дальше", f"wdone:{i}")])
    nav = []
    if i > 0:
        nav.append(("◀️", f"wk:{i - 1}"))
    nav.append(("📋 Весь план", "plan"))
    if i < n - 1:
        nav.append(("▶️", f"wk:{i + 1}"))
    rows += [nav, [BACK]]
    markup = kb(rows)
    if llo_marks:
        markup.inline_keyboard.insert(len(markup.inline_keyboard) - 2, [InlineKeyboardButton(text="🖥 Открыть LLO.lu", url=DATA["llo"]["url"])])
    return "\n".join(lines), markup


def plan_view(p: Plan) -> tuple[str, InlineKeyboardMarkup]:
    n = len(p.sched.slots)
    lines = [p.head(), "", f"<b>План: {n} {plural(n, 'неделя', 'недели', 'недель')}</b> · {escape(PLAN['mode_name'][p.sched.mode])}", ""]
    width = 44 if n <= 30 else 30
    for sl in p.sched.slots:
        mark = "👉" if sl.i == p.cur else "✅" if slot_done(sl, p.state, DATA) else "▫️"
        t = p.title(sl.i)
        t = t if len(t) <= width else t[: width - 1] + "…"
        dates = f" · {p.dates(sl.i)}" if sl.start else ""
        lines.append(f"{mark} <b>{sl.i}</b> · {escape(t)}{dates}")
    out = [m for m in MODS if m["n"] not in p.mslot]
    if out:
        lines.append(f"\nНе вошло в план: {len(out)} {plural(len(out), 'неделя', 'недели', 'недель')} (по уровню или уже пройдены).")
    lines.append("\nВыбери неделю:")
    btns = [(f"👉{sl.i}" if sl.i == p.cur else str(sl.i), f"wk:{sl.i}") for sl in p.sched.slots]
    return "\n".join(lines), kb(chunk(btns, 6) + [[("⚙️ Настройки", "set"), BACK]])


NO_PROFILE = "Сначала настроим план: ответь на три вопроса 👇"


async def need_setup(target: Message, state: FSMContext) -> None:
    await target.answer(NO_PROFILE)
    await ask_date(target, state, "onb")


# ---------- menu / start ----------

WELCOME = (
    "Moien! 👋 Я помогу подготовиться к <b>Sproochentest</b>.\n\n"
    "• <b>Сегодня</b>: задачи этой недели по твоему плану, отмечай выполненное\n"
    "• <b>План</b>: все недели до экзамена\n"
    "• <b>Карточки</b>: слова и фразы с транскрипцией\n"
    "• <b>Устная часть</b>: вопрос экзаменатора, ты отвечаешь вслух или голосовым\n"
    "• <b>Описание фото</b>, <b>квиз</b>, <b>грамматика</b> и <b>курс RTL</b>\n\n"
    "Прогресс общий с сайтом-тренажёром, если ввести там тот же код (⚙️ Настройки → Код).\n\n"
    "Выбирай 👇"
)


@router.message(CommandStart())
async def cmd_start(m: Message, state: FSMContext) -> None:
    await state.clear()
    if await load_plan(m.from_user.id):
        await m.answer(WELCOME, reply_markup=MENU)
        return
    await m.answer(
        "Moien! 👋 Я помогу подготовиться к <b>Sproochentest</b>: план по неделям до твоего экзамена, "
        "карточки, симулятор устной части, квиз и курс RTL.\n\n" + NO_PROFILE
    )
    await ask_date(m, state, "onb")


@router.message(Command("menu"))
async def cmd_menu(m: Message, state: FSMContext) -> None:
    await state.clear()
    await m.answer(WELCOME, reply_markup=MENU)


@router.callback_query(F.data == "menu")
async def cb_menu(c: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await c.message.answer(WELCOME, reply_markup=MENU)
    await c.answer()


# ---------- setup and settings ----------

class Setup(StatesGroup):
    date = State()
    code = State()


async def ask_date(m: Message, state: FSMContext, mode: str) -> None:
    await state.set_state(Setup.date)
    await state.update_data(mode=mode)
    rows = [[("🧭 Даты пока нет, свой темп", "sx:none")]]
    if mode == "onb":
        rows.append([("🔗 У меня есть код с сайта", "sx:code")])
    else:
        rows.append([("⬅️ Настройки", "set")])
    await m.answer(
        "📅 <b>Когда экзамен?</b>\nНапиши дату, например <code>18.02.2027</code>.\n"
        "Даты сессий и запись — в <a href=\"https://myinl.inll.lu/exam/offer/sproochentest-8\">MyINL</a>.",
        reply_markup=kb(rows),
        disable_web_page_preview=True,
    )


def parse_date(text: str) -> date | None:
    t = text.strip()
    m = re.fullmatch(r"(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})", t)
    try:
        if m:
            d, mo, y = map(int, m.groups())
            return date(y + 2000 if y < 100 else y, mo, d)
        return date.fromisoformat(t)
    except ValueError:
        return None


async def ask_level(m: Message, state: FSMContext) -> None:
    await state.set_state(None)
    rows = [[(f"{lv['label']}", f"lv:{lv['key']}")] for lv in PLAN["levels"]]
    text = "🎯 <b>С чего начинаешь?</b>\n\n" + "\n".join(f"• <b>{escape(lv['label'])}</b>: {escape(lv['about'])}" for lv in PLAN["levels"])
    await m.answer(text, reply_markup=kb(rows))


async def ask_mins(m: Message) -> None:
    await m.answer("⏱ <b>Сколько времени в день?</b>", reply_markup=kb([[(f"{x} минут", f"mn:{x}") for x in PLAN["mins"]]]))


async def save_cfg(uid: int, **changes) -> Plan:
    async with store.lock(uid):
        st = await store.load(uid)
        cfg = dict(st["cfg"] or {"exam": "", "level": "zero", "mins": 30, "start": iso(today()), "skip": []})
        cfg.update(changes)
        cfg["ts"] = now_ms()
        st["cfg"] = cfg
        await store.save(uid, st)
        return Plan(st)


@router.message(StateFilter(Setup.date), F.text)
async def on_date(m: Message, state: FSMContext) -> None:
    d = parse_date(m.text)
    if not d:
        await m.answer("Не понял дату 🙂 Напиши так: <code>18.02.2027</code>")
        return
    if d < today():
        await m.answer("Эта дата уже прошла. Напиши дату будущего экзамена.")
        return
    mode = (await state.get_data()).get("mode")
    if mode == "onb":
        await state.update_data(exam=iso(d))
        await ask_level(m, state)
    else:
        await state.clear()
        p = await save_cfg(m.from_user.id, exam=iso(d))
        await m.answer(f"Готово: экзамен {fmt_long(d)}. План пересчитан.")
        await send(m, week_view(p, p.cur))


@router.callback_query(F.data == "sx:none")
async def cb_no_exam(c: CallbackQuery, state: FSMContext) -> None:
    mode = (await state.get_data()).get("mode")
    await c.answer()
    if mode == "onb":
        await state.update_data(exam="")
        await ask_level(c.message, state)
    else:
        await state.clear()
        p = await save_cfg(c.from_user.id, exam="")
        await c.message.answer("Готово: свой темп, без даты. Когда появится дата — укажи её в настройках.")
        await send(c.message, week_view(p, p.cur))


@router.callback_query(F.data.startswith("lv:"))
async def cb_level(c: CallbackQuery, state: FSMContext) -> None:
    level = c.data[3:]
    await c.answer(LEVELS[level]["label"])
    data = await state.get_data()
    if data.get("mode") == "onb":
        await state.update_data(level=level)
        await ask_mins(c.message)
    else:
        p = await save_cfg(c.from_user.id, level=level)
        await c.message.answer(f"Готово: уровень «{escape(LEVELS[level]['label'])}». План пересчитан.")
        await send(c.message, week_view(p, p.cur))


@router.callback_query(F.data.startswith("mn:"))
async def cb_mins(c: CallbackQuery, state: FSMContext) -> None:
    mins = int(c.data[3:])
    await c.answer()
    data = await state.get_data()
    if data.get("mode") == "onb":
        await state.clear()
        p = await save_cfg(
            c.from_user.id, exam=data.get("exam", ""), level=data.get("level", "zero"), mins=mins, start=iso(today()), skip=[]
        )
        await c.message.answer("🎉 План готов! Вот твоя неделя:")
        await send(c.message, week_view(p, p.cur))
        await c.message.answer(WELCOME, reply_markup=MENU)
    else:
        p = await save_cfg(c.from_user.id, mins=mins)
        await c.message.answer(f"Готово: {mins} минут в день.")
        await send(c.message, week_view(p, p.cur))


def settings_text(p: Plan, code: str) -> str:
    exam = fmt_long(p.exam) if p.exam else "нет, свой темп"
    return (
        "⚙️ <b>Настройки плана</b>\n\n"
        f"📅 Экзамен: {exam}\n"
        f"🎯 Уровень: {escape(LEVELS[p.cfg['level']]['label'])}\n"
        f"⏱ В день: {p.cfg['mins']} минут\n"
        f"📋 Режим: {escape(PLAN['mode_name'][p.sched.mode])}, {len(p.sched.slots)} нед.\n"
        f"🔗 Код синхронизации: <code>{code}</code>\n\n"
        "Отметки при изменениях сохраняются."
    )


SETTINGS_KB = kb(
    [
        [("📅 Дата экзамена", "set:date"), ("🎯 Уровень", "set:level")],
        [("⏱ Минут в день", "set:mins"), ("🔄 Начать заново", "set:restart")],
        [("🔗 Код и сайт", "set:code")],
        [BACK],
    ]
)


@router.message(Command("settings"))
async def cmd_settings(m: Message, state: FSMContext) -> None:
    await state.clear()
    p = await load_plan(m.from_user.id)
    if not p:
        return await need_setup(m, state)
    await m.answer(settings_text(p, await store.code_for(m.from_user.id)), reply_markup=SETTINGS_KB)


@router.callback_query(F.data == "set")
async def cb_settings(c: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await c.answer()
    p = await load_plan(c.from_user.id)
    if not p:
        return await need_setup(c.message, state)
    await c.message.answer(settings_text(p, await store.code_for(c.from_user.id)), reply_markup=SETTINGS_KB)


@router.callback_query(F.data.startswith("set:"))
async def cb_setting(c: CallbackQuery, state: FSMContext) -> None:
    what = c.data[4:]
    await c.answer()
    await state.update_data(mode="edit")
    if what == "date":
        await ask_date(c.message, state, "edit")
    elif what == "level":
        rows = [[(lv["label"], f"lv:{lv['key']}")] for lv in PLAN["levels"]] + [[("⬅️ Настройки", "set")]]
        await c.message.answer("🎯 <b>Уровень</b>", reply_markup=kb(rows))
    elif what == "mins":
        await c.message.answer("⏱ <b>Минут в день</b>", reply_markup=kb([[(f"{x} минут", f"mn:{x}") for x in PLAN["mins"]], [("⬅️ Настройки", "set")]]))
    elif what == "restart":
        await c.message.answer(
            "🔄 <b>Начать план заново с этой недели?</b>\nУже пройденные недели не повторятся, отметки останутся.",
            reply_markup=kb([[("Да, начать заново", "restart"), ("⬅️ Нет", "set")]]),
        )
    elif what == "code":
        code = await store.code_for(c.from_user.id)
        await c.message.answer(
            f"🔗 <b>Код синхронизации</b>: <code>{code}</code>\n\n"
            "Чтобы прогресс был общим с сайтом-тренажёром: на сайте открой «Сегодня» → «Синхронизация между устройствами» → "
            "«У меня уже есть код» и введи этот код.\n\n"
            "Если на сайте у тебя уже есть свой код с прогрессом — подключи его здесь, и бот будет работать с ним.",
            reply_markup=kb([[("🔗 Подключить код с сайта", "sx:code")], [("⬅️ Настройки", "set")]]),
        )


@router.callback_query(F.data == "restart")
async def cb_restart(c: CallbackQuery) -> None:
    await c.answer()
    st = await store.load(c.from_user.id)
    finished = [m["n"] for m in MODS if all(st["done"].get(f"w{m['n']}-{ti}") for ti in range(len(m["tasks"])))]
    p = await save_cfg(c.from_user.id, start=iso(today()), skip=finished)
    await c.message.answer("Готово: план начинается с этой недели.")
    await send(c.message, week_view(p, p.cur))


@router.callback_query(F.data == "sx:code")
async def cb_ask_code(c: CallbackQuery, state: FSMContext) -> None:
    await c.answer()
    await state.set_state(Setup.code)
    await c.message.answer("Пришли код с сайта: три слова и число через дефис, например <code>moien-kaffi-gaart-7342</code>.")


@router.message(StateFilter(Setup.code), F.text)
async def on_code(m: Message, state: FSMContext) -> None:
    code = clean_code(m.text)
    if not CODE_RE.match(code):
        await m.answer("Код выглядит как три слова и четыре цифры через дефис. Попробуй ещё раз.")
        return
    async with store.lock(m.from_user.id):
        ok = await store.link(m.from_user.id, code)
    if not ok:
        await m.answer("Такого кода нет. Проверь написание.")
        return
    await state.clear()
    p = await load_plan(m.from_user.id)
    if p:
        await m.answer("✅ Код подключён: бот и сайт теперь видят один прогресс.")
        await send(m, week_view(p, p.cur))
    else:
        await m.answer("✅ Код подключён. Осталось настроить план.")
        await ask_date(m, state, "onb")


# ---------- today and plan ----------

@router.message(Command("today"))
async def cmd_today(m: Message, state: FSMContext) -> None:
    p = await load_plan(m.from_user.id)
    if not p:
        return await need_setup(m, state)
    await send(m, week_view(p, p.cur))


@router.callback_query(F.data == "today")
async def cb_today(c: CallbackQuery, state: FSMContext) -> None:
    await c.answer()
    p = await load_plan(c.from_user.id)
    if not p:
        return await need_setup(c.message, state)
    await send(c.message, week_view(p, p.cur))


@router.message(Command("plan"))
async def cmd_plan(m: Message, state: FSMContext) -> None:
    p = await load_plan(m.from_user.id)
    if not p:
        return await need_setup(m, state)
    await send(m, plan_view(p))


@router.callback_query(F.data == "plan")
async def cb_plan(c: CallbackQuery, state: FSMContext) -> None:
    await c.answer()
    p = await load_plan(c.from_user.id)
    if not p:
        return await need_setup(c.message, state)
    await show(c, *plan_view(p))


@router.callback_query(F.data.startswith("wk:"))
async def cb_week(c: CallbackQuery, state: FSMContext) -> None:
    await c.answer()
    p = await load_plan(c.from_user.id)
    if not p:
        return await need_setup(c.message, state)
    i = min(int(c.data[3:]), len(p.sched.slots) - 1)
    await show(c, *week_view(p, i))


async def toggle(c: CallbackQuery, f: str, key: str) -> Plan | None:
    uid = c.from_user.id
    async with store.lock(uid):
        st = await store.load(uid)
        if not st.get("cfg"):
            return None
        on = not st[f].get(key)
        set_flag(st, f, key, on)
        await store.save(uid, st)
    await c.answer("✅ Отмечено" if on else "Отметка снята")
    return Plan(st)


@router.callback_query(F.data.startswith("tk:"))
async def cb_task(c: CallbackQuery) -> None:
    _, i, key = c.data.split(":", 2)
    p = await toggle(c, "done", key)
    if p:
        await show(c, *week_view(p, min(int(i), len(p.sched.slots) - 1)))


@router.callback_query(F.data.startswith("day:"))
async def cb_day(c: CallbackQuery) -> None:
    p = await toggle(c, "days", iso(today()))
    if p:
        await show(c, *week_view(p, min(int(c.data[4:]), len(p.sched.slots) - 1)))


@router.callback_query(F.data.startswith("wdone:"))
async def cb_week_done(c: CallbackQuery) -> None:
    uid, i = c.from_user.id, int(c.data[6:])
    async with store.lock(uid):
        st = await store.load(uid)
        p = Plan(st)
        for k in slot_keys(p.sched.slots[i], DATA):
            if not st["done"].get(k):
                set_flag(st, "done", k, True)
        await store.save(uid, st)
    await c.answer("Неделя пройдена 🎉")
    p = Plan(st)
    await show(c, *week_view(p, p.cur))


# ---------- flashcards ----------

@router.message(Command("cards"))
async def cmd_cards(m: Message) -> None:
    await m.answer("Выбери набор карточек:", reply_markup=cards_menu())


def cards_menu() -> InlineKeyboardMarkup:
    main = [(title, f"cg:{gid}") for gid, (title, _) in CARD_GROUPS.items() if not gid.startswith("t:")]
    return kb(chunk(main, 2) + [[("💬 Фразы по темам…", "cgt")], [BACK]])


@router.callback_query(F.data == "cards")
async def cb_cards(c: CallbackQuery) -> None:
    await c.message.answer("Выбери набор карточек:", reply_markup=cards_menu())
    await c.answer()


@router.callback_query(F.data == "cgt")
async def cb_cards_topics(c: CallbackQuery) -> None:
    items = [(t["ru"], f"cg:t:{t['key']}") for t in DATA["topics"]]
    await c.message.edit_reply_markup(reply_markup=kb(chunk(items, 3) + [[("⬅️ Назад", "cards")]]))
    await c.answer()


@router.callback_query(F.data.startswith("cg:"))
async def cb_card(c: CallbackQuery) -> None:
    # cg:<group>  or  cg:<group>|<index>|r  (r = reveal)
    payload = c.data[3:]
    gid, _, rest = payload.partition("|")
    title, cards = CARD_GROUPS[gid]
    if rest:
        idx = int(rest.split("|")[0])
        card = cards[idx]
        await c.message.edit_text(
            f"{escape(title)}\n\n{phrase(card)}\n\n🗣 Произнеси вслух!",
            reply_markup=kb([[("➡️ Дальше", f"cg:{gid}"), ("🔁 Другой набор", "cards")]]),
        )
    else:
        idx = random.randrange(len(cards))
        card = cards[idx]
        await c.message.answer(
            f"{escape(title)}\n\n<b>{escape(card['ru'])}</b>\n\nКак это по-люксембургски?",
            reply_markup=kb([[("👀 Показать", f"cg:{gid}|{idx}|r")], [("➡️ Пропустить", f"cg:{gid}"), BACK]]),
        )
    await c.answer()


# ---------- oral exam simulator ----------

def sim_question() -> tuple[str, InlineKeyboardMarkup]:
    t = random.choice(DATA["topics"])
    q = random.choice(t["questions"])
    text = (
        f"🎤 <b>Тема: {escape(t['lb'])}</b> ({escape(t['ru'])})\n\n"
        f"Экзаменатор спрашивает:\n{phrase(q)}\n\n"
        "Ответь вслух 60–90 секунд или пришли голосовое 🎙 (себе на память). "
        "В конце задай встречный вопрос!"
    )
    return text, kb(
        [
            [("💡 Подсказки", f"simh:{t['key']}"), ("➡️ Другой вопрос", "sim")],
            [("❓ Уточняющий вопрос", "simf"), BACK],
        ]
    )


@router.message(Command("sim"))
async def cmd_sim(m: Message) -> None:
    text, markup = sim_question()
    await m.answer(text, reply_markup=markup)


@router.callback_query(F.data == "sim")
async def cb_sim(c: CallbackQuery) -> None:
    text, markup = sim_question()
    await c.message.answer(text, reply_markup=markup)
    await c.answer()


@router.callback_query(F.data.startswith("simh:"))
async def cb_sim_hints(c: CallbackQuery) -> None:
    t = TOPICS[c.data[5:]]
    body = "\n\n".join(phrase(p) for p in t["phrases"])
    links = ", ".join(f"<i>{escape(p['lb'])}</i>" for p in DATA["connect"][:6])
    await c.message.answer(
        f"💡 <b>Фразы по теме «{escape(t['ru'])}»</b>\n\n{body}\n\n🔗 Связки: {links}",
        reply_markup=kb([[("➡️ Следующий вопрос", "sim"), BACK]]),
    )
    await c.answer()


@router.callback_query(F.data == "simf")
async def cb_sim_followup(c: CallbackQuery) -> None:
    q = random.choice(DATA["questions_generic"])
    await c.message.answer(
        f"Экзаменатор уточняет:\n\n{phrase(q)}\n\nПродолжай ответ 🗣",
        reply_markup=kb([[("❓ Ещё уточнение", "simf"), ("➡️ Новый вопрос", "sim")]]),
    )
    await c.answer()


@router.message(F.voice)
async def on_voice(m: Message) -> None:
    await m.answer(
        "👍 Ответ записан! Прослушай себя и проверь:\n"
        "▫️ Не было долгих пауз?\n▫️ Использовал(а) связки (well, duerno, awer)?\n"
        "▫️ Глагол на втором месте?\n▫️ Задал(а) встречный вопрос?",
        reply_markup=kb([[("➡️ Следующий вопрос", "sim"), BACK]]),
    )


# ---------- photo description ----------

def photo_text() -> str:
    scene = random.choice(DATA["scenes"])
    steps = "\n\n".join(f"<b>{i}. {escape(s['step'])}</b>\n<i>{escape(s['lb'])}</i>\n{escape(s['ru'])}" for i, s in enumerate(DATA["photo_steps"], 1))
    return (
        f"🖼 <b>Представь фото:</b>\n«{escape(scene)}»\n\n"
        "Опиши его по-люксембургски за 1–2 минуты. Шаблон:\n\n" + steps
    )


@router.message(Command("photo"))
async def cmd_photo(m: Message) -> None:
    await m.answer(photo_text(), reply_markup=kb([[("🔄 Другое фото", "pic"), ("🃏 Слова для фото", "cg:photo")], [BACK]]))


@router.callback_query(F.data == "pic")
async def cb_photo(c: CallbackQuery) -> None:
    await c.message.answer(photo_text(), reply_markup=kb([[("🔄 Другое фото", "pic"), ("🃏 Слова для фото", "cg:photo")], [BACK]]))
    await c.answer()


# ---------- quiz ----------

def quiz_message(i: int) -> tuple[str, InlineKeyboardMarkup]:
    q = DATA["quiz"][i]
    buttons = [(o, f"qa:{i}:{j}") for j, o in enumerate(q["options"])]
    return f"❓ <b>Квиз</b> ({i + 1}/{len(DATA['quiz'])})\n\n{escape(q['q'])}", kb([buttons, [BACK]])


@router.message(Command("quiz"))
async def cmd_quiz(m: Message) -> None:
    text, markup = quiz_message(random.randrange(len(DATA["quiz"])))
    await m.answer(text, reply_markup=markup)


@router.callback_query(F.data == "quiz")
async def cb_quiz(c: CallbackQuery) -> None:
    text, markup = quiz_message(random.randrange(len(DATA["quiz"])))
    await c.message.answer(text, reply_markup=markup)
    await c.answer()


@router.callback_query(F.data.startswith("qa:"))
async def cb_quiz_answer(c: CallbackQuery) -> None:
    _, i, j = c.data.split(":")
    i, j = int(i), int(j)
    q = DATA["quiz"][i]
    ok = j == q["answer"]
    verdict = "✅ Правильно!" if ok else f"❌ Нет. Правильно: <b>{escape(q['options'][q['answer']])}</b>"
    nxt = (i + 1) % len(DATA["quiz"])
    await c.message.edit_text(
        f"❓ {escape(q['q'])}\n\n{verdict}\n💬 {escape(q['explain'])}",
        reply_markup=kb([[("➡️ Следующий", f"qn:{nxt}"), BACK]]),
    )
    await c.answer("✅" if ok else "❌")


@router.callback_query(F.data.startswith("qn:"))
async def cb_quiz_next(c: CallbackQuery) -> None:
    text, markup = quiz_message(int(c.data[3:]))
    await c.message.answer(text, reply_markup=markup)
    await c.answer()


# ---------- grammar ----------

@router.message(Command("grammar"))
async def cmd_grammar(m: Message) -> None:
    await m.answer("📚 Выбери тему грамматики:", reply_markup=grammar_menu())


def grammar_menu() -> InlineKeyboardMarkup:
    items = [(g["ru"], f"g:{g['key']}") for g in DATA["grammar"]]
    return kb(chunk(items, 2) + [[BACK]])


@router.callback_query(F.data == "gram")
async def cb_grammar(c: CallbackQuery) -> None:
    await c.message.answer("📚 Выбери тему грамматики:", reply_markup=grammar_menu())
    await c.answer()


@router.callback_query(F.data.startswith("g:"))
async def cb_grammar_item(c: CallbackQuery) -> None:
    g = GRAMMAR[c.data[2:]]
    rules = "\n".join(f"▫️ {escape(r)}" for r in g["rules"])
    ex = "\n\n".join(phrase(p) for p in g["examples"])
    await c.message.answer(
        f"📚 <b>{escape(g['lb'])}</b> ({escape(g['ru'])})\n\n{escape(g['why'])}\n\n{rules}\n\n<b>Примеры:</b>\n\n{ex}",
        reply_markup=kb([[("❓ Квиз", "quiz"), ("📚 Другая тема", "gram")], [BACK]]),
    )
    await c.answer()


# ---------- topics ----------

@router.message(Command("topics"))
async def cmd_topics(m: Message) -> None:
    await m.answer("💬 Темы устной части:", reply_markup=topics_menu())


def topics_menu() -> InlineKeyboardMarkup:
    items = [(t["ru"], f"topic:{t['key']}") for t in DATA["topics"]]
    return kb(chunk(items, 3) + [[BACK]])


@router.callback_query(F.data == "topics")
async def cb_topics(c: CallbackQuery) -> None:
    await c.message.answer("💬 Темы устной части:", reply_markup=topics_menu())
    await c.answer()


@router.callback_query(F.data.startswith("topic:"))
async def cb_topic(c: CallbackQuery) -> None:
    t = TOPICS[c.data[6:]]
    qs = "\n\n".join(phrase(q) for q in t["questions"])
    ps = "\n\n".join(phrase(p) for p in t["phrases"])
    await c.message.answer(
        f"💬 <b>{escape(t['lb'])}</b> ({escape(t['ru'])})\n\n<b>Вопросы экзаменатора:</b>\n\n{qs}\n\n<b>Твои фразы:</b>\n\n{ps}",
        reply_markup=kb([[("🃏 Карточки", f"cg:t:{t['key']}"), ("💬 Все темы", "topics")], [BACK]]),
    )
    await c.answer()


# ---------- rescue ----------

def rescue_text() -> str:
    return "🆘 <b>Фразы-спасатели</b>: если не понял(а) или забыл(а) слово\n\n" + "\n\n".join(phrase(p) for p in DATA["rescue"])


@router.message(Command("rescue"))
async def cmd_rescue(m: Message) -> None:
    await m.answer(rescue_text(), reply_markup=kb([[("🃏 Учить карточками", "cg:rescue"), BACK]]))


@router.callback_query(F.data == "rescue")
async def cb_rescue(c: CallbackQuery) -> None:
    await c.message.answer(rescue_text(), reply_markup=kb([[("🃏 Учить карточками", "cg:rescue"), BACK]]))
    await c.answer()


# ---------- RTL Today course ----------
# Lessons open on today.rtl.lu as URL buttons; nothing is copied into the bot.
# Weeks follow the learner's own schedule, and lessons can be ticked off like on the site.

RTL = DATA["rtl"]
RTL_PAGE = 10
RTL_SECTIONS = {
    "lesson": "🗣 Разговорные уроки",
    "basics": "📐 Language Basics (грамматика)",
    "exam": "✅ Итоговый тест",
    "other": "➕ Полезные материалы",
}
RTL_KIND_ORDER = {"lesson": 0, "basics": 1, "exam": 2, "other": 3}
RTL_HOME = "https://today.rtl.lu/luxembourg-insider/language"


def rtl_short(x: dict) -> str:
    return {"lesson": f"У{x['n']}", "basics": f"Г{x['n']}", "exam": "Тест", "other": "📎"}[x["kind"]]


def rtl_label(x: dict, week: int | None = None) -> str:
    tail = " 🎧" if x["audio"] else ""
    wk = f" · нед. {week}" if week is not None else ""
    return f"{rtl_short(x)} · {x['ru']}{tail}{wk}"


def rtl_url_rows(items: list[dict], p: "Plan | None" = None) -> list[list[InlineKeyboardButton]]:
    return [
        [InlineKeyboardButton(text=rtl_label(x, p.mslot.get(x["module"]) if p else None), url=x["url"])] for x in items
    ]


def rtl_of_slot(p: Plan, i: int) -> list[dict]:
    mods = p.sched.slots[i].mods
    return sorted((x for x in RTL if x["module"] in mods), key=lambda x: RTL_KIND_ORDER[x["kind"]])


def rtl_weeks(p: Plan) -> list[int]:
    return [sl.i for sl in p.sched.slots if rtl_of_slot(p, sl.i)]


def with_buttons(markup: InlineKeyboardMarkup, rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    markup.inline_keyboard += kb(rows).inline_keyboard
    return markup


def rtl_root(p: Plan | None) -> tuple[str, InlineKeyboardMarkup]:
    counts = {k: sum(1 for x in RTL if x["kind"] == k) for k in RTL_SECTIONS}
    done = sum(1 for x in RTL if p and p.state["done"].get("rtl-" + x["id"]))
    progress = f"\n\nПройдено: {done} из {len(RTL)}." if p else ""
    text = (
        "📺 <b>Курс RTL Today «Learn Luxembourgish»</b>\n\n"
        f"{counts['lesson']} разговорных уроков с аудио, {counts['basics']} уроков грамматики, "
        f"итоговый тест и {counts['other']} полезных материалов. Курс на английском.\n\n"
        "По плану: одна сессия 30–40 минут в выходные, урок недели и его грамматика. "
        f"Кнопки открывают страницы на сайте RTL Today. 🎧 — есть аудио.{progress}"
    )
    rows = []
    if p:
        weeks = rtl_weeks(p)
        nxt = next((i for i in weeks if i >= p.cur), None)
        if nxt is not None:
            rows.append([("⭐ Уроки этой недели" if nxt == p.cur else f"⭐ Ближайшие: неделя {nxt}", f"rtlw:{nxt}")])
        rows.append([("📅 По неделям плана", "rtlws")])
    rows += [
        [(RTL_SECTIONS["lesson"], "rtls:lesson:0"), (RTL_SECTIONS["basics"].split(" (")[0], "rtls:basics:0")],
        [(RTL_SECTIONS["exam"], "rtls:exam:0"), (RTL_SECTIONS["other"], "rtls:other:0")],
    ]
    markup = kb(rows)
    markup.inline_keyboard.append([InlineKeyboardButton(text="🌐 Раздел на RTL Today", url=RTL_HOME)])
    return text, with_buttons(markup, [[BACK]])


@router.message(Command("rtl"))
async def cmd_rtl(m: Message) -> None:
    text, markup = rtl_root(await load_plan(m.from_user.id))
    await m.answer(text, reply_markup=markup, disable_web_page_preview=True)


@router.callback_query(F.data == "rtl")
async def cb_rtl(c: CallbackQuery) -> None:
    await c.answer()
    text, markup = rtl_root(await load_plan(c.from_user.id))
    await c.message.answer(text, reply_markup=markup, disable_web_page_preview=True)


@router.callback_query(F.data == "rtlhome")
async def cb_rtl_home(c: CallbackQuery) -> None:
    await c.answer()
    await show(c, *rtl_root(await load_plan(c.from_user.id)))


@router.callback_query(F.data == "rtlws")
async def cb_rtl_weeks(c: CallbackQuery, state: FSMContext) -> None:
    await c.answer()
    p = await load_plan(c.from_user.id)
    if not p:
        return await need_setup(c.message, state)
    items = [(f"{'👉 ' if i == p.cur else ''}Нед. {i}", f"rtlw:{i}") for i in rtl_weeks(p)]
    await show(
        c,
        "📅 <b>Курс RTL по неделям плана</b>\n\nВыбери неделю (👉 — текущая):",
        kb(chunk(items, 4) + [[("⬅️ Курс RTL", "rtlhome")]]),
    )


def rtl_week_view(p: Plan, i: int) -> tuple[str, InlineKeyboardMarkup]:
    items = rtl_of_slot(p, i)
    now = " (сейчас)" if i == p.cur else ""
    dates = f"\n{p.dates(i)}" if p.sched.slots[i].start else ""
    status = "\n".join(
        f"{'✅' if p.done('rtl-' + x['id']) else '⬜'} {rtl_short(x)} · {escape(x['ru'])}" for x in items
    )
    text = (
        f"📺 <b>Неделя {i}: {escape(p.title(i))}</b>{now}{dates}\n\n{status}\n\n"
        "Открой урок кнопкой со ссылкой, а пройденное отметь кнопками с галочками."
    )
    weeks = rtl_weeks(p)
    j = weeks.index(i) if i in weeks else -1
    nav = []
    if j > 0:
        nav.append((f"◀️ Нед. {weeks[j - 1]}", f"rtlw:{weeks[j - 1]}"))
    if 0 <= j < len(weeks) - 1:
        nav.append((f"Нед. {weeks[j + 1]} ▶️", f"rtlw:{weeks[j + 1]}"))
    marks = [(f"{'✅' if p.done('rtl-' + x['id']) else '⬜'} {rtl_short(x)}", f"rt:{i}:{x['id']}") for x in items]
    markup = InlineKeyboardMarkup(inline_keyboard=rtl_url_rows(items))
    return text, with_buttons(markup, chunk(marks, 4) + [nav, [("📅 Все недели", "rtlws"), ("⬅️ Курс RTL", "rtlhome")]])


@router.callback_query(F.data.startswith("rtlw:"))
async def cb_rtl_week(c: CallbackQuery, state: FSMContext) -> None:
    await c.answer()
    p = await load_plan(c.from_user.id)
    if not p:
        return await need_setup(c.message, state)
    await show(c, *rtl_week_view(p, min(int(c.data[5:]), len(p.sched.slots) - 1)))


@router.callback_query(F.data.startswith("rt:"))
async def cb_rtl_mark(c: CallbackQuery) -> None:
    _, i, rid = c.data.split(":", 2)
    p = await toggle(c, "done", "rtl-" + rid)
    if p:
        await show(c, *rtl_week_view(p, min(int(i), len(p.sched.slots) - 1)))


@router.callback_query(F.data.startswith("rtls:"))
async def cb_rtl_section(c: CallbackQuery) -> None:
    await c.answer()
    _, kind, page = c.data.split(":")
    page = int(page)
    p = await load_plan(c.from_user.id)
    items = [x for x in RTL if x["kind"] == kind]
    pages = (len(items) + RTL_PAGE - 1) // RTL_PAGE
    shown = items[page * RTL_PAGE : (page + 1) * RTL_PAGE]
    pager = f" · стр. {page + 1}/{pages}" if pages > 1 else ""
    hint = "Номер недели твоего плана показан справа. " if p else ""
    text = f"{RTL_SECTIONS[kind]}{pager}\n\n{hint}🎧 — есть аудио."
    nav = []
    if page > 0:
        nav.append(("◀️ Назад", f"rtls:{kind}:{page - 1}"))
    if page < pages - 1:
        nav.append(("Дальше ▶️", f"rtls:{kind}:{page + 1}"))
    markup = InlineKeyboardMarkup(inline_keyboard=rtl_url_rows(shown, p))
    await show(c, text, with_buttons(markup, [nav, [("⬅️ Курс RTL", "rtlhome")]]))


@router.message()
async def fallback(m: Message) -> None:
    await m.answer("Не понял 🙂 Выбери в меню:", reply_markup=MENU)


@router.errors()
async def on_error(event: ErrorEvent) -> bool:
    if not isinstance(event.exception, StoreError):
        return False
    text = f"⚠️ Не получилось связаться с базой прогресса ({escape(str(event.exception))}). Попробуй ещё раз через минуту."
    u = event.update
    if u.callback_query:
        await u.callback_query.answer("Ошибка связи, попробуй ещё раз", show_alert=False)
        await u.callback_query.message.answer(text)
    elif u.message:
        await u.message.answer(text)
    return True


# ---------- startup ----------

COMMANDS = [
    BotCommand(command="today", description="Задачи этой недели"),
    BotCommand(command="plan", description="Весь план по неделям"),
    BotCommand(command="menu", description="Главное меню"),
    BotCommand(command="cards", description="Карточки"),
    BotCommand(command="sim", description="Симулятор устной части"),
    BotCommand(command="photo", description="Описание фото"),
    BotCommand(command="quiz", description="Квиз по грамматике"),
    BotCommand(command="grammar", description="Грамматика"),
    BotCommand(command="topics", description="Темы экзамена"),
    BotCommand(command="rescue", description="Фразы-спасатели"),
    BotCommand(command="rtl", description="Курс RTL Today"),
    BotCommand(command="settings", description="Дата экзамена, уровень, код"),
]


def main() -> None:
    global store
    logging.basicConfig(level=logging.INFO)
    token = os.environ["BOT_TOKEN"]
    # BOT_SECRET turns a Telegram id into the learner's sync code; changing it loses everyone's link to their progress
    secret = os.environ.get("BOT_SECRET") or hashlib.sha256(token.encode()).hexdigest()
    store = Store(DATA, secret.encode())
    bot = Bot(token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    base_url = os.environ.get("WEBHOOK_URL") or os.environ.get("RENDER_EXTERNAL_URL")

    if not base_url:
        async def run_polling() -> None:
            await bot.set_my_commands(COMMANDS)
            await bot.delete_webhook(drop_pending_updates=True)
            await router.start_polling(bot)

        asyncio.run(run_polling())
        return

    from aiohttp import web
    from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

    secret_token = os.environ.get("WEBHOOK_SECRET") or token.split(":")[-1][:32]
    path = "/webhook"

    async def on_startup(bot: Bot) -> None:
        await bot.set_my_commands(COMMANDS)
        await bot.set_webhook(base_url.rstrip("/") + path, secret_token=secret_token, drop_pending_updates=True)

    router.startup.register(on_startup)
    app = web.Application()
    app.router.add_get("/", lambda _: web.Response(text="ok"))
    SimpleRequestHandler(dispatcher=router, bot=bot, secret_token=secret_token).register(app, path=path)
    setup_application(app, router, bot=bot)
    web.run_app(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))


if __name__ == "__main__":
    main()
