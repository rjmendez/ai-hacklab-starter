use serde::{Deserialize, Serialize};
use thiserror::Error;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Message {
    pub role: String,
    pub content: String,
}

impl Message {
    pub fn user(content: impl Into<String>) -> Self { Self { role: "user".into(), content: content.into() } }
    pub fn system(content: impl Into<String>) -> Self { Self { role: "system".into(), content: content.into() } }
    pub fn assistant(content: impl Into<String>) -> Self { Self { role: "assistant".into(), content: content.into() } }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Tier { Nano, Mini, Mid, Strong, Premium }

impl Tier {
    pub fn from_str(s: &str) -> Self {
        match s {
            "nano" => Self::Nano, "mini" => Self::Mini,
            "strong" => Self::Strong, "premium" => Self::Premium,
            _ => Self::Mid,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LlmRequest {
    pub messages: Vec<Message>,
    pub model: Option<String>,
    pub tier: Option<Tier>,
    pub max_tokens: u32,
    pub temperature: f32,
    pub system: Option<String>,
    pub agent: Option<String>,
}

impl Default for LlmRequest {
    fn default() -> Self {
        Self { messages: vec![], model: None, tier: Some(Tier::Mid), max_tokens: 4096, temperature: 0.3, system: None, agent: None }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LlmResponse {
    pub content: String,
    pub model: String,
    pub provider: String,
    pub input_tokens: u32,
    pub output_tokens: u32,
    pub cost_usd: f64,
    pub latency_ms: u64,
}

#[derive(Debug, Error)]
pub enum RouterError {
    #[error("all providers failed for tier {tier:?}: {last_error}")]
    AllFailed { tier: Tier, last_error: String },
    #[error("no provider for model: {0}")]
    NoProvider(String),
    #[error("provider error ({provider}): {message}")]
    Provider { provider: String, message: String },
    #[error("http: {0}")]
    Http(#[from] reqwest::Error),
    #[error("json: {0}")]
    Json(#[from] serde_json::Error),
    #[error("redis: {0}")]
    Redis(#[from] redis::RedisError),
}
