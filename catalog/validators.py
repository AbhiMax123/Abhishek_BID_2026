"""
Pure-Python row/header validation for the product CSV.

Deliberately has NO Django imports so it is fast, trivially unit-testable,
and reusable (the serializers and the importer both use it).
"""
import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

REQUIRED_COLUMNS = ("sku", "name", "price", "category")
OPTIONAL_COLUMNS = ("stock", "is_active")
KNOWN_COLUMNS = REQUIRED_COLUMNS + OPTIONAL_COLUMNS

SKU_MAX_LENGTH = 64
NAME_MAX_LENGTH = 255
MAX_PRICE = Decimal("9999999999.99")  # DecimalField(max_digits=12, decimal_places=2)
MAX_STOCK = 2_147_483_647

SKU_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9._\-/]*$")
PRICE_PATTERN = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)$")
INT_PATTERN = re.compile(r"^\d+$")

TRUE_VALUES = {"true", "1", "yes", "y", "t"}
FALSE_VALUES = {"false", "0", "no", "n", "f"}


@dataclass
class ParsedRow:
    sku: Optional[str] = None
    name: Optional[str] = None
    price: Optional[Decimal] = None
    category: Optional[str] = None
    stock: Optional[int] = None
    is_active: Optional[bool] = None
    issues: List[Dict[str, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.issues


# ---------------------------------------------------------------- headers
def normalize_header(value: str) -> str:
    return value.replace("\ufeff", "").strip().lower()


def normalize_headers(raw_headers) -> List[str]:
    return [normalize_header(h) for h in raw_headers]


def check_headers(headers: List[str]) -> List[str]:
    """Return a list of human-readable problems (empty list = headers are OK)."""
    if not any(headers):
        return ["The file has no header row."]
    problems = []
    missing = [c for c in REQUIRED_COLUMNS if c not in headers]
    if missing:
        problems.append(
            "Missing required column(s): %s. Required columns are: %s."
            % (", ".join(missing), ", ".join(REQUIRED_COLUMNS))
        )
    duplicated = sorted({h for h in headers if h and headers.count(h) > 1})
    if duplicated:
        problems.append("Duplicate column(s) in header: %s." % ", ".join(duplicated))
    return problems


# ------------------------------------------------------------------- cells
def clean_sku(value: str) -> Tuple[Optional[str], Optional[str]]:
    sku = (value or "").strip().upper()
    if not sku:
        return None, "SKU is required."
    if len(sku) > SKU_MAX_LENGTH:
        return None, "SKU must be at most %d characters." % SKU_MAX_LENGTH
    if not SKU_PATTERN.match(sku):
        return None, "SKU may only contain letters, digits and . _ - / (no spaces)."
    return sku, None


def clean_price(value: str) -> Tuple[Optional[Decimal], Optional[str]]:
    text = (value or "").strip()
    if not text:
        return None, "Price is required."
    if not PRICE_PATTERN.match(text):
        return None, "Price %r is not a valid number." % text[:30]
    if "." in text and len(text.split(".", 1)[1].rstrip("0")) > 2:
        return None, "Price must have at most 2 decimal places."
    price = Decimal(text).quantize(Decimal("0.01"))
    if price < 0:
        return None, "Price must not be negative."
    if price == 0:
        price = Decimal("0.00")  # normalise "-0"
    if price > MAX_PRICE:
        return None, "Price is too large."
    return price, None


def clean_stock(value: str) -> Tuple[Optional[int], Optional[str]]:
    text = (value or "").strip()
    if not text:
        return None, None  # optional
    if not INT_PATTERN.match(text):
        return None, "Stock must be a whole number >= 0."
    number = int(text)
    if number > MAX_STOCK:
        return None, "Stock is too large."
    return number, None


def clean_bool(value: str) -> Tuple[Optional[bool], Optional[str]]:
    text = (value or "").strip().lower()
    if not text:
        return None, None  # optional
    if text in TRUE_VALUES:
        return True, None
    if text in FALSE_VALUES:
        return False, None
    return None, "is_active must be true/false (or 1/0, yes/no)."


# --------------------------------------------------------------------- row
def validate_row(raw: Dict[str, str]) -> ParsedRow:
    """Validate one CSV row (dict of normalised header -> cell). Collects ALL issues."""
    row = ParsedRow()

    def add(field_name, message):
        row.issues.append({"field": field_name, "message": message})

    row.sku, err = clean_sku(raw.get("sku", ""))
    if err:
        add("sku", err)

    name = (raw.get("name") or "").strip()
    if not name:
        add("name", "Name is required.")
    elif len(name) > NAME_MAX_LENGTH:
        add("name", "Name must be at most %d characters." % NAME_MAX_LENGTH)
    else:
        row.name = name

    row.price, err = clean_price(raw.get("price", ""))
    if err:
        add("price", err)

    category = (raw.get("category") or "").strip()
    if not category:
        add("category", "Category is required.")
    else:
        row.category = category

    row.stock, err = clean_stock(raw.get("stock", ""))
    if err:
        add("stock", err)

    row.is_active, err = clean_bool(raw.get("is_active", ""))
    if err:
        add("is_active", err)

    return row
