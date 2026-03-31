//! scanner — high-throughput async pipeline worker.
//!
//! Pulls target URLs from a Redis queue, downloads and hashes content,
//! routes results back into the mesh via MeshQueue.
//!
//! Usage:
//!   scanner --agent gamma --queue-key scanner:queue:firebase --workers 32

mod worker;

pub use worker::{ScanResult, ScanWorker, ScannerConfig};
