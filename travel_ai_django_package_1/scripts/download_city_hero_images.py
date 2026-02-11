\
# -*- coding: utf-8 -*-
"""
Download fixed city hero images (landmark-focused) into static/img/city_hero

Why?
- Web preview hero images become stable (no random "bus" photo / no missing)
- PDF export uses these images as cover & day cards

Usage:
    python scripts/download_city_hero_images.py

Notes:
- Uses MediaWiki (Wikipedia) API to find a landmark page and fetch its lead image thumbnail.
- Stores attribution metadata to static/img/city_hero/credits.json
"""
import os
import json
import time
import hashlib
from urllib.parse import urlencode

import requests

USER_AGENT = "TravelAI/CityHeroDownloader (https://localhost; contact: local)"
SLEEP_SEC = 0.4
TIMEOUT = 20

# (country, city) -> landmark search query (English title/keywords for better hit rate)
CITY_LANDMARK_QUERY = {
    ("대한민국","서울"): "Gyeongbokgung Palace",
    ("대한민국","부산"): "Haeundae Beach",
    ("대한민국","제주"): "Seongsan Ilchulbong",

    ("일본","도쿄"): "Tokyo Tower",
    ("일본","오사카"): "Osaka Castle",

    ("태국","방콕"): "Wat Arun",

    ("베트남","호치민"): "Notre-Dame Cathedral Basilica of Saigon",
    ("베트남","다낭"): "Golden Bridge Da Nang",

    ("영국","런던"): "Tower Bridge",

    ("프랑스","파리"): "Eiffel Tower",

    ("네덜란드","암스테르담"): "Canals of Amsterdam",

    ("독일","베를린"): "Brandenburg Gate",
    ("독일","뮌헨"): "Marienplatz",

    ("오스트리아","빈"): "Schönbrunn Palace",

    ("스위스","인터라켄"): "Interlaken",

    ("이탈리아","베네치아"): "Grand Canal Venice",
    ("이탈리아","피렌체"): "Florence Cathedral",
    ("이탈리아","로마"): "Colosseum",

    ("스페인","마드리드"): "Plaza Mayor Madrid",
    ("스페인","바르셀로나"): "Sagrada Família",

    ("체코","프라하"): "Charles Bridge",

    ("호주","시드니"): "Sydney Opera House",
    ("호주","멜버른"): "Flinders Street railway station",
    ("호주","퍼스"): "Kings Park and Botanic Garden",
}

# Must match services/images.py CITY_HERO_SLUGS filenames
CITY_HERO_SLUGS = {
    ("대한민국","서울"): "kr_seoul.jpg",
    ("대한민국","부산"): "kr_busan.jpg",
    ("대한민국","제주"): "kr_jeju.jpg",

    ("일본","도쿄"): "jp_tokyo.jpg",
    ("일본","오사카"): "jp_osaka.jpg",

    ("태국","방콕"): "th_bangkok.jpg",

    ("베트남","호치민"): "vn_hcmc.jpg",
    ("베트남","다낭"): "vn_danang.jpg",

    ("영국","런던"): "uk_london.jpg",

    ("프랑스","파리"): "fr_paris.jpg",

    ("네덜란드","암스테르담"): "nl_amsterdam.jpg",

    ("독일","베를린"): "de_berlin.jpg",
    ("독일","뮌헨"): "de_munich.jpg",

    ("오스트리아","빈"): "at_vienna.jpg",

    ("스위스","인터라켄"): "ch_interlaken.jpg",

    ("이탈리아","베네치아"): "it_venice.jpg",
    ("이탈리아","피렌체"): "it_florence.jpg",
    ("이탈리아","로마"): "it_rome.jpg",

    ("스페인","마드리드"): "es_madrid.jpg",
    ("스페인","바르셀로나"): "es_barcelona.jpg",

    ("체코","프라하"): "cz_prague.jpg",

    ("호주","시드니"): "au_sydney.jpg",
    ("호주","멜버른"): "au_melbourne.jpg",
    ("호주","퍼스"): "au_perth.jpg",
}

WIKI_API = "https://en.wikipedia.org/w/api.php"

def _api_get(params):
    headers = {"User-Agent": USER_AGENT}
    r = requests.get(WIKI_API, params=params, timeout=TIMEOUT, headers=headers)
    r.raise_for_status()
    return r.json()

def _search_best_title(query: str) -> str:
    data = _api_get({
        "action": "query",
        "list": "search",
        "srsearch": query,
        "srlimit": 1,
        "format": "json",
    })
    hits = (data.get("query") or {}).get("search") or []
    return hits[0]["title"] if hits else ""

def _get_page_image(title: str, thumb_size: int = 2400) -> dict:
    data = _api_get({
        "action": "query",
        "prop": "pageimages|info",
        "titles": title,
        "pithumbsize": thumb_size,
        "inprop": "url",
        "format": "json",
    })
    pages = (data.get("query") or {}).get("pages") or {}
    # pages is a dict keyed by pageid
    page = next(iter(pages.values()), {})
    return page

def _download(url: str, out_path: str):
    headers = {"User-Agent": USER_AGENT}
    r = requests.get(url, timeout=TIMEOUT, headers=headers)
    r.raise_for_status()
    with open(out_path, "wb") as f:
        f.write(r.content)

def main():
    here = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.abspath(os.path.join(here, ".."))
    out_dir = os.path.join(project_root, "static", "img", "city_hero")
    os.makedirs(out_dir, exist_ok=True)

    credits = {}
    ok = 0
    fail = 0

    for (country, city), slug in CITY_HERO_SLUGS.items():
        query = CITY_LANDMARK_QUERY.get((country, city), f"{city} {country} landmark")
        out_path = os.path.join(out_dir, slug)

        # skip if already present and non-trivial
        if os.path.exists(out_path) and os.path.getsize(out_path) > 50_000:
            credits[f"{country}|{city}"] = {"status": "exists", "file": slug}
            ok += 1
            continue

        try:
            title = _search_best_title(query) or query
            page = _get_page_image(title)
            thumb = (page.get("thumbnail") or {}).get("source") or ""
            page_url = page.get("fullurl") or f"https://en.wikipedia.org/wiki/{title.replace(' ', '_')}"
            if not thumb:
                raise RuntimeError("no thumbnail found")

            _download(thumb, out_path)
            # tiny check
            if os.path.getsize(out_path) < 10_000:
                raise RuntimeError("download too small")

            credits[f"{country}|{city}"] = {
                "status": "downloaded",
                "file": slug,
                "query": query,
                "page_title": title,
                "page_url": page_url,
                "image_url": thumb,
            }
            ok += 1
            print(f"✅ {country} {city}: {slug}")
        except Exception as e:
            fail += 1
            credits[f"{country}|{city}"] = {"status": "failed", "file": slug, "query": query, "error": str(e)}
            print(f"❌ {country} {city}: {e}")

        time.sleep(SLEEP_SEC)

    credits_path = os.path.join(out_dir, "credits.json")
    with open(credits_path, "w", encoding="utf-8") as f:
        json.dump(credits, f, ensure_ascii=False, indent=2)

    print("\nDone.")
    print("OK:", ok, "FAIL:", fail)
    print("Credits:", credits_path)

if __name__ == "__main__":
    main()
