#[derive(Debug, Clone, Eq, PartialEq)]
pub enum LocalCommand {
    Quit,
    Accept(Option<String>),
    Reject(Option<String>),
    Refresh,
    Clear,
    Help,
    Crash(Option<String>),
}

#[derive(Debug, Clone, Eq, PartialEq)]
pub enum InputAction {
    Ignore,
    Local(LocalCommand),
    SendToAgent(String),
}

pub fn parse_input(input: &str) -> InputAction {
    let trimmed = input.trim();
    if trimmed.is_empty() {
        return InputAction::Ignore;
    }

    if trimmed == "/q" {
        return InputAction::Local(LocalCommand::Quit);
    }

    if let Some(rest) = trimmed
        .strip_prefix("/accept")
        .or_else(|| trimmed.strip_prefix("/approve"))
    {
        let feedback = rest.trim();
        return InputAction::Local(LocalCommand::Accept(optional_text(feedback)));
    }

    if trimmed == "/refresh" {
        return InputAction::Local(LocalCommand::Refresh);
    }

    if trimmed == "/clear" {
        return InputAction::Local(LocalCommand::Clear);
    }

    if trimmed == "/help" {
        return InputAction::Local(LocalCommand::Help);
    }

    if let Some(rest) = trimmed.strip_prefix("/reject") {
        let feedback = rest.trim();
        return InputAction::Local(LocalCommand::Reject(optional_text(feedback)));
    }

    if let Some(rest) = trimmed.strip_prefix("/crash") {
        let reason = rest.trim();
        return InputAction::Local(LocalCommand::Crash(optional_text(reason)));
    }

    InputAction::SendToAgent(trimmed.to_string())
}

fn optional_text(value: &str) -> Option<String> {
    let trimmed = value.trim();
    if trimmed.is_empty() {
        None
    } else {
        Some(trimmed.to_string())
    }
}
