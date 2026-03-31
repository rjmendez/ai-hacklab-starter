use redis::{AsyncCommands, aio::ConnectionManager};
use thiserror::Error;

#[derive(Debug, Error)]
pub enum DedupError {
    #[error("redis: {0}")]
    Redis(#[from] redis::RedisError),
}

pub struct RedisDedup {
    conn: ConnectionManager,
    key: String,
    ttl_secs: i64,
}

impl RedisDedup {
    pub fn new(conn: ConnectionManager, key: impl Into<String>, ttl_days: usize) -> Self {
        Self { conn, key: key.into(), ttl_secs: (ttl_days * 86400) as i64 }
    }

    /// Returns items from `values` not yet in the set. Uses SMISMEMBER (Redis 6.2+).
    pub async fn filter_new(&mut self, values: &[&str]) -> Result<Vec<String>, DedupError> {
        if values.is_empty() { return Ok(vec![]); }
        let results: Vec<i64> = redis::cmd("SMISMEMBER")
            .arg(&self.key)
            .arg(values)
            .query_async(&mut self.conn)
            .await?;
        Ok(values.iter().zip(results).filter_map(|(v, seen)| {
            if seen == 0 { Some(v.to_string()) } else { None }
        }).collect())
    }

    pub async fn mark_many(&mut self, values: &[&str]) -> Result<(), DedupError> {
        if values.is_empty() { return Ok(()); }
        redis::pipe()
            .sadd(&self.key, values).ignore()
            .expire(&self.key, self.ttl_secs).ignore()
            .query_async::<_, ()>(&mut self.conn).await?;
        Ok(())
    }

    pub async fn count(&mut self) -> Result<usize, DedupError> {
        Ok(self.conn.scard(&self.key).await?)
    }

    pub async fn reset(&mut self) -> Result<(), DedupError> {
        self.conn.del::<_, ()>(&self.key).await?;
        Ok(())
    }
}
