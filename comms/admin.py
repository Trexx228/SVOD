from django.contrib import admin

from .models import Attachment, Message, Notification, Thread


class ReadOnlyAdmin(admin.ModelAdmin):
    list_per_page = 50

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Notification)
class NotificationAdmin(ReadOnlyAdmin):
    list_display = ('created_at', 'recipient', 'kind', 'text_short', 'read')
    list_filter = ('kind', 'read', 'created_at')
    search_fields = ('recipient__full_name', 'text')
    date_hierarchy = 'created_at'
    list_select_related = ('recipient',)

    @admin.display(description='Текст')
    def text_short(self, obj):
        return (obj.text or '')[:80]


@admin.register(Thread)
class ThreadAdmin(ReadOnlyAdmin):
    list_display = ('id', 'title', 'task', 'direct', 'created_at')
    list_filter = ('direct',)
    search_fields = ('title', 'task__title')
    list_select_related = ('task',)
    filter_horizontal = ('participants',)


@admin.register(Message)
class MessageAdmin(ReadOnlyAdmin):
    list_display = ('created_at', 'thread', 'author', 'text_short')
    search_fields = ('text', 'author__full_name')
    date_hierarchy = 'created_at'
    list_select_related = ('thread', 'author')

    @admin.display(description='Текст')
    def text_short(self, obj):
        return (obj.text or '')[:80]


@admin.register(Attachment)
class AttachmentAdmin(ReadOnlyAdmin):
    list_display = ('created_at', 'name', 'owner', 'size', 'image')
    list_filter = ('image',)
    search_fields = ('name', 'owner__full_name')
    date_hierarchy = 'created_at'
    list_select_related = ('owner',) #from django.contrib import admin

# Register your models here.
