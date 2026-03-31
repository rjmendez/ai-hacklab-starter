//! Envelope — wire format matching the Python MeshQueue schema exactly.

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use uuid::Uuid;

/// A message envelope in the mesh queue.
/// Schema must stay compatible with queue/mesh_queue.py.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Envelope {
    pub id: String,
    pub from: String,
    pub to: String,
    pub skill_id: String,
    pub input: Value,
    pub reply_to: String,
    pub created_at: DateTime<Utc>,
    #[serde(default)]
    pub attempts: u32,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub result: Option<Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub reply_to_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub dead_letter_reason: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub dead_letter_at: Option<DateTime<Utc>>,
}

impl Envelope {
    pub fn new(from: &str, to: &str, skill_id: &str, input: Value) -> Self {
        let inbox_key = format!("mesh:inbox:{from}");
        Self {
            id: Uuid::new_v4().to_string(),
            from: from.to_string(),
            to: to.to_string(),
            skill_id: skill_id.to_string(),
            input,
            reply_to: inbox_key,
            created_at: Utc::now(),
            attempts: 0,
            result: None,
            reply_to_id: None,
            dead_letter_reason: None,
            dead_letter_at: None,
        }
    }

    pub fn reply_envelope(&self, from_agent: &str, result: Value) -> Self {
        Self {
            id: Uuid::new_v4().to_string(),
            from: from_agent.to_string(),
            to: self.from.clone(),
            skill_id: format!("{}:reply", self.skill_id),
            input: Value::Null,
            reply_to: format!("mesh:inbox:{from_agent}"),
            created_at: Utc::now(),
            attempts: 0,
            result: Some(result),
            reply_to_id: Some(self.id.clone()),
            dead_letter_reason: None,
            dead_letter_at: None,
        }
    }
}
