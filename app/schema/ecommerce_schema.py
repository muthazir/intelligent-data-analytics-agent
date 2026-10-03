# ecommerce_schema.py
# --------------------
# Schema definition for D2C (Direct-to-Consumer) ecommerce CSV uploads.
#
# Structure follows cafe_schema.py flat format — one key per column,
# each containing: dtype, required, nullable, and optional constraints.
#
# Columns overview:
#   Core order data     → order_id, order_date, order_status
#   Customer data       → customer_id, customer_email
#   Product data        → product_id, product_name, category
#   Financials          → unit_price, quantity, discount, total_amount
#   Logistics           → payment_method, shipping_city, shipping_state
#   Post-sale           → return_requested

ECOMMERCE_SCHEMA = {

    # ------------------------------------------------------------------
    # Core Order Data
    # ------------------------------------------------------------------

    "order_id": {
        "aliases": ["order_no", "order_number", "invoice_id", "invoice_no", "transaction_id"],
        "dtype": "string",
        "required": True,
        "nullable": False,
        "unique": True                  # every order must be distinct
    },

    "order_date": {
        "aliases": ["date", "order_dt", "purchase_date", "transaction_date", "order_datetime", "orderdate"],
        "dtype": "datetime",
        "required": True,
        "nullable": False
    },

    "order_status": {
        "aliases": ["status", "orderstatus", "delivery_status"],
        "dtype": "string",
        "required": True,
        "nullable": False,
        "allowed_values": [
            "pending",
            "confirmed",
            "shipped",
            "delivered",
            "cancelled",
            "returned"
        ]
    },

    # ------------------------------------------------------------------
    # Customer Data
    # ------------------------------------------------------------------

    "customer_id": {
        "aliases": ["cust_id", "customer", "customerid", "client_id", "buyer_id"],
        "dtype": "string",
        "required": True,
        "nullable": False
    },

    "customer_email": {
        "aliases": ["email", "email_id", "email_address", "customer_mail", "mail"],
        "dtype": "string",
        "required": False,
        "nullable": True,
        "regex": r"^[\w\.-]+@[\w\.-]+\.\w{2,}$"   # validated in Phase 2
    },

    # ------------------------------------------------------------------
    # Product Data
    # ------------------------------------------------------------------

    "product_id": {
        "aliases": ["prod_id", "productid", "sku", "item_id"],
        "dtype": "string",
        "required": True,
        "nullable": False
    },

    "product_name": {
        "aliases": ["product", "item", "item_name", "productname", "product_title"],
        "dtype": "string",
        "required": True,
        "nullable": False
    },

    "category": {
        "aliases": ["product_category", "category_name"],
        "dtype": "string",
        "required": True,
        "nullable": False,
        "allowed_values": [
            "electronics",
            "clothing",
            "footwear",
            "beauty",
            "home",
            "sports",
            "food",
            "books",
            "toys",
            "other"
        ]
    },

    # ------------------------------------------------------------------
    # Financials
    # ------------------------------------------------------------------

    "unit_price": {
        "aliases": ["price", "unitprice", "mrp", "item_price", "selling_price"],
        "dtype": "float",
        "required": True,
        "nullable": False,
        "min": 0.0
    },

    "quantity": {
        "aliases": ["qty", "units", "quantity_ordered"],
        "dtype": "int",
        "required": True,
        "nullable": False,
        "min": 1,
        "max": 10000
    },

    "discount": {
        "aliases": ["discount_pct", "discount_percent", "discount_rate", "disc"],
        "dtype": "float",
        "required": False,
        "nullable": True,
        "min": 0.0,
        "max": 100.0                    # discount is a percentage — 0 to 100
    },

    "total_amount": {
        "aliases": ["amount", "total", "order_total", "totalamount", "total_price", "order_value", "net_amount"],
        "dtype": "float",
        "required": True,
        "nullable": False,
        "min": 0.0
        # cross-column rule: total_amount ≈ unit_price * quantity * (1 - discount/100)
        # enforced in Phase 3 cross_rules, not here
    },

    # ------------------------------------------------------------------
    # Logistics
    # ------------------------------------------------------------------

    "payment_method": {
        "aliases": ["payment", "payment_mode", "payment_type", "pay_method", "paymentmethod"],
        "dtype": "string",
        "required": True,
        "nullable": False,
        "allowed_values": [
            "credit_card",
            "debit_card",
            "upi",
            "net_banking",
            "wallet",
            "cod",              # cash on delivery — common in Indian D2C
            "emi"
        ]
    },

    "shipping_city": {
        "aliases": ["city", "ship_city", "delivery_city"],
        "dtype": "string",
        "required": False,
        "nullable": True
    },

    "shipping_state": {
        "aliases": ["state", "ship_state", "delivery_state"],
        "dtype": "string",
        "required": False,
        "nullable": True
    },

    # ------------------------------------------------------------------
    # Post-Sale
    # ------------------------------------------------------------------

    "return_requested": {
        "aliases": ["returned", "is_returned", "return", "return_flag"],
        "dtype": "string",
        "required": False,
        "nullable": True,
        "allowed_values": [
            "yes",
            "no"
        ]
    },
}