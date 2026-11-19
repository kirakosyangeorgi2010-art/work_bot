"""Поиск по открытым картам Overture Maps (бесплатно, без ключа и без карты).

Overture каждый месяц выпускает свежую базу мест (данные Meta/Facebook, Microsoft и др.).
Мы скачиваем только нужный город и только нужные категории — это ~10–30 секунд.

Проверка без бота:  python finder.py
(скачает Ереван и напечатает первые 15 подходящих салонов, в базу ничего не пишет)
"""
from urllib.parse import quote, urlparse

import duckdb
import requests

import db

CATALOG_URL = "https://stac.overturemaps.org/catalog.json"
PLACES_PATH = "s3://overturemaps-us-west-2/release/{release}/theme=places/type=place/*"

EXHAUSTED = "exhausted"   # все города из файла ниши уже загружены из последнего выпуска


def latest_release():
    return requests.get(CATALOG_URL, timeout=30).json()["latest"]


def download_city(release, bbox, categories):
    """Скачивает места в прямоугольнике города. bbox = [запад, юг, восток, север]."""
    west, south, east, north = bbox
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs; SET s3_region='us-west-2';")
    marks = ",".join("?" * len(categories))
    sql = f"""
        SELECT id, names.primary AS name, addresses[1].freeform AS address,
               phones, websites, socials, confidence, operating_status,
               taxonomy.primary AS category, bbox.xmin AS lon, bbox.ymin AS lat
        FROM read_parquet('{PLACES_PATH.format(release=release)}', hive_partitioning=1)
        WHERE bbox.xmin BETWEEN ? AND ? AND bbox.ymin BETWEEN ? AND ?
          AND taxonomy.primary IN ({marks})
        ORDER BY confidence DESC
    """
    cur = con.execute(sql, [west, east, south, north, *categories])
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def is_social(url, social_domains):
    host = urlparse(url).netloc.lower()
    return any(host == d or host.endswith("." + d) for d in social_domains)


def check_place(p, flt):
    """Проверяет место по фильтру ниши. Возвращает (подходит?, данные о месте)."""
    name = p.get("name") or "?"
    phones = p.get("phones") or []
    socials = list(p.get("socials") or [])
    websites = p.get("websites") or []
    conf = p.get("confidence") or 0

    info = {
        "id": p["id"],
        "name": name,
        "address": p.get("address") or "",
        "phone": phones[0] if phones else "",
        "maps_url": "",
        "social_url": "",
        "rating": None,
        "reviews_count": None,
        "last_review": "",
        "reason": "",
        "category": p.get("category") or "",
    }
    if p.get("lat") is not None:
        info["maps_url"] = ("https://www.google.com/maps/search/?api=1&query="
                            + quote(f"{name} {p['lat']:.6f},{p['lon']:.6f}"))

    if p.get("operating_status") not in (None, "open"):
        info["reason"] = "закрыт"
        return False, info

    if conf < flt["min_confidence"]:
        info["reason"] = f"мало уверенности, что существует ({conf:.2f})"
        return False, info

    # Сайт: если хоть один настоящий сайт — не подходит. Ссылки на соцсети считаем контактами.
    for w in websites:
        if is_social(w, flt["social_domains"]):
            socials.insert(0, w)
        else:
            info["reason"] = f"есть сайт: {w}"
            return False, info

    if socials:
        # Instagram показываем первым, если он есть
        socials.sort(key=lambda u: "instagram" not in u)
        info["social_url"] = socials[0]

    if not info["phone"] and not info["social_url"]:
        info["reason"] = "нет контакта"
        return False, info

    parts = ["нет своего сайта"]
    if info["social_url"]:
        parts.append("есть только страница в соцсети")
    parts.append(f"уверенность, что работает: {conf:.2f}")
    info["reason"] = ", ".join(parts)
    return True, info


def find_more(niche_id, niche):
    """Загружает следующий город из файла ниши (или обновляет, если вышел новый выпуск Overture).
    Возвращает число новых подходящих салонов или EXHAUSTED."""
    release = latest_release()
    s = niche["search"]
    for city in s["cities"]:
        job = db.one("SELECT page_token FROM search_jobs WHERE niche = ? AND query = ?",
                     (niche_id, city["name"]))
        if job is not None and job["page_token"] == release:
            continue  # этот город уже загружен из последнего выпуска
        added = 0
        for p in download_city(release, city["bbox"], s["categories"]):
            if db.place_exists(p["id"]):
                continue
            ok, info = check_place(p, niche["filter"])
            if ok and db.phone_taken(niche_id, info["phone"]):
                ok, info["reason"] = False, "дубль: этот телефон уже есть"
            db.add_place(niche_id, info, "new" if ok else "filtered_out")
            added += ok
        db.add_job(niche_id, city["name"])
        db.update_job(niche_id, city["name"], release)
        db.run("UPDATE search_jobs SET done = 1 WHERE niche = ? AND query = ?", (niche_id, city["name"]))
        return added
    return EXHAUSTED


if __name__ == "__main__":
    import json

    niche = json.load(open("niches/beauty_salons.json", encoding="utf-8"))
    release = latest_release()
    city = niche["search"]["cities"][0]
    print(f"Выпуск Overture: {release}. Скачиваю {city['name']}…")
    places = download_city(release, city["bbox"], niche["search"]["categories"])
    good = [info for ok, info in (check_place(p, niche["filter"]) for p in places) if ok]
    print(f"Всего мест: {len(places)}, подходят: {len(good)}\n")
    for info in good[:15]:
        print(f"✅ {info['name']} | {info['phone'] or '-'} | {info['social_url'] or '-'}\n   {info['reason']}")
