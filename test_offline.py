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
print("OK   ссылки WhatsApp")
print("\nВсё в порядке.")
