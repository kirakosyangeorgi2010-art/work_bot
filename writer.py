"""Генерация текста сообщения.
Сначала через Claude (программа claude на этом компьютере, по вашей подписке),
если не получилось — через бесплатный Gemini API.

Проверка без бота:  python writer.py
"""
import os
import random
import re
import shutil
import subprocess
import tempfile

import requests

import db  # noqa: F401  (настраивает кодировку консоли)

LANGUAGE_NAMES = {
    "hy": "армянский (пиши только армянскими буквами, никогда не латиницей и не транслитом)",
    "ru": "русский",
}


def get(p, key):
    """Значение из строки базы или словаря (в обоих случаях безопасно)."""
    return p[key] if key in p.keys() else None


def build_prompt(niche, place, lang):
    t = niche["text"]
    offer = niche["my_offer"]
    category = t.get("category_names", {}).get(get(place, "category") or "", "салон красоты")

    facts = [f"название: {place['name']}", f"тип: {category}",
             f"адрес: {(get(place, 'address') or '').strip() or 'Ереван'}"]
    social = get(place, "social_url")
    if social:
        net = "Instagram" if "instagram" in social else "Facebook" if "facebook" in social else "страница в соцсети"
        facts.append(f"у них пока нет сайта, есть только {net}")
    else:
        facts.append("своего сайта у них нет")

    avoid = ""
    prev = db.recent_texts(get(place, "niche") or "", lang) if db._conn else []
    if prev:
        avoid = ("\nПрошлые сообщения (не повторяй их начало, структуру и обороты):\n"
                 + "\n".join(f"- {x}" for x in prev) + "\n")

    return t["prompt"].format(
        facts="\n".join("- " + f for f in facts),
        includes=offer["includes"],
        example_url=offer["example_url"],
        language_name=LANGUAGE_NAMES.get(lang, lang),
        length=random.choice(t["lengths"]),
        style=random.choice(t["styles"]),
        ending=random.choice(t["endings"]),
        angle=random.choice(t["angles"]).format(category=category),
        avoid=avoid,
    )


def ask_claude(prompt):
    exe = shutil.which("claude")
    if not exe:
        raise RuntimeError("программа claude не найдена (см. README, шаг 4)")
    cmd = [exe, "-p"]
    model = os.getenv("CLAUDE_MODEL", "").strip()
    if model:
        cmd += ["--model", model]
    # Запускаем в пустой временной папке, чтобы Claude не видел ваши файлы
    with tempfile.TemporaryDirectory() as tmp:
        r = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                           encoding="utf-8", timeout=180, cwd=tmp)
    text = (r.stdout or "").strip()
    if r.returncode != 0 or not text:
        raise RuntimeError(f"claude: код {r.returncode}: {(r.stderr or r.stdout)[:300]}")
    return text


def ask_gemini(prompt):
    key = os.getenv("GEMINI_API_KEY", "").strip()
    if not key:
        raise RuntimeError("GEMINI_API_KEY не задан в .env")
    model = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite").strip()
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    r = requests.post(url, headers={"x-goog-api-key": key},
                      json={"contents": [{"parts": [{"text": prompt}]}]}, timeout=60)
    if r.status_code != 200:
        raise RuntimeError(f"Gemini ответил {r.status_code}: {r.text[:300]}")
    parts = r.json()["candidates"][0]["content"]["parts"]
    return "".join(p.get("text", "") for p in parts).strip()


def looks_ok(text, lang):
    """Армянский текст должен быть написан армянскими буквами, а не латиницей."""
    body = re.sub(r"https?://\S+", "", text)
    if lang == "hy":
        letters = [c for c in body if c.isalpha()]
        arm = [c for c in letters if "\u0531" <= c <= "\u058f"]
        return bool(letters) and len(arm) / len(letters) >= 0.6
    return len(text) > 40


def generate(niche, place, lang):
    """Возвращает (текст, кто написал). Если оба ИИ недоступны — выбрасывает ошибку."""
    t = niche["text"]
    if t.get("mode", "template") == "template":
        return t["templates"][lang].replace("{name}", place["name"]), "шаблон"
    prompt = build_prompt(niche, place, lang)
    errors = []
    order = ["claude", "gemini"] if os.getenv("AI_ORDER", "claude") == "claude" else ["gemini", "claude"]
    for name in order:
        for attempt in range(3):
            try:
                text = ask_claude(prompt) if name == "claude" else ask_gemini(prompt)
                text = text.strip().strip('"«»')
                if looks_ok(text, lang):
                    return text, name
                errors.append(f"{name}: текст не на нужном языке, пробую ещё раз")
            except Exception as e:
                errors.append(f"{name}: {e}")
                break
    raise RuntimeError("; ".join(errors))


if __name__ == "__main__":
    import json
    from dotenv import load_dotenv

    load_dotenv()
    niche = json.load(open("niches/beauty_salons.json", encoding="utf-8"))
    samples = [
        {"name": "Nare Beauty", "address": "Bashinjaghyan 185", "category": "beauty_salon",
         "social_url": "https://www.facebook.com/nare"},
        {"name": "ROF Barbershop", "address": "Margaryan St", "category": "barber", "social_url": ""},
        {"name": "Glamour Nail Bar", "address": "65/1 Halabyan St", "category": "nail_salon",
         "social_url": "https://www.instagram.com/glamour"},
    ]
    for lang in ("ru", "hy"):
        for s in samples:
            text, who = generate(niche, s, lang)
            print(f"--- {s['name']} / {lang} ({who}) ---\n{text}\n")
