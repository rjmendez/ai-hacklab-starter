//! Async Redis queue — send, receive, reply, dead-letter.

use redis::AsyncCommands;
use serde_json::Value;
use thiserror::Error;
use tracing::{debug, warn};

use crate::envelope::Envelope;

#[derive(Debug, Error)]
pub enum QueueError {
    #[error("redis: {0}")]
    Redis(#[from] redis::RedisError),
    #[error("json: {0}")]
    Json(#[from] serde_json::Error),
    #[error("receive timeout")]
    Timeout,
}

#[derive(Debug, Clone)]
pub struct QueueConfig {
    pub host: String,
    pub port: u16,
    pub password: Option<String>,
    pub db: u8,
}

impl QueueConfig {
    pub fn from_env() -> Self {
        Self {
            host: std::env::var("REDIS_HOST").unwrap_or_else(|_| "redis".into()),
            port: std::env::var("REDIS_PORT").ok().and_then(|p| p.parse().ok()).unwrap_or(6379),
            password: std::env::var("REDIS_PASSWORD").ok().filter(|s| !s.is_empty()),
            db: 0,
        }
    }

    fn url(&self) -> String {
        match &self.password {
            Some(pw) => format!("redis://:{}@{}:{}/{}", pw, self.host, self.port, self.db),
            None => format!("redis://{}:{}/{}", self.host, self.port, self.db),
        }
    }
}

pub struct MeshQueue {
    agent_name: String,
    conn: redis::aio::ConnectionManager,
}

impl MeshQueue {
    pub async fn connect(agent_name: impl Into<String>, config: QueueConfig) -> Result<Self, QueueError> {
        let client = redis::Client::open(config.url())?;
        let conn = redis::aio::ConnectionManager::new(client).await?;
        Ok(Self { agent_name: agent_name.into(), conn })
    }

    fn inbox_key(agent: &str) -> String { format!("mesh:inbox:{agent}") }
    fn dl_key(agent: &str) -> String { format!("mesh:dead_letter:{agent}") }

    /// Send a task to another agent's inbox. Returns the message ID.
    pub async fn send(&mut self, to: &str, skill_id: &str, input: Value) -> Result<String, QueueError> {
        let env = Envelope::new(&self.agent_name, to, skill_id, input);
        let id = env.id.clone();
        let raw = serde_json::to_string(&env)?;
        let key = Self::inbox_key(to);
        self.conn.lpush::<_, _, ()>(&key, &raw).await?;
        debug!("sent skill={skill_id} to={to} id={id}");
        Ok(id)
    }

    /// Block until a message arrives. Returns None on timeout.
    pub async fn receive(&mut self, timeout_secs: u64) -> Result<Option<Envelope>, QueueError> {
        let key = Self::inbox_key(&self.agent_name);
        let result: Option<(String, String)> = self.conn
            .brpop(&key, timeout_secs as f64)
            .await?;
        match result {
            None => Ok(None),
            Some((_, raw)) => {
                let env: Envelope = serde_json::from_str(&raw)?;
                debug!("recv skill={} from={} id={}", env.skill_id, env.from, env.id);
                Ok(Some(env))
            }
        }
    }

    /// Send a result back to the original sender.
    pub async fn reply(&mut self, original: &Envelope, result: Value) -> Result<String, QueueError> {
        let env = original.reply_envelope(&self.agent_name, result);
        let id = env.id.clone();
        let raw = serde_json::to_string(&env)?;
        self.conn.lpush::<_, _, ()>(&original.reply_to, &raw).await?;
        Ok(id)
    }

    /// Move a failed message to the dead-letter queue (capped at 500).
    pub async fn dead_letter(&mut self, mut env: Envelope, reason: &str) -> Result<(), QueueError> {
        env.dead_letter_reason = Some(reason.to_string());
        env.dead_letter_at = Some(chrono::Utc::now());
        let raw = serde_json::to_string(&env)?;
        let key = Self::dl_key(&self.agent_name);
        redis::pipe()
            .lpush(&key, &raw)
            .ignore()
            .ltrim(&key, 0, 499)
            .ignore()
            .query_async::<_, ()>(&mut self.conn).await?;
        warn!("dead-lettered id={} skill={} reason={reason}", env.id, env.skill_id);
        Ok(())
    }

    /// Queue depth for any agent inbox.
    pub async fn depth(&mut self, agent: &str) -> Result<usize, QueueError> {
        let n: usize = self.conn.llen(Self::inbox_key(agent)).await?;
        Ok(n)
    }
}
