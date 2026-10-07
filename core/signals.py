from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver
from django.core.cache import cache
from .models import Holiday


@receiver(post_save, sender=Holiday)
@receiver(post_delete, sender=Holiday)
def clear_holiday_cache(sender, **kwargs):
    cache.delete('svod_holiday_map')
