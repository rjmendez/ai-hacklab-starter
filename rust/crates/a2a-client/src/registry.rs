//! Agent registry — loads agent_registry.json and provides dispatch routing.

use std::collections::HashMap;
use std::path::Path;

use serde::{Deserialize, Serialize};
use thiserror::Error;

#[derive(Debug, Error)]
pub enum RegistryError {
    #[error("io: {0}")]
    Io(#[from] std::io::Error),
    #[error("json: {0}")]
    Json(#[from] serde_json::Error),
    #[error("no agent registered for skill prefix: {0}")]
    NoRoute(String),
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AgentEntry {
    pub name: String,
    pub a2a_url: String,
    pub token: String,
    pub totp_seed: Option<String>,
    #[serde(default)]
    pub capabilities: Vec<String>,
}

/// Loaded from agent_registry.json — wraps `{ "agents": { ... } }`.
#[derive(Debug, Clone)]
pub struct AgentRegistry {
    agents: HashMap<String, AgentEntry>,
}

impl AgentRegistry {
    pub fn empty() -> Self {
        Self { agents: Default::default() }
    }

    pub fn load(path: &Path) -> Result<Self, RegistryError> {
        let raw = std::fs::read_to_string(path)?;
        #[derive(Deserialize)]
        struct Root { agents: HashMap<String, AgentEntry> }
        let root: Root = serde_json::from_str(&raw)?;
        Ok(Self { agents: root.agents })
    }

    pub fn get(&self, name: &str) -> Option<&AgentEntry> {
        self.agents.get(name)
    }

    pub fn all(&self) -> impl Iterator<Item = (&str, &AgentEntry)> {
        self.agents.iter().map(|(k, v)| (k.as_str(), v))
    }
}

/// Maps skill names / prefixes to owning agent names.
///
/// Rules mirror the dispatch policy documented in MEMORY.md:
///   findings_* / pipeline_* / data_* / report_* / crypto_* / contract_* → charlie
///   gpu_* / hashcat_* / docker_*                                         → oxalis
///   osint_* / firebase_* / browser_* / ct_*                              → mrpink
///   db_* / batch_* / archive_*                                           → rex
pub struct DispatchPolicy {
    /// (prefix, agent_name) — checked in order, first match wins.
    rules: Vec<(String, String)>,
}

impl DispatchPolicy {
    pub fn empty() -> Self {
        Self { rules: vec![] }
    }

    /// Build from the default mesh ownership rules.
    pub fn default_mesh() -> Self {
        let rules = vec![
            // Charlie owns pipeline + findings + reports + crypto/contract analysis
            ("findings_",  "charlie"),
            ("pipeline_",  "charlie"),
            ("data_",      "charlie"),
            ("report_",    "charlie"),
            ("crypto_",    "charlie"),
            ("contract_",  "charlie"),
            // Oxalis owns GPU / hashcat / docker
            ("gpu_",       "oxalis"),
            ("hashcat_",   "oxalis"),
            ("docker_",    "oxalis"),
            // MrPink owns OSINT / firebase / browser / CT
            ("osint_",     "mrpink"),
            ("firebase_",  "mrpink"),
            ("browser_",   "mrpink"),
            ("ct_",        "mrpink"),
            // Rex owns DB ingestion, batch, archive
            ("db_",        "rex"),
            ("batch_",     "rex"),
            ("archive_",   "rex"),
        ];
        Self {
            rules: rules.into_iter().map(|(p, a)| (p.to_string(), a.to_string())).collect(),
        }
    }

    /// Return the agent name that owns `skill_id`, or Err if no rule matches.
    pub fn route<'a>(&self, skill_id: &'a str) -> Result<&str, RegistryError> {
        for (prefix, agent) in &self.rules {
            if skill_id.starts_with(prefix.as_str()) || skill_id == prefix.trim_end_matches('_') {
                return Ok(agent.as_str());
            }
        }
        // Exact match fallback for skills without a clear prefix (e.g. "mesh_ping")
        Err(RegistryError::NoRoute(skill_id.to_string()))
    }
}
