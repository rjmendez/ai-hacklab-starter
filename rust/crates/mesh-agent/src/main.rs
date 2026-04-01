//! mesh-agent — wired agent binary.
//!
//! Runs a multi-turn agent loop using ConversationRuntime<RouterApiClient, MeshToolExecutor>:
//!   - LLM calls go through `router::Router` (AnthropicProvider / OpenAiProvider / GeminiProvider)
//!   - Tool calls are dispatched to the mesh via A2A (A2aToolExecutor)
//!   - Sessions are persisted to a JSON file (--session-file) for resumable runs
//!
//! Usage:
//!   echo "Audit paxos contracts" | mesh-agent --agent iris --tier strong
//!   mesh-agent --agent iris --prompt "scan for reentrancy" --tier premium
//!   mesh-agent a2a-call --agent charlie --skill findings_ingest --input '{"url":"..."}'

use std::io::Read;
use std::path::PathBuf;
use std::sync::Arc;

use clap::{Parser, Subcommand};
use serde_json::Value;
use tracing::{error, info};
use tracing_subscriber::EnvFilter;

use a2a_client::{AgentRegistry, DispatchPolicy};
use router::{LlmRequest, Message, Router, Tier};
use runtime::{
    ContentBlock, MessageRole, PermissionMode, PermissionPolicy, Session, TokenUsage,
    conversation::{ApiClient, ApiRequest, AssistantEvent, ConversationRuntime, RuntimeError, ToolExecutor, ToolError},
};

// ── ApiClient bridge (sync trait, async router) ─────────────────────────────

struct RouterApiClient {
    router: Arc<Router>,
    model: Option<String>,
    tier: Tier,
}

impl RouterApiClient {
    fn new(router: Arc<Router>, model: Option<String>, tier: Tier) -> Self {
        Self { router, model, tier }
    }
}

fn role_str(role: &MessageRole) -> &'static str {
    match role {
        MessageRole::System    => "system",
        MessageRole::User      => "user",
        MessageRole::Assistant => "assistant",
        MessageRole::Tool      => "tool",
    }
}

impl ApiClient for RouterApiClient {
    fn stream(&mut self, request: ApiRequest) -> Result<Vec<AssistantEvent>, RuntimeError> {
        let messages: Vec<Message> = request.messages.iter().flat_map(|msg| {
            msg.blocks.iter().filter_map(|block| match block {
                ContentBlock::Text { text } => Some(Message {
                    role: role_str(&msg.role).to_string(),
                    content: text.clone(),
                }),
                _ => None,
            })
        }).collect();

        let system = request.system_prompt.join("\n\n");
        let system = if system.is_empty() { None } else { Some(system) };

        let llm_req = LlmRequest {
            messages,
            model: self.model.clone(),
            tier: Some(self.tier),
            max_tokens: 8192,
            temperature: 0.3,
            system,
            agent: None,
        };

        let resp = tokio::task::block_in_place(|| {
            tokio::runtime::Handle::current().block_on(self.router.complete(llm_req))
        }).map_err(|e| RuntimeError::new(e.to_string()))?;

        let usage = TokenUsage {
            input_tokens: resp.input_tokens,
            output_tokens: resp.output_tokens,
            cache_creation_input_tokens: 0,
            cache_read_input_tokens: 0,
        };

        Ok(vec![
            AssistantEvent::TextDelta(resp.content),
            AssistantEvent::Usage(usage),
            AssistantEvent::MessageStop,
        ])
    }
}

// ── ToolExecutor bridge ──────────────────────────────────────────────────────

struct MeshToolExecutor(a2a_client::A2aToolExecutor);

impl ToolExecutor for MeshToolExecutor {
    fn execute(&mut self, tool_name: &str, input: &str) -> Result<String, ToolError> {
        self.0.execute(tool_name, input)
            .map_err(|e| ToolError::new(e.to_string()))
    }
}

fn make_tool_executor(registry_path: &Option<PathBuf>) -> MeshToolExecutor {
    let (registry, policy) = match registry_path {
        Some(path) => {
            match AgentRegistry::load(path) {
                Ok(r) => {
                    let p = DispatchPolicy::default_mesh();
                    (r, p)
                }
                Err(e) => {
                    error!("failed to load registry {}: {e}", path.display());
                    std::process::exit(1);
                }
            }
        }
        None => {
            info!("no registry — tool dispatch will return errors");
            (AgentRegistry::empty(), DispatchPolicy::empty())
        }
    };
    MeshToolExecutor(a2a_client::A2aToolExecutor::new(registry, policy))
}

// ── CLI ──────────────────────────────────────────────────────────────────────

#[derive(Parser, Debug)]
#[command(name = "mesh-agent", about = "Multi-turn agent loop over mesh A2A + multi-provider LLM router")]
struct Cli {
    #[command(subcommand)]
    command: Option<Commands>,

    /// Agent name (session namespacing)
    #[arg(long, env = "MESH_AGENT_NAME", default_value = "agent", global = true)]
    agent: String,

    /// Path to agent_registry.json
    #[arg(long, env = "AGENT_REGISTRY_PATH", global = true)]
    registry: Option<PathBuf>,

    /// Prompt to run (if omitted, reads from stdin)
    #[arg(long)]
    prompt: Option<String>,

    /// System prompt override
    #[arg(long, env = "MESH_AGENT_SYSTEM")]
    system: Option<String>,

    /// LLM tier (nano/mini/mid/strong/premium)
    #[arg(long, env = "MESH_AGENT_TIER", default_value = "mid")]
    tier: String,

    /// Explicit model override (bypasses tier selection)
    #[arg(long, env = "MESH_AGENT_MODEL")]
    model: Option<String>,

    /// Max agent iterations (tool-call rounds)
    #[arg(long, default_value_t = 16)]
    max_iterations: usize,

    /// Session file for persistence (loaded + saved each turn)
    #[arg(long, env = "MESH_AGENT_SESSION_FILE")]
    session_file: Option<PathBuf>,

    /// Redis URL for spend tracking
    #[arg(long, env = "REDIS_URL", default_value = "redis://localhost:6379")]
    redis_url: String,

    /// Pool ID for spend tracking
    #[arg(long, env = "MESH_POOL_ID", default_value = "mrpink")]
    pool_id: String,
}

#[derive(Subcommand, Debug)]
enum Commands {
    /// Make a direct A2A call to a mesh agent (no LLM)
    A2aCall {
        /// Target agent name (must be in registry)
        #[arg(long)]
        agent: String,
        /// Skill/tool name to invoke
        #[arg(long)]
        skill: String,
        /// JSON input (defaults to {})
        #[arg(long, default_value = "{}")]
        input: String,
    },
}

// ── main ─────────────────────────────────────────────────────────────────────

#[tokio::main]
async fn main() {
    tracing_subscriber::fmt()
        .with_env_filter(EnvFilter::from_default_env()
            .add_directive("mesh_agent=info".parse().unwrap()))
        .init();

    let cli = Cli::parse();

    // Subcommand: a2a-call
    if let Some(Commands::A2aCall { agent: target_agent, skill, input }) = cli.command {
        run_a2a_call(&cli.registry, &target_agent, &skill, &input).await;
        return;
    }

    // --- Agent turn mode ---
    let prompt = match cli.prompt {
        Some(p) => p,
        None => {
            let mut buf = String::new();
            std::io::stdin().read_to_string(&mut buf).expect("failed to read stdin");
            buf.trim().to_string()
        }
    };

    if prompt.is_empty() {
        eprintln!("mesh-agent: no prompt (use --prompt or pipe to stdin)");
        std::process::exit(1);
    }

    // Build router with optional Redis spend tracking
    let mut router_builder = Router::from_env(&cli.pool_id);
    if let Ok(client) = redis::Client::open(cli.redis_url.as_str()) {
        if let Ok(conn) = redis::aio::ConnectionManager::new(client).await {
            router_builder = router_builder.with_redis(conn);
            info!("spend tracking active");
        }
    }
    let router = Arc::new(router_builder);

    // Load or create session
    let session = if let Some(ref path) = cli.session_file {
        if path.exists() {
            Session::load_from_path(path)
                .map(|s| { info!("loaded session from {}", path.display()); s })
                .unwrap_or_else(|e| { error!("load session failed: {e}"); Session::new() })
        } else {
            Session::new()
        }
    } else {
        Session::new()
    };

    let tier = Tier::from_str(&cli.tier);
    let api_client = RouterApiClient::new(router, cli.model.clone(), tier);
    let tool_executor = make_tool_executor(&cli.registry);
    let permission_policy = PermissionPolicy::new(PermissionMode::Allow);
    let system_prompts = cli.system
        .map(|s| vec![s])
        .unwrap_or_else(|| vec![format!("You are the {} agent in the MrPink mesh.", cli.agent)]);

    let mut runtime = ConversationRuntime::new(
        session,
        api_client,
        tool_executor,
        permission_policy,
        system_prompts,
    ).with_max_iterations(cli.max_iterations);

    info!("agent={} tier={:?} model={:?}", cli.agent, tier, cli.model);

    match runtime.run_turn(&prompt, None) {
        Ok(summary) => {
            for msg in &summary.assistant_messages {
                for block in &msg.blocks {
                    if let ContentBlock::Text { text } = block {
                        print!("{text}");
                    }
                }
            }
            println!();
            info!("done: iter={} in={} out={}",
                  summary.iterations, summary.usage.input_tokens, summary.usage.output_tokens);

            if let Some(ref path) = cli.session_file {
                if let Err(e) = runtime.session().save_to_path(path) {
                    error!("save session failed: {e}");
                } else {
                    info!("session saved to {}", path.display());
                }
            }
        }
        Err(e) => { error!("runtime error: {e}"); std::process::exit(1); }
    }
}

// ── a2a-call subcommand ──────────────────────────────────────────────────────

async fn run_a2a_call(
    registry_path: &Option<PathBuf>,
    target_agent: &str,
    skill: &str,
    input_str: &str,
) {
    let path = registry_path.as_deref().unwrap_or_else(|| {
        eprintln!("a2a-call: --registry required");
        std::process::exit(1);
    });

    let registry = AgentRegistry::load(path).unwrap_or_else(|e| {
        eprintln!("failed to load registry: {e}");
        std::process::exit(1);
    });

    let entry = registry.get(target_agent).unwrap_or_else(|| {
        eprintln!("agent '{target_agent}' not in registry");
        std::process::exit(1);
    });

    let input: Value = serde_json::from_str(input_str)
        .unwrap_or(Value::Object(Default::default()));

    let client = a2a_client::A2aClient::new(
        entry.a2a_url.clone(),
        entry.token.clone(),
        entry.totp_seed.clone(),
    );

    match client.call(skill, input).await {
        Ok(result) => println!("{}", serde_json::to_string_pretty(&result).unwrap_or_default()),
        Err(e) => { eprintln!("a2a call failed: {e}"); std::process::exit(1); }
    }
}
