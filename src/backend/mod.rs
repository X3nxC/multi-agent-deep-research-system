pub mod http_sse;
pub mod process;

use std::sync::mpsc::{Receiver, SendError, Sender, TryRecvError};

use crate::model::{AgentStatus, AgentSummary};

#[derive(Debug, Clone)]
pub enum AgentCommand {
    BootstrapSession,
    RefreshAgents,
    RefreshSession,
    StartResearch { content: String },
    AcceptReview { feedback: Option<String> },
    RejectReview { feedback: Option<String> },
    TriggerCrash { reason: Option<String> },
}

#[derive(Debug, Clone)]
pub enum BackendEvent {
    Connected {
        session_id: String,
    },
    AgentsUpdated {
        agents: Vec<AgentSummary>,
    },
    SessionUpdated {
        session_id: String,
        status: String,
        query: Option<String>,
        draft_report: Option<String>,
        final_report: Option<String>,
        errors: Vec<String>,
    },
    AgentStatusChanged {
        agent_name: String,
        status: AgentStatus,
        last_event: String,
    },
    Progress {
        offset: Option<i64>,
        agent_name: Option<String>,
        message: String,
    },
    Error {
        message: String,
    },
}

pub struct BackendHandle {
    tx_command: Sender<AgentCommand>,
    rx_event: Receiver<BackendEvent>,
}

impl BackendHandle {
    pub fn new(tx_command: Sender<AgentCommand>, rx_event: Receiver<BackendEvent>) -> Self {
        Self {
            tx_command,
            rx_event,
        }
    }

    pub fn send(&self, command: AgentCommand) -> Result<(), SendError<AgentCommand>> {
        self.tx_command.send(command)
    }

    pub fn try_recv(&self) -> Result<BackendEvent, TryRecvError> {
        self.rx_event.try_recv()
    }
}
