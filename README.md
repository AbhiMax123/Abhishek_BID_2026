# Bulk CSV Import & Data Quality — Django REST API

Bulk CSV Import and Data Quality** (`BID_2026`).
Django · DRF · MySQL 8 · JWT · drf-spectacular · REST API only.

## What it does

| Requirement | Where |
|---|---|
| Models: Product, Category, ImportJob, ImportError | `catalog/models.py` |
| `POST /products/import/` – CSV ≤ 5 MB → ImportJob → chunks of 500 with `bulk_create` | `views.ProductImportView`, `services.BulkImporter` |
| Validate file type, headers, duplicate SKUs (in file **and** DB), negative prices, unknown categories | `serializers.ImportUploadSerializer`, `validators.py`, `services.py` |
| `GET /imports/{id}/` status + inserted + failed counts | `views.ImportJobViewSet` |
| `GET /imports/{id}/errors/` paginated | `views.ImportJobViewSet.errors` |
| Optional "update existing" mode with `bulk_update` | `update_existing=true` form field |
| `GET /products/export/` streams CSV without loading everything into memory | `views.ProductExportView` (keyset-paginated generator) |
| 50,000 rows reasonably + document time | `manage.py benchmark_import` + table below |
| Settings from `.env` | `config/settings.py` (django-environ), `.env.example` |
| JWT, pagination, consistent errors | simplejwt, `pagination.py`, `exceptions.py` |
| Migrations, indexes, DB constraints | `catalog/migrations/0001_initial.py` (indexes, unique SKU, `CHECK price >= 0`) |
| ≥ 8 tests with APITestCase | `catalog/tests/` (≈40 tests) |
| README + Postman | this file, `postman/…json`, `/api/docs/` |

## Setup

```bash
python -m venv venv & venv\Scripts\activate      
pip install -r requirements.txt                       
cp .env.example .env                           

# MySQL 8
mysql -u root -p -e "CREATE DATABASE bulk_import_db CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;"

python manage.py migrate
python manage.py seed_categories        # Electronics, Books, Clothing, Home, Toys
python manage.py createsuperuser        # optional, or use POST /api/auth/register/
python manage.py runserver
```

`mysqlclient` build deps — Ubuntu: `sudo apt install libmysqlclient-dev pkg-config build-essential python3-dev` · macOS: `brew install mysql pkg-config`.

UI: <http://127.0.0.1:8000/api/docs/> · OpenAPI schema: `/api/schema/`.

## Endpoints

All except `auth/*` need `Authorization: Bearer <access_token>`.

| Method | URL | Purpose |
|---|---|---|
| POST | `/api/auth/register/` | create a user |
| POST | `/api/auth/token/` · `/api/auth/token/refresh/` | JWT login / refresh |
| GET/POST | `/api/categories/` (+ `/{id}/`) | manage categories |
| GET/POST | `/api/products/` (+ `/{id}/`) | list (`?search=&category=&is_active=&ordering=&page=&page_size=`) / CRUD |
| **POST** | **`/api/products/import/`** | multipart: `file` (CSV), optional `update_existing=true` → **201** + job |
| GET | `/api/imports/` | my import jobs |
| **GET** | **`/api/imports/{id}/`** | status, `total_rows`, `inserted_count`, `updated_count`, `failed_count`, `duration_seconds` |
| **GET** | **`/api/imports/{id}/errors/`** | paginated failed rows (`row_number`, `sku`, `issues[]`, `raw_data`) |
| **GET** | **`/api/products/export/`** | streamed CSV (`?category=&is_active=`) |

### CSV format

```csv
sku,name,price,category,stock,is_active
LAP-001,Laptop 14-inch,799.99,Electronics,25,true
```

Required: `sku, name, price, category`. Optional: `stock` (blank = 0 on insert / unchanged on update), `is_active`.
Headers are case-insensitive; UTF-8 (BOM OK); extra columns are ignored. SKUs are stored upper-case.
A sample with deliberate errors is in `samples/sample_products.csv` (8 good rows, 7 bad ones).

### Data-quality rules

File level (→ HTTP 400/413, **no job created**): extension `.csv`; plausible content type; ≤ 5 MB (413); not binary; valid UTF-8; required headers present; no duplicate headers.

Row level (→ row recorded in `ImportError`, other rows still import): SKU required/≤64 chars/allowed characters · name required · price numeric, **≥ 0**, ≤ 2 decimals · **category must already exist** (case-insensitive) · stock whole number ≥ 0 · `is_active` boolean · correct column count · **duplicate SKU inside the file** (first occurrence wins) · **SKU already in DB** (error, or update in `update_existing` mode).
Every problem on a row is reported at once, with the CSV line number (header = line 1).

### Error format (every endpoint)

```json
{"error": {"code": "validation_error", "message": "Invalid input.", "details": {"file": ["Missing required column(s): price. ..."]}}}
```
Codes: `validation_error, unauthorized, permission_denied, not_found, payload_too_large, conflict, server_error, …`

### Example

```bash
TOKEN=$(curl -s -X POST localhost:8000/api/auth/token/ -H 'Content-Type: application/json' \
        -d '{"username":"trainee","password":"S3cure-Pass-123"}' | python -c 'import sys,json;print(json.load(sys.stdin)["access"])')

curl -X POST localhost:8000/api/products/import/ -H "Authorization: Bearer $TOKEN" \
     -F file=@samples/sample_products.csv -F update_existing=false
curl localhost:8000/api/imports/1/ -H "Authorization: Bearer $TOKEN"
curl "localhost:8000/api/imports/1/errors/?page_size=5" -H "Authorization: Bearer $TOKEN"
curl localhost:8000/api/products/export/ -H "Authorization: Bearer $TOKEN" -o products.csv
```

## Design notes

* **Streaming, not slurping** – the upload is read through `csv.reader` row by row; only one chunk (500 rows) of work objects plus a `{sku: line}` dict for duplicate detection is held in memory.
* **One SELECT per chunk** (`sku IN (...)`) finds existing products; then one `bulk_create` and (update mode) one `bulk_update`. `bulk_update` bypasses `auto_now`, so `updated_at` is set explicitly.
* **Each chunk = its own transaction**: a failure never blocks valid rows, and `GET /imports/{id}/` shows live progress. If a chunk hits an `IntegrityError` (e.g. a concurrent insert of the same SKU) it is retried row-by-row so the exact bad rows are reported.
* **Job statuses**: `completed`, `completed_with_errors`, `failed` (file had no data rows, or an unexpected crash — never left stuck in `processing`).
* **Import runs synchronously** inside the request (50k rows takes seconds, and the spec ties the job to the request). Because progress is already persisted per chunk, moving `BulkImporter.run()` into Celery later is a one-line change in the view.
* **Export** uses keyset pagination (`WHERE id > last ORDER BY id LIMIT n`). `QuerySet.iterator()` alone would *not* bound memory on MySQL (mysqlclient buffers the whole result). Text cells starting with `= + - @` get a leading `'` to prevent spreadsheet formula injection. The export ignores the `Accept` header (otherwise `Accept: text/csv` would be a 406).
* **DB-level safety nets**: unique SKU, `CHECK (price >= 0)`, FK `PROTECT` (deleting a category that has products → 409), indexes on `(category, is_active)`, `name`, job `(created_by, -created_at)`, job `status`, error `(job, row_number)`.
* Users only see **their own** import jobs (staff see all).

## Tests

```bash
python manage.py test                                              
DB_ENGINE=django.db.backends.sqlite3 python manage.py test         
python -m unittest catalog.tests.test_validators                   
```

Covers: auth/JWT, happy path, BOM/CRLF, chunking (asserts `bulk_create` call count), file validation (type, size 413, binary, encoding, headers, empty), row errors with row numbers, duplicate SKU in file and in DB, update mode, paginated errors, job ownership, export (streaming, batching, filters, `Accept: text/csv`, round trip, formula injection), CRUD, DB check constraint, error envelope.

## Performance — 50,000 rows

Reproduce on your machine (generates ~2 MB CSV, ~2 % invalid rows, runs the real importer, then the export):

```bash
python manage.py benchmark_import --rows 50000
```

Measured **pure-Python parsing + validation of 50,000 rows (no DB)**: **0.25 s** (≈ 200k rows/s, sandbox CPU) — so total time is dominated by MySQL, which is why bulk operations in chunks of 500 are used instead of 50,000 single `INSERT`s.

Full run on MySQL 8 — :

| Metric | Value |
|---|---|
| Machine / MySQL version | Windows, MySQL 8.0.46 |
| Rows / file size | 50,000 / 2.66 MB (~2% deliberately invalid) |
| Chunk size | 500 |
| Result | 49,030 inserted, 970 failed (negative price, unknown category, duplicate SKU, bad price) |
| Import time | 7.95 s |
| Throughput | ~6,290 rows/s |
| Export time | 2.42 s for 49,030 rows |

##Checklist

1. Create a **separate private repo for this use case** named `<trainee-name>-bulk-csv-import`.
2. Add the trainer as a **collaborator with push access** *before* coding/pushing.
3. `git init && git add . && git commit -m "Bulk CSV import API" && git remote add origin <url> && git push -u origin main`
