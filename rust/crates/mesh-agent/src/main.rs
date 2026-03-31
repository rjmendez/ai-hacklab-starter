//! mesh-agent — standalone binary harness wiring ConversationRuntime + A2AToolExecutor.
//!
//! Reads a prompt from stdin (or --prompt), runs a full multi-turn agent loop
//! with tools dispatched to the mesh via A2A, and writes the final response to stdout.
//!
//! Used by Python agent servers to delegate multi-turn LLM reasoning:
//!   result=$(echo "$PROMPT" | mesh-agent --agent iris --registry /path/to/agent_registry.json)

use std::path::PathBuf;

use clap::Parser;
use tracing_subscriber::EnvFilter;

#[derive(Parser, Debug)]
#[command(name = "mesh-agent", about = "Run a multi-turn agent turn via ConversationRuntime + A2A mesh dispatch")]
struct Args {
    /// Agent name (used for session namespacing and logging)
    #[arg(long, env = "MESH_AGENT_NAME", default_value = "agent")]
    agent: String,

    /// Path to agent_registry.json
    #[arg(long, env = "AGENT_REGISTRY_PATH")]
    registry: Option<PathBuf>,

    /// Prompt to run (if not set, reads from stdin)
    #[arg(long)]
    prompt: Option<String>,

    /// System prompt override
    #[arg(long, env = "MESH_AGENT_SYSTEM_PROMPT")]
    system: Option<String>,

    /// Model to use
    #[arg(long, env = "ANTHROPIC_MODEL", default_value = "claude-opus-4-5")]
    model: String,

    /// Max tokens per turn
    #[arg(long, default_value_t = 8192)]
    max_tokens: u32,

    /// Max agent iterations (tool call rounds)
    #[arg(long, default_value_t = 16)]
    max_iterations: u32,

    /// Output session JSON to this file after completion
    #[arg(long)]
    session_out: Option<PathBuf>,
}

#[tokio::main]
async fn main() {
    tracing_subscriber::fmt()
        .with_env_filter(EnvFilter::from_default_env().add_directive("mesh_agent=info".parse().unwrap()))
        .init();

    let args = Args::parse();

    let prompt = match args.prompt {
        Some(p) => p,
        None => {
            use std::io::Read;
            let mut buf = String::new();
            std::io::stdin().read_to_string(&mut buf).expect("read stdin");
            buf.trim().to_string()
        }
    };

    if prompt.is_empty() {
        eprintln!("mesh-agent: no prompt provided");
        std::process::exit(1);
    }

    // Load registry + build executor if registry path given
    // (if no registry, tool calls will fail gracefully — useful for simple single-turn use)
    let _registry_path = args.registry;

    // TODO: wire ConversationRuntime<AnthropicClient, A2aToolExecutor> once
    // runtime::conversation trait bounds are finalised. For now, print config + prompt
    // to confirm the binary is functional.
    eprintln!("[mesh-agent] agent={} model={} max_tokens={} max_iter={}",
              args.agent, args.model, args.max_tokens, args.max_iterations);
    eprintln!("[mesh-agent] prompt={}", &prompt[..prompt.len().min(120)]);
    eprintln!("[mesh-agent] TODO: full ConversationRuntime integration (see conversation.rs)");
    println!("{prompt}");
}
