use std::time::Instant;
use async_trait::async_trait;
use reqwest::Client;
use serde_json::{Value, json};
use crate::cost::estimate_cost;
use crate::provider::Provider;
use crate::types::{LlmRequest, LlmResponse, RouterError};

const OPENAI_MODELS: &[&str] = &["gpt-4o", "gpt-4.1", "gpt-4.1-mini", "gpt-4.1-nano"];
const GEMINI_MODELS: &[&str] = &["gemini-2.5-flash", "gemini-2.0-flash"];

pub struct OpenAiProvider {
    client: Client,
    base_url: String,
    api_key: String,
    provider_name: String,
}

impl OpenAiProvider {
    pub fn new(base_url: impl Into<String>, api_key: impl Into<String>, provider_name: impl Into<String>) -> Self {
        Self { client: Client::new(), base_url: base_url.into(), api_key: api_key.into(), provider_name: provider_name.into() }
    }

    pub fn openai() -> Option<Self> {
        std::env::var("OPENAI_API_KEY").ok().map(|k| {
            Self::new("https://api.openai.com/v1", k, "openai")
        })
    }

    pub fn gemini() -> Option<Self> {
        std::env::var("GEMINI_API_KEY").ok().map(|k| {
            Self::new("https://generativelanguage.googleapis.com/v1beta/openai", k, "gemini")
        })
    }
}

#[async_trait]
impl Provider for OpenAiProvider {
    fn name(&self) -> &str { &self.provider_name }

    fn supports_model(&self, model: &str) -> bool {
        OPENAI_MODELS.contains(&model) || GEMINI_MODELS.contains(&model)
    }

    async fn complete(&self, req: &LlmRequest, model: &str) -> Result<LlmResponse, RouterError> {
        let t0 = Instant::now();

        let mut messages: Vec<Value> = req.messages.iter()
            .map(|m| json!({"role": m.role, "content": m.content}))
            .collect();

        if let Some(sys) = &req.system {
            messages.insert(0, json!({"role": "system", "content": sys}));
        }

        let body = json!({
            "model": model,
            "messages": messages,
            "max_tokens": req.max_tokens,
            "temperature": req.temperature,
        });

        let resp = self.client
            .post(format!("{}/chat/completions", self.base_url.trim_end_matches('/')))
            .bearer_auth(&self.api_key)
            .json(&body)
            .send()
            .await?;

        if !resp.status().is_success() {
            let status = resp.status();
            let text = resp.text().await.unwrap_or_default();
            return Err(RouterError::Provider { provider: self.provider_name.clone(), message: format!("HTTP {status}: {text}") });
        }

        let data: Value = resp.json().await?;
        let content = data["choices"][0]["message"]["content"].as_str().unwrap_or("").to_string();
        let input_tokens = data["usage"]["prompt_tokens"].as_u64().unwrap_or(0) as u32;
        let output_tokens = data["usage"]["completion_tokens"].as_u64().unwrap_or(0) as u32;

        Ok(LlmResponse {
            content,
            model: model.to_string(),
            provider: self.provider_name.clone(),
            input_tokens,
            output_tokens,
            cost_usd: estimate_cost(model, input_tokens, output_tokens),
            latency_ms: t0.elapsed().as_millis() as u64,
        })
    }
}
