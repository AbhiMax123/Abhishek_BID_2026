from django.conf import settings
from django.db import models


class Category(models.Model):
    name = models.CharField(max_length=100, unique=True)
    description = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "categories"

    def __str__(self):
        return self.name


class Product(models.Model):
    sku = models.CharField(max_length=64, unique=True)
    name = models.CharField(max_length=255)
    price = models.DecimalField(max_digits=12, decimal_places=2)
    stock = models.PositiveIntegerField(default=0)
    category = models.ForeignKey(Category, on_delete=models.PROTECT, related_name="products")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["sku"]
        indexes = [
            models.Index(fields=["category", "is_active"], name="prod_cat_active_idx"),
            models.Index(fields=["name"], name="prod_name_idx"),
        ]
        constraints = [
            models.CheckConstraint(condition=models.Q(price__gte=0), name="product_price_gte_0"),
        ]

    def __str__(self):
        return f"{self.sku} - {self.name}"


class ImportJob(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        PROCESSING = "processing", "Processing"
        COMPLETED = "completed", "Completed"
        COMPLETED_WITH_ERRORS = "completed_with_errors", "Completed with errors"
        FAILED = "failed", "Failed"

    class Mode(models.TextChoices):
        INSERT_ONLY = "insert_only", "Insert only"
        UPDATE_EXISTING = "update_existing", "Update existing"

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="import_jobs",
    )
    original_filename = models.CharField(max_length=255)
    file_size = models.PositiveIntegerField(default=0)
    mode = models.CharField(max_length=20, choices=Mode.choices, default=Mode.INSERT_ONLY)
    status = models.CharField(max_length=30, choices=Status.choices, default=Status.PENDING)
    total_rows = models.PositiveIntegerField(default=0)
    inserted_count = models.PositiveIntegerField(default=0)
    updated_count = models.PositiveIntegerField(default=0)
    failed_count = models.PositiveIntegerField(default=0)
    failure_reason = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    duration_seconds = models.FloatField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["created_by", "-created_at"], name="job_user_created_idx"),
            models.Index(fields=["status"], name="job_status_idx"),
        ]

    def __str__(self):
        return f"ImportJob #{self.pk} ({self.status})"


class ImportError(models.Model):  # noqa: A001 - name required by the spec
    """One failed CSV row. `issues` lists every problem found on that row."""

    job = models.ForeignKey(ImportJob, on_delete=models.CASCADE, related_name="errors")
    row_number = models.PositiveIntegerField(help_text="Line number in the CSV (header is line 1).")
    sku = models.CharField(max_length=64, blank=True)
    issues = models.JSONField(default=list)
    raw_data = models.JSONField(default=dict)

    class Meta:
        ordering = ["row_number", "id"]
        indexes = [models.Index(fields=["job", "row_number"], name="imperr_job_row_idx")]

    def __str__(self):
        return f"Job {self.job_id} row {self.row_number}"
