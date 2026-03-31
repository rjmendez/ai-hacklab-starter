use async_trait::async_trait;
use crate::types::{LlmRequest, LlmResponse, RouterError};

#[async_trait]
pub trait Provider: Send + Sync {
    async fn complete(&self, req: &LlmRequest, model: &str) -> Result<LlmResponse, RouterError>;
    fn name(&self) -> &str;
    fn supports_model(&self, model: &str) -> bool;
}
