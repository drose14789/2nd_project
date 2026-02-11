"""
여행 팁 생성 서비스
- RAG 기반 + 기본 팁
"""
from typing import List, Tuple, Dict, Any

# 도시별 기본 여행 팁
CITY_TIPS = {
    "tokyo": {
        "ko_name": "도쿄",
        "tips": [
            "스이카(Suica) 카드 구입 필수 - 편의점, 지하철 모두 사용",
            "지하철 막차는 대부분 자정경, 택시비가 비싸니 주의",
            "팁 문화가 없음 - 팁을 주면 오히려 실례될 수 있음",
            "대부분 현금 결제 - ATM은 편의점(세븐일레븐) 이용",
            "조용한 목소리로 대화하는 것이 매너",
        ],
        "foods": ["라멘", "스시", "돈까스", "우동", "타코야끼"],
    },
    "osaka": {
        "ko_name": "오사카",
        "tips": [
            "오사카 주유패스로 무료 입장 + 교통 이용 가능",
            "도톤보리는 저녁에 방문하면 네온이 화려함",
            "쿠시카츠 집에서 소스 2번 찍기 금지",
            "교토, 나라 당일치기 가능 (30-45분)",
        ],
        "foods": ["타코야끼", "오코노미야끼", "쿠시카츠", "551호라이만두"],
    },
    "paris": {
        "ko_name": "파리",
        "tips": [
            "뮤지엄 패스 구입 권장 (루브르, 오르세 무료입장)",
            "소매치기 주의 - 지하철과 관광지에서 각별히",
            "'Bonjour' 인사 필수 - 상점 입장 시 꼭 인사",
            "일요일은 많은 상점이 문을 닫음",
            "메트로 티켓 10장 묶음(카르네)이 저렴",
        ],
        "foods": ["크루아상", "에스카르고", "스테이크 프리트", "마카롱"],
    },
    "bangkok": {
        "ko_name": "방콕",
        "tips": [
            "그랩(Grab) 앱 필수 - 택시보다 안전하고 저렴",
            "사원 방문 시 긴 바지, 어깨 덮는 옷 필수",
            "왕실 관련 발언 주의 - 왕실 모독죄 엄격",
            "길거리 음식은 현지인 많은 곳이 안전",
            "흥정은 기본 - 시장에서 절반 가격부터 시작",
        ],
        "foods": ["팟타이", "똠얌꿍", "망고 스티키라이스", "그린커리"],
    },
    "seoul": {
        "ko_name": "서울",
        "tips": [
            "T-money 카드로 지하철, 버스, 편의점 결제",
            "대중교통이 발달되어 택시보다 지하철 추천",
            "무료 와이파이가 거의 모든 곳에서 가능",
            "24시간 영업하는 가게가 많음",
            "카카오맵이나 네이버지도 앱 설치 추천",
        ],
        "foods": ["삼겹살", "김치찌개", "비빔밥", "치킨", "떡볶이"],
    },
    "sydney": {
        "ko_name": "시드니",
        "tips": [
            "오팔(Opal) 카드로 모든 대중교통 이용",
            "자외선이 매우 강함 - 선크림, 선글라스 필수",
            "일요일 대중교통 $2.5 캡 적용",
            "본다이~쿠지 코스탈 워크 추천",
            "팁 문화 없음",
        ],
        "foods": ["피시앤칩스", "미트파이", "플랫화이트", "바라문디"],
    },
}

# 기본 여행 팁
DEFAULT_TIPS = [
    "현지 심카드 또는 포켓 와이파이 준비",
    "여행자 보험 가입 권장",
    "여권 사본 별도 보관",
    "현지 긴급 연락처 미리 저장",
    "환전은 공항보다 시내가 유리한 경우가 많음",
]


def get_city_key(city_name: str) -> str:
    """도시 이름을 키로 변환"""
    city_mapping = {
        "도쿄": "tokyo", "tokyo": "tokyo", "東京": "tokyo",
        "오사카": "osaka", "osaka": "osaka", "大阪": "osaka",
        "파리": "paris", "paris": "paris",
        "서울": "seoul", "seoul": "seoul",
        "방콕": "bangkok", "bangkok": "bangkok",
        "시드니": "sydney", "sydney": "sydney",
    }
    return city_mapping.get(city_name.lower(), "")


def get_city_tips(city_name: str) -> List[str]:
    """도시별 여행 팁 반환"""
    key = get_city_key(city_name)
    if key and key in CITY_TIPS:
        return CITY_TIPS[key].get("tips", DEFAULT_TIPS)[:5]
    return DEFAULT_TIPS[:3]


def get_city_foods(city_name: str) -> List[str]:
    """도시별 추천 음식"""
    key = get_city_key(city_name)
    if key and key in CITY_TIPS:
        return CITY_TIPS[key].get("foods", [])
    return []


def generate_tip(context: Dict[str, Any], rag_snippets: List[Tuple[str, str]] = None) -> str:
    """
    여행 팁 생성
    context: {"to_city": str, "global_day": int}
    rag_snippets: RAG 검색 결과 [(doc_id, text), ...]
    """
    city = context.get("to_city", "")
    tips = get_city_tips(city)
    
    # RAG 결과가 있으면 일부 포함
    if rag_snippets:
        for doc_id, text in rag_snippets[:1]:
            # 첫 100자만 추가
            snippet = text[:100].strip()
            if snippet:
                tips.insert(0, snippet)
    
    # 랜덤하게 1-2개 선택
    import random
    selected = random.sample(tips, min(2, len(tips)))
    return " | ".join(selected)


def generate_tips(city: str, country: str = "") -> List[str]:
    """도시별 여행 팁 리스트 반환 (Django 호환용)"""
    return get_city_tips(city)
