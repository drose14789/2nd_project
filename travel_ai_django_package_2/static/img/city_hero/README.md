# City Hero Images (대표 도시 이미지)

이 폴더는 **결과 미리보기**와 **PDF 내보내기**에서 사용할 도시 대표 이미지를 저장합니다.

✅ 한 번만 실행:
```bash
python scripts/download_city_hero_images.py
```

- 실행하면 각 도시(24개)의 랜드마크 중심 대표 이미지를 자동으로 다운로드합니다.
- 파일명은 `services/images.py`의 `CITY_HERO_SLUGS`와 1:1 매칭됩니다.
- 다운로드 출처/페이지 정보는 `static/img/city_hero/credits.json`에 저장됩니다.
