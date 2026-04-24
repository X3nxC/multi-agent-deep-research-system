use std::{
    env,
    fs::{self, OpenOptions},
    path::PathBuf,
    process::{Child, Command, Stdio},
    thread,
    time::{Duration, Instant},
};

use reqwest::blocking::Client;

pub struct LocalBackendProcess {
    child: Option<Child>,
}

impl LocalBackendProcess {
    pub fn spawn(base_url: &str) -> Result<Self, String> {
        if env::var("MANAGE_LOCAL_BACKEND")
            .map(|value| value == "0")
            .unwrap_or(false)
        {
            return Ok(Self { child: None });
        }

        let python_path = PathBuf::from(".venv/bin/python");
        if !python_path.exists() {
            return Err("missing .venv/bin/python for managed backend startup".into());
        }

        let (host, port) = parse_host_port(base_url)?;
        fs::create_dir_all("data").map_err(|error| format!("failed to create data directory: {error}"))?;
        let log_path = PathBuf::from("data/backend-host.log");
        let stdout = OpenOptions::new()
            .create(true)
            .append(true)
            .open(&log_path)
            .map_err(|error| format!("failed to open backend log file: {error}"))?;
        let stderr = stdout
            .try_clone()
            .map_err(|error| format!("failed to clone backend log file handle: {error}"))?;

        let mut child = Command::new(&python_path)
            .arg("-m")
            .arg("backend.server")
            .env("PYTHONUNBUFFERED", "1")
            .env("APP_HOST", &host)
            .env("APP_PORT", port.to_string())
            .env("PUBLIC_BASE_URL", base_url)
            .stdout(Stdio::from(stdout))
            .stderr(Stdio::from(stderr))
            .spawn()
            .map_err(|error| format!("failed to spawn managed backend: {error}"))?;

        wait_for_health(base_url, &mut child, &log_path)?;
        Ok(Self { child: Some(child) })
    }
}

impl Drop for LocalBackendProcess {
    fn drop(&mut self) {
        let Some(child) = self.child.as_mut() else {
            return;
        };

        if child.try_wait().ok().flatten().is_some() {
            return;
        }

        let _ = child.kill();
        let _ = child.wait();
    }
}

fn wait_for_health(base_url: &str, child: &mut Child, log_path: &PathBuf) -> Result<(), String> {
    let health_url = format!("{}/health", base_url.trim_end_matches('/'));
    let client = Client::builder()
        .timeout(Duration::from_millis(400))
        .build()
        .map_err(|error| format!("failed to build backend health client: {error}"))?;
    let started = Instant::now();

    while started.elapsed() < Duration::from_secs(10) {
        if let Some(status) = child
            .try_wait()
            .map_err(|error| format!("failed to inspect backend process: {error}"))?
        {
            return Err(format!(
                "managed backend exited early with status {status}; see {}",
                log_path.display()
            ));
        }

        if let Ok(response) = client.get(&health_url).send() {
            if response.status().is_success() {
                return Ok(());
            }
        }

        thread::sleep(Duration::from_millis(200));
    }

    Err(format!(
        "managed backend did not become healthy within 10s; see {}",
        log_path.display()
    ))
}

fn parse_host_port(base_url: &str) -> Result<(String, u16), String> {
    let trimmed = base_url
        .trim()
        .strip_prefix("http://")
        .or_else(|| base_url.trim().strip_prefix("https://"))
        .ok_or_else(|| format!("unsupported coordinator base url: {base_url}"))?;
    let host_port = trimmed
        .split('/')
        .next()
        .ok_or_else(|| format!("invalid coordinator base url: {base_url}"))?;
    let mut parts = host_port.split(':');
    let host = parts
        .next()
        .filter(|value| !value.is_empty())
        .ok_or_else(|| format!("invalid coordinator host in {base_url}"))?;
    let port = parts
        .next()
        .ok_or_else(|| format!("missing coordinator port in {base_url}"))?
        .parse::<u16>()
        .map_err(|error| format!("invalid coordinator port in {base_url}: {error}"))?;
    Ok((host.to_string(), port))
}
