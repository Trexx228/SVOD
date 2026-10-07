from django.contrib import admin
from .models import Norm, TaskType, Holiday


@admin.register(TaskType)
class TaskTypeAdmin(admin.ModelAdmin):
    list_display = ('name', 'plan_hours', 'due_days', 'is_active')
    list_editable = ('plan_hours', 'due_days', 'is_active')
    search_fields = ('name',)
    list_filter = ('is_active',)
    ordering = ('name',)


@admin.register(Norm)
class NormAdmin(admin.ModelAdmin):
    list_display = ('hours_per_day', 'note')


@admin.register(Holiday)
class HolidayAdmin(admin.ModelAdmin):
    list_display = ('date', 'name', 'is_working')
    list_filter = ('is_working',)
    search_fields = ('name',)
    ordering = ('-date',)
    date_hierarchy = 'date'
