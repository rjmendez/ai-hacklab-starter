/// Per-model pricing in USD per 1M tokens (input, output).
const PRICING: &[(&str, f64, f64)] = &[
    ("claude-opus-4-5",   15.00, 75.00),
    ("claude-sonnet-4-5",  3.00, 15.00),
    ("claude-haiku-3-5",   0.80,  4.00),
    ("gpt-4o",             2.50, 10.00),
    ("gpt-4.1-nano",       0.10,  0.40),
    ("gpt-4.1-mini",       0.40,  1.60),
    ("gpt-4.1",            2.00,  8.00),
    ("gemini-2.5-flash",   0.15,  0.60),
    ("gemini-2.0-flash",   0.10,  0.40),
];

pub fn estimate_cost(model: &str, input_tokens: u32, output_tokens: u32) -> f64 {
    let (in_price, out_price) = PRICING.iter()
        .find(|(m, _, _)| model.starts_with(m))
        .map(|(_, i, o)| (*i, *o))
        .unwrap_or((1.00, 5.00)); // conservative fallback
    (input_tokens as f64 / 1_000_000.0) * in_price
        + (output_tokens as f64 / 1_000_000.0) * out_price
}
