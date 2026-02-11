from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import User, TravelPreference, VisitedPlace, FavoriteCity


@admin.register(User)
class CustomUserAdmin(UserAdmin):
    list_display = ('username', 'email', 'total_trips', 'total_countries', 'date_joined', 'is_active')
    list_filter = ('is_active', 'is_staff', 'date_joined')
    search_fields = ('username', 'email')
    ordering = ('-date_joined',)
    
    fieldsets = UserAdmin.fieldsets + (
        ('여행 통계', {
            'fields': ('total_trips', 'total_countries', 'total_cities', 'profile_image')
        }),
    )


@admin.register(TravelPreference)
class TravelPreferenceAdmin(admin.ModelAdmin):
    list_display = ('user', 'travel_style', 'budget_level', 'landmark_preference', 'food_preference', 'updated_at')
    list_filter = ('travel_style', 'budget_level', 'michelin_preference')
    search_fields = ('user__username', 'user__email')
    
    fieldsets = (
        ('사용자', {
            'fields': ('user',)
        }),
        ('기본 설정', {
            'fields': ('travel_style', 'budget_level', 'accommodation_preference')
        }),
        ('선호도 점수', {
            'fields': (
                'landmark_preference', 'local_experience_preference',
                'food_preference', 'nature_preference',
                'museum_preference', 'shopping_preference', 'nightlife_preference'
            )
        }),
        ('일정 선호', {
            'fields': ('daily_poi_count', 'walking_tolerance', 'early_bird')
        }),
        ('식사 선호', {
            'fields': ('michelin_preference', 'local_food_preference', 'vegetarian', 'halal_required')
        }),
        ('기타', {
            'fields': ('avoid_crowds', 'accessibility_required')
        }),
    )


@admin.register(VisitedPlace)
class VisitedPlaceAdmin(admin.ModelAdmin):
    list_display = ('user', 'place_name', 'place_type', 'city', 'country', 'user_rating', 'liked', 'visited_at')
    list_filter = ('place_type', 'liked', 'country')
    search_fields = ('user__username', 'place_name', 'city')
    date_hierarchy = 'visited_at'


@admin.register(FavoriteCity)
class FavoriteCityAdmin(admin.ModelAdmin):
    list_display = ('user', 'city', 'country', 'visit_count', 'rating')
    list_filter = ('country',)
    search_fields = ('user__username', 'city')
