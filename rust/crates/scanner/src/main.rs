use clap::Parser;
use scanner::{ScannerConfig, ScanWorker};
use tracing_subscriber::EnvFilter;

#[derive(Parser, Debug)]
#[command(name = "scanner", about = "Mesh pipeline scanner — async parallel URL fetch/hash/report")]
struct Args {
    /// Redis queue key to pull targets from
    #[arg(long, env = "SCANNER_QUEUE_KEY", default_value = "scanner:queue:targets")]
    queue_key: String,

    /// Redis key to push results to
    #[arg(long, env = "SCANNER_RESULTS_KEY", default_value = "scanner:results")]
    results_key: String,

    /// Number of parallel workers
    #[arg(long, env = "SCANNER_WORKERS", default_value_t = 16)]
    workers: usize,

    /// Request timeout (seconds)
    #[arg(long, default_value_t = 30)]
    timeout: u64,
}

#[tokio::main]
async fn main() {
    tracing_subscriber::fmt()
        .with_env_filter(EnvFilter::from_default_env().add_directive("scanner=info".parse().unwrap()))
        .init();

    let args = Args::parse();
    let mut config = ScannerConfig::from_env();
    config.queue_key = args.queue_key;
    config.results_key = args.results_key;
    config.concurrency = args.workers;
    config.request_timeout_secs = args.timeout;

    if let Err(e) = ScanWorker::run(config).await {
        eprintln!("scanner error: {e}");
        std::process::exit(1);
    }
}
