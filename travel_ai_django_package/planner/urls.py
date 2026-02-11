"""
여행 플래너 URL 패턴
"""
from django.urls import path
from . import views

app_name = 'planner'

urlpatterns = [
    # 메인
    path('', views.index, name='index'),
    
    # 계획 생성
    path('generate/', views.generate_plan, name='generate'),
    
    # 결과
    path('plan/<int:plan_id>/', views.results, name='results'),
    
    # 일자별 상세
    path('plan/<int:plan_id>/day/<int:day_number>/', views.itinerary, name='itinerary'),
    
    # POI 순서 변경
    path('plan/<int:plan_id>/day/<int:day_number>/reorder/', views.reorder_pois, name='reorder_pois'),
    # LLM+RAG: Day explanation
    path('plan/<int:plan_id>/day/<int:day_number>/explain/', views.day_explain_api, name='day_explain'),

    # LLM+RAG: Day tips (for PDF/PPT + optional UI)
    path('plan/<int:plan_id>/day/<int:day_number>/tips/', views.day_tips_api, name='day_tips'),

    # AI Copilot chat (collapsed "AI 가이드" panel)
    path('plan/<int:plan_id>/day/<int:day_number>/copilot/', views.copilot_chat_api, name='copilot_chat'),

    
    # PDF 내보내기 (A: PDF 메인)
    path('plan/<int:plan_id>/export/pdf/', views.export_pdf, name='export_pdf'),

    # PPT 내보내기(옵션)
    path('plan/<int:plan_id>/export/ppt/', views.export_ppt, name='export_ppt'),

    # Tools: City hero downloader (no script)
    path('tools/city-hero/', views.city_hero_tool, name='city_hero_tool'),
    path('tools/city-hero/run/', views.city_hero_tool_run, name='city_hero_tool_run'),
    path("personalization/", views.personalization_dashboard, name="personalization_dashboard"),
]
