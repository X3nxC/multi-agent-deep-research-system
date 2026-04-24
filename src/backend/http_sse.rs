use std::{
    io::{BufRead, BufReader},
    sync::mpsc,
    thread,
    time::Duration,
};

use reqwest::blocking::{Client, Response};
use serde_json::{Value, json};

use crate::{
    backend::{AgentCommand, BackendEvent, BackendHandle},
    model::{AgentStatus, AgentSummary, InputPermission},
};

pub struct HttpSseBackend {
    base_url: String,
    session_id: String,
}

impl HttpSseBackend {
    pub fn spawn(base_url: String, session_id: String) -> BackendHandle {
        Self { base_url, session_id }.spawn_inner()
    }

    fn spawn_inner(self) -> BackendHandle {
        let (tx_command, rx_command) = mpsc::channel();
        let (tx_event, rx_event) = mpsc::channel();

        let command_base_url = self.base_url.clone();
        let command_session_id = self.session_id.clone();
        let command_tx_event = tx_event.clone();
        thread::spawn(move || {
            let client = Client::builder()
                .timeout(Duration::from_secs(30))
                .build()
                .expect("failed to build blocking reqwest client");

            let _ = command_tx_event.send(BackendEvent::Connected {
                session_id: command_session_id.clone(),
            });

            let _ = refresh_agents(&client, &command_base_url, &command_session_id, &command_tx_event);
            let _ = refresh_session(&client, &command_base_url, &command_session_id, &command_tx_event);

            while let Ok(command) = rx_command.recv() {
                let result = match command {
                    AgentCommand::BootstrapSession => bootstrap_session(
                        &client,
                        &command_base_url,
                        &command_session_id,
                        &command_tx_event,
                    ),
                    AgentCommand::RefreshAgents => {
                        refresh_agents(&client, &command_base_url, &command_session_id, &command_tx_event)
                    }
                    AgentCommand::RefreshSession => {
                        refresh_session(&client, &command_base_url, &command_session_id, &command_tx_event)
                    }
                    AgentCommand::StartResearch { content } => start_research(
                        &client,
                        &command_base_url,
                        &command_session_id,
                        &content,
                        &command_tx_event,
                    ),
                    AgentCommand::AcceptReview { feedback } => approve_review(
                        &client,
                        &command_base_url,
                        &command_session_id,
                        feedback.as_deref(),
                        &command_tx_event,
                    ),
                    AgentCommand::RejectReview { feedback } => reject_review(
                        &client,
                        &command_base_url,
                        &command_session_id,
                        feedback.as_deref(),
                        &command_tx_event,
                    ),
                    AgentCommand::TriggerCrash { reason } => trigger_crash(
                        &client,
                        &command_base_url,
                        &command_session_id,
                        reason.as_deref(),
                        &command_tx_event,
                    ),
                };

                if let Err(message) = result {
                    let _ = command_tx_event.send(BackendEvent::Error { message });
                }
            }

            let _ = command_tx_event.send(BackendEvent::Error {
                message: "backend command channel closed".into(),
            });
        });

        let sse_base_url = self.base_url.clone();
        let sse_session_id = self.session_id.clone();
        thread::spawn(move || {
            loop {
                let client = Client::new();
                let stream_url = format!(
                    "{}/api/research/{}/stream",
                    sse_base_url.trim_end_matches('/'),
                    sse_session_id
                );
                let response = match client.get(&stream_url).send() {
                    Ok(response) => response,
                    Err(error) => {
                        let _ = tx_event.send(BackendEvent::Error {
                            message: format!("SSE connect failed: {}", error),
                        });
                        thread::sleep(Duration::from_secs(2));
                        continue;
                    }
                };

                if response.status().as_u16() == 404 {
                    thread::sleep(Duration::from_secs(1));
                    continue;
                }

                if !response.status().is_success() {
                    let _ = tx_event.send(BackendEvent::Error {
                        message: format!("SSE stream returned HTTP {}", response.status()),
                    });
                    thread::sleep(Duration::from_secs(2));
                    continue;
                }

                let mut current_event = String::new();
                let mut current_data = String::new();

                for line in BufReader::new(response).lines() {
                    let Ok(line) = line else {
                        break;
                    };

                    if line.is_empty() {
                        dispatch_sse_event(&tx_event, &current_event, &current_data);
                        current_event.clear();
                        current_data.clear();
                        continue;
                    }

                    if let Some(value) = line.strip_prefix("event:") {
                        current_event = value.trim().to_string();
                        continue;
                    }

                    if let Some(value) = line.strip_prefix("data:") {
                        if !current_data.is_empty() {
                            current_data.push('\n');
                        }
                        current_data.push_str(value.trim_start());
                    }
                }
                thread::sleep(Duration::from_secs(1));
            }
        });

        BackendHandle::new(tx_command, rx_event)
    }
}

fn refresh_agents(
    client: &Client,
    base_url: &str,
    session_id: &str,
    tx_event: &mpsc::Sender<BackendEvent>,
) -> Result<(), String> {
    let mut agents = default_agents();

    match client
        .get(format!("{}/health", base_url.trim_end_matches('/')))
        .send()
    {
        Ok(response) if response.status().is_success() => {
            if let Some(agent) = agents.iter_mut().find(|agent| agent.name == "coordinator") {
                agent.status = AgentStatus::Idle;
                agent.last_event = "coordinator available".into();
            }
        }
        Ok(response) => {
            if let Some(agent) = agents.iter_mut().find(|agent| agent.name == "coordinator") {
                agent.status = AgentStatus::Offline;
                agent.last_event = format!("health check failed: HTTP {}", response.status());
            }
        }
        Err(error) => {
            if let Some(agent) = agents.iter_mut().find(|agent| agent.name == "coordinator") {
                agent.status = AgentStatus::Offline;
                agent.last_event = format!("health check failed: {}", error);
            }
        }
    }

    let availability_response = client
        .get(format!(
            "{}/internal/workers/availability",
            base_url.trim_end_matches('/')
        ))
        .send()
        .map_err(|error| format!("failed to load worker availability: {}", error))?;
    let availability_payload = availability_response
        .json::<Value>()
        .map_err(|error| format!("failed to parse worker availability: {}", error))?;

    if let Some(workers) = availability_payload.get("workers").and_then(Value::as_object) {
        for (worker_name, worker_state) in workers {
            if let Some(agent) = agents.iter_mut().find(|agent| agent.name == *worker_name) {
                let available = worker_state
                    .get("available")
                    .and_then(Value::as_bool)
                    .unwrap_or(false);
                agent.status = if available {
                    AgentStatus::Idle
                } else {
                    AgentStatus::Offline
                };
                agent.last_event = if available {
                    "worker available".into()
                } else {
                    worker_state
                        .get("last_error")
                        .and_then(Value::as_str)
                        .unwrap_or("worker unavailable")
                        .to_string()
                };
            }
        }
    }

    let session_agents_url = format!(
        "{}/api/research/{}/agents",
        base_url.trim_end_matches('/'),
        session_id
    );
    if let Ok(response) = client.get(&session_agents_url).send() {
        if response.status().is_success() {
            let payload = response
                .json::<Value>()
                .map_err(|error| format!("failed to parse session agents: {}", error))?;
            if let Some(session_agents) = payload.get("agents").and_then(Value::as_object) {
                for (agent_name, agent_payload) in session_agents {
                    if let Some(agent) = agents.iter_mut().find(|entry| entry.name == *agent_name) {
                        agent.status = map_agent_status_value(agent_payload);
                        agent.last_event = agent_last_event(agent_payload);
                    }
                }
            }
        }
    }

    tx_event
        .send(BackendEvent::AgentsUpdated { agents })
        .map_err(|error| format!("failed to publish agents update: {}", error))?;
    Ok(())
}

fn bootstrap_session(
    client: &Client,
    base_url: &str,
    session_id: &str,
    tx_event: &mpsc::Sender<BackendEvent>,
) -> Result<(), String> {
    let response = client
        .post(format!(
            "{}/api/research/{}/command",
            base_url.trim_end_matches('/'),
            session_id
        ))
        .json(&json!({
            "type": "ack",
        }))
        .send()
        .map_err(|error| format!("failed to bootstrap session: {}", error))?;

    if !response.status().is_success() {
        return Err(error_response_message(response, "failed to bootstrap session"));
    }

    let _ = refresh_agents(client, base_url, session_id, tx_event);
    let _ = refresh_session(client, base_url, session_id, tx_event);
    Ok(())
}

fn refresh_session(
    client: &Client,
    base_url: &str,
    session_id: &str,
    tx_event: &mpsc::Sender<BackendEvent>,
) -> Result<(), String> {
    let session_url = format!(
        "{}/api/research/{}",
        base_url.trim_end_matches('/'),
        session_id
    );
    let response = client
        .get(&session_url)
        .send()
        .map_err(|error| format!("failed to load session: {}", error))?;

    if response.status().as_u16() == 404 {
        tx_event
            .send(BackendEvent::SessionUpdated {
                session_id: session_id.to_string(),
                status: "idle".into(),
                query: None,
                draft_report: None,
                final_report: None,
                errors: Vec::new(),
            })
            .map_err(|error| format!("failed to publish idle session state: {}", error))?;
        return Ok(());
    }

    if !response.status().is_success() {
        return Err(error_response_message(response, "failed to load session"));
    }

    let payload = response
        .json::<Value>()
        .map_err(|error| format!("failed to parse session payload: {}", error))?;
    publish_session_update(tx_event, &payload)
}

fn start_research(
    client: &Client,
    base_url: &str,
    session_id: &str,
    content: &str,
    tx_event: &mpsc::Sender<BackendEvent>,
) -> Result<(), String> {
    let response = client
        .post(format!(
            "{}/api/research/{}/command",
            base_url.trim_end_matches('/'),
            session_id
        ))
        .json(&json!({
            "type": "user_message",
            "content": content,
        }))
        .send()
        .map_err(|error| format!("failed to start research: {}", error))?;

    if !response.status().is_success() {
        return Err(error_response_message(response, "failed to start research"));
    }

    let payload = response
        .json::<Value>()
        .map_err(|error| format!("failed to parse start response: {}", error))?;
    if let Some(text) = payload.get("text").and_then(Value::as_str) {
        let _ = tx_event.send(BackendEvent::Progress {
            offset: None,
            agent_name: Some("coordinator".into()),
            message: text.to_string(),
        });
    }
    let _ = refresh_agents(client, base_url, session_id, tx_event);
    let _ = refresh_session(client, base_url, session_id, tx_event);
    Ok(())
}

fn approve_review(
    client: &Client,
    base_url: &str,
    session_id: &str,
    feedback: Option<&str>,
    tx_event: &mpsc::Sender<BackendEvent>,
) -> Result<(), String> {
    let response = client
        .post(format!(
            "{}/api/research/{}/command",
            base_url.trim_end_matches('/'),
            session_id
        ))
        .json(&json!({
            "type": "accept",
            "feedback": feedback,
        }))
        .send()
        .map_err(|error| format!("failed to approve review: {}", error))?;

    if !response.status().is_success() {
        return Err(error_response_message(response, "failed to approve review"));
    }

    let payload = response
        .json::<Value>()
        .map_err(|error| format!("failed to parse approve response: {}", error))?;
    if let Some(text) = payload.get("text").and_then(Value::as_str) {
        let _ = tx_event.send(BackendEvent::Progress {
            offset: None,
            agent_name: Some("coordinator".into()),
            message: text.to_string(),
        });
    }
    let _ = refresh_agents(client, base_url, session_id, tx_event);
    let _ = refresh_session(client, base_url, session_id, tx_event);
    Ok(())
}

fn reject_review(
    client: &Client,
    base_url: &str,
    session_id: &str,
    feedback: Option<&str>,
    tx_event: &mpsc::Sender<BackendEvent>,
) -> Result<(), String> {
    let response = client
        .post(format!(
            "{}/api/research/{}/command",
            base_url.trim_end_matches('/'),
            session_id
        ))
        .json(&json!({
            "type": "reject",
            "feedback": feedback,
        }))
        .send()
        .map_err(|error| format!("failed to reject review: {}", error))?;

    if !response.status().is_success() {
        return Err(error_response_message(response, "failed to reject review"));
    }

    let payload = response
        .json::<Value>()
        .map_err(|error| format!("failed to parse reject response: {}", error))?;
    if let Some(text) = payload.get("text").and_then(Value::as_str) {
        let _ = tx_event.send(BackendEvent::Progress {
            offset: None,
            agent_name: Some("coordinator".into()),
            message: text.to_string(),
        });
    }
    let _ = refresh_agents(client, base_url, session_id, tx_event);
    let _ = refresh_session(client, base_url, session_id, tx_event);
    Ok(())
}

fn trigger_crash(
    client: &Client,
    base_url: &str,
    session_id: &str,
    reason: Option<&str>,
    _tx_event: &mpsc::Sender<BackendEvent>,
) -> Result<(), String> {
    let response = client
        .post(format!("{}/api/debug/crash", base_url.trim_end_matches('/')))
        .json(&json!({
            "session_id": session_id,
            "reason": reason.unwrap_or("intentional backend crash from TUI"),
        }))
        .send();

    match response {
        Ok(response) if response.status().is_success() => Ok(()),
        Ok(response) => Err(error_response_message(response, "backend crash endpoint returned")),
        Err(error) => Err(format!("backend crash request disconnected: {}", error)),
    }
}

fn dispatch_sse_event(
    tx_event: &mpsc::Sender<BackendEvent>,
    event_name: &str,
    data: &str,
) {
    if data.is_empty() {
        return;
    }

    let parsed = match serde_json::from_str::<Value>(data) {
        Ok(value) => value,
        Err(_) => return,
    };

    match event_name {
        "snapshot" => {
            if let Some(session_payload) = parsed.get("session") {
                let _ = publish_session_update(tx_event, session_payload);
            }
            if let Some(agent_payload) = parsed.get("agents") {
                let _ = publish_agent_snapshot(tx_event, agent_payload);
            }
        }
        "progress" => {
            let event_type = parsed
                .get("event_type")
                .and_then(Value::as_str)
                .unwrap_or("message");
            match event_type {
                "agent" => {
                    let agent_name = parsed
                        .get("agent_name")
                        .and_then(Value::as_str)
                        .unwrap_or("unknown")
                        .to_string();
                    let status = map_agent_status_from_queue(&parsed);
                    let last_event = parsed
                        .get("message")
                        .and_then(Value::as_str)
                        .unwrap_or("updated")
                        .to_string();
                    let _ = tx_event.send(BackendEvent::AgentStatusChanged {
                        agent_name,
                        status,
                        last_event,
                    });
                }
                "session" => {
                    let _ = publish_session_update_from_queue(tx_event, &parsed);
                }
                _ => {
                    if let Some(message) = parsed.get("message").and_then(Value::as_str) {
                        let agent_name = parsed
                            .get("agent_name")
                            .and_then(Value::as_str)
                            .map(ToString::to_string);
                        let offset = parsed.get("offset").and_then(Value::as_i64);
                        let _ = tx_event.send(BackendEvent::Progress {
                            offset,
                            agent_name,
                            message: message.to_string(),
                        });
                    }
                }
            }
        }
        "terminal" => {
            let _ = publish_session_update_from_queue(tx_event, &parsed);
        }
        _ => {}
    }
}

fn publish_agent_snapshot(
    tx_event: &mpsc::Sender<BackendEvent>,
    payload: &Value,
) -> Result<(), String> {
    let session_agents = payload
        .get("agents")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    let mut agents = default_agents();
    for (agent_name, agent_payload) in session_agents {
        if let Some(agent) = agents.iter_mut().find(|entry| entry.name == agent_name) {
            agent.status = map_agent_status_value(&agent_payload);
            agent.last_event = agent_last_event(&agent_payload);
        }
    }
    tx_event
        .send(BackendEvent::AgentsUpdated { agents })
        .map_err(|error| format!("failed to publish agents snapshot: {}", error))
}

fn publish_session_update(
    tx_event: &mpsc::Sender<BackendEvent>,
    payload: &Value,
) -> Result<(), String> {
    tx_event
        .send(BackendEvent::SessionUpdated {
            session_id: payload
                .get("session_id")
                .and_then(Value::as_str)
                .unwrap_or("test-session")
                .to_string(),
            status: payload
                .get("status")
                .and_then(Value::as_str)
                .unwrap_or("unknown")
                .to_string(),
            query: payload
                .get("query")
                .and_then(Value::as_str)
                .map(ToString::to_string),
            draft_report: payload
                .get("draft_report")
                .and_then(Value::as_str)
                .map(ToString::to_string),
            final_report: payload
                .get("final_report")
                .and_then(Value::as_str)
                .map(ToString::to_string),
            errors: payload
                .get("errors")
                .and_then(Value::as_array)
                .map(|items| {
                    items
                        .iter()
                        .filter_map(Value::as_str)
                        .map(ToString::to_string)
                        .collect::<Vec<_>>()
                })
                .unwrap_or_default(),
        })
        .map_err(|error| format!("failed to publish session update: {}", error))
}

fn publish_session_update_from_queue(
    tx_event: &mpsc::Sender<BackendEvent>,
    payload: &Value,
) -> Result<(), String> {
    let data = payload.get("data").and_then(Value::as_object);
    tx_event
        .send(BackendEvent::SessionUpdated {
            session_id: payload
                .get("session_id")
                .and_then(Value::as_str)
                .unwrap_or("test-session")
                .to_string(),
            status: payload
                .get("session_status")
                .or_else(|| payload.get("status"))
                .and_then(Value::as_str)
                .unwrap_or("unknown")
                .to_string(),
            query: data
                .and_then(|item| item.get("query"))
                .and_then(Value::as_str)
                .map(ToString::to_string),
            draft_report: data
                .and_then(|item| item.get("draft_report"))
                .and_then(Value::as_str)
                .map(ToString::to_string),
            final_report: data
                .and_then(|item| item.get("final_report"))
                .and_then(Value::as_str)
                .map(ToString::to_string),
            errors: data
                .and_then(|item| item.get("errors"))
                .and_then(Value::as_array)
                .map(|items| {
                    items
                        .iter()
                        .filter_map(Value::as_str)
                        .map(ToString::to_string)
                        .collect::<Vec<_>>()
                })
                .unwrap_or_default(),
        })
        .map_err(|error| format!("failed to publish queued session update: {}", error))
}

fn default_agents() -> Vec<AgentSummary> {
    vec![
        AgentSummary {
            name: "coordinator".into(),
            status: AgentStatus::Offline,
            last_event: "not connected".into(),
            input_permission: InputPermission::Allowed,
        },
        AgentSummary {
            name: "planner".into(),
            status: AgentStatus::Offline,
            last_event: "worker unavailable".into(),
            input_permission: InputPermission::Blocked {
                reason: "planner only accepts work dispatched by coordinator".into(),
            },
        },
        AgentSummary {
            name: "researcher".into(),
            status: AgentStatus::Offline,
            last_event: "worker unavailable".into(),
            input_permission: InputPermission::Blocked {
                reason: "researcher only accepts work dispatched by coordinator".into(),
            },
        },
        AgentSummary {
            name: "reporter".into(),
            status: AgentStatus::Offline,
            last_event: "worker unavailable".into(),
            input_permission: InputPermission::Blocked {
                reason: "reporter only accepts work dispatched by coordinator".into(),
            },
        },
    ]
}

fn error_response_message(response: Response, context: &str) -> String {
    let status = response.status();
    let body = response.text().unwrap_or_default();
    if let Ok(payload) = serde_json::from_str::<Value>(&body) {
        if let Some(detail) = payload.get("detail").and_then(Value::as_str) {
            return format!("{context}: HTTP {status} - {detail}");
        }
        if let Some(text) = payload.get("text").and_then(Value::as_str) {
            return format!("{context}: HTTP {status} - {text}");
        }
    }

    let snippet = body.trim();
    if snippet.is_empty() {
        format!("{context}: HTTP {status}")
    } else {
        format!("{context}: HTTP {status} - {snippet}")
    }
}

fn map_agent_status_value(payload: &Value) -> AgentStatus {
    let state = payload
        .get("state")
        .and_then(Value::as_str)
        .unwrap_or("unknown");
    let details = payload.get("details").and_then(Value::as_object);
    let activity = details
        .and_then(|details| details.get("activity"))
        .and_then(Value::as_str)
        .unwrap_or(state);

    match (state, activity) {
        ("running", _) => AgentStatus::Running,
        ("error", _) => AgentStatus::Failed,
        ("suspended", "awaiting_review") => AgentStatus::WaitingUser,
        ("stopped", _) => AgentStatus::Offline,
        (_, "error") => AgentStatus::Failed,
        _ => AgentStatus::Idle,
    }
}

fn map_agent_status_from_queue(payload: &Value) -> AgentStatus {
    match payload
        .get("agent_state")
        .and_then(Value::as_str)
        .unwrap_or("unknown")
    {
        "running" => AgentStatus::Running,
        "error" => AgentStatus::Failed,
        "stopped" => AgentStatus::Offline,
        _ => {
            let activity = payload
                .get("data")
                .and_then(Value::as_object)
                .and_then(|item| item.get("activity"))
                .and_then(Value::as_str)
                .unwrap_or("");
            if activity == "awaiting_review" {
                AgentStatus::WaitingUser
            } else {
                AgentStatus::Idle
            }
        }
    }
}

fn agent_last_event(payload: &Value) -> String {
    let details = payload.get("details").and_then(Value::as_object);
    if let Some(message) = details
        .and_then(|details| details.get("user_message"))
        .and_then(Value::as_str)
    {
        return message.to_string();
    }
    if let Some(error) = details
        .and_then(|details| details.get("error"))
        .and_then(Value::as_str)
    {
        return error.to_string();
    }
    if let Some(activity) = details
        .and_then(|details| details.get("activity"))
        .and_then(Value::as_str)
    {
        return activity.to_string();
    }
    payload
        .get("state")
        .and_then(Value::as_str)
        .unwrap_or("unknown")
        .to_string()
}
