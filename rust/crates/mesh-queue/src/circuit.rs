use redis::{Script, AsyncCommands, aio::ConnectionManager};
use thiserror::Error;

const LUA: &str = r#"
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local threshold = tonumber(ARGV[3])
local open_ttl = tonumber(ARGV[4])
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now - window)
redis.call('ZADD', KEYS[1], now, tostring(now) .. '-' .. tostring(math.random(1,999999)))
redis.call('EXPIRE', KEYS[1], window * 2)
local count = redis.call('ZCARD', KEYS[1])
if count >= threshold then
  redis.call('SET', KEYS[2], '1', 'EX', open_ttl)
  return 1
end
return 0
"#;

const BREAK_ERRORS: i64 = 3;
const BREAK_WINDOW: i64 = 60;
const OPEN_TTL: i64 = 300;

#[derive(Debug, Error)]
pub enum CircuitError {
    #[error("redis: {0}")]
    Redis(#[from] redis::RedisError),
}

pub struct CircuitBreaker {
    conn: ConnectionManager,
    errors_key: String,
    circuit_key: String,
    script: Script,
}

impl CircuitBreaker {
    pub fn new(conn: ConnectionManager, pool_id: &str) -> Self {
        Self {
            conn,
            errors_key: format!("dispatch:errors:{pool_id}"),
            circuit_key: format!("dispatch:circuit:{pool_id}"),
            script: Script::new(LUA),
        }
    }

    pub async fn is_open(&mut self) -> Result<bool, CircuitError> {
        let val: Option<String> = self.conn.get(&self.circuit_key).await?;
        Ok(val.as_deref() == Some("1"))
    }

    /// Record an error. Returns true if the circuit just opened.
    pub async fn record_error(&mut self) -> Result<bool, CircuitError> {
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs_f64();
        let opened: i64 = self.script
            .key(&self.errors_key)
            .key(&self.circuit_key)
            .arg(now)
            .arg(BREAK_WINDOW)
            .arg(BREAK_ERRORS)
            .arg(OPEN_TTL)
            .invoke_async(&mut self.conn)
            .await?;
        Ok(opened == 1)
    }

    pub async fn reset(&mut self) -> Result<(), CircuitError> {
        redis::pipe()
            .del(&self.errors_key).ignore()
            .del(&self.circuit_key).ignore()
            .query_async::<_, ()>(&mut self.conn).await?;
        Ok(())
    }
}
