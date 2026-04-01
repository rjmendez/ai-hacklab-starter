use redis::{AsyncCommands, aio::ConnectionManager};
use serde_json::Value;
use thiserror::Error;

#[derive(Debug, Error)]
pub enum CheckpointError {
    #[error("redis: {0}")]
    Redis(#[from] redis::RedisError),
    #[error("json: {0}")]
    Json(#[from] serde_json::Error),
}

pub struct Checkpoint {
    conn: ConnectionManager,
    key: String,
    ttl_secs: usize,
}

impl Checkpoint {
    pub fn new(conn: ConnectionManager, run_id: &str, ttl_hours: usize) -> Self {
        Self { conn, key: format!("checkpoint:{run_id}"), ttl_secs: ttl_hours * 3600 }
    }

    pub async fn save(&mut self, state: &Value) -> Result<(), CheckpointError> {
        let mut merged = self.load().await?.unwrap_or(Value::Object(Default::default()));
        if let (Value::Object(base), Value::Object(update)) = (&mut merged, state) {
            for (k, v) in update { base.insert(k.clone(), v.clone()); }
        } else {
            merged = state.clone();
        }
        let raw = serde_json::to_string(&merged)?;
        self.conn.set_ex::<_, _, ()>(&self.key, raw, self.ttl_secs as u64).await?;
        Ok(())
    }

    pub async fn load(&mut self) -> Result<Option<Value>, CheckpointError> {
        let raw: Option<String> = self.conn.get(&self.key).await?;
        match raw {
            None => Ok(None),
            Some(s) => Ok(Some(serde_json::from_str(&s)?)),
        }
    }

    pub async fn exists(&mut self) -> Result<bool, CheckpointError> {
        let n: i64 = self.conn.exists(&self.key).await?;
        Ok(n > 0)
    }

    pub async fn complete(&mut self) -> Result<(), CheckpointError> {
        self.conn.del::<_, ()>(&self.key).await?;
        Ok(())
    }

    pub async fn ttl(&mut self) -> Result<i64, CheckpointError> {
        Ok(self.conn.ttl(&self.key).await?)
    }
}
