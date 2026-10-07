"""Формы кабинета руководителя.

Сейчас: работа с графиками (WorkSchedule).
"""
import json

from django import forms
from django.core.validators import MinValueValidator, MaxValueValidator
from django.utils import timezone
from datetime import timedelta

from accounts.models import WorkSchedule


class WorkScheduleForm(forms.ModelForm):
    """Форма создания/редактирования графика.

    Паттерн приходит из UI как JSON-строка вида "[1,1,1,1,1,0,0]".
    Парсится и валидируется в clean_pattern_json; результат
    возвращается через cleaned_data['pattern_json'] (list[int]).
    """

    PATTERN_MIN = 2
    PATTERN_MAX = 31

    pattern_json = forms.CharField(
        label='Паттерн (JSON-строка 0/1)',
        widget=forms.HiddenInput(),
        required=True,
    )

    hours_per_shift = forms.FloatField(
        label='Часов в смене',
        min_value=0.1,
        max_value=24.0,
        initial=8.0,
        validators=[
            MinValueValidator(0.1, 'Часов в смене должно быть больше нуля.'),
            MaxValueValidator(24.0, 'В сутках 24 часа.'),
        ],
    )

    anchor_date = forms.DateField(
        label='Начало цикла',
        widget=forms.DateInput(attrs={'type': 'date'}),
        help_text=(
            'День, в который цикл начинался «с первого элемента паттерна». '
            'Для стандартной пятидневки — любой понедельник.'
        ),
    )

    class Meta:
        model = WorkSchedule
        fields = (
            'name', 'description', 'hours_per_shift',
            'anchor_date', 'is_active', 'use_calendar',
        )
        widgets = {
            'description': forms.TextInput(
                attrs={'placeholder': 'Например: пятидневка для офиса'},
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk and not self.is_bound:
            # Первичная подстановка паттерна в hidden-поле.
            self.fields['pattern_json'].initial = json.dumps(
                self.instance.pattern or [],
                )

    def clean_name(self):
        name = (self.cleaned_data.get('name') or '').strip()
        if not name:
            raise forms.ValidationError('Название обязательно.')
        qs = WorkSchedule.objects.filter(name__iexact=name)
        if self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise forms.ValidationError('График с таким названием уже есть.')
        return name

    def clean_pattern_json(self):
        raw = (self.cleaned_data.get('pattern_json') or '').strip()
        if not raw:
            raise forms.ValidationError('Паттерн пустой.')

        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            raise forms.ValidationError('Паттерн — не валидный JSON.')

        if not isinstance(parsed, list):
            raise forms.ValidationError('Паттерн должен быть массивом.')

        n = len(parsed)
        if n < self.PATTERN_MIN or n > self.PATTERN_MAX:
            raise forms.ValidationError(
                f'Длина паттерна: от {self.PATTERN_MIN} до {self.PATTERN_MAX} '
                f'элементов. Сейчас {n}.'
            )

        clean = []
        for i, x in enumerate(parsed):
            if x in (0, 1, False, True):
                clean.append(1 if x else 0)
            else:
                raise forms.ValidationError(
                    f'Элемент #{i + 1} = {x!r}: допустимы только 0 и 1.'
                )

        if all(x == 0 for x in clean):
            raise forms.ValidationError(
                'Паттерн не может быть весь из нулей — не будет ни одного '
                'рабочего дня.'
            )

        return clean

    def clean_anchor_date(self):
        d = self.cleaned_data.get('anchor_date')
        if d is None:
            raise forms.ValidationError('Укажите начало цикла.')

        today = timezone.localdate()
        if d > today + timedelta(days=366):
            raise forms.ValidationError(
                'Начало цикла — не более года в будущем.'
            )
        if d < today - timedelta(days=365 * 5):
            raise forms.ValidationError(
                'Начало цикла — не более 5 лет назад.'
            )
        return d

    def save(self, commit=True):
        obj = super().save(commit=False)
        obj.pattern = self.cleaned_data['pattern_json']
        if commit:
            obj.save()
        return obj
