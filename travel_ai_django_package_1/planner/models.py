"""
여행 계획 모델
"""
from django.db import models
from django.conf import settings
import json


class TravelPlan(models.Model):
    """여행 계획"""
    
    STATUS_CHOICES = [
        ('draft', '작성 중'),
        ('planned', '계획 완료'),
        ('ongoing', '여행 중'),
        ('completed', '완료'),
        ('cancelled', '취소'),
    ]
    
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='travel_plans',
        verbose_name='사용자',
        null=True,  # 비회원도 생성 가능
        blank=True
    )
    
    # 세션 ID (비회원용)
    session_key = models.CharField('세션 키', max_length=100, blank=True)
    
    # 기본 정보
    title = models.CharField('제목', max_length=200, blank=True)
    start_date = models.DateField('출발일')
    end_date = models.DateField('도착일')
    total_days = models.PositiveIntegerField('총 일수', default=1)
    
    # 출발지
    origin_city = models.CharField('출발 도시', max_length=100)
    origin_country = models.CharField('출발 국가', max_length=100, blank=True)
    origin_country_code = models.CharField('출발 국가 코드', max_length=10, blank=True)
    
    # 목적지들 (JSON)
    destinations = models.JSONField('목적지 목록', default=list)
    # 예: [{"city": "Paris", "country": "France", "days": 3}, ...]
    
    # 여행 테마
    theme = models.CharField('여행 테마', max_length=100, blank=True)
    
    # 동행 정보
    travelers_count = models.PositiveSmallIntegerField('여행자 수', default=1)
    has_children = models.BooleanField('아이 동반', default=False)
    has_elderly = models.BooleanField('노약자 동반', default=False)
    
    # 상태
    status = models.CharField(
        '상태',
        max_length=20,
        choices=STATUS_CHOICES,
        default='draft'
    )
    
    # 메타데이터
    created_at = models.DateTimeField('생성일', auto_now_add=True)
    updated_at = models.DateTimeField('수정일', auto_now=True)
    
    # 전체 계획 JSON (백업/빠른 로딩용)
    plan_data = models.JSONField('전체 계획 데이터', default=dict, blank=True)
    
    class Meta:
        verbose_name = '여행 계획'
        verbose_name_plural = '여행 계획들'
        ordering = ['-created_at']
    
    def __str__(self):
        cities = ', '.join([d.get('city', '') for d in self.destinations[:3]])
        return f"{self.title or cities} ({self.start_date})"
    
    def get_destination_cities(self):
        """목적지 도시 목록 반환"""
        return [d.get('city') for d in self.destinations if d.get('city')]


class DayPlan(models.Model):
    """일자별 계획"""
    
    travel_plan = models.ForeignKey(
        TravelPlan,
        on_delete=models.CASCADE,
        related_name='day_plans',
        verbose_name='여행 계획'
    )
    
    day_number = models.PositiveSmallIntegerField('일차')
    date = models.DateField('날짜')
    weekday = models.CharField('요일', max_length=10, blank=True)
    
    # 이동 정보
    from_city = models.CharField('출발 도시', max_length=100, blank=True)
    to_city = models.CharField('도착 도시', max_length=100)
    to_country = models.CharField('도착 국가', max_length=100, blank=True)
    to_country_code = models.CharField('국가 코드', max_length=10, blank=True)
    
    # 좌표
    latitude = models.FloatField('위도', null=True, blank=True)
    longitude = models.FloatField('경도', null=True, blank=True)
    
    # 이동 수단
    transport_from_prev = models.CharField('이전 도시에서 이동수단', max_length=50, blank=True)
    transport_duration = models.CharField('이동 시간', max_length=50, blank=True)
    
    # 숙소 정보 (JSON)
    hotel = models.JSONField('숙소 정보', default=dict, blank=True)
    
    # 루트 정보
    route = models.JSONField('루트 정보', default=list, blank=True)
    route_line = models.JSONField('루트 라인', default=list, blank=True)
    route_map_url = models.URLField('루트 맵 URL', max_length=2000, blank=True)
    total_move_time = models.CharField('총 이동 시간', max_length=50, blank=True)
    
    # 히어로 이미지
    hero_image_url = models.URLField('히어로 이미지', max_length=500, blank=True)
    
    # 팁
    tips = models.JSONField('여행 팁', default=list, blank=True)
    
    # POI 후보 (JSON)
    poi_candidates = models.JSONField('POI 후보', default=list, blank=True)
    
    class Meta:
        verbose_name = '일자별 계획'
        verbose_name_plural = '일자별 계획들'
        ordering = ['day_number']
        unique_together = ['travel_plan', 'day_number']
    
    def __str__(self):
        return f"Day {self.day_number}: {self.to_city}"


class POI(models.Model):
    """방문 장소 (Point of Interest)"""
    
    TYPE_CHOICES = [
        ('Attraction', '관광지'),
        ('Restaurant', '맛집'),
        ('Cafe', '카페'),
    ]
    
    day_plan = models.ForeignKey(
        DayPlan,
        on_delete=models.CASCADE,
        related_name='pois',
        verbose_name='일자별 계획'
    )
    
    order = models.PositiveSmallIntegerField('순서', default=0)
    
    # 기본 정보
    name = models.CharField('장소명', max_length=200)
    raw_name = models.CharField('원본명', max_length=200, blank=True)
    poi_type = models.CharField('타입', max_length=20, choices=TYPE_CHOICES)
    feature = models.CharField('카테고리', max_length=50, blank=True)
    
    # Google Places 정보
    google_place_id = models.CharField('Place ID', max_length=100, blank=True)
    
    # 위치
    latitude = models.FloatField('위도', null=True, blank=True)
    longitude = models.FloatField('경도', null=True, blank=True)
    distance_km = models.FloatField('중심부 거리(km)', null=True, blank=True)
    
    # 평점
    rating = models.FloatField('평점', null=True, blank=True)
    review_count = models.PositiveIntegerField('리뷰 수', null=True, blank=True)
    
    # 특별 표시
    is_landmark = models.BooleanField('랜드마크', default=False)
    michelin_grade = models.CharField('미슐랭 등급', max_length=20, blank=True)
    
    # 이미지
    image_url = models.URLField('이미지 URL', max_length=500, blank=True)
    image_hd_url = models.URLField('HD 이미지 URL', max_length=500, blank=True)
    
    # 추천 이유
    reason = models.TextField('추천 이유', blank=True)
    
    # 사용자 피드백 (학습용)
    user_selected = models.BooleanField('사용자 선택', default=True)
    user_rating = models.PositiveSmallIntegerField('사용자 평점', null=True, blank=True)
    user_liked = models.BooleanField('사용자 좋아요', null=True, blank=True)
    
    class Meta:
        verbose_name = 'POI'
        verbose_name_plural = 'POIs'
        ordering = ['order']
    
    def __str__(self):
        return f"{self.name} ({self.get_poi_type_display()})"


class UserPOIInteraction(models.Model):
    """사용자-POI 상호작용 기록 (추천 학습용)"""
    
    ACTION_CHOICES = [
        ('view', '조회'),
        ('add', '추가'),
        ('remove', '삭제'),
        ('reorder', '순서 변경'),
        ('like', '좋아요'),
        ('dislike', '싫어요'),
    ]
    
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='poi_interactions',
        verbose_name='사용자'
    )
    
    # POI 정보
    poi_name = models.CharField('장소명', max_length=200)
    poi_type = models.CharField('타입', max_length=20)
    poi_feature = models.CharField('카테고리', max_length=50, blank=True)
    google_place_id = models.CharField('Place ID', max_length=100, blank=True)
    
    # 위치 컨텍스트
    city = models.CharField('도시', max_length=100)
    country = models.CharField('국가', max_length=100)
    
    # 행동
    action = models.CharField('행동', max_length=20, choices=ACTION_CHOICES)
    
    # 메타데이터
    is_landmark = models.BooleanField('랜드마크 여부', default=False)
    is_michelin = models.BooleanField('미슐랭 여부', default=False)
    
    created_at = models.DateTimeField('기록일', auto_now_add=True)
    
    class Meta:
        verbose_name = 'POI 상호작용'
        verbose_name_plural = 'POI 상호작용들'
        ordering = ['-created_at']
    
    def __str__(self):
        return f"{self.user.username} - {self.action} - {self.poi_name}"


class RecommendationFeedback(models.Model):
    """추천 결과에 대한 피드백 (모델 학습용)"""
    
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='recommendation_feedbacks',
        verbose_name='사용자',
        null=True,
        blank=True
    )
    
    travel_plan = models.ForeignKey(
        TravelPlan,
        on_delete=models.CASCADE,
        related_name='feedbacks',
        verbose_name='여행 계획',
        null=True,
        blank=True
    )
    
    # 피드백 대상
    feedback_type = models.CharField('피드백 타입', max_length=50)
    # 'overall', 'hotel', 'poi', 'route', 'tip'
    
    target_name = models.CharField('대상명', max_length=200, blank=True)
    
    # 평가
    rating = models.PositiveSmallIntegerField(
        '평점',
        null=True,
        blank=True
    )
    
    helpful = models.BooleanField('도움됨', null=True, blank=True)
    
    # 상세 피드백
    comment = models.TextField('코멘트', blank=True)
    
    # 컨텍스트
    city = models.CharField('도시', max_length=100, blank=True)
    country = models.CharField('국가', max_length=100, blank=True)
    
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        verbose_name = '추천 피드백'
        verbose_name_plural = '추천 피드백들'
    
    def __str__(self):
        return f"{self.feedback_type} - {self.rating or 'N/A'}"
