from django.urls import include, path
from rest_framework.routers import SimpleRouter
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from . import views

router = SimpleRouter()
router.register("categories", views.CategoryViewSet, basename="category")
router.register("products", views.ProductViewSet, basename="product")
router.register("imports", views.ImportJobViewSet, basename="import")

urlpatterns = [
    path("auth/register/", views.RegisterView.as_view(), name="auth-register"),
    path("auth/token/", TokenObtainPairView.as_view(), name="token-obtain"),
    path("auth/token/refresh/", TokenRefreshView.as_view(), name="token-refresh"),
    # These two MUST come before the router so "import"/"export" aren't treated as a product id.
    path("products/import/", views.ProductImportView.as_view(), name="product-import"),
    path("products/export/", views.ProductExportView.as_view(), name="product-export"),
    path("", include(router.urls)),
]
