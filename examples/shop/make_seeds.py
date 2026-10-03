"""Regenerates seeds/customers.csv and seeds/orders.csv (deterministic)."""
import csv
import random
from datetime import date, timedelta
from pathlib import Path

rng = random.Random(20261003)
here = Path(__file__).parent / "seeds"
here.mkdir(exist_ok=True)

countries = ["US", "DE", "FR", "KR", "GB"]
customers = [{"customer_id": 100 + i, "country": rng.choice(countries)} for i in range(60)]
with (here / "customers.csv").open("w", newline="") as fh:
    w = csv.DictWriter(fh, fieldnames=["customer_id", "country"], lineterminator="\n")
    w.writeheader()
    w.writerows(customers)

statuses = ["placed", "shipped", "delivered", "returned", "cancelled"]
weights = [15, 25, 50, 5, 5]
start = date(2026, 8, 1)
orders = []
for i in range(240):
    c = rng.choice(customers)
    currency = "EUR" if c["country"] in ("DE", "FR") else "USD"
    amount = round(rng.lognormvariate(4.3, 0.7), 2)   # median about 74
    amount = min(max(amount, 5.0), 1800.0)
    orders.append({
        "order_id": 50000 + i,
        "customer_id": c["customer_id"],
        "ordered_at": (start + timedelta(days=rng.randrange(0, 58))).isoformat(),
        "status": rng.choices(statuses, weights)[0],
        "currency": currency,
        "amount": f"{amount:.2f}",
    })
with (here / "orders.csv").open("w", newline="") as fh:
    w = csv.DictWriter(fh, fieldnames=list(orders[0]), lineterminator="\n")
    w.writeheader()
    w.writerows(orders)
print(len(customers), "customers,", len(orders), "orders")
