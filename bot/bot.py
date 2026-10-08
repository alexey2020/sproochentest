"""Telegram bot for Sproochentest preparation.

Uses the same material as the web trainer (index.html), exported to data.json
by tools/extract_data.js.

Runs in long-polling mode by default. If WEBHOOK_URL (or Render's
RENDER_EXTERNAL_URL) is set, it starts an aiohttp server on $PORT and
receives updates by webhook instead.
"""

import asyncio
import json
import logging
import os
import random
from datetime import date
from html import escape
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

DATA = json.loads((Path(__file__).parent / "data.json").read_text(encoding="utf-8"))
EXAM_DATE = date(*DATA["exam_date"])
TOPICS = {t["key"]: t for t in DATA["topics"]}
GRAMMAR = {g["key"]: g for g in DATA["grammar"]}

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


def kb(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows]
    )


def chunk(items: list[tuple[str, str]], n: int) -> list[list[tuple[str, str]]]:
    return [items[i : i + n] for i in range(0, len(items), n)]


MENU = kb(
    [
        [("📅 Сегодня", "today"), ("🃏 Карточки", "cards")],
        [("🎤 Устная часть", "sim"), ("🖼 Описание фото", "pic")],
        [("❓ Квиз", "quiz"), ("📚 Грамматика", "gram")],
        [("💬 Темы", "topics"), ("🆘 Спасатели", "rescue")],
    ]
)
BACK = ("⬅️ Меню", "menu")


def phrase(p: dict) -> str:
    tr = f"\n<code>{escape(p['tr'])}</code>" if p.get("tr") else ""
    return f"<i>{escape(p['lb'])}</i>{tr}\n{escape(p['ru'])}"


def current_week() -> tuple[int, dict] | None:
    today = date.today()
    found = None
    for i, w in enumerate(DATA["weeks"]):
        if date(*w["start"]) <= today:
            found = (i, w)
    return found


# ---------- menu / start ----------

WELCOME = (
    "Moien! 👋 Я помогу подготовиться к <b>Sproochentest</b>.\n\n"
    "• <b>Сегодня</b>: что делать на этой неделе по плану\n"
    "• <b>Карточки</b>: слова и фразы с транскрипцией\n"
    "• <b>Устная часть</b>: вопрос экзаменатора, ты отвечаешь вслух или голосовым\n"
    "• <b>Описание фото</b>: сцена и шаблон ответа\n"
    "• <b>Квиз</b>: грамматика с объяснениями\n\n"
    "Выбирай 👇"
)


@router.message(CommandStart())
@router.message(Command("menu"))
async def cmd_start(m: Message) -> None:
    await m.answer(WELCOME, reply_markup=MENU)


@router.callback_query(F.data == "menu")
async def cb_menu(c: CallbackQuery) -> None:
    await c.message.answer(WELCOME, reply_markup=MENU)
    await c.answer()


# ---------- today ----------

def today_text() -> str:
    days = (EXAM_DATE - date.today()).days
    head = f"🗓 До экзамена <b>{days}</b> дн. ({EXAM_DATE:%d.%m.%Y})\n\n" if days >= 0 else "Экзамен уже прошёл 🎉\n\n"
    cw = current_week()
    if not cw:
        return head + "План ещё не начался."
    i, w = cw
    phase = next((p for p in DATA["phases"] if i in p["weeks"]), None)
    topic = TOPICS.get(w["topic"])
    lines = [head + f"<b>Неделя {i}: {escape(w['title'])}</b>"]
    if phase:
        lines.append(f"Этап: {escape(phase['name'])} ({escape(phase['range'])})")
    if w["grammar"] and w["grammar"] != "—":
        lines.append(f"📚 Грамматика: {escape(w['grammar'])}")
    if topic:
        lines.append(f"💬 Тема: {escape(topic['lb'])} ({escape(topic['ru'])})")
    lines.append(f"🎧 {escape(w['listening'])}")
    lines.append("\n<b>Задачи:</b>")
    lines += [f"▫️ {escape(t)}" for t in w["tasks"]]
    return "\n".join(lines)


@router.message(Command("today"))
async def cmd_today(m: Message) -> None:
    await m.answer(today_text(), reply_markup=today_kb())


def today_kb() -> InlineKeyboardMarkup:
    cw = current_week()
    rows = []
    if cw and cw[1]["topic"] in TOPICS:
        rows.append([("💬 Тема недели", f"topic:{cw[1]['topic']}"), ("🃏 Её фразы", f"cg:t:{cw[1]['topic']}")])
    rows.append([BACK])
    return kb(rows)


@router.callback_query(F.data == "today")
async def cb_today(c: CallbackQuery) -> None:
    await c.message.answer(today_text(), reply_markup=today_kb())
    await c.answer()


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


@router.message()
async def fallback(m: Message) -> None:
    await m.answer("Не понял 🙂 Выбери в меню:", reply_markup=MENU)


# ---------- startup ----------

COMMANDS = [
    BotCommand(command="menu", description="Главное меню"),
    BotCommand(command="today", description="План на эту неделю"),
    BotCommand(command="cards", description="Карточки"),
    BotCommand(command="sim", description="Симулятор устной части"),
    BotCommand(command="photo", description="Описание фото"),
    BotCommand(command="quiz", description="Квиз по грамматике"),
    BotCommand(command="grammar", description="Грамматика"),
    BotCommand(command="topics", description="Темы экзамена"),
    BotCommand(command="rescue", description="Фразы-спасатели"),
]


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    token = os.environ["BOT_TOKEN"]
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

    secret = os.environ.get("WEBHOOK_SECRET") or token.split(":")[-1][:32]
    path = "/webhook"

    async def on_startup(bot: Bot) -> None:
        await bot.set_my_commands(COMMANDS)
        await bot.set_webhook(base_url.rstrip("/") + path, secret_token=secret, drop_pending_updates=True)

    router.startup.register(on_startup)
    app = web.Application()
    app.router.add_get("/", lambda _: web.Response(text="ok"))
    SimpleRequestHandler(dispatcher=router, bot=bot, secret_token=secret).register(app, path=path)
    setup_application(app, router, bot=bot)
    web.run_app(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))


if __name__ == "__main__":
    main()
