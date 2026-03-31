//! mesh-queue — async Rust port of queue/mesh_queue.py + queue/worker.py
//!
//! Provides:
//!   - [`MeshQueue`]  — async send/receive/reply/dead-letter (Redis LPUSH/BRPOP)
//!   - [`Worker`]     — async BRPOP dispatch loop with retry + dead-letter
//!   - [`Envelope`]   — wire format (matches Python schema exactly)

mod envelope;
mod queue;
mod worker;

pub use envelope::Envelope;
pub use queue::{MeshQueue, QueueConfig, QueueError};
pub use worker::{SkillHandler, Worker, WorkerConfig};
