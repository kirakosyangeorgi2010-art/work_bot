"""Telegram-бот. Запуск:  python bot.py

Команды в Telegram:
  /start  — приветствие (и ваш Telegram ID для файла .env)
  /next   — следующий салон с готовым текстом
  /stats  — сколько найдено и написано
"""
import html
import json
import logging
import os
import re
import threading
import time
from urllib.parse import quote

import telebot
from dotenv import load_dotenv
from telebot import types
from telebot.apihelper import ApiTelegramException

import db
import finder
import writer

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("bot")

TOKEN = os.environ["TELEGRAM_TOKEN"]
OWNER_ID = os.getenv("OWNER_ID", "").strip()
NICHE = os.getenv("NICHE", "beauty_salons").strip()
QUEUE_SIZE = int(os.getenv("QUEUE_SIZE", "5"))

niche = json.load(open(f"niches/{NICHE}.json", encoding="utf-8"))
DEFAULT_LANG = niche["text"].get("default_language", "hy")

db.connect()
bot = telebot.TeleBot(TOKEN, parse_mode="HTML")

wake = threading.Event()         # «разбудить» фоновый поиск
gen_lock = threading.Lock()      # чтобы два текста для одного салона не писались одновременно
state = {"search": "ещё не запускался", "error": ""}

# Мобильные коды Армении: на такие номера можно писать в WhatsApp/Telegram
MOBILE_CODES = {"33", "41", "43", "44", "47", "49", "55", "77", "91", "93", "94", "95", "96", "97", "98", "99"}
LANG_LABEL = {"hy": "армянский", "ru": "русский"}
DONE_LABEL = {"w": ("written", "✅ Написал"), "s": ("skipped", "⏭ Пропущен"), "r": ("rejected", "🚫 Не подходит")}


# ---------------- тексты и очередь ----------------

def offer_filled():
    return not any("ЗАПОЛНИТЕ" in str(v) for v in niche["my_offer"].values())


def make_text(place_id, lang):
    with gen_lock:
        p = db.get_place(place_id)
        old = p["text_hy"] if lang == "hy" else p["text_ru"]
        if old:
            return old
        text, who = writer.generate(niche, p, lang)
        log.info("Текст для «%s» (%s) написал %s", p["name"], lang, who)
        db.set_text(place_id, lang, text)
        return text


def prepare_one():
    """Пишет текст для одного найденного салона. True — если было что готовить."""
    p = db.first_with_status(NICHE, "new")
    if p is None:
        return False
    make_text(p["id"], DEFAULT_LANG)
    db.change_status(p["id"], "new", "ready")
    return True


def take_next():
    """Берёт следующий готовый салон и помечает его показанным (чтобы не было повторов)."""
    while True:
        p = db.first_with_status(NICHE, "ready")
        if p is None:
            return None
        if db.change_status(p["id"], "ready", "shown"):
            return db.get_place(p["id"])


def worker():
    """Фон: держит в очереди QUEUE_SIZE готовых салонов."""
    while True:
        try:
            if db.count_status(NICHE, "ready") < QUEUE_SIZE and offer_filled():
                if prepare_one():
                    state["error"] = ""
                    continue
                state["search"] = "загружаю салоны из открытых карт…"
                res = finder.find_more(NICHE, niche)
                if res == finder.EXHAUSTED:
                    state["search"] = ("все салоны из файла ниши загружены. Новые появятся с выпуском "
                                       "Overture (раз в месяц) или добавьте город в файл ниши")
                else:
                    state["search"] = f"последняя загрузка: +{res} подходящих"
                    state["error"] = ""
                    continue
        except Exception as e:
            log.exception("Ошибка в фоне")
            state["error"] = str(e)[:400]
            time.sleep(300)
            continue
        wake.wait(600)
        wake.clear()


# ---------------- карточка салона ----------------

def esc(s):
    return html.escape(str(s or ""))


def card_text(p, lang):
    text = p["text_hy"] if lang == "hy" else p["text_ru"]
    lines = [f"<b>{esc(p['name'])}</b>", f"📍 {esc(p['address'])}"]
    if p["phone"]:
        lines.append(f"📞 {esc(p['phone'])}")
    if p["social_url"]:
        lines.append(f"🔗 {esc(p['social_url'])}")
    if p["rating"]:
        lines.append(f"⭐ {p['rating']} ({p['reviews_count']} отзывов), последний отзыв {esc(p['last_review'])}")
    lines.append(f"\n<b>Почему подходит:</b> {esc(p['reason'])}")
    lines.append(f"\n<b>Текст ({LANG_LABEL[lang]})</b> — следующим сообщением ⬇️")
    return "\n".join(lines)


def mobile_digits(phone):
    d = re.sub(r"\D", "", phone or "")
    return d if len(d) == 11 and d.startswith("374") and d[3:5] in MOBILE_CODES else ""


def link_buttons(p, lang, with_text=True):
    btns = []
    d = mobile_digits(p["phone"])
    if d:
        text = p["text_hy"] if lang == "hy" else p["text_ru"]
        wa = f"https://wa.me/{d}"
        if with_text and text:
            wa += "?text=" + quote(text)
        btns.append(types.InlineKeyboardButton("WhatsApp", url=wa))
        btns.append(types.InlineKeyboardButton("Telegram", url=f"https://t.me/+{d}"))
    if p["social_url"]:
        url = p["social_url"]
        label = "Instagram" if "instagram" in url else "Facebook" if "facebook" in url else "Соцсеть"
        btns.append(types.InlineKeyboardButton(label, url=url))
    if p["maps_url"]:
        btns.append(types.InlineKeyboardButton("Google Maps", url=p["maps_url"]))
    return btns


def keyboard(p, lang, finished=False, with_text=True):
    kb = types.InlineKeyboardMarkup()
    links = link_buttons(p, lang, with_text)
    if links:
        kb.row(*links)
    if not finished:
        pid = p["id"]
        kb.row(types.InlineKeyboardButton("✅ Написал", callback_data=f"w|{lang}|{pid}"),
               types.InlineKeyboardButton("⏭ Пропустить", callback_data=f"s|{lang}|{pid}"),
               types.InlineKeyboardButton("🚫 Не подходит", callback_data=f"r|{lang}|{pid}"))
        other = "ru" if lang == "hy" else "hy"
        kb.row(types.InlineKeyboardButton(f"Текст на {'русском' if other == 'ru' else 'армянском'}",
                                          callback_data=f"{other}|{lang}|{pid}"))
    return kb


text_msgs = {}  # (чат, id карточки) -> id сообщения с текстом


def plain_text(p, lang):
    return p["text_hy"] if lang == "hy" else p["text_ru"]


def send_card(chat_id, p, lang):
    try:
        card = bot.send_message(chat_id, card_text(p, lang), reply_markup=keyboard(p, lang),
                                disable_web_page_preview=True)
    except ApiTelegramException:
        # слишком длинная ссылка WhatsApp с текстом — шлём ссылку без текста
        card = bot.send_message(chat_id, card_text(p, lang), reply_markup=keyboard(p, lang, with_text=False),
                                disable_web_page_preview=True)
    # Текст — отдельным сообщением БЕЗ форматирования: долгое нажатие -> «Копировать» копирует только его
    sent = bot.send_message(chat_id, plain_text(p, lang), parse_mode="", disable_web_page_preview=True)
    text_msgs[(chat_id, card.message_id)] = sent.message_id


def update_text_message(msg, p, lang):
    """После смены языка обновляем отдельное сообщение с текстом (или шлём новое)."""
    key = (msg.chat.id, msg.message_id)
    text = plain_text(p, lang)
    if key in text_msgs:
        try:
            bot.edit_message_text(text, msg.chat.id, text_msgs[key], parse_mode="",
                                  disable_web_page_preview=True)
            return
        except ApiTelegramException as e:
            if "not modified" in str(e):
                return
    sent = bot.send_message(msg.chat.id, text, parse_mode="", disable_web_page_preview=True)
    text_msgs[key] = sent.message_id


def edit_card(msg, p, lang, finished=False, footer=""):
    text = card_text(p, lang) + footer
    for with_text in (True, False):
        try:
            bot.edit_message_text(text, msg.chat.id, msg.message_id, disable_web_page_preview=True,
                                  reply_markup=keyboard(p, lang, finished, with_text))
            return
        except ApiTelegramException as e:
            if "not modified" in str(e):
                return
    log.warning("Не удалось обновить карточку")


# ---------------- команды ----------------

def is_owner(user_id):
    return OWNER_ID and str(user_id) == OWNER_ID


@bot.message_handler(commands=["start"])
def cmd_start(m):
    if not OWNER_ID:
        bot.reply_to(m, f"Ваш Telegram ID: <code>{m.from_user.id}</code>\n"
                        "Впишите его в файл .env в строку OWNER_ID= и перезапустите бота.")
        return
    if not is_owner(m.from_user.id):
        return
    msg = f"Ниша: <b>{esc(niche['title'])}</b>\n/next — следующий салон\n/stats — статистика"
    if not offer_filled():
        msg += f"\n\n⚠️ Заполните my_offer в файле niches/{NICHE}.json (что входит, цена, пример) и перезапустите бота."
    bot.reply_to(m, msg)


@bot.message_handler(commands=["next"])
def cmd_next(m):
    if not is_owner(m.from_user.id):
        return
    if not offer_filled():
        bot.reply_to(m, f"Сначала заполните my_offer в niches/{NICHE}.json и перезапустите бота.")
        return
    p = take_next()
    if p is None:
        new = db.first_with_status(NICHE, "new")
        if new is not None:
            bot.send_message(m.chat.id, "Пишу текст, подождите до минуты…")
            try:
                make_text(new["id"], DEFAULT_LANG)
                db.change_status(new["id"], "new", "ready")
            except Exception as e:
                bot.send_message(m.chat.id, f"Не получилось написать текст:\n{esc(e)}")
                return
            p = take_next()
    wake.set()  # пусть фон пополнит очередь
    if p is None:
        msg = "Очередь пуста, ищу новые салоны. Попробуйте /next через минуту."
        msg += f"\nПоиск: {esc(state['search'])}"
        if state["error"]:
            msg += f"\nОшибка: {esc(state['error'])}"
        bot.send_message(m.chat.id, msg)
        return
    send_card(m.chat.id, p, DEFAULT_LANG)


@bot.message_handler(commands=["stats"])
def cmd_stats(m):
    if not is_owner(m.from_user.id):
        return
    s = db.stats(NICHE)
    msg = (f"<b>{esc(niche['title'])}</b>\n"
           f"Готово в очереди: {s.get('ready', 0)}\n"
           f"Найдено, ждут текста: {s.get('new', 0)}\n"
           f"Показано без ответа: {s.get('shown', 0)}\n"
           f"Написал: {s.get('written', 0)}\n"
           f"Пропущено: {s.get('skipped', 0)}\n"
           f"Не подходит: {s.get('rejected', 0)}\n"
           f"Отсеяно фильтром: {s.get('filtered_out', 0)}\n\n"
           f"Поиск: {esc(state['search'])}")
    if state["error"]:
        msg += f"\nОшибка: {esc(state['error'])}"
    bot.send_message(m.chat.id, msg)


@bot.callback_query_handler(func=lambda c: True)
def on_button(c):
    if not is_owner(c.from_user.id):
        return
    action, lang, pid = c.data.split("|", 2)
    p = db.get_place(pid)
    if p is None:
        bot.answer_callback_query(c.id, "Салон не найден")
        return

    if action in ("hy", "ru"):  # переключить язык текста
        bot.answer_callback_query(c.id, "Пишу текст…")
        try:
            make_text(pid, action)
        except Exception as e:
            bot.send_message(c.message.chat.id, f"Не получилось написать текст:\n{esc(e)}")
            return
        p = db.get_place(pid)
        edit_card(c.message, p, action)
        update_text_message(c.message, p, action)
        return

    status, label = DONE_LABEL[action]
    db.set_status(pid, status)
    bot.answer_callback_query(c.id, label)
    edit_card(c.message, p, lang, finished=True, footer=f"\n\n<b>{label}</b>")


if __name__ == "__main__":
    if not OWNER_ID:
        log.info("OWNER_ID не задан: напишите боту /start, он пришлёт ваш ID.")
    if not offer_filled():
        log.warning("Заполните my_offer в niches/%s.json", NICHE)
    threading.Thread(target=worker, daemon=True).start()
    log.info("Бот запущен. Остановить: Ctrl+C")
    bot.infinity_polling()
