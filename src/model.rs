#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Focus {
    Agents,
    SessionView,
    Input,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum AgentStatus {
    Idle,
    Running,
    WaitingUser,
    Failed,
    Offline,
}

impl AgentStatus {
    pub fn label(&self) -> &'static str {
        match self {
            Self::Idle => "idle",
            Self::Running => "running",
            Self::WaitingUser => "waiting_user",
            Self::Failed => "failed",
            Self::Offline => "offline",
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum InputPermission {
    Allowed,
    Blocked { reason: String },
}

impl InputPermission {
    pub fn allows_direct_send(&self) -> bool {
        matches!(self, Self::Allowed)
    }

    pub fn label(&self) -> &'static str {
        match self {
            Self::Allowed => "direct",
            Self::Blocked { .. } => "managed",
        }
    }

    pub fn reason(&self) -> Option<&str> {
        match self {
            Self::Allowed => None,
            Self::Blocked { reason } => Some(reason.as_str()),
        }
    }
}

#[derive(Clone, Debug)]
pub struct AgentSummary {
    pub name: String,
    pub status: AgentStatus,
    pub last_event: String,
    pub input_permission: InputPermission,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum LogSource {
    System,
    User,
    Coordinator,
    Planner,
    Researcher,
    Reporter,
    Error,
}

#[derive(Clone, Debug)]
pub struct LogEntry {
    pub source: LogSource,
    pub message: String,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ClearedSessionBaseline {
    pub status: String,
    pub query: Option<String>,
    pub draft_report: Option<String>,
    pub final_report: Option<String>,
}

#[derive(Debug)]
pub struct AppState {
    pub should_quit: bool,
    pub focus: Focus,
    pub selected_agent: usize,
    pub session_view_offset_from_bottom: u16,
    pub input: String,
    pub input_cursor: usize,
    pub logs: Vec<LogEntry>,
    pub agents: Vec<AgentSummary>,
    pub active_session_id: Option<String>,
    pub session_status: String,
    pub current_query: Option<String>,
    pub draft_report: Option<String>,
    pub final_report: Option<String>,
    pub last_stream_offset: i64,
    pub cleared_stream_offset: Option<i64>,
    pub cleared_session_baseline: Option<ClearedSessionBaseline>,
}

impl Default for AppState {
    fn default() -> Self {
        Self {
            should_quit: false,
            focus: Focus::Input,
            selected_agent: 0,
            session_view_offset_from_bottom: 0,
            input: String::new(),
            input_cursor: 0,
            logs: vec![
                LogEntry {
                    source: LogSource::System,
                    message: "terminal UI started".into(),
                },
                LogEntry {
                    source: LogSource::System,
                    message: "waiting for local coordinator backend".into(),
                },
            ],
            agents: vec![
                AgentSummary {
                    name: "coordinator".into(),
                    status: AgentStatus::Offline,
                    last_event: "not connected".into(),
                    input_permission: InputPermission::Allowed,
                },
                AgentSummary {
                    name: "planner".into(),
                    status: AgentStatus::Offline,
                    last_event: "not connected".into(),
                    input_permission: InputPermission::Blocked {
                        reason: "planner only accepts work dispatched by coordinator".into(),
                    },
                },
                AgentSummary {
                    name: "researcher".into(),
                    status: AgentStatus::Offline,
                    last_event: "not connected".into(),
                    input_permission: InputPermission::Blocked {
                        reason: "researcher only accepts work dispatched by coordinator".into(),
                    },
                },
                AgentSummary {
                    name: "reporter".into(),
                    status: AgentStatus::Offline,
                    last_event: "not connected".into(),
                    input_permission: InputPermission::Blocked {
                        reason: "reporter only accepts work dispatched by coordinator".into(),
                    },
                },
            ],
            active_session_id: None,
            session_status: "idle".into(),
            current_query: None,
            draft_report: None,
            final_report: None,
            last_stream_offset: -1,
            cleared_stream_offset: None,
            cleared_session_baseline: None,
        }
    }
}

impl AppState {
    pub fn selected_agent(&self) -> Option<&AgentSummary> {
        self.agents.get(self.selected_agent)
    }

    pub fn push_log(&mut self, source: LogSource, message: impl Into<String>) {
        self.logs.push(LogEntry {
            source,
            message: message.into(),
        });
        self.trim_logs();
    }

    fn trim_logs(&mut self) {
        const MAX_LOGS: usize = 200;
        if self.logs.len() > MAX_LOGS {
            let drop_count = self.logs.len() - MAX_LOGS;
            self.logs.drain(0..drop_count);
        }
    }
}
