use ratatui::{
    Frame,
    layout::{Constraint, Direction, Layout, Rect},
    style::{Color, Modifier, Style, Stylize},
    text::{Line, Text},
    widgets::{Block, Borders, List, ListItem, ListState, Paragraph, Wrap},
};

use crate::model::{AppState, Focus, InputPermission, LogEntry, LogSource};

pub fn draw(frame: &mut Frame, state: &AppState) {
    let root = Layout::default()
        .direction(Direction::Horizontal)
        .constraints([Constraint::Percentage(28), Constraint::Percentage(72)])
        .split(frame.area());

    let left = Layout::default()
        .direction(Direction::Vertical)
        .constraints([Constraint::Min(10), Constraint::Length(8)])
        .split(root[0]);

    let right = Layout::default()
        .direction(Direction::Vertical)
        .constraints([Constraint::Min(10), Constraint::Length(5)])
        .split(root[1]);

    render_agents(frame, state, left[0]);
    render_help(frame, state, left[1]);
    render_logs(frame, state, right[0]);
    render_input(frame, state, right[1]);
}

fn render_agents(frame: &mut Frame, state: &AppState, area: Rect) {
    let items: Vec<ListItem> = state
        .agents
        .iter()
        .map(|agent| {
            ListItem::new(vec![
                Line::from(agent.name.as_str().bold()),
                Line::from(format!("state: {}", agent.status.label()))
                    .style(status_style(agent.status.label())),
                Line::from(format!("input: {}", agent.input_permission.label())).fg(
                    match agent.input_permission {
                        InputPermission::Allowed => Color::Green,
                        InputPermission::Blocked { .. } => Color::Yellow,
                    },
                ),
                Line::from(format!("last: {}", agent.last_event)).fg(Color::DarkGray),
            ])
        })
        .collect();

    let mut list_state = ListState::default().with_selected(Some(state.selected_agent));
    let title = if state.focus == Focus::Agents {
        " Agents "
    } else {
        " Agents"
    };

    let list = List::new(items)
        .block(
            Block::default()
                .title(title)
                .borders(Borders::ALL)
                .border_style(focus_style(state, Focus::Agents)),
        )
        .highlight_style(
            Style::default()
                .fg(Color::Black)
                .bg(Color::Green)
                .add_modifier(Modifier::BOLD),
        )
        .highlight_symbol(">> ");

    frame.render_stateful_widget(list, area, &mut list_state);
}

fn render_help(frame: &mut Frame, state: &AppState, area: Rect) {
    let selected = state.selected_agent();
    let active_name = selected.map(|agent| agent.name.as_str()).unwrap_or("none");
    let active_session = state.active_session_id.as_deref().unwrap_or("none");
    let report_state = if state.final_report.is_some() {
        "final report ready"
    } else if state.draft_report.is_some() {
        "draft report ready"
    } else {
        "no report yet"
    };
    let input_mode = selected
        .map(|agent| match &agent.input_permission {
            InputPermission::Allowed => "direct send enabled".to_string(),
            InputPermission::Blocked { reason } => format!("direct send blocked: {}", reason),
        })
        .unwrap_or_else(|| "no agent selected".into());

    let text = Text::from(vec![
        Line::from("Tab  switch focus"),
        Line::from("Up/Down  navigate focused pane"),
        Line::from("Enter  submit input"),
        Line::from("/help  show commands"),
        Line::from(""),
        Line::from(format!("session: {}", active_session)).style(Style::default().fg(Color::Magenta)),
        Line::from(format!("session state: {}", state.session_status))
            .style(status_style(state.session_status.as_str())),
        Line::from(report_state).style(Style::default().fg(Color::LightYellow)),
        Line::from(format!("active agent: {}", active_name))
            .style(Style::default().fg(Color::Yellow)),
        Line::from(input_mode).style(Style::default().fg(Color::Cyan)),
    ]);

    let help = Paragraph::new(text).block(
        Block::default()
            .title(" Help ")
            .borders(Borders::ALL)
            .border_style(Style::default().fg(Color::DarkGray)),
    );

    frame.render_widget(help, area);
}

fn render_logs(frame: &mut Frame, state: &AppState, area: Rect) {
    let mut lines: Vec<Line> = Vec::new();
    lines.push(Line::from(format!(
        "session: {}",
        state.active_session_id.as_deref().unwrap_or("none")
    )));
    lines.push(Line::from(format!("status: {}", state.session_status)).style(status_style(
        state.session_status.as_str(),
    )));
    if let Some(query) = &state.current_query {
        lines.push(Line::from(format!("query: {}", query)).fg(Color::Magenta));
    }

    lines.push(Line::from(""));
    lines.push(Line::from("== event stream ==").fg(Color::Gray));
    lines.extend(state.logs.iter().flat_map(render_log_lines));

    let inner_width = area.width.saturating_sub(2);
    let inner_height = area.height.saturating_sub(2);
    let total_visual_lines = count_visual_lines(&lines, inner_width);
    let max_scroll = total_visual_lines.saturating_sub(inner_height as usize);
    let scroll_y = max_scroll
        .saturating_sub(usize::from(state.session_view_offset_from_bottom))
        as u16;
    let title = if state.focus == Focus::SessionView {
        " Session View "
    } else {
        " Session View"
    };

    let logs = Paragraph::new(Text::from(lines))
        .block(
            Block::default()
                .title(title)
                .borders(Borders::ALL)
                .border_style(focus_style(state, Focus::SessionView)),
        )
        .scroll((scroll_y, 0))
        .wrap(Wrap { trim: false });

    frame.render_widget(logs, area);
}

fn render_input(frame: &mut Frame, state: &AppState, area: Rect) {
    let title = match state.selected_agent() {
        Some(agent)
            if state.focus == Focus::Input && !agent.input_permission.allows_direct_send() =>
        {
            " Command Input (restricted) "
        }
        Some(_) if state.focus == Focus::Input => " Command Input ",
        Some(agent) if !agent.input_permission.allows_direct_send() => {
            " Command Input (restricted)"
        }
        _ => " Command Input",
    };

    let input = Paragraph::new(state.input.as_str()).block(
        Block::default()
            .title(title)
            .borders(Borders::ALL)
            .border_style(focus_style(state, Focus::Input)),
    );

    frame.render_widget(input, area);

    if state.focus == Focus::Input {
        let cursor_x = area.x + state.input_cursor as u16 + 1;
        let cursor_y = area.y + 1;
        frame.set_cursor_position((cursor_x, cursor_y));
    }
}

fn focus_style(state: &AppState, focus: Focus) -> Style {
    if state.focus == focus {
        Style::default().fg(Color::Green)
    } else {
        Style::default().fg(Color::DarkGray)
    }
}

fn status_style(status: &str) -> Style {
    match status {
        "running" => Style::default().fg(Color::LightBlue),
        "researching" | "planning" | "queued" => Style::default().fg(Color::LightBlue),
        "awaiting_review" | "waiting_user" => Style::default().fg(Color::Yellow),
        "failed" | "error" => Style::default().fg(Color::Red),
        "offline" => Style::default().fg(Color::Red),
        _ => Style::default().fg(Color::Cyan),
    }
}

fn render_log_lines(entry: &LogEntry) -> Vec<Line<'static>> {
    let prefix = match entry.source {
        LogSource::System => "system",
        LogSource::User => "you",
        LogSource::Coordinator => "coordinator",
        LogSource::Planner => "planner",
        LogSource::Researcher => "researcher",
        LogSource::Reporter => "reporter",
        LogSource::Error => "error",
    };
    let indent = " ".repeat(prefix.len() + 3);
    let style = log_style(&entry.source);
    let mut rendered = Vec::new();
    let mut lines = entry.message.lines();

    if let Some(first_line) = lines.next() {
        rendered.push(Line::from(format!("[{}] {}", prefix, first_line)).style(style));
    } else {
        rendered.push(Line::from(format!("[{}]", prefix)).style(style));
    }

    for line in lines {
        rendered.push(Line::from(format!("{}{}", indent, line)).style(style));
    }

    rendered
}

fn log_style(source: &LogSource) -> Style {
    match source {
        LogSource::System => Style::default().fg(Color::Gray),
        LogSource::User => Style::default().fg(Color::White),
        LogSource::Coordinator => Style::default().fg(Color::Cyan),
        LogSource::Planner => Style::default().fg(Color::Yellow),
        LogSource::Researcher => Style::default().fg(Color::LightGreen),
        LogSource::Reporter => Style::default().fg(Color::LightMagenta),
        LogSource::Error => Style::default().fg(Color::Red),
    }
}

fn count_visual_lines(lines: &[Line<'_>], width: u16) -> usize {
    let width = usize::from(width.max(1));
    lines.iter()
        .map(|line| {
            let text = line.to_string();
            let len = text.chars().count();
            usize::max(1, len.div_ceil(width))
        })
        .sum()
}
