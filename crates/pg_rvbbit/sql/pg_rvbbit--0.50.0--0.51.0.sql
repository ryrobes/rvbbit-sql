-- Seed direct Gemini text model pricing. Rates are standard text rates;
-- long-context, cache, batch, flex, priority, image, audio, and grounding
-- SKUs need explicit policy overrides.
SELECT rvbbit.set_model_rate('gemini-3.5-flash', 1.500000, 9.000000);
SELECT rvbbit.set_model_rate('gemini-3-flash-preview', 0.500000, 3.000000);
SELECT rvbbit.set_model_rate('gemini-3.1-flash-lite', 0.250000, 1.500000);
SELECT rvbbit.set_model_rate('gemini-3.1-flash-lite-preview', 0.250000, 1.500000);
SELECT rvbbit.set_model_rate('gemini-3.1-pro-preview', 2.000000, 12.000000);
SELECT rvbbit.set_model_rate('gemini-2.5-pro', 1.250000, 10.000000);
SELECT rvbbit.set_model_rate('gemini-2.5-flash', 0.300000, 2.500000);
SELECT rvbbit.set_model_rate('gemini-2.5-flash-lite', 0.100000, 0.400000);
SELECT rvbbit.set_model_rate('gemini-2.0-flash', 0.100000, 0.400000);
