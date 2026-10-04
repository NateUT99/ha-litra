//! litra-agent: exposes USB-attached Logitech Litra lights to Home Assistant.
//!
//! Runs as a LaunchAgent in the logged-in user's session, which is what grants
//! USB HID access — no sudo, no service account.

mod api;
mod device;
mod store;

use std::net::SocketAddr;
use std::sync::Arc;
use std::time::Duration;

use axum_server::tls_rustls::RustlsConfig;
use clap::{Parser, Subcommand};
use mdns_sd::{ServiceDaemon, ServiceInfo};
use tracing::{error, info, warn};
use tracing_subscriber::EnvFilter;

use crate::api::{AppState, API_VERSION};
use crate::store::Store;

const SERVICE_TYPE: &str = "_litra-agent._tcp.local.";

#[derive(Parser)]
#[command(version, about)]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    /// Run the API server (what the LaunchAgent runs).
    Serve {
        /// Address to listen on.
        #[arg(long, default_value = "0.0.0.0:47810")]
        listen: SocketAddr,
        /// How often to poll attached lights for out-of-band changes, in milliseconds.
        #[arg(long, default_value_t = 1000)]
        poll_ms: u64,
        /// Don't advertise the agent over mDNS.
        #[arg(long)]
        no_zeroconf: bool,
    },
    /// Issue a new API token (revoking the old one) and print the pairing details.
    Pair,
    /// Print the TLS certificate fingerprint.
    Fingerprint,
}

#[tokio::main]
async fn main() -> std::process::ExitCode {
    tracing_subscriber::fmt()
        .with_env_filter(EnvFilter::try_from_default_env().unwrap_or_else(|_| "info".into()))
        .with_target(false)
        .init();

    match run(Cli::parse().command).await {
        Ok(()) => std::process::ExitCode::SUCCESS,
        Err(err) => {
            error!("{err}");
            std::process::ExitCode::FAILURE
        }
    }
}

async fn run(command: Command) -> std::io::Result<()> {
    let store = Store::open()?;
    let name = machine_name();
    store.ensure_certificate(&name)?;

    match command {
        Command::Pair => {
            let token = store.rotate_token()?;
            println!("Token:       {token}");
            println!("Fingerprint: {}", store.certificate_fingerprint()?);
            println!();
            println!("Enter the token in Home Assistant and confirm the fingerprint matches.");
            println!("Already-open event streams keep running until the agent restarts:");
            println!("  launchctl kickstart -k gui/$(id -u)/com.github.nateut99.litra-agent");
            Ok(())
        }
        Command::Fingerprint => {
            println!("{}", store.certificate_fingerprint()?);
            Ok(())
        }
        Command::Serve { listen, poll_ms, no_zeroconf } => {
            serve(store, name, listen, Duration::from_millis(poll_ms), !no_zeroconf).await
        }
    }
}

async fn serve(
    store: Store,
    name: String,
    listen: SocketAddr,
    poll_interval: Duration,
    zeroconf: bool,
) -> std::io::Result<()> {
    if !store.has_token() {
        warn!("no API token issued yet; run `litra-agent pair` — all requests will be refused");
    }
    let agent_id = store.agent_id()?;
    let tls = RustlsConfig::from_pem_file(store.cert_path(), store.key_path()).await?;
    info!("certificate fingerprint {}", store.certificate_fingerprint()?);

    let (worker, snapshots) = device::spawn(poll_interval);
    spawn_watchdog(worker.clone());

    // Held for the life of the process; dropping it withdraws the advertisement.
    let _mdns = if zeroconf {
        advertise(&agent_id, &name, listen.port())
            .inspect_err(|err| warn!("mDNS advertisement failed: {err}"))
            .ok()
    } else {
        None
    };

    let state = Arc::new(AppState { agent_id, name, store, worker, snapshots });
    info!("listening on https://{listen}");
    axum_server::bind_rustls(listen, tls)
        .serve(api::router(state).into_make_service())
        .await
}

/// The `litra` crate's HID reads block without a timeout. If the worker stops
/// cycling, exit and let launchd's KeepAlive start a fresh process rather than
/// serve stale state forever.
fn spawn_watchdog(worker: device::DeviceWorker) {
    tokio::spawn(async move {
        let mut interval = tokio::time::interval(Duration::from_secs(5));
        loop {
            interval.tick().await;
            let stalled = worker.seconds_since_heartbeat();
            if stalled > 30 {
                error!("HID worker unresponsive for {stalled}s; exiting for launchd to restart");
                std::process::exit(1);
            }
        }
    });
}

fn advertise(agent_id: &str, name: &str, port: u16) -> Result<ServiceDaemon, mdns_sd::Error> {
    let daemon = ServiceDaemon::new()?;
    let host = format!("{}.local.", name.replace(' ', "-"));
    let api_version = API_VERSION.to_string();
    let properties = [("id", agent_id), ("api", api_version.as_str())];
    let service = ServiceInfo::new(
        SERVICE_TYPE,
        &format!("Litra Agent on {name}"),
        &host,
        "",
        port,
        &properties[..],
    )?
    .enable_addr_auto();
    daemon.register(service)?;
    Ok(daemon)
}

fn machine_name() -> String {
    hostname::get()
        .ok()
        .and_then(|name| name.into_string().ok())
        .and_then(|name| name.split('.').next().map(str::to_owned))
        .unwrap_or_else(|| "litra-agent".to_owned())
}
