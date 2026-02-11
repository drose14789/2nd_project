"""
계정 관련 폼
"""
from django import forms
from django.contrib.auth.forms import UserCreationForm, AuthenticationForm
from django.contrib.auth import get_user_model
from .models import TravelPreference

User = get_user_model()


class SignUpForm(UserCreationForm):
    """회원가입 폼"""
    email = forms.EmailField(
        label='이메일',
        required=True,
        widget=forms.EmailInput(attrs={
            'class': 'form-control',
            'placeholder': 'example@email.com'
        })
    )
    
    class Meta:
        model = User
        fields = ('username', 'email', 'password1', 'password2')
        labels = {
            'username': '아이디',
        }
        widgets = {
            'username': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': '아이디를 입력하세요'
            }),
        }
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['password1'].widget.attrs.update({
            'class': 'form-control',
            'placeholder': '비밀번호'
        })
        self.fields['password2'].widget.attrs.update({
            'class': 'form-control',
            'placeholder': '비밀번호 확인'
        })
        self.fields['password1'].label = '비밀번호'
        self.fields['password2'].label = '비밀번호 확인'


class LoginForm(AuthenticationForm):
    """로그인 폼"""
    username = forms.CharField(
        label='아이디',
        widget=forms.TextInput(attrs={
            'class': 'form-control',
            'placeholder': '아이디',
            'autofocus': True
        })
    )
    password = forms.CharField(
        label='비밀번호',
        widget=forms.PasswordInput(attrs={
            'class': 'form-control',
            'placeholder': '비밀번호'
        })
    )


class UserProfileForm(forms.ModelForm):
    """사용자 프로필 수정 폼"""
    class Meta:
        model = User
        fields = ('username', 'email', 'profile_image')
        labels = {
            'username': '아이디',
            'email': '이메일',
            'profile_image': '프로필 이미지',
        }
        widgets = {
            'username': forms.TextInput(attrs={'class': 'form-control'}),
            'email': forms.EmailInput(attrs={'class': 'form-control'}),
            'profile_image': forms.FileInput(attrs={'class': 'form-control'}),
        }


class TravelPreferenceForm(forms.ModelForm):
    """여행 선호도 설정 폼"""
    class Meta:
        model = TravelPreference
        exclude = ('user', 'created_at', 'updated_at')
        
        widgets = {
            'travel_style': forms.Select(attrs={'class': 'form-select'}),
            'budget_level': forms.Select(attrs={'class': 'form-select'}),
            'accommodation_preference': forms.Select(attrs={'class': 'form-select'}),
            'landmark_preference': forms.NumberInput(attrs={
                'class': 'form-range',
                'type': 'range',
                'min': '1',
                'max': '10'
            }),
            'local_experience_preference': forms.NumberInput(attrs={
                'class': 'form-range',
                'type': 'range',
                'min': '1',
                'max': '10'
            }),
            'food_preference': forms.NumberInput(attrs={
                'class': 'form-range',
                'type': 'range',
                'min': '1',
                'max': '10'
            }),
            'nature_preference': forms.NumberInput(attrs={
                'class': 'form-range',
                'type': 'range',
                'min': '1',
                'max': '10'
            }),
            'museum_preference': forms.NumberInput(attrs={
                'class': 'form-range',
                'type': 'range',
                'min': '1',
                'max': '10'
            }),
            'shopping_preference': forms.NumberInput(attrs={
                'class': 'form-range',
                'type': 'range',
                'min': '1',
                'max': '10'
            }),
            'nightlife_preference': forms.NumberInput(attrs={
                'class': 'form-range',
                'type': 'range',
                'min': '1',
                'max': '10'
            }),
            'daily_poi_count': forms.NumberInput(attrs={
                'class': 'form-control',
                'min': '2',
                'max': '8'
            }),
            'walking_tolerance': forms.NumberInput(attrs={
                'class': 'form-range',
                'type': 'range',
                'min': '1',
                'max': '10'
            }),
            'early_bird': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'michelin_preference': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'local_food_preference': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'vegetarian': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'halal_required': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'avoid_crowds': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'accessibility_required': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        }
