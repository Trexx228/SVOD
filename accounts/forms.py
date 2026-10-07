import re

from django import forms
from django.contrib.auth.forms import BaseUserCreationForm

from .models import User

from django.conf import settings

_EMAIL_DOMAINS = getattr(
    settings, 'ALLOWED_EMAIL_DOMAINS', ['eag.su']
)
EMAIL_RE = re.compile(
    r'^[\w.+-]+@('
    + '|'.join(re.escape(d) for d in _EMAIL_DOMAINS)
    + r')$',
    re.IGNORECASE,
    )


class RegistrationForm(BaseUserCreationForm):
    full_name = forms.CharField(
        label='ФИО',
        max_length=150,
    )
    department_hint = forms.CharField(
        label='Подразделение',
        max_length=150,
        help_text='Например: КБ генераторов контейнерного типа',
    )

    class Meta:
        model = User
        fields = ('email', 'full_name', 'department_hint')

    def clean_email(self):
        email = (self.cleaned_data.get('email') or '').strip().lower()

        if not EMAIL_RE.match(email):
            raise forms.ValidationError(
                'Регистрация только с корпоративной почтой @eag.su'
            )

        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError(
                'Пользователь с таким email уже зарегистрирован'
            )

        return email
