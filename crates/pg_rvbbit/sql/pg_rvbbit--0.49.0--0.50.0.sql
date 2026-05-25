-- Seed direct Anthropic standard token pricing for current Claude text models.
-- Cache read/write pricing and data residency multipliers are intentionally
-- left to explicit policies.
SELECT rvbbit.set_model_rate('claude-opus-4-7', 5.000000, 25.000000);
SELECT rvbbit.set_model_rate('claude-opus-4-6', 5.000000, 25.000000);
SELECT rvbbit.set_model_rate('claude-opus-4-5-20251101', 5.000000, 25.000000);
SELECT rvbbit.set_model_rate('claude-opus-4-1-20250805', 15.000000, 75.000000);
SELECT rvbbit.set_model_rate('claude-opus-4-20250514', 15.000000, 75.000000);
SELECT rvbbit.set_model_rate('claude-sonnet-4-6', 3.000000, 15.000000);
SELECT rvbbit.set_model_rate('claude-sonnet-4-5-20250929', 3.000000, 15.000000);
SELECT rvbbit.set_model_rate('claude-sonnet-4-20250514', 3.000000, 15.000000);
SELECT rvbbit.set_model_rate('claude-haiku-4-5-20251001', 1.000000, 5.000000);
