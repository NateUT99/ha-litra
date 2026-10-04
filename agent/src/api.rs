//! HTTPS + WebSocket API. Every route requires `Authorization: Bearer <token>`.

use std::sync::Arc;

use axum::extract::ws::{Message, WebSocket, WebSocketUpgrade};
use axum::extract::{DefaultBodyLimit, Path, Request, State};
use axum::http::{header, StatusCode};
use axum::middleware::{self, Next};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use serde_json::json;
use tokio::sync::watch;
use tracing::{debug, warn};

use crate::device::{DeviceWorker, SetError, SetRequest, Snapshot};
use crate::store::Store;

pub const API_VERSION: u32 = 1;

pub struct AppState {
    pub agent_id: String,
    pub name: String,
    pub store: Store,
    pub worker: DeviceWorker,
    pub snapshots: watch::Receiver<Snapshot>,
}

pub fn router(state: Arc<AppState>) -> Router {
    Router::new()
        .route("/api/v1/info", get(info))
        .route("/api/v1/devices", get(devices))
        .route("/api/v1/devices/{id}", post(set_device))
        .route("/api/v1/events", get(events))
        // Requests are tiny JSON objects; refuse anything larger outright.
        .layer(DefaultBodyLimit::max(1024))
        .layer(middleware::from_fn_with_state(state.clone(), require_token))
        .with_state(state)
}

async fn require_token(State(state): State<Arc<AppState>>, request: Request, next: Next) -> Response {
    let presented = request
        .headers()
        .get(header::AUTHORIZATION)
        .and_then(|value| value.to_str().ok())
        .and_then(|value| value.strip_prefix("Bearer "));
    match presented {
        Some(token) if state.store.verify_token(token) => next.run(request).await,
        _ => {
            warn!("rejected unauthenticated request to {}", request.uri().path());
            StatusCode::UNAUTHORIZED.into_response()
        }
    }
}

async fn info(State(state): State<Arc<AppState>>) -> impl IntoResponse {
    Json(json!({
        "agent_id": state.agent_id,
        "name": state.name,
        "version": env!("CARGO_PKG_VERSION"),
        "api_version": API_VERSION,
    }))
}

async fn devices(State(state): State<Arc<AppState>>) -> impl IntoResponse {
    let snapshot = state.snapshots.borrow().clone();
    Json(snapshot.as_ref().clone())
}

async fn set_device(
    State(state): State<Arc<AppState>>,
    Path(id): Path<String>,
    Json(request): Json<SetRequest>,
) -> Response {
    match state.worker.set(id, request).await {
        Ok(device) => Json(device).into_response(),
        Err(SetError::NotFound) => StatusCode::NOT_FOUND.into_response(),
        Err(SetError::Device(message)) => {
            (StatusCode::BAD_GATEWAY, Json(json!({ "error": message }))).into_response()
        }
        Err(SetError::WorkerGone) => StatusCode::SERVICE_UNAVAILABLE.into_response(),
    }
}

async fn events(State(state): State<Arc<AppState>>, upgrade: WebSocketUpgrade) -> Response {
    upgrade.on_upgrade(move |socket| stream_events(socket, state.snapshots.clone()))
}

/// Send the full device list on connect and again on every change. Snapshots
/// are a few hundred bytes, so full state beats deltas: a client that reconnects
/// or misses a frame can never drift.
async fn stream_events(mut socket: WebSocket, mut snapshots: watch::Receiver<Snapshot>) {
    debug!("event stream opened");
    loop {
        let frame = {
            let snapshot = snapshots.borrow_and_update().clone();
            json!({ "type": "devices", "devices": snapshot.as_ref() }).to_string()
        };
        if socket.send(Message::Text(frame.into())).await.is_err() {
            break;
        }
        tokio::select! {
            changed = snapshots.changed() => {
                if changed.is_err() {
                    break;
                }
            }
            incoming = socket.recv() => {
                // Clients don't send anything; any close or error ends the stream.
                match incoming {
                    Some(Ok(Message::Close(_))) | Some(Err(_)) | None => break,
                    _ => {}
                }
            }
        }
    }
    debug!("event stream closed");
}
