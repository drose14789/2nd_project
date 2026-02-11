"""
사용자 계정 및 여행 선호도 모델
"""
from django.db import models
from django.contrib.auth.models import AbstractUser
from django.core.validators import MinValueValidator, MaxValueValidator


class User(AbstractUser):
    """커스텀 사용자 모델"""
    email = models.EmailField('이메일', unique=True)
    profile_image = models.ImageField('프로필 이미지', upload_to='profiles/', blank=True, null=True)
    created_at = models.DateTimeField('가입일', auto_now_add=True)
    updated_at = models.DateTimeField('수정일', auto_now=True)
    
    # 여행 통계
    total_trips = models.PositiveIntegerField('총 여행 수', default=0)
    total_countries = models.PositiveIntegerField('방문 국가 수', default=0)
    total_cities = models.PositiveIntegerField('방문 도시 수', default=0)
    
    class Meta:
        verbose_name = '사용자'
        verbose_name_plural = '사용자들'
    
    def __str__(self):
        return self.username


class TravelPreference(models.Model):
    """사용자별 여행 선호도 (개인화 학습용)"""
    
    TRAVEL_STYLE_CHOICES = [
        ('relaxed', '여유로운 휴식'),
        ('active', '활동적인 탐험'),
        ('cultural', '문화/역사 탐방'),
        ('foodie', '미식 여행'),
        ('adventure', '모험/액티비티'),
        ('shopping', '쇼핑 중심'),
        ('nature', '자연/힐링'),
        ('mixed', '골고루'),
    ]
    
    BUDGET_CHOICES = [
        ('budget', '가성비 (저예산)'),
        ('moderate', '적당한 예산'),
        ('comfort', '편안한 여행'),
        ('luxury', '럭셔리'),
    ]
    
    ACCOMMODATION_CHOICES = [
        ('hostel', '호스텔/게스트하우스'),
        ('hotel_budget', '저가 호텔'),
        ('hotel_mid', '중급 호텔'),
        ('hotel_luxury', '고급 호텔'),
        ('airbnb', '에어비앤비'),
        ('resort', '리조트'),
    ]
    
    user = models.OneToOneField(
        User, 
        on_delete=models.CASCADE, 
        related_name='travel_preference',
        verbose_name='사용자'
    )
    
    # 여행 스타일
    travel_style = models.CharField(
        '여행 스타일',
        max_length=20,
        choices=TRAVEL_STYLE_CHOICES,
        default='mixed'
    )
    
    # 예산
    budget_level = models.CharField(
        '예산 수준',
        max_length=20,
        choices=BUDGET_CHOICES,
        default='moderate'
    )
    
    # 숙소 선호
    accommodation_preference = models.CharField(
        '숙소 선호',
        max_length=20,
        choices=ACCOMMODATION_CHOICES,
        default='hotel_mid'
    )
    
    # 선호도 점수 (1-10)
    landmark_preference = models.PositiveSmallIntegerField(
        '랜드마크 선호도',
        default=8,
        validators=[MinValueValidator(1), MaxValueValidator(10)],
        help_text='유명 명소 방문 선호도'
    )
    
    local_experience_preference = models.PositiveSmallIntegerField(
        '로컬 체험 선호도',
        default=5,
        validators=[MinValueValidator(1), MaxValueValidator(10)],
        help_text='현지인처럼 여행하기 선호도'
    )
    
    food_preference = models.PositiveSmallIntegerField(
        '미식 선호도',
        default=7,
        validators=[MinValueValidator(1), MaxValueValidator(10)],
        help_text='맛집/미슐랭 탐방 선호도'
    )
    
    nature_preference = models.PositiveSmallIntegerField(
        '자연 선호도',
        default=5,
        validators=[MinValueValidator(1), MaxValueValidator(10)],
        help_text='자연/공원 방문 선호도'
    )
    
    museum_preference = models.PositiveSmallIntegerField(
        '박물관/미술관 선호도',
        default=5,
        validators=[MinValueValidator(1), MaxValueValidator(10)]
    )
    
    shopping_preference = models.PositiveSmallIntegerField(
        '쇼핑 선호도',
        default=4,
        validators=[MinValueValidator(1), MaxValueValidator(10)]
    )
    
    nightlife_preference = models.PositiveSmallIntegerField(
        '나이트라이프 선호도',
        default=3,
        validators=[MinValueValidator(1), MaxValueValidator(10)]
    )
    
    # 하루 일정 선호
    daily_poi_count = models.PositiveSmallIntegerField(
        '하루 방문 장소 수',
        default=4,
        validators=[MinValueValidator(2), MaxValueValidator(8)],
        help_text='하루에 방문하고 싶은 장소 수'
    )
    
    walking_tolerance = models.PositiveSmallIntegerField(
        '도보 이동 선호도',
        default=6,
        validators=[MinValueValidator(1), MaxValueValidator(10)],
        help_text='많이 걷는 것에 대한 선호도'
    )
    
    early_bird = models.BooleanField(
        '아침형 여행자',
        default=False,
        help_text='이른 아침 일정 선호'
    )
    
    # 식사 선호
    michelin_preference = models.BooleanField(
        '미슐랭 레스토랑 선호',
        default=True
    )
    
    local_food_preference = models.BooleanField(
        '현지 음식 선호',
        default=True
    )
    
    vegetarian = models.BooleanField('채식주의자', default=False)
    halal_required = models.BooleanField('할랄 음식 필요', default=False)
    
    # 기타
    avoid_crowds = models.BooleanField(
        '붐비는 곳 피하기',
        default=False
    )
    
    accessibility_required = models.BooleanField(
        '접근성 필요',
        default=False,
        help_text='휠체어 접근 등'
    )
    
    # 메타데이터
    created_at = models.DateTimeField('생성일', auto_now_add=True)
    updated_at = models.DateTimeField('수정일', auto_now=True)
    
    class Meta:
        verbose_name = '여행 선호도'
        verbose_name_plural = '여행 선호도들'
    
    def __str__(self):
        return f"{self.user.username}의 여행 선호도"
    
    def to_dict(self):
        """학습/추천에 사용할 딕셔너리 반환"""
        return {
            'travel_style': self.travel_style,
            'budget_level': self.budget_level,
            'landmark_preference': self.landmark_preference,
            'local_experience_preference': self.local_experience_preference,
            'food_preference': self.food_preference,
            'nature_preference': self.nature_preference,
            'museum_preference': self.museum_preference,
            'shopping_preference': self.shopping_preference,
            'nightlife_preference': self.nightlife_preference,
            'daily_poi_count': self.daily_poi_count,
            'walking_tolerance': self.walking_tolerance,
            'michelin_preference': self.michelin_preference,
            'avoid_crowds': self.avoid_crowds,
        }


class VisitedPlace(models.Model):
    """사용자가 방문한 장소 기록 (학습용)"""
    
    RATING_CHOICES = [(i, str(i)) for i in range(1, 6)]
    
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='visited_places',
        verbose_name='사용자'
    )
    
    # 장소 정보
    place_name = models.CharField('장소명', max_length=200)
    place_id = models.CharField('Google Place ID', max_length=100, blank=True)
    place_type = models.CharField('장소 타입', max_length=50)  # Attraction, Restaurant, Cafe
    feature = models.CharField('카테고리', max_length=50, blank=True)  # 명소, 역사, 맛집 등
    
    # 위치
    city = models.CharField('도시', max_length=100)
    country = models.CharField('국가', max_length=100)
    latitude = models.FloatField('위도', null=True, blank=True)
    longitude = models.FloatField('경도', null=True, blank=True)
    
    # 사용자 평가
    user_rating = models.PositiveSmallIntegerField(
        '사용자 평점',
        choices=RATING_CHOICES,
        null=True,
        blank=True
    )
    
    liked = models.BooleanField('좋아요', default=True)
    would_revisit = models.BooleanField('재방문 의사', default=True)
    
    # 방문 정보
    visited_at = models.DateField('방문일', null=True, blank=True)
    visit_duration_minutes = models.PositiveIntegerField('체류 시간(분)', null=True, blank=True)
    
    # 메모
    notes = models.TextField('메모', blank=True)
    
    created_at = models.DateTimeField('기록일', auto_now_add=True)
    
    class Meta:
        verbose_name = '방문 장소'
        verbose_name_plural = '방문 장소들'
        ordering = ['-visited_at', '-created_at']
        unique_together = ['user', 'place_id']
    
    def __str__(self):
        return f"{self.user.username} - {self.place_name}"


class FavoriteCity(models.Model):
    """사용자가 좋아하는 도시"""
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='favorite_cities',
        verbose_name='사용자'
    )
    city = models.CharField('도시', max_length=100)
    country = models.CharField('국가', max_length=100)
    visit_count = models.PositiveIntegerField('방문 횟수', default=1)
    rating = models.PositiveSmallIntegerField(
        '평점',
        validators=[MinValueValidator(1), MaxValueValidator(5)],
        null=True,
        blank=True
    )
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        verbose_name = '관심 도시'
        verbose_name_plural = '관심 도시들'
        unique_together = ['user', 'city', 'country']
    
    def __str__(self):
        return f"{self.user.username} - {self.city}, {self.country}"
