import os
import csv
import json
import itertools
import random
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from django.core.management.base import BaseCommand
from django.conf import settings

# Reuse exact app logic (rules + seeds + query)
from services.local_poi import (
    get_local_index,
    feature_to_typ,
    is_landmark,
    calculate_landmark_score,
    _load_landmark_seeds,
    _seed_is_landmark,
)

# Reuse exact route selection logic from the app (so the check matches reality)
from planner.views import _sort_by_preference, _select_default_pois


def _safe_float(x, default=None):
    try:
        if x is None or x == "":
            return default
        return float(x)
    except Exception:
        return default


def _safe_int(x, default=0) -> int:
    try:
        return int(float(x))
    except Exception:
        return int(default)


@dataclass
class TempPref:
    """Lightweight preference carrier for verification runs (no DB write)."""
    travel_style: str = "mixed"
    michelin_preference: bool = True


def _load_dataset_rows(dataset_path: str) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    with open(dataset_path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for r in reader:
            rows.append(r)
    return rows


def _city_centers(rows: List[Dict[str, Any]]) -> Dict[Tuple[str, str], Tuple[Optional[float], Optional[float]]]:
    """Compute a robust city center from the dataset (mean lat/lon of all items)."""
    acc = defaultdict(lambda: {"n": 0, "lat": 0.0, "lon": 0.0})
    for r in rows:
        country = (r.get("country") or "").strip()
        city = (r.get("city") or "").strip()
        if not country or not city:
            continue
        lat = _safe_float(r.get("google_lat") or r.get("lat"), None)
        lon = _safe_float(r.get("google_lon") or r.get("lon"), None)
        if lat is None or lon is None:
            continue
        k = (country, city)
        acc[k]["n"] += 1
        acc[k]["lat"] += float(lat)
        acc[k]["lon"] += float(lon)

    centers: Dict[Tuple[str, str], Tuple[Optional[float], Optional[float]]] = {}
    for k, d in acc.items():
        if d["n"] <= 0:
            centers[k] = (None, None)
        else:
            centers[k] = (d["lat"] / d["n"], d["lon"] / d["n"])
    return centers


def _parse_csv_num_list(s: str, cast=float) -> List:
    out = []
    if not s:
        return out
    for tok in (s.split(",") if isinstance(s, str) else list(s)):
        t = str(tok).strip()
        if not t:
            continue
        try:
            out.append(cast(t))
        except Exception:
            continue
    return out


def _default_sweep(base_rating: float, base_reviews: int) -> Tuple[List[float], List[int], List[float], List[int]]:
    # Rating: include baseline + stricter and looser points
    rating_levels = sorted(set([
        round(float(base_rating), 2),
        4.0, 4.2, 4.4, 4.6,
        round(float(base_rating) + 0.2, 2),
        round(max(0.0, float(base_rating) - 0.2), 2),
    ]))
    # Reviews: baseline + stricter points
    review_levels = sorted(set([
        int(base_reviews),
        0, 50, 200, 500, 1000,
        int(base_reviews * 2) if base_reviews > 0 else 100,
    ]))
    # Radius: include very tight to generous
    radius_levels = [3.0, 5.0, 8.0, 12.0, 20.0, 35.0, 45.0]
    # top_pool: tighter pools can collapse diversity
    top_pools = [60, 120, 200]
    return rating_levels, review_levels, radius_levels, top_pools


def _build_candidates_with_overrides(
    *,
    base_dir: str,
    country: str,
    city: str,
    center_lat: Optional[float],
    center_lon: Optional[float],
    pref: TempPref,
    learn: Optional[dict],
    include_cafe: bool,
    min_rating: float,
    min_reviews: int,
    radius_km: float,
    top_pool: int,
) -> List[Dict[str, Any]]:
    """Mimic planner.views._build_poi_candidates, but with explicit query overrides."""
    local_index = get_local_index(base_dir)
    if not local_index or not local_index.enabled():
        return []

    candidates: List[Dict[str, Any]] = []

    # Attractions
    attractions = local_index.query(
        country=country,
        city=city,
        typ="Attraction",
        center_lat=center_lat,
        center_lon=center_lon,
        limit=60,  # grab more, then re-cut like the view
        min_rating=min_rating,
        min_reviews=min_reviews,
        radius_km=radius_km,
        top_pool=top_pool,
    )
    landmarks = _sort_by_preference([a for a in attractions if a.get("is_landmark")], pref, learn)
    non_landmarks = _sort_by_preference([a for a in attractions if not a.get("is_landmark")], pref, learn)
    selected_attractions = landmarks[:4] + non_landmarks[:max(0, 10 - len(landmarks[:4]))]
    candidates.extend(selected_attractions[:10])

    # Restaurants
    restaurants = local_index.query(
        country=country,
        city=city,
        typ="Restaurant",
        center_lat=center_lat,
        center_lon=center_lon,
        limit=60,
        min_rating=min_rating,
        min_reviews=min_reviews,
        radius_km=radius_km,
        top_pool=top_pool,
    )
    michelin = _sort_by_preference([r for r in restaurants if r.get("michelin_grade")], pref, learn)
    non_michelin = _sort_by_preference([r for r in restaurants if not r.get("michelin_grade")], pref, learn)
    selected_restaurants = michelin[:4] + non_michelin[:max(0, 10 - len(michelin[:4]))]
    candidates.extend(selected_restaurants[:10])

    # Cafes
    if include_cafe:
        cafes = local_index.query(
            country=country,
            city=city,
            typ="Cafe",
            center_lat=center_lat,
            center_lon=center_lon,
            limit=20,
            min_rating=min_rating,
            min_reviews=min_reviews,
            radius_km=radius_km,
            top_pool=top_pool,
        )
        cafes = _sort_by_preference(cafes, pref, learn)
        candidates.extend(cafes[:8])

    return candidates


def _selected_two_landmarks(selected: List[Dict[str, Any]]) -> Tuple[bool, int, int]:
    """Return (ok, attractions_selected, landmarks_selected_in_first_two_attractions)."""
    atts = [x for x in selected if x.get("type") == "Attraction"]
    first2 = atts[:2]
    lm2 = sum(1 for x in first2 if bool(x.get("is_landmark")))
    return (len(first2) == 2 and lm2 == 2), len(atts), lm2


class Command(BaseCommand):
    help = (
        "Hard verification for the 'route includes 2 landmark Attractions' rule.\n"
        "- Verifies baseline condition under current env thresholds\n"
        "- Stress-sweeps thresholds/radius/top_pool/styles and simulates exclusions\n"
        "- Classifies failures into (SUPPLY) vs (LOGIC) to avoid false alarms\n"
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dataset",
            type=str,
            default="",
            help="Dataset CSV path (default: env LOCAL_POI_DATA_PATH or <BASE_DIR>/data/poi_dataset.csv)",
        )

        # Baseline thresholds
        parser.add_argument("--min-rating", type=float, default=None)
        parser.add_argument("--min-reviews", type=int, default=None)

        # Run modes
        parser.add_argument(
            "--mode",
            type=str,
            default="stress",
            choices=["basic", "stress"],
            help="basic: just baseline; stress: sweep params + exclusions + style matrix",
        )
        parser.add_argument("--only-warn", action="store_true", help="Print only problematic cases.")
        parser.add_argument("--strict", action="store_true", help="Exit with non-zero if any LOGIC failure happens.")
        parser.add_argument("--report-json", type=str, default="", help="Write a JSON report file (optional).")

        parser.add_argument(
            "--center-mode",
            type=str,
            default="both",
            choices=["app", "none", "geo", "both"],
            help="app: use the same city center as the running app (LocalPOIIndex.get_city_center); none: center=None (no radius filter); geo: dataset-mean center; both: run baseline for app+geo+none",
        )

        # Stress sweep overrides (comma-separated lists)
        parser.add_argument("--sweep-ratings", type=str, default="", help="e.g. 4.0,4.2,4.4")
        parser.add_argument("--sweep-reviews", type=str, default="", help="e.g. 0,50,200,500")
        parser.add_argument("--sweep-radii", type=str, default="", help="e.g. 3,5,10,20,35")
        parser.add_argument("--sweep-top-pools", type=str, default="", help="e.g. 60,120,200")
        parser.add_argument("--styles", type=str, default="", help="e.g. mixed,foodie,cultural,nature")

        # Exclusion simulation
        parser.add_argument(
            "--exclude-top-k",
            type=str,
            default="0,1,2,4",
            help="Simulate 'already seen' by removing top-K landmark attractions from candidates.",
        )
        parser.add_argument("--random-exclude", type=int, default=0, help="Randomly drop N attractions from candidates.")
        parser.add_argument("--random-runs", type=int, default=3, help="How many random runs per combo (default: 3).")

        # Seed support
        parser.add_argument("--write-seeds", action="store_true", help="Regenerate <BASE_DIR>/data/landmark_seeds.json")
        parser.add_argument("--top-n", type=int, default=25, help="When writing seeds, keep N per city (default: 25).")

    def handle(self, *args, **opts):
        base_dir = str(settings.BASE_DIR)

        dataset_path = (opts.get("dataset") or "").strip()
        if not dataset_path:
            dataset_path = (os.getenv("LOCAL_POI_DATA_PATH") or "").strip()
        if dataset_path and (not os.path.isabs(dataset_path)):
            dataset_path = os.path.join(base_dir, dataset_path)
        if not dataset_path:
            dataset_path = os.path.join(base_dir, "data", "poi_dataset.csv")

        if not os.path.exists(dataset_path):
            self.stderr.write(self.style.ERROR(f"Dataset not found: {dataset_path}"))
            return

        # Baseline thresholds (service conditions)
        min_rating = opts.get("min_rating")
        if min_rating is None:
            try:
                min_rating = float((os.getenv("LOCAL_POI_MIN_RATING") or "4.0").strip() or 4.0)
            except Exception:
                min_rating = 4.0

        min_reviews = opts.get("min_reviews")
        if min_reviews is None:
            try:
                min_reviews = int(float((os.getenv("LOCAL_POI_MIN_REVIEWS") or "50").strip() or 50))
            except Exception:
                min_reviews = 50

        mode = (opts.get("mode") or "stress").strip().lower()
        center_mode = (opts.get("center_mode") or "both").strip().lower()
        only_warn = bool(opts.get("only_warn"))
        strict = bool(opts.get("strict"))
        report_json = (opts.get("report_json") or "").strip()
        write_seeds = bool(opts.get("write_seeds"))
        top_n = int(opts.get("top_n") or 25)
        top_n = max(5, min(top_n, 120))

        # Load rows for centers + seed generation
        rows = _load_dataset_rows(dataset_path)
        centers = _city_centers(rows)

        # Optional: load seed map (for supply computation and suggestions)
        seed_map = _load_landmark_seeds(base_dir)

        # Prepare stress sweeps
        rating_levels, review_levels, radius_levels, top_pools = _default_sweep(float(min_rating), int(min_reviews))

        # Allow explicit override lists
        override_ratings = _parse_csv_num_list(opts.get("sweep_ratings") or "", float)
        if override_ratings:
            rating_levels = sorted(set(float(x) for x in override_ratings))

        override_reviews = _parse_csv_num_list(opts.get("sweep_reviews") or "", int)
        if override_reviews:
            review_levels = sorted(set(int(x) for x in override_reviews))

        override_radii = _parse_csv_num_list(opts.get("sweep_radii") or "", float)
        if override_radii:
            radius_levels = sorted(set(float(x) for x in override_radii))

        override_pools = _parse_csv_num_list(opts.get("sweep_top_pools") or "", int)
        if override_pools:
            top_pools = sorted(set(int(x) for x in override_pools))

        styles = [
            "relaxed", "active", "cultural", "foodie", "adventure", "shopping", "nature", "mixed"
        ]
        override_styles = [s.strip() for s in (opts.get("styles") or "").split(",") if s.strip()]
        if override_styles:
            styles = override_styles

        exclude_top_k_list = _parse_csv_num_list(opts.get("exclude_top_k") or "0,1,2,4", int)
        if not exclude_top_k_list:
            exclude_top_k_list = [0, 1, 2, 4]
        exclude_top_k_list = sorted(set(max(0, int(x)) for x in exclude_top_k_list))

        random_exclude = int(opts.get("random_exclude") or 0)
        random_runs = int(opts.get("random_runs") or 3)
        random_runs = max(1, min(random_runs, 25))

        # Ensure local index is enabled (verification uses it to match app behavior)
        local_index = get_local_index(base_dir)
        if not local_index or not local_index.enabled():
            self.stderr.write(self.style.ERROR("LocalPOIIndex is disabled. Set USE_LOCAL_POI_DATASET=1 in .env"))
            return

        # Report structures
        report = {
            "dataset": dataset_path,
            "baseline": {"min_rating": float(min_rating), "min_reviews": int(min_reviews)},
            "mode": mode,
            "summary": {},
            "cities": [],
        }

        logic_failures = 0
        supply_breakpoints = 0
        baseline_fails = 0

        # Group dataset for seed write candidates
        by_city_items = defaultdict(list)

        # Helper: compute landmark supply under a filter combo (rules+seeds)
        def _supply(country: str, city: str, rating_thr: float, reviews_thr: int, center_lat: Optional[float], center_lon: Optional[float], radius_km: float) -> Tuple[int, int]:
            # Use local_index.query to mirror app filtering (including seeds inside index)
            atts = local_index.query(
                country=country,
                city=city,
                typ="Attraction",
                center_lat=center_lat,
                center_lon=center_lon,
                limit=120,
                min_rating=rating_thr,
                min_reviews=reviews_thr,
                radius_km=radius_km,
                top_pool=200,
            )
            if not atts:
                return (0, 0)
            lm = sum(1 for a in atts if bool(a.get("is_landmark")))
            return (lm, len(atts))

        # Iterate cities from dataset rows (dedup)
        seen_city = set()
        for r in rows:
            country = (r.get("country") or "").strip()
            city = (r.get("city") or "").strip()
            if not country or not city:
                continue
            key = (country, city)
            if key in seen_city:
                continue
            seen_city.add(key)

            c_lat, c_lon = centers.get(key, (None, None))
            app_lat, app_lon = local_index.get_city_center(country=country, city=city)
            if center_mode == "none":
                s_lat, s_lon = (None, None)
            elif center_mode == "geo":
                s_lat, s_lon = (c_lat, c_lon)
            else:
                # app or both
                s_lat, s_lon = (app_lat, app_lon)
            city_entry = {
                "country": country,
                "city": city,
                "center": {"lat": c_lat, "lon": c_lon},
                "baseline": {},
                "stress": {"logic_failures": [], "supply_breakpoints": []},
            }

            # --- Baseline runs (service conditions) ---
            pref0 = TempPref(travel_style="mixed", michelin_preference=True)
            baselines: Dict[str, Dict[str, Any]] = {}

            def _run_baseline(name: str, center_lat: Optional[float], center_lon: Optional[float], radius_km: float) -> Dict[str, Any]:
                candidates0 = _build_candidates_with_overrides(
                    base_dir=base_dir,
                    country=country,
                    city=city,
                    center_lat=center_lat,
                    center_lon=center_lon,
                    pref=pref0,
                    learn=None,
                    include_cafe=True,
                    min_rating=float(min_rating),
                    min_reviews=int(min_reviews),
                    radius_km=float(radius_km),
                    top_pool=120,
                )
                selected0 = _select_default_pois(candidates0, pref=pref0, include_cafe=True, learn=None)
                ok0, att_sel_cnt0, lm20 = _selected_two_landmarks(selected0)

                out0: Dict[str, Any] = {
                    "ok": bool(ok0),
                    "attractions_selected": int(att_sel_cnt0),
                    "landmarks_in_first_two_attractions": int(lm20),
                    "min_rating": float(min_rating),
                    "min_reviews": int(min_reviews),
                    "radius_km": float(radius_km),
                    "top_pool": 120,
                    "center_lat": center_lat,
                    "center_lon": center_lon,
                }
                if not ok0:
                    lm_supply0, att_supply0 = _supply(country, city, float(min_rating), int(min_reviews), center_lat, center_lon, float(radius_km))
                    out0["supply_landmarks"] = int(lm_supply0)
                    out0["supply_attractions"] = int(att_supply0)
                    out0["failure_type"] = "LOGIC" if lm_supply0 >= 2 else "SUPPLY"
                return out0

            # none: center=None (radius does not filter)
            if center_mode in ("none", "both"):
                baselines["none"] = _run_baseline("none", None, None, float(os.getenv("LOCAL_POI_RADIUS_KM") or 45.0))

            # app: match current app behavior (dataset-derived center)
            if center_mode in ("app", "both"):
                baselines["app"] = _run_baseline("app", app_lat, app_lon, float(os.getenv("LOCAL_POI_RADIUS_KM") or 45.0))

            # geo: dataset-mean center (sanity check / comparison)
            if center_mode in ("geo", "both"):
                baselines["geo"] = _run_baseline("geo", c_lat, c_lon, float(os.getenv("LOCAL_POI_RADIUS_KM") or 45.0))

            city_entry["baseline"] = baselines

            # Count/print baselines
            any_bad = any((not b.get("ok")) for b in baselines.values())
            for bname, b in baselines.items():
                if not b.get("ok"):
                    baseline_fails += 1
                    if b.get("failure_type") == "LOGIC":
                        logic_failures += 1
                    else:
                        supply_breakpoints += 1

            if (not only_warn) or any_bad:
                for bname, b in baselines.items():
                    tag = "OK" if b.get("ok") else "WARN"
                    lm2 = int(b.get("landmarks_in_first_two_attractions") or 0)
                    att_sel_cnt = int(b.get("attractions_selected") or 0)
                    self.stdout.write(f"[BASE-{bname}:{tag}] {country}/{city}  first2_landmarks={lm2}/2  attractions_selected={att_sel_cnt}")

            # --- Stress sweep ---
            if mode == "stress":
                # Sweep combos, but keep it bounded
                combos = list(itertools.product(rating_levels, review_levels, radius_levels, top_pools, styles))
                # Hard cap to avoid accidental huge runs
                if len(combos) > 2500:
                    combos = combos[:2500]

                for (rt, rv, rad, pool, style) in combos:
                    pref = TempPref(travel_style=str(style), michelin_preference=True)

                    # supply check first (avoid counting supply shortage as logic error)
                    lm_supply, att_supply = _supply(country, city, float(rt), int(rv), s_lat, s_lon, float(rad))
                    if lm_supply < 2:
                        # record breakpoint (filters too tight) - not a logic failure
                        city_entry["stress"]["supply_breakpoints"].append({
                            "min_rating": float(rt),
                            "min_reviews": int(rv),
                            "radius_km": float(rad),
                            "top_pool": int(pool),
                            "style": str(style),
                            "supply_landmarks": int(lm_supply),
                            "supply_attractions": int(att_supply),
                        })
                        supply_breakpoints += 1
                        continue

                    # build candidates and run selection
                    candidates = _build_candidates_with_overrides(
                        base_dir=base_dir,
                        country=country,
                        city=city,
                        center_lat=s_lat,
                        center_lon=s_lon,
                        pref=pref,
                        learn=None,
                        include_cafe=True,
                        min_rating=float(rt),
                        min_reviews=int(rv),
                        radius_km=float(rad),
                        top_pool=int(pool),
                    )
                    selected = _select_default_pois(candidates, pref=pref, include_cafe=True, learn=None)
                    ok, att_sel_cnt, lm2 = _selected_two_landmarks(selected)
                    if not ok:
                        # LOGIC failure: supply had >=2, but selection didn't output 2 landmarks
                        city_entry["stress"]["logic_failures"].append({
                            "min_rating": float(rt),
                            "min_reviews": int(rv),
                            "radius_km": float(rad),
                            "top_pool": int(pool),
                            "style": str(style),
                            "supply_landmarks": int(lm_supply),
                            "supply_attractions": int(att_supply),
                            "landmarks_in_first_two_attractions": int(lm2),
                        })
                        logic_failures += 1
                        continue

                    # Exclusion simulation: remove top-K landmark attractions and re-run
                    atts = [c for c in candidates if c.get("type") == "Attraction"]
                    lms = [a for a in atts if a.get("is_landmark")]
                    if len(lms) >= 2:
                        # sort landmarks by (reviews, rating, score)
                        lms_sorted = sorted(lms, key=lambda x: (
                            int(x.get("reviews") or 0),
                            float(x.get("rating") or 0.0),
                            float(x.get("score") or 0.0),
                        ), reverse=True)

                        for k in exclude_top_k_list:
                            if k <= 0:
                                continue
                            if len(lms_sorted) - k < 2:
                                continue  # would violate supply, skip
                            drop = set()
                            for it in lms_sorted[:k]:
                                pid = (it.get("google_place_id") or it.get("place_id") or "").strip()
                                nm = (it.get("name") or "").strip()
                                drop.add(pid or nm)

                            cand2 = []
                            for c in candidates:
                                if c.get("type") != "Attraction":
                                    cand2.append(c)
                                    continue
                                pid = (c.get("google_place_id") or c.get("place_id") or "").strip()
                                nm = (c.get("name") or "").strip()
                                if (pid or nm) in drop:
                                    continue
                                cand2.append(c)

                            sel2 = _select_default_pois(cand2, pref=pref, include_cafe=True, learn=None)
                            ok2, _, lm2b = _selected_two_landmarks(sel2)
                            if not ok2:
                                city_entry["stress"]["logic_failures"].append({
                                    "min_rating": float(rt),
                                    "min_reviews": int(rv),
                                    "radius_km": float(rad),
                                    "top_pool": int(pool),
                                    "style": str(style),
                                    "scenario": f"exclude_top_{k}_landmarks",
                                    "supply_landmarks": int(lm_supply),
                                    "supply_attractions": int(att_supply),
                                    "landmarks_in_first_two_attractions": int(lm2b),
                                })
                                logic_failures += 1
                                break  # no need to test larger k if already failing

                    # Random exclusion simulation (optional)
                    if random_exclude > 0 and len(atts) >= (2 + random_exclude):
                        for rr in range(random_runs):
                            rng = (hash(f"{country}:{city}:{rt}:{rv}:{rad}:{pool}:{style}:{rr}") & 0xFFFFFFFF)
                            rnd = random.Random(rng)
                            atts_only = [c for c in candidates if c.get("type") == "Attraction"]
                            # don't drop restaurants/cafes; only stress attractions
                            drop = set()
                            for it in rnd.sample(atts_only, k=min(random_exclude, max(0, len(atts_only) - 2))):
                                pid = (it.get("google_place_id") or it.get("place_id") or "").strip()
                                nm = (it.get("name") or "").strip()
                                drop.add(pid or nm)
                            cand3 = []
                            for c in candidates:
                                if c.get("type") != "Attraction":
                                    cand3.append(c)
                                    continue
                                pid = (c.get("google_place_id") or c.get("place_id") or "").strip()
                                nm = (c.get("name") or "").strip()
                                if (pid or nm) in drop:
                                    continue
                                cand3.append(c)
                            sel3 = _select_default_pois(cand3, pref=pref, include_cafe=True, learn=None)
                            ok3, _, lm2c = _selected_two_landmarks(sel3)
                            if not ok3:
                                # supply after random exclude may drop below 2 landmarks; treat as breakpoint not logic
                                # but record it for visibility
                                city_entry["stress"]["supply_breakpoints"].append({
                                    "min_rating": float(rt),
                                    "min_reviews": int(rv),
                                    "radius_km": float(rad),
                                    "top_pool": int(pool),
                                    "style": str(style),
                                    "scenario": f"random_exclude_{random_exclude}_run_{rr}",
                                    "note": "post-exclusion supply may be <2",
                                    "landmarks_in_first_two_attractions": int(lm2c),
                                })
                                break

            report["cities"].append(city_entry)

        report["summary"] = {
            "cities_checked": len(seen_city),
            "baseline_fails": int(baseline_fails),
            "logic_failures": int(logic_failures),
            "supply_breakpoints": int(supply_breakpoints),
        }

        # Print summary
        self.stdout.write("\nSummary")
        self.stdout.write(f" - Cities checked: {len(seen_city)}")
        self.stdout.write(f" - Baseline fails: {baseline_fails}")
        self.stdout.write(f" - LOGIC failures: {logic_failures}")
        self.stdout.write(f" - Supply breakpoints (filters too tight): {supply_breakpoints}")

        # Optional JSON report
        if report_json:
            if not os.path.isabs(report_json):
                report_json = os.path.join(base_dir, report_json)
            os.makedirs(os.path.dirname(report_json), exist_ok=True)
            with open(report_json, "w", encoding="utf-8") as f:
                json.dump(report, f, ensure_ascii=False, indent=2)
            self.stdout.write(self.style.SUCCESS(f" - Wrote report: {report_json}"))

        # Optional: regenerate seeds (as before)
        if write_seeds:
            # Collect by city from dataset (Attraction only), prioritize existing landmark rules/seeds and popularity
            by_city = defaultdict(list)
            for r in rows:
                country = (r.get("country") or "").strip()
                city = (r.get("city") or "").strip()
                feature = (r.get("feature") or "").strip()
                name = (r.get("google_name") or r.get("name") or "").strip()
                place_id = (r.get("place_id") or "").strip()
                rating = _safe_float(r.get("google_rating"), 0.0)
                reviews = _safe_int(r.get("google_review_count"), 0)
                if not country or not city or not name:
                    continue
                if feature_to_typ(feature) != "Attraction":
                    continue
                if rating <= 0:
                    continue

                lm = bool(is_landmark(name, feature))
                if (not lm) and seed_map:
                    lm = bool(_seed_is_landmark(seed_map, country, city, place_id, name))
                sc = 0.0
                try:
                    sc = float(calculate_landmark_score(str(name), str(feature), float(rating), int(reviews)))
                except Exception:
                    sc = 0.0
                by_city[(country, city)].append({
                    "name": name,
                    "feature": feature,
                    "place_id": place_id,
                    "rating": float(rating),
                    "reviews": int(reviews),
                    "is_landmark": bool(lm),
                    "score": float(sc),
                })

            out_path = os.path.join(base_dir, "data", "landmark_seeds.json")
            payload = []
            for (country, city), items in by_city.items():
                items = list(items)
                items.sort(key=lambda x: (x["is_landmark"], x["score"], x["reviews"], x["rating"]), reverse=True)

                picked = []
                seen = set()
                for it in items:
                    pid = (it.get("place_id") or "").strip()
                    nm = (it.get("name") or "").strip()
                    key = pid or nm
                    if not key or key in seen:
                        continue
                    seen.add(key)

                    strong = (int(it.get("reviews", 0)) >= 1000 and float(it.get("rating", 0.0)) >= 4.3)
                    if bool(it.get("is_landmark")) or strong:
                        picked.append(it)
                    if len(picked) >= top_n:
                        break

                payload.append({
                    "country": country,
                    "city": city,
                    "place_ids": [p["place_id"] for p in picked if p.get("place_id")],
                    "names": [p["name"] for p in picked if p.get("name")],
                })

            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            self.stdout.write(self.style.SUCCESS(f"\nWrote seeds: {out_path}"))

        if strict and logic_failures > 0:
            # Raising CommandError yields non-zero exit and surfaces in CI.
            raise SystemExit(1)
