"""
API 엔드포인트
"""
import json
import os
import math
from django.http import JsonResponse
from django.views.decorators.http import require_POST, require_GET
from django.views.decorators.csrf import csrf_exempt
from django.shortcuts import get_object_or_404
from django.contrib.auth.decorators import login_required
from django.db.models import Count
from django.conf import settings

from services.transit import get_transit_route, build_transit_fallback_url

from planner.models import TravelPlan, DayPlan, POI, UserPOIInteraction
from accounts.models import VisitedPlace


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
        return 9999.0


@require_POST
def add_poi(request):
    """POI 추가"""
    try:
        plan_id = request.POST.get('plan_id')
        day_number = int(request.POST.get('day_number', 0))
        poi_id = request.POST.get('poi_id', '')
        
        day_plan = get_object_or_404(
            DayPlan,
            travel_plan_id=plan_id,
            day_number=day_number
        )
        
        # POI 후보에서 찾기
        candidates = day_plan.poi_candidates or []
        found = None
        for c in candidates:
            if c.get('id') == poi_id:
                found = c
                break
        
        if not found:
            return JsonResponse({'ok': False, 'error': 'POI not found'}, status=404)
        
        # 중복 확인
        existing_names = set(p.name for p in day_plan.pois.all())
        if found.get('name') in existing_names:
            return JsonResponse({'ok': True, 'message': 'Already added'})
        
        # POI 추가
        max_order = day_plan.pois.count()
        poi = POI.objects.create(
            day_plan=day_plan,
            order=max_order,
            name=found.get('name', ''),
            raw_name=found.get('raw_name', ''),
            poi_type=found.get('type', 'Attraction'),
            feature=found.get('feature', ''),
            google_place_id=found.get('google_place_id', ''),
            latitude=found.get('latlng', [None, None])[0] if found.get('latlng') else None,
            longitude=found.get('latlng', [None, None])[1] if found.get('latlng') else None,
            rating=found.get('rating'),
            review_count=found.get('reviews'),
            is_landmark=found.get('is_landmark', False),
            michelin_grade=found.get('michelin_grade', ''),
            image_url=found.get('image', ''),
            image_hd_url=found.get('image_hd', ''),
        )
        
        # 상호작용 기록
        if request.user.is_authenticated:
            UserPOIInteraction.objects.create(
                user=request.user,
                poi_name=poi.name,
                poi_type=poi.poi_type,
                poi_feature=poi.feature,
                google_place_id=poi.google_place_id,
                city=day_plan.to_city,
                country=day_plan.to_country,
                action='add',
                is_landmark=poi.is_landmark,
                is_michelin=bool(poi.michelin_grade),
            )
        
        # 현재 POI 목록 반환
        pois = list(day_plan.pois.order_by('order').values(
            'id', 'name', 'poi_type', 'rating', 'is_landmark', 'michelin_grade'
        ))
        
        return JsonResponse({
            'ok': True,
            'pois': pois,
        })
        
    except Exception as e:
        return JsonResponse({'ok': False, 'error': str(e)}, status=400)


@require_POST
def remove_poi(request):
    """POI 삭제"""
    try:
        poi_id = request.POST.get('poi_id')
        
        if not poi_id:
            return JsonResponse({'ok': False, 'error': 'POI ID required'}, status=400)
        
        poi = get_object_or_404(POI, id=poi_id)
        day_plan = poi.day_plan
        
        # 상호작용 기록
        if request.user.is_authenticated:
            UserPOIInteraction.objects.create(
                user=request.user,
                poi_name=poi.name,
                poi_type=poi.poi_type,
                poi_feature=poi.feature,
                google_place_id=poi.google_place_id,
                city=day_plan.to_city,
                country=day_plan.to_country,
                action='remove',
                is_landmark=poi.is_landmark,
                is_michelin=bool(poi.michelin_grade),
            )
        
        poi.delete()
        
        # 순서 재정렬
        for idx, p in enumerate(day_plan.pois.order_by('order')):
            p.order = idx
            p.save()
        
        # 현재 POI 목록 반환
        pois = list(day_plan.pois.order_by('order').values(
            'id', 'name', 'poi_type', 'rating', 'is_landmark', 'michelin_grade'
        ))
        
        return JsonResponse({
            'ok': True,
            'pois': pois,
        })
        
    except Exception as e:
        return JsonResponse({'ok': False, 'error': str(e)}, status=400)


@require_POST
@login_required
def like_poi(request):
    """POI 좋아요"""
    try:
        poi_id = int(request.POST.get('poi_id', 0))
        liked = request.POST.get('liked', 'true').lower() == 'true'
        
        poi = get_object_or_404(POI, id=poi_id)
        poi.user_liked = liked
        poi.save()
        
        # 상호작용 기록
        UserPOIInteraction.objects.create(
            user=request.user,
            poi_name=poi.name,
            poi_type=poi.poi_type,
            poi_feature=poi.feature,
            google_place_id=poi.google_place_id,
            city=poi.day_plan.to_city,
            country=poi.day_plan.to_country,
            action='like' if liked else 'dislike',
            is_landmark=poi.is_landmark,
            is_michelin=bool(poi.michelin_grade),
        )
        
        return JsonResponse({'ok': True})
        
    except Exception as e:
        return JsonResponse({'ok': False, 'error': str(e)}, status=400)


@require_POST
@login_required
def record_visit(request):
    """방문 기록 저장"""
    try:
        poi_id = int(request.POST.get('poi_id', 0))
        rating = request.POST.get('rating')
        notes = request.POST.get('notes', '')
        
        poi = get_object_or_404(POI, id=poi_id)
        
        # 방문 장소 기록
        VisitedPlace.objects.update_or_create(
            user=request.user,
            place_id=poi.google_place_id or f'poi:{poi.id}',
            defaults={
                'place_name': poi.name,
                'place_type': poi.poi_type,
                'feature': poi.feature,
                'city': poi.day_plan.to_city,
                'country': poi.day_plan.to_country,
                'latitude': poi.latitude,
                'longitude': poi.longitude,
                'user_rating': int(rating) if rating else None,
                'liked': True,
                'notes': notes,
            }
        )
        
        return JsonResponse({'ok': True})
        
    except Exception as e:
        return JsonResponse({'ok': False, 'error': str(e)}, status=400)


@require_GET
def search_pois(request):
    """POI 검색"""
    try:
        city = request.GET.get('city', '')
        query = request.GET.get('q', '')
        poi_type = request.GET.get('type', '')
        
        if not city:
            return JsonResponse({'ok': False, 'error': 'City required'}, status=400)
        
        from services.local_poi import get_local_index
        from django.conf import settings
        
        # BASE_DIR을 문자열로 변환
        base_dir = str(settings.BASE_DIR)
        local_index = get_local_index(base_dir)
        results = []
        
        if local_index and local_index.enabled():
            for typ in ['Attraction', 'Restaurant', 'Cafe']:
                if poi_type and poi_type != typ:
                    continue
                    
                items = local_index.query(
                    country='',
                    city=city,
                    typ=typ,
                    center_lat=None,
                    center_lon=None,
                    limit=20
                )
                
                # 검색어 필터
                if query:
                    items = [i for i in items if query.lower() in (i.get('name', '') or '').lower()]
                
                results.extend(items)
        
        return JsonResponse({
            'ok': True,
            'results': results[:30],
        })
        
    except Exception as e:
        return JsonResponse({'ok': False, 'error': str(e)}, status=400)


@require_GET
def get_user_stats(request):
    """사용자 통계 (로그인 필요)"""
    if not request.user.is_authenticated:
        return JsonResponse({'ok': False, 'error': 'Login required'}, status=401)
    
    user = request.user
    
    # 상호작용 통계
    interactions = UserPOIInteraction.objects.filter(user=user)
    
    # 좋아요한 POI 타입 분포
    liked_types = interactions.filter(action='like').values('poi_type').distinct()
    
    # 자주 방문하는 도시
    top_cities = interactions.values('city').annotate(
        count=Count('id')
    ).order_by('-count')[:5]
    
    # 랜드마크 vs 로컬 선호도
    total = interactions.count()
    landmark_count = interactions.filter(is_landmark=True, action='add').count()
    michelin_count = interactions.filter(is_michelin=True, action='add').count()
    
    stats = {
        'total_interactions': total,
        'landmark_ratio': landmark_count / max(total, 1),
        'michelin_ratio': michelin_count / max(total, 1),
        'top_cities': list(top_cities),
        'travel_plans_count': user.travel_plans.count(),
        'visited_places_count': user.visited_places.count(),
    }
    
    return JsonResponse({'ok': True, 'stats': stats})


@require_GET
def place_photo(request):
    """Google Places Photo 프록시 (API 키 숨김 + 디스크 캐시)

    ⚠️ 중요: Windows/동시요청 상황에서 캐시 파일이 0바이트/깨진 상태로 남으면
    이후에도 계속 '깨진 이미지'로만 보이는 문제가 생길 수 있어,
    - 캐시 유효성(크기/시그니처/종료바이트) 검사
    - 원자적(atomic) 저장(os.replace)
    - 깨진 캐시는 자동 삭제 후 재다운로드
    를 수행한다.
    """
    import os
    import hashlib
    import time
    import requests
    from django.http import HttpResponse
    from django.conf import settings

    MIN_BYTES = 5_000  # 너무 작은 파일(깨진 캐시) 방지

    def _sniff_content_type(b: bytes) -> str:
        if not b:
            return "application/octet-stream"
        if b.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if b.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if len(b) >= 12 and b[:4] == b"RIFF" and b[8:12] == b"WEBP":
            return "image/webp"
        return "application/octet-stream"

    def _is_valid_image(b: bytes) -> bool:
        if not b or len(b) < MIN_BYTES:
            return False
        ctype = _sniff_content_type(b)
        if ctype == "image/jpeg":
            # JPEG는 보통 EOI(FFD9)로 끝남 (잘린 캐시 방지)
            return b.endswith(b"\xff\xd9")
        if ctype == "image/png":
            # PNG는 IEND 청크로 끝남
            return b.endswith(b"IEND\xaeB`\x82")
        if ctype == "image/webp":
            return True
        return False

    ref = (request.GET.get('ref') or '').strip()
    place_id = (request.GET.get('place_id') or request.GET.get('pid') or '').strip()

    # Allow callers to pass place_id; we will resolve it to a photo_reference via Place Details.
    if not ref and place_id:
        try:
            from storage.place_cache import PlacesCache
            try:
                pttl_days = int(os.getenv('PLACE_DETAILS_TTL_DAYS', '14'))
            except Exception:
                pttl_days = 14
            pttl_days = max(1, min(30, pttl_days))
            pttl = pttl_days * 24 * 60 * 60

            db = PlacesCache(base_dir=str(settings.BASE_DIR))
            cached = db.get_place(place_id) or {}
            ref = (cached.get('photo_reference') or '').strip()

            if not ref:
                details_url = "https://maps.googleapis.com/maps/api/place/details/json"
                api_key = (
                    os.getenv('GOOGLE_PLACES_API_KEY', '')
                    or os.getenv('GOOGLE_MAPS_API_KEY', '')
                    or os.getenv('Google_Places_API_Key', '')
                ).strip()
                params = {"place_id": place_id, "fields": "photos", "key": api_key}
                rr = requests.get(details_url, params=params, timeout=10)
                if rr.status_code == 200:
                    data = rr.json() or {}
                    result = data.get("result") or {}
                    photos = result.get("photos") or []
                    if photos:
                        ref = (photos[0] or {}).get("photo_reference") or ""
                        ref = (ref or "").strip()
                        if ref:
                            try:
                                db.set_place(place_id, photo_reference=ref, ttl_seconds=pttl)
                            except Exception:
                                pass
        except Exception:
            ref = ref or ""

    if not ref:
        return HttpResponse(status=404)

    try:
        w = int(request.GET.get('w') or 900)
    except Exception:
        w = 900
    w = max(100, min(2000, w))

    key = (
        os.getenv('GOOGLE_PLACES_API_KEY', '')
        or os.getenv('GOOGLE_MAPS_API_KEY', '')
        or os.getenv('Google_Places_API_Key', '')
    ).strip()
    if not key:
        return HttpResponse(status=404)

    # 디스크 캐시
    try:
        ttl_days = int(os.getenv('PHOTO_CACHE_TTL_DAYS', '14'))
    except Exception:
        ttl_days = 14
    ttl_days = max(1, min(30, ttl_days))
    ttl_sec = ttl_days * 24 * 60 * 60

    cache_dir = os.path.join(str(settings.BASE_DIR), 'instance', 'photo_cache')
    os.makedirs(cache_dir, exist_ok=True)

    cache_key = hashlib.sha1(f"{ref}:{w}".encode('utf-8', errors='ignore')).hexdigest()
    cache_path = os.path.join(cache_dir, f"{cache_key}.img")
    tmp_path = cache_path + ".tmp"

    # ✅ 캐시 확인 (깨진 캐시 자동 삭제)
    try:
        if os.path.exists(cache_path):
            age = time.time() - os.path.getmtime(cache_path)
            if 0 <= age <= ttl_sec and os.path.getsize(cache_path) >= MIN_BYTES:
                with open(cache_path, 'rb') as f:
                    content = f.read()
                if _is_valid_image(content):
                    ctype = _sniff_content_type(content)
                    resp = HttpResponse(content, content_type=ctype)
                    resp["Cache-Control"] = "public, max-age=86400"
                    return resp
                # invalid -> remove
                try:
                    os.remove(cache_path)
                except Exception:
                    pass
    except Exception:
        pass

    # Google API 호출
    try:
        url = "https://maps.googleapis.com/maps/api/place/photo"
        params = {"maxwidth": w, "photoreference": ref, "key": key}
        r = requests.get(url, params=params, timeout=12, allow_redirects=True)

        if r.status_code != 200 or not r.content:
            return HttpResponse(status=404)

        content = r.content
        # ✅ 유효성 검사 (HTML 에러/깨진 바이트 방지)
        if not _is_valid_image(content):
            return HttpResponse(status=404)

        ctype = _sniff_content_type(content)

        # ✅ 원자적 저장: tmp -> replace
        try:
            with open(tmp_path, 'wb') as f:
                f.write(content)
            if os.path.getsize(tmp_path) >= MIN_BYTES:
                os.replace(tmp_path, cache_path)
            else:
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass
        except Exception:
            # best-effort: ignore cache write errors
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except Exception:
                pass

        resp = HttpResponse(content, content_type=ctype)
        resp["Cache-Control"] = "public, max-age=86400"
        return resp

    except Exception:
        return HttpResponse(status=500)


@require_POST
def route_summary(request):
    """Return per-segment distance/time for a list of points.

    Payload JSON:
      {"points": [{"lat":..,"lon":..}, ...], "profile": "driving"}

    Response:
      {"ok": True, "segments": [{"distance_km":..,"duration_min":..}], "total": {...}, "provider": "osrm"|"approx"}
    """
    try:
        payload = json.loads(request.body or '{}')
        pts = payload.get('points') or []
        profile = (payload.get('profile') or 'driving').strip().lower()
        if profile not in ('driving',):
            profile = 'driving'
        if not isinstance(pts, list) or len(pts) < 2:
            return JsonResponse({'ok': False, 'error': 'need >=2 points'}, status=400)

        from services.routing import get_segment

        segments = []
        total_km = 0.0
        total_min = 0.0
        providers = []
        for i in range(len(pts) - 1):
            a = pts[i] or {}
            b = pts[i + 1] or {}
            try:
                lat1 = float(a.get('lat'))
                lon1 = float(a.get('lon'))
                lat2 = float(b.get('lat'))
                lon2 = float(b.get('lon'))
            except Exception:
                continue
            seg = get_segment(lat1, lon1, lat2, lon2, profile=profile)
            segments.append({
                'distance_km': float(seg.distance_km),
                'duration_min': float(seg.duration_min),
                'provider': seg.provider,
            })
            total_km += float(seg.distance_km)
            total_min += float(seg.duration_min)
            providers.append(seg.provider)

        provider = 'osrm' if any(p == 'osrm' for p in providers) else 'approx'
        return JsonResponse({
            'ok': True,
            'segments': segments,
            'total': {'distance_km': total_km, 'duration_min': total_min},
            'provider': provider,
        })
    except Exception as e:
        return JsonResponse({'ok': False, 'error': str(e)}, status=500)


@require_GET
def hotel_search(request):
    """Search hotels by name using Google Places (best-effort).
    
    v5.1: 리뷰수 + 평점 순 정렬, 가격 정보 추가

    Query params:
      q: hotel name
      city, country: for disambiguation
      lat, lon: optional for bias
    """
    import os
    import requests
    from django.conf import settings

    q = (request.GET.get('q') or '').strip()
    city = (request.GET.get('city') or '').strip()
    country = (request.GET.get('country') or '').strip()
    lat = request.GET.get('lat')
    lon = request.GET.get('lon')

    if not q:
        return JsonResponse({'ok': False, 'error': 'q required'}, status=400)

    api_key = (
        os.getenv('GOOGLE_PLACES_API_KEY', '')
        or os.getenv('GOOGLE_MAPS_API_KEY', '')
        or os.getenv('Google_Places_API_Key', '')
    ).strip()
    if not api_key:
        return JsonResponse({'ok': False, 'error': 'GOOGLE_PLACES_API_KEY missing'}, status=400)

    query = " ".join([x for x in [q, city, country, 'hotel'] if x]).strip()

    params = {
        'query': query,
        'type': 'lodging',
        'language': 'en',
        'key': api_key,
    }
    try:
        if lat is not None and lon is not None and str(lat).strip() and str(lon).strip():
            params['location'] = f"{float(lat)},{float(lon)}"
            params['radius'] = 15000
    except Exception:
        pass

    url = 'https://maps.googleapis.com/maps/api/place/textsearch/json'
    try:
        rr = requests.get(url, params=params, timeout=8, headers={'User-Agent': 'Mozilla/5.0'})
        if rr.status_code != 200:
            return JsonResponse({'ok': False, 'error': 'places request failed'}, status=502)
        data = rr.json() or {}
        results = data.get('results') or []
        out = []

        # best-effort store to PlacesCache (for later photo proxy)
        try:
            from storage.place_cache import PlacesCache
            db = PlacesCache(base_dir=str(settings.BASE_DIR))
        except Exception:
            db = None

        for r in results[:12]:  # 더 많이 가져와서 정렬 후 상위 표시
            pid = (r.get('place_id') or '').strip()
            name = (r.get('name') or '').strip()
            addr = (r.get('formatted_address') or r.get('vicinity') or '').strip()
            geo = (r.get('geometry') or {}).get('location') or {}
            rlat = geo.get('lat')
            rlon = geo.get('lng')
            photos = r.get('photos') or []
            pref = (photos[0] or {}).get('photo_reference') if photos else ''
            rating = r.get('rating')  # 평점
            review_count = r.get('user_ratings_total')  # 리뷰 수
            price_level = r.get('price_level')  # 가격 수준 (0-4)
            
            item = {
                'place_id': pid,
                'name': name,
                'address': addr,
                'lat': rlat,
                'lon': rlon,
                'photo_reference': pref or '',
                'image_url': f"/api/place_photo/?pid={pid}&w=900" if pid else '',
                'rating': rating,
                'review_count': review_count,
                'price_level': price_level,
            }
            if pid and db is not None:
                try:
                    db.set_place(pid, name_en=name, photo_reference=(pref or ''), lat=rlat, lon=rlon, ttl_seconds=14*24*3600)
                except Exception:
                    pass
            out.append(item)

        # v5.2: 루트 중심 거리 + 평점 기준 정렬
        # 검색 중심 좌표
        center_lat = float(lat) if lat else None
        center_lon = float(lon) if lon else None
        
        def sort_key(h):
            rt = h.get('rating') or 0
            # 좌표가 있으면 거리 계산
            if center_lat and center_lon and h.get('lat') and h.get('lon'):
                dist_km = _haversine_km(center_lat, center_lon, h.get('lat'), h.get('lon'))
            else:
                dist_km = 9999
            # 정렬: 거리 가까운 순 (1순위), 평점 높은 순 (2순위)
            return (dist_km, -rt)
        
        out.sort(key=sort_key)

        return JsonResponse({'ok': True, 'results': out[:8]})  # 상위 8개 반환
    except Exception as e:
        return JsonResponse({'ok': False, 'error': str(e)}, status=500)


@require_POST
def select_hotel_custom(request):
    """Select a hotel by Google place_id even if not in the original candidates.
    
    v5.1: Day별 개별 숙소 선택 가능 (같은 도시라도 Day마다 다른 숙소 선택)
    """
    import os
    import requests
    from django.conf import settings

    try:
        if request.content_type and 'application/json' in request.content_type:
            payload = json.loads(request.body or '{}')
        else:
            payload = request.POST

        plan_id = payload.get('plan_id')
        day_number = int(payload.get('day_number') or 0)
        place_id = (payload.get('place_id') or payload.get('pid') or '').strip()
        # v5.1: apply_all_days 옵션 (기본: False = 해당 Day만 적용)
        apply_all_days = str(payload.get('apply_all_days', '')).lower() in ('true', '1', 'yes')
        
        if not plan_id or day_number <= 0 or not place_id:
            return JsonResponse({'ok': False, 'error': 'invalid params'}, status=400)

        api_key = (
            os.getenv('GOOGLE_PLACES_API_KEY', '')
            or os.getenv('GOOGLE_MAPS_API_KEY', '')
            or os.getenv('Google_Places_API_Key', '')
        ).strip()
        if not api_key:
            return JsonResponse({'ok': False, 'error': 'GOOGLE_PLACES_API_KEY missing'}, status=400)

        travel_plan = get_object_or_404(TravelPlan, id=plan_id)
        day_plan = get_object_or_404(DayPlan, travel_plan=travel_plan, day_number=day_number)

        # Fetch details (best-effort)
        name = ''
        addr = ''
        lat = None
        lon = None
        photo_ref = ''
        rating = None
        review_count = None
        price_level = None
        
        try:
            from storage.place_cache import PlacesCache
            db = PlacesCache(base_dir=str(settings.BASE_DIR))
            cached = db.get_place(place_id) or {}
            name = (cached.get('name') or '').strip()
            photo_ref = (cached.get('photo_reference') or '').strip()
            lat = cached.get('lat', None)
            lon = cached.get('lon', None)
        except Exception:
            db = None

        if not name or not lat or not lon or not photo_ref:
            details_url = 'https://maps.googleapis.com/maps/api/place/details/json'
            params = {
                'place_id': place_id,
                'fields': 'name,formatted_address,geometry,photos,rating,user_ratings_total,price_level',
                'key': api_key,
                'language': 'en',
            }
            rr = requests.get(details_url, params=params, timeout=10)
            if rr.status_code == 200:
                data = rr.json() or {}
                result = data.get('result') or {}
                name = (result.get('name') or name).strip()
                addr = (result.get('formatted_address') or '').strip()
                geo = (result.get('geometry') or {}).get('location') or {}
                lat = geo.get('lat') if lat is None else lat
                lon = geo.get('lng') if lon is None else lon
                rating = result.get('rating')
                review_count = result.get('user_ratings_total')
                price_level = result.get('price_level')
                photos = result.get('photos') or []
                if photos and not photo_ref:
                    photo_ref = (photos[0] or {}).get('photo_reference') or ''
                    photo_ref = (photo_ref or '').strip()

                if db is not None:
                    try:
                        db.set_place(place_id, name_en=name, photo_reference=photo_ref, lat=lat, lon=lon, ttl_seconds=14*24*3600)
                    except Exception:
                        pass

        chosen = {
            'place_id': place_id,
            'name': name or place_id,
            'address': addr or '',
            'lat': lat,
            'lon': lon,
            'photo_reference': photo_ref or '',
            'image_url': f"/api/place_photo/?pid={place_id}&w=900",
            'google_rating': rating,
            'google_review_count': review_count,
            'price_level': price_level,
        }

        # v5.1: 해당 Day만 적용 (apply_all_days=True면 같은 도시 전체)
        if apply_all_days:
            to_city = (day_plan.to_city or '').strip()
            to_country = (day_plan.to_country or '').strip()
            dps = DayPlan.objects.filter(travel_plan=travel_plan, to_city=to_city, to_country=to_country)
        else:
            dps = [day_plan]

        for dp in dps:
            hp = dp.hotel or {}
            if not isinstance(hp, dict):
                hp = {}
            cands = list(hp.get('candidates') or [])

            # insert if missing
            exists = False
            for c in cands:
                if (c.get('place_id') or '').strip() == place_id:
                    exists = True
                    break
            if not exists:
                cands.insert(0, chosen)
            hp['candidates'] = cands[:10]
            hp['selected'] = chosen
            dp.hotel = hp
            dp.save(update_fields=['hotel'])

        return JsonResponse({'ok': True, 'selected_place_id': place_id})
    except Exception as e:
        return JsonResponse({'ok': False, 'error': str(e)}, status=500)


@require_POST
def select_hotel(request):
    """숙소 선택
    
    v5.1: Day별 개별 숙소 선택 가능 (같은 도시라도 Day마다 다른 숙소 선택)

    Payload (form or json):
      - plan_id: int
      - day_number: int
      - place_id: str (preferred)
      - index: int (fallback)
      - apply_all_days: bool (optional, default=False)
    """
    try:
        # Support both form-data and JSON
        if request.content_type and 'application/json' in request.content_type:
            payload = json.loads(request.body or '{}')
        else:
            payload = request.POST

        plan_id = payload.get('plan_id')
        day_number = int(payload.get('day_number') or 0)
        place_id = (payload.get('place_id') or payload.get('pid') or '').strip()
        # v5.1: apply_all_days 옵션 (기본: False = 해당 Day만 적용)
        apply_all_days = str(payload.get('apply_all_days', '')).lower() in ('true', '1', 'yes')
        idx_raw = payload.get('index')
        try:
            idx = int(idx_raw) if idx_raw is not None and str(idx_raw).strip() != '' else None
        except Exception:
            idx = None

        if not plan_id or day_number <= 0:
            return JsonResponse({'ok': False, 'error': 'invalid params'}, status=400)

        travel_plan = get_object_or_404(TravelPlan, id=plan_id)
        day_plan = get_object_or_404(DayPlan, travel_plan=travel_plan, day_number=day_number)

        hp = day_plan.hotel or {}
        candidates = (hp.get('candidates') or []) if isinstance(hp, dict) else []
        if not candidates:
            return JsonResponse({'ok': False, 'error': 'no hotel candidates'}, status=400)

        chosen = None
        if place_id:
            for c in candidates:
                pid = (c.get('place_id') or c.get('google_place_id') or c.get('pid') or '').strip()
                if pid and pid == place_id:
                    chosen = c
                    break
        if chosen is None and idx is not None and 0 <= idx < len(candidates):
            chosen = candidates[idx]
            place_id = (chosen.get('place_id') or chosen.get('google_place_id') or '').strip() or place_id

        if chosen is None:
            return JsonResponse({'ok': False, 'error': 'hotel not found'}, status=400)

        # v5.1: 해당 Day만 적용 (apply_all_days=True면 같은 도시 전체)
        if apply_all_days:
            to_city = (day_plan.to_city or '').strip()
            to_country = (day_plan.to_country or '').strip()
            dps = DayPlan.objects.filter(travel_plan=travel_plan, to_city=to_city, to_country=to_country)
        else:
            dps = [day_plan]
            
        for dp in dps:
            hp2 = dp.hotel or {}
            if not isinstance(hp2, dict):
                hp2 = {}
            # keep per-day candidates (if present); fallback to current day candidates
            hp2['candidates'] = hp2.get('candidates') or candidates
            hp2['selected'] = chosen
            dp.hotel = hp2
            dp.save(update_fields=['hotel'])

        return JsonResponse({'ok': True, 'selected_place_id': place_id})

    except Exception as e:
        return JsonResponse({'ok': False, 'error': str(e)}, status=500)



@require_POST
def transit_route(request):
    """대중교통 경로(버스/지하철) 조회: Google Directions API(mode=transit)

    Body(JSON):
      {
        origin: {lat, lon},
        dest: {lat, lon},
        departure_time?: unix seconds,
        language?: 'ko'
      }

    Returns JSON:
      { ok, fallback_url, provider, duration_min, distance_km, steps:[...] }
    """
    try:
        payload = json.loads(request.body or "{}")
    except Exception:
        payload = {}

    try:
        o = payload.get("origin") or {}
        d = payload.get("dest") or {}
        olat = float(o.get("lat"))
        olon = float(o.get("lon"))
        dlat = float(d.get("lat"))
        dlon = float(d.get("lon"))
    except Exception:
        return JsonResponse({"ok": False, "error": "invalid params"}, status=400)

    fallback = build_transit_fallback_url((olat, olon), (dlat, dlon))

    # ✅ 키가 아예 없으면, 원인 메시지를 명확히 반환(프론트에서 안내)
    import os
    _key = (
        os.getenv("GOOGLE_MAPS_API_KEY", "")
        or os.getenv("GOOGLE_PLACES_API_KEY", "")
        or os.getenv("Google_Places_API_Key", "")
    ).strip()
    if not _key:
        return JsonResponse({"ok": False, "fallback_url": fallback, "error": "GOOGLE_MAPS_API_KEY missing"}, status=200)

    dep = payload.get("departure_time")
    try:
        dep = int(dep) if dep is not None and str(dep).strip() != "" else None
    except Exception:
        dep = None

    lang = (payload.get("language") or "ko").strip() or "ko"

    route = get_transit_route(
        (olat, olon),
        (dlat, dlon),
        departure_time=dep,
        language=lang,
        base_dir=str(settings.BASE_DIR),
    )

    if not route:
        return JsonResponse({"ok": False, "fallback_url": fallback, "error": "transit unavailable"}, status=200)

    out = {"ok": True, "fallback_url": fallback}
    out.update(route)
    return JsonResponse(out, status=200)


@csrf_exempt
def ai_chat(request):
    """AI 챗봇 엔드포인트 - RAG + LLM 기반 여행 질문 답변 (25개 도시 이상 확장)

    POST JSON (예):
      {
        "query": "도쿄 2박3일 일정 추천",
        "country": "jp",      # optional
        "city": "tokyo",      # optional
        "topic": "sights"     # optional (transport/food/cafe/sights/tips)
      }

    Returns:
      { "ok": true, "answer": "...", "sources": [...], "routed": {...} }
    """
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "POST only"}, status=405)

    try:
        body = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"ok": False, "error": "Invalid JSON"}, status=400)

    query = (body.get("query") or "").strip()
    country = (body.get("country") or "").strip() or None
    city = (body.get("city") or "").strip() or None
    topic = (body.get("topic") or "").strip() or None

    if not query:
        return JsonResponse({"ok": False, "error": "query required"}, status=400)

    try:
        from services.llm import ask_with_rag
        from django.conf import settings

        # ── 라우팅(옵션): country/city/topic이 비어있으면 query에서 추정 ──
        routed = {"country": country, "city": city, "topic": topic}
        try:
            from rag.routing import detect_city, detect_topic
            enable_city_routing = str(getattr(settings, "RAG_ENABLE_CITY_ROUTING", os.getenv("RAG_ENABLE_CITY_ROUTING", "1"))).lower() in ("1","true","yes")
            enable_topic_routing = str(getattr(settings, "RAG_ENABLE_TOPIC_ROUTING", os.getenv("RAG_ENABLE_TOPIC_ROUTING", "1"))).lower() in ("1","true","yes")

            if enable_city_routing and not city:
                rc, rcity = detect_city(query)
                if rcity:
                    city = city or rcity
                    country = country or rc
            if enable_topic_routing and not topic:
                rtopic = detect_topic(query)
                if rtopic:
                    topic = topic or rtopic
        except Exception:
            pass

        routed = {"country": country, "city": city, "topic": topic}

        model = getattr(settings, "LLM_MODEL", "gpt-4o-mini")

        # retrieve/final 튜닝값(.env)
        retrieve_k = int(getattr(settings, "RAG_RETRIEVE_K", os.getenv("RAG_RETRIEVE_K", "40")))
        final_k = int(getattr(settings, "RAG_FINAL_K", os.getenv("RAG_FINAL_K", "8")))

        result = ask_with_rag(
            query=query,
            country=country,
            city=city,
            topic=topic,
            model=model,
            retrieve_k=retrieve_k,
            final_k=final_k,
            return_sources=True,
            routed=routed,
        )

        answer = (result or {}).get("answer") if isinstance(result, dict) else result

        if not answer:
            return JsonResponse({"ok": False, "error": "LLM returned empty response"}, status=500)

        if isinstance(result, dict):
            return JsonResponse({
                "ok": True,
                "answer": result.get("answer", ""),
                "sources": result.get("sources", []),
                "routed": result.get("routed", routed),
            })

        return JsonResponse({"ok": True, "answer": answer, "routed": routed})

    except ImportError:
        return JsonResponse({"ok": False, "error": "LLM service not available. Install: pip install openai faiss-cpu"}, status=501)
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"ai_chat error: {e}")
        return JsonResponse({"ok": False, "error": str(e)}, status=500)

