use redis::{Script, AsyncCommands, aio::ConnectionManager};
use thiserror::Error;
use uuid::Uuid;

const RELEASE_LUA: &str = r#"
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
else
  return 0
end
"#;

#[derive(Debug, Error)]
pub enum LockError {
    #[error("redis: {0}")]
    Redis(#[from] redis::RedisError),
}

pub struct DistributedLock {
    conn: ConnectionManager,
    key: String,
    token: String,
    script: Script,
}

impl DistributedLock {
    pub fn new(conn: ConnectionManager, key: impl Into<String>) -> Self {
        Self {
            conn,
            key: key.into(),
            token: Uuid::new_v4().to_string(),
            script: Script::new(RELEASE_LUA),
        }
    }

    pub async fn acquire(&mut self, ttl_secs: u64) -> Result<bool, LockError> {
        let result: Option<String> = redis::cmd("SET")
            .arg(&self.key)
            .arg(&self.token)
            .arg("NX")
            .arg("EX")
            .arg(ttl_secs)
            .query_async(&mut self.conn)
            .await?;
        Ok(result.is_some())
    }

    pub async fn release(&mut self) -> Result<bool, LockError> {
        let n: i64 = self.script
            .key(&self.key)
            .arg(&self.token)
            .invoke_async(&mut self.conn)
            .await?;
        Ok(n == 1)
    }

    pub async fn extend(&mut self, ttl_secs: u64) -> Result<bool, LockError> {
        let current: Option<String> = self.conn.get(&self.key).await?;
        if current.as_deref() == Some(&self.token) {
            self.conn.expire::<_, ()>(&self.key, ttl_secs as i64).await?;
            Ok(true)
        } else {
            Ok(false)
        }
    }
}
