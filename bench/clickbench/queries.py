"""ClickBench standard query set — 43 SQL queries.

Adapted from the upstream postgresql/queries.sql so column names match
our quoted DDL (mixed-case identifiers preserved).

Source: https://github.com/ClickHouse/ClickBench/blob/main/postgresql/queries.sql
"""

# Wrap every column reference in double quotes so it survives PG's
# lowercasing rule, since our DDL preserves the upstream CamelCase.
# Numbered as Q0..Q42 to match ClickBench convention (Q0 is the first).
QUERIES = [
    ("Q0", "count(*)", 'SELECT COUNT(*) FROM hits'),
    ("Q1", "count(*) with filter", 'SELECT COUNT(*) FROM hits WHERE "AdvEngineID" <> 0'),
    ("Q2", "sum/count/avg over big table",
     'SELECT SUM("AdvEngineID"), COUNT(*), AVG("ResolutionWidth") FROM hits'),
    ("Q3", "avg(UserID)", 'SELECT AVG("UserID") FROM hits'),
    ("Q4", "count distinct UserID", 'SELECT COUNT(DISTINCT "UserID") FROM hits'),
    ("Q5", "count distinct SearchPhrase", 'SELECT COUNT(DISTINCT "SearchPhrase") FROM hits'),
    ("Q6", "min/max EventDate", 'SELECT MIN("EventDate"), MAX("EventDate") FROM hits'),
    ("Q7", "AdvEngineID group + filter",
     'SELECT "AdvEngineID", COUNT(*) FROM hits WHERE "AdvEngineID" <> 0 '
     'GROUP BY "AdvEngineID" ORDER BY COUNT(*) DESC'),
    ("Q8", "RegionID top 10 unique users",
     'SELECT "RegionID", COUNT(DISTINCT "UserID") AS u FROM hits '
     'GROUP BY "RegionID" ORDER BY u DESC LIMIT 10'),
    ("Q9", "RegionID rollup",
     'SELECT "RegionID", SUM("AdvEngineID"), COUNT(*) AS c, '
     'AVG("ResolutionWidth"), COUNT(DISTINCT "UserID") FROM hits '
     'GROUP BY "RegionID" ORDER BY c DESC LIMIT 10'),
    ("Q10", "MobilePhoneModel filter+groupby",
     'SELECT "MobilePhoneModel", COUNT(DISTINCT "UserID") AS u FROM hits '
     'WHERE "MobilePhoneModel" <> \'\' GROUP BY "MobilePhoneModel" '
     'ORDER BY u DESC LIMIT 10'),
    ("Q11", "MobilePhone+Model groupby",
     'SELECT "MobilePhone", "MobilePhoneModel", COUNT(DISTINCT "UserID") AS u '
     'FROM hits WHERE "MobilePhoneModel" <> \'\' '
     'GROUP BY "MobilePhone", "MobilePhoneModel" ORDER BY u DESC LIMIT 10'),
    ("Q12", "SearchPhrase top 10 count",
     'SELECT "SearchPhrase", COUNT(*) AS c FROM hits '
     'WHERE "SearchPhrase" <> \'\' GROUP BY "SearchPhrase" '
     'ORDER BY c DESC LIMIT 10'),
    ("Q13", "SearchPhrase top 10 unique users",
     'SELECT "SearchPhrase", COUNT(DISTINCT "UserID") AS u FROM hits '
     'WHERE "SearchPhrase" <> \'\' GROUP BY "SearchPhrase" '
     'ORDER BY u DESC LIMIT 10'),
    ("Q14", "SearchEngineID+Phrase top 10",
     'SELECT "SearchEngineID", "SearchPhrase", COUNT(*) AS c FROM hits '
     'WHERE "SearchPhrase" <> \'\' GROUP BY "SearchEngineID", "SearchPhrase" '
     'ORDER BY c DESC LIMIT 10'),
    ("Q15", "UserID top 10",
     'SELECT "UserID", COUNT(*) FROM hits GROUP BY "UserID" '
     'ORDER BY COUNT(*) DESC LIMIT 10'),
    ("Q16", "UserID+SearchPhrase ordered top 10",
     'SELECT "UserID", "SearchPhrase", COUNT(*) FROM hits '
     'GROUP BY "UserID", "SearchPhrase" ORDER BY COUNT(*) DESC LIMIT 10'),
    ("Q17", "UserID+SearchPhrase any 10",
     'SELECT "UserID", "SearchPhrase", COUNT(*) FROM hits '
     'GROUP BY "UserID", "SearchPhrase" LIMIT 10'),
    ("Q18", "UserID+minute+SearchPhrase groupby",
     'SELECT "UserID", extract(minute FROM "EventTime") AS m, "SearchPhrase", COUNT(*) '
     'FROM hits GROUP BY "UserID", m, "SearchPhrase" '
     'ORDER BY COUNT(*) DESC LIMIT 10'),
    ("Q19", "UserID point lookup",
     'SELECT "UserID" FROM hits WHERE "UserID" = 435090932899640449'),
    ("Q20", "LIKE URL %google%",
     'SELECT COUNT(*) FROM hits WHERE "URL" LIKE \'%google%\''),
    ("Q21", "LIKE URL + SearchPhrase top 10",
     'SELECT "SearchPhrase", MIN("URL"), COUNT(*) AS c FROM hits '
     'WHERE "URL" LIKE \'%google%\' AND "SearchPhrase" <> \'\' '
     'GROUP BY "SearchPhrase" ORDER BY c DESC LIMIT 10'),
    ("Q22", "LIKE Title + URL NOT LIKE rollup",
     'SELECT "SearchPhrase", MIN("URL"), MIN("Title"), COUNT(*) AS c, '
     'COUNT(DISTINCT "UserID") FROM hits WHERE "Title" LIKE \'%Google%\' '
     'AND "URL" NOT LIKE \'%.google.%\' AND "SearchPhrase" <> \'\' '
     'GROUP BY "SearchPhrase" ORDER BY c DESC LIMIT 10'),
    ("Q23", "SELECT * filter LIKE",
     'SELECT * FROM hits WHERE "URL" LIKE \'%google%\' ORDER BY "EventTime" LIMIT 10'),
    ("Q24", "SearchPhrase order by EventTime",
     'SELECT "SearchPhrase" FROM hits WHERE "SearchPhrase" <> \'\' '
     'ORDER BY "EventTime" LIMIT 10'),
    ("Q25", "SearchPhrase order by SearchPhrase",
     'SELECT "SearchPhrase" FROM hits WHERE "SearchPhrase" <> \'\' '
     'ORDER BY "SearchPhrase" LIMIT 10'),
    ("Q26", "SearchPhrase order EventTime+Phrase",
     'SELECT "SearchPhrase" FROM hits WHERE "SearchPhrase" <> \'\' '
     'ORDER BY "EventTime", "SearchPhrase" LIMIT 10'),
    ("Q27", "CounterID avg URL length",
     'SELECT "CounterID", AVG(length("URL")) AS l, COUNT(*) AS c FROM hits '
     'WHERE "URL" <> \'\' GROUP BY "CounterID" HAVING COUNT(*) > 100000 '
     'ORDER BY l DESC LIMIT 25'),
    ("Q28", "regex_replace Referer",
     "SELECT REGEXP_REPLACE(\"Referer\", '^https?://(?:www\\.)?([^/]+)/.*$', '\\1') AS k, "
     'AVG(length("Referer")) AS l, COUNT(*) AS c, MIN("Referer") FROM hits '
     "WHERE \"Referer\" <> '' GROUP BY k HAVING COUNT(*) > 100000 "
     'ORDER BY l DESC LIMIT 25'),
    ("Q29", "90 sum exprs (wide tlist)",
     'SELECT ' + ', '.join([f'SUM("ResolutionWidth" + {i})' for i in range(90)]) +
     ' FROM hits'),
    ("Q30", "SearchEngineID+ClientIP rollup",
     'SELECT "SearchEngineID", "ClientIP", COUNT(*) AS c, SUM("IsRefresh"), '
     'AVG("ResolutionWidth") FROM hits WHERE "SearchPhrase" <> \'\' '
     'GROUP BY "SearchEngineID", "ClientIP" ORDER BY c DESC LIMIT 10'),
    ("Q31", "WatchID+ClientIP rollup (filtered)",
     'SELECT "WatchID", "ClientIP", COUNT(*) AS c, SUM("IsRefresh"), '
     'AVG("ResolutionWidth") FROM hits WHERE "SearchPhrase" <> \'\' '
     'GROUP BY "WatchID", "ClientIP" ORDER BY c DESC LIMIT 10'),
    ("Q32", "WatchID+ClientIP rollup (full)",
     'SELECT "WatchID", "ClientIP", COUNT(*) AS c, SUM("IsRefresh"), '
     'AVG("ResolutionWidth") FROM hits GROUP BY "WatchID", "ClientIP" '
     'ORDER BY c DESC LIMIT 10'),
    ("Q33", "URL top 10",
     'SELECT "URL", COUNT(*) AS c FROM hits GROUP BY "URL" ORDER BY c DESC LIMIT 10'),
    ("Q34", "literal 1 + URL top 10",
     'SELECT 1, "URL", COUNT(*) AS c FROM hits GROUP BY 1, "URL" '
     'ORDER BY c DESC LIMIT 10'),
    ("Q35", "ClientIP+N rollups",
     'SELECT "ClientIP", "ClientIP" - 1, "ClientIP" - 2, "ClientIP" - 3, '
     'COUNT(*) AS c FROM hits GROUP BY "ClientIP", "ClientIP" - 1, '
     '"ClientIP" - 2, "ClientIP" - 3 ORDER BY c DESC LIMIT 10'),
    ("Q36", "selective filter date range",
     'SELECT "URL", COUNT(*) AS PageViews FROM hits '
     'WHERE "CounterID" = 62 AND "EventDate" >= \'2013-07-01\' '
     'AND "EventDate" <= \'2013-07-31\' AND "DontCountHits" = 0 '
     'AND "IsRefresh" = 0 AND "URL" <> \'\' GROUP BY "URL" '
     'ORDER BY PageViews DESC LIMIT 10'),
    ("Q37", "Title selective filter",
     'SELECT "Title", COUNT(*) AS PageViews FROM hits '
     'WHERE "CounterID" = 62 AND "EventDate" >= \'2013-07-01\' '
     'AND "EventDate" <= \'2013-07-31\' AND "DontCountHits" = 0 '
     'AND "IsRefresh" = 0 AND "Title" <> \'\' GROUP BY "Title" '
     'ORDER BY PageViews DESC LIMIT 10'),
    ("Q38", "URL link clicks offset 1000",
     'SELECT "URL", COUNT(*) AS PageViews FROM hits '
     'WHERE "CounterID" = 62 AND "EventDate" >= \'2013-07-01\' '
     'AND "EventDate" <= \'2013-07-31\' AND "IsRefresh" = 0 '
     'AND "IsLink" <> 0 AND "IsDownload" = 0 GROUP BY "URL" '
     'ORDER BY PageViews DESC LIMIT 10 OFFSET 1000'),
    ("Q39", "Src/Dst rollup",
     'SELECT "TraficSourceID", "SearchEngineID", "AdvEngineID", '
     'CASE WHEN ("SearchEngineID" = 0 AND "AdvEngineID" = 0) THEN "Referer" '
     'ELSE \'\' END AS Src, "URL" AS Dst, COUNT(*) AS PageViews FROM hits '
     'WHERE "CounterID" = 62 AND "EventDate" >= \'2013-07-01\' '
     'AND "EventDate" <= \'2013-07-31\' AND "IsRefresh" = 0 '
     'GROUP BY "TraficSourceID", "SearchEngineID", "AdvEngineID", Src, Dst '
     'ORDER BY PageViews DESC LIMIT 10 OFFSET 1000'),
    ("Q40", "URLHash+EventDate IN filter",
     'SELECT "URLHash", "EventDate", COUNT(*) AS PageViews FROM hits '
     'WHERE "CounterID" = 62 AND "EventDate" >= \'2013-07-01\' '
     'AND "EventDate" <= \'2013-07-31\' AND "IsRefresh" = 0 '
     'AND "TraficSourceID" IN (-1, 6) AND "RefererHash" = 3594120000172545465 '
     'GROUP BY "URLHash", "EventDate" ORDER BY PageViews DESC LIMIT 10 OFFSET 100'),
    ("Q41", "WindowClientWidth+Height filter",
     'SELECT "WindowClientWidth", "WindowClientHeight", COUNT(*) AS PageViews '
     'FROM hits WHERE "CounterID" = 62 AND "EventDate" >= \'2013-07-01\' '
     'AND "EventDate" <= \'2013-07-31\' AND "IsRefresh" = 0 '
     'AND "DontCountHits" = 0 AND "URLHash" = 2868770270353813622 '
     'GROUP BY "WindowClientWidth", "WindowClientHeight" '
     'ORDER BY PageViews DESC LIMIT 10 OFFSET 10000'),
    ("Q42", "DATE_TRUNC minute groupby",
     "SELECT DATE_TRUNC('minute', \"EventTime\") AS M, COUNT(*) AS PageViews "
     'FROM hits WHERE "CounterID" = 62 AND "EventDate" >= \'2013-07-14\' '
     'AND "EventDate" <= \'2013-07-15\' AND "IsRefresh" = 0 '
     "AND \"DontCountHits\" = 0 GROUP BY DATE_TRUNC('minute', \"EventTime\") "
     "ORDER BY DATE_TRUNC('minute', \"EventTime\") LIMIT 10 OFFSET 1000"),
]
