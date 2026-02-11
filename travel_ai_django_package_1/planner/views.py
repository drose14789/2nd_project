"""
여행 플래너 뷰
"""
import json
import os
import math
import logging
import re
import unicodedata
from datetime import datetime, timedelta
from types import SimpleNamespace

from django.shortcuts import render, redirect, get_object_or_404
from django.http import JsonResponse, HttpResponse
from django.views.decorators.http import require_POST, require_GET
from django.views.decorators.csrf import ensure_csrf_cookie
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.conf import settings
from django.db import transaction

from .models import TravelPlan, DayPlan, POI, UserPOIInteraction, RecommendationFeedback
from accounts.models import TravelPreference
from accounts.models import VisitedPlace

# 서비스 모듈 import
from services.local_poi import get_local_index, get_michelin_index, is_landmark, calculate_landmark_score
from services.images import get_poi_images, get_city_hero_image, get_city_image, get_image_candidates, google_places_resolve, CITY_HERO_SLUGS, download_city_hero_pack
from services.tips import generate_tips
from services.geoapify import autocomplete_place, hotels_near
from services.ai_explain import build_day_context, generate_day_explanation
from services.ai_tips import generate_day_tips as generate_ai_day_tips

logger = logging.getLogger(__name__)


# ------------------------------------------------------------
# LLM+RAG: Day explanation cache (file-based, no DB migration)
# ------------------------------------------------------------
def _ai_explain_dir() -> str:
    base_dir = getattr(settings, "BASE_DIR", os.getcwd())
    d = os.path.join(base_dir, "storage", "ai_explain")
    os.makedirs(d, exist_ok=True)
    return d

def _ai_explain_path(plan_id: int, day_number: int) -> str:
    return os.path.join(_ai_explain_dir(), f"plan_{plan_id}_day_{day_number}.json")

def _ai_explain_signature(day_plan: DayPlan, pois_qs) -> str:
    """
    Signature to invalidate cached explanation when itinerary changes.
    """
    try:
        poi_ids = [int(p.id) for p in pois_qs]
    except Exception:
        poi_ids = []
    hotel = day_plan.hotel or {}
    hs = (hotel.get("selected") or {})
    key = {
        "to_city": day_plan.to_city,
        "hotel_place_id": hs.get("place_id") or "",
        "hotel_name": hs.get("name") or "",
        "poi_ids": poi_ids,
    }
    raw = json.dumps(key, ensure_ascii=False, sort_keys=True)
    import hashlib
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

def _load_ai_explain(plan_id: int, day_number: int):
    p = _ai_explain_path(plan_id, day_number)
    if not os.path.exists(p):
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None

def _save_ai_explain(plan_id: int, day_number: int, payload: dict) -> None:
    p = _ai_explain_path(plan_id, day_number)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)


# ------------------------------------------------------------
# LLM+RAG: Day tips cache (file-based, no DB migration)
# ------------------------------------------------------------
def _ai_tips_dir() -> str:
    base_dir = getattr(settings, "BASE_DIR", os.getcwd())
    d = os.path.join(base_dir, "storage", "ai_tips")
    os.makedirs(d, exist_ok=True)
    return d


def _ai_tips_path(plan_id: int, day_number: int) -> str:
    return os.path.join(_ai_tips_dir(), f"plan_{plan_id}_day_{day_number}.json")


def _load_ai_tips(plan_id: int, day_number: int):
    p = _ai_tips_path(plan_id, day_number)
    if not os.path.exists(p):
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _save_ai_tips(plan_id: int, day_number: int, payload: dict) -> None:
    p = _ai_tips_path(plan_id, day_number)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)


def _haversine_km(lat1, lon1, lat2, lon2):
    """Return great-circle distance in km."""
    try:
        R = 6371.0
        phi1 = math.radians(float(lat1))
        phi2 = math.radians(float(lat2))
        dphi = math.radians(float(lat2) - float(lat1))
        dl = math.radians(float(lon2) - float(lon1))
        a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dl / 2) ** 2
        return 2 * R * math.asin(min(1.0, math.sqrt(a)))
    except Exception:
        return 0.0





_NORM_SPACE_RE = re.compile(r"\s+")
_NORM_PUNCT_RE = re.compile(r"[^0-9A-Za-z가-힣]+")

def _norm_key(s: str) -> str:
    """Normalize a name/key for loose dedupe."""
    try:
        t = unicodedata.normalize('NFKC', (s or '')).strip().lower()
        t = _NORM_SPACE_RE.sub(' ', t)
        t = t.replace(' ', '')
        t = _NORM_PUNCT_RE.sub('', t)
        return t
    except Exception:
        return (s or '').strip().lower()


def _format_duration_hours(hours: float) -> str:
    try:
        h = float(hours or 0.0)
    except Exception:
        h = 0.0
    if h <= 0:
        return ''
    mins = int(round(h * 60))
    hh = mins // 60
    mm = mins % 60
    if hh <= 0:
        return f"약 {mm}분"
    if mm == 0:
        return f"약 {hh}시간"
    return f"약 {hh}시간 {mm}분"


def _enrich_hotels_with_google(cands, city: str, country: str, radius_m: int = 12000, limit: int = 8):
    """Attach Google place_id (and indirectly photo via /api/place_photo) to hotel candidates.

    We keep this best-effort to avoid hard failures when API key is missing.
    Uses internal PlacesCache via services.images.google_places_resolve.

    Candidate schema (dict): expects name, lat, lon, address.
    Adds: place_id, photo_reference (optional), image_url (optional).
    """
    out = []
    if not cands:
        return out
    city = (city or '').strip()
    country = (country or '').strip()

    for h in list(cands)[: max(1, int(limit))]:
        try:
            d = dict(h or {})
            name = (d.get('name') or '').strip()
            lat = d.get('lat')
            lon = d.get('lon')
            pid = (d.get('place_id') or '').strip()
            pref = (d.get('photo_reference') or '').strip()

            if not pid and name:
                q = " ".join([x for x in [name, city, country, "hotel"] if x]).strip()
                g = google_places_resolve(q, lat=lat, lon=lon, radius_m=radius_m) or {}
                pid = (g.get('place_id') or '').strip() or pid
                pref = (g.get('photo_reference') or '').strip() or pref
                # attach rating/review count/price_level (best-effort)
                if g.get('rating') is not None:
                    d['google_rating'] = g.get('rating')
                if g.get('user_ratings_total') is not None:
                    d['google_review_count'] = g.get('user_ratings_total')
                if g.get('price_level') is not None:
                    d['price_level'] = g.get('price_level')

            if pid:
                d['place_id'] = pid
                d['image_url'] = f"/api/place_photo/?pid={pid}&w=900"
            elif pref:
                d['photo_reference'] = pref
                d['image_url'] = f"/api/place_photo/?ref={pref}&w=900"

            out.append(d)
        except Exception:
            try:
                out.append(dict(h or {}))
            except Exception:
                pass
    return out


def _dedup_hotels(hotels):
    """Dedupe hotel candidates by place_id (preferred) or normalized (name+address)."""
    out = []
    seen = set()
    for h in (hotels or []):
        if not isinstance(h, dict):
            continue
        pid = (h.get('place_id') or h.get('google_place_id') or '').strip()
        nm = (h.get('name') or '').strip()
        addr = (h.get('address') or '').strip()
        key = pid or _norm_key(nm + '|' + addr)
        if not key:
            continue
        if key in seen:
            continue
        seen.add(key)
        out.append(h)
    return out


def _get_city_center(country: str, city: str):
    """Get city center lat/lon from dataset, then Geoapify fallback."""
    cc = (country or '').strip()
    ct = (city or '').strip()
    if not ct:
        return (None, None)

    # 1) dataset-derived city center
    try:
        base_dir = str(settings.BASE_DIR)
        idx = get_local_index(base_dir)
        if idx and idx.enabled():
            lat, lon = idx.get_city_center(country=cc, city=ct)
            if lat is not None and lon is not None:
                return (float(lat), float(lon))
    except Exception:
        pass

    # 2) Geoapify geocoding
    try:
        q = f"{ct} {cc}".strip()
        res = autocomplete_place(q, limit=1, kind='city_fallback')
        if res:
            lat = res[0].get('lat')
            lon = res[0].get('lon')
            if lat is not None and lon is not None:
                return (float(lat), float(lon))
    except Exception:
        pass

    return (None, None)


def _estimate_travel_hours(dist_km: float, from_country: str = '', to_country: str = ''):
    """Estimate travel time between cities.

    We keep this intentionally simple:
    - If country changes OR long distance -> flight (includes airport+city transfer overhead)
    - Otherwise -> ground (train/drive)

    Returns: (mode, hours)
    """
    try:
        d = float(dist_km or 0.0)
    except Exception:
        d = 0.0

    fc = (from_country or '').strip()
    tc = (to_country or '').strip()
    country_changed = bool(fc and tc and fc != tc)

    if country_changed or d >= 400:
        # flight speed ~750km/h + overhead (check-in/security 2h + airport<->city 1h)
        return ('flight', max(1.0, d / 750.0 + 3.0))
    if d >= 120:
        # train/intercity bus
        return ('ground', max(0.8, d / 120.0 + 0.5))
    # short move
    return ('ground', max(0.3, d / 60.0 + 0.3))


def _load_locations():
    """국가-도시 목록 로드"""
    try:
        json_path = os.path.join(settings.BASE_DIR, 'data', 'locations_kor.json')
        with open(json_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"locations_kor.json 로드 실패: {e}")
        return {}


def index(request):
    """메인 페이지 - 여행 계획 입력"""
    # 사용자 선호도 가져오기 (로그인한 경우)
    user_preference = None
    if request.user.is_authenticated:
        user_preference, _ = TravelPreference.objects.get_or_create(user=request.user)
    
    # 국가-도시 목록
    locations = _load_locations()
    
    context = {
        'user_preference': user_preference,
        'locations_json': json.dumps(locations, ensure_ascii=False),
        'today': datetime.now().strftime('%Y-%m-%d'),
    }
    return render(request, 'planner/index.html', context)


def _history_profile(user):
    """Very lightweight "learning" from history.

    - Uses VisitedPlace + like/dislike interactions.
    - Returns feature/type weights to bias next recommendation.

    This is intentionally conservative (no heavy ML) to keep the first Django port stable.
    """
    profile = {
        'type_weights': {},      # e.g. {'Restaurant': 1.2}
        'feature_weights': {},   # e.g. {'자연/힐링': 0.8}
        'landmark_boost': 0.0,
        'michelin_boost': 0.0,
    }
    if not user or not getattr(user, 'is_authenticated', False):
        return profile
    try:
        # 최근 기록 위주로 200개만
        vp = list(VisitedPlace.objects.filter(user=user).order_by('-created_at')[:200])
        if not vp:
            return profile

        total = len(vp)
        type_cnt = {}
        feat_cnt = {}
        lm_cnt = 0
        mi_cnt = 0

        for v in vp:
            t = (v.place_type or '').strip() or 'Attraction'
            type_cnt[t] = type_cnt.get(t, 0) + (1 if v.liked else 0.5)
            f = (v.feature or '').strip()
            if f:
                feat_cnt[f] = feat_cnt.get(f, 0) + (1 if v.liked else 0.5)

            # landmark/michelin signals are stored indirectly (feature/type)
            if (v.feature or '').strip() in ('명소', '역사', '박물관/미술관', '광장'):
                lm_cnt += 1
            if '미슐' in (v.notes or '') or 'MICHELIN' in (v.notes or '').upper():
                mi_cnt += 1

        # Normalize to small boosts
        for k, c in type_cnt.items():
            profile['type_weights'][k] = min(1.5, max(0.0, (c / max(1.0, total)) * 1.8))
        # keep only top 8 features
        top_feats = sorted(feat_cnt.items(), key=lambda x: -x[1])[:8]
        for k, c in top_feats:
            profile['feature_weights'][k] = min(1.2, max(0.0, (c / max(1.0, total)) * 1.5))

        profile['landmark_boost'] = min(1.0, (lm_cnt / max(1.0, total)) * 2.0)
        profile['michelin_boost'] = min(1.0, (mi_cnt / max(1.0, total)) * 2.0)
        return profile
    except Exception:
        return profile


def _maybe_update_preference_from_history(user_pref: TravelPreference, user) -> None:
    """Nudge preference sliders from recent history.

    We only move values by +/-1 per plan-generation to avoid surprising jumps.
    """
    if not user_pref or not user or not getattr(user, 'is_authenticated', False):
        return
    try:
        vp = list(VisitedPlace.objects.filter(user=user).order_by('-created_at')[:120])
        if not vp:
            return
        liked = [v for v in vp if v.liked]
        if not liked:
            return

        # simple ratios
        tcnt = {'Restaurant': 0, 'Attraction': 0, 'Cafe': 0}
        lm_cnt = 0
        nat_cnt = 0
        cul_cnt = 0
        shop_cnt = 0
        for v in liked:
            t = (v.place_type or '').strip() or 'Attraction'
            if t in tcnt:
                tcnt[t] += 1
            feat = (v.feature or '').strip()
            if feat in ('명소', '역사', '박물관/미술관', '광장'):
                lm_cnt += 1
                cul_cnt += 1
            if any(x in feat for x in ['자연', '힐링', '산', '해변', '공원']):
                nat_cnt += 1
            if any(x in feat for x in ['쇼핑', '시장', '백화점', '아울렛']):
                shop_cnt += 1

        def _nudge(val: int, direction: int) -> int:
            return max(1, min(10, int(val) + int(direction)))

        # Food
        if tcnt['Restaurant'] >= max(3, tcnt['Attraction'] + 2):
            user_pref.food_preference = _nudge(user_pref.food_preference, +1)
        elif tcnt['Restaurant'] <= max(1, tcnt['Attraction'] - 3):
            user_pref.food_preference = _nudge(user_pref.food_preference, -1)

        # Landmark / Cultural
        if lm_cnt >= 4:
            user_pref.landmark_preference = _nudge(user_pref.landmark_preference, +1)
            user_pref.museum_preference = _nudge(user_pref.museum_preference, +1)

        # Nature
        if nat_cnt >= 4:
            user_pref.nature_preference = _nudge(user_pref.nature_preference, +1)

        # Shopping
        if shop_cnt >= 4:
            user_pref.shopping_preference = _nudge(user_pref.shopping_preference, +1)

        user_pref.save()
    except Exception:
        return


def generate_plan(request):
    """여행 계획 생성"""
    if request.method != 'POST':
        return redirect('planner:index')
    
    # 세션 생성
    if not request.session.session_key:
        request.session.create()
    
    # legs_json 파싱
    legs_json_str = request.POST.get('legs_json', '[]')
    people = int(request.POST.get('people', 2) or 2)
    include_cafe = request.POST.get('include_cafe', '1') == '1'
    
    # 여행 스타일 옵션 (이번 검색에서의 선호)
    travel_style = (request.POST.get('travel_style') or '').strip() or 'mixed'
    landmark_pref = int(request.POST.get('landmark_pref', 8) or 8)
    michelin_pref = request.POST.get('michelin_pref', '1') == '1'
    food_pref = int(request.POST.get('food_pref', 5) or 5)
    
    try:
        legs = json.loads(legs_json_str)
    except json.JSONDecodeError:
        messages.error(request, '일정 데이터가 올바르지 않습니다.')
        return redirect('planner:index')
    
    if not legs:
        messages.error(request, '일정을 최소 1개 이상 추가해주세요.')
        return redirect('planner:index')
    
    # 전체 여행 기간 계산
    all_dates = []
    for leg in legs:
        try:
            start = datetime.strptime(leg['start_date'], '%Y-%m-%d').date()
            end = datetime.strptime(leg['end_date'], '%Y-%m-%d').date()
            all_dates.extend([start, end])
        except (KeyError, ValueError):
            pass
    
    if not all_dates:
        messages.error(request, '날짜를 확인해주세요.')
        return redirect('planner:index')
    
    start_date = min(all_dates)
    end_date = max(all_dates)
    total_days = (end_date - start_date).days + 1
    
    # 목적지 추출
    destinations = []
    for leg in legs:
        destinations.append({
            'city': leg.get('to_city', ''),
            'country': leg.get('to_country', ''),
        })
    
    # 첫 번째 목적지로 타이틀 생성
    first_dest = legs[0].get('to_city', '여행') if legs else '여행'
    
    try:
        with transaction.atomic():
            # 여행 계획 생성
            travel_plan = TravelPlan.objects.create(
                user=request.user if request.user.is_authenticated else None,
                session_key=request.session.session_key or '',
                title=f"{first_dest} 여행",
                start_date=start_date,
                end_date=end_date,
                total_days=total_days,
                origin_city=legs[0].get('from_city', ''),
                origin_country=legs[0].get('from_country', ''),
                origin_country_code=legs[0].get('from_country_code', ''),
                destinations=destinations,
                theme=travel_style,
                travelers_count=people,
                status='planned'
            )
            
            # 사용자 선호도 저장/가져오기
            user_preference = None
            if request.user.is_authenticated:
                user_preference, _ = TravelPreference.objects.get_or_create(user=request.user)
                # ✅ 아주 가벼운 "학습"(기록 기반) → 선호도에 반영
                _maybe_update_preference_from_history(user_preference, request.user)

                # 이번 검색에서 사용자가 직접 선택한 값(슬라이더/스타일)이 우선
                user_preference.travel_style = travel_style
                user_preference.landmark_preference = landmark_pref
                user_preference.michelin_preference = michelin_pref
                user_preference.food_preference = food_pref
                user_preference.save()
            
            # 임시 선호도 객체 (비로그인)
            # NOTE: class body는 함수 스코프 변수를 참조(클로저)하지 못해 NameError가 날 수 있어서
            # SimpleNamespace로 안전하게 생성한다.
            temp_pref = SimpleNamespace(
                landmark_preference=landmark_pref,
                michelin_preference=michelin_pref,
                food_preference=food_pref,
                daily_poi_count=4,
                travel_style=travel_style,
            )
            
            pref = user_preference or temp_pref

            learn = _history_profile(request.user) if request.user.is_authenticated else {
                'type_weights': {},
                'feature_weights': {},
                'landmark_boost': 0.0,
                'michelin_boost': 0.0,
            }
            
            # 일자별 계획 생성
            # ✅ 핵심 버그fix:
            # - 구간(legs) 날짜가 겹치거나 비어있을 때 Day 3부터 중복/누락이 발생할 수 있음
            # - 전체 기간(start_date~end_date)을 Day 1..N으로 고정하고,
            #   각 날짜에 매핑되는 leg(도시)를 찾아서 생성하면 항상 안정적임.
            #
            # 매핑 규칙:
            # - 같은 날짜에 여러 leg가 겹치면 "뒤에 있는 leg"가 우선(사용자 입력 순서 기준)
            # - 날짜가 비는 구간(gap)이 있으면 직전 날짜의 도시를 그대로 유지(연속 일정 가정)

            normalized_legs = []
            for leg in legs:
                try:
                    leg_start = datetime.strptime(leg['start_date'], '%Y-%m-%d').date()
                    leg_end = datetime.strptime(leg['end_date'], '%Y-%m-%d').date()
                except (KeyError, ValueError):
                    continue
                to_country = leg.get('to_country', '') or ''
                to_city = leg.get('to_city', '') or ''
                if not to_city:
                    continue
                normalized_legs.append({
                    'start': leg_start,
                    'end': leg_end,
                    'to_country': to_country,
                    'to_city': to_city,
                })

            if not normalized_legs:
                raise ValueError("유효한 일정(도시/날짜)이 없습니다.")

            # date -> leg (later overwrites earlier on overlap)
            # + date -> covering legs (to detect same-day move)
            date_map = {}
            date_legs = {}
            for leg in normalized_legs:
                d = leg['start']
                while d <= leg['end']:
                    date_map[d] = leg
                    date_legs.setdefault(d, []).append(leg)
                    d += timedelta(days=1)

            last_leg = None

            # Per-trip dedupe + cached lookups
            used_place_ids = {}   # {(country, city): set(place_id)}
            used_name_keys = {}   # {(country, city): set(normalized_name)}
            city_centers = {}     # {(country, city): (lat, lon)}
            city_hotels = {}      # {(country, city): hotel_payload}

            prev_city = (travel_plan.origin_city or '').strip()
            prev_country = (getattr(travel_plan, 'origin_country', '') or '').strip()

            for day_offset in range(int(total_days)):
                current_date = start_date + timedelta(days=day_offset)
                leg = date_map.get(current_date) or last_leg or normalized_legs[0]
                last_leg = leg or last_leg

                to_country = (leg or {}).get('to_country', '') or ''
                to_city = (leg or {}).get('to_city', '') or ''

                city_key = ((to_country or '').strip(), (to_city or '').strip())

                # 이동 정보(표시용)
                from_city = ''
                from_country = ''
                transport = ''
                if day_offset == 0:
                    from_city = prev_city
                    from_country = prev_country
                else:
                    covering = date_legs.get(current_date) or []
                    if len(covering) >= 2:
                        from_city = (covering[-2].get('to_city') or '').strip()
                        from_country = (covering[-2].get('to_country') or '').strip()
                        transport = '당일 이동'
                    elif prev_city and prev_city != to_city:
                        from_city = prev_city
                        from_country = prev_country

                # City center cache
                if city_key not in city_centers:
                    city_centers[city_key] = _get_city_center(to_country, to_city)
                center_lat, center_lon = city_centers.get(city_key) or (None, None)

                # Travel duration estimate (flight + airport/city overhead)
                travel_hours = 0.0
                transport_duration = ''
                if from_city and (from_city.strip() != to_city.strip() or (from_country and from_country != to_country)):
                    from_key = ((from_country or '').strip(), (from_city or '').strip())
                    if from_key not in city_centers:
                        city_centers[from_key] = _get_city_center(from_country, from_city)
                    fl, fn = city_centers.get(from_key) or (None, None)

                    if fl is not None and fn is not None and center_lat is not None and center_lon is not None:
                        dist_km = _haversine_km(fl, fn, center_lat, center_lon)
                        mode, travel_hours = _estimate_travel_hours(dist_km, from_country, to_country)
                        transport_duration = _format_duration_hours(travel_hours)
                        if not transport and (from_city.strip() != to_city.strip()):
                            transport = '비행' if mode == 'flight' else '이동'
                    else:
                        if not transport and (from_city.strip() != to_city.strip()):
                            transport = '이동'

                # Daily POI count adjustment on heavy travel days
                try:
                    base_count = int(_pref_attr(pref, 'daily_poi_count', 4) or 4)
                except Exception:
                    base_count = 4
                base_count = max(1, min(6, base_count))
                daily_count = base_count
                if travel_hours >= 7.0:
                    daily_count = min(base_count, 2)
                elif travel_hours >= 4.0:
                    daily_count = min(base_count, 3)

                day_plan = DayPlan.objects.create(
                    travel_plan=travel_plan,
                    day_number=day_offset + 1,
                    date=current_date,
                    weekday=current_date.strftime('%A'),
                    from_city=from_city or '',
                    transport_from_prev=transport or '',
                    transport_duration=transport_duration or '',
                    to_city=to_city,
                    to_country=to_country,
                    latitude=center_lat,
                    longitude=center_lon,
                )

                # 숙소(Geoapify) - 같은 도시면 같은 숙소 재사용
                hotel_payload = city_hotels.get(city_key)
                if hotel_payload is None and center_lat is not None and center_lon is not None:
                    try:
                        hotels = hotels_near(center_lat, center_lon, radius_m=8000, limit=12) or []
                        hotels = [h for h in hotels if (h or {}).get('name') and (h or {}).get('lat') is not None and (h or {}).get('lon') is not None]
                        hotels = _dedup_hotels(hotels)
                        # ✅ Flask 버전 UX: 자동 "강제 선택"은 하지 않고, 후보만 제공 → 사용자가 선택
                        if hotels:
                            # 비용/속도: 상위 몇 개만 Google로 보강(사진/Place ID 캐시)
                            hotels = _enrich_hotels_with_google(hotels, city=to_city, country=to_country, radius_m=12000, limit=10)
                            hotels = _dedup_hotels(hotels)
                            
                            # v5.2: 루트 중심에서 가까운 거리 + 평점 순 정렬
                            def _hotel_sort_key(h):
                                # 루트 중심(center_lat, center_lon)에서의 거리 계산
                                h_lat = h.get('lat') or 0
                                h_lon = h.get('lon') or 0
                                dist_km = _haversine_km(center_lat, center_lon, h_lat, h_lon)
                                # 거리 정보 저장
                                h['distance_km'] = round(dist_km, 2)
                                # 평점 (없으면 0)
                                rating = h.get('google_rating') or 0
                                # 정렬: 거리 가까운 순 (1순위), 평점 높은 순 (2순위)
                                return (dist_km, -rating)
                            
                            hotels = sorted(hotels, key=_hotel_sort_key)
                            
                            hotel_payload = {
                                'selected': None,
                                'candidates': hotels[:6],
                                'city': to_city,
                                'country': to_country,
                                'center_lat': center_lat,
                                'center_lon': center_lon,
                            }
                    except Exception:
                        hotel_payload = None
                    city_hotels[city_key] = hotel_payload

                if hotel_payload:
                    day_plan.hotel = hotel_payload

                # 같은 도시 안에서 일정(추천루트) 중복 방지
                ex_ids = used_place_ids.setdefault(city_key, set())
                ex_names = used_name_keys.setdefault(city_key, set())

                # POI 후보 생성 (장소 추가 모달용)
                poi_candidates = _build_poi_candidates(
                    city=to_city,
                    country=to_country,
                    pref=pref,
                    learn=learn,
                    include_cafe=include_cafe,
                    exclude_place_ids=ex_ids,
                    exclude_names=ex_names,
                    anchor_lat=center_lat,
                    anchor_lon=center_lon,
                )
                day_plan.poi_candidates = poi_candidates

                # 기본 POI 선택 (관광지 2, 맛집 1, 카페 1) + 이동시간 반영
                selected_pois = _select_default_pois(
                    poi_candidates,
                    pref=pref,
                    learn=learn,
                    include_cafe=include_cafe,
                    daily_poi_count=daily_count,
                    exclude_place_ids=ex_ids,
                    exclude_names=ex_names,
                )

                # POI 저장 (이미지 포함)
                for idx, poi_data in enumerate(selected_pois):
                    _create_poi_with_image(day_plan, idx, poi_data)

                # Dedupe memory (same-city within this trip)
                try:
                    for pd in selected_pois:
                        pid = (pd.get('google_place_id') or pd.get('place_id') or '').strip()
                        if pid:
                            ex_ids.add(pid)
                        nk = _norm_key(pd.get('name') or '')
                        if nk:
                            ex_names.add(nk)
                except Exception:
                    pass

                # 히어로 이미지 (랜드마크/명소 우선 → 도시 이미지 fallback)
                try:
                    hero = _pick_best_day_hero_image(day_plan)
                    if not hero:
                        hero = get_city_image(to_city, to_country)
                    day_plan.hero_image_url = hero or ''
                except Exception:
                    try:
                        day_plan.hero_image_url = get_city_image(to_city, to_country) or ''
                    except Exception:
                        day_plan.hero_image_url = ''

                # 팁 생성
                try:
                    day_plan.tips = generate_tips(to_city, to_country)
                except Exception:
                    day_plan.tips = []

                day_plan.save()

                # 다음 Day를 위한 이전 도시/국가 갱신
                prev_city = to_city
                prev_country = to_country


        
        # ✅ Step-by-step UX (Flask 버전과 동일한 흐름)
        # 계획 생성 직후 "전체 일정(카드 그리드)" 화면이 아니라 Day 1 상세로 바로 진입
        return redirect('planner:itinerary', plan_id=travel_plan.id, day_number=1)
        
    except Exception as e:
        logger.exception(f"여행 계획 생성 오류: {e}")
        messages.error(request, f'여행 계획 생성 중 오류가 발생했습니다.')
        return redirect('planner:index')


def _create_poi_with_image(day_plan, order, poi_data):
    """POI 생성 + 이미지 가져오기"""
    latlng = poi_data.get('latlng')
    lat = latlng[0] if latlng and len(latlng) >= 2 else None
    lon = latlng[1] if latlng and len(latlng) >= 2 else None
    
    # 이미지 가져오기
    image_url = ''
    image_hd_url = ''
    try:
        web, hd = get_poi_images(
            place_name=poi_data.get('name', ''),
            city_name=day_plan.to_city,
            typ=poi_data.get('type', 'Attraction'),
            seed=f"poi_{poi_data.get('id', '')}_{day_plan.to_city}",
            place_id=poi_data.get('google_place_id', ''),
            lat=lat,
            lon=lon
        )
        image_url = web or ''
        image_hd_url = hd or ''
    except Exception as e:
        logger.warning(f"이미지 가져오기 실패: {e}")
    
    POI.objects.create(
        day_plan=day_plan,
        order=order,
        name=poi_data.get('name', ''),
        raw_name=poi_data.get('raw_name', ''),
        poi_type=poi_data.get('type', 'Attraction'),
        feature=poi_data.get('feature', ''),
        google_place_id=poi_data.get('google_place_id', ''),
        latitude=lat,
        longitude=lon,
        rating=poi_data.get('rating'),
        review_count=poi_data.get('reviews'),
        is_landmark=poi_data.get('is_landmark', False),
        michelin_grade=poi_data.get('michelin_grade') or '',
        image_url=image_url,
        image_hd_url=image_hd_url,
        reason=poi_data.get('reason') or '',
    )





def _pick_best_day_hero_image(day_plan) -> str:
    """Day 카드/커버용 이미지 선택 (B안: '도시 고정 히어로' 우선).

    우선순위:
    1) 선택된 POI 중 랜드마크(is_landmark=True) 이미지 (있으면)
    2) 없으면 ✅ 도시 고정 히어로(get_city_image)
    3) 최후 fallback: 아무 POI 이미지(있으면)
    """
    try:
        pois = list(day_plan.pois.all().order_by('order'))
    except Exception:
        pois = []

    def _img(p) -> str:
        return ((getattr(p, 'image_hd_url', '') or getattr(p, 'image_url', '') or '')).strip()

    # 1) Explicit landmark only
    for p in pois:
        if getattr(p, 'is_landmark', False) and _img(p):
            return _img(p)

    # 2) Always prefer city hero (prevents cafe/food/random hero)
    try:
        city_hero = get_city_image(getattr(day_plan, 'to_city', ''), getattr(day_plan, 'to_country', ''))
        if city_hero:
            return city_hero
    except Exception:
        pass

    # 3) Fallback any POI image
    for p in pois:
        if _img(p):
            return _img(p)

    return ''




def _ensure_day_hero_image(day_plan, force: bool = False):
    """Ensure the day plan has a representative hero image URL.

    Why this exists:
    - 도시 사진이 랜덤(카페/버스 등)으로 뜨거나, 외부 호스트에서 막혀서 회색으로 뜨는 문제 해결

    Strategy:
    1) services.images의 **도시 고정 히어로(로컬 static/img/city_hero)** 우선
    2) 없으면 Wikimedia(랜드마크) 고정 URL
    3) 최후 fallback: POI 이미지(있으면)

    + remote URL은 MEDIA_ROOT/hero_cache 로 한번 받아서 /media/... 로 다시 제공(안정)
    """
    import hashlib
    import os
    import requests

    def _cache_to_media(remote_url: str, cache_key: str) -> str:
        """Download remote_url into MEDIA_ROOT/hero_cache and return a local /media/... URL when possible."""
        try:
            if not remote_url or not remote_url.startswith("http"):
                return remote_url

            h = hashlib.md5((cache_key + "|" + remote_url).encode("utf-8")).hexdigest()[:16]
            ext = os.path.splitext(remote_url.split("?")[0])[1].lower()
            if ext not in {".jpg", ".jpeg", ".png", ".webp"}:
                ext = ".jpg"

            rel_dir = "hero_cache"
            out_dir = os.path.join(settings.MEDIA_ROOT, rel_dir)
            os.makedirs(out_dir, exist_ok=True)
            fname = f"{h}{ext}"
            out_path = os.path.join(out_dir, fname)

            if os.path.exists(out_path) and os.path.getsize(out_path) > 5_000:
                return f"{settings.MEDIA_URL.rstrip('/')}/{rel_dir}/{fname}"

            r = requests.get(remote_url, timeout=12, headers={"User-Agent": "TravelAI/1.0"})
            if r.status_code != 200 or not r.content:
                return remote_url
            with open(out_path, "wb") as f:
                f.write(r.content)

            if os.path.getsize(out_path) > 5_000:
                return f"{settings.MEDIA_URL.rstrip('/')}/{rel_dir}/{fname}"
            return remote_url
        except Exception:
            return remote_url

    try:
        existing = (getattr(day_plan, "hero_image_url", "") or "").strip()

        city = (getattr(day_plan, "to_city", "") or getattr(day_plan, "city", "") or "").strip()
        country = (getattr(day_plan, "to_country", "") or getattr(day_plan, "country", "") or "").strip()
        # locations_kor.json: '제주도' -> hero slug는 '제주'
        if city == "제주도":
            city = "제주"

        cache_key = f"{country}|{city}".strip("|") or "unknown"

        # 1) ✅ 도시 고정 히어로 우선 (static/img/city_hero)
        city_url = ""
        try:
            if city or country:
                city_url = get_city_image(city, country, width=1600)
        except Exception:
            city_url = ""

        if city_url:
            city_url = _cache_to_media(city_url, cache_key)
            # 기존 값이 카페/랜덤(Unsplash/Picsum)인 경우 또는 force면 덮어쓰기
            bad = ("unsplash.com" in (existing or "") or "picsum.photos" in (existing or ""))
            if force or (not existing) or bad or (existing != city_url):
                day_plan.hero_image_url = city_url
                day_plan.save(update_fields=["hero_image_url"])
            return

        # 도시 히어로를 못 구했는데 기존 값이 있으면(그리고 강제가 아니면) 유지
        if existing and not force:
            return

        url = ""

        # 2) POI 이미지 fallback
        if not url:
            try:
                pois = list(day_plan.pois.all().order_by("order"))
            except Exception:
                pois = []

            def _is_landmark(p):
                return bool(getattr(p, "is_landmark", False)) or (getattr(p, "michelin_grade", "") or "") != "" or (getattr(p, "poi_type", "") or "") == "Attraction"

            best = next((p for p in pois if _is_landmark(p)), None) or (pois[0] if pois else None)
            if best:
                url = (getattr(best, "image_hd_url", "") or getattr(best, "image_url", "") or "").strip()

        if url:
            url = _cache_to_media(url, cache_key)
            day_plan.hero_image_url = url
            day_plan.save(update_fields=["hero_image_url"])
    except Exception as e:
        logger.warning("Hero image ensure failed: %s", e)


def results(request, plan_id):
    """결과 페이지"""
    travel_plan = get_object_or_404(TravelPlan, id=plan_id)
    
    # 권한 확인 (본인 또는 세션)
    if travel_plan.user and travel_plan.user != request.user:
        if not request.user.is_staff:
            messages.error(request, '접근 권한이 없습니다.')
            return redirect('planner:index')
    
    day_plans = travel_plan.day_plans.all().prefetch_related('pois').order_by('day_number')
    
    # hero 이미지 보정(랜드마크 우선)
    try:
        for dp in day_plans:
            _ensure_day_hero_image(dp, force=False)
    except Exception:
        pass

    context = {
        'plan': travel_plan,
        'day_plans': day_plans,
    }
    return render(request, 'planner/results.html', context)


def itinerary(request, plan_id, day_number):
    """일자별 상세 일정"""
    travel_plan = get_object_or_404(TravelPlan, id=plan_id)
    day_plan = get_object_or_404(DayPlan, travel_plan=travel_plan, day_number=day_number)
    
    pois = day_plan.pois.all().order_by('order')
    poi_candidates = day_plan.poi_candidates or []
    
    # 선택된 POI 이름 목록
    selected_names = set(poi.name for poi in pois)

    # LLM+RAG: load cached explanation (if any)
    sig = _ai_explain_signature(day_plan, pois)
    ai_explain = _load_ai_explain(plan_id, day_number)
    ai_explain_stale = False
    if ai_explain and ai_explain.get('signature') and ai_explain.get('signature') != sig:
        ai_explain_stale = True

    # LLM+RAG: load cached tips (optional)
    ai_tips = _load_ai_tips(plan_id, day_number)
    ai_tips_stale = False
    if ai_tips and ai_tips.get('signature') and ai_tips.get('signature') != sig:
        ai_tips_stale = True
    
    
    # v5.2.2: 숙소 미선택 Day 목록 (PPT 다운로드 시 경고용)
    all_day_plans = travel_plan.day_plans.all().prefetch_related('pois').order_by('day_number')
    try:
        for dp in all_day_plans:
            _ensure_day_hero_image(dp, force=False)
    except Exception:
        pass
    days_without_hotel = []
    for dp in all_day_plans:
        hotel = dp.hotel or {}
        if not hotel.get('selected'):
            days_without_hotel.append(dp.day_number)
    
    context = {
        'plan': travel_plan,
        'day_plan': day_plan,
        'day': day_number,
        'total_days': travel_plan.total_days,
        'day_numbers': list(range(1, int(travel_plan.total_days) + 1)),
        'pois': pois,
        'poi_candidates': poi_candidates,
        'selected_names': json.dumps(list(selected_names)),
        'days_without_hotel': days_without_hotel,
        'all_day_plans': all_day_plans,
        'ai_explain': ai_explain,
        'ai_explain_stale': ai_explain_stale,
        'ai_explain_signature': sig,

        'ai_tips': ai_tips,
        'ai_tips_stale': ai_tips_stale,
    }
    return render(request, 'planner/itinerary.html', context)


@require_POST
def reorder_pois(request, plan_id, day_number):
    """POI 순서 변경"""
    try:
        data = json.loads(request.body)
        new_order = data.get('order', [])
        
        day_plan = get_object_or_404(
            DayPlan, 
            travel_plan_id=plan_id, 
            day_number=day_number
        )
        
        pois = {poi.id: poi for poi in day_plan.pois.all()}
        
        for idx, poi_id in enumerate(new_order):
            if poi_id in pois:
                pois[poi_id].order = idx
                pois[poi_id].save()
        
        return JsonResponse({'ok': True})
    except Exception as e:
        return JsonResponse({'ok': False, 'error': str(e)}, status=400)


@require_POST
def day_explain_api(request, plan_id, day_number):
    """
    Generate (or load) an explanation for the day's itinerary.
    - Uses file-based cache with signature invalidation.
    - Safe when OPENAI_API_KEY is not set: returns deterministic explanation.
    """
    try:
        travel_plan = get_object_or_404(TravelPlan, id=plan_id)
        day_plan = get_object_or_404(DayPlan, travel_plan=travel_plan, day_number=day_number)
        pois = list(day_plan.pois.all().order_by('order'))

        sig = _ai_explain_signature(day_plan, pois)

        data = {}
        try:
            data = json.loads(request.body or "{}")
        except Exception:
            data = {}

        force = bool(data.get("force", False))

        cached = _load_ai_explain(plan_id, day_number)
        if cached and not force and cached.get("signature") == sig and cached.get("text"):
            return JsonResponse({"ok": True, "cached": True, "payload": cached})

        # Build POI dicts
        poi_dicts = []
        for p in pois:
            poi_dicts.append({
                "id": p.id,
                "name": p.name,
                "type": getattr(p, "poi_type", "") or getattr(p, "category", "") or "",
                "lat": p.latitude,
                "lon": p.longitude,
            })

        day_ctx = build_day_context(
            city=day_plan.to_city or "",
            country=day_plan.to_country or "",
            day_number=day_number,
            hotel=day_plan.hotel or {},
            pois=poi_dicts,
        )

        result = generate_day_explanation(day_ctx, top_k=5)

        payload = {
            "signature": sig,
            "plan_id": plan_id,
            "day_number": day_number,
            "city": day_plan.to_city,
            "text": result.get("text", ""),
            "sources": result.get("sources", []),
            "generated_at": datetime.utcnow().isoformat() + "Z",
            "used_llm": result.get("used_llm", False),
        }
        _save_ai_explain(plan_id, day_number, payload)
        return JsonResponse({"ok": True, "cached": False, "payload": payload})
    except Exception as e:
        logger.exception("Day explanation generation failed")
        return JsonResponse({"ok": False, "error": str(e)}, status=500)


@require_POST
def day_tips_api(request, plan_id, day_number):
    """Generate (or load) short travel tips for the day (LLM+RAG, cached).

    - File-based cache with signature invalidation (same signature as explanation).
    - Safe when OPENAI_API_KEY is not set: returns deterministic RAG-only tips.
    - Returned payload is also used by PDF/PPT export.
    """
    try:
        travel_plan = get_object_or_404(TravelPlan, id=plan_id)
        day_plan = get_object_or_404(DayPlan, travel_plan=travel_plan, day_number=day_number)
        pois = list(day_plan.pois.all().order_by('order'))

        sig = _ai_explain_signature(day_plan, pois)

        data = {}
        try:
            data = json.loads(request.body or "{}")
        except Exception:
            data = {}

        force = bool(data.get("force", False))

        cached = _load_ai_tips(plan_id, day_number)
        if cached and not force and cached.get("signature") == sig and cached.get("bullets"):
            return JsonResponse({"ok": True, "cached": True, "payload": cached})

        poi_dicts = []
        for p in pois:
            poi_dicts.append({
                "id": p.id,
                "name": p.name,
                "type": getattr(p, "poi_type", "") or getattr(p, "category", "") or "",
                "lat": p.latitude,
                "lon": p.longitude,
            })

        day_ctx = build_day_context(
            city=day_plan.to_city or "",
            country=day_plan.to_country or "",
            day_number=day_number,
            hotel=day_plan.hotel or {},
            pois=poi_dicts,
        )

        result = generate_ai_day_tips(day_ctx, top_k=5, max_bullets=3)
        payload = {
            "signature": sig,
            "plan_id": plan_id,
            "day_number": day_number,
            "city": day_plan.to_city,
            "bullets": result.get("bullets", []),
            "sources": result.get("sources", []),
            "generated_at": datetime.utcnow().isoformat() + "Z",
            "used_llm": result.get("used_llm", False),
        }
        _save_ai_tips(plan_id, day_number, payload)
        return JsonResponse({"ok": True, "cached": False, "payload": payload})
    except Exception as e:
        logger.exception("Day tips generation failed")
        return JsonResponse({"ok": False, "error": str(e)}, status=500)


def export_pdf(request, plan_id):
    """A) PDF export (main).

    PPTX는 환경(폰트/버전) 차이로 도형/글씨가 쉽게 틀어질 수 있어,
    동일한 16:9 레이아웃을 **PDF로 안정적으로** 출력합니다.

    ✅ 요청 반영
    - Day별 POI는 최대 5개까지만 출력
    - 숙소(Hotel) 정보가 한 줄에서 **짤릴 정도로 길면**,
      바로 다음 페이지에 "Accommodation Details" 템플릿 페이지를 추가해 전체 정보를 출력
    - Summary는 4일 단위로 묶어서 출력
    """
    import io
    import os
    import math
    import textwrap

    from django.http import HttpResponse
    from django.conf import settings

    # NOTE: ReportLab은 가상환경에 설치가 필요합니다.
    # reportlab이 없으면(초기 세팅) 500 에러 대신 "인쇄 → PDF 저장" 가능한 HTML 프린트 뷰로 폴백합니다.
    REPORTLAB_AVAILABLE = True
    try:
        from reportlab.pdfgen import canvas
        from reportlab.lib.utils import ImageReader
        from reportlab.lib.colors import Color
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
    except Exception:
        REPORTLAB_AVAILABLE = False

    from PIL import Image, ImageDraw

    # 로그인 여부에 따라 접근 제약을 유연하게(로컬/데모 환경에서 PDF 버튼이 로그인으로 튕기는 문제 방지)
    if getattr(request, "user", None) is not None and request.user.is_authenticated:
        plan = get_object_or_404(TravelPlan, id=plan_id, user=request.user)
    else:
        plan = get_object_or_404(TravelPlan, id=plan_id)

    day_plans = list(plan.day_plans.all().order_by("day_number").prefetch_related("pois"))
    MAX_POIS_PER_DAY = 5

    # 대표사진(도시/랜드마크) 강제 보정
    for dp in day_plans:
        try:
            _ensure_day_hero_image(dp, force=False)
        except Exception:
            pass

    # ------------------------------------------------------------
    # ✅ Print(HTML→PDF)용 뷰모델 보강
    #  - 호텔 블록: (사진) + 호텔명/주소(2줄)/평점·리뷰·거리 (B 레이아웃)
    #  - route 지도: Geoapify(있으면) → 없으면 오프라인 다이어그램 PNG 생성
    # ------------------------------------------------------------
    from services.maps import build_route_static_map_url
    from services.images import get_place_image
    import requests

    def _cache_url_to_media(url: str, key: str, ext: str = "jpg") -> str:
        """Download remote image once into MEDIA_ROOT/cache and return MEDIA_URL path.

        실패 시 원본 URL 반환.
        """
        if not url or not isinstance(url, str) or not url.lower().startswith(("http://", "https://")):
            return url
        try:
            cache_dir = os.path.join(settings.MEDIA_ROOT, "cache")
            os.makedirs(cache_dir, exist_ok=True)
            safe_key = re.sub(r"[^a-zA-Z0-9._-]+", "_", key)[:120]
            out_path = os.path.join(cache_dir, f"{safe_key}.{ext}")
            if os.path.exists(out_path) and os.path.getsize(out_path) > 1024:
                return f"{settings.MEDIA_URL}cache/{safe_key}.{ext}?v={int(os.path.getmtime(out_path))}"
            headers = {"User-Agent": "TravelAI/1.0"}
            r = requests.get(url, headers=headers, timeout=10)
            if r.status_code == 200 and r.content and len(r.content) > 2048:
                with open(out_path, "wb") as f:
                    f.write(r.content)
                return f"{settings.MEDIA_URL}cache/{safe_key}.{ext}?v={int(os.path.getmtime(out_path))}"
        except Exception:
            pass
        return url

    def _split_addr_2lines(addr: str) -> tuple[str, str]:
        addr = (addr or "").strip()
        if not addr:
            return "", ""
        # 우선 콤마 기준으로 한 번 정리 후, 길면 wrap
        compact = re.sub(r"\s+", " ", addr)
        lines = []
        for part in compact.split(","):
            part = part.strip()
            if part:
                lines.append(part)
        compact2 = ", ".join(lines) if lines else compact
        wrapped = textwrap.wrap(compact2, width=34)
        if not wrapped:
            return compact2, ""
        if len(wrapped) == 1:
            return wrapped[0], ""
        l1, l2 = wrapped[0], wrapped[1]
        if len(wrapped) > 2:
            # 두 줄 초과면 2번째 줄을 말줄임
            if len(l2) >= 2:
                l2 = (l2[: max(0, 33)] + "…")
            else:
                l2 = l2 + "…"
        return l1, l2

    def _selected_hotel_quick(dp):
        h = dp.hotel or {}
        if isinstance(h, dict) and h.get("selected"):
            return h
        sh = getattr(dp, "selected_hotel", None)
        return sh or {}

    def _ensure_route_map_for_print(dp):
        if getattr(dp, "route_map_url", None):
            return
        try:
            # nodes
            nodes = []
            h = _selected_hotel_quick(dp)
            hlat, hlon = h.get("lat"), h.get("lon")
            if hlat is not None and hlon is not None:
                nodes.append({"lat": float(hlat), "lon": float(hlon)})
            pois = list(dp.pois.all().order_by("order"))
            for p in pois[:MAX_POIS_PER_DAY]:
                if p.lat is None or p.lon is None:
                    continue
                nodes.append({"lat": float(p.lat), "lon": float(p.lon)})
            if len(nodes) < 2:
                return
            # try Geoapify
            route_line = [(n["lon"], n["lat"]) for n in nodes]
            url = build_route_static_map_url(nodes, route_line_lonlat=route_line, width=980, height=520)
            if url:
                dp.route_map_url = url
                return

            # offline diagram png
            out_dir = os.path.join(settings.MEDIA_ROOT, "route_maps")
            os.makedirs(out_dir, exist_ok=True)
            fname = f"plan{plan.id}_day{dp.day_number}_route.png"
            out_path = os.path.join(out_dir, fname)

            W, H = 980, 520
            img = Image.new("RGB", (W, H), (245, 248, 255))
            dr = ImageDraw.Draw(img)

            lats = [n["lat"] for n in nodes]
            lons = [n["lon"] for n in nodes]
            min_lat, max_lat = min(lats), max(lats)
            min_lon, max_lon = min(lons), max(lons)
            # pad
            pad_lat = (max_lat - min_lat) * 0.15 or 0.01
            pad_lon = (max_lon - min_lon) * 0.15 or 0.01
            min_lat -= pad_lat
            max_lat += pad_lat
            min_lon -= pad_lon
            max_lon += pad_lon

            def proj(lat, lon):
                x = int((lon - min_lon) / (max_lon - min_lon) * (W - 80) + 40)
                y = int((max_lat - lat) / (max_lat - min_lat) * (H - 80) + 40)
                return x, y

            pts = [proj(n["lat"], n["lon"]) for n in nodes]
            # line
            dr.line(pts, fill=(0, 98, 255), width=6, joint="curve")
            # nodes
            for i, (x, y) in enumerate(pts):
                r = 16
                dr.ellipse((x - r, y - r, x + r, y + r), fill=(0, 98, 255), outline=(255, 255, 255), width=4)
                dr.text((x - 6, y - 10), str(i), fill=(255, 255, 255))

            img.save(out_path, format="PNG")
            dp.route_map_url = f"{settings.MEDIA_URL}route_maps/{fname}?v={int(os.path.getmtime(out_path))}"
        except Exception:
            return

    for dp in day_plans:
        try:
            _ensure_route_map_for_print(dp)
        except Exception:
            pass

        # 호텔 뷰모델
        try:
            h = _selected_hotel_quick(dp)
            name = (h.get("name") or h.get("hotel_name") or h.get("title") or "").strip()
            addr = (h.get("address") or h.get("formatted_address") or h.get("vicinity") or "").strip()
            a1, a2 = _split_addr_2lines(addr)
            rating = h.get("google_rating") or h.get("rating")
            reviews = h.get("google_review_count") or h.get("review_count") or h.get("reviews")
            dist = h.get("distance_km") or h.get("distance")
            try:
                dist_km = float(dist) if dist is not None else None
            except Exception:
                dist_km = None

            photo = (h.get("photo") or h.get("photo_url") or h.get("image_url") or "").strip()
            if not photo and name:
                try:
                    photo = get_place_image(name, city=dp.to_city or dp.city or "", country=dp.to_country or "") or ""
                except Exception:
                    photo = ""
            if photo:
                photo = _cache_url_to_media(photo, f"hotel_{plan.id}_{dp.day_number}")

            dp.hotel_view = {
                "name": name,
                "addr1": a1,
                "addr2": a2,
                "rating": rating,
                "reviews": reviews,
                "distance_km": dist_km,
                "photo_url": photo,
            }
        except Exception:
            dp.hotel_view = {"name": ""}

    # reportlab이 없으면 HTML 프린트 뷰로 폴백(에러 없이 사용 가능)
    if not REPORTLAB_AVAILABLE:
        from django.shortcuts import render
        return render(
            request,
            "planner/export_pdf_print.html",
            {
                "plan": plan,
                "day_plans": day_plans,
                "MAX_POIS_PER_DAY": MAX_POIS_PER_DAY,
                "reportlab_missing": True,
                "auto_print": True,
            },
        )

    # 16:9 wide (PPT 13.333in x 7.5in -> 960x540pt)
    W, H = 960, 540

    # palette (0~1)
    C_BG = Color(0.965, 0.975, 1.0)
    C_CARD = Color(1, 1, 1)
    C_LINE = Color(0.86, 0.90, 0.97)
    C_TITLE = Color(0.07, 0.11, 0.20)
    C_MUTED = Color(0.34, 0.40, 0.50)
    C_PRIMARY = Color(0.10, 0.38, 0.92)
    C_TEAL = Color(0.13, 0.74, 0.67)

    def _register_font():
        """Try to register a Korean-capable font for PDF."""
        # Windows: Malgun Gothic
        win_font = r"C:\Windows\Fonts\malgun.ttf"
        try:
            if os.path.exists(win_font):
                pdfmetrics.registerFont(TTFont("KFont", win_font))
                return "KFont"
        except Exception:
            pass

        # Common Linux fonts
        for p in [
            "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ]:
            try:
                if os.path.exists(p):
                    pdfmetrics.registerFont(TTFont("KFont", p))
                    return "KFont"
            except Exception:
                continue

        return "Helvetica"

    FONT = _register_font()

    def _register_font_bold(base_font: str) -> str:
        """Return a usable bold font name.

        This must never raise. If bold registration fails, we fall back to:
        - Helvetica-Bold when the base is Helvetica
        - otherwise the base font itself
        """
        # If we ended up using the built-in Helvetica, use its bold face.
        if base_font == "Helvetica":
            return "Helvetica-Bold"

        # Try a bold companion file for common environments.
        # Windows: Malgun Gothic Bold
        win_bold = r"C:\Windows\Fonts\malgunbd.ttf"
        try:
            if os.path.exists(win_bold):
                pdfmetrics.registerFont(TTFont("KFontB", win_bold))
                return "KFontB"
        except Exception:
            pass

        # Common Linux fonts
        for p in [
            "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
            "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        ]:
            try:
                if os.path.exists(p):
                    pdfmetrics.registerFont(TTFont("KFontB", p))
                    return "KFontB"
            except Exception:
                continue

        # Last resort: reuse base font
        return base_font

    FONT_B = _register_font_bold(FONT)

    def _selected_hotel(dp) -> dict:
        """Return selected hotel dict if exists."""
        try:
            h = getattr(dp, "hotel", None) or {}
            if isinstance(h, dict) and isinstance(h.get("selected"), dict):
                return h.get("selected") or {}
            # Some older payloads may store directly
            if isinstance(h, dict):
                return h
        except Exception:
            pass
        return {}

    def _coerce_float(x):
        try:
            if x is None:
                return None
            return float(x)
        except Exception:
            return None

    def _hotel_latlon(h: dict):
        lat = _coerce_float(h.get("lat") or h.get("latitude"))
        lon = _coerce_float(h.get("lon") or h.get("lng") or h.get("longitude"))
        return lat, lon

    def _resolve_to_local_path(url: str) -> str:
        """Resolve /static/... or /media/... URL to a local filesystem path."""
        if not url:
            return ""
        try:
            if url.startswith("/static/"):
                base = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
                return os.path.join(base, url.lstrip("/").replace("/", os.sep))
            if hasattr(settings, "MEDIA_URL") and url.startswith(settings.MEDIA_URL):
                rel = url[len(settings.MEDIA_URL):].lstrip("/")
                return os.path.join(settings.MEDIA_ROOT, rel)
        except Exception:
            pass
        return ""

    def _load_image_any(url: str, target_w: int, target_h: int) -> Image.Image:
        """Load and cover-crop image to target size. Returns PIL Image (RGB)."""
        if not url:
            return None
        try:
            local_path = _resolve_to_local_path(url)
            if local_path and os.path.exists(local_path):
                img = Image.open(local_path).convert("RGB")
            else:
                # remote or absolute-uri
                import requests
                if url.startswith("/"):
                    url2 = request.build_absolute_uri(url)
                else:
                    url2 = url
                headers = {"User-Agent": "TravelAI PDF Exporter"}
                r = requests.get(url2, timeout=12, headers=headers)
                r.raise_for_status()
                img = Image.open(io.BytesIO(r.content)).convert("RGB")

            # cover crop
            iw, ih = img.size
            if iw <= 0 or ih <= 0:
                return None
            scale = max(target_w / iw, target_h / ih)
            nw, nh = int(iw * scale), int(ih * scale)
            img = img.resize((nw, nh))
            left = max(0, (nw - target_w) // 2)
            top = max(0, (nh - target_h) // 2)
            img = img.crop((left, top, left + target_w, top + target_h))
            return img
        except Exception:
            return None

    def _draw_card(cnv, x, y, w, h, r=18, shadow=True):
        if shadow:
            cnv.saveState()
            cnv.setFillColor(Color(0, 0, 0, alpha=0.10))
            cnv.roundRect(x + 4, y - 4, w, h, r, stroke=0, fill=1)
            cnv.restoreState()
        cnv.setFillColor(C_CARD)
        cnv.roundRect(x, y, w, h, r, stroke=0, fill=1)

    def _truncate_to_width(s: str, max_w: float, font_name: str, font_size: float) -> tuple[str, bool]:
        """Return (text, truncated?)."""
        s = (s or "").strip()
        if not s:
            return "", False
        try:
            w = pdfmetrics.stringWidth(s, font_name, font_size)
            if w <= max_w:
                return s, False
        except Exception:
            # if measurement fails, do naive cutoff
            if len(s) <= 45:
                return s, False

        ell = "…"
        lo, hi = 0, len(s)
        best = ""
        while lo <= hi:
            mid = (lo + hi) // 2
            cand = s[:mid].rstrip() + ell
            try:
                if pdfmetrics.stringWidth(cand, font_name, font_size) <= max_w:
                    best = cand
                    lo = mid + 1
                else:
                    hi = mid - 1
            except Exception:
                # fallback by length
                if mid <= 45:
                    best = cand
                    lo = mid + 1
                else:
                    hi = mid - 1
        return best or (s[:40] + ell), True

    def _wrap_to_lines(s: str, max_w: float, font_name: str, font_size: float, max_lines: int = 2) -> list[str]:
        """Wrap text into up to max_lines, width-aware (ReportLab stringWidth).

        - Returns list[str] (len <= max_lines)
        - If overflowing, last line is truncated with ellipsis.
        """
        s = (s or "").strip()
        if not s:
            return []

        words = s.split()
        lines: list[str] = []
        cur = ""
        for w in words:
            cand = (cur + " " + w).strip()
            try:
                if pdfmetrics.stringWidth(cand, font_name, font_size) <= max_w:
                    cur = cand
                    continue
            except Exception:
                if len(cand) <= 40:
                    cur = cand
                    continue

            # commit current line
            if cur:
                lines.append(cur)
            else:
                lines.append(w)
            cur = w
            if len(lines) >= max_lines:
                break

        if len(lines) < max_lines and cur:
            lines.append(cur)

        # enforce max_lines with truncation
        if len(lines) > max_lines:
            lines = lines[:max_lines]

        if lines:
            # ensure last line fits
            last = lines[-1]
            t, _ = _truncate_to_width(last, max_w, font_name, font_size)
            lines[-1] = t

        return lines[:max_lines]

    def _points_for_day(dp):
        """Collect ordered points for route diagram: Hotel(0) + POIs(1..)."""
        pts = []

        # hotel (0)
        h = _selected_hotel(dp)
        hlat, hlon = _hotel_latlon(h)
        hname = (h.get("name") or h.get("hotel_name") or h.get("title") or "").strip()
        if hlat is not None and hlon is not None:
            pts.append({"kind": "hotel", "idx": 0, "name": hname or "Hotel", "lat": hlat, "lon": hlon})

        # POIs (1..)
        try:
            for i, p in enumerate(list(dp.pois.all().order_by("order"))[:MAX_POIS_PER_DAY], start=1):
                lat = getattr(p, "latitude", None)
                lon = getattr(p, "longitude", None)
                lat = _coerce_float(lat)
                lon = _coerce_float(lon)
                if lat is None or lon is None:
                    continue
                pts.append({"kind": "poi", "idx": i, "name": (p.name or "").strip(), "lat": lat, "lon": lon})
        except Exception:
            pass

        return pts

    def _make_route_diagram(points, size=(520, 320)):
        """Offline-safe minimal route visualization (no external map tiles)."""
        w, h = size
        img = Image.new("RGB", (w, h), (245, 248, 255))
        draw = ImageDraw.Draw(img)

        if len(points) < 2:
            # show single marker if any
            if points:
                x, y = w // 2, h // 2
                r = 14
                draw.ellipse((x - r, y - r, x + r, y + r), fill=(25, 98, 255), outline=(255, 255, 255), width=2)
                draw.text((x - 4, y - 8), str(points[0]["idx"]), fill=(255, 255, 255))
            return img

        lats = [p["lat"] for p in points]
        lons = [p["lon"] for p in points]
        min_lat, max_lat = min(lats), max(lats)
        min_lon, max_lon = min(lons), max(lons)

        pad = 0.08
        lat_span = (max_lat - min_lat) or 0.01
        lon_span = (max_lon - min_lon) or 0.01
        min_lat -= lat_span * pad
        max_lat += lat_span * pad
        min_lon -= lon_span * pad
        max_lon += lon_span * pad

        def proj(lat, lon):
            x = int((lon - min_lon) / (max_lon - min_lon) * (w - 40) + 20)
            y = int((max_lat - lat) / (max_lat - min_lat) * (h - 40) + 20)
            return x, y

        pts_xy = [proj(p["lat"], p["lon"]) for p in points]

        # polyline
        draw.line(pts_xy, width=6, fill=(64, 132, 255))
        draw.line(pts_xy, width=2, fill=(255, 255, 255))

        for p, (x, y) in zip(points, pts_xy):
            r = 13
            draw.ellipse((x - r, y - r, x + r, y + r), fill=(25, 98, 255), outline=(255, 255, 255), width=2)
            label = "0" if p["kind"] == "hotel" else str(p["idx"])
            draw.text((x - 4, y - 8), label, fill=(255, 255, 255))

        return img


    def _load_image_contain(url: str, target_w: int, target_h: int, pad: int = 10, bg=(255, 255, 255)) -> Image.Image:
        """Load image and fit-inside (contain) with padding; returns PIL RGB image."""
        if not url:
            return None
        try:
            local_path = _resolve_to_local_path(url)
            if local_path and os.path.exists(local_path):
                img = Image.open(local_path).convert("RGB")
            else:
                import requests
                url2 = request.build_absolute_uri(url) if url.startswith("/") else url
                headers = {"User-Agent": "TravelAI PDF Exporter"}
                r = requests.get(url2, timeout=12, headers=headers)
                r.raise_for_status()
                img = Image.open(io.BytesIO(r.content)).convert("RGB")

            iw, ih = img.size
            if iw <= 0 or ih <= 0:
                return None

            inner_w = max(1, int(target_w - pad * 2))
            inner_h = max(1, int(target_h - pad * 2))
            scale = min(inner_w / iw, inner_h / ih)
            nw, nh = max(1, int(iw * scale)), max(1, int(ih * scale))
            img2 = img.resize((nw, nh))

            canvas_img = Image.new("RGB", (int(target_w), int(target_h)), bg)
            left = (int(target_w) - nw) // 2
            top = (int(target_h) - nh) // 2
            canvas_img.paste(img2, (left, top))
            return canvas_img
        except Exception:
            return None

    def _get_geoapify_key() -> str:
        """Try to read Geoapify key from multiple env/settings names (user .env variants)."""
        for k in [
            "GEOAPIFY_API_KEY",
            "GEOAPIFY_KEY",
            "GEOAPIFY_MAPS_API_KEY",
            "GEOAPIFY_PLACES_API_KEY",
            "Geoapify_Places_API",
            "Geoapify_API_Key",
        ]:
            v = (os.getenv(k, "") or "").strip()
            if v:
                return v
        # settings fallback
        for k in ["GEOAPIFY_API_KEY", "GEOAPIFY_KEY"]:
            try:
                v = getattr(settings, k, "") or ""
                if isinstance(v, str) and v.strip():
                    return v.strip()
            except Exception:
                pass
        return ""

    def _mercator_norm(lat: float, lon: float):
        """Return WebMercator normalized (0..1) coordinates."""
        lat = max(min(float(lat), 85.05112878), -85.05112878)
        lon = float(lon)
        x = (lon + 180.0) / 360.0
        siny = math.sin(math.radians(lat))
        y = 0.5 - math.log((1 + siny) / (1 - siny)) / (4 * math.pi)
        return x, y

    def _choose_zoom_center(points, w: int, h: int, pad_px: int = 44):
        """Compute a reasonable (zoom, center_lat, center_lon) for a set of points."""
        lats = [p["lat"] for p in points]
        lons = [p["lon"] for p in points]
        min_lat, max_lat = min(lats), max(lats)
        min_lon, max_lon = min(lons), max(lons)

        # add padding to bounds
        pad = 0.12
        lat_span = (max_lat - min_lat) or 0.01
        lon_span = (max_lon - min_lon) or 0.01
        min_lat -= lat_span * pad
        max_lat += lat_span * pad
        min_lon -= lon_span * pad
        max_lon += lon_span * pad

        center_lat = (min_lat + max_lat) / 2.0
        center_lon = (min_lon + max_lon) / 2.0

        # pick the highest zoom that fits bounds inside image
        for z in range(17, 1, -1):
            world = 256 * (2 ** z)
            x1, y1 = _mercator_norm(min_lat, min_lon)
            x2, y2 = _mercator_norm(max_lat, max_lon)
            span_x = abs(x2 - x1) * world
            span_y = abs(y2 - y1) * world
            if span_x <= (w - pad_px * 2) and span_y <= (h - pad_px * 2):
                return z, center_lat, center_lon

        return 3, center_lat, center_lon

    def _download_static_map_bg(points, size=(520, 320)):
        """Try to download a map background image; fallback to None."""
        if not points:
            return None
        w, h = size
        zoom, clat, clon = _choose_zoom_center(points, w, h)

        import requests
        headers = {"User-Agent": "TravelAI PDF Exporter"}

        # 1) Geoapify (if key available)
        key = _get_geoapify_key()
        if key:
            try:
                url = "https://maps.geoapify.com/v1/staticmap"
                params = {
                    "style": "osm-carto",
                    "width": int(w),
                    "height": int(h),
                    "center": f"lonlat:{clon},{clat}",
                    "zoom": int(zoom),
                    "apiKey": key,
                }
                r = requests.get(url, params=params, timeout=12, headers=headers)
                if r.status_code == 200 and r.content and len(r.content) > 5000:
                    img = Image.open(io.BytesIO(r.content)).convert("RGB")
                    if img.size != (w, h):
                        img = img.resize((w, h))
                    return img
            except Exception:
                pass

        # 2) Public OSM static map (no key)
        try:
            url = "https://staticmap.openstreetmap.de/staticmap.php"
            params = {
                "center": f"{clat},{clon}",
                "zoom": int(zoom),
                "size": f"{int(w)}x{int(h)}",
                "maptype": "mapnik",
            }
            r = requests.get(url, params=params, timeout=12, headers=headers)
            if r.status_code == 200 and r.content and len(r.content) > 5000:
                img = Image.open(io.BytesIO(r.content)).convert("RGB")
                if img.size != (w, h):
                    img = img.resize((w, h))
                return img
        except Exception:
            pass

        return None

    def _make_route_map(points, size=(520, 320)):
        """Map + route overlay. Falls back to the offline diagram when map tiles are unavailable."""
        w, h = size
        if not points:
            return Image.new("RGB", (w, h), (245, 248, 255))

        bg = _download_static_map_bg(points, size=size)
        if bg is None:
            # fallback: keep previous offline diagram (always works)
            return _make_route_diagram(points, size=size)

        img = bg.copy()
        draw = ImageDraw.Draw(img)

        zoom, clat, clon = _choose_zoom_center(points, w, h)
        world = 256 * (2 ** zoom)
        cx, cy = _mercator_norm(clat, clon)
        cx *= world
        cy *= world

        def _to_xy(lat, lon):
            x, y = _mercator_norm(lat, lon)
            x *= world
            y *= world
            px = int(w / 2 + (x - cx))
            py = int(h / 2 + (y - cy))
            return px, py

        pts_xy = [_to_xy(p["lat"], p["lon"]) for p in points]

        if len(pts_xy) >= 2:
            draw.line(pts_xy, width=7, fill=(64, 132, 255))
            draw.line(pts_xy, width=3, fill=(255, 255, 255))

        for p, (x, y) in zip(points, pts_xy):
            r = 13
            draw.ellipse((x - r, y - r, x + r, y + r), fill=(25, 98, 255), outline=(255, 255, 255), width=2)
            label = "0" if p.get("kind") == "hotel" else str(p.get("idx") or "")
            try:
                draw.text((x - 4, y - 8), label, fill=(255, 255, 255))
            except Exception:
                pass

        return img

    def _make_travel_illustration(w: int, h: int):
        """Simple in-code illustration for cover (plane/train 느낌의 미니멀 아트)."""
        img = Image.new("RGB", (w, h), (242, 247, 255))
        draw = ImageDraw.Draw(img)

        # soft vertical gradient
        for yy in range(h):
            t = yy / max(1, h - 1)
            r = int(245 - 20 * t)
            g = int(250 - 18 * t)
            b = int(255 - 10 * t)
            draw.line((0, yy, w, yy), fill=(r, g, b))

        # waves
        for i in range(3):
            y0 = int(h * 0.70) + i * 10
            draw.arc((-80, y0 - 50, w + 80, y0 + 90), start=0, end=180, fill=(210, 225, 245), width=3)

        # plane silhouette (simple polygon)
        cx, cy = int(w * 0.55), int(h * 0.40)
        plane = [
            (cx - 180, cy + 10),
            (cx - 10, cy - 6),
            (cx + 70, cy - 42),
            (cx + 78, cy - 30),
            (cx + 10, cy + 2),
            (cx + 85, cy + 26),
            (cx + 78, cy + 38),
            (cx - 10, cy + 12),
            (cx - 180, cy + 10),
        ]
        draw.polygon(plane, fill=(200, 214, 235))

        # dotted contrail
        for k in range(18):
            x = int(w * 0.10 + k * (w * 0.02))
            y = int(h * 0.52 + (k % 3) * 2)
            draw.ellipse((x, y, x + 3, y + 3), fill=(185, 200, 225))

        return img


    def _make_hotel_placeholder(w: int, h: int):
        """Fallback thumbnail when a real hotel photo is not available."""
        img = Image.new("RGB", (w, h), (248, 250, 255))
        draw = ImageDraw.Draw(img)

        # subtle border
        draw.rectangle((0, 0, w - 1, h - 1), outline=(220, 228, 245), width=1)

        # simple bed icon
        bx, by = int(w * 0.20), int(h * 0.52)
        bw, bh = int(w * 0.60), int(h * 0.22)
        draw.rectangle((bx, by, bx + bw, by + bh), fill=(205, 216, 240))
        draw.rectangle((bx, by - int(bh * 0.55), bx + int(bw * 0.18), by), fill=(195, 208, 235))
        draw.rectangle((bx + int(bw * 0.22), by - int(bh * 0.55), bx + int(bw * 0.40), by), fill=(195, 208, 235))

        # label
        try:
            draw.text((int(w * 0.08), int(h * 0.12)), "Hotel", fill=(110, 125, 160))
        except Exception:
            pass
        return img


    # -------------------------
    # PDF composition
    # -------------------------
    buf = io.BytesIO()
    cnv = canvas.Canvas(buf, pagesize=(W, H))
    cnv.setTitle("TravelAI Itinerary")

    def _bg():
        cnv.setFillColor(C_BG)
        cnv.rect(0, 0, W, H, stroke=0, fill=1)

    def _draw_header(title: str):
        cnv.setFont(FONT, 20)
        cnv.setFillColor(C_TITLE)
        cnv.drawString(40, 500, title)
        cnv.setStrokeColor(C_LINE)
        cnv.setLineWidth(2)
        cnv.line(40, 492, 920, 492)

    def _cover():
        _bg()
        x, y, w, h = 40, 55, 880, 430
        _draw_card(cnv, x, y, w, h, r=26, shadow=True)

        # ✅ Cover: 사진 대신 '여행 일러스트' + 가운데 정렬 타이포 (PPT 예시 느낌)
        ill = _make_travel_illustration(int(w), 240)
        cnv.drawImage(ImageReader(ill), x, y + h - 240, width=w, height=240, mask="auto")

        cx = x + w / 2.0

        def _center(text, yy, size, color=C_TITLE):
            try:
                cnv.setFont(FONT, size)
                cnv.setFillColor(color)
                tw = pdfmetrics.stringWidth(text, FONT, size)
                cnv.drawString(cx - tw / 2.0, yy, text)
            except Exception:
                cnv.setFont(FONT, size)
                cnv.setFillColor(color)
                cnv.drawCentredString(cx, yy, text)

        _center("TRAVEL ITINERARY", y + 250, 34, C_TITLE)

        # date range
        start_date = day_plans[0].date if (day_plans and getattr(day_plans[0], "date", None)) else None
        end_date = day_plans[-1].date if (day_plans and getattr(day_plans[-1], "date", None)) else None
        date_line = ""
        try:
            if start_date and end_date:
                date_line = f"{start_date.strftime('%b %d')} – {end_date.strftime('%b %d, %Y')}"
        except Exception:
            date_line = ""
        if date_line:
            _center(date_line, y + 220, 12, C_MUTED)

        # cities line
        cities = []
        for dp in day_plans:
            c = (getattr(dp, "to_city", "") or "").strip()
            if c and (not cities or cities[-1] != c):
                cities.append(c)
        if cities:
            _center(" · ".join(cities)[:60], y + 196, 12, C_MUTED)

        # small brand
        cnv.setFont(FONT, 11)
        cnv.setFillColor(Color(0.55, 0.62, 0.76))
        cnv.drawString(x + 40, y + 48, "TravelAI")

        cnv.showPage()

    # ------------------------------------------------------------
    # LLM+RAG tips for PDF/PPT (safe + cached)
    # - If no cached tips exist, we generate deterministic RAG-only tips.
    # - When OPENAI_API_KEY is configured, the generator may use LLM, but
    #   failures always fall back to RAG-only output.
    # ------------------------------------------------------------
    def _get_or_make_day_tips(dp):
        try:
            pois = list(dp.pois.all().order_by('order'))
            sig = _ai_explain_signature(dp, pois)  # reuse same invalidation logic

            cached = _load_ai_tips(plan_id, dp.day_number)
            if cached and cached.get('signature') == sig and cached.get('bullets'):
                return cached

            poi_dicts = []
            for p in pois:
                poi_dicts.append({
                    "id": p.id,
                    "name": p.name,
                    "type": getattr(p, "poi_type", "") or getattr(p, "category", "") or "",
                    "lat": p.latitude,
                    "lon": p.longitude,
                })

            day_ctx = build_day_context(
                city=getattr(dp, "to_city", "") or "",
                country=getattr(dp, "to_country", "") or "",
                day_number=getattr(dp, "day_number", 0) or 0,
                hotel=getattr(dp, "hotel", {}) or {},
                pois=poi_dicts,
            )

            result = generate_ai_day_tips(day_ctx, top_k=5, max_bullets=3)
            payload = {
                "signature": sig,
                "plan_id": plan_id,
                "day_number": dp.day_number,
                "city": getattr(dp, "to_city", ""),
                "bullets": result.get("bullets", []),
                "sources": result.get("sources", []),
                "generated_at": datetime.utcnow().isoformat() + "Z",
                "used_llm": result.get("used_llm", False),
            }
            _save_ai_tips(plan_id, dp.day_number, payload)
            return payload
        except Exception:
            return None

    def _trip_overview(dp) -> bool:
        """Return True if accommodation detail page is needed."""
        _bg()
        _draw_header("Trip Overview")

        # main card
        x, y, w, h = 40, 70, 880, 400
        _draw_card(cnv, x, y, w, h, r=24, shadow=True)

        # pill
        cnv.setFillColor(Color(0.88, 0.93, 1.0))
        cnv.roundRect(x + 30, y + h - 58, 62, 28, 14, stroke=0, fill=1)
        cnv.setFont(FONT, 10)
        cnv.setFillColor(C_PRIMARY)
        cnv.drawString(x + 46, y + h - 49, f"Day {dp.day_number}")

        # city
        cnv.setFont(FONT, 18)
        cnv.setFillColor(C_TITLE)
        city = (getattr(dp, "to_city", "") or "").strip()
        cnv.drawString(x + 110, y + h - 52, city or "City")

        # date
        cnv.setFont(FONT, 9)
        cnv.setFillColor(C_MUTED)
        try:
            dline = dp.date.strftime("%b %d, %Y | %A") if dp.date else ""
        except Exception:
            dline = ""
        if dline:
            cnv.drawString(x + 110, y + h - 70, dline)

        # list items (left) - up to 5
        pois = list(dp.pois.all().order_by("order"))[:MAX_POIS_PER_DAY]
        cnv.setFont(FONT, 11)
        cnv.setFillColor(C_TITLE)
        ly = y + h - 120
        step = 28
        for p in pois:
            name = (p.name or "").strip()
            cnv.setFillColor(C_TEAL)
            cnv.roundRect(x + 32, ly - 6, 10, 10, 2, stroke=0, fill=1)
            cnv.setFillColor(C_TITLE)
            cnv.drawString(x + 50, ly, name[:40])
            ly -= step

        # --- Tips box (LLM+RAG, compact) ---
        tips_payload = _get_or_make_day_tips(dp)
        if tips_payload and tips_payload.get("bullets"):
            try:
                tbx = x + 30
                tby = y + 12 + 78 + 8  # above hotel thumbnail area
                tbw = 300
                tbh = 54

                cnv.setFillColor(Color(1.0, 0.98, 0.90))
                cnv.roundRect(tbx, tby, tbw, tbh, 12, stroke=0, fill=1)

                # title
                cnv.setFont(FONT_B, 9)
                cnv.setFillColor(Color(0.55, 0.40, 0.05))
                cnv.drawString(tbx + 12, tby + tbh - 15, "Tips")

                # bullets (max 3)
                cnv.setFont(FONT, 7.6)
                cnv.setFillColor(C_TITLE)
                lines = []
                for b in tips_payload.get("bullets", [])[:3]:
                    b = (b or "").strip()
                    if not b:
                        continue
                    if not (b.startswith("•") or b.startswith("-") or b.startswith("*")):
                        b = "• " + b
                    # keep each bullet to 1 line to avoid layout break
                    b, _ = _truncate_to_width(b, tbw - 16, FONT, 7.6)
                    lines.append(b)

                yy = tby + tbh - 28
                for ln in lines[:3]:
                    cnv.drawString(tbx + 12, yy, ln)
                    yy -= 10

                # sources hint (top 2 titles)
                srcs = tips_payload.get("sources", [])[:2]
                if srcs:
                    cnv.setFont(FONT, 6.2)
                    cnv.setFillColor(C_MUTED)
                    parts = []
                    for i, s in enumerate(srcs, start=1):
                        t = (s.get("title") or "").strip()
                        if not t:
                            continue
                        t, _ = _truncate_to_width(t, 110, FONT, 6.2)
                        parts.append(f"[{i}] {t}")
                    foot = "근거: " + " · ".join(parts)
                    foot, _ = _truncate_to_width(foot, tbw - 16, FONT, 6.2)
                    cnv.drawString(tbx + 12, tby + 6, foot)
            except Exception:
                pass

        # footer hotel block (B layout): thumbnail on the left, stacked text on the right
        hsel = _selected_hotel(dp)
        hname_raw = (hsel.get("name") or hsel.get("hotel_name") or hsel.get("title") or "").strip()
        address_raw = (hsel.get("address") or hsel.get("formatted_address") or "").strip()
        hr = hsel.get("google_rating") or hsel.get("rating")
        hc = hsel.get("google_review_count") or hsel.get("review_count") or hsel.get("reviews")
        dist = hsel.get("distance_km") or hsel.get("distance") or ""
        try:
            dist = f"{float(dist):.1f}km" if dist != "" else ""
        except Exception:
            dist = ""

        # resolve hotel thumbnail
        h_img_url = (hsel.get("image_url") or hsel.get("photo_url") or hsel.get("image") or "").strip()
        if not h_img_url:
            pid = (hsel.get("place_id") or hsel.get("google_place_id") or hsel.get("pid") or "").strip()
            if pid:
                h_img_url = f"/api/place_photo/?place_id={pid}&w=1200"

        thumb_w, thumb_h = 120, 78
        thumb = _load_image_any(h_img_url, thumb_w, thumb_h) if h_img_url else None
        if not thumb:
            thumb = _make_hotel_placeholder(thumb_w, thumb_h)

        thumb_x, thumb_y = x + 30, y + 12
        cnv.drawImage(ImageReader(thumb), thumb_x, thumb_y, width=thumb_w, height=thumb_h, mask="auto")

        # text area to the right
        tx = thumb_x + thumb_w + 12
        tw = (x + 330) - tx  # keep within left column; map starts at x+340
        if tw < 140:
            tw = w - (tx - x) - 30

        # name (1 line)
        cnv.setFont(FONT_B, 10)
        cnv.setFillColor(C_TITLE)
        hname, name_trunc = _truncate_to_width(hname_raw or "숙소", tw, FONT_B, 10)
        name_y = thumb_y + thumb_h - 12
        cnv.drawString(tx, name_y, hname)

        # address (up to 2 lines)
        cnv.setFont(FONT, 8.7)
        cnv.setFillColor(C_MUTED)
        addr_lines = _wrap_to_lines(address_raw, tw, FONT, 8.7, max_lines=2)
        ay = name_y - 14
        for line in addr_lines:
            cnv.drawString(tx, ay, line)
            ay -= 11

        # rating/reviews/distance (1 line)
        parts = []
        if hr not in (None, ""):
            try:
                parts.append(f"⭐ {float(hr):.1f}")
            except Exception:
                parts.append(f"⭐ {hr}")
        if hc not in (None, ""):
            try:
                parts.append(f"리뷰 {int(float(hc)):,}")
            except Exception:
                parts.append(f"리뷰 {hc}")
        if dist:
            parts.append(dist)
        meta = " · ".join([p for p in parts if p])
        meta, _ = _truncate_to_width(meta, tw, FONT, 8.7)
        cnv.drawString(tx, thumb_y + 4, meta)

        truncated = bool(name_trunc or (addr_lines and addr_lines[-1].endswith("…")))

        # route diagram (right) - max 5 POIs + hotel
        pts = _points_for_day(dp)
        map_img = _make_route_map(pts, size=(520, 320))
        cnv.drawImage(ImageReader(map_img), x + 340, y + 50, width=520, height=320, mask="auto")

        cnv.showPage()

        # 숙소 정보가 짤렸으면 다음 페이지에 상세 템플릿 추가
        return bool(truncated and hname)

    def _hotel_details(dp):
        _bg()
        _draw_header("Accommodation Details")

        x, y, w, h = 40, 70, 880, 400
        _draw_card(cnv, x, y, w, h, r=24, shadow=True)

        hsel = _selected_hotel(dp)
        hname = (hsel.get("name") or hsel.get("hotel_name") or hsel.get("title") or "").strip()
        address = (hsel.get("address") or hsel.get("formatted_address") or "").strip()
        hr = hsel.get("google_rating") or hsel.get("rating")
        hc = hsel.get("google_review_count") or hsel.get("review_count") or hsel.get("reviews")
        phone = (hsel.get("phone") or "").strip()
        website = (hsel.get("website") or "").strip()

        # image (prefer hotel image -> city hero)
        img_url = (hsel.get("image_url") or hsel.get("photo_url") or "").strip()
        if not img_url:
            img_url = getattr(dp, "hero_image_url", "") or ""
        hero = _load_image_any(img_url, 520, 320) if img_url else None
        if hero:
            cnv.drawImage(ImageReader(hero), x + 340, y + 50, width=520, height=320, mask="auto")

        # left text
        cnv.setFont(FONT, 12)
        cnv.setFillColor(C_TITLE)
        cnv.drawString(x + 30, y + h - 70, f"Day {dp.day_number} · {(getattr(dp, 'to_city', '') or '').strip()}")

        cnv.setFont(FONT, 16)
        cnv.setFillColor(C_TITLE)
        name_lines = textwrap.wrap(hname, width=26) if hname else ["(No hotel selected)"]
        cnv.drawString(x + 30, y + h - 105, name_lines[0][:40])
        if len(name_lines) > 1:
            cnv.setFont(FONT, 12)
            cnv.setFillColor(C_MUTED)
            cnv.drawString(x + 30, y + h - 128, name_lines[1][:40])

        cy = y + h - 165
        cnv.setFont(FONT, 11)
        cnv.setFillColor(C_MUTED)

        def _line(label, value):
            nonlocal cy
            if not value:
                return
            cnv.setFont(FONT, 10)
            cnv.setFillColor(C_MUTED)
            cnv.drawString(x + 30, cy, label)
            cnv.setFont(FONT, 11)
            cnv.setFillColor(C_TITLE)
            txt, _ = _truncate_to_width(str(value), 300, FONT, 11)
            cnv.drawString(x + 120, cy, txt)
            cy -= 26

        if hr not in (None, ""):
            try:
                _line("Rating", f"{float(hr):.1f}")
            except Exception:
                _line("Rating", hr)
        if hc not in (None, ""):
            try:
                _line("Reviews", f"{int(float(hc)):,}")
            except Exception:
                _line("Reviews", hc)
        _line("Address", address)
        _line("Phone", phone)
        _line("Website", website)

        cnv.showPage()

    def _summary_page(dps):
        _bg()
        _draw_header("Summary of the Trip")

        margin_x = 40
        gap = 18
        card_w = (W - margin_x * 2 - gap * 3) / 4.0
        card_h = 370
        x0 = margin_x
        y0 = 105

        # draw empty cards (always 4 for layout stability)
        for i in range(4):
            x = x0 + i * (card_w + gap)
            _draw_card(cnv, x, y0, card_w, card_h, r=18, shadow=True)

        for i, dp in enumerate(dps):
            x = x0 + i * (card_w + gap)

            hero_url = getattr(dp, "hero_image_url", "") or ""
            hero = _load_image_any(hero_url, int(card_w), 120)
            if hero:
                cnv.drawImage(ImageReader(hero), x, y0 + card_h - 120, width=card_w, height=120, mask="auto")

            cnv.setFillColor(Color(0.88, 0.93, 1.0))
            cnv.roundRect(x + 12, y0 + card_h - 32, 50, 18, 9, stroke=0, fill=1)
            cnv.setFont(FONT, 8)
            cnv.setFillColor(C_PRIMARY)
            cnv.drawString(x + 20, y0 + card_h - 28, f"Day {dp.day_number}")

            cnv.setFont(FONT, 11)
            cnv.setFillColor(C_TITLE)
            city = (getattr(dp, "to_city", "") or "").strip()
            cnv.drawString(x + 12, y0 + card_h - 150, city[:14] if city else "City")

            cnv.setFont(FONT, 7)
            cnv.setFillColor(C_MUTED)
            try:
                dline = dp.date.strftime("%b %d, %Y") if dp.date else ""
            except Exception:
                dline = ""
            if dline:
                cnv.drawString(x + 12, y0 + card_h - 164, dline)

            # POIs list (compact)
            cnv.setFont(FONT, 8.5)
            cnv.setFillColor(C_TITLE)
            py = y0 + card_h - 190
            pois = list(dp.pois.all().order_by("order"))[:3]
            for p in pois:
                cnv.setFillColor(C_TEAL)
                cnv.roundRect(x + 12, py - 6, 8, 8, 2, stroke=0, fill=1)
                cnv.setFillColor(C_TITLE)
                cnv.drawString(x + 24, py, (p.name or "")[:18])
                py -= 22
            # bottom hotel snippet (B): photo + name + rating (instead of POI image)
            hsel = _selected_hotel(dp)
            hname_raw = (hsel.get("name") or hsel.get("hotel_name") or hsel.get("title") or "").strip()
            hr = hsel.get("google_rating") or hsel.get("rating")
            hc = hsel.get("google_review_count") or hsel.get("review_count") or hsel.get("reviews")

            h_img_url = (hsel.get("image_url") or hsel.get("photo_url") or hsel.get("image") or "").strip()
            if not h_img_url:
                pid = (hsel.get("place_id") or hsel.get("google_place_id") or hsel.get("pid") or "").strip()
                if pid:
                    h_img_url = f"/api/place_photo/?place_id={pid}&w=1200"

            hw, hh = 64, 46
            hthumb = _load_image_any(h_img_url, hw, hh) if h_img_url else None
            if not hthumb:
                hthumb = _make_hotel_placeholder(hw, hh)

            bx = x + 12
            by = y0 + 16
            cnv.drawImage(ImageReader(hthumb), bx, by, width=hw, height=hh, mask="auto")

            tx = bx + hw + 8
            tw = card_w - (tx - x) - 12
            cnv.setFont(FONT_B, 8.3)
            cnv.setFillColor(C_TITLE)
            nm, _ = _truncate_to_width(hname_raw or "숙소", tw, FONT_B, 8.3)
            cnv.drawString(tx, by + hh - 12, nm)

            parts = []
            if hr not in (None, ""):
                try:
                    parts.append(f"⭐ {float(hr):.1f}")
                except Exception:
                    parts.append(f"⭐ {hr}")
            if hc not in (None, ""):
                try:
                    parts.append(f"리뷰 {int(float(hc)):,}")
                except Exception:
                    parts.append(f"리뷰 {hc}")
            meta = " · ".join([p for p in parts if p])
            cnv.setFont(FONT, 7.2)
            cnv.setFillColor(C_MUTED)
            meta, _ = _truncate_to_width(meta, tw, FONT, 7.2)
            cnv.drawString(tx, by + 4, meta)

        cnv.showPage()

    # Build pages
    # ✅ ReportLab이 설치되어 있어도(또는 폰트/이미지 이슈 등으로) 렌더링 중 예외가 나면,
    #    500 에러 대신 HTML 프린트 뷰로 안전하게 폴백합니다.
    try:
        _cover()
        for dp in day_plans:
            need_hotel_details = _trip_overview(dp)
            if need_hotel_details:
                try:
                    _hotel_details(dp)
                except Exception:
                    pass

        # summary pages (4 per page)
        for i in range(0, len(day_plans), 4):
            _summary_page(day_plans[i:i+4])

        cnv.save()
        pdf_bytes = buf.getvalue()
        buf.close()

        fname = f"TravelAI_plan_{plan_id}.pdf"
        resp = HttpResponse(pdf_bytes, content_type="application/pdf")
        resp["Content-Disposition"] = f'attachment; filename="{fname}"'
        return resp
    except Exception as e:
        try:
            buf.close()
        except Exception:
            pass
        from django.shortcuts import render
        return render(
            request,
            "planner/export_pdf_print.html",
            {
                "plan": plan,
                "day_plans": day_plans,
                "MAX_POIS_PER_DAY": MAX_POIS_PER_DAY,
                "reportlab_error": str(e),
                "auto_print": True,
            },
        )


def export_ppt(request, plan_id):
    """PPT export.

    ✅ 기본 목표: **PDF 결과와 동일한 비주얼**을 PPT로도 안정적으로 제공.
    - pypdfium2 가 설치되어 있으면: PDF를 렌더링해 각 페이지를 슬라이드 배경 이미지로 넣는 방식(권장)
      → PDF와 1:1로 동일하게 보임(폰트/줄바꿈/도형 깨짐 문제 없음)
    - pypdfium2 가 없으면: 기존 레거시 PPT(Design A) 생성 로직으로 폴백

    참고: PDF→PPT 방식은 "보기용"(배경 이미지)이라 텍스트 편집은 제한됩니다.
    """

    # ------------------------------------------------------------
    # 0) Prefer: PDF -> PPT (pixel-perfect)
    # ------------------------------------------------------------
    try:
        import pypdfium2 as pdfium
    except Exception:
        pdfium = None


    # ------------------------------------------------------------
    # 0-1) Guard: if pypdfium2 is missing, avoid generating "messy" legacy PPT by default
    #      (Users expect PPT to look exactly like PDF.)
    #      Use ?legacy=1 only when you explicitly want the old editable PPT.
    # ------------------------------------------------------------
    if pdfium is None and str(request.GET.get("legacy", "")).lower() not in ("1", "true", "yes"):
        from django.http import HttpResponse
        msg = (
            "PPT 다운로드를 위해 'pypdfium2' 설치가 필요합니다.\n\n"
            "현재 환경에서는 pypdfium2가 없어, PDF와 동일한(Pixel-perfect) PPT 생성이 불가능합니다.\n"
            "아래 명령으로 설치 후 다시 시도해 주세요.\n\n"
            "  .venv\\Scripts\\activate\n"
            "  pip install pypdfium2\n\n"
            "※ 임시로 (편집 가능한) 레거시 PPT가 필요하면 아래처럼 legacy 옵션을 붙이세요:\n"
            f"  /export/ppt/{plan_id}/?legacy=1\n"
        )
        return HttpResponse(msg, content_type="text/plain; charset=utf-8", status=501)

    if pdfium is not None:
        try:
            from django.http import HttpResponse
            from pptx import Presentation
            from pptx.util import Inches
            from PIL import Image
            import io

            pdf_resp = export_pdf(request, plan_id)
            ctype = ''
            try:
                ctype = pdf_resp.get('Content-Type', '')
            except Exception:
                try:
                    ctype = pdf_resp['Content-Type']
                except Exception:
                    ctype = ''

            # export_pdf가 HTML 폴백을 반환한 경우에는 레거시로 폴백
            if 'application/pdf' in (ctype or ''):
                pdf_bytes = pdf_resp.content

                prs = Presentation()
                prs.slide_width = Inches(13.333)
                prs.slide_height = Inches(7.5)
                blank = prs.slide_layouts[6]

                doc = pdfium.PdfDocument(pdf_bytes)
                try:
                    page_count = len(doc)
                except Exception:
                    page_count = getattr(doc, 'page_count', 0) or 0

                # 16:9 PDF(cover 포함)를 1920x1080 근사로 렌더링
                # scale=2.0 ~ 2.5 정도면 발표용으로 충분히 선명
                render_scale = 2.2

                for i in range(page_count):
                    page = doc.get_page(i)
                    try:
                        bmp = page.render(scale=render_scale)
                        pil_img = bmp.to_pil()
                    finally:
                        try:
                            page.close()
                        except Exception:
                            pass

                    if pil_img.mode != 'RGB':
                        pil_img = pil_img.convert('RGB')

                    bio = io.BytesIO()
                    pil_img.save(bio, format='PNG', optimize=True)
                    bio.seek(0)

                    sl = prs.slides.add_slide(blank)
                    sl.shapes.add_picture(bio, 0, 0, prs.slide_width, prs.slide_height)

                out = io.BytesIO()
                prs.save(out)
                ppt_bytes = out.getvalue()

                filename = f"TravelAI_plan_{plan_id}.pptx"
                resp = HttpResponse(
                    ppt_bytes,
                    content_type='application/vnd.openxmlformats-officedocument.presentationml.presentation'
                )
                resp['Content-Disposition'] = f'attachment; filename="{filename}"'
                return resp
        except Exception:
            # 어떤 이유로든 PDF->PPT가 실패하면, 아래 레거시 PPT 로직으로 폴백
            pass
    import io
    import math
    import re
    import textwrap
    import requests

    from django.http import HttpResponse
    from django.shortcuts import get_object_or_404

    from pptx import Presentation
    from pptx.util import Inches, Pt
    from pptx.dml.color import RGBColor
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.enum.text import PP_ALIGN

    from PIL import Image, ImageDraw, ImageFont, ImageFilter

    # 로그인 환경/데모 환경 모두 대응
    if getattr(request, 'user', None) is not None and request.user.is_authenticated:
        travel_plan = get_object_or_404(TravelPlan, id=plan_id, user=request.user)
    else:
        travel_plan = get_object_or_404(TravelPlan, id=plan_id)

    day_plans = list(
        travel_plan.day_plans.order_by('day_number').prefetch_related('pois')
    )

    # Ensure representative city/landmark images are used (fixes "Japan looks not Japan" cases)
    for dp in day_plans:
        _ensure_day_hero_image(dp, force=False)

    # -------------------------
    # Helpers
    # -------------------------
    FONT_KO = 'Malgun Gothic'
    FONT_EN = 'Calibri'

    C_BG = RGBColor(245, 248, 255)     # very light blue
    C_CARD = RGBColor(255, 255, 255)
    C_LINE = RGBColor(225, 232, 246)
    C_TITLE = RGBColor(15, 23, 42)     # slate-900
    C_SUB = RGBColor(71, 85, 105)      # slate-600
    C_ACCENT = RGBColor(37, 99, 235)   # blue-600

    DAY_COLORS = [
        (37, 99, 235),   # blue
        (20, 184, 166),  # teal
        (249, 115, 22),  # orange
        (168, 85, 247),  # purple
        (234, 179, 8),   # amber
        (239, 68, 68),   # red
    ]

    def _safe(s: str, limit=120):
        s = (s or '').strip()
        s = re.sub(r'\s+', ' ', s)
        return s[:limit]

    def _haversine_km(lat1, lon1, lat2, lon2):
        # robust even if floats come as None
        if lat1 is None or lon1 is None or lat2 is None or lon2 is None:
            return 0.0
        r = 6371.0
        p1 = math.radians(float(lat1)); p2 = math.radians(float(lat2))
        dp = math.radians(float(lat2) - float(lat1))
        dl = math.radians(float(lon2) - float(lon1))
        a = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
        return 2*r*math.asin(math.sqrt(a))

    def _points_for_day(dp):
        pts = []
        # hotel as 0
        if getattr(dp, 'selected_hotel_name', None) and dp.selected_hotel_lat and dp.selected_hotel_lon:
            pts.append({
                'label': 'Hotel',
                'name': dp.selected_hotel_name,
                'lat': float(dp.selected_hotel_lat),
                'lon': float(dp.selected_hotel_lon),
                'idx': 0,
                'kind': 'hotel',
            })
        pois = list(dp.pois.order_by('order'))
        start = 1
        for i, poi in enumerate(pois, start=start):
            # POI model fields: latitude/longitude (keep backward-compat for older dumps)
            lat = getattr(poi, 'latitude', None)
            lon = getattr(poi, 'longitude', None)
            if lat is None:
                lat = getattr(poi, 'lat', None)
            if lon is None:
                lon = getattr(poi, 'lon', None)
            if lat is None or lon is None:
                continue
            pts.append({
                'label': str(i),
                'name': getattr(poi, 'name', ''),
                'lat': float(lat),
                'lon': float(lon),
                'idx': i,
                'kind': getattr(poi, 'category', '') or '',
            })
        return pts

    def _download_image(url, timeout=8):
        if not url:
            return None
        if isinstance(url, str) and url.startswith('/'):
            url = request.build_absolute_uri(url)
        try:
            r = requests.get(url, timeout=timeout, headers={'User-Agent': 'TravelAI/1.0'})
            if r.status_code != 200:
                return None
            img = Image.open(io.BytesIO(r.content)).convert('RGB')
            return img
        except Exception:
            return None

    def _pil_to_png_bytes(img: Image.Image):
        bio = io.BytesIO()
        img.save(bio, format='PNG', optimize=True)
        return bio.getvalue()

    def _fit_cover(img: Image.Image, w, h):
        # center-crop to 16:9
        if img is None:
            img = Image.new('RGB', (w, h), (235, 242, 255))
        iw, ih = img.size
        target = w / h
        src = iw / ih
        if src > target:
            # too wide
            new_w = int(ih * target)
            x0 = (iw - new_w) // 2
            img = img.crop((x0, 0, x0 + new_w, ih))
        else:
            new_h = int(iw / target)
            y0 = (ih - new_h) // 2
            img = img.crop((0, y0, iw, y0 + new_h))
        return img.resize((w, h))

    def _make_route_diagram(points, w=1400, h=820, title=None, subtitle=None, segment_colors=None):
        """Offline-safe route visualization (no map tiles)."""
        img = Image.new('RGB', (w, h), (245, 248, 255))
        d = ImageDraw.Draw(img)

        # soft gradient band
        band_h = int(h * 0.32)
        grad = Image.new('RGB', (w, band_h), (225, 235, 255))
        d2 = ImageDraw.Draw(grad)
        for y in range(band_h):
            t = y / max(1, band_h-1)
            r = int(225 + (245-225)*t)
            g = int(235 + (248-235)*t)
            b = int(255 + (255-255)*t)
            d2.line([(0, y), (w, y)], fill=(r, g, b))
        img.paste(grad, (0, 0))

        # frame
        pad = 36
        d.rounded_rectangle([pad, pad, w-pad, h-pad], radius=28, fill=(255,255,255), outline=(220,230,250), width=3)

        if not points:
            # empty placeholder
            txt = 'Route map unavailable'
            d.text((w//2, h//2), txt, fill=(100,120,150), anchor='mm')
            return img

        lats = [p['lat'] for p in points]
        lons = [p['lon'] for p in points]
        min_lat, max_lat = min(lats), max(lats)
        min_lon, max_lon = min(lons), max(lons)
        # avoid div by zero
        if abs(max_lat - min_lat) < 1e-8:
            max_lat += 0.01
            min_lat -= 0.01
        if abs(max_lon - min_lon) < 1e-8:
            max_lon += 0.01
            min_lon -= 0.01

        inner = [pad+40, pad+70, w-pad-40, h-pad-40]
        ix0, iy0, ix1, iy1 = inner

        def proj(lat, lon):
            x = (lon - min_lon) / (max_lon - min_lon)
            y = 1 - (lat - min_lat) / (max_lat - min_lat)
            return (ix0 + x*(ix1-ix0), iy0 + y*(iy1-iy0))

        pts_xy = [proj(p['lat'], p['lon']) for p in points]

        # route line
        if len(pts_xy) >= 2:
            for i in range(len(pts_xy)-1):
                x0,y0 = pts_xy[i]
                x1,y1 = pts_xy[i+1]
                col = (37,99,235)
                if segment_colors and i < len(segment_colors):
                    col = segment_colors[i]
                d.line([(x0,y0),(x1,y1)], fill=col, width=8)
                # shadow-ish
                d.line([(x0,y0),(x1,y1)], fill=(200,210,230), width=2)

        # markers
        for i,(x,y) in enumerate(pts_xy):
            p = points[i]
            r = 16
            # halo
            d.ellipse([x-r-6,y-r-6,x+r+6,y+r+6], fill=(235,242,255), outline=None)
            d.ellipse([x-r,y-r,x+r,y+r], fill=(37,99,235), outline=(20,60,160), width=2)
            # label
            lbl = str(p.get('idx', i))
            try:
                font = ImageFont.truetype('arial.ttf', 16)
            except Exception:
                font = ImageFont.load_default()
            d.text((x, y-1), lbl, fill=(255,255,255), anchor='mm', font=font)

        # title/subtitle
        try:
            f_title = ImageFont.truetype('arialbd.ttf', 28)
            f_sub = ImageFont.truetype('arial.ttf', 18)
        except Exception:
            f_title = ImageFont.load_default()
            f_sub = ImageFont.load_default()

        if title:
            d.text((pad+60, pad+28), title, fill=(15,23,42), anchor='lm', font=f_title)
        if subtitle:
            d.text((pad+60, pad+56), subtitle, fill=(71,85,105), anchor='lm', font=f_sub)

        return img

    def _add_bg(slide):
        bg = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, 0, 0, prs.slide_width, prs.slide_height)
        bg.fill.solid()
        bg.fill.fore_color.rgb = C_BG
        bg.line.fill.background()

    def _add_card(slide, x, y, w, h, radius=True):
        shape = slide.shapes.add_shape(
            MSO_SHAPE.ROUNDED_RECTANGLE if radius else MSO_SHAPE.RECTANGLE,
            x, y, w, h
        )
        shape.fill.solid()
        shape.fill.fore_color.rgb = C_CARD
        shape.line.color.rgb = C_LINE
        shape.line.width = Pt(1)
        return shape

    def _add_text(slide, x, y, w, h, text, size=18, bold=False, color=C_TITLE, align='left', font=FONT_KO):
        tb = slide.shapes.add_textbox(x, y, w, h)
        tf = tb.text_frame
        tf.clear()
        p = tf.paragraphs[0]
        p.text = text
        p.font.size = Pt(size)
        p.font.bold = bold
        p.font.color.rgb = color
        p.font.name = font
        if align == 'center':
            p.alignment = PP_ALIGN.CENTER
        elif align == 'right':
            p.alignment = PP_ALIGN.RIGHT
        else:
            p.alignment = PP_ALIGN.LEFT
        return tb

    # -------------------------
    # PPT setup
    # -------------------------
    prs = Presentation()
    # 16:9 wide
    prs.slide_width = Inches(13.333)
    prs.slide_height = Inches(7.5)

    blank = prs.slide_layouts[6]

    # -------------------------
    # 1) Cover
    # -------------------------
    def _add_cover():
        sl = prs.slides.add_slide(blank)
        _add_bg(sl)

        # pick hero from first day (or fallback)
        hero_url = day_plans[0].hero_image_url if day_plans else ''
        hero = _download_image(hero_url) or Image.new('RGB', (1600, 900), (220, 232, 255))
        hero = _fit_cover(hero, 1920, 1080)
        hero_blur = hero.filter(ImageFilter.GaussianBlur(radius=3))
        # overlay soft white
        overlay = Image.new('RGBA', hero_blur.size, (255, 255, 255, 170))
        composed = hero_blur.convert('RGBA')
        composed.alpha_composite(overlay)

        png = _pil_to_png_bytes(composed.convert('RGB'))
        sl.shapes.add_picture(io.BytesIO(png), 0, 0, prs.slide_width, prs.slide_height)

        # title area card
        card = _add_card(sl, Inches(0.8), Inches(0.9), Inches(5.9), Inches(4.7))
        card.fill.solid(); card.fill.fore_color.rgb = RGBColor(255,255,255)
        card.fill.transparency = 0.08

        title = 'TRAVEL ITINERARY'
        sub = _safe(getattr(travel_plan, 'title', '') or 'TravelAI')
        cities = []
        for dp in day_plans:
            if dp.to_city:
                cities.append(dp.to_city)
        cities_line = ' · '.join(dict.fromkeys(cities))[:60]

        _add_text(sl, Inches(1.2), Inches(1.35), Inches(5.1), Inches(0.8), title, size=34, bold=True, font=FONT_EN)
        if sub and sub != 'TravelAI':
            _add_text(sl, Inches(1.2), Inches(2.05), Inches(5.1), Inches(0.4), sub, size=16, color=C_SUB, font=FONT_KO)
        if cities_line:
            _add_text(sl, Inches(1.2), Inches(2.55), Inches(5.3), Inches(0.4), cities_line, size=18, color=C_TITLE, font=FONT_KO)

        # small badge
        badge = sl.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, Inches(1.2), Inches(3.1), Inches(2.1), Inches(0.42))
        badge.fill.solid(); badge.fill.fore_color.rgb = C_ACCENT
        badge.line.fill.background()
        _add_text(sl, Inches(1.25), Inches(3.12), Inches(2.0), Inches(0.4), 'by TravelAI', size=12, bold=True, color=RGBColor(255,255,255), align='center', font=FONT_EN)

    # -------------------------
    # 2) Day detail slide
    # -------------------------
    def _add_day_detail(dp, day_idx):
        sl = prs.slides.add_slide(blank)
        _add_bg(sl)

        # header
        day_no = dp.day_number
        city = _safe(dp.to_city or '')
        country = _safe(dp.to_country or '')
        date_txt = ''
        if getattr(dp, 'date', None):
            try:
                date_txt = dp.date.strftime('%Y-%m-%d')
            except Exception:
                date_txt = str(dp.date)

        # Day pill
        pill = sl.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, Inches(0.8), Inches(0.55), Inches(1.05), Inches(0.42))
        pill.fill.solid(); pill.fill.fore_color.rgb = RGBColor(219, 234, 254)
        pill.line.fill.background()
        _add_text(sl, Inches(0.8), Inches(0.55), Inches(1.05), Inches(0.42), f'Day {day_no}', size=12, bold=True, color=C_ACCENT, align='center', font=FONT_EN)

        _add_text(sl, Inches(1.95), Inches(0.46), Inches(8.2), Inches(0.65), f'{city}', size=32, bold=True, color=C_TITLE, font=FONT_KO)
        if country or date_txt:
            _add_text(sl, Inches(1.95), Inches(1.05), Inches(8.2), Inches(0.35), '  '.join([t for t in [country, date_txt] if t]), size=14, color=C_SUB, font=FONT_KO)

        # panels
        left_x = Inches(0.8)
        left_y = Inches(1.55)
        left_w = Inches(5.6)
        left_h = Inches(5.55)
        right_x = Inches(6.6)
        right_y = Inches(1.55)
        right_w = Inches(5.93)
        right_h = Inches(5.55)

        _add_card(sl, left_x, left_y, left_w, left_h)
        _add_card(sl, right_x, right_y, right_w, right_h)

        # itinerary list
        pts = _points_for_day(dp)
        # distance
        dist_km = 0.0
        for a,b in zip(pts, pts[1:]):
            dist_km += _haversine_km(a['lat'], a['lon'], b['lat'], b['lon'])

        _add_text(sl, left_x+Inches(0.35), left_y+Inches(0.28), left_w-Inches(0.7), Inches(0.4), 'Trip Overview', size=18, bold=True, color=C_TITLE, font=FONT_EN)
        _add_text(sl, left_x+Inches(0.35), left_y+Inches(0.66), left_w-Inches(0.7), Inches(0.3), f'총 이동거리 약 {dist_km:.1f} km', size=12, color=C_SUB, font=FONT_KO)

        y = left_y + Inches(1.05)
        max_items = 7
        for i, p in enumerate(pts[:max_items]):
            idx = p.get('idx', i)
            name = _safe(p.get('name',''), limit=42)
            if not name:
                continue
            # number bubble
            bub = sl.shapes.add_shape(MSO_SHAPE.OVAL, left_x+Inches(0.35), y, Inches(0.32), Inches(0.32))
            bub.fill.solid(); bub.fill.fore_color.rgb = C_ACCENT
            bub.line.fill.background()
            _add_text(sl, left_x+Inches(0.35), y+Inches(0.01), Inches(0.32), Inches(0.32), str(idx), size=11, bold=True, color=RGBColor(255,255,255), align='center', font=FONT_EN)

            _add_text(sl, left_x+Inches(0.75), y-Inches(0.03), left_w-Inches(1.05), Inches(0.4), name, size=15, bold=(idx==0), color=C_TITLE, font=FONT_KO)
            y += Inches(0.50)

        if len(pts) > max_items:
            _add_text(sl, left_x+Inches(0.75), y, left_w-Inches(1.05), Inches(0.35), f"외 {len(pts)-max_items}개 더…", size=12, color=C_SUB, font=FONT_KO)

        # right: route map
        _add_text(sl, right_x+Inches(0.35), right_y+Inches(0.28), right_w-Inches(0.7), Inches(0.4), 'Route Map', size=18, bold=True, color=C_TITLE, font=FONT_EN)

        # diagram image
        seg_cols = None
        if len(pts) >= 2:
            seg_cols = [DAY_COLORS[day_idx % len(DAY_COLORS)] for _ in range(len(pts)-1)]
        diagram = _make_route_diagram(pts, w=1500, h=900, title=city, subtitle=country, segment_colors=seg_cols)
        png = _pil_to_png_bytes(diagram)
        sl.shapes.add_picture(io.BytesIO(png), right_x+Inches(0.25), right_y+Inches(0.75), right_w-Inches(0.5), right_h-Inches(1.05))

    # -------------------------
    # 3) Full route map summary
    # -------------------------
    def _add_full_route_summary():
        sl = prs.slides.add_slide(blank)
        _add_bg(sl)

        _add_text(sl, Inches(0.8), Inches(0.55), Inches(11.8), Inches(0.6), '전체 루트 지도 요약', size=30, bold=True, color=C_TITLE, font=FONT_KO)
        _add_text(sl, Inches(0.8), Inches(1.05), Inches(11.8), Inches(0.35), '여행 전체 동선을 한 장으로 확인해요.', size=14, color=C_SUB, font=FONT_KO)

        # collect all points in traversal order; color per day segment
        all_pts = []
        seg_cols = []
        for di, dp in enumerate(day_plans):
            pts = _points_for_day(dp)
            if not pts:
                continue
            if all_pts:
                # connect last of previous day to first of this day
                seg_cols.append(DAY_COLORS[di % len(DAY_COLORS)])
                all_pts.append(pts[0])
            for a,b in zip(pts, pts[1:]):
                seg_cols.append(DAY_COLORS[di % len(DAY_COLORS)])
                all_pts.append(b)
            if not all_pts:
                all_pts = pts
        # if all_pts empty (no POIs), still show placeholders
        if not all_pts:
            all_pts = []

        diag = _make_route_diagram(all_pts, w=1800, h=980, title='Full Trip Route', subtitle=''.join([]), segment_colors=seg_cols if seg_cols else None)
        png = _pil_to_png_bytes(diag)
        sl.shapes.add_picture(io.BytesIO(png), Inches(0.8), Inches(1.55), Inches(11.75), Inches(5.65))

    # -------------------------
    # 4) Summary slide (day cards)
    # -------------------------
    def _add_summary():
        sl = prs.slides.add_slide(blank)
        _add_bg(sl)

        _add_text(sl, Inches(0.8), Inches(0.55), Inches(11.8), Inches(0.6), 'Summary of the Trip', size=30, bold=True, color=C_TITLE, font=FONT_EN)
        _add_text(sl, Inches(0.8), Inches(1.05), Inches(11.8), Inches(0.35), '각 Day 하이라이트를 한 번에 확인해요.', size=14, color=C_SUB, font=FONT_KO)

        cols = 2
        rows = 2
        card_w = Inches(5.75)
        card_h = Inches(2.7)
        gap_x = Inches(0.55)
        gap_y = Inches(0.55)
        start_x = Inches(0.8)
        start_y = Inches(1.65)

        for i, dp in enumerate(day_plans[:4]):
            r = i // cols
            c = i % cols
            x = start_x + c*(card_w + gap_x)
            y = start_y + r*(card_h + gap_y)

            card = _add_card(sl, x, y, card_w, card_h)

            # image strip
            hero = _download_image(dp.hero_image_url) or Image.new('RGB', (900, 500), (220,232,255))
            hero = _fit_cover(hero, 900, 420)
            hero_bytes = _pil_to_png_bytes(hero)
            sl.shapes.add_picture(io.BytesIO(hero_bytes), x, y, card_w, Inches(1.1))

            # overlay title
            _add_text(sl, x+Inches(0.3), y+Inches(1.22), card_w-Inches(0.6), Inches(0.35), f"Day {dp.day_number} · {_safe(dp.to_city)}", size=16, bold=True, color=C_TITLE, font=FONT_KO)

            # 3 POIs
            pois = list(dp.pois.order_by('order'))[:3]
            yy = y+Inches(1.62)
            for poi in pois:
                nm = _safe(getattr(poi,'name',''), limit=32)
                _add_text(sl, x+Inches(0.35), yy, card_w-Inches(0.7), Inches(0.28), f"• {nm}", size=12, color=C_SUB, font=FONT_KO)
                yy += Inches(0.3)

        if len(day_plans) > 4:
            _add_text(sl, Inches(0.8), Inches(7.1), Inches(11.8), Inches(0.3), f"※ PPT에는 최대 4일 카드만 요약 표시 (총 {len(day_plans)}일)", size=10, color=C_SUB, font=FONT_KO)

    # Build deck
    _add_cover()
    for i, dp in enumerate(day_plans):
        _add_day_detail(dp, i)

    # Insert "전체 루트 지도 요약" as the 2nd-last slide (after all day detail slides, before summary)
    _add_full_route_summary()
    _add_summary()

    bio = io.BytesIO()
    prs.save(bio)
    ppt_bytes = bio.getvalue()

    filename = f"TravelAI_{travel_plan.id}.pptx"
    resp = HttpResponse(
        ppt_bytes,
        content_type='application/vnd.openxmlformats-officedocument.presentationml.presentation'
    )
    resp['Content-Disposition'] = f'attachment; filename="{filename}"'
    return resp





# ============================================================
# ✅ Restored candidate/selection helpers (fix: missing NameError)
# ============================================================
def _pref_attr(pref, name: str, default=None):
    """Safe getattr for pref object (TravelPreference or TempPref)."""
    try:
        return getattr(pref, name)
    except Exception:
        return default


def _poi_preference_score(poi: dict, pref, learn: dict) -> float:
    """Score a POI candidate for ordering/selection.

    Design goals for v1 Django port:
    - lightweight (no heavy ML)
    - predictable (small boosts only)
    - honors explicit search choices (travel_style + sliders)
    - uses history as a gentle bias (learn dict)
    """
    learn = learn or {
        'type_weights': {},
        'feature_weights': {},
        'landmark_boost': 0.0,
        'michelin_boost': 0.0,
    }

    typ = (poi.get('type') or 'Attraction').strip()
    feat = (poi.get('feature') or '').strip()
    rating = float(poi.get('rating') or 0.0)
    reviews = int(poi.get('reviews') or 0)
    is_lm = bool(poi.get('is_landmark'))
    is_michelin = bool(poi.get('michelin_grade'))

    # base quality (very gentle)
    score = rating * 0.3 + math.log1p(max(0, reviews)) * 0.07

    # slider preferences
    lm_pref = float(_pref_attr(pref, 'landmark_preference', 7) or 7)
    food_pref = float(_pref_attr(pref, 'food_preference', 5) or 5)
    mic_pref = bool(_pref_attr(pref, 'michelin_preference', True))

    if is_lm:
        score += (lm_pref / 2.2) + (float(learn.get('landmark_boost') or 0.0) * 2.0)
    if typ == 'Restaurant':
        score += (food_pref / 2.5)
        if is_michelin:
            score += (2.8 if mic_pref else -0.5) + (float(learn.get('michelin_boost') or 0.0) * 2.0)

    # explicit travel style (coarse buckets)
    style = (_pref_attr(pref, 'travel_style', '') or 'mixed').strip()
    if style == 'foodie':
        if typ == 'Restaurant':
            score += 2.0
        if is_michelin:
            score += 1.0
    elif style == 'cultural':
        if typ == 'Attraction':
            score += 1.2
        if is_lm or feat in ('역사', '박물관/미술관', '명소', '광장'):
            score += 1.3
    elif style == 'nature':
        if any(k in feat for k in ['자연', '힐링', '산', '해변', '공원']):
            score += 1.8
    elif style == 'shopping':
        if any(k in feat for k in ['쇼핑', '시장', '백화점', '아울렛']):
            score += 1.8
    elif style == 'relaxed':
        if typ == 'Cafe':
            score += 1.2
        if any(k in feat for k in ['자연', '힐링', '공원', '정원']):
            score += 1.0

    # history bias
    score += float((learn.get('type_weights') or {}).get(typ, 0.0)) * 1.2
    if feat:
        score += float((learn.get('feature_weights') or {}).get(feat, 0.0)) * 1.0

    return float(score)


def _sort_by_preference(items: list, pref, learn: dict) -> list:
    return sorted(
        items,
        key=lambda x: (
            -_poi_preference_score(x, pref, learn),
            -(float(x.get('rating') or 0.0)),
            -(int(x.get('reviews') or 0)),
            (x.get('name') or ''),
        )
    )


def _build_poi_candidates(
    city,
    country='',
    pref=None,
    include_cafe=True,
    learn=None,
    exclude_place_ids=None,
    exclude_names=None,
    anchor_lat=None,
    anchor_lon=None,
):
    """POI 후보 생성 - 랜드마크 3-4개, 미슐랭 3-4개 우선, 각각 10개 이하"""
    candidates = []
    
    base_dir = str(settings.BASE_DIR)
    
    try:
        local_index = get_local_index(base_dir)
        
        if not local_index or not local_index.enabled():
            logger.warning("LocalPOIIndex를 사용할 수 없습니다.")
            return candidates
        
        logger.info(f"POI 쿼리 시작: city={city}, country={country}")

        # City center (dataset-derived) - makes radius_km filtering meaningful
        center_lat, center_lon = local_index.get_city_center(country=country, city=city)

        # If caller provides a better anchor (e.g., hotel / UI-selected center), use it.
        try:
            if anchor_lat is not None and anchor_lon is not None:
                center_lat, center_lon = float(anchor_lat), float(anchor_lon)
        except Exception:
            pass

        ex_ids = set(exclude_place_ids or [])
        ex_names = set(exclude_names or [])

        def _is_excluded(item: dict) -> bool:
            try:
                pid = str((item or {}).get('google_place_id') or (item or {}).get('place_id') or '').strip()
                if pid and pid in ex_ids:
                    return True
                nk = _norm_key((item or {}).get('name') or '')
                if nk and nk in ex_names:
                    return True
            except Exception:
                return False
            return False
        
        # 관광지 (랜드마크 우선)
        attractions = local_index.query(
            country=country,
            city=city,
            typ='Attraction',
            center_lat=center_lat,
            center_lon=center_lon,
            limit=30  # 많이 가져와서 필터링
        )
        attractions = [a for a in attractions if not _is_excluded(a)]

        # 랜드마크와 일반 관광지 분리 + 선호/기록 기반으로 내부 정렬
        landmarks = _sort_by_preference([a for a in attractions if a.get('is_landmark')], pref, learn)
        non_landmarks = _sort_by_preference([a for a in attractions if not a.get('is_landmark')], pref, learn)
        
        # 랜드마크 우선, 총 10개를 항상 채우도록 보강
        # - 랜드마크 4개를 먼저 넣고
        # - 나머지는 비랜드마크로 채우되, 부족하면 추가 랜드마크로 채움
        lm_first = landmarks[:4]
        need = max(0, 10 - len(lm_first))
        fill = non_landmarks[:need]
        if len(fill) < need:
            fill += landmarks[4:4 + (need - len(fill))]
        selected_attractions = (lm_first + fill)[:10]
        candidates.extend(selected_attractions)
        
        logger.info(f"  Attractions: {len(selected_attractions)}개 (랜드마크 {len(landmarks[:4])}개)")
        
        # 맛집 (미슐랭 우선)
        restaurants = local_index.query(
            country=country,
            city=city,
            typ='Restaurant',
            center_lat=center_lat,
            center_lon=center_lon,
            limit=30
        )
        restaurants = [r for r in restaurants if not _is_excluded(r)]

        # 미슐랭과 일반 맛집 분리 + 선호/기록 기반으로 내부 정렬
        michelin = _sort_by_preference([r for r in restaurants if r.get('michelin_grade')], pref, learn)
        non_michelin = _sort_by_preference([r for r in restaurants if not r.get('michelin_grade')], pref, learn)
        
        # 미슐랭 우선, 총 10개를 항상 채우도록 보강
        mi_first = michelin[:4]
        need = max(0, 10 - len(mi_first))
        fill = non_michelin[:need]
        if len(fill) < need:
            fill += michelin[4:4 + (need - len(fill))]
        selected_restaurants = (mi_first + fill)[:10]
        candidates.extend(selected_restaurants)
        
        logger.info(f"  Restaurants: {len(selected_restaurants)}개 (미슐랭 {len(michelin[:4])}개)")
        
        # 카페
        if include_cafe:
            cafes = local_index.query(
                country=country,
                city=city,
                typ='Cafe',
                center_lat=center_lat,
                center_lon=center_lon,
                limit=8
            )
            cafes = [c for c in cafes if not _is_excluded(c)]
            cafes = _sort_by_preference(cafes, pref, learn)
            candidates.extend(cafes[:8])
            logger.info(f"  Cafes: {len(cafes[:8])}개")
        
        logger.info(f"총 POI 후보: {len(candidates)}개")
        
    except Exception as e:
        logger.exception(f"POI 후보 생성 오류: {e}")
    
    return candidates



def _poi_latlon(p):
    try:
        if not p:
            return (None, None)
        latlng = p.get('latlng')
        if latlng and len(latlng) >= 2:
            return (latlng[0], latlng[1])
        lat = p.get('lat')
        lon = p.get('lon')
        if lat is not None and lon is not None:
            return (lat, lon)
    except Exception:
        pass
    return (None, None)


def _route_cycle_cost(anchor_lat, anchor_lon, seq):
    """Cost for: anchor -> seq... -> anchor (km)."""
    if anchor_lat is None or anchor_lon is None or not seq:
        return 1e18
    cost = 0.0
    prev_lat, prev_lon = anchor_lat, anchor_lon
    for p in seq:
        lat, lon = _poi_latlon(p)
        if lat is None or lon is None:
            continue
        cost += _haversine_km(prev_lat, prev_lon, lat, lon)
        prev_lat, prev_lon = lat, lon
    cost += _haversine_km(prev_lat, prev_lon, anchor_lat, anchor_lon)
    return float(cost)


def _reorder_selected_pois_for_travel(selected, *, anchor_lat=None, anchor_lon=None):
    """
    Reorder selected POIs to make the route more travel-friendly.
    - Uses small-N exact permutation (<=8) and falls back to greedy.
    - Adds a light 'variety' preference when multiple attractions exist:
      try to start/end with attractions and keep food/cafe in the middle.
    """
    if not selected or len(selected) <= 2:
        return selected

    # split (coords ok) vs (coords missing)
    with_xy = []
    no_xy = []
    for p in selected:
        lat, lon = _poi_latlon(p)
        (with_xy if (lat is not None and lon is not None) else no_xy).append(p)

    if len(with_xy) <= 2:
        return selected

    # anchor fallback: centroid
    if anchor_lat is None or anchor_lon is None:
        try:
            lat_sum = 0.0
            lon_sum = 0.0
            n = 0
            for p in with_xy:
                lat, lon = _poi_latlon(p)
                if lat is None or lon is None:
                    continue
                lat_sum += float(lat)
                lon_sum += float(lon)
                n += 1
            if n > 0:
                anchor_lat = lat_sum / n
                anchor_lon = lon_sum / n
        except Exception:
            anchor_lat = anchor_lon = None

    if anchor_lat is None or anchor_lon is None:
        return selected

    # classify types
    attractions = [p for p in with_xy if (p.get('type') == 'Attraction')]
    restaurants = [p for p in with_xy if (p.get('type') == 'Restaurant')]
    cafes = [p for p in with_xy if (p.get('type') == 'Cafe')]
    others = [p for p in with_xy if p not in attractions + restaurants + cafes]

    n = len(with_xy)

    # exact search when small
    if n <= 8:
        import itertools

        def ok_pattern(seq):
            # If >=2 attractions and have food/cafe, encourage: Attraction ... Attraction (food/cafe inside)
            if len(attractions) >= 2 and (restaurants or cafes) and len(seq) >= 4:
                if seq[0].get('type') != 'Attraction':
                    return False
                if seq[-1].get('type') != 'Attraction':
                    return False
                # avoid restaurant/cafe at the very end when we can
                if seq[-2].get('type') in ('Restaurant', 'Cafe'):
                    return False
            return True

        best = None
        best_cost = None
        # First try constrained permutations, then relax
        for constrained in (True, False):
            for perm in itertools.permutations(with_xy, n):
                if constrained and not ok_pattern(perm):
                    continue
                c = _route_cycle_cost(anchor_lat, anchor_lon, perm)
                if best_cost is None or c < best_cost:
                    best_cost = c
                    best = list(perm)
            if best is not None:
                break

        if best is None:
            best = with_xy
        return best + no_xy

    # greedy nearest-neighbor + light variety
    remaining = with_xy[:]
    route = []
    cur_lat, cur_lon = anchor_lat, anchor_lon

    def _type_penalty(prev, nxt):
        try:
            if not prev or not nxt:
                return 0.0
            pt = prev.get('type')
            nt = nxt.get('type')
            if pt == nt and pt in ('Restaurant', 'Cafe'):
                return 0.25  # discourage consecutive food/cafe
        except Exception:
            pass
        return 0.0

    prev = None
    while remaining:
        best_i = 0
        best_score = None
        for i, p in enumerate(remaining):
            lat, lon = _poi_latlon(p)
            if lat is None or lon is None:
                continue
            d = _haversine_km(cur_lat, cur_lon, lat, lon)
            score = d + _type_penalty(prev, p)
            if best_score is None or score < best_score:
                best_score = score
                best_i = i
        p = remaining.pop(best_i)
        route.append(p)
        lat, lon = _poi_latlon(p)
        if lat is not None and lon is not None:
            cur_lat, cur_lon = lat, lon
        prev = p

    # If multiple attractions exist, try to end with an attraction by swapping a nearby one
    try:
        if len(attractions) >= 2 and route and route[-1].get('type') != 'Attraction':
            last_att_idx = None
            for i in range(len(route) - 1, -1, -1):
                if route[i].get('type') == 'Attraction':
                    last_att_idx = i
                    break
            if last_att_idx is not None and last_att_idx != len(route) - 1:
                route[last_att_idx], route[-1] = route[-1], route[last_att_idx]
    except Exception:
        pass

    return route + no_xy


def _select_default_pois(candidates, pref=None, include_cafe=True, learn=None, daily_poi_count=4, exclude_place_ids=None, exclude_names=None, anchor_lat=None, anchor_lon=None, travel_order=True):
    """기본 POI 선택 - 관광지 2, 맛집 1, 카페 1 (랜드마크/미슐랭 우선)"""
    selected = []

    ex_ids = set(exclude_place_ids or [])
    ex_names = set(exclude_names or [])

    def _is_excluded(it):
        try:
            pid = str((it or {}).get('google_place_id') or (it or {}).get('place_id') or '').strip()
            if pid and pid in ex_ids:
                return True
            nk = _norm_key((it or {}).get('name') or '')
            if nk and nk in ex_names:
                return True
        except Exception:
            return False
        return False

    if ex_ids or ex_names:
        candidates = [c for c in (candidates or []) if not _is_excluded(c)]
    
    # 타입별 분류
    attractions = [c for c in candidates if c.get('type') == 'Attraction']
    restaurants = [c for c in candidates if c.get('type') == 'Restaurant']
    cafes = [c for c in candidates if c.get('type') == 'Cafe']
    
    # 관광지 2개: "랜드마크 위주" (가능하면 2개 모두 랜드마크)
    landmarks = _sort_by_preference([a for a in attractions if a.get('is_landmark')], pref, learn)
    non_landmarks = _sort_by_preference([a for a in attractions if not a.get('is_landmark')], pref, learn)

    if len(landmarks) >= 2:
        # ✅ 가까운 랜드마크 2개를 우선 선택 (너무 멀리 떨어진 조합 방지)
        top_lm = landmarks[:min(15, len(landmarks))]
        best_pair = None
        best_cost = None

        def _lm_value(x):
            try:
                nm = str(x.get('name') or '')
                feat = str(x.get('feature') or '')
                rt = float(x.get('rating') or 0.0)
                rv = int(x.get('reviews') or x.get('google_review_count') or 0)
                return float(calculate_landmark_score(nm, feat, rt, rv))
            except Exception:
                return float(x.get('rating') or 0.0) * 2.0

        for i in range(len(top_lm)):
            for j in range(i + 1, len(top_lm)):
                a = top_lm[i]
                b = top_lm[j]
                la, loa = (a.get('latlng') or (None, None))
                lb, lob = (b.get('latlng') or (None, None))
                if la is None or loa is None or lb is None or lob is None:
                    continue
                d = _haversine_km(la, loa, lb, lob)
                v = _lm_value(a) + _lm_value(b)
                cost = d * 3.0 - v  # 거리 페널티 + 인기/랜드마크 가치 보상
                if best_cost is None or cost < best_cost:
                    best_cost = cost
                    best_pair = (a, b)

        if best_pair:
            selected.extend([best_pair[0], best_pair[1]])
        else:
            selected.extend(landmarks[:2])
    else:
        # ✅ 보강 로직: 필터/반경 등으로 랜드마크가 부족한 경우,
        # 데이터셋(이름/feature/평점/리뷰수) 기반 landmark_score로 "랜드마크 후보"를 승격해 2개를 채움.
        scored = []
        for a in attractions:
            try:
                nm = a.get('name') or a.get('google_name') or ''
                feat = a.get('feature') or ''
                rt = float(a.get('rating') or a.get('google_rating') or 0.0)
                rv = int(a.get('reviews') or a.get('google_review_count') or 0)
                sc = float(calculate_landmark_score(str(nm), str(feat), rt, rv))
            except Exception:
                sc = 0.0
            scored.append((sc, a))

        scored.sort(key=lambda x: x[0], reverse=True)

        promoted = []
        seen = set()
        for sc, a in scored:
            pid = str(a.get('place_id') or '')
            key = pid or str(a.get('name') or a.get('google_name') or '')
            if key in seen:
                continue
            seen.add(key)

            if a.get('is_landmark'):
                promoted.append(a)
            else:
                # 너무 무리한 승격을 막기 위한 최소 기준(리뷰/평점 없는 경우는 승격 안 함)
                try:
                    rv = int(a.get('reviews') or a.get('google_review_count') or 0)
                    rt = float(a.get('rating') or a.get('google_rating') or 0.0)
                except Exception:
                    rv, rt = 0, 0.0

                if rv >= 200 and rt >= 4.2 and sc > 0:
                    aa = dict(a)
                    aa['is_landmark'] = True
                    aa['landmark_promoted'] = True  # 디버그/로그용(템플릿에서 써도 되고 안 써도 됨)
                    promoted.append(aa)

            if len(promoted) >= 2:
                break

        if len(promoted) >= 2:
            selected.extend(promoted[:2])
        elif len(landmarks) == 1:
            selected.extend(landmarks[:1] + non_landmarks[:1])
        else:
            selected.extend(non_landmarks[:2])
    
    michelin = _sort_by_preference([r for r in restaurants if r.get('michelin_grade')], pref, learn)
    non_michelin = _sort_by_preference([r for r in restaurants if not r.get('michelin_grade')], pref, learn)

    # 맛집 1개 (미슐랭 우선은 체크/스타일에 따라)
    style = (_pref_attr(pref, 'travel_style', '') or 'mixed').strip()
    mic_pref = bool(_pref_attr(pref, 'michelin_preference', True)) or (style == 'foodie')
    sorted_restaurants = (michelin + non_michelin) if mic_pref else (non_michelin + michelin)
    # ✅ 선택된 랜드마크(관광지) 근처로 맛집을 붙임 (미슐랭 우선은 유지)
    tgt_lat = tgt_lon = None
    try:
        pts = [s.get('latlng') for s in selected if s.get('type') == 'Attraction' and s.get('latlng')]
        pts = [(p[0], p[1]) for p in pts if p and p[0] is not None and p[1] is not None]
        if pts:
            tgt_lat = sum(p[0] for p in pts) / len(pts)
            tgt_lon = sum(p[1] for p in pts) / len(pts)
    except Exception:
        tgt_lat = tgt_lon = None

    chosen_rest = None
    if sorted_restaurants:
        if tgt_lat is not None and tgt_lon is not None:
            best = None
            best_cost = None
            for r in sorted_restaurants[:min(25, len(sorted_restaurants))]:
                lat, lon = (r.get('latlng') or (None, None))
                if lat is None or lon is None:
                    continue
                d = _haversine_km(tgt_lat, tgt_lon, lat, lon)
                rt = float(r.get('rating') or 0.0)
                rv = int(r.get('reviews') or r.get('google_review_count') or 0)
                val = rt * 2.0 + math.log1p(max(rv, 0)) * 0.2
                if r.get('michelin_grade'):
                    val += 2.0
                cost = d * 2.8 - val
                if best_cost is None or cost < best_cost:
                    best_cost = cost
                    best = r
            chosen_rest = best or sorted_restaurants[0]
        else:
            chosen_rest = sorted_restaurants[0]
        selected.append(chosen_rest)

    # 카페 1개
    if include_cafe and cafes:
        cafes_sorted = _sort_by_preference(cafes, pref, learn)
        chosen_cafe = None
        # 음식점이 있으면 음식점 근처, 없으면 관광지 중심 근처
        cafe_tgt_lat = cafe_tgt_lon = None
        try:
            if chosen_rest and chosen_rest.get('latlng'):
                cafe_tgt_lat, cafe_tgt_lon = chosen_rest.get('latlng')
            elif tgt_lat is not None and tgt_lon is not None:
                cafe_tgt_lat, cafe_tgt_lon = tgt_lat, tgt_lon
        except Exception:
            cafe_tgt_lat = cafe_tgt_lon = None

        if cafe_tgt_lat is not None and cafe_tgt_lon is not None:
            best = None
            best_cost = None
            for c in cafes_sorted[:min(25, len(cafes_sorted))]:
                lat, lon = (c.get('latlng') or (None, None))
                if lat is None or lon is None:
                    continue
                d = _haversine_km(cafe_tgt_lat, cafe_tgt_lon, lat, lon)
                rt = float(c.get('rating') or 0.0)
                rv = int(c.get('reviews') or c.get('google_review_count') or 0)
                val = rt * 2.0 + math.log1p(max(rv, 0)) * 0.15
                cost = d * 2.5 - val
                if best_cost is None or cost < best_cost:
                    best_cost = cost
                    best = c
            chosen_cafe = best or cafes_sorted[0]
        else:
            chosen_cafe = cafes_sorted[0]
        selected.append(chosen_cafe)
    

    # 이동이 큰 날에는 기본 선택을 줄일 수 있음 (daily_poi_count)
    try:
        n = int(daily_poi_count or 4)
    except Exception:
        n = 4
    n = max(1, min(6, n))

    if n < len(selected):
        if n == 3:
            selected = selected[:3]  # 카페 드롭
        elif n == 2:
            # 관광지 1 + 맛집 1 (가능하면)
            first_attr = next((x for x in selected if x.get('type') == 'Attraction'), None)
            first_rest = next((x for x in selected if x.get('type') == 'Restaurant'), None)
            tmp = []
            if first_attr:
                tmp.append(first_attr)
            if first_rest:
                tmp.append(first_rest)
            if len(tmp) < 2:
                for x in selected:
                    if x not in tmp:
                        tmp.append(x)
                    if len(tmp) >= 2:
                        break
            selected = tmp[:2]
        elif n == 1:
            selected = selected[:1]
        else:
            selected = selected[:n]
    if travel_order:
        selected = _reorder_selected_pois_for_travel(selected, anchor_lat=anchor_lat, anchor_lon=anchor_lon)
    return selected
# =========================================================
# ✅ Tools: City hero downloader (no script)
# =========================================================
@require_GET
@ensure_csrf_cookie
def city_hero_tool(request):
    '''
    UI page: show city hero image status and provide one-click download.
    This writes into: static/img/city_hero/<slug>
    '''
    items = []
    base = os.path.join(settings.BASE_DIR, "static", "img", "city_hero")
    for (country, city), slug in CITY_HERO_SLUGS.items():
        p = os.path.join(base, slug)
        size = os.path.getsize(p) if os.path.exists(p) else 0
        items.append({
            "country": country,
            "city": city,
            "slug": slug,
            "exists": os.path.exists(p),
            "size": size,
            "url": f"/static/img/city_hero/{slug}?v={int(os.path.getmtime(p))}" if os.path.exists(p) else "",
        })
    items.sort(key=lambda x: (x["country"], x["city"]))
    return render(request, "planner/cityhero_tool.html", {"items": items})

@require_POST
def city_hero_tool_run(request):
    '''
    Run downloader. Returns JSON summary.
    '''
    force = request.POST.get("force") == "1"
    try:
        summary = download_city_hero_pack(force=force, width=1600)
        return JsonResponse({"ok": True, "summary": summary})
    except Exception as e:
        return JsonResponse({"ok": False, "error": str(e)}, status=500)

