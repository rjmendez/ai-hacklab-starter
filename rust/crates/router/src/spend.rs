use redis::{AsyncCommands, aio::ConnectionManager};
use chrono::Utc;
use serde::{Deserialize, Serialize};
use thiserror::Error;

#[derive(Debug, Error)]
pub enum SpendError {
    #[error("redis: {0}")]
    Redis(#[from] redis::RedisError),
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SpendSummary {
    pub pool_id: String,
    pub date: String,
    pub cost_usd: f64,
    pub tokens: i64,
    pub calls: i64,
}

const TTL_SECS: i64 = 8 * 86400;

pub async fn record(
    conn: &mut ConnectionManager,
    pool_id: &str,
    input_tokens: u32,
    output_tokens: u32,
    cost_usd: f64,
) -> Result<(), SpendError> {
    let date = Utc::now().format("%Y-%m-%d").to_string();
    let spend_key  = format!("dispatch:spend:{pool_id}:{date}");
    let tokens_key = format!("dispatch:tokens:{pool_id}:{date}");
    let calls_key  = format!("dispatch:calls:{pool_id}:{date}");
    let tokens = (input_tokens + output_tokens) as i64;

    redis::pipe()
        .cmd("INCRBYFLOAT").arg(&spend_key).arg(cost_usd).ignore()
        .expire(&spend_key, TTL_SECS).ignore()
        .incr(&tokens_key, tokens).ignore()
        .expire(&tokens_key, TTL_SECS).ignore()
        .incr(&calls_key, 1i64).ignore()
        .expire(&calls_key, TTL_SECS).ignore()
        .query_async::<_, ()>(conn).await?;

    Ok(())
}

pub async fn daily_summary(
    conn: &mut ConnectionManager,
    pool_id: &str,
    date: &str,
) -> Result<SpendSummary, SpendError> {
    let spend_key  = format!("dispatch:spend:{pool_id}:{date}");
    let tokens_key = format!("dispatch:tokens:{pool_id}:{date}");
    let calls_key  = format!("dispatch:calls:{pool_id}:{date}");

    let (cost, tokens, calls): (Option<f64>, Option<i64>, Option<i64>) = redis::pipe()
        .cmd("GET").arg(&spend_key)
        .cmd("GET").arg(&tokens_key)
        .cmd("GET").arg(&calls_key)
        .query_async(conn).await?;

    Ok(SpendSummary {
        pool_id: pool_id.to_string(),
        date: date.to_string(),
        cost_usd: cost.unwrap_or(0.0),
        tokens: tokens.unwrap_or(0),
        calls: calls.unwrap_or(0),
    })
}
