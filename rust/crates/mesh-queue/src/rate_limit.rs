use redis::{Script, aio::ConnectionManager};
use thiserror::Error;

const LUA: &str = r#"
local count = redis.call('INCR', KEYS[1])
if count > tonumber(ARGV[1]) then
  redis.call('DECR', KEYS[1])
  return 0
end
redis.call('EXPIRE', KEYS[1], tonumber(ARGV[2]))
return 1
"#;

#[derive(Debug, Error)]
pub enum RateLimitError {
    #[error("redis: {0}")]
    Redis(#[from] redis::RedisError),
}

pub struct RateLimit {
    conn: ConnectionManager,
    key: String,
    max_calls: i64,
    window_secs: i64,
    script: Script,
}

impl RateLimit {
    pub fn new(conn: ConnectionManager, key: impl Into<String>, max_calls: i64, window_secs: i64) -> Self {
        Self { conn, key: key.into(), max_calls, window_secs, script: Script::new(LUA) }
    }

    pub async fn try_acquire(&mut self) -> Result<bool, RateLimitError> {
        let result: i64 = self.script
            .key(&self.key)
            .arg(self.max_calls)
            .arg(self.window_secs)
            .invoke_async(&mut self.conn)
            .await?;
        Ok(result == 1)
    }

    pub async fn remaining(&mut self) -> Result<i64, RateLimitError> {
        let count: Option<i64> = redis::cmd("GET")
            .arg(&self.key)
            .query_async(&mut self.conn)
            .await?;
        Ok((self.max_calls - count.unwrap_or(0)).max(0))
    }

    pub async fn reset(&mut self) -> Result<(), RateLimitError> {
        redis::cmd("DEL").arg(&self.key).query_async::<_, ()>(&mut self.conn).await?;
        Ok(())
    }
}
