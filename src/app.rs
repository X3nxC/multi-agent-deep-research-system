use std::time::Duration;

use crossterm::event::{self, Event, KeyCode, KeyEvent, KeyEventKind};
use ratatui::{Terminal, backend::CrosstermBackend};

use crate::{
    backend::{AgentCommand, BackendEvent, BackendHandle},
    command::{InputAction, LocalCommand, parse_input},
    model::{AppState, ClearedSessionBaseline, Focus, LogSource},
    ui,
};

type AppResult<T> = Result<T, Box<dyn std::error::Error>>;

pub struct App {
    state: AppState,
    backend: BackendHandle,
}

impl App {
    pub fn new(backend: BackendHandle) -> Self {
        Self {
            state: AppState::default(),
            backend,
        }
    }

    pub fn run(
        &mut self,
        terminal: &mut Terminal<CrosstermBackend<std::io::Stdout>>,
    ) -> AppResult<()> {
        let _ = self.backend.send(AgentCommand::BootstrapSession);
        let _ = self.backend.send(AgentCommand::RefreshAgents);
        let _ = self.backend.send(AgentCommand::RefreshSession);

        while !self.state.should_quit {
            self.drain_backend_events();
            terminal.draw(|frame| ui::draw(frame, &self.state))?;

            let timeout = Duration::from_millis(250);
            if event::poll(timeout)? {
                if let Event::Key(key) = event::read()? {
                    if key.kind == KeyEventKind::Press {
                        self.handle_key(key);
                    }
                }
            }
        }

        Ok(())
    }

    fn drain_backend_events(&mut self) {
        while let Ok(event) = self.backend.try_recv() {
            self.apply_backend_event(event);
        }
    }

    fn apply_backend_event(&mut self, event: BackendEvent) {
        match event {
            BackendEvent::Connected { session_id } => {
                self.state.active_session_id = Some(session_id.clone());
            }
            BackendEvent::AgentsUpdated { agents } => {
                self.state.agents = agents;
                if self.state.selected_agent >= self.state.agents.len() {
                    self.state.selected_agent = self.state.agents.len().saturating_sub(1);
                }
            }
            BackendEvent::SessionUpdated {
                session_id,
                status,
                query,
                draft_report,
                final_report,
                errors,
            } => {
                let previous_status = self.state.session_status.clone();
                let incoming_baseline = ClearedSessionBaseline {
                    status: status.clone(),
                    query: query.clone(),
                    draft_report: draft_report.clone(),
                    final_report: final_report.clone(),
                };
                self.state.active_session_id = Some(session_id);
                self.state.session_status = status.clone();
                if let Some(cleared) = self.state.cleared_session_baseline.as_ref() {
                    if *cleared == incoming_baseline {
                        return;
                    }
                    self.state.cleared_session_baseline = None;
                }
                self.state.current_query = query;
                let previous_draft_report = self.state.draft_report.clone();
                let previous_final_report = self.state.final_report.clone();
                self.state.draft_report = draft_report.clone();
                self.state.final_report = final_report.clone();
                if let Some(report) = final_report {
                    if previous_final_report.as_deref() != Some(report.as_str()) {
                        self.state.push_log(
                            LogSource::Reporter,
                            format_report_log("Final Report", &report),
                        );
                    }
                } else if let Some(report) = draft_report {
                    if previous_draft_report.as_deref() != Some(report.as_str()) {
                        self.state.push_log(
                            LogSource::Reporter,
                            format_report_log("Draft Report", &report),
                        );
                    }
                }
                let _ = previous_status;
                for error in errors {
                    self.state.push_log(LogSource::Error, error);
                }
            }
            BackendEvent::AgentStatusChanged {
                agent_name,
                status,
                last_event,
            } => {
                let log_source = log_source_for_agent(Some(agent_name.as_str()));
                let log_message = format!("state: {} | {}", status.label(), last_event);
                let should_log_state = !self.last_log_matches(&log_source, last_event.as_str());
                if let Some(agent) = self
                    .state
                    .agents
                    .iter_mut()
                    .find(|agent| agent.name == agent_name)
                {
                    agent.status = status;
                    agent.last_event = last_event.clone();
                }
                if should_log_state {
                    self.push_synced_log(log_source, log_message);
                }
            }
            BackendEvent::Progress {
                offset,
                agent_name,
                message,
            } => {
                if let Some(offset) = offset {
                    self.state.last_stream_offset = self.state.last_stream_offset.max(offset);
                    if let Some(cutoff) = self.state.cleared_stream_offset {
                        if offset <= cutoff {
                            return;
                        }
                    }
                }
                self.push_progress_log(log_source_for_agent(agent_name.as_deref()), message);
            }
            BackendEvent::Error { message } => {
                self.state.push_log(LogSource::Error, message);
            }
        }
    }

    fn handle_key(&mut self, key: KeyEvent) {
        match key.code {
            KeyCode::Tab => {
                self.state.focus = match self.state.focus {
                    Focus::Agents => Focus::SessionView,
                    Focus::SessionView => Focus::Input,
                    Focus::Input => Focus::Agents,
                }
            }
            KeyCode::Up => {
                match self.state.focus {
                    Focus::Agents if self.state.selected_agent > 0 => {
                        self.state.selected_agent -= 1;
                    }
                    Focus::SessionView => {
                        self.state.session_view_offset_from_bottom = self
                            .state
                            .session_view_offset_from_bottom
                            .saturating_add(1);
                    }
                    _ => {}
                }
            }
            KeyCode::Down => {
                match self.state.focus {
                    Focus::Agents if self.state.selected_agent + 1 < self.state.agents.len() => {
                        self.state.selected_agent += 1;
                    }
                    Focus::SessionView => {
                        self.state.session_view_offset_from_bottom = self
                            .state
                            .session_view_offset_from_bottom
                            .saturating_sub(1);
                    }
                    _ => {}
                }
            }
            KeyCode::Left => {
                if self.state.focus == Focus::Input {
                    self.move_input_cursor_left();
                }
            }
            KeyCode::Right => {
                if self.state.focus == Focus::Input {
                    self.move_input_cursor_right();
                }
            }
            KeyCode::Backspace => {
                if self.state.focus == Focus::Input {
                    self.backspace_input();
                }
            }
            KeyCode::Enter => {
                if self.state.focus == Focus::Input {
                    self.submit_input();
                }
            }
            KeyCode::Char(c) => {
                if self.state.focus == Focus::Input {
                    self.insert_input_char(c);
                }
            }
            _ => {}
        }
    }

    fn submit_input(&mut self) {
        match parse_input(&self.state.input) {
            InputAction::Ignore => {}
            InputAction::Local(LocalCommand::Quit) => {
                self.state.should_quit = true;
                return;
            }
            InputAction::Local(LocalCommand::Accept(feedback)) => {
                self.send_backend_command(AgentCommand::AcceptReview { feedback });
                self.clear_input();
                return;
            }
            InputAction::Local(LocalCommand::Reject(feedback)) => {
                self.send_backend_command(AgentCommand::RejectReview { feedback });
                self.clear_input();
                return;
            }
            InputAction::Local(LocalCommand::Crash(reason)) => {
                self.send_backend_command(AgentCommand::TriggerCrash { reason });
                self.clear_input();
                return;
            }
            InputAction::Local(LocalCommand::Refresh) => {
                self.send_backend_command(AgentCommand::RefreshAgents);
                self.send_backend_command(AgentCommand::RefreshSession);
                self.clear_input();
                return;
            }
            InputAction::Local(LocalCommand::Clear) => {
                self.clear_screen_output();
                self.clear_input();
                return;
            }
            InputAction::Local(LocalCommand::Help) => {
                self.print_help();
                self.clear_input();
                return;
            }
            InputAction::SendToAgent(content) => {
                let Some(agent) = self.state.selected_agent().cloned() else {
                    self.state.push_log(LogSource::Error, "no agent selected");
                    return;
                };

                if !agent.input_permission.allows_direct_send() {
                    let reason = agent
                        .input_permission
                        .reason()
                        .unwrap_or("direct user input is disabled");
                    self.state.push_log(
                        LogSource::Error,
                        format!("direct input blocked for {}: {}", agent.name, reason),
                    );
                    return;
                }

                self.state.push_log(
                    LogSource::User,
                    format!("to {}: {}", agent.name, content),
                );
                self.send_backend_command(AgentCommand::StartResearch { content });
                self.clear_input();
                return;
            }
        }

        self.clear_input();
    }

    fn send_backend_command(&mut self, command: AgentCommand) {
        if let Err(error) = self.backend.send(command) {
            self.state.push_log(
                LogSource::Error,
                format!("failed to queue command: {}", error),
            );
        }
    }

    fn clear_screen_output(&mut self) {
        self.state.logs.clear();
        self.state.session_view_offset_from_bottom = 0;
        self.state.cleared_stream_offset = Some(self.state.last_stream_offset);
        self.state.cleared_session_baseline = Some(ClearedSessionBaseline {
            status: self.state.session_status.clone(),
            query: self.state.current_query.clone(),
            draft_report: self.state.draft_report.clone(),
            final_report: self.state.final_report.clone(),
        });
        self.state.current_query = None;
        self.state.draft_report = None;
        self.state.final_report = None;

        while self.backend.try_recv().is_ok() {}
    }

    fn print_help(&mut self) {
        let lines = [
            "local commands:",
            "/help - show this command list",
            "/clear - hide current output and only show newer events",
            "/refresh - reload agent and session state from backend",
            "/accept [opinion] - approve the current draft and optionally attach review text",
            "/reject [opinion] - request another research pass with optional review text",
            "/crash [reason] - intentionally crash the backend for debugging",
            "/q - quit the TUI",
            "plain text input is sent to the selected direct-input agent",
        ];
        for line in lines {
            self.state.push_log(LogSource::System, line);
        }
    }

    fn push_synced_log(&mut self, source: LogSource, message: String) {
        let should_skip = self
            .state
            .logs
            .last()
            .map(|entry| entry.source == source && entry.message == message)
            .unwrap_or(false);
        if !should_skip {
            self.state.push_log(source, message);
        }
    }

    fn push_progress_log(&mut self, source: LogSource, message: String) {
        if let Some(last_entry) = self.state.logs.last_mut() {
            if last_entry.source == source {
                if let Some(suffix) = state_log_suffix(&last_entry.message) {
                    if suffix == message {
                        last_entry.message = message;
                        return;
                    }
                }
            }
        }
        self.state.push_log(source, message);
    }

    fn last_log_matches(&self, source: &LogSource, message: &str) -> bool {
        self.state
            .logs
            .last()
            .map(|entry| &entry.source == source && entry.message == message)
            .unwrap_or(false)
    }

    fn clear_input(&mut self) {
        self.state.input.clear();
        self.state.input_cursor = 0;
    }

    fn move_input_cursor_left(&mut self) {
        if self.state.input_cursor > 0 {
            self.state.input_cursor -= 1;
        }
    }

    fn move_input_cursor_right(&mut self) {
        let len = self.state.input.chars().count();
        if self.state.input_cursor < len {
            self.state.input_cursor += 1;
        }
    }

    fn insert_input_char(&mut self, c: char) {
        let byte_index = char_to_byte_index(&self.state.input, self.state.input_cursor);
        self.state.input.insert(byte_index, c);
        self.state.input_cursor += 1;
    }

    fn backspace_input(&mut self) {
        if self.state.input_cursor == 0 {
            return;
        }

        let start = char_to_byte_index(&self.state.input, self.state.input_cursor - 1);
        let end = char_to_byte_index(&self.state.input, self.state.input_cursor);
        self.state.input.drain(start..end);
        self.state.input_cursor -= 1;
    }
}

fn char_to_byte_index(text: &str, char_index: usize) -> usize {
    text.char_indices()
        .nth(char_index)
        .map(|(index, _)| index)
        .unwrap_or(text.len())
}

fn state_log_suffix(message: &str) -> Option<&str> {
    message.split_once(" | ").map(|(_, suffix)| suffix)
}

fn format_report_log(title: &str, report: &str) -> String {
    let trimmed = report.trim();
    if trimmed.is_empty() {
        return title.to_string();
    }
    format!("{title}\n{trimmed}")
}

fn log_source_for_agent(agent_name: Option<&str>) -> LogSource {
    match agent_name {
        Some("coordinator") => LogSource::Coordinator,
        Some("planner") => LogSource::Planner,
        Some("researcher") => LogSource::Researcher,
        Some("reporter") => LogSource::Reporter,
        _ => LogSource::System,
    }
}
