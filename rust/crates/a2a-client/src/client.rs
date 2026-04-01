//! A2A JSON-RPC client with token + TOTP auth and retry/backoff.

use std::time::Duration;

use reqwest::Client;
use serde_json::{Value, json};
use thiserror::Error;
use totp_rs::{Algorithm, Secret, TOTP};
use tracing::{debug, warn};
use uuid::Uuid;

#[derive(Debug, Error)]
pub enum A2aError {
    #[error("http error: {0}")]
    Http(#[from] reqwest::Error),
    #[error("json error: {0}")]
    Json(#[from] serde_json::Error),
    #[error("a2a error ({code}): {message}")]
    Rpc { code: i64, message: String },
    #[error("no agent handles skill: {0}")]
    NoRoute(String),
    #[error("retries exhausted after {attempts} attempts: {last_error}")]
    RetriesExhausted { attempts: u32, last_error: Box<A2aError> },
    #[error("totp error: {0}")]
    Totp(String),
}

/// Low-level A2A client for a single agent endpoint.
#[derive(Clone)]
pub struct A2aClient {
    pub url: String,
    pub token: String,
    pub totp_seed: Option<String>,
    http: Client,
    max_retries: u32,
}

impl A2aClient {
    pub fn new(url: impl Into<String>, token: impl Into<String>, totp_seed: Option<String>) -> Self {
        Self {
            url: url.into(),
            token: token.into(),
            totp_seed,
            http: Client::builder()
                .timeout(Duration::from_secs(60))
                .build()
                .expect("reqwest client"),
            max_retries: 3,
        }
    }

    /// Call a skill on this agent. Returns the JSON output value.
    pub async fn call(&self, skill_id: &str, input: Value) -> Result<Value, A2aError> {
        let mut last_err = None;
        for attempt in 0..=self.max_retries {
            if attempt > 0 {
                let delay = Duration::from_millis(200 * 2u64.pow(attempt - 1));
                tokio::time::sleep(delay).await;
                warn!("a2a retry attempt={attempt} skill={skill_id}");
            }
            match self.call_once(skill_id, input.clone()).await {
                Ok(v) => return Ok(v),
                Err(e) if is_retryable(&e) => {
                    last_err = Some(e);
                }
                Err(e) => return Err(e),
            }
        }
        Err(A2aError::RetriesExhausted {
            attempts: self.max_retries,
            last_error: Box::new(last_err.unwrap()),
        })
    }

    async fn call_once(&self, skill_id: &str, input: Value) -> Result<Value, A2aError> {
        let payload = json!({
            "jsonrpc": "2.0",
            "method": "tasks/send",
            "id": Uuid::new_v4().to_string(),
            "params": {
                "skill_id": skill_id,
                "input": input,
            }
        });

        let mut req = self.http
            .post(&self.url)
            .bearer_auth(&self.token)
            .json(&payload);

        if let Some(seed) = &self.totp_seed {
            let totp = TOTP::new(
                Algorithm::SHA1, 6, 1, 30,
                Secret::Encoded(seed.clone()).to_bytes().map_err(|e| A2aError::Totp(e.to_string()))?,
            ).map_err(|e| A2aError::Totp(e.to_string()))?;
            req = req.header("X-TOTP", totp.generate_current().map_err(|e| A2aError::Totp(e.to_string()))?);
        }

        let resp = req.send().await?;
        let status = resp.status();

        if status == 429 || status.is_server_error() {
            // Force retry path
            let body = resp.text().await.unwrap_or_default();
            return Err(A2aError::Rpc { code: status.as_u16() as i64, message: body });
        }

        let body: Value = resp.json().await?;
        debug!("a2a response skill={skill_id} status={status}");

        if let Some(error) = body.get("error") {
            return Err(A2aError::Rpc {
                code: error.get("code").and_then(Value::as_i64).unwrap_or(-1),
                message: error.get("message").and_then(Value::as_str).unwrap_or("unknown").to_string(),
            });
        }

        Ok(body.get("result").cloned().unwrap_or(Value::Null))
    }
}

fn is_retryable(e: &A2aError) -> bool {
    match e {
        A2aError::Http(e) => e.is_connect() || e.is_timeout(),
        A2aError::Rpc { code, .. } => *code == 429 || *code >= 500,
        _ => false,
    }
}
