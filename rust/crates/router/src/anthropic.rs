use std::time::Instant;
use async_trait::async_trait;
use api::{AnthropicClient, ContentBlockDelta, InputContentBlock, InputMessage, MessageRequest, StreamEvent};
use crate::cost::estimate_cost;
use crate::provider::Provider;
use crate::types::{LlmRequest, LlmResponse, RouterError};

pub struct AnthropicProvider {
    client: AnthropicClient,
}

impl AnthropicProvider {
    pub fn new(api_key: impl Into<String>) -> Self {
        Self { client: AnthropicClient::new(api_key) }
    }

    pub fn from_env() -> Option<Self> {
        std::env::var("ANTHROPIC_API_KEY").ok().map(Self::new)
    }
}

#[async_trait]
impl Provider for AnthropicProvider {
    fn name(&self) -> &str { "anthropic" }

    fn supports_model(&self, model: &str) -> bool { model.starts_with("claude-") }

    async fn complete(&self, req: &LlmRequest, model: &str) -> Result<LlmResponse, RouterError> {
        let t0 = Instant::now();

        let messages: Vec<InputMessage> = req.messages.iter().map(|m| InputMessage {
            role: m.role.clone(),
            content: vec![InputContentBlock::Text { text: m.content.clone() }],
        }).collect();

        let api_req = MessageRequest {
            model: model.to_string(),
            max_tokens: req.max_tokens,
            messages,
            system: req.system.clone(),
            tools: None,
            tool_choice: None,
            stream: true,
        };

        let mut stream = self.client.stream_message(&api_req).await
            .map_err(|e| RouterError::Provider { provider: "anthropic".into(), message: e.to_string() })?;

        let mut content = String::new();
        let mut input_tokens = 0u32;
        let mut output_tokens = 0u32;

        while let Some(event) = stream.next_event().await
            .map_err(|e| RouterError::Provider { provider: "anthropic".into(), message: e.to_string() })?
        {
            match event {
                StreamEvent::ContentBlockDelta(e) => {
                    if let ContentBlockDelta::TextDelta { text } = e.delta {
                        content.push_str(&text);
                    }
                }
                StreamEvent::MessageStart(e) => {
                    input_tokens = e.message.usage.input_tokens;
                }
                StreamEvent::MessageDelta(e) => {
                    output_tokens = e.usage.output_tokens;
                }
                _ => {}
            }
        }

        Ok(LlmResponse {
            content,
            model: model.to_string(),
            provider: "anthropic".to_string(),
            input_tokens,
            output_tokens,
            cost_usd: estimate_cost(model, input_tokens, output_tokens),
            latency_ms: t0.elapsed().as_millis() as u64,
        })
    }
}
