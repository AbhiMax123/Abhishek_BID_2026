"""
python manage.py benchmark_import --rows 50000

Generates a CSV (default 50,000 rows, ~2% deliberately bad), runs it through the SAME
BulkImporter the API uses, then times the streaming export. Prints a table you can paste
into the README ("document the time taken").
"""
import csv
import os
import random
import tempfile
import time

from django.contrib.auth import get_user_model
from django.core.files import File
from django.core.management.base import BaseCommand

from catalog import services
from catalog.management.commands.seed_categories import DEFAULT_CATEGORIES
from catalog.models import Category, ImportJob, Product
from catalog.views import ProductExportView

PREFIX = "BENCH-"


def peak_rss_mb():
    try:
        import resource
        import sys
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return round(peak / (1024 * 1024 if sys.platform == "darwin" else 1024), 1)
    except Exception:  # Windows has no `resource`
        return None


class Command(BaseCommand):
    help = "Benchmark bulk import (and export) with a generated CSV."

    def add_arguments(self, parser):
        parser.add_argument("--rows", type=int, default=50_000)
        parser.add_argument("--bad-ratio", type=float, default=0.02, help="Fraction of invalid rows (default 0.02)")
        parser.add_argument("--keep", help="Also save the generated CSV to this path")

    def handle(self, *args, **opts):
        rows, bad_ratio = opts["rows"], opts["bad_ratio"]
        for name in DEFAULT_CATEGORIES:
            Category.objects.get_or_create(name=name)
        user, _ = get_user_model().objects.get_or_create(username="benchmark")
        Product.objects.filter(sku__startswith=PREFIX).delete()

        path = self._generate(rows, bad_ratio, opts.get("keep"))
        size_mb = os.path.getsize(path) / 1048576
        self.stdout.write("Generated %d rows (%.2f MB) at %s" % (rows, size_mb, path))

        with open(path, "rb") as fh:
            f = File(fh, name="benchmark.csv")
            services.check_text_file(f)
            services.read_header(f)
            job = ImportJob.objects.create(
                created_by=user, original_filename="benchmark.csv", file_size=os.path.getsize(path)
            )
            wall = time.perf_counter()
            services.BulkImporter(job, f).run()
            wall = time.perf_counter() - wall

        t = time.perf_counter()
        exported_rows = exported_bytes = 0
        for piece in ProductExportView._rows(Product.objects.filter(sku__startswith=PREFIX)):
            exported_bytes += len(piece)
            exported_rows += piece.count("\n")
        export_seconds = time.perf_counter() - t

        self.stdout.write("")
        self.stdout.write("| Metric | Value |")
        self.stdout.write("|---|---|")
        from django.db import connection
        self.stdout.write("| Database | %s |" % connection.vendor)
        self.stdout.write("| Rows in file | %d (%.2f MB) |" % (job.total_rows, size_mb))
        self.stdout.write("| Chunk size | %d |" % services.settings.IMPORT_CHUNK_SIZE)
        self.stdout.write("| Inserted / failed | %d / %d |" % (job.inserted_count, job.failed_count))
        self.stdout.write("| Import time | %.2f s (job.duration_seconds=%s) |" % (wall, job.duration_seconds))
        self.stdout.write("| Throughput | %d rows/s |" % (job.total_rows / wall if wall else 0))
        self.stdout.write("| Export time | %.2f s for %d rows |" % (export_seconds, exported_rows - 1))
        rss = peak_rss_mb()
        if rss:
            self.stdout.write("| Peak process memory (RSS) | %s MB |" % rss)
        self.stdout.write("| Job status | %s |" % job.status)

        if not opts.get("keep"):
            os.unlink(path)

    def _generate(self, rows, bad_ratio, keep):
        rnd = random.Random(42)
        handle, path = (None, keep) if keep else tempfile.mkstemp(suffix=".csv")
        out = open(path, "w", newline="", encoding="utf-8") if keep else os.fdopen(handle, "w", newline="", encoding="utf-8")
        with out:
            w = csv.writer(out)
            w.writerow(["sku", "name", "price", "category", "stock"])
            for i in range(rows):
                sku, price, cat = "%s%06d" % (PREFIX, i), "%.2f" % rnd.uniform(1, 999), rnd.choice(DEFAULT_CATEGORIES)
                if rnd.random() < bad_ratio:
                    kind = rnd.randrange(4)
                    if kind == 0:
                        price = "-%s" % price
                    elif kind == 1:
                        cat = "Nonexistent"
                    elif kind == 2 and i > 0:
                        sku = "%s%06d" % (PREFIX, rnd.randrange(i))  # duplicate of an earlier SKU
                    else:
                        price = "abc"
                w.writerow([sku, "Benchmark product %d" % i, price, cat, rnd.randrange(0, 500)])
        return path
