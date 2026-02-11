# -*- coding: utf-8 -*-
"""Template-based PPT export (Design A).

Why this file exists
- Building slides from scratch is fragile (fonts/metrics differ by OS).
- The user provided an editable reference PPTX template. We clone its vector
  layout (shapes/text) and ONLY replace placeholders + images.

Output
- Cover
- Day detail slides for ALL days
- (Optional) Full route overview slide
- Summary slides (4 days per slide)

Note
- We add picture shapes then adjust Z-order so they sit behind overlay text.
- For rounded corners, we pre-render PNG with alpha using PIL.

"""

from __future__ import annotations

import io
import math
import os
import re
from copy import deepcopy
from dataclasses import dataclass
from datetime import date
from typing import Iterable, List, Optional, Sequence, Tuple

import requests
from PIL import Image, ImageDraw
from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE
from pptx.util import Emu

from services.maps import build_route_static_map_url

# -----------------------------
# Helpers
# -----------------------------

def _iter_shapes(container):
    """Yield shapes recursively (slide.shapes or group.shapes)."""
    for sh in container.shapes:
        yield sh
        if sh.shape_type == MSO_SHAPE_TYPE.GROUP:
            yield from _iter_shapes(sh)


def _shape_text(sh) -> str:
    if hasattr(sh, "has_text_frame") and sh.has_text_frame:
        return sh.text_frame.text or ""
    return ""


def _set_text(sh, text: str):
    if hasattr(sh, "has_text_frame") and sh.has_text_frame:
        sh.text_frame.clear()
        p = sh.text_frame.paragraphs[0]
        run = p.add_run()
        run.text = text


def _emu_to_int(v: Emu) -> int:
    return int(v)


def _download_image(url: str, timeout: float = 8.0) -> Optional[Image.Image]:
    if not url:
        return None
    try:
        r = requests.get(url, timeout=timeout, headers={"User-Agent": "TravelAI/1.0"})
        r.raise_for_status()
        img = Image.open(io.BytesIO(r.content))
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGBA")
        return img
    except Exception:
        return None


def _fit_cover(img: Image.Image, target_w: int, target_h: int) -> Image.Image:
    """Center-crop to fill target."""
    if img.mode != "RGBA":
        img = img.convert("RGBA")
    w, h = img.size
    if w == 0 or h == 0:
        return Image.new("RGBA", (target_w, target_h), (230, 236, 245, 255))
    scale = max(target_w / w, target_h / h)
    nw, nh = int(w * scale), int(h * scale)
    img2 = img.resize((nw, nh), Image.LANCZOS)
    left = (nw - target_w) // 2
    top = (nh - target_h) // 2
    return img2.crop((left, top, left + target_w, top + target_h))


def _rounded_png(img: Image.Image, radius: int = 28) -> bytes:
    """Return PNG bytes with rounded corners (alpha)."""
    img = img.convert("RGBA")
    w, h = img.size
    mask = Image.new("L", (w, h), 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle((0, 0, w, h), radius=radius, fill=255)
    out = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    buf = io.BytesIO()
    out.save(buf, format="PNG")
    return buf.getvalue()


def _ppt_add_picture_behind(slide, png_bytes: bytes, left: int, top: int, width: int, height: int, before_shape=None):
    """Add picture and optionally move it behind `before_shape`."""
    pic = slide.shapes.add_picture(io.BytesIO(png_bytes), Emu(left), Emu(top), width=Emu(width), height=Emu(height))

    # Z-order: earlier = behind, later = in front.
    if before_shape is not None:
        spTree = slide.shapes._spTree
        pic_el = pic._element
        try:
            spTree.remove(pic_el)
        except Exception:
            return pic
        # insert pic before the target element to place behind it
        target_el = before_shape._element
        idx = list(spTree).index(target_el)
        spTree.insert(idx, pic_el)
    return pic


def _format_date_kor(d: date) -> str:
    # Fallback formatting if locale is not installed
    weekday = ["월", "화", "수", "목", "금", "토", "일"][d.weekday()]
    return f"{d.year}.{d.month:02d}.{d.day:02d} | {weekday}"


def _chunk(seq, n: int):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


@dataclass
class DayData:
    day_index: int
    city: str
    country: str
    date: Optional[date]
    pois: List[Tuple[str, str]]  # (name, subtitle)
    hotel_name: str
    hotel_subtitle: str
    hero_url: str
    hotel_img_url: str
    map_url: str
    bottom_img_url: str


def _extract_day_data(day_plans) -> List[DayData]:
    out: List[DayData] = []
    for i, dp in enumerate(day_plans, start=1):
        # City/Country
        city = (getattr(dp, "to_city", "") or getattr(dp, "city_name", "") or "").strip()
        country = (getattr(dp, "to_country", "") or getattr(dp, "country_name", "") or "").strip()

        # Date
        d = getattr(dp, "day_date", None)

        # POIs (top 3)
        pois = []
        qs = getattr(dp, "pois", None)
        if qs is not None:
            try:
                items = list(qs.all())
            except Exception:
                items = []
            for p in items[:3]:
                name = (getattr(p, "name", "") or "").strip()[:40]
                feature = (getattr(p, "feature", "") or "").strip()
                subtitle = f"{feature} · {city}" if feature else city
                pois.append((name, subtitle))
        while len(pois) < 3:
            pois.append(("", ""))

        # Hero image
        hero = (getattr(dp, "hero_image_url", "") or "").strip()

        # Hotel
        h = getattr(dp, "hotel", None) or {}
        hotel_name = (h.get("name") or h.get("hotel_name") or h.get("title") or "").strip()
        rating = h.get("rating") or h.get("google_rating")
        reviews = h.get("review_count") or h.get("google_review_count")
        hotel_subtitle = ""
        if rating is not None and reviews is not None:
            try:
                hotel_subtitle = f"평점 {float(rating):.1f} · 리뷰 {int(reviews):,}"
            except Exception:
                hotel_subtitle = ""

        hotel_img_url = (h.get("image_url") or h.get("image") or h.get("photo") or "").strip()

        # Map URL
        points = []
        # hotel lat/lon
        try:
            lat = h.get("lat") or h.get("latitude")
            lon = h.get("lon") or h.get("lng") or h.get("longitude")
            if lat is not None and lon is not None:
                points.append(("Hotel", float(lat), float(lon)))
        except Exception:
            pass
        # poi lat/lon
        if qs is not None:
            try:
                for p in list(qs.all()):
                    lat = getattr(p, "latitude", None)
                    lon = getattr(p, "longitude", None)
                    if lat is None or lon is None:
                        continue
                    points.append((getattr(p, "name", "") or "", float(lat), float(lon)))
            except Exception:
                pass
        map_url = ""
        if len(points) >= 2:
            try:
                map_url = build_route_static_map_url(
                    points,
                    width=900,
                    height=600,
                    zoom=12,
                    marker_style="blue",
                )
            except Exception:
                map_url = ""

        # Bottom image (first POI if possible)
        bottom = ""
        if qs is not None:
            try:
                items = list(qs.all())
                if items:
                    bottom = (getattr(items[0], "image_hd_url", "") or getattr(items[0], "image_url", "") or "").strip()
            except Exception:
                bottom = ""

        out.append(DayData(
            day_index=i,
            city=city,
            country=country,
            date=d,
            pois=pois,
            hotel_name=hotel_name,
            hotel_subtitle=hotel_subtitle,
            hero_url=hero,
            hotel_img_url=hotel_img_url,
            map_url=map_url,
            bottom_img_url=bottom,
        ))
    return out


def build_ppt_a(travel_plan, day_plans, template_path: str, include_full_route: bool = True) -> bytes:
    """Build PPTX bytes using the reference template."""

    tpl = Presentation(template_path)
    if len(tpl.slides) < 3:
        raise ValueError("Template must have at least 3 slides (cover/day/summary).")

    prs = Presentation()
    prs.slide_width = tpl.slide_width
    prs.slide_height = tpl.slide_height
    blank = prs.slide_layouts[6]

    def clone_slide(src_slide, skip_pictures: bool = False):
        dst = prs.slides.add_slide(blank)
        spTree = dst.shapes._spTree
        for sh in list(src_slide.shapes):
            if skip_pictures and sh.shape_type == MSO_SHAPE_TYPE.PICTURE:
                continue
            el = deepcopy(sh._element)
            # insert before extLst to keep correct ordering
            spTree.insert(len(spTree) - 1, el)
        return dst

    # -----------------------------
    # Data
    # -----------------------------
    days = _extract_day_data(day_plans)

    # Date range (cover)
    start_d = None
    end_d = None
    for d in days:
        if d.date:
            start_d = start_d or d.date
            end_d = d.date
    if start_d and end_d:
        date_range = f"{start_d.year}-{start_d.month:02d}-{start_d.day:02d}  ~  {end_d.year}-{end_d.month:02d}-{end_d.day:02d}"
    else:
        date_range = ""

    cities_line = " · ".join([d.city for d in days if d.city])

    # -----------------------------
    # 1) Cover
    # -----------------------------
    src_cover = tpl.slides[0]

    # Determine cover hero bounds from the largest picture in template
    pic_shapes = [sh for sh in src_cover.shapes if sh.shape_type == MSO_SHAPE_TYPE.PICTURE]
    hero_bounds = None
    if pic_shapes:
        largest = max(pic_shapes, key=lambda s: int(s.width) * int(s.height))
        hero_bounds = (int(largest.left), int(largest.top), int(largest.width), int(largest.height))

    cover = clone_slide(src_cover, skip_pictures=True)

    # Replace cover texts
    for sh in _iter_shapes(cover):
        t = _shape_text(sh)
        if not t:
            continue
        if "TRAVEL ITINERARY" in t:
            # keep
            continue
        if re.search(r"Jul\s+\d+\s*-\s*Jul\s+\d+", t) or re.search(r"\d{4}", t):
            if date_range:
                _set_text(sh, date_range)
        if "Seoul" in t and "Tokyo" in t:
            if cities_line:
                _set_text(sh, cities_line)

    # Add cover hero image (use first day's hero)
    cover_hero_url = days[0].hero_url if days else ""
    if hero_bounds and cover_hero_url:
        img = _download_image(cover_hero_url)
        if img is None:
            img = Image.new("RGBA", (1600, 900), (210, 220, 235, 255))
        target_w, target_h = hero_bounds[2] // 9525, hero_bounds[3] // 9525  # rough? not used
        # We add picture at exact emu size; PIL uses pixels. We'll render at 2x scale for quality.
        w_px = max(800, int(hero_bounds[2] / 9525) * 2)
        h_px = max(450, int(hero_bounds[3] / 9525) * 2)
        hero_img = _fit_cover(img, w_px, h_px)
        hero_png = _rounded_png(hero_img, radius=42)

        # Put hero behind all text: insert right after the first shape element
        first_text = None
        for sh in cover.shapes:
            if getattr(sh, "has_text_frame", False) and sh.has_text_frame and sh.text_frame.text.strip():
                first_text = sh
                break
        ref = first_text
        _ppt_add_picture_behind(
            cover,
            hero_png,
            hero_bounds[0], hero_bounds[1], hero_bounds[2], hero_bounds[3],
            before_shape=ref,
        )

    # -----------------------------
    # 2) Day detail slides (ALL days)
    # -----------------------------
    src_day = tpl.slides[1]

    # Identify day-map picture bounds from template (largest picture)
    day_pic_shapes = [sh for sh in src_day.shapes if sh.shape_type == MSO_SHAPE_TYPE.PICTURE]
    map_bounds = None
    if day_pic_shapes:
        largest = max(day_pic_shapes, key=lambda s: int(s.width) * int(s.height))
        map_bounds = (int(largest.left), int(largest.top), int(largest.width), int(largest.height))

    # Hotel photo placeholder bounds: inside the group with text "호텔사진"
    def _find_hotel_photo_bounds(slide):
        for sh in _iter_shapes(slide):
            if sh.shape_type == MSO_SHAPE_TYPE.AUTO_SHAPE:
                if "호텔사진" in _shape_text(sh):
                    return (int(sh.left), int(sh.top), int(sh.width), int(sh.height))
        return None

    hotel_bounds_tpl = _find_hotel_photo_bounds(src_day)

    for d in days:
        s = clone_slide(src_day, skip_pictures=True)

        # Update day/city/date & POI texts
        for sh in _iter_shapes(s):
            t = _shape_text(sh)
            if not t:
                continue
            if t.strip().startswith("Day") and len(t.strip()) <= 6:
                _set_text(sh, f"Day {d.day_index}")
            elif t.strip() == "Seoul":
                if d.city:
                    _set_text(sh, d.city)
            elif "Wednesday" in t or "|" in t:
                if d.date:
                    _set_text(sh, _format_date_kor(d.date))

        # POI titles/subtitles: detect the 3 title lines by looking for the sample names
        poi_title_candidates = []
        poi_sub_candidates = []
        for sh in _iter_shapes(s):
            t = _shape_text(sh).strip()
            if not t:
                continue
            if t in ("Gyeongbokgung Palace", "Bukchon Hanok Village", "Gyeongnidan-gil"):
                poi_title_candidates.append(sh)
            if t.startswith("Cute"):
                poi_sub_candidates.append(sh)
        poi_title_candidates = sorted(poi_title_candidates, key=lambda x: int(x.top))
        poi_sub_candidates = sorted(poi_sub_candidates, key=lambda x: int(x.top))
        for idx in range(min(3, len(poi_title_candidates))):
            _set_text(poi_title_candidates[idx], d.pois[idx][0])
        for idx in range(min(3, len(poi_sub_candidates))):
            _set_text(poi_sub_candidates[idx], d.pois[idx][1])

        # Hotel info text in group
        for sh in _iter_shapes(s):
            if "호텔이름" in _shape_text(sh):
                line = f"• {d.hotel_name}" if d.hotel_name else "• Hotel"
                if d.hotel_subtitle:
                    line += f" | {d.hotel_subtitle}"
                _set_text(sh, line)

        # Add hotel photo (rounded)
        if hotel_bounds_tpl and d.hotel_img_url:
            img = _download_image(d.hotel_img_url)
            if img is None:
                img = Image.new("RGBA", (800, 600), (220, 230, 245, 255))
            w_px = max(400, int(hotel_bounds_tpl[2] / 9525) * 2)
            h_px = max(300, int(hotel_bounds_tpl[3] / 9525) * 2)
            img = _fit_cover(img, w_px, h_px)
            png = _rounded_png(img, radius=24)
            _ppt_add_picture_behind(s, png, *hotel_bounds_tpl, before_shape=None)
            # Clear the placeholder text
            for sh in _iter_shapes(s):
                if "호텔사진" in _shape_text(sh):
                    _set_text(sh, "")

        # Add map image (rounded) behind nothing (it sits inside a card area)
        if map_bounds and d.map_url:
            img = _download_image(d.map_url, timeout=10.0)
            if img is None:
                img = Image.new("RGBA", (1200, 800), (235, 240, 248, 255))
            w_px = max(800, int(map_bounds[2] / 9525) * 2)
            h_px = max(500, int(map_bounds[3] / 9525) * 2)
            img = _fit_cover(img, w_px, h_px)
            png = _rounded_png(img, radius=36)
            _ppt_add_picture_behind(s, png, *map_bounds, before_shape=None)

    # -----------------------------
    # 3) Optional full route overview slide
    # -----------------------------
    if include_full_route and days:
        # Simple slide: reuse day slide background (without pictures) and drop a big map
        s = clone_slide(src_day, skip_pictures=True)
        # replace title to Korean label
        for sh in _iter_shapes(s):
            if _shape_text(sh).strip() == "Trip Overview":
                _set_text(sh, "전체 루트 지도 요약")
                break

        # Build full map points from all days
        points = []
        for d in days:
            # we don't store all points; use the per-day map_url if exists; fall back to city hero only
            pass
        # Use the first day map bounds and generate a static map from all POIs if possible
        # For simplicity: we leave the day slide's map area and put a neutral map there.
        if map_bounds:
            # fallback to OpenStreetMap-like neutral pattern by reusing first day's map or blank
            img = None
            for d in days:
                if d.map_url:
                    img = _download_image(d.map_url, timeout=10.0)
                    if img is not None:
                        break
            if img is None:
                img = Image.new("RGBA", (1200, 800), (235, 240, 248, 255))
            w_px = max(800, int(map_bounds[2] / 9525) * 2)
            h_px = max(500, int(map_bounds[3] / 9525) * 2)
            img = _fit_cover(img, w_px, h_px)
            png = _rounded_png(img, radius=36)
            _ppt_add_picture_behind(s, png, *map_bounds, before_shape=None)

        # Hide POI list texts in left panel to keep it clean
        for sh in _iter_shapes(s):
            t = _shape_text(sh)
            if t in ("Gyeongbokgung Palace", "Bukchon Hanok Village", "Gyeongnidan-gil") or t.startswith("Cute"):
                _set_text(sh, "")
            if "호텔이름" in t or "Hotel" in t and "•" in t:
                _set_text(sh, "")

    # -----------------------------
    # 4) Summary (4 days per slide)
    # -----------------------------
    src_sum = tpl.slides[2]

    # Find card background shapes by their width/height and sort by left
    def _find_card_backgrounds(slide):
        cards = []
        for sh in slide.shapes:
            if sh.shape_type == MSO_SHAPE_TYPE.AUTO_SHAPE:
                # background cards are tall rounded rects
                if int(sh.height) > 4_500_000 and int(sh.width) > 2_300_000:
                    cards.append(sh)
        # Use the four left positions from template
        cards = sorted(cards, key=lambda s: int(s.left))
        return cards[:4]

    card_bg_tpl = _find_card_backgrounds(src_sum)
    if len(card_bg_tpl) < 4:
        # fallback positions (approx)
        card_bg_tpl = []

    # Hero and bottom placeholders inside each card are rectangles with specific texts
    def _find_placeholders_for_card(slide, card_left: int, card_right: int):
        hero_title = None
        hero_date = None
        hero_box = None
        bottom_box = None
        # identify via text + bounding
        for sh in slide.shapes:
            l = int(sh.left)
            if not (card_left - 50_000 <= l <= card_right + 50_000):
                continue
            t = _shape_text(sh)
            if "도시 사진" in t:
                hero_title = sh
            if "2024" in t and "Jul" in t:
                hero_date = sh
            if sh.shape_type == MSO_SHAPE_TYPE.AUTO_SHAPE and "" in t:
                pass
            if sh.shape_type == MSO_SHAPE_TYPE.AUTO_SHAPE and "관광지" in t:
                bottom_box = sh
            if sh.shape_type == MSO_SHAPE_TYPE.AUTO_SHAPE and t.strip() == "":
                # candidate hero box: around top area inside card
                if int(sh.height) >= 1_400_000 and int(sh.height) <= 1_600_000 and int(sh.top) < 2_000_000:
                    hero_box = sh
        return hero_box, hero_title, hero_date, bottom_box

    # Precompute template's per-card placeholder locations from src_sum
    card_placeholders = []
    for bg in card_bg_tpl:
        card_left = int(bg.left)
        card_right = int(bg.left + bg.width)
        hero_box, hero_title, hero_date, bottom_box = _find_placeholders_for_card(src_sum, card_left, card_right)
        card_placeholders.append((bg, hero_box, hero_title, hero_date, bottom_box))

    for chunk_days in _chunk(days, 4):
        s = clone_slide(src_sum, skip_pictures=False)

        # On destination slide, refind card backgrounds by left positions
        dst_cards = _find_card_backgrounds(s)

        # For each card slot
        for idx in range(4):
            if idx >= len(dst_cards):
                break
            bg = dst_cards[idx]
            card_left = int(bg.left)
            card_right = int(bg.left + bg.width)

            # Identify card-related text shapes (day label, city, poi lines) within x-range
            card_shapes = [sh for sh in _iter_shapes(s) if card_left - 50_000 <= int(sh.left) <= card_right + 50_000]

            day_data = chunk_days[idx] if idx < len(chunk_days) else None

            # Find specific text placeholders
            # Day label big
            day_label = None
            city_label = None
            hero_title = None
            hero_date = None
            poi_titles = []
            poi_subs = []
            bottom_placeholder = None
            hero_box = None

            for sh in card_shapes:
                t = _shape_text(sh).strip()
                if not t and sh.shape_type == MSO_SHAPE_TYPE.AUTO_SHAPE:
                    if int(sh.height) >= 1_400_000 and int(sh.height) <= 1_600_000 and int(sh.top) < 2_000_000:
                        hero_box = sh
                if "도시 사진" in t:
                    hero_title = sh
                if "Jul" in t and "2024" in t:
                    hero_date = sh
                if t in ("Day 1", "Day 2", "Day 3", "Day 4"):
                    day_label = sh
                if t in ("Seoul", "Tokyo", "Kyoto"):
                    city_label = sh
                if t in ("Gyeongbokgung", "Deckgung", "Me? Strane", "Fasker..."):
                    poi_titles.append(sh)
                if t.startswith("Cult"):
                    poi_subs.append(sh)
                if "관광지" in t:
                    bottom_placeholder = sh

            poi_titles = sorted(poi_titles, key=lambda x: int(x.top))
            poi_subs = sorted(poi_subs, key=lambda x: int(x.top))

            if day_data is None:
                # blank out
                if hero_title:
                    _set_text(hero_title, "")
                if hero_date:
                    _set_text(hero_date, "")
                if day_label:
                    _set_text(day_label, "")
                if city_label:
                    _set_text(city_label, "")
                for sh in poi_titles + poi_subs:
                    _set_text(sh, "")
                if bottom_placeholder:
                    _set_text(bottom_placeholder, "")
                continue

            # Set texts
            if hero_title:
                _set_text(hero_title, f"Day {day_data.day_index}")
            if hero_date and day_data.date:
                _set_text(hero_date, _format_date_kor(day_data.date))
            if day_label:
                _set_text(day_label, f"Day {day_data.day_index}")
            if city_label:
                _set_text(city_label, day_data.city)

            for j in range(min(3, len(poi_titles))):
                _set_text(poi_titles[j], day_data.pois[j][0])
            for j in range(min(3, len(poi_subs))):
                _set_text(poi_subs[j], day_data.pois[j][1])

            # Hero image: add behind hero_title (overlay)
            if hero_box is not None and day_data.hero_url:
                img = _download_image(day_data.hero_url)
                if img is None:
                    img = Image.new("RGBA", (1200, 800), (215, 225, 240, 255))
                w_px = max(500, int(hero_box.width / 9525) * 2)
                h_px = max(320, int(hero_box.height / 9525) * 2)
                img = _fit_cover(img, w_px, h_px)
                png = _rounded_png(img, radius=28)
                _ppt_add_picture_behind(s, png, int(hero_box.left), int(hero_box.top), int(hero_box.width), int(hero_box.height), before_shape=hero_title)

            # Bottom POI image: cover the placeholder shape
            if bottom_placeholder is not None and day_data.bottom_img_url:
                img = _download_image(day_data.bottom_img_url)
                if img is None:
                    img = Image.new("RGBA", (800, 600), (235, 240, 248, 255))
                w_px = max(400, int(bottom_placeholder.width / 9525) * 2)
                h_px = max(250, int(bottom_placeholder.height / 9525) * 2)
                img = _fit_cover(img, w_px, h_px)
                png = _rounded_png(img, radius=18)
                _ppt_add_picture_behind(s, png, int(bottom_placeholder.left), int(bottom_placeholder.top), int(bottom_placeholder.width), int(bottom_placeholder.height), before_shape=None)
                _set_text(bottom_placeholder, "")

    # Save to bytes
    bio = io.BytesIO()
    prs.save(bio)
    return bio.getvalue()
