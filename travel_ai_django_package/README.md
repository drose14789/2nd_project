# TravelAI Django

AI 기반 맞춤형 여행 플래너 - Django 버전

## 🚀 주요 기능

### 👤 사용자 인증
- 회원가입 / 로그인 / 로그아웃
- 비밀번호 재설정
- 프로필 관리

### 🎯 개인화 추천
- 여행 선호도 설정 (랜드마크, 미식, 자연, 박물관 등)
- 예산 및 숙소 선호 설정
- 사용자 행동 학습 (POI 추가/삭제/좋아요)
- ✅ 검색 시 "여행 스타일" 선택(밸런스/미식/문화/자연/쇼핑/느긋) → 추천에 반영
- ✅ 로그인 상태에서는 방문/좋아요 기록을 다음 검색에 "가벼운 편향"으로 반영(초기 v1)

### ✈️ 여행 계획
- AI 기반 일정 생성
- 랜드마크 우선 추천
- 미슐랭 레스토랑 추천
- POI 드래그앤드롭 정렬

### 📊 데이터 저장
- 여행 계획 저장
- 방문 장소 기록
- 관심 도시 저장
- 추천 피드백 수집

---


## 📍 도시 중심점(센터) 기반 반경 필터

- 추천은 로컬 데이터셋(POI CSV)에서 **도시 중심점(중앙값 기준)**을 계산해 `center_lat/lon`으로 사용합니다.  
- 따라서 `LOCAL_POI_RADIUS_KM` 설정이 **실제로 의미 있게 동작**합니다. (도시 외곽/엉뚱한 지역 outlier 억제)

`.env`에서 조절:
- `LOCAL_POI_RADIUS_KM=45` (기본)
- 서울/부산처럼 촘촘한 도시: 15~25 권장(취향에 맞춰 조절)

---


## 🛠️ 설치 방법


## ⚡ 원클릭 실행 (압축 풀고 바로 실행)

### Windows
- `run_windows.bat` 더블클릭  
  (가상환경 생성 → 패키지 설치 → 마이그레이션 → 서버 실행까지 자동)

### Mac / Linux
```bash
chmod +x run_mac_linux.sh
./run_mac_linux.sh
```

> 기본 실행 주소: http://127.0.0.1:8000

---


### 1. 가상환경 생성
```bash
python -m venv venv
source venv/bin/activate  # Linux/Mac
# venv\Scripts\activate  # Windows
```

### 2. 패키지 설치
```bash
pip install -r requirements.txt
```

### 3. 환경변수 설정
```bash
cp .env.example .env
# .env 파일을 열어 API 키 등을 입력
```

### 4. 데이터베이스 마이그레이션
```bash
python manage.py makemigrations
python manage.py migrate
```

### 5. 관리자 계정 생성
```bash
python manage.py createsuperuser
```

### 6. 서버 실행
```bash
python manage.py runserver
```

---

## 📁 프로젝트 구조

```
travel_ai_django/
├── manage.py
├── travel_ai/              # 프로젝트 설정
│   ├── settings.py
│   ├── urls.py
│   └── wsgi.py
├── accounts/               # 사용자 인증 앱
│   ├── models.py          # User, TravelPreference, VisitedPlace
│   ├── views.py           # 로그인, 회원가입, 프로필
│   ├── forms.py
│   └── urls.py
├── planner/               # 여행 플래너 앱
│   ├── models.py          # TravelPlan, DayPlan, POI
│   ├── views.py           # 계획 생성, 일정 조회
│   └── urls.py
├── api/                   # API 엔드포인트
│   ├── views.py           # POI 추가/삭제, 피드백
│   └── urls.py
├── services/              # 외부 서비스 연동
│   ├── local_poi.py       # 로컬 POI 데이터셋
│   ├── images.py          # 이미지 서비스
│   └── ...
├── templates/             # HTML 템플릿
├── static/                # CSS, JS, 이미지
└── data/                  # 데이터셋 (CSV, JSON)
```

---

## 🗄️ 데이터베이스 모델

### User (사용자)
- 기본 인증 정보 + 여행 통계

### TravelPreference (여행 선호도)
- 여행 스타일, 예산, 숙소 선호
- 랜드마크/미식/자연 등 카테고리별 선호도 점수 (1-10)
- 일정 선호 (하루 방문 장소 수, 도보 선호도)
- 식사 선호 (미슐랭, 채식, 할랄)

### TravelPlan (여행 계획)
- 출발지, 목적지, 날짜
- 여행자 수, 테마

### DayPlan (일자별 계획)
- 도시, 숙소, 루트
- POI 후보 목록

### POI (방문 장소)
- 장소 정보, 평점, 리뷰 수
- 랜드마크/미슐랭 여부
- 사용자 피드백

### UserPOIInteraction (사용자 상호작용)
- POI에 대한 행동 기록 (조회, 추가, 삭제, 좋아요)
- 추천 모델 학습에 활용

---

## 🧠 개인화 학습

사용자의 행동을 기반으로 추천을 개선합니다:

1. **POI 선택 패턴**
   - 랜드마크 vs 로컬 선호도
   - 미슐랭 vs 로컬 맛집 선호도

2. **방문 기록**
   - 좋아요한 장소의 카테고리 분석
   - 재방문 의사가 있는 장소 패턴

3. **선호도 설정**
   - 명시적 선호도 점수 활용
   - 행동 데이터와 결합

---

## 🔒 보안

- Django 내장 보안 기능 (CSRF, XSS 방지)
- 비밀번호 해싱 (PBKDF2)
- 세션 기반 인증
- 환경변수로 민감 정보 관리

---

## 📝 라이선스

MIT License

---

## 🙋 문의

버그 리포트나 기능 요청은 이슈로 등록해주세요.



## ✅ 랜드마크 2개 규칙 검증

```bash
python manage.py verify_landmarks --mode stress --center-mode both --report-json storage/reports/landmark_verify.json
```

- 실패를 `SUPPLY`(필터가 너무 빡세서 공급 부족) vs `LOGIC`(공급은 충분한데 선택 로직 문제)로 분리해 리포트합니다.

