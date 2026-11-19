"""Telegram-бот. Запуск:  python bot.py

Команды в Telegram:
  /start  — приветствие (и ваш Telegram ID для файла .env)
  /next    — следующий салон с готовым текстом
  /regions — выбрать города/регионы для поиска
  /stats   — сколько найдено и написано
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
CITIES = {c["id"]: c for c in niche["search"]["cities"]}          # ID города -> его настройки
COUNTRIES = niche["search"].get("countries", {"AM": "Армения"})    # код страны -> название
SETTING_CITIES = f"cities:{NICHE}"

db.connect()
bot = telebot.TeleBot(TOKEN, parse_mode="HTML")

wake = threading.Event()         # «разбудить» фоновый поиск
gen_lock = threading.Lock()      # чтобы два текста для одного салона не писались одновременно
state = {"search": "ещё не запускался", "error": ""}

# Мобильные коды Армении: на такие номера можно писать в WhatsApp/Telegram
MOBILE_CODES = {"33", "41", "43", "44", "47", "49", "55", "77", "91", "93", "94", "95", "96", "97", "98", "99"}
LANG_LABEL = {"hy": "армянский", "ru": "русский"}
LANG_BUTTON = {"hy": "Текст на армянском", "ru": "Текст на русском"}
DONE_LABEL = {"w": ("written", "✅ Написал"), "s": ("skipped", "⏭ Пропущен"), "r": ("rejected", "🚫 Не подходит")}


# ---------------- регионы ----------------

def selected():
    """ID выбранных городов (сохраняются в базе, переживают перезапуск)."""
    raw = db.get_setting(SETTING_CITIES)
    ids = json.loads(raw) if raw else niche["search"].get("default_cities", [next(iter(CITIES))])
    return [i for i in ids if i in CITIES]


def set_selected(ids):
    db.set_setting(SETTING_CITIES, json.dumps([i for i in CITIES if i in ids]))


def city_of(p):
    return CITIES.get(p["city"]) or next(iter(CITIES.values()))


def langs_for(p):
    """Языки текста для салона: для Армении армянский и русский, для России только русский."""
    return niche["text"].get("languages", {}).get(city_of(p).get("country", "AM"), ["ru"])


def default_lang(p):
    return langs_for(p)[0]


def selected_names():
    return ", ".join(CITIES[i]["name"] for i in selected()) or "не выбраны"


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
    p = db.first_with_status(NICHE, "new", selected())
    if p is None:
        return False
    make_text(p["id"], default_lang(p))
    db.change_status(p["id"], "new", "ready")
    return True


def take_next():
    """Берёт следующий готовый салон из выбранных регионов и помечает его показанным (без повторов)."""
    while True:
        p = db.first_with_status(NICHE, "ready", selected())
        if p is None:
            return None
        if db.change_status(p["id"], "ready", "shown"):
            return db.get_place(p["id"])


def worker():
    """Фон: держит в очереди QUEUE_SIZE готовых салонов."""
    while True:
        try:
            sel = selected()
            if sel and db.count_status(NICHE, "ready", sel) < QUEUE_SIZE and offer_filled():
                if prepare_one():
                    state["error"] = ""
                    continue
                state["search"] = "загружаю салоны из открытых карт…"
                res = finder.find_more(NICHE, niche, sel)
                if res == finder.EXHAUSTED:
                    state["search"] = ("все выбранные регионы загружены. Новые салоны появятся с выпуском "
                                       "Overture (раз в месяц), или выберите ещё регионы: /regions")
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
    city = city_of(p)["name"]
    address = f"{city}, {p['address']}" if p["address"] else city
    lines = [f"<b>{esc(p['name'])}</b>", f"📍 {esc(address)}"]
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
    """Цифры мобильного номера (Армения +374 или Россия +7 9xx), иначе пусто.
    На городские номера WhatsApp/Telegram не работают."""
    d = re.sub(r"\D", "", phone or "")
    if len(d) == 11 and d.startswith("374") and d[3:5] in MOBILE_CODES:
        return d
    if len(d) == 11 and d.startswith("79"):
        return d
    return ""


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
        others = [x for x in langs_for(p) if x != lang]
        if others:  # для России язык один — кнопки переключения нет
            kb.row(types.InlineKeyboardButton(LANG_BUTTON[others[0]],
                                              callback_data=f"{others[0]}|{lang}|{pid}"))
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
    msg = (f"Ниша: <b>{esc(niche['title'])}</b>\nРегионы: {esc(selected_names())}\n\n"
           "/next — следующий салон\n/regions — выбрать регионы\n/stats — статистика")
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
    if not selected():
        bot.reply_to(m, "Регионы не выбраны. Выберите их: /regions")
        return
    p = take_next()
    if p is None:
        new = db.first_with_status(NICHE, "new", selected())
        if new is not None:
            bot.send_message(m.chat.id, "Пишу текст, подождите до минуты…")
            try:
                make_text(new["id"], default_lang(new))
                db.change_status(new["id"], "new", "ready")
            except Exception as e:
                bot.send_message(m.chat.id, f"Не получилось написать текст:\n{esc(e)}")
                return
            p = take_next()
    wake.set()  # пусть фон пополнит очередь
    if p is None:
        msg = ("Очередь пуста, загружаю салоны выбранных регионов. "
               "Попробуйте /next через минуту (большие города, например Москва, грузятся дольше).")
        msg += f"\nРегионы: {esc(selected_names())}\nПоиск: {esc(state['search'])}"
        if state["error"]:
            msg += f"\nОшибка: {esc(state['error'])}"
        bot.send_message(m.chat.id, msg)
        return
    send_card(m.chat.id, p, default_lang(p))


@bot.message_handler(commands=["stats"])
def cmd_stats(m):
    if not is_owner(m.from_user.id):
        return
    s = db.stats(NICHE, selected())
    msg = (f"<b>{esc(niche['title'])}</b>\n"
           f"Регионы: {esc(selected_names())}\n\n"
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


def regions_keyboard():
    sel = set(selected())
    kb = types.InlineKeyboardMarkup()
    for code, country in COUNTRIES.items():
        ids = [i for i, c in CITIES.items() if c.get("country") == code]
        all_on = bool(ids) and all(i in sel for i in ids)
        kb.row(types.InlineKeyboardButton(f"{'✅' if all_on else '⬜'} {country}: все города",
                                          callback_data=f"regc|{code}"))
        row = []
        for i in ids:
            row.append(types.InlineKeyboardButton(f"{'✅ ' if i in sel else ''}{CITIES[i]['name']}",
                                                  callback_data=f"reg|{i}"))
            if len(row) == 2:
                kb.row(*row)
                row = []
        if row:
            kb.row(*row)
    kb.row(types.InlineKeyboardButton("Готово", callback_data="regdone"))
    return kb


@bot.message_handler(commands=["regions"])
def cmd_regions(m):
    if not is_owner(m.from_user.id):
        return
    bot.send_message(m.chat.id, "Выберите регионы для поиска (можно несколько), затем «Готово».\n"
                                f"Сейчас: {esc(selected_names())}", reply_markup=regions_keyboard())


@bot.callback_query_handler(func=lambda c: c.data.startswith("reg"))
def on_region(c):
    if not is_owner(c.from_user.id):
        return
    sel = set(selected())
    if c.data == "regdone":
        bot.answer_callback_query(c.id, "Сохранено")
        msg = f"Регионы: <b>{esc(selected_names())}</b>\nТеперь /next"
        if not sel:
            msg = "Регионы не выбраны. Выберите хотя бы один: /regions"
        bot.edit_message_text(msg, c.message.chat.id, c.message.message_id)
        wake.set()
        return
    kind, value = c.data.split("|", 1)
    if kind == "regc":  # вся страна: включить всё или выключить всё
        ids = {i for i, x in CITIES.items() if x.get("country") == value}
        sel = sel - ids if ids <= sel else sel | ids
    elif value in sel:
        sel.discard(value)
    else:
        sel.add(value)
    set_selected(sel)
    bot.answer_callback_query(c.id)
    try:
        bot.edit_message_text("Выберите регионы для поиска (можно несколько), затем «Готово».\n"
                              f"Сейчас: {esc(selected_names())}", c.message.chat.id, c.message.message_id,
                              reply_markup=regions_keyboard())
    except ApiTelegramException as e:
        if "not modified" not in str(e):
            raise
    wake.set()  # фон сразу начнёт загружать новые регионы


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
    try:  # меню команд в Telegram (кнопка «/» рядом с полем ввода)
        bot.set_my_commands([
            types.BotCommand("next", "Следующий салон"),
            types.BotCommand("regions", "Выбрать регионы"),
            types.BotCommand("stats", "Статистика"),
            types.BotCommand("start", "Помощь"),
        ])
    except Exception:
        log.warning("Не удалось обновить меню команд")
    threading.Thread(target=worker, daemon=True).start()
    log.info("Бот запущен. Регионы: %s", selected_names())
    bot.infinity_polling()
