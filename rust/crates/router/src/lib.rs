pub mod anthropic;
pub mod cost;
pub mod openai;
pub mod provider;
pub mod selector;
pub mod spend;
pub mod types;

use std::sync::Arc;
use tracing::{info, warn};

pub use types::{LlmRequest, LlmResponse, Message, RouterError, Tier};

use crate::provider::Provider;

pub struct Router {
    providers: Vec<Arc<dyn Provider>>,
    redis: Option<redis::aio::ConnectionManager>,
    pool_id: String,
}

impl Router {
    pub fn new(pool_id: impl Into<String>) -> Self {
        Self { providers: vec![], redis: None, pool_id: pool_id.into() }
    }

    pub fn with_provider(mut self, p: impl Provider + 'static) -> Self {
        self.providers.push(Arc::new(p));
        self
    }

    pub fn with_redis(mut self, conn: redis::aio::ConnectionManager) -> Self {
        self.redis = Some(conn);
        self
    }

    /// Build from environment variables. Loads whichever providers have keys set.
    pub fn from_env(pool_id: impl Into<String>) -> Self {
        let mut r = Self::new(pool_id);
        if let Some(p) = anthropic::AnthropicProvider::from_env() { r = r.with_provider(p); }
        if let Some(p) = openai::OpenAiProvider::openai()          { r = r.with_provider(p); }
        if let Some(p) = openai::OpenAiProvider::gemini()          { r = r.with_provider(p); }
        r
    }

    pub async fn complete(&self, req: LlmRequest) -> Result<LlmResponse, RouterError> {
        let tier = req.tier.unwrap_or(Tier::Mid);

        if let Some(ref model) = req.model {
            return self.complete_with_model(&req, model).await;
        }

        let chain = selector::model_chain(tier);
        let mut last_err = String::new();

        for model in chain {
            match self.complete_with_model(&req, model).await {
                Ok(resp) => {
                    info!("router: tier={tier:?} model={model} in={} out={} cost=${:.6}",
                          resp.input_tokens, resp.output_tokens, resp.cost_usd);
                    if let Some(mut redis) = self.redis.clone() {
                        let _ = spend::record(&mut redis, &self.pool_id,
                                              resp.input_tokens, resp.output_tokens, resp.cost_usd).await;
                    }
                    return Ok(resp);
                }
                Err(e) => {
                    warn!("router: model={model} failed: {e}");
                    last_err = e.to_string();
                }
            }
        }

        Err(RouterError::AllFailed { tier, last_error: last_err })
    }

    async fn complete_with_model(&self, req: &LlmRequest, model: &str) -> Result<LlmResponse, RouterError> {
        let provider = self.providers.iter()
            .find(|p| p.supports_model(model))
            .ok_or_else(|| RouterError::NoProvider(model.to_string()))?;
        provider.complete(req, model).await
    }
}
