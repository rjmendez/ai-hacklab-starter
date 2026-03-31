//! Async worker — BRPOP dispatch loop with retry and dead-letter.
//! Drop-in replacement for queue/worker.py behaviour.

use std::collections::HashMap;
use std::future::Future;
use std::pin::Pin;
use std::sync::Arc;

use serde_json::Value;
use tokio::signal;
use tracing::{error, info, warn};

use crate::envelope::Envelope;
use crate::queue::{MeshQueue, QueueConfig, QueueError};

pub type SkillHandler = Arc<
    dyn Fn(Value) -> Pin<Box<dyn Future<Output = Result<Value, String>> + Send>> + Send + Sync,
>;

/// Configuration for the worker loop.
pub struct WorkerConfig {
    pub agent_name: String,
    pub queue: QueueConfig,
    /// Max attempts before dead-lettering. Matches Python MAX_RETRIES=3.
    pub max_retries: u32,
    /// BRPOP timeout in seconds.
    pub receive_timeout_secs: u64,
}

impl WorkerConfig {
    pub fn from_env(agent_name: impl Into<String>) -> Self {
        Self {
            agent_name: agent_name.into(),
            queue: QueueConfig::from_env(),
            max_retries: 3,
            receive_timeout_secs: 5,
        }
    }
}

pub struct Worker {
    config: WorkerConfig,
    handlers: HashMap<String, SkillHandler>,
}

impl Worker {
    pub fn new(config: WorkerConfig) -> Self {
        Self { config, handlers: HashMap::new() }
    }

    pub fn register<F, Fut>(mut self, skill_id: impl Into<String>, handler: F) -> Self
    where
        F: Fn(Value) -> Fut + Send + Sync + 'static,
        Fut: Future<Output = Result<Value, String>> + Send + 'static,
    {
        let skill = skill_id.into();
        self.handlers.insert(
            skill,
            Arc::new(move |v| Box::pin(handler(v))),
        );
        self
    }

    /// Run the worker until SIGTERM/SIGINT.
    pub async fn run(self) -> Result<(), QueueError> {
        let queue_cfg = self.config.queue.clone();
        let mut q = MeshQueue::connect(&self.config.agent_name, queue_cfg).await?;
        info!("[worker:{}] started — {} handlers: {:?}",
              self.config.agent_name,
              self.handlers.len(),
              self.handlers.keys().collect::<Vec<_>>());

        let shutdown = async {
            signal::ctrl_c().await.ok();
        };
        tokio::pin!(shutdown);

        loop {
            tokio::select! {
                _ = &mut shutdown => {
                    info!("[worker:{}] shutdown signal received", self.config.agent_name);
                    break;
                }
                result = q.receive(self.config.receive_timeout_secs) => {
                    match result {
                        Ok(None) => continue,
                        Err(e) => { error!("queue receive error: {e}"); continue; }
                        Ok(Some(env)) => {
                            self.dispatch(&mut q, env).await;
                        }
                    }
                }
            }
        }
        Ok(())
    }

    async fn dispatch(&self, q: &mut MeshQueue, mut env: Envelope) {
        let skill = env.skill_id.clone();
        let handler = self.handlers.get(&skill);

        let Some(handler) = handler else {
            warn!("[worker:{}] unknown skill={skill} id={}", self.config.agent_name, env.id);
            let _ = q.dead_letter(env, &format!("unknown skill: {skill}")).await;
            return;
        };

        let t0 = std::time::Instant::now();
        match handler(env.input.clone()).await {
            Ok(result) => {
                let ms = t0.elapsed().as_millis();
                info!("[worker:{}] skill={skill} id={} latency={ms}ms ok", self.config.agent_name, env.id);
                let _ = q.reply(&env, result).await;
            }
            Err(e) => {
                let ms = t0.elapsed().as_millis();
                let attempts = env.attempts + 1;
                warn!("[worker:{}] skill={skill} id={} attempt={attempts} failed in {ms}ms: {e}",
                      self.config.agent_name, env.id);
                if attempts >= self.config.max_retries {
                    error!("[worker:{}] dead-lettering skill={skill} id={} after {attempts} attempts",
                           self.config.agent_name, env.id);
                    let _ = q.dead_letter(env, &e).await;
                } else {
                    // Re-queue with incremented attempt counter
                    env.attempts = attempts;
                    let _ = q.send(&self.config.agent_name.clone(), &skill, env.input.clone()).await;
                }
            }
        }
    }
}
