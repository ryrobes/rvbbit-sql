"""Filter-pushdown correctness check: run many predicate shapes on hits via
native+parquet vs native+vortex and assert identical results. A wrong/over-
restrictive Vortex filter would drop rows -> a mismatch here. Run inside the
bench container:  docker compose ... exec -T bench python /bench/verify_vortex_filter.py
"""
import psycopg

BASE = "postgresql://postgres:rvbbit@pg-rvbbit:5432/bench?options=-c%20rvbbit.route_force_candidate%3Drvbbit_native%20-c%20rvbbit.native_vortex%3D"
P = BASE + "off"   # parquet
V = BASE + "on"    # vortex (filter pushdown active)

# Each: a WHERE predicate exercising a different pushable shape.
PREDS = [
    ("eq int",            '"CounterID" = 62'),
    ("gt int",            '"CounterID" > 100000'),
    ("ge+lt range (AND)", '"CounterID" >= 1000 AND "CounterID" < 5000'),
    ("IN int set",        '"AdvEngineID" IN (2, 13, 27)'),
    ("OR of two eq",      '"CounterID" = 62 OR "AdvEngineID" = 2'),
    ("ne int",            '"AdvEngineID" <> 0'),
    ("LIKE contains",     '"Title" LIKE \'%Google%\''),
    ("LIKE prefix",       '"URL" LIKE \'http://%\''),
    ("mixed int+LIKE",    '"CounterID" > 0 AND "Title" LIKE \'%news%\''),
    ("not-pushable ts",   '"EventTime" >= TIMESTAMP \'2013-07-15 00:00:00\''),  # residual, must still match
    ("very selective eq", '"WatchID" = 5061672097061931638'),
]

def run(dsn, pred):
    sql = (f'SELECT count(*) AS n, '
           f'coalesce(sum("CounterID"),0) AS s_counter, '
           f'coalesce(sum(length("Title")),0) AS s_title, '
           f"md5(coalesce(string_agg(\"WatchID\"::text, ',' ORDER BY \"WatchID\"),'')) AS dig "
           f"FROM hits WHERE {pred}")
    with psycopg.connect(dsn) as c, c.cursor() as cur:
        cur.execute(sql)
        return cur.fetchone()

ok = True
print(f"{'shape':22} {'rows':>9}  result")
for name, pred in PREDS:
    p, v = run(P, pred), run(V, pred)
    match = (p == v)
    ok = ok and match
    flag = "MATCH" if match else "*** DIFFER ***"
    print(f"{name:22} {p[0]:>9}  {flag}")
    if not match:
        print(f"    parquet={p}\n    vortex ={v}")

print("\nALL FILTER SHAPES IDENTICAL ✓" if ok else "\nMISMATCH — filter pushdown is dropping/altering rows ✗")
