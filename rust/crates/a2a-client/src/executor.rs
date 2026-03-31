//! A2AToolExecutor — implements runtime::ToolExecutor by dispatching to the mesh via A2A.

use serde_json::Value;

use crate::client::{A2aClient, A2aError};
use crate::registry::{AgentRegistry, DispatchPolicy, RegistryError};

/// Error type for tool execution failures.
#[derive(Debug)]
pub enum ToolError {
    NoRoute(String),
    A2a(A2aError),
    Registry(RegistryError),
}

impl std::fmt::Display for ToolError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::NoRoute(s) => write!(f, "no route for skill: {s}"),
            Self::A2a(e) => write!(f, "a2a error: {e}"),
            Self::Registry(e) => write!(f, "registry error: {e}"),
        }
    }
}

impl From<A2aError> for ToolError {
    fn from(e: A2aError) -> Self { Self::A2a(e) }
}

impl From<RegistryError> for ToolError {
    fn from(e: RegistryError) -> Self { Self::Registry(e) }
}

/// Routes `ConversationRuntime` tool calls to the appropriate mesh agent via A2A.
///
/// Tool name → DispatchPolicy → AgentEntry → A2aClient::call()
pub struct A2aToolExecutor {
    registry: AgentRegistry,
    policy: DispatchPolicy,
    rt: tokio::runtime::Handle,
}

impl A2aToolExecutor {
    pub fn new(registry: AgentRegistry, policy: DispatchPolicy) -> Self {
        Self {
            registry,
            policy,
            rt: tokio::runtime::Handle::current(),
        }
    }

    /// Execute a tool call by routing to the owning agent.
    /// `input` is a JSON string (as passed by ConversationRuntime).
    pub fn execute(&mut self, tool_name: &str, input: &str) -> Result<String, ToolError> {
        let agent_name = self.policy.route(tool_name)?;
        let entry = self.registry.get(agent_name)
            .ok_or_else(|| ToolError::NoRoute(agent_name.to_string()))?;

        let client = A2aClient::new(
            entry.a2a_url.clone(),
            entry.token.clone(),
            entry.totp_seed.clone(),
        );

        let input_val: Value = serde_json::from_str(input).unwrap_or(Value::Null);
        let result = self.rt.block_on(client.call(tool_name, input_val))?;
        Ok(result.to_string())
    }
}
