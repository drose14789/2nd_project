[Fix32] 도시 히어로(cover) 이미지 안정화

무슨 문제가 있었나?
- static/img/city_hero/*.jpg에 '거품(placeholder)' 이미지가 포함돼 있었고,
  코드에서 파일 존재만으로 '진짜'로 판단해 다운로드를 안 했습니다.
- 또한 CITY_HERO_OVERRIDES 일부 파일명이 실제 Wikimedia Commons 파일명과 달라 404가 나면서
  최종적으로 랜덤(카페/비/계란 등) 이미지로 fallback 되는 케이스가 있었습니다.

Fix32 내용
1) CITY_HERO_MIN_REAL_BYTES 를 120KB로 올려 placeholder(약 50~75KB)는 무시하고 자동 다운로드를 트리거
2) CITY_HERO_FILES 로 Wikimedia Commons 실제 파일명을 명시하고, quote()로 안전하게 Special:FilePath URL 생성
3) Day 카드 히어로는 (B안) 랜드마크 이미지가 없으면 무조건 도시 히어로(get_city_image) 사용

사용법
- 서버 실행 후 '플랜 요약 카드' 화면을 새로고침하면, 첫 요청 시 도시별 히어로가 자동 다운로드되어
  static/img/city_hero/*.jpg 로 저장됩니다.
- 만약 방화벽/네트워크 때문에 자동 다운로드가 막히면:
  python scripts/download_city_hero_images.py
  를 1회 실행하면 동일 폴더에 일괄 저장됩니다.
