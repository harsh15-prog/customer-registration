import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal


MIN_DOB = date(1900, 1, 1)
EMAIL = re.compile(r"[a-zA-Z0-9.!#$%&'*+/=?^_`{|}~-]+@"
                   r"(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]*[a-zA-Z0-9])?\.)+"
                   r"[a-zA-Z]{2,}")
BALANCE = re.compile(r"[0-9]{1,12}(?:\.[0-9]{1,2})?")
ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


@dataclass(frozen=True)
class Customer:
    name: str
    email: str
    dob: date
    age: int
    balance: Decimal

    def identity(self):
        return [self.name, self.email, self.dob.isoformat(), str(self.balance)]


def validate_customer(data, today):
    errors = {}
    name, email, dob_text, amount = (
        data.get(key) for key in ("name", "email", "dob", "balance")
    )
    name = name.strip() if isinstance(name, str) else ""
    email = email.strip() if isinstance(email, str) else ""
    if not name or len(name) > 50 or any(ord(char) < 32 for char in name):
        errors["name"] = "Enter a full name with 1 to 50 characters."
    if len(email) > 50 or not EMAIL.fullmatch(email):
        errors["email"] = "Enter a valid email address with up to 50 characters."

    dob = None
    if isinstance(dob_text, str) and ISO_DATE.fullmatch(dob_text):
        try:
            dob = date.fromisoformat(dob_text)
        except ValueError:
            pass
    if dob is None or not MIN_DOB <= dob <= today:
        errors["dob"] = "Enter a real date from 1900 up to today."

    if type(amount) in (int, float):
        amount = str(amount)
    if not isinstance(amount, str) or not BALANCE.fullmatch(amount.strip()):
        errors["balance"] = (
            "Enter a non-negative balance with up to 12 digits before the decimal "
            "and up to 2 decimal places."
        )
    if errors:
        return None, errors

    local_part, domain = email.rsplit("@", 1)
    age = today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
    balance = Decimal(amount.strip()).quantize(Decimal("0.01"))
    return Customer(name, f"{local_part}@{domain.lower()}", dob, age, balance), {}
