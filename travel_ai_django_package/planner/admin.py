from django.contrib import admin
from .models import TravelPlan, DayPlan, POI, UserPOIInteraction, RecommendationFeedback


@admin.register(TravelPlan)
class TravelPlanAdmin(admin.ModelAdmin):
    list_display = ('title', 'user', 'start_date', 'end_date', 'total_days', 'status', 'created_at')
    list_filter = ('status', 'created_at')
    search_fields = ('title', 'user__username')
    date_hierarchy = 'start_date'


@admin.register(DayPlan)
class DayPlanAdmin(admin.ModelAdmin):
    list_display = ('travel_plan', 'day_number', 'date', 'to_city', 'to_country')
    list_filter = ('to_country',)
    search_fields = ('to_city', 'travel_plan__title')


@admin.register(POI)
class POIAdmin(admin.ModelAdmin):
    list_display = ('name', 'poi_type', 'day_plan', 'rating', 'is_landmark', 'michelin_grade', 'user_selected')
    list_filter = ('poi_type', 'is_landmark', 'user_selected')
    search_fields = ('name', 'day_plan__to_city')


@admin.register(UserPOIInteraction)
class UserPOIInteractionAdmin(admin.ModelAdmin):
    list_display = ('user', 'poi_name', 'action', 'city', 'is_landmark', 'is_michelin', 'created_at')
    list_filter = ('action', 'is_landmark', 'is_michelin')
    search_fields = ('user__username', 'poi_name', 'city')
    date_hierarchy = 'created_at'


@admin.register(RecommendationFeedback)
class RecommendationFeedbackAdmin(admin.ModelAdmin):
    list_display = ('user', 'feedback_type', 'target_name', 'rating', 'helpful', 'created_at')
    list_filter = ('feedback_type', 'helpful')
    search_fields = ('user__username', 'target_name')
