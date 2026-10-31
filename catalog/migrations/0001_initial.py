import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="Category",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=100, unique=True)),
                ("description", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={"verbose_name_plural": "categories", "ordering": ["name"]},
        ),
        migrations.CreateModel(
            name="Product",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("sku", models.CharField(max_length=64, unique=True)),
                ("name", models.CharField(max_length=255)),
                ("price", models.DecimalField(decimal_places=2, max_digits=12)),
                ("stock", models.PositiveIntegerField(default=0)),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "category",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="products",
                        to="catalog.category",
                    ),
                ),
            ],
            options={"ordering": ["sku"]},
        ),
        migrations.CreateModel(
            name="ImportJob",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("original_filename", models.CharField(max_length=255)),
                ("file_size", models.PositiveIntegerField(default=0)),
                (
                    "mode",
                    models.CharField(
                        choices=[("insert_only", "Insert only"), ("update_existing", "Update existing")],
                        default="insert_only",
                        max_length=20,
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "Pending"),
                            ("processing", "Processing"),
                            ("completed", "Completed"),
                            ("completed_with_errors", "Completed with errors"),
                            ("failed", "Failed"),
                        ],
                        default="pending",
                        max_length=30,
                    ),
                ),
                ("total_rows", models.PositiveIntegerField(default=0)),
                ("inserted_count", models.PositiveIntegerField(default=0)),
                ("updated_count", models.PositiveIntegerField(default=0)),
                ("failed_count", models.PositiveIntegerField(default=0)),
                ("failure_reason", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
                ("duration_seconds", models.FloatField(blank=True, null=True)),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="import_jobs",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={"ordering": ["-created_at", "-id"]},
        ),
        migrations.CreateModel(
            name="ImportError",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "row_number",
                    models.PositiveIntegerField(help_text="Line number in the CSV (header is line 1)."),
                ),
                ("sku", models.CharField(blank=True, max_length=64)),
                ("issues", models.JSONField(default=list)),
                ("raw_data", models.JSONField(default=dict)),
                (
                    "job",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="errors",
                        to="catalog.importjob",
                    ),
                ),
            ],
            options={"ordering": ["row_number", "id"]},
        ),
        migrations.AddIndex(
            model_name="product",
            index=models.Index(fields=["category", "is_active"], name="prod_cat_active_idx"),
        ),
        migrations.AddIndex(
            model_name="product",
            index=models.Index(fields=["name"], name="prod_name_idx"),
        ),
        migrations.AddConstraint(
            model_name="product",
            constraint=models.CheckConstraint(condition=models.Q(("price__gte", 0)), name="product_price_gte_0"),
        ),
        migrations.AddIndex(
            model_name="importjob",
            index=models.Index(fields=["created_by", "-created_at"], name="job_user_created_idx"),
        ),
        migrations.AddIndex(
            model_name="importjob",
            index=models.Index(fields=["status"], name="job_status_idx"),
        ),
        migrations.AddIndex(
            model_name="importerror",
            index=models.Index(fields=["job", "row_number"], name="imperr_job_row_idx"),
        ),
    ]
