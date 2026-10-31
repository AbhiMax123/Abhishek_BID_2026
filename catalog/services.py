"""
Bulk CSV import engine.

Flow
----
1. `check_text_file` / `read_header`  - cheap file-level checks done BEFORE a job exists.
2. `BulkImporter.run`                 - streams the CSV row by row (never loads the file
   into memory), validates each row, and writes valid rows in chunks of
   IMPORT_CHUNK_SIZE (default 500) with bulk_create / bulk_update.

Each chunk is committed in its own transaction, so one bad row never blocks the good
ones, and progress is visible on the job while it runs.
"""
import codecs
import csv
import io
import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Dict, List

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from . import validators as v
from .models import Category, ImportJob, Product
from .models import ImportError as ImportRowError

logger = logging.getLogger(__name__)

UPDATE_FIELDS = ["name", "price", "category", "stock", "is_active", "updated_at"]


class CSVFileError(Exception):
    """File-level problem (bad encoding, missing columns...). `messages` is a list of str."""

    def __init__(self, messages):
        self.messages = [messages] if isinstance(messages, str) else list(messages)
        super().__init__(" ".join(self.messages))


# ------------------------------------------------------------ file helpers
def check_text_file(uploaded_file) -> None:
    """Reject binary files and anything that is not valid UTF-8 (one cheap streaming pass)."""
    decoder = codecs.getincrementaldecoder("utf-8")()
    try:
        for chunk in uploaded_file.chunks():
            if b"\x00" in chunk:
                raise CSVFileError("The file looks like a binary file, not a text CSV.")
            decoder.decode(chunk)
        decoder.decode(b"", final=True)
    except UnicodeDecodeError:
        raise CSVFileError("The file must be UTF-8 encoded text.")
    uploaded_file.seek(0)


@contextmanager
def open_text(uploaded_file):
    """Stream the uploaded file as text. utf-8-sig strips an Excel BOM; newline='' is what csv wants."""
    uploaded_file.seek(0)
    wrapper = io.TextIOWrapper(uploaded_file.file, encoding="utf-8-sig", newline="")
    try:
        yield wrapper
    finally:
        wrapper.detach()  # don't close the underlying upload


def read_header(uploaded_file) -> List[str]:
    """Return normalised header names, raising CSVFileError if they are unusable."""
    with open_text(uploaded_file) as text:
        try:
            first = next(csv.reader(text), [])
        except csv.Error as exc:
            raise CSVFileError("Malformed CSV: %s" % exc)
    uploaded_file.seek(0)
    headers = v.normalize_headers(first)
    problems = v.check_headers(headers)
    if problems:
        raise CSVFileError(problems)
    return headers


# ---------------------------------------------------------------- importer
@dataclass
class _Pending:
    line_no: int
    row: v.ParsedRow
    category_id: int
    raw: Dict[str, str]


def _truncate(raw: Dict[str, str], limit: int = 200) -> Dict[str, str]:
    return {k: (val or "")[:limit] for k, val in raw.items()}


class BulkImporter:
    def __init__(self, job: ImportJob, uploaded_file, chunk_size: int = None):
        self.job = job
        self.file = uploaded_file
        self.chunk_size = chunk_size or settings.IMPORT_CHUNK_SIZE
        self.update_existing = job.mode == ImportJob.Mode.UPDATE_EXISTING
        self._error_buffer: List[ImportRowError] = []

    # -- public ---------------------------------------------------------
    def run(self) -> ImportJob:
        job = self.job
        started = time.perf_counter()
        job.status = ImportJob.Status.PROCESSING
        job.started_at = timezone.now()
        job.save(update_fields=["status", "started_at"])
        try:
            self._process()
            if job.total_rows == 0:
                job.status = ImportJob.Status.FAILED
                job.failure_reason = "The file contains a header but no data rows."
            elif job.failed_count:
                job.status = ImportJob.Status.COMPLETED_WITH_ERRORS
            else:
                job.status = ImportJob.Status.COMPLETED
        except CSVFileError as exc:
            job.status = ImportJob.Status.FAILED
            job.failure_reason = " ".join(exc.messages)[:1000]
        except csv.Error as exc:
            job.status = ImportJob.Status.FAILED
            job.failure_reason = ("Malformed CSV: %s" % exc)[:1000]
        except Exception:  # noqa: BLE001 - never leave a job stuck in "processing"
            logger.exception("Import job %s crashed", job.pk)
            job.status = ImportJob.Status.FAILED
            job.failure_reason = "Import aborted because of an unexpected server error."
        finally:
            job.finished_at = timezone.now()
            job.duration_seconds = round(time.perf_counter() - started, 3)
            job.save()
        logger.info(
            "Import job %s %s: rows=%s inserted=%s updated=%s failed=%s in %ss",
            job.pk, job.status, job.total_rows, job.inserted_count,
            job.updated_count, job.failed_count, job.duration_seconds,
        )
        return job

    # -- internals ------------------------------------------------------
    def _process(self):
        job = self.job
        categories = {c.name.casefold(): c.pk for c in Category.objects.only("id", "name")}
        seen_skus: Dict[str, int] = {}  # sku -> first line number (duplicate detection inside the file)
        batch: List[_Pending] = []

        with open_text(self.file) as text:
            reader = csv.reader(text)
            headers = v.normalize_headers(next(reader, []))
            problems = v.check_headers(headers)
            if problems:
                raise CSVFileError(problems)
            col_index = {h: i for i, h in enumerate(headers) if h in v.KNOWN_COLUMNS}

            for cells in reader:
                if not cells or not any(c.strip() for c in cells):
                    continue  # blank line
                line_no = reader.line_num
                job.total_rows += 1

                if len(cells) != len(headers):
                    self._record_error(
                        line_no, "", {"raw_line": ",".join(cells)[:500]},
                        [{"field": "row", "message": "Expected %d columns but found %d." % (len(headers), len(cells))}],
                    )
                    continue

                raw = {name: cells[i] for name, i in col_index.items()}
                parsed = v.validate_row(raw)

                category_id = None
                if parsed.category is not None:
                    category_id = categories.get(parsed.category.casefold())
                    if category_id is None:
                        parsed.issues.append(
                            {"field": "category", "message": "Unknown category %r." % parsed.category[:100]}
                        )
                if parsed.sku is not None and parsed.sku in seen_skus:
                    parsed.issues.append({
                        "field": "sku",
                        "message": "Duplicate SKU in file (first seen on row %d)." % seen_skus[parsed.sku],
                    })

                if parsed.issues:
                    self._record_error(line_no, parsed.sku or raw.get("sku", "").strip()[:64], raw, parsed.issues)
                    continue

                seen_skus[parsed.sku] = line_no
                batch.append(_Pending(line_no, parsed, category_id, raw))
                if len(batch) >= self.chunk_size:
                    self._flush(batch)
                    batch = []

        if batch:
            self._flush(batch)
        self._flush_errors()
        self._save_progress()

    def _record_error(self, line_no, sku, raw, issues):
        self.job.failed_count += 1
        self._error_buffer.append(
            ImportRowError(job=self.job, row_number=line_no, sku=sku, issues=issues, raw_data=_truncate(raw))
        )
        if len(self._error_buffer) >= self.chunk_size:
            self._flush_errors()

    def _flush_errors(self):
        if self._error_buffer:
            ImportRowError.objects.bulk_create(self._error_buffer, batch_size=self.chunk_size)
            self._error_buffer = []

    def _save_progress(self):
        self.job.save(update_fields=["total_rows", "inserted_count", "updated_count", "failed_count"])

    def _flush(self, batch: List[_Pending]):
        """Write one chunk: ONE select for existing SKUs, then bulk_create + bulk_update."""
        existing = {p.sku: p for p in Product.objects.filter(sku__in=[p.row.sku for p in batch])}
        to_create, to_update = [], []

        for item in batch:
            r = item.row
            product = existing.get(r.sku)
            if product is None:
                to_create.append((item, Product(
                    sku=r.sku, name=r.name, price=r.price, category_id=item.category_id,
                    stock=r.stock if r.stock is not None else 0,
                    is_active=r.is_active if r.is_active is not None else True,
                )))
            elif self.update_existing:
                product.name = r.name
                product.price = r.price
                product.category_id = item.category_id
                if r.stock is not None:
                    product.stock = r.stock
                if r.is_active is not None:
                    product.is_active = r.is_active
                product.updated_at = timezone.now()  # bulk_update skips auto_now
                to_update.append((item, product))
            else:
                self._record_error(item.line_no, r.sku, item.raw, [
                    {"field": "sku", "message": "SKU already exists in the database (use update_existing=true to update it)."}
                ])

        try:
            with transaction.atomic():
                if to_create:
                    Product.objects.bulk_create([p for _, p in to_create], batch_size=self.chunk_size)
                if to_update:
                    Product.objects.bulk_update([p for _, p in to_update], UPDATE_FIELDS, batch_size=self.chunk_size)
            self.job.inserted_count += len(to_create)
            self.job.updated_count += len(to_update)
        except IntegrityError:
            # Rare (e.g. a concurrent request inserted the same SKU between our SELECT and INSERT).
            # Fall back to row-by-row so we can say exactly which rows failed.
            logger.warning("Chunk hit IntegrityError; retrying row by row")
            self._save_individually(to_create, to_update)

        self._save_progress()

    def _save_individually(self, to_create, to_update):
        for items, is_new in ((to_create, True), (to_update, False)):
            for item, product in items:
                try:
                    with transaction.atomic():
                        if is_new:
                            product.save(force_insert=True)
                        else:
                            product.save()
                except IntegrityError:
                    self._record_error(item.line_no, item.row.sku, item.raw, [
                        {"field": "sku", "message": "Could not be saved because of a database constraint conflict."}
                    ])
                else:
                    if is_new:
                        self.job.inserted_count += 1
                    else:
                        self.job.updated_count += 1
