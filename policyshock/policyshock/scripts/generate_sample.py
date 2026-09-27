"""Generate SYNTHETIC sample data. Numbers here are made up; label them as sample data in the UI."""
import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
END = pd.Timestamp("2026-09-21")
START = END - pd.Timedelta(days=364)  # 365 days

PRODUCTS = {
    # category: (share of txns, median price, sigma, products)
    "Accessories": (0.30, 450, 0.60, ["Phone case", "Charger", "Earphones", "Power bank", "Screen guard"]),
    "Apparel": (0.22, 1400, 0.50, ["Jacket", "Jeans", "Sports shoes", "Formal shirt", "Kurta set"]),
    "Home Essentials": (0.16, 700, 0.50, ["Cookware set", "Bedsheet set", "Mixer jar", "Storage box", "Curtains"]),
    "Electronics": (0.13, 16000, 0.80, ["Smart TV 43in", "Laptop", "Soundbar", "Tablet", "Gaming console"]),
    "Appliances": (0.09, 22000, 0.60, ["Washing machine", "Refrigerator", "Air conditioner", "Microwave", "Water purifier"]),
    "Furniture": (0.06, 14000, 0.60, ["Sofa set", "Dining table", "Wardrobe", "Office chair", "Bed frame"]),
    "Mobiles": (0.04, 24000, 0.50, ["Smartphone A", "Smartphone B", "Smartphone C", "Foldable phone"]),
}


def _dates(rng, n, festive_boost=True):
    days = pd.date_range(START, END, freq="D")
    w = np.ones(len(days))
    w[days.dayofweek >= 5] *= 1.4
    if festive_boost:
        w[(days >= "2025-10-10") & (days <= "2025-11-05")] *= 2.2
    w /= w.sum()
    return rng.choice(days, size=n, p=w)


def retailer(rng, n=12500):
    cats = list(PRODUCTS)
    shares = np.array([PRODUCTS[c][0] for c in cats])
    shares /= shares.sum()
    cat = rng.choice(cats, size=n, p=shares)
    amount, product = [], []
    for c in cat:
        _, med, sig, prods = PRODUCTS[c]
        a = float(np.clip(rng.lognormal(np.log(med), sig), 99, 250000))
        amount.append(round(a / 10) * 10 if a > 1000 else round(a))
        product.append(rng.choice(prods))
    amount = np.array(amount, dtype=float)
    method = []
    for a in amount:
        if a >= 30000:
            probs = [0.25, 0.15, 0.55, 0.05]
        else:
            probs = [0.40, 0.20, 0.28, 0.12]
        method.append(rng.choice(["UPI_QR", "UPI_APP", "CARD", "CASH"], p=probs))
    df = pd.DataFrame({
        "transaction_id": [f"R{100000 + i}" for i in range(n)],
        "date": pd.to_datetime(_dates(rng, n)),
        "product": product, "category": cat, "amount": amount, "payment_method": method,
    }).sort_values("date").reset_index(drop=True)
    df["transaction_id"] = [f"R{100000 + i}" for i in range(len(df))]
    df["date"] = df["date"].dt.strftime("%Y-%m-%d")
    return df


def small_shop(rng, n=3200):
    items = {"Groceries": ["Rice 5kg", "Atta", "Oil 1L", "Dal", "Sugar"],
             "Dairy": ["Milk", "Curd", "Paneer", "Butter"],
             "Snacks": ["Biscuits", "Chips", "Namkeen", "Chocolates"]}
    cats = rng.choice(list(items), size=n, p=[0.55, 0.25, 0.20])
    amount = np.clip(rng.lognormal(np.log(170), 0.75, size=n), 10, 1800).round()
    prod = [rng.choice(items[c]) for c in cats]
    df = pd.DataFrame({"date": pd.to_datetime(_dates(rng, n, festive_boost=False)),
                       "product": prod, "category": cats, "amount": amount,
                       "payment_method": rng.choice(["UPI_QR", "CASH", "UPI_APP"], size=n, p=[0.68, 0.27, 0.05])})
    # a few festive bulk orders that cross INR 2,000
    k = 14
    bulk = pd.DataFrame({
        "date": pd.to_datetime(rng.choice(pd.date_range(START, END), size=k)),
        "product": "Festival hamper / bulk order", "category": "Groceries",
        "amount": rng.integers(25, 60, size=k) * 100.0, "payment_method": "UPI_QR"})
    df = pd.concat([df, bulk]).sort_values("date").reset_index(drop=True)
    df.insert(0, "transaction_id", [f"S{500000 + i}" for i in range(len(df))])
    df["date"] = df["date"].dt.strftime("%Y-%m-%d")
    return df


def investment_platform(rng, n_sip=4000, n_onetime=350):
    """A mutual-fund/broking platform: mostly Autopay SIPs (exempt) plus occasional
    one-time top-ups (capital-markets 0.02% rate). Demonstrates R-00, R-09 and R-10."""
    sip_amounts = np.clip(rng.lognormal(np.log(5000), 0.6, size=n_sip), 500, 100000).round(-1)
    sip_products = rng.choice(
        ["Flexi Cap Fund SIP", "Index Fund SIP", "ELSS SIP", "Debt Fund SIP", "Small Cap Fund SIP"],
        size=n_sip)
    sip = pd.DataFrame({"date": pd.to_datetime(_dates(rng, n_sip, festive_boost=False)),
                        "product": sip_products, "category": "Mutual Fund SIP",
                        "amount": sip_amounts, "payment_method": "UPI_AUTOPAY"})

    onetime_amounts = np.clip(rng.lognormal(np.log(25000), 1.0, size=n_onetime), 2500, 3000000).round(-2)
    onetime_products = rng.choice(
        ["One-time fund purchase", "Demat top-up", "IPO application", "Broker wallet top-up"],
        size=n_onetime)
    onetime = pd.DataFrame({"date": pd.to_datetime(_dates(rng, n_onetime, festive_boost=False)),
                            "product": onetime_products, "category": "One-time Investment",
                            "amount": onetime_amounts,
                            "payment_method": rng.choice(["UPI_APP", "UPI_QR"], size=n_onetime, p=[0.85, 0.15])})

    onetime = pd.concat([onetime, pd.DataFrame({
        "date": pd.to_datetime(rng.choice(pd.date_range(START, END), size=6)),
        "product": "IPO application (HNI category)", "category": "One-time Investment",
        "amount": rng.integers(1800000, 3500000, size=6).astype(float),  # above the ~15L cap breakeven
        "payment_method": "UPI_APP",
    })]).sort_values("date").reset_index(drop=True)

    df = pd.concat([sip, onetime]).sort_values("date").reset_index(drop=True)
    df.insert(0, "transaction_id", [f"I{700000 + i}" for i in range(len(df))])
    df["date"] = df["date"].dt.strftime("%Y-%m-%d")
    return df


def main():
    rng = np.random.default_rng(2026)
    DATA.mkdir(exist_ok=True)
    r, s = retailer(rng), small_shop(rng)
    i = investment_platform(rng)
    r.to_csv(DATA / "sample_retailer.csv", index=False)
    s.to_csv(DATA / "sample_small_shop.csv", index=False)
    i.to_csv(DATA / "sample_investment_platform.csv", index=False)
    (DATA / "merchant_retailer.json").write_text(json.dumps(
        {"name": "Sample retailer (synthetic data)", "sector": "general_retail",
         "qr_settles_directly_to_account": False}, indent=2))
    (DATA / "merchant_small_shop.json").write_text(json.dumps(
        {"name": "Sample small shop (synthetic data)", "sector": "general_retail",
         "qr_settles_directly_to_account": True}, indent=2))
    (DATA / "merchant_investment_platform.json").write_text(json.dumps(
        {"name": "Sample investment platform (synthetic data)", "sector": "capital_markets",
         "qr_settles_directly_to_account": False}, indent=2))
    # sanity: the small shop must really sit under the monthly QR limit
    s["m"] = s["date"].str[:7]
    qr = s[s["payment_method"] == "UPI_QR"].groupby("m")["amount"].sum()
    print("small shop max monthly UPI_QR receipts:", int(qr.max()))
    print("rows:", len(r), len(s), len(i))
    print("investment platform: autopay rows =", (i["payment_method"] == "UPI_AUTOPAY").sum(),
         "one-time rows =", (i["payment_method"] != "UPI_AUTOPAY").sum())


if __name__ == "__main__":
    main()
