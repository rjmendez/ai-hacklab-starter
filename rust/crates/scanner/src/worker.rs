//! Scanner worker — async parallel URL fetch + hash + report-to-mesh.

use std::sync::Arc;
use std::time::Duration;

use redis::AsyncCommands;
use reqwest::Client;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use thiserror::Error;
use tracing::{error, info, warn};

#[derive(Debug, Error)]
pub enum ScanError {
    #[error("http: {0}")]
    Http(#[from] reqwest::Error),
    #[error("redis: {0}")]
    Redis(#[from] redis::RedisError),
    #[error("json: {0}")]
    Json(#[from] serde_json::Error),
}

/// Configuration for the scanner pool.
#[derive(Debug, Clone)]
pub struct ScannerConfig {
    /// Redis connection URL.
    pub redis_url: String,
    /// Key to BRPOP scan targets from.
    pub queue_key: String,
    /// Results are LPUSH'd here (or sent via MeshQueue if `report_agent` is set).
    pub results_key: String,
    /// How many concurrent workers to run.
    pub concurrency: usize,
    /// HTTP request timeout per target.
    pub request_timeout_secs: u64,
    /// Max bytes to download per target (default 50 MB).
    pub max_bytes: usize,
}

impl ScannerConfig {
    pub fn from_env() -> Self {
        let redis_pw = std::env::var("REDIS_PASSWORD").unwrap_or_default();
        let redis_host = std::env::var("REDIS_HOST").unwrap_or_else(|_| "redis".into());
        let redis_port = std::env::var("REDIS_PORT").unwrap_or_else(|_| "6379".into());
        let redis_url = if redis_pw.is_empty() {
            format!("redis://{redis_host}:{redis_port}")
        } else {
            format!("redis://:{redis_pw}@{redis_host}:{redis_port}")
        };
        Self {
            redis_url,
            queue_key: std::env::var("SCANNER_QUEUE_KEY")
                .unwrap_or_else(|_| "scanner:queue:targets".into()),
            results_key: std::env::var("SCANNER_RESULTS_KEY")
                .unwrap_or_else(|_| "scanner:results".into()),
            concurrency: std::env::var("SCANNER_WORKERS")
                .ok().and_then(|s| s.parse().ok()).unwrap_or(16),
            request_timeout_secs: 30,
            max_bytes: 50 * 1024 * 1024,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ScanTarget {
    pub url: String,
    #[serde(default)]
    pub meta: serde_json::Value,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ScanResult {
    pub url: String,
    pub sha256: String,
    pub size_bytes: usize,
    pub status_code: u16,
    pub content_type: Option<String>,
    pub error: Option<String>,
    pub scanned_at: chrono::DateTime<chrono::Utc>,
    pub meta: serde_json::Value,
}

pub struct ScanWorker {
    config: Arc<ScannerConfig>,
    http: Client,
}

impl ScanWorker {
    pub fn new(config: Arc<ScannerConfig>) -> Self {
        let http = Client::builder()
            .timeout(Duration::from_secs(config.request_timeout_secs))
            .user_agent("mesh-scanner/0.1")
            .build()
            .expect("reqwest client");
        Self { config, http }
    }

    /// Run the scanner pool until SIGTERM/SIGINT.
    pub async fn run(config: ScannerConfig) -> Result<(), ScanError> {
        let config = Arc::new(config);
        let client = redis::Client::open(config.redis_url.clone())?;

        info!("scanner starting — concurrency={} queue={}", config.concurrency, config.queue_key);

        let mut handles = Vec::new();
        for _ in 0..config.concurrency {
            let cfg = config.clone();
            let conn = redis::aio::ConnectionManager::new(client.clone()).await?;
            handles.push(tokio::spawn(async move {
                let worker = ScanWorker::new(cfg.clone());
                worker.worker_loop(conn).await;
            }));
        }

        // Wait for shutdown signal
        tokio::signal::ctrl_c().await.ok();
        info!("scanner shutting down — waiting for workers");
        for h in handles { h.abort(); }
        Ok(())
    }

    async fn worker_loop(&self, mut conn: redis::aio::ConnectionManager) {
        loop {
            let result: Result<Option<(String, String)>, _> = conn
                .brpop(&self.config.queue_key, 5.0)
                .await;

            match result {
                Err(e) => { error!("redis brpop error: {e}"); tokio::time::sleep(Duration::from_secs(1)).await; }
                Ok(None) => continue,
                Ok(Some((_, raw))) => {
                    let target: ScanTarget = match serde_json::from_str(&raw) {
                        Ok(t) => t,
                        Err(e) => { warn!("bad target json: {e}"); continue; }
                    };

                    let result = self.scan(&target).await;
                    let result_json = serde_json::to_string(&result).unwrap_or_default();

                    if let Err(e) = conn.lpush::<_, _, ()>(&self.config.results_key, &result_json).await {
                        error!("failed to push result: {e}");
                    }

                    if let Some(err) = &result.error {
                        warn!("scan error url={} err={err}", target.url);
                    } else {
                        info!("scanned url={} size={} sha256={}", result.url, result.size_bytes, &result.sha256[..12]);
                    }
                }
            }
        }
    }

    async fn scan(&self, target: &ScanTarget) -> ScanResult {
        let started = chrono::Utc::now();
        match self.fetch(&target.url).await {
            Ok((status, content_type, body)) => {
                let mut hasher = Sha256::new();
                hasher.update(&body);
                let sha256 = hex::encode(hasher.finalize());
                ScanResult {
                    url: target.url.clone(),
                    sha256,
                    size_bytes: body.len(),
                    status_code: status,
                    content_type,
                    error: None,
                    scanned_at: started,
                    meta: target.meta.clone(),
                }
            }
            Err(e) => ScanResult {
                url: target.url.clone(),
                sha256: String::new(),
                size_bytes: 0,
                status_code: 0,
                content_type: None,
                error: Some(e.to_string()),
                scanned_at: started,
                meta: target.meta.clone(),
            }
        }
    }

    async fn fetch(&self, url: &str) -> Result<(u16, Option<String>, Vec<u8>), reqwest::Error> {
        let resp = self.http.get(url).send().await?;
        let status = resp.status().as_u16();
        let content_type = resp.headers()
            .get("content-type")
            .and_then(|v| v.to_str().ok())
            .map(|s| s.split(';').next().unwrap_or(s).trim().to_string());

        // Cap download at max_bytes
        let max = self.config.max_bytes;
        let bytes = resp.bytes().await?;
        let body = if bytes.len() > max { bytes[..max].to_vec() } else { bytes.to_vec() };
        Ok((status, content_type, body))
    }
}
