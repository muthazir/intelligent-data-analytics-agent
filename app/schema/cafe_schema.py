CAFE_SCHEMA = {
    "order_id": {
        "aliases": ["order_no", "order_number", "bill_no", "bill_id", "invoice_no", "receipt_id", "transaction_id"],
        "dtype": "string",
        "required": True,
        "nullable": False
    },
    
    "order_date": {
        "aliases": ["date", "order_dt", "transaction_date", "bill_date"],
        "dtype": "datetime",
        "required": True,
        "nullable": False
    },

    "customer_id": {
        "aliases": ["cust_id", "customer", "customerid"],
        "dtype": "string",
        "required": False,
        "nullable": True
    },

    "item_name": {
        "aliases": ["item", "menu_item", "product", "product_name", "itemname"],
        "dtype": "string",
        "required": True,
        "nullable": False
    },

    "category": {
        "aliases": ["item_category", "category_name"],
        "dtype": "string",
        "required": True,
        "nullable": False,
        "allowed_values": [
            "coffee",
            "tea",
            "snacks",
            "dessert",
            "sandwich",
            "other"
        ]
    },

    "price": {
        "aliases": ["unit_price", "item_price", "rate", "mrp"],
        "dtype": "float",
        "required": True,
        "nullable": False,
        "min": 0
    },

    "quantity": {
        "aliases": ["qty", "units"],
        "dtype": "int",
        "required": True,
        "nullable": False,
        "min": 1
    },

    "payment_method": {
        "aliases": ["payment", "payment_mode", "payment_type"],
        "dtype": "string",
        "required": True,
        "nullable": False,
        "allowed_values": [
            "cash",
            "card",
            "upi",
            "wallet"
        ]
    },

    "store_location": {
        "aliases": ["store", "location", "outlet", "branch", "store_name"],
        "dtype": "string",
        "required": True,
        "nullable": False
    }
}