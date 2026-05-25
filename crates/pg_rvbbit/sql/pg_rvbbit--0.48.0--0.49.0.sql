-- Seed direct OpenAI standard token pricing for current text models.
-- The rows are deliberately direct OpenAI model ids, not OpenRouter ids.
SELECT rvbbit.set_model_rate('gpt-5.5', 5.000000, 30.000000);
SELECT rvbbit.set_model_rate('gpt-5.5-pro', 30.000000, 180.000000);
SELECT rvbbit.set_model_rate('gpt-5.4', 2.500000, 15.000000);
SELECT rvbbit.set_model_rate('gpt-5.4-mini', 0.750000, 4.500000);
SELECT rvbbit.set_model_rate('gpt-5.4-nano', 0.200000, 1.250000);
SELECT rvbbit.set_model_rate('gpt-5.4-pro', 30.000000, 180.000000);
SELECT rvbbit.set_model_rate('gpt-5.3-codex', 1.750000, 14.000000);
SELECT rvbbit.set_model_rate('gpt-4.1', 2.000000, 8.000000);
SELECT rvbbit.set_model_rate('gpt-4.1-mini', 0.400000, 1.600000);
SELECT rvbbit.set_model_rate('gpt-4.1-nano', 0.100000, 0.400000);
SELECT rvbbit.set_model_rate('gpt-4o', 2.500000, 10.000000);
SELECT rvbbit.set_model_rate('gpt-4o-mini', 0.150000, 0.600000);
