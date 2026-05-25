#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::Duration;
#[cfg(target_os = "windows")]
use std::os::windows::process::CommandExt;

fn chrome_candidates() -> Vec<PathBuf> {
    #[cfg(target_os = "windows")]
    {
        let mut paths = Vec::new();
        if let Ok(program_files) = std::env::var("PROGRAMFILES") {
            paths.push(PathBuf::from(program_files).join("Google\\Chrome\\Application\\chrome.exe"));
        }
        if let Ok(program_files_x86) = std::env::var("PROGRAMFILES(X86)") {
            paths.push(PathBuf::from(program_files_x86).join("Google\\Chrome\\Application\\chrome.exe"));
        }
        if let Ok(local) = std::env::var("LOCALAPPDATA") {
            paths.push(PathBuf::from(local).join("Google\\Chrome\\Application\\chrome.exe"));
        }
        paths
    }

    #[cfg(target_os = "macos")]
    {
        vec![
            PathBuf::from("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            PathBuf::from("/Applications/Chromium.app/Contents/MacOS/Chromium"),
        ]
    }

    #[cfg(all(unix, not(target_os = "macos")))]
    {
        vec![
            PathBuf::from("/usr/bin/google-chrome"),
            PathBuf::from("/usr/bin/google-chrome-stable"),
            PathBuf::from("/usr/bin/chromium"),
            PathBuf::from("/usr/bin/chromium-browser"),
            PathBuf::from("/snap/bin/chromium"),
        ]
    }
}

#[tauri::command]
fn start_chrome() -> Result<String, String> {
    let chrome = chrome_candidates()
        .into_iter()
        .find(|path| path.exists())
        .ok_or_else(|| "Could not find Google Chrome or Chromium in the usual places.".to_string())?;

    let profile_dir = dirs_like_profile_dir()?;

    Command::new(&chrome)
        .arg("--remote-debugging-port=9222")
        .arg(format!("--user-data-dir={}", profile_dir.display()))
        .arg("--no-first-run")
        .arg("--new-window")
        .arg("https://r.inna.is/adgangur")
        .spawn()
        .map_err(|err| format!("Could not start Chrome: {err}"))?;

    Ok(format!("Started Chrome: {}", chrome.display()))
}

fn dirs_like_profile_dir() -> Result<PathBuf, String> {
    let home = std::env::var("USERPROFILE")
        .or_else(|_| std::env::var("HOME"))
        .map_err(|_| "Could not determine home directory.".to_string())?;
    Ok(PathBuf::from(home).join(".inna_archive_chrome_profile"))
}

#[tauri::command]
fn open_path(path: String) -> Result<(), String> {
    open::that(path).map_err(|err| format!("Could not open path: {err}"))
}

#[tauri::command]
fn read_text_file(path: String) -> Result<String, String> {
    let bytes = std::fs::read(&path).map_err(|err| format!("Could not read file {path}: {err}"))?;
    Ok(String::from_utf8_lossy(&bytes).to_string())
}

#[derive(Default)]
struct DownloaderState {
    child: Mutex<Option<Child>>,
}

fn sidecar_candidates() -> Vec<PathBuf> {
    let mut out = Vec::new();

    let triple = option_env!("TAURI_ENV_TARGET_TRIPLE")
        .unwrap_or("x86_64-pc-windows-msvc");

    #[cfg(target_os = "windows")]
    let names = vec![
        format!("inna_downloader_cli-{triple}.exe"),
        "inna_downloader_cli.exe".to_string(),
    ];

    #[cfg(not(target_os = "windows"))]
    let names = vec![
        format!("inna_downloader_cli-{triple}"),
        "inna_downloader_cli".to_string(),
    ];

    let mut bases = Vec::new();

    if let Ok(exe) = std::env::current_exe() {
        if let Some(parent) = exe.parent() {
            bases.push(parent.to_path_buf());
            bases.push(parent.join("binaries"));
            bases.push(parent.join("resources"));
            bases.push(parent.join("resources").join("binaries"));
        }
    }

    if let Ok(cwd) = std::env::current_dir() {
        bases.push(cwd.clone());
        bases.push(cwd.join("binaries"));
        bases.push(cwd.join("src-tauri").join("binaries"));
        bases.push(cwd.join("dist"));
    }

    for base in bases {
        for name in &names {
            out.push(base.join(name));
        }
    }

    out
}

fn find_downloader_sidecar() -> Result<PathBuf, String> {
    for candidate in sidecar_candidates() {
        if candidate.exists() {
            return Ok(candidate);
        }
    }

    let searched = sidecar_candidates()
        .into_iter()
        .map(|p| p.display().to_string())
        .collect::<Vec<_>>()
        .join("\n  ");

    Err(format!(
        "Could not find bundled downloader sidecar. Searched:\n  {searched}"
    ))
}

#[tauri::command]
async fn run_downloader(
    args: Vec<String>,
    state: tauri::State<'_, DownloaderState>,
) -> Result<i32, String> {
    let sidecar = find_downloader_sidecar()?;

    {
        let guard = state
            .child
            .lock()
            .map_err(|_| "Downloader state lock was poisoned".to_string())?;
        if guard.is_some() {
            return Err("Another downloader sidecar is already running".to_string());
        }
    }

    let mut cmd = Command::new(&sidecar);
    cmd.args(args)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());

    // Avoid scary blank console windows and avoid UTF-8 decoding of process output.
    #[cfg(target_os = "windows")]
    {
        const CREATE_NO_WINDOW: u32 = 0x08000000;
        cmd.creation_flags(CREATE_NO_WINDOW);
    }

    let child = cmd
        .spawn()
        .map_err(|err| format!("Failed to run downloader sidecar {}: {err}", sidecar.display()))?;

    {
        let mut guard = state
            .child
            .lock()
            .map_err(|_| "Downloader state lock was poisoned".to_string())?;
        *guard = Some(child);
    }

    loop {
        {
            let mut guard = state
                .child
                .lock()
                .map_err(|_| "Downloader state lock was poisoned".to_string())?;

            match guard.as_mut() {
                Some(child) => match child.try_wait() {
                    Ok(Some(status)) => {
                        *guard = None;
                        return Ok(status.code().unwrap_or(-1));
                    }
                    Ok(None) => {
                        // Still running.
                    }
                    Err(err) => {
                        *guard = None;
                        return Err(format!("Failed while waiting for downloader sidecar: {err}"));
                    }
                },
                None => {
                    // stop_downloader removed/killed the child.
                    return Ok(-2);
                }
            }
        }

        std::thread::sleep(Duration::from_millis(250));
    }
}

#[tauri::command]
fn stop_downloader(state: tauri::State<'_, DownloaderState>) -> Result<bool, String> {
    let mut guard = state
        .child
        .lock()
        .map_err(|_| "Downloader state lock was poisoned".to_string())?;

    if let Some(mut child) = guard.take() {
        let _ = child.kill();
        let _ = child.wait();
        return Ok(true);
    }

    Ok(false)
}


fn main() {
    tauri::Builder::default()
        .manage(DownloaderState::default())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_fs::init())
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_shell::init())
        .invoke_handler(tauri::generate_handler![start_chrome, open_path, read_text_file, run_downloader, stop_downloader])
        .run(tauri::generate_context!())
        .expect("error while running INNAGUNN");
}
