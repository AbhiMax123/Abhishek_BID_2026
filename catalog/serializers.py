from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from rest_framework import serializers
from rest_framework.reverse import reverse

from . import services
from . import validators as v
from .exceptions import FileTooLarge
from .models import Category, ImportError, ImportJob, Product

User = get_user_model()

ALLOWED_CONTENT_TYPES = {
    "text/csv",
    "application/csv",
    "text/plain",
    "application/vnd.ms-excel",  # what Windows/Excel often sends for .csv
    "application/octet-stream",
    "",
}


class RegisterSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, min_length=8, style={"input_type": "password"})

    class Meta:
        model = User
        fields = ("id", "username", "email", "password")

    def validate_password(self, value):
        validate_password(value)
        return value

    def create(self, validated_data):
        return User.objects.create_user(**validated_data)


class CategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = Category
        fields = ("id", "name", "description", "created_at")
        read_only_fields = ("created_at",)


class ProductSerializer(serializers.ModelSerializer):
    sku = serializers.CharField(max_length=v.SKU_MAX_LENGTH)
    price = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=0)
    category_name = serializers.CharField(source="category.name", read_only=True)

    class Meta:
        model = Product
        fields = (
            "id", "sku", "name", "price", "stock", "is_active",
            "category", "category_name", "created_at", "updated_at",
        )
        read_only_fields = ("created_at", "updated_at")

    def validate_sku(self, value):
        sku, error = v.clean_sku(value)
        if error:
            raise serializers.ValidationError(error)
        qs = Product.objects.filter(sku=sku)
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError("A product with this SKU already exists.")
        return sku  # stored upper-case, same as the CSV importer


class ImportUploadSerializer(serializers.Serializer):
    file = serializers.FileField(help_text="CSV file, UTF-8, max 5 MB. Columns: sku,name,price,category[,stock,is_active]")
    update_existing = serializers.BooleanField(
        required=False, default=False,
        help_text="If true, rows whose SKU already exists update that product instead of failing.",
    )

    def validate_file(self, f):
        if not f.name.lower().endswith(".csv"):
            raise serializers.ValidationError("Only .csv files are accepted.")
        if (f.content_type or "").split(";")[0].strip().lower() not in ALLOWED_CONTENT_TYPES:
            raise serializers.ValidationError("Unsupported content type %r; upload a CSV file." % f.content_type)
        if f.size > settings.IMPORT_MAX_FILE_SIZE:
            raise FileTooLarge(
                "File is %.1f MB; the maximum allowed is %.1f MB."
                % (f.size / 1048576, settings.IMPORT_MAX_FILE_SIZE / 1048576)
            )
        try:
            services.check_text_file(f)
            services.read_header(f)
        except services.CSVFileError as exc:
            raise serializers.ValidationError(exc.messages)
        return f


class ImportJobSerializer(serializers.ModelSerializer):
    errors_url = serializers.SerializerMethodField()

    class Meta:
        model = ImportJob
        fields = (
            "id", "status", "mode", "original_filename", "file_size",
            "total_rows", "inserted_count", "updated_count", "failed_count",
            "failure_reason", "created_at", "started_at", "finished_at",
            "duration_seconds", "errors_url",
        )
        read_only_fields = fields

    def get_errors_url(self, obj) -> str:
        return reverse("import-errors", args=[obj.pk], request=self.context.get("request"))


class ImportErrorSerializer(serializers.ModelSerializer):
    class Meta:
        model = ImportError
        fields = ("id", "row_number", "sku", "issues", "raw_data")
