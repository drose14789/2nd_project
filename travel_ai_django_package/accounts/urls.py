"""
계정 관련 URL 패턴
"""
from django.urls import path
from django.contrib.auth import views as auth_views
from . import views

app_name = 'accounts'

urlpatterns = [
    # 인증
    path('signup/', views.SignUpView.as_view(), name='signup'),
    path('login/', views.CustomLoginView.as_view(), name='login'),
    path('logout/', views.CustomLogoutView.as_view(), name='logout'),
    
    # 비밀번호
    path('password/change/', 
         auth_views.PasswordChangeView.as_view(
             template_name='accounts/password_change.html',
             success_url='/accounts/password/change/done/'
         ), 
         name='password_change'),
    path('password/change/done/', 
         auth_views.PasswordChangeDoneView.as_view(
             template_name='accounts/password_change_done.html'
         ), 
         name='password_change_done'),
    path('password/reset/', 
         auth_views.PasswordResetView.as_view(
             template_name='accounts/password_reset.html',
             email_template_name='accounts/password_reset_email.html',
             success_url='/accounts/password/reset/done/'
         ), 
         name='password_reset'),
    path('password/reset/done/', 
         auth_views.PasswordResetDoneView.as_view(
             template_name='accounts/password_reset_done.html'
         ), 
         name='password_reset_done'),
    path('password/reset/<uidb64>/<token>/', 
         auth_views.PasswordResetConfirmView.as_view(
             template_name='accounts/password_reset_confirm.html',
             success_url='/accounts/password/reset/complete/'
         ), 
         name='password_reset_confirm'),
    path('password/reset/complete/', 
         auth_views.PasswordResetCompleteView.as_view(
             template_name='accounts/password_reset_complete.html'
         ), 
         name='password_reset_complete'),
    
    # 프로필
    path('profile/', views.profile_view, name='profile'),
    path('profile/edit/', views.profile_edit_view, name='profile_edit'),
    path('preferences/', views.preference_edit_view, name='preferences'),
    
    # 내 여행
    path('trips/', views.my_trips_view, name='my_trips'),
    
    # 방문 장소
    path('visited/', views.visited_places_view, name='visited_places'),
    path('visited/add/', views.add_visited_place_view, name='add_visited_place'),
    
    # 계정 삭제
    path('delete/', views.delete_account_view, name='delete_account'),
]
