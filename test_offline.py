"""Проверка без интернета:  python test_offline.py"""
import json
import os
import tempfile

import db
import finder

niche = json.load(open("niches/beauty_salons.json", encoding="utf-8"))
flt = niche["filter"]


def place(**kw):
    p = {"id": "id1", "name": "Beauty Lab", "address": "Ереван", "phones": ["+37499123456"],
         "websites": None, "socials": ["https://www.facebook.com/123"], "confidence": 0.9,
         "operating_status": None, "lat": 40.18, "lon": 44.51}
    p.update(kw)
    return p


def check(name, p, expected):
    ok, info = finder.check_place(p, flt)
    status = "OK  " if ok == expected else "FAIL"
    print(f"{status} {name}: {ok} — {info['reason']}")
    assert ok == expected, name
    return info


info = check("без сайта, есть Facebook", place(), True)
assert info["maps_url"].startswith("https://www.google.com/maps/search/")
info = check("Instagram в поле сайта", place(websites=["https://instagram.com/beautylab"]), True)
assert "instagram" in info["social_url"], "Instagram должен показываться первым"
check("свой сайт", place(websites=["https://beautylab.am"]), False)
check("закрыт", place(operating_status="permanently_closed"), False)
check("низкая уверенность", place(confidence=0.5), False)
check("нет контактов", place(phones=[], socials=[]), False)
check("только телефон", place(socials=None), True)

# База: повторов нет, статусы меняются правильно
tmp = os.path.join(tempfile.mkdtemp(), "t.db")
db.connect(tmp)
_, info = finder.check_place(place(), flt)
assert db.add_place("n", info, "new") == 1
assert db.add_place("n", info, "new") == 0, "повтор добавился!"
assert db.change_status("id1", "new", "ready")
assert db.change_status("id1", "ready", "shown")
assert not db.change_status("id1", "ready", "shown"), "показали дважды!"
print("OK   база: без повторов")

# Ссылки WhatsApp: только для мобильных номеров
os.environ.setdefault("TELEGRAM_TOKEN", "123:test")
import bot  # noqa: E402
assert bot.mobile_digits("+374 99 123456") == "37499123456"
assert bot.mobile_digits("+374 10 123456") == "", "городской номер не для WhatsApp"
assert bot.mobile_digits("+7 912 345-67-89") == "79123456789", "российский мобильный"
assert bot.mobile_digits("+7 495 123-45-67") == "", "московский городской не для WhatsApp"
print("OK   ссылки WhatsApp (Армения и Россия)")

# Регионы: выбор сохраняется, /next берёт салоны только из выбранных городов
import writer  # noqa: E402

db.connect(os.path.join(tempfile.mkdtemp(), "r.db"))  # отдельная пустая база, настоящую не трогаем
assert bot.selected() == ["yerevan"], "по умолчанию Ереван"
bot.set_selected({"gyumri", "kazan"})
assert set(bot.selected()) == {"gyumri", "kazan"}
for pid, city, phone in [("p_yer", "yerevan", "+37499000001"), ("p_gyu", "gyumri", "+37499000002"),
                         ("p_kaz", "kazan", "+79120000003")]:
    db.add_place(bot.NICHE, dict(info, id=pid, city=city, phone=phone), "ready")
got = []
while (p := bot.take_next()) is not None:
    got.append(p["id"])
assert sorted(got) == ["p_gyu", "p_kaz"], got  # Ереван не выбран — его салон не показан
# вернём показанные в очередь для проверок ниже
for pid in got:
    db.set_status(pid, "ready")
print("OK   регионы: только выбранные города")

kaz = db.get_place("p_kaz")
assert bot.langs_for(kaz) == ["ru"] and bot.default_lang(kaz) == "ru"
assert bot.langs_for(db.get_place("p_gyu")) == ["hy", "ru"]
ru_text, _ = writer.generate(niche, kaz, "ru")
assert "Яндекс" in ru_text and "армянск" not in ru_text, "шаблон для России"
am_text, _ = writer.generate(niche, db.get_place("p_gyu"), "ru")
assert "в Гюмри" in am_text and "{" not in am_text, "шаблон для Армении с городом"
kb = bot.keyboard(dict(kaz, text_ru=ru_text), "ru")
assert not any(b.callback_data and b.callback_data.startswith("hy|") for row in kb.keyboard for b in row), \
    "для России не должно быть кнопки армянского текста"
print("OK   шаблоны и язык по стране")
print("\nВсё в порядке.")
