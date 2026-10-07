from django.contrib import admin

from .models import Achievement, Kudos, Streak, UserAchievement


@admin.register(Achievement)
class AchievementAdmin(admin.ModelAdmin):
    list_display = ('code', 'title', 'description')
    search_fields = ('code', 'title')
    ordering = ('title',)


@admin.register(UserAchievement)
class UserAchievementAdmin(admin.ModelAdmin):
    list_display = ('user', 'achievement', 'awarded_at')
    list_filter = ('achievement',)
    search_fields = ('user__full_name', 'user__email', 'achievement__title')
    ordering = ('-awarded_at',)
    raw_id_fields = ('user', 'achievement')
    list_select_related = ('user', 'achievement')


@admin.register(Kudos)
class KudosAdmin(admin.ModelAdmin):
    list_display = ('from_user', 'to_user', 'text_short', 'created_at')
    search_fields = (
        'from_user__full_name',
        'from_user__email',
        'to_user__full_name',
        'to_user__email',
        'text',
    )
    ordering = ('-created_at',)
    raw_id_fields = ('from_user', 'to_user')
    list_select_related = ('from_user', 'to_user')

    @admin.display(description='Текст')
    def text_short(self, obj):
        return obj.text[:80]


@admin.register(Streak)
class StreakAdmin(admin.ModelAdmin):
    list_display = ('user', 'current_days', 'best_days', 'last_ok_date')
    search_fields = ('user__full_name', 'user__email')
    ordering = ('user__full_name',)
    raw_id_fields = ('user',)
    list_select_related = ('user',)
