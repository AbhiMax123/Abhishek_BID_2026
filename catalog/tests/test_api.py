from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse
from rest_framework.test import APIClient, APITestCase

from catalog.models import Category, ImportJob, Product

User = get_user_model()
PASSWORD = "S3cure-Pass-123"

HEADER = "sku,name,price,category,stock\n"
GOOD_CSV = HEADER + "A-1,Widget,10.50,Electronics,5\nA-2,Gadget,20,Electronics,\nB-1,Novel,7.99,Books,3\n"


def csv_upload(text, name="products.csv", content_type="text/csv"):
    return SimpleUploadedFile(name, text.encode("utf-8"), content_type=content_type)


class BaseAPITest(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user("alice", password=PASSWORD)
        self.client.force_authenticate(self.user)
        self.electronics = Category.objects.create(name="Electronics")
        self.books = Category.objects.create(name="Books")

    def do_import(self, text, update_existing=False, **file_kwargs):
        data = {"file": csv_upload(text, **file_kwargs)}
        if update_existing:
            data["update_existing"] = "true"
        return self.client.post(reverse("product-import"), data, format="multipart")

    def errors_of(self, job_id, **params):
        return self.client.get(reverse("import-errors", args=[job_id]), params)


class AuthTests(APITestCase):
    def test_endpoints_require_authentication_with_consistent_error(self):
        resp = self.client.get(reverse("product-list"))
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.data["error"]["code"], "unauthorized")
        self.assertIn("message", resp.data["error"])

    def test_register_login_and_use_jwt(self):
        resp = self.client.post(
            reverse("auth-register"), {"username": "bob", "password": PASSWORD, "email": "b@x.com"}, format="json"
        )
        self.assertEqual(resp.status_code, 201)
        self.assertNotIn("password", resp.data)

        tokens = self.client.post(reverse("token-obtain"), {"username": "bob", "password": PASSWORD}, format="json")
        self.assertEqual(tokens.status_code, 200)
        self.assertIn("access", tokens.data)

        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION="Bearer " + tokens.data["access"])
        self.assertEqual(client.get(reverse("product-list")).status_code, 200)

        refreshed = self.client.post(reverse("token-refresh"), {"refresh": tokens.data["refresh"]}, format="json")
        self.assertEqual(refreshed.status_code, 200)

    def test_bad_login_uses_error_envelope(self):
        resp = self.client.post(reverse("token-obtain"), {"username": "nobody", "password": "x"}, format="json")
        self.assertEqual(resp.status_code, 401)
        self.assertIn("error", resp.data)


class ImportHappyPathTests(BaseAPITest):
    def test_import_creates_products_and_job(self):
        resp = self.do_import(GOOD_CSV)
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(resp.data["status"], "completed")
        self.assertEqual(resp.data["total_rows"], 3)
        self.assertEqual(resp.data["inserted_count"], 3)
        self.assertEqual(resp.data["failed_count"], 0)
        self.assertIsNotNone(resp.data["duration_seconds"])
        self.assertEqual(Product.objects.count(), 3)
        p = Product.objects.get(sku="A-1")
        self.assertEqual(p.price, Decimal("10.50"))
        self.assertEqual(p.category, self.electronics)
        self.assertEqual(Product.objects.get(sku="A-2").stock, 0)  # blank stock -> default

    def test_bom_crlf_and_case_insensitive_headers_and_category(self):
        text = "\ufeffSKU,Name,Price,Category\r\nx-1,Thing,1.00,books\r\n"
        resp = self.do_import(text)
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(Product.objects.get(sku="X-1").category, self.books)

    def test_blank_lines_are_ignored(self):
        resp = self.do_import(HEADER + "A-1,W,1,Books,1\n\n\nA-2,W,1,Books,1\n")
        self.assertEqual(resp.data["total_rows"], 2)

    def test_processes_in_chunks_with_bulk_create(self):
        rows = "".join("S-%d,Item,1.00,Books,1\n" % i for i in range(5))
        with override_settings(IMPORT_CHUNK_SIZE=2), mock.patch.object(
            Product.objects, "bulk_create", wraps=Product.objects.bulk_create
        ) as spy:
            resp = self.do_import(HEADER + rows)
        self.assertEqual(resp.data["inserted_count"], 5)
        self.assertEqual(spy.call_count, 3)  # 2 + 2 + 1


class FileValidationTests(BaseAPITest):
    def test_rejects_non_csv_extension(self):
        resp = self.do_import(GOOD_CSV, name="products.xlsx")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data["error"]["code"], "validation_error")
        self.assertIn("file", resp.data["error"]["details"])

    def test_rejects_binary_content(self):
        binary = SimpleUploadedFile("x.csv", b"PK\x03\x04\x00\x00binary", content_type="text/csv")
        resp = self.client.post(reverse("product-import"), {"file": binary}, format="multipart")
        self.assertEqual(resp.status_code, 400)

    def test_rejects_non_utf8(self):
        bad = SimpleUploadedFile("x.csv", HEADER.encode() + b"A,\xe9\xe8,1,Books,1\n", content_type="text/csv")
        resp = self.client.post(reverse("product-import"), {"file": bad}, format="multipart")
        self.assertEqual(resp.status_code, 400)

    def test_rejects_wrong_content_type(self):
        resp = self.do_import(GOOD_CSV, content_type="image/png")
        self.assertEqual(resp.status_code, 400)

    def test_rejects_file_over_size_limit_with_413(self):
        with override_settings(IMPORT_MAX_FILE_SIZE=100):
            resp = self.do_import(GOOD_CSV + "A-9,Padding,1,Books,1\n" * 20)
        self.assertEqual(resp.status_code, 413)
        self.assertEqual(resp.data["error"]["code"], "payload_too_large")
        self.assertEqual(ImportJob.objects.count(), 0)

    def test_rejects_missing_required_header(self):
        resp = self.do_import("sku,name,category\nA,B,Books\n")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("price", str(resp.data["error"]["details"]))
        self.assertEqual(ImportJob.objects.count(), 0)

    def test_rejects_empty_file(self):
        resp = self.do_import("")
        self.assertEqual(resp.status_code, 400)

    def test_missing_file_field(self):
        resp = self.client.post(reverse("product-import"), {}, format="multipart")
        self.assertEqual(resp.status_code, 400)

    def test_header_only_file_creates_failed_job(self):
        resp = self.do_import(HEADER)
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.data["status"], "failed")


class DataQualityTests(BaseAPITest):
    def test_row_level_errors_report_row_numbers_and_reasons(self):
        text = HEADER + "\n".join([
            "OK-1,Fine,5.00,Books,1",         # line 2  ok
            "NEG-1,Negative,-3,Books,1",      # line 3  negative price
            "CAT-1,NoCat,3,Garden,1",         # line 4  unknown category
            "PRC-1,BadPrice,abc,Books,1",     # line 5  bad price
            ",NoSku,3,Books,1",               # line 6  missing sku
            "SHORT-1,TooFew,3",               # line 7  wrong column count
            "STK-1,BadStock,3,Books,-2",      # line 8  bad stock
        ]) + "\n"
        resp = self.do_import(text)
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.data["status"], "completed_with_errors")
        self.assertEqual(resp.data["total_rows"], 7)
        self.assertEqual(resp.data["inserted_count"], 1)
        self.assertEqual(resp.data["failed_count"], 6)
        self.assertEqual(Product.objects.count(), 1)

        errors = self.errors_of(resp.data["id"]).data["results"]
        by_row = {e["row_number"]: e for e in errors}
        self.assertEqual(sorted(by_row), [3, 4, 5, 6, 7, 8])
        self.assertEqual(by_row[3]["issues"][0]["field"], "price")
        self.assertIn("negative", by_row[3]["issues"][0]["message"].lower())
        self.assertIn("Unknown category", by_row[4]["issues"][0]["message"])
        self.assertEqual(by_row[6]["issues"][0]["field"], "sku")
        self.assertIn("columns", by_row[7]["issues"][0]["message"])
        self.assertEqual(by_row[3]["sku"], "NEG-1")

    def test_duplicate_sku_inside_file(self):
        text = HEADER + "D-1,First,1,Books,1\nD-2,Other,1,Books,1\nd-1,Again,2,Books,1\n"
        resp = self.do_import(text)
        self.assertEqual(resp.data["inserted_count"], 2)
        self.assertEqual(resp.data["failed_count"], 1)
        err = self.errors_of(resp.data["id"]).data["results"][0]
        self.assertEqual(err["row_number"], 4)
        self.assertIn("Duplicate SKU in file", err["issues"][0]["message"])
        self.assertEqual(Product.objects.get(sku="D-1").name, "First")  # first occurrence wins

    def test_duplicate_sku_already_in_database(self):
        Product.objects.create(sku="A-1", name="Old", price=1, category=self.books)
        resp = self.do_import(GOOD_CSV)
        self.assertEqual(resp.data["inserted_count"], 2)
        self.assertEqual(resp.data["failed_count"], 1)
        err = self.errors_of(resp.data["id"]).data["results"][0]
        self.assertIn("already exists", err["issues"][0]["message"])
        self.assertEqual(Product.objects.get(sku="A-1").name, "Old")  # untouched

    def test_update_existing_mode_uses_bulk_update(self):
        Product.objects.create(sku="A-1", name="Old", price=1, stock=99, category=self.books)
        text = HEADER + "A-1,New name,12.34,Electronics,\nA-3,Brand new,5,Books,2\n"
        with mock.patch.object(Product.objects, "bulk_update", wraps=Product.objects.bulk_update) as spy:
            resp = self.do_import(text, update_existing=True)
        self.assertEqual(spy.call_count, 1)
        self.assertEqual(resp.data["mode"], "update_existing")
        self.assertEqual(resp.data["inserted_count"], 1)
        self.assertEqual(resp.data["updated_count"], 1)
        p = Product.objects.get(sku="A-1")
        self.assertEqual((p.name, p.price, p.category), ("New name", Decimal("12.34"), self.electronics))
        self.assertEqual(p.stock, 99)  # blank stock cell leaves it unchanged

    def test_errors_endpoint_is_paginated(self):
        text = HEADER + "".join("N-%d,Bad,-1,Books,1\n" % i for i in range(25))
        job_id = self.do_import(text).data["id"]
        page1 = self.errors_of(job_id, page_size=10)
        self.assertEqual(page1.status_code, 200)
        self.assertEqual(page1.data["count"], 25)
        self.assertEqual(len(page1.data["results"]), 10)
        self.assertEqual(page1.data["total_pages"], 3)
        self.assertIsNotNone(page1.data["next"])
        page3 = self.errors_of(job_id, page_size=10, page=3)
        self.assertEqual(len(page3.data["results"]), 5)
        self.assertEqual(self.errors_of(job_id, page_size=10, page=9).status_code, 404)

    def test_db_check_constraint_blocks_negative_price(self):
        from django.db import IntegrityError, transaction
        with self.assertRaises(IntegrityError), transaction.atomic():
            Product.objects.create(sku="Z", name="Z", price=-1, category=self.books)


class ImportJobEndpointTests(BaseAPITest):
    def test_job_detail_and_list(self):
        job_id = self.do_import(GOOD_CSV).data["id"]
        detail = self.client.get(reverse("import-detail", args=[job_id]))
        self.assertEqual(detail.status_code, 200)
        for key in ("status", "inserted_count", "failed_count", "total_rows", "duration_seconds", "errors_url"):
            self.assertIn(key, detail.data)
        self.assertEqual(self.client.get(reverse("import-list")).data["count"], 1)

    def test_users_cannot_see_each_others_jobs(self):
        job_id = self.do_import(GOOD_CSV).data["id"]
        other = APIClient()
        other.force_authenticate(User.objects.create_user("mallory", password=PASSWORD))
        self.assertEqual(other.get(reverse("import-detail", args=[job_id])).status_code, 404)
        self.assertEqual(other.get(reverse("import-errors", args=[job_id])).status_code, 404)
        self.assertEqual(other.get(reverse("import-list")).data["count"], 0)

    def test_unknown_job_404_uses_error_envelope(self):
        resp = self.client.get(reverse("import-detail", args=[99999]))
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.data["error"]["code"], "not_found")


class ExportTests(BaseAPITest):
    def make_products(self, n):
        Product.objects.bulk_create(
            [Product(sku="E-%03d" % i, name="Item %d" % i, price=Decimal("1.50") * i, stock=i,
                     category=self.books) for i in range(1, n + 1)]
        )

    def read(self, response):
        return b"".join(response.streaming_content).decode("utf-8")

    def test_export_streams_csv(self):
        self.make_products(5)
        resp = self.client.get(reverse("product-export"))
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.streaming)
        self.assertTrue(resp["Content-Type"].startswith("text/csv"))
        lines = self.read(resp).strip().splitlines()
        self.assertEqual(lines[0], "sku,name,price,category,stock,is_active")
        self.assertEqual(len(lines), 6)
        self.assertEqual(lines[1], "E-001,Item 1,1.50,Books,1,true")

    def test_export_batches_with_keyset_pagination(self):
        self.make_products(7)
        with override_settings(EXPORT_BATCH_SIZE=3):
            lines = self.read(self.client.get(reverse("product-export"))).strip().splitlines()
        self.assertEqual(len(lines), 8)
        self.assertEqual([l.split(",")[0] for l in lines[1:]], ["E-%03d" % i for i in range(1, 8)])

    def test_export_ignores_accept_header_and_filters(self):
        self.make_products(2)
        Product.objects.create(sku="EL-1", name="Phone", price=9, category=self.electronics)
        resp = self.client.get(reverse("product-export"), {"category": self.electronics.pk}, HTTP_ACCEPT="text/csv")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(self.read(resp).strip().splitlines()), 2)

    def test_export_requires_auth(self):
        self.assertEqual(APIClient().get(reverse("product-export")).status_code, 401)

    def test_export_then_import_round_trip(self):
        self.make_products(4)
        csv_text = self.read(self.client.get(reverse("product-export")))
        Product.objects.all().delete()
        resp = self.do_import(csv_text)
        self.assertEqual(resp.data["inserted_count"], 4)
        self.assertEqual(resp.data["failed_count"], 0)

    def test_export_neutralises_formula_injection(self):
        Product.objects.create(sku="F-1", name="=HYPERLINK(1)", price=1, category=self.books)
        body = self.read(self.client.get(reverse("product-export")))
        self.assertIn("'=HYPERLINK(1)", body)


class ProductCrudTests(BaseAPITest):
    def test_create_list_filter_and_paginate(self):
        resp = self.client.post(
            reverse("product-list"),
            {"sku": "p-1", "name": "Pen", "price": "1.20", "category": self.books.pk},
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(resp.data["sku"], "P-1")
        for i in range(25):
            Product.objects.create(sku="Q-%02d" % i, name="Q", price=1, category=self.electronics)
        page = self.client.get(reverse("product-list"), {"page_size": 10})
        self.assertEqual(page.data["count"], 26)
        self.assertEqual(len(page.data["results"]), 10)
        filtered = self.client.get(reverse("product-list"), {"category": self.books.pk})
        self.assertEqual(filtered.data["count"], 1)

    def test_validation_error_format(self):
        resp = self.client.post(
            reverse("product-list"), {"sku": "p 1", "name": "", "price": "-1", "category": 999}, format="json"
        )
        self.assertEqual(resp.status_code, 400)
        details = resp.data["error"]["details"]
        for field in ("sku", "name", "price", "category"):
            self.assertIn(field, details)

    def test_cannot_delete_category_with_products(self):
        Product.objects.create(sku="P", name="P", price=1, category=self.books)
        resp = self.client.delete(reverse("category-detail", args=[self.books.pk]))
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.data["error"]["code"], "conflict")

    def test_invalid_filter_value_is_400_not_500(self):
        self.assertEqual(self.client.get(reverse("product-list"), {"category": "abc"}).status_code, 400)
        self.assertEqual(self.client.get(reverse("product-list"), {"ordering": "secret"}).status_code, 400)
