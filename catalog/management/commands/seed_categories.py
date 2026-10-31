from django.core.management.base import BaseCommand

from catalog.models import Category

DEFAULT_CATEGORIES = ["Electronics", "Books", "Clothing", "Home", "Toys"]


class Command(BaseCommand):
    help = "Create the default categories used by samples/sample_products.csv (safe to re-run)."

    def handle(self, *args, **options):
        for name in DEFAULT_CATEGORIES:
            _, created = Category.objects.get_or_create(name=name)
            self.stdout.write(("created " if created else "exists  ") + name)
