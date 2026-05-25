"""Shared schema for the taxi trips table across all systems.

The parquet schema from NYC TLC isn't friendly to all targets — some
columns use DOUBLE for naturally-integer fields (passenger_count, etc.).
We normalize to a portable column set: small ints where possible,
TIMESTAMP for the pickup/dropoff cols, NUMERIC for money.

Each system needs slightly different DDL — keep it here so all loaders
share one source of truth.
"""

# Columns we keep, in the order they should appear in DDL + COPY.
# Each entry is (column_name, parquet_type, normalized_pg_type).
COLUMNS = [
    ("vendor_id",             "BIGINT",    "smallint"),
    ("tpep_pickup_datetime",  "TIMESTAMP", "timestamp"),
    ("tpep_dropoff_datetime", "TIMESTAMP", "timestamp"),
    ("passenger_count",       "DOUBLE",    "smallint"),
    ("trip_distance",         "DOUBLE",    "double precision"),
    ("ratecode_id",           "DOUBLE",    "smallint"),
    ("store_and_fwd_flag",    "VARCHAR",   "text"),
    ("pu_location_id",        "BIGINT",    "smallint"),
    ("do_location_id",        "BIGINT",    "smallint"),
    ("payment_type",          "BIGINT",    "smallint"),
    ("fare_amount",           "DOUBLE",    "double precision"),
    ("extra",                 "DOUBLE",    "double precision"),
    ("mta_tax",               "DOUBLE",    "double precision"),
    ("tip_amount",            "DOUBLE",    "double precision"),
    ("tolls_amount",          "DOUBLE",    "double precision"),
    ("improvement_surcharge", "DOUBLE",    "double precision"),
    ("total_amount",          "DOUBLE",    "double precision"),
    ("congestion_surcharge",  "DOUBLE",    "double precision"),
    ("airport_fee",           "DOUBLE",    "double precision"),
]

# Parquet column → our column rename map (parquet uses CamelCase here).
PARQUET_TO_OURS = {
    "VendorID":              "vendor_id",
    "tpep_pickup_datetime":  "tpep_pickup_datetime",
    "tpep_dropoff_datetime": "tpep_dropoff_datetime",
    "passenger_count":       "passenger_count",
    "trip_distance":         "trip_distance",
    "RatecodeID":            "ratecode_id",
    "store_and_fwd_flag":    "store_and_fwd_flag",
    "PULocationID":          "pu_location_id",
    "DOLocationID":          "do_location_id",
    "payment_type":          "payment_type",
    "fare_amount":           "fare_amount",
    "extra":                 "extra",
    "mta_tax":               "mta_tax",
    "tip_amount":            "tip_amount",
    "tolls_amount":          "tolls_amount",
    "improvement_surcharge": "improvement_surcharge",
    "total_amount":          "total_amount",
    "congestion_surcharge":  "congestion_surcharge",
    "airport_fee":           "airport_fee",
}


def ddl_postgres(table: str = "trips", using: str | None = None) -> str:
    """Generate CREATE TABLE for any PG-flavored system. `using` adds a
    USING clause (e.g. 'rvbbit', 'columnar' for Citus/Hydra)."""
    cols = ",\n    ".join(f"{name} {pg_type}" for name, _, pg_type in COLUMNS)
    using_clause = f"\nUSING {using}" if using else ""
    return f"CREATE TABLE IF NOT EXISTS {table} (\n    {cols}\n){using_clause}"


def ddl_clickhouse(table: str = "trips", engine: str = "MergeTree") -> str:
    """ClickHouse DDL — uses ClickHouse type names and an engine clause."""
    # Map our PG types to CH equivalents.
    ch_map = {
        "smallint": "Int16",
        "double precision": "Float64",
        "text": "String",
        "timestamp": "DateTime64(0)",
    }
    cols = ",\n    ".join(f"{name} Nullable({ch_map[t]})" for name, _, t in COLUMNS)
    return (
        f"CREATE TABLE IF NOT EXISTS {table} (\n    {cols}\n) "
        f"ENGINE = {engine} ORDER BY tpep_pickup_datetime "
        f"SETTINGS allow_nullable_key = 1"
    )
