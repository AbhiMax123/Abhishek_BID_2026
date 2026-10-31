import csv

from django.conf import settings
from django.db.models import Q
from django.http import JsonResponse, StreamingHttpResponse
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import (OpenApiParameter, extend_schema,
                                   extend_schema_view)
from rest_framework import generics, mixins, permissions, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.negotiation import DefaultContentNegotiation
from rest_framework.parsers import MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from .exceptions import FileTooLarge
from .models import Category, ImportJob, Product
from .serializers import (CategorySerializer, ImportErrorSerializer,
                          ImportJobSerializer, ImportUploadSerializer,
                          ProductSerializer, RegisterSerializer)
from .services import BulkImporter

EXPORT_COLUMNS = ["sku", "name", "price", "category", "stock", "is_active"]
PRODUCT_FILTER_PARAMS = [
    OpenApiParameter("category", int, description="Filter by category id"),
    OpenApiParameter("is_active", bool, description="Filter by active flag"),
]


def api_not_found(request, exception=None):
    return JsonResponse({"error": {"code": "not_found", "message": "Resource not found.", "details": {}}}, status=404)


def api_server_error(request):
    return JsonResponse(
        {"error": {"code": "server_error", "message": "An unexpected error occurred.", "details": {}}}, status=500
    )


def filter_products(qs, params):
    """Shared by the product list and the CSV export."""
    category = params.get("category")
    if category:
        if not category.isdigit():
            raise ValidationError({"category": ["Must be a category id (integer)."]})
        qs = qs.filter(category_id=int(category))
    is_active = params.get("is_active")
    if is_active:
        if is_active.lower() not in ("true", "false", "1", "0"):
            raise ValidationError({"is_active": ["Must be true or false."]})
        qs = qs.filter(is_active=is_active.lower() in ("true", "1"))
    return qs


# ------------------------------------------------------------------ auth
@extend_schema(auth=[], responses={201: RegisterSerializer})
class RegisterView(generics.CreateAPIView):
    serializer_class = RegisterSerializer
    permission_classes = [permissions.AllowAny]
    authentication_classes = []


# ------------------------------------------------------------ categories
class CategoryViewSet(viewsets.ModelViewSet):
    queryset = Category.objects.all()
    serializer_class = CategorySerializer


# -------------------------------------------------------------- products
@extend_schema_view(
    list=extend_schema(parameters=PRODUCT_FILTER_PARAMS + [
        OpenApiParameter("search", str, description="Search in SKU or name"),
        OpenApiParameter("ordering", str, description="sku, name, price, created_at (prefix - for descending)"),
    ])
)
class ProductViewSet(viewsets.ModelViewSet):
    serializer_class = ProductSerializer
    ALLOWED_ORDERING = {"sku", "name", "price", "created_at"}

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return Product.objects.none()
        qs = Product.objects.select_related("category")
        params = self.request.query_params
        qs = filter_products(qs, params)
        search = params.get("search")
        if search:
            qs = qs.filter(Q(sku__icontains=search) | Q(name__icontains=search))
        ordering = params.get("ordering")
        if ordering:
            if ordering.lstrip("-") not in self.ALLOWED_ORDERING:
                raise ValidationError({"ordering": ["Must be one of: %s." % ", ".join(sorted(self.ALLOWED_ORDERING))]})
            qs = qs.order_by(ordering, "id")
        return qs


# ---------------------------------------------------------------- import
class ProductImportView(APIView):
    """POST /api/products/import/"""

    parser_classes = [MultiPartParser]

    @extend_schema(
        request={"multipart/form-data": ImportUploadSerializer},
        responses={201: ImportJobSerializer},
        summary="Bulk import products from a CSV file",
    )
    def post(self, request):
        # Reject oversized bodies early, before Django parses the multipart payload.
        declared = int(request.META.get("CONTENT_LENGTH") or 0)
        if declared > settings.IMPORT_MAX_FILE_SIZE + 64 * 1024:
            raise FileTooLarge()

        serializer = ImportUploadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        uploaded = serializer.validated_data["file"]
        update_existing = serializer.validated_data["update_existing"]

        job = ImportJob.objects.create(
            created_by=request.user,
            original_filename=uploaded.name[:255],
            file_size=uploaded.size,
            mode=ImportJob.Mode.UPDATE_EXISTING if update_existing else ImportJob.Mode.INSERT_ONLY,
        )
        BulkImporter(job, uploaded).run()
        job.refresh_from_db()
        return Response(ImportJobSerializer(job, context={"request": request}).data, status=201)


class ImportJobViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """GET /api/imports/, /api/imports/{id}/, /api/imports/{id}/errors/"""

    serializer_class = ImportJobSerializer

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return ImportJob.objects.none()
        qs = ImportJob.objects.all()
        if not self.request.user.is_staff:
            qs = qs.filter(created_by=self.request.user)
        return qs

    @extend_schema(responses=ImportErrorSerializer(many=True), summary="Paginated list of failed rows")
    @action(detail=True, methods=["get"], url_path="errors")
    def errors(self, request, pk=None):
        job = self.get_object()
        page = self.paginate_queryset(job.errors.all())
        return self.get_paginated_response(ImportErrorSerializer(page, many=True).data)


# ---------------------------------------------------------------- export
class _Echo:
    """File-like object whose write() just returns the value, so csv.writer can feed a generator."""

    def write(self, value):
        return value


class AnyAcceptNegotiation(DefaultContentNegotiation):
    """The export returns CSV whatever the Accept header says (otherwise 'Accept: text/csv' -> 406)."""

    def select_renderer(self, request, renderers, format_suffix=None):
        return renderers[0], renderers[0].media_type


def _safe(cell):
    """Neutralise spreadsheet formula injection (=, +, -, @ at the start of a text cell)."""
    if isinstance(cell, str) and cell[:1] in ("=", "+", "-", "@"):
        return "'" + cell
    return cell


class ProductExportView(APIView):
    """GET /api/products/export/ - streams a CSV in bounded memory."""

    content_negotiation_class = AnyAcceptNegotiation

    @extend_schema(
        parameters=PRODUCT_FILTER_PARAMS,
        responses={(200, "text/csv"): OpenApiTypes.STR},
        summary="Stream all products as CSV",
    )
    def get(self, request):
        qs = filter_products(Product.objects.all(), request.query_params)
        response = StreamingHttpResponse(self._rows(qs), content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = 'attachment; filename="products.csv"'
        response["Cache-Control"] = "no-store"
        return response

    @staticmethod
    def _rows(qs):
        """
        Keyset pagination (WHERE id > last ORDER BY id LIMIT n): constant memory on every DB.
        (.iterator() alone is NOT enough on MySQL - mysqlclient buffers the whole result set.)
        """
        writer = csv.writer(_Echo())
        yield writer.writerow(EXPORT_COLUMNS)
        batch_size = settings.EXPORT_BATCH_SIZE
        last_id = 0
        while True:
            rows = list(
                qs.filter(id__gt=last_id)
                .order_by("id")
                .values_list("id", "sku", "name", "price", "category__name", "stock", "is_active")[:batch_size]
            )
            if not rows:
                break
            yield "".join(
                writer.writerow([_safe(sku), _safe(name), price, _safe(cat), stock, "true" if active else "false"])
                for _id, sku, name, price, cat, stock, active in rows
            )
            last_id = rows[-1][0]
