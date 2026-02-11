"""
API URL 패턴
"""
from django.urls import path
from . import views

app_name = 'api'

urlpatterns = [
    # POI 관리
    path('add_poi/', views.add_poi, name='add_poi'),
    path('remove_poi/', views.remove_poi, name='remove_poi'),
    path('like_poi/', views.like_poi, name='like_poi'),
    
    # 방문 기록
    path('record_visit/', views.record_visit, name='record_visit'),
    
    # 검색
    path('search_pois/', views.search_pois, name='search_pois'),
    
    # 통계
    path('user_stats/', views.get_user_stats, name='user_stats'),
    
    # 이미지 프록시
    path('place_photo/', views.place_photo, name='place_photo'),

    # AI 챗봇
    path('ai/chat/', views.ai_chat, name='ai_chat'),

    # 루트 요약(구간 거리/시간)
    path('route_summary/', views.route_summary, name='route_summary'),
    path('transit_route/', views.transit_route, name='transit_route'),

    # 숙소 검색/선택
    path('hotel_search/', views.hotel_search, name='hotel_search'),
    path('select_hotel_custom/', views.select_hotel_custom, name='select_hotel_custom'),

    # 숙소 선택
    path('select_hotel/', views.select_hotel, name='select_hotel'),
]
