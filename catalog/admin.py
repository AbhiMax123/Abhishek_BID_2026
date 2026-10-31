from django.contrib import admin

from .models import Category, ImportError, ImportJob, Product

admin.site.register(Category)
admin.site.register(Product)
admin.site.register(ImportJob)
admin.site.register(ImportError)
