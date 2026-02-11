"""
계정 관련 뷰
"""
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import login, logout, get_user_model
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView, LogoutView, PasswordChangeView
from django.contrib import messages
from django.urls import reverse_lazy
from django.views.generic import CreateView, UpdateView, DetailView

from .forms import SignUpForm, LoginForm, UserProfileForm, TravelPreferenceForm
from .models import TravelPreference, VisitedPlace, FavoriteCity

User = get_user_model()


class SignUpView(CreateView):
    """회원가입 뷰"""
    model = User
    form_class = SignUpForm
    template_name = 'accounts/signup.html'
    success_url = reverse_lazy('planner:index')
    
    def form_valid(self, form):
        response = super().form_valid(form)
        # 자동 로그인
        login(self.request, self.object)
        # 기본 여행 선호도 생성
        TravelPreference.objects.create(user=self.object)
        messages.success(self.request, f'환영합니다, {self.object.username}님! 회원가입이 완료되었습니다.')
        return response
    
    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            return redirect('planner:index')
        return super().dispatch(request, *args, **kwargs)


class CustomLoginView(LoginView):
    """로그인 뷰"""
    form_class = LoginForm
    template_name = 'accounts/login.html'
    redirect_authenticated_user = True
    
    def get_success_url(self):
        next_url = self.request.GET.get('next')
        if next_url:
            return next_url
        return reverse_lazy('planner:index')
    
    def form_valid(self, form):
        messages.success(self.request, f'환영합니다, {form.get_user().username}님!')
        return super().form_valid(form)


class CustomLogoutView(LogoutView):
    """로그아웃 뷰"""
    next_page = reverse_lazy('planner:index')
    
    def dispatch(self, request, *args, **kwargs):
        messages.info(request, '로그아웃되었습니다.')
        return super().dispatch(request, *args, **kwargs)


@login_required
def profile_view(request):
    """프로필 조회"""
    user = request.user
    travel_plans = user.travel_plans.all()[:5]
    visited_places = user.visited_places.all()[:10]
    favorite_cities = user.favorite_cities.all()[:5]
    
    # 여행 선호도 (없으면 생성)
    preference, _ = TravelPreference.objects.get_or_create(user=user)
    
    context = {
        'user': user,
        'travel_plans': travel_plans,
        'visited_places': visited_places,
        'favorite_cities': favorite_cities,
        'preference': preference,
    }
    return render(request, 'accounts/profile.html', context)


@login_required
def profile_edit_view(request):
    """프로필 수정"""
    if request.method == 'POST':
        form = UserProfileForm(request.POST, request.FILES, instance=request.user)
        if form.is_valid():
            form.save()
            messages.success(request, '프로필이 수정되었습니다.')
            return redirect('accounts:profile')
    else:
        form = UserProfileForm(instance=request.user)
    
    return render(request, 'accounts/profile_edit.html', {'form': form})


@login_required
def preference_edit_view(request):
    """여행 선호도 설정"""
    preference, _ = TravelPreference.objects.get_or_create(user=request.user)
    
    if request.method == 'POST':
        form = TravelPreferenceForm(request.POST, instance=preference)
        if form.is_valid():
            form.save()
            messages.success(request, '여행 선호도가 저장되었습니다.')
            return redirect('accounts:profile')
    else:
        form = TravelPreferenceForm(instance=preference)
    
    return render(request, 'accounts/preference_edit.html', {'form': form})


@login_required
def my_trips_view(request):
    """내 여행 목록"""
    travel_plans = request.user.travel_plans.all().order_by('-created_at')
    
    context = {
        'travel_plans': travel_plans,
    }
    return render(request, 'accounts/my_trips.html', context)


@login_required
def visited_places_view(request):
    """방문한 장소 목록"""
    places = request.user.visited_places.all()
    
    # 필터링
    place_type = request.GET.get('type')
    if place_type:
        places = places.filter(place_type=place_type)
    
    city = request.GET.get('city')
    if city:
        places = places.filter(city__icontains=city)
    
    liked_only = request.GET.get('liked')
    if liked_only:
        places = places.filter(liked=True)
    
    context = {
        'places': places,
        'place_type': place_type,
        'city': city,
        'liked_only': liked_only,
    }
    return render(request, 'accounts/visited_places.html', context)


@login_required
def add_visited_place_view(request):
    """방문 장소 추가"""
    if request.method == 'POST':
        place = VisitedPlace.objects.create(
            user=request.user,
            place_name=request.POST.get('place_name'),
            place_type=request.POST.get('place_type', 'Attraction'),
            city=request.POST.get('city'),
            country=request.POST.get('country'),
            place_id=request.POST.get('place_id', ''),
            user_rating=request.POST.get('user_rating') or None,
            liked=request.POST.get('liked') == 'true',
            notes=request.POST.get('notes', ''),
        )
        messages.success(request, f'{place.place_name}이(가) 추가되었습니다.')
        return redirect('accounts:visited_places')
    
    return render(request, 'accounts/add_visited_place.html')


@login_required
def delete_account_view(request):
    """계정 삭제"""
    if request.method == 'POST':
        user = request.user
        logout(request)
        user.delete()
        messages.success(request, '계정이 삭제되었습니다.')
        return redirect('planner:index')
    
    return render(request, 'accounts/delete_account.html')
