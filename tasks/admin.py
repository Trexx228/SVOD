from django.contrib import admin

from .models import (
    Order, ShopSession, Task, TaskLog, TaskProgress,
    TimeSession, WeekCommit, TaskBranch
)


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = ('number', 'product', 'ship_due', 'comment')
    search_fields = ('number', 'product')
    list_filter = ('ship_due',)
    list_per_page = 25


class ReadOnlyAdmin(admin.ModelAdmin):
    """Только просмотр: данные заполняются через UI приложения."""
    list_per_page = 50

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Task)
class TaskAdmin(ReadOnlyAdmin):
    list_display = (
        'id', 'title', 'executor', 'requester', 'status',
        'priority', 'branch', 'stage_order',
        'start_due', 'due', 'plan_hours', 'accumulated_hours',
    )
    list_filter = ('status', 'priority', 'kind', 'scale', 'branch')
    search_fields = ('title', 'executor__full_name', 'requester__full_name')
    date_hierarchy = 'created_at'
    list_select_related = ('executor', 'requester')

    def get_queryset(self, request):
        return super().get_queryset(request).select_related(
            'executor', 'requester', 'executor__department'
        )


@admin.register(TaskLog)
class TaskLogAdmin(ReadOnlyAdmin):
    list_display = ('task', 'kind', 'author', 'created_at', 'comment')
    list_filter = ('kind', 'created_at')
    search_fields = ('task__title', 'author__full_name', 'comment')
    date_hierarchy = 'created_at'
    list_select_related = ('task', 'author')


@admin.register(TaskProgress)
class TaskProgressAdmin(ReadOnlyAdmin):
    list_display = ('task', 'author', 'created_at', 'text_short')
    search_fields = ('task__title', 'author__full_name', 'text')
    date_hierarchy = 'created_at'
    list_select_related = ('task', 'author')

    @admin.display(description='Запись')
    def text_short(self, obj):
        return (obj.text or '')[:80]


@admin.register(TimeSession)
class TimeSessionAdmin(ReadOnlyAdmin):
    list_display = ('task', 'executor', 'started_at', 'finished_at', 'duration_hours')
    search_fields = ('task__title', 'executor__full_name')
    date_hierarchy = 'started_at'
    list_select_related = ('task', 'executor')


@admin.register(ShopSession)
class ShopSessionAdmin(ReadOnlyAdmin):
    list_display = ('task', 'executor', 'started_at', 'finished_at', 'duration_hours')
    search_fields = ('task__title', 'executor__full_name')
    date_hierarchy = 'started_at'
    list_select_related = ('task', 'executor')


@admin.register(WeekCommit)
class WeekCommitAdmin(ReadOnlyAdmin):
    list_display = ('user', 'task', 'week_start')
    list_filter = ('week_start',)
    search_fields = ('user__full_name', 'task__title')
    list_select_related = ('user', 'task')



@admin.register(TaskBranch)
class TaskBranchAdmin(admin.ModelAdmin):
    list_display = ('name', 'order', 'created_at')
    search_fields = ('name', 'order__number', 'order__product')
    list_select_related = ('order',)


from .models import OrderTemplate, OrderTemplateStage


class OrderTemplateStageInline(admin.StackedInline):
    model = OrderTemplateStage
    extra = 1
    fields = (
        'order', 'title', 'branch_name', 'task_type',
        'executor_role_code',
        'offset_anchor', 'offset_days', 'duration_days',
        'use_task_type_hours', 'plan_hours',
        'kind', 'priority',
    )
    ordering = ('order',)


@admin.register(OrderTemplate)
class OrderTemplateAdmin(admin.ModelAdmin):
    list_display = ('name', 'is_default', 'is_active', 'stages_count', 'created_at')
    list_filter = ('is_default', 'is_active')
    search_fields = ('name', 'description')
    inlines = (OrderTemplateStageInline,)

    @admin.display(description='Этапов')
    def stages_count(self, obj):
        return obj.stages.count()
