"""ClickBench hits table schema.

105 columns: a mix of small ints (Yandex Metrika encoded values),
strings (URLs, titles, search phrases), timestamps, and a few BIGINTs.

Source: https://github.com/ClickHouse/ClickBench/blob/main/postgresql/create.sql

NOT NULL stripped: the upstream parquet uses NULLs in places the
DDL claims NOT NULL. We don't enforce nullability for the bench.
"""

# (column_name, pg_type) in declaration order
COLUMNS = [
    ("WatchID", "bigint"),
    ("JavaEnable", "smallint"),
    ("Title", "text"),
    ("GoodEvent", "smallint"),
    ("EventTime", "timestamp"),
    ("EventDate", "date"),
    ("CounterID", "integer"),
    ("ClientIP", "integer"),
    ("RegionID", "integer"),
    ("UserID", "bigint"),
    ("CounterClass", "smallint"),
    ("OS", "smallint"),
    ("UserAgent", "smallint"),
    ("URL", "text"),
    ("Referer", "text"),
    ("IsRefresh", "smallint"),
    ("RefererCategoryID", "smallint"),
    ("RefererRegionID", "integer"),
    ("URLCategoryID", "smallint"),
    ("URLRegionID", "integer"),
    ("ResolutionWidth", "smallint"),
    ("ResolutionHeight", "smallint"),
    ("ResolutionDepth", "smallint"),
    ("FlashMajor", "smallint"),
    ("FlashMinor", "smallint"),
    ("FlashMinor2", "text"),
    ("NetMajor", "smallint"),
    ("NetMinor", "smallint"),
    ("UserAgentMajor", "smallint"),
    ("UserAgentMinor", "text"),
    ("CookieEnable", "smallint"),
    ("JavascriptEnable", "smallint"),
    ("IsMobile", "smallint"),
    ("MobilePhone", "smallint"),
    ("MobilePhoneModel", "text"),
    ("Params", "text"),
    ("IPNetworkID", "integer"),
    ("TraficSourceID", "smallint"),
    ("SearchEngineID", "smallint"),
    ("SearchPhrase", "text"),
    ("AdvEngineID", "smallint"),
    ("IsArtifical", "smallint"),
    ("WindowClientWidth", "smallint"),
    ("WindowClientHeight", "smallint"),
    ("ClientTimeZone", "smallint"),
    ("ClientEventTime", "timestamp"),
    ("SilverlightVersion1", "smallint"),
    ("SilverlightVersion2", "smallint"),
    ("SilverlightVersion3", "integer"),
    ("SilverlightVersion4", "smallint"),
    ("PageCharset", "text"),
    ("CodeVersion", "integer"),
    ("IsLink", "smallint"),
    ("IsDownload", "smallint"),
    ("IsNotBounce", "smallint"),
    ("FUniqID", "bigint"),
    ("OriginalURL", "text"),
    ("HID", "integer"),
    ("IsOldCounter", "smallint"),
    ("IsEvent", "smallint"),
    ("IsParameter", "smallint"),
    ("DontCountHits", "smallint"),
    ("WithHash", "smallint"),
    ("HitColor", "text"),  # CHAR in upstream; treated as text for portability
    ("LocalEventTime", "timestamp"),
    ("Age", "smallint"),
    ("Sex", "smallint"),
    ("Income", "smallint"),
    ("Interests", "smallint"),
    ("Robotness", "smallint"),
    ("RemoteIP", "integer"),
    ("WindowName", "integer"),
    ("OpenerName", "integer"),
    ("HistoryLength", "smallint"),
    ("BrowserLanguage", "text"),
    ("BrowserCountry", "text"),
    ("SocialNetwork", "text"),
    ("SocialAction", "text"),
    ("HTTPError", "smallint"),
    ("SendTiming", "integer"),
    ("DNSTiming", "integer"),
    ("ConnectTiming", "integer"),
    ("ResponseStartTiming", "integer"),
    ("ResponseEndTiming", "integer"),
    ("FetchTiming", "integer"),
    ("SocialSourceNetworkID", "smallint"),
    ("SocialSourcePage", "text"),
    ("ParamPrice", "bigint"),
    ("ParamOrderID", "text"),
    ("ParamCurrency", "text"),
    ("ParamCurrencyID", "smallint"),
    ("OpenstatServiceName", "text"),
    ("OpenstatCampaignID", "text"),
    ("OpenstatAdID", "text"),
    ("OpenstatSourceID", "text"),
    ("UTMSource", "text"),
    ("UTMMedium", "text"),
    ("UTMCampaign", "text"),
    ("UTMContent", "text"),
    ("UTMTerm", "text"),
    ("FromTag", "text"),
    ("HasGCLID", "smallint"),
    ("RefererHash", "bigint"),
    ("URLHash", "bigint"),
    ("CLID", "integer"),
]


# DuckDB SELECT expression to use when reading a column out of the
# upstream hits.parquet. Defaults to the bare column name; overrides
# below convert epoch-int storage to typed TIMESTAMP / DATE values
# that PG COPY can ingest into the declared schema.
DUCKDB_READ_CAST: dict[str, str] = {
    "EventTime":       'to_timestamp("EventTime")',
    "ClientEventTime": 'to_timestamp("ClientEventTime")',
    "LocalEventTime":  'to_timestamp("LocalEventTime")',
    "EventDate":       "(DATE '1970-01-01' + INTERVAL (\"EventDate\") DAY)::DATE",
}


def duckdb_select_list() -> str:
    """SELECT-list expressions for reading hits.parquet through DuckDB,
    aliased back to the canonical column names in our DDL."""
    parts = []
    for name, _t in COLUMNS:
        expr = DUCKDB_READ_CAST.get(name, f'"{name}"')
        parts.append(f'{expr} AS "{name}"')
    return ", ".join(parts)


def ddl_postgres(table: str = "hits", using: str | None = None) -> str:
    # Quote the column names — they're mixed-case and PG folds unquoted
    # identifiers to lowercase, breaking the standard ClickBench queries.
    cols = ",\n    ".join(f'"{n}" {t}' for n, t in COLUMNS)
    using_clause = f"\nUSING {using}" if using else ""
    return f"CREATE TABLE IF NOT EXISTS {table} (\n    {cols}\n){using_clause}"


def ddl_clickhouse(table: str = "hits") -> str:
    ch_map = {
        "smallint": "Int16",
        "integer": "Int32",
        "bigint": "Int64",
        "text": "String",
        "date": "Date",
        "timestamp": "DateTime64(0)",
    }
    cols = ",\n    ".join(f'"{n}" Nullable({ch_map[t]})' for n, t in COLUMNS)
    return (
        f"CREATE TABLE IF NOT EXISTS {table} (\n    {cols}\n) "
        f"ENGINE = MergeTree ORDER BY tuple()"
    )
