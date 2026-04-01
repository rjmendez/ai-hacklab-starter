use crate::types::Tier;

/// Ordered fallback chains: cheapest first within each tier.
pub fn model_chain(tier: Tier) -> &'static [&'static str] {
    match tier {
        Tier::Nano    => &["gemini-2.0-flash", "gpt-4.1-nano"],
        Tier::Mini    => &["gpt-4.1-nano", "gemini-2.0-flash", "gpt-4.1-mini"],
        Tier::Mid     => &["gpt-4.1-mini", "gemini-2.5-flash"],
        Tier::Strong  => &["claude-sonnet-4-5", "gemini-2.5-flash"],
        Tier::Premium => &["claude-opus-4-5", "claude-sonnet-4-5"],
    }
}
