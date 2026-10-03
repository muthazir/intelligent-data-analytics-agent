# gym_schema.py
# -------------
# Schema definition for gym / fitness membership CSV uploads.
#
# REFACTORED: Migrated from nested structure (required_columns / optional_columns / constraints)
# to flat per-column structure matching cafe_schema.py and ecommerce_schema.py.
#
# Old structure (broken — validator could not read):
#   GYM_SCHEMA = { "required_columns": {...}, "optional_columns": {...}, "constraints": {...} }
#
# New structure (flat — one key per column, validator reads directly):
#   GYM_SCHEMA = { "member_id": { "dtype": ..., "required": ..., ... }, ... }
#
# All original constraints preserved — nothing lost, just restructured:
#   age_min / age_max              → "min" / "max" on "age" column
#   valid_gender                   → "allowed_values" on "gender" column
#   valid_membership_type          → "allowed_values" on "membership_type" column
#   valid_payment_status           → "allowed_values" on "payment_status" column
#   monthly_fee_positive           → "min": 0.0 on "monthly_fee" column
#
# Columns overview:
#   Member identity     → member_id, member_name, gender, age
#   Membership details  → membership_type, membership_start_date, membership_end_date
#   Financials          → monthly_fee, payment_status
#   Optional / fitness  → trainer_assigned, height_cm, weight_kg, attendance_days, fitness_goal

GYM_SCHEMA = {

    # ------------------------------------------------------------------
    # Member Identity
    # ------------------------------------------------------------------

    "member_id": {
        "aliases": ["memberid", "member_no", "membership_id"],
        "dtype": "string",
        "required": True,
        "nullable": False,
        "unique": True                  # each member must have a distinct ID
    },

    "member_name": {
        "aliases": ["name", "full_name", "membername"],
        "dtype": "string",
        "required": True,
        "nullable": False
    },

    "gender": {
        "aliases": ["sex"],
        "dtype": "string",
        "required": True,
        "nullable": False,
        "allowed_values": [             # from old constraints.valid_gender
            "male",
            "female",
            "other"
        ]
    },

    "age": {
        "aliases": ["member_age"],
        "dtype": "int",
        "required": True,
        "nullable": False,
        "min": 14,                      # from old constraints.age_min
        "max": 80                       # from old constraints.age_max
    },

    # ------------------------------------------------------------------
    # Membership Details
    # ------------------------------------------------------------------

    "membership_type": {
        "aliases": ["plan", "plan_type", "membership", "membership_plan"],
        "dtype": "string",
        "required": True,
        "nullable": False,
        "allowed_values": [             # from old constraints.valid_membership_type
            "monthly",
            "quarterly",
            "yearly"
        ]
    },

    "membership_start_date": {
        "aliases": ["start_date", "join_date", "joining_date", "membership_start"],
        "dtype": "datetime",
        "required": True,
        "nullable": False
        # cross-column rule: membership_start_date < membership_end_date
        # enforced in Phase 3 cross_rules, not here
    },

    "membership_end_date": {
        "aliases": ["end_date", "expiry_date", "membership_end", "expiry"],
        "dtype": "datetime",
        "required": True,
        "nullable": False
    },

    # ------------------------------------------------------------------
    # Financials
    # ------------------------------------------------------------------

    "monthly_fee": {
        "aliases": ["fee", "fees", "monthly_fees", "membership_fee"],
        "dtype": "float",
        "required": True,
        "nullable": False,
        "min": 0.0                      # from old constraints.monthly_fee_positive
    },

    "payment_status": {
        "aliases": ["fee_status", "payment"],
        "dtype": "string",
        "required": True,
        "nullable": False,
        "allowed_values": [             # from old constraints.valid_payment_status
            "paid",
            "pending",
            "overdue"
        ]
    },

    # ------------------------------------------------------------------
    # Optional — Trainer & Physical Stats
    # ------------------------------------------------------------------

    "trainer_assigned": {
        "aliases": ["trainer", "trainer_name", "coach"],
        "dtype": "string",
        "required": False,
        "nullable": True
    },

    "height_cm": {
        "aliases": ["height"],
        "dtype": "float",
        "required": False,
        "nullable": True,
        "min": 50.0,                    # reasonable human height floor
        "max": 250.0                    # reasonable human height ceiling
    },

    "weight_kg": {
        "aliases": ["weight"],
        "dtype": "float",
        "required": False,
        "nullable": True,
        "min": 20.0,                    # reasonable human weight floor
        "max": 300.0                    # reasonable human weight ceiling
    },

    # ------------------------------------------------------------------
    # Optional — Activity & Goals
    # ------------------------------------------------------------------

    "attendance_days": {
        "aliases": ["attendance", "days_attended", "visits"],
        "dtype": "int",
        "required": False,
        "nullable": True,
        "min": 0,
        "max": 31                       # max days in a calendar month
    },

    "fitness_goal": {
        "aliases": ["goal"],
        "dtype": "string",
        "required": False,
        "nullable": True,
        "allowed_values": [
            "weight_loss",
            "muscle_gain",
            "endurance",
            "flexibility",
            "general_fitness",
            "rehabilitation",
            "other"
        ]
    },
}