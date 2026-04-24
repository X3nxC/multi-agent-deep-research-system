mod app;
mod backend;
mod command;
mod model;
mod ui;

use std::io::{self, Stdout};
use std::env;

use app::App;
use backend::http_sse::HttpSseBackend;
use backend::process::LocalBackendProcess;
use crossterm::{
    execute,
    terminal::{EnterAlternateScreen, LeaveAlternateScreen, disable_raw_mode, enable_raw_mode},
};
use ratatui::{Terminal, backend::CrosstermBackend};

type AppResult<T> = Result<T, Box<dyn std::error::Error>>;

fn main() -> AppResult<()> {
    let base_url = env::var("COORDINATOR_BASE_URL").unwrap_or_else(|_| "http://127.0.0.1:8000".into());
    let session_id = env::var("FIXED_TEST_SESSION_ID").unwrap_or_else(|_| "test-session".into());
    let _managed_backend = LocalBackendProcess::spawn(&base_url)
        .map_err(|error| -> Box<dyn std::error::Error> { error.into() })?;
    let backend = HttpSseBackend::spawn(base_url, session_id);
    let mut terminal = setup_terminal()?;
    let result = App::new(backend).run(&mut terminal);
    restore_terminal(&mut terminal)?;
    result
}

fn setup_terminal() -> AppResult<Terminal<CrosstermBackend<Stdout>>> {
    enable_raw_mode()?;
    let mut stdout = io::stdout();
    execute!(stdout, EnterAlternateScreen)?;
    let backend = CrosstermBackend::new(stdout);
    Ok(Terminal::new(backend)?)
}

fn restore_terminal(terminal: &mut Terminal<CrosstermBackend<Stdout>>) -> AppResult<()> {
    disable_raw_mode()?;
    execute!(terminal.backend_mut(), LeaveAlternateScreen)?;
    terminal.show_cursor()?;
    Ok(())
}
