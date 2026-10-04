//! Device worker: the only code that touches USB HID.
//!
//! All HID access runs on one dedicated OS thread so this process never
//! interleaves its own requests. The thread polls every attached Litra on a
//! fixed interval and publishes a snapshot whenever anything changes, which is
//! how physical button presses and USB hotplug reach Home Assistant.

use std::collections::BTreeMap;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::mpsc::{self, RecvTimeoutError};
use std::sync::Arc;
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use litra::{DeviceHandle, DeviceResult, DeviceType, Litra};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use tokio::sync::{oneshot, watch};
use tracing::{debug, error, info, warn};

/// How long to wait for the response to one query.
const RESPONSE_TIMEOUT: Duration = Duration::from_millis(500);
/// Consecutive failed polls before a device is closed and reopened.
const MAX_READ_FAILURES: u32 = 3;

// HID++ query functions (function number << 4 | software ID 1), as used by litra-rs.
const FN_GET_ON: u8 = 0x01;
const FN_GET_BRIGHTNESS: u8 = 0x31;
const FN_GET_TEMPERATURE: u8 = 0x81;

/// Current state of one attached light, as served by the API.
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct DeviceState {
    pub id: String,
    pub serial: Option<String>,
    pub model: String,
    pub is_on: bool,
    pub brightness_lumen: u16,
    pub temperature_kelvin: u16,
    pub min_brightness_lumen: u16,
    pub max_brightness_lumen: u16,
    pub min_temperature_kelvin: u16,
    pub max_temperature_kelvin: u16,
}

/// All attached lights, ordered by id.
pub type Snapshot = Arc<Vec<DeviceState>>;

/// A state change request. Every field is optional; unknown fields are rejected.
#[derive(Debug, Default, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SetRequest {
    pub on: Option<bool>,
    pub brightness_lumen: Option<u16>,
    pub temperature_kelvin: Option<u16>,
}

#[derive(Debug)]
pub enum SetError {
    NotFound,
    Device(String),
    WorkerGone,
}

enum Command {
    Set {
        id: String,
        request: SetRequest,
        reply: oneshot::Sender<Result<DeviceState, SetError>>,
    },
}

/// Handle used by the async side to talk to the worker thread.
#[derive(Clone)]
pub struct DeviceWorker {
    commands: mpsc::Sender<Command>,
    heartbeat: Arc<AtomicU64>,
}

impl DeviceWorker {
    /// Apply a state change and return the device's state read back afterwards.
    pub async fn set(&self, id: String, request: SetRequest) -> Result<DeviceState, SetError> {
        let (reply, response) = oneshot::channel();
        self.commands
            .send(Command::Set { id, request, reply })
            .map_err(|_| SetError::WorkerGone)?;
        response.await.map_err(|_| SetError::WorkerGone)?
    }

    /// Seconds since the worker last completed a poll cycle.
    pub fn seconds_since_heartbeat(&self) -> u64 {
        now_secs().saturating_sub(self.heartbeat.load(Ordering::Relaxed))
    }
}

struct OpenDevice {
    id: String,
    serial: Option<String>,
    model: String,
    handle: DeviceHandle,
    /// Last good reading, reported while transient read failures are retried.
    last: Option<DeviceState>,
    failures: u32,
}

/// Start the worker thread. Returns the command handle and a snapshot receiver.
pub fn spawn(poll_interval: Duration) -> (DeviceWorker, watch::Receiver<Snapshot>) {
    let (commands, rx) = mpsc::channel();
    let (snapshots, snapshot_rx) = watch::channel(Snapshot::default());
    let heartbeat = Arc::new(AtomicU64::new(now_secs()));
    let beat = heartbeat.clone();

    thread::Builder::new()
        .name("litra-hid".into())
        .spawn(move || run(rx, snapshots, beat, poll_interval))
        .expect("failed to spawn HID worker thread");

    (DeviceWorker { commands, heartbeat }, snapshot_rx)
}

fn run(
    commands: mpsc::Receiver<Command>,
    snapshots: watch::Sender<Snapshot>,
    heartbeat: Arc<AtomicU64>,
    poll_interval: Duration,
) {
    let mut context = loop {
        match Litra::new() {
            Ok(context) => break context,
            Err(err) => {
                error!("failed to initialise HID: {err}; retrying in 5s");
                thread::sleep(Duration::from_secs(5));
            }
        }
    };
    // Keyed by HID device path, which is what enumeration reports.
    let mut devices: BTreeMap<String, OpenDevice> = BTreeMap::new();

    loop {
        refresh_devices(&mut context, &mut devices);
        publish(&snapshots, read_all(&mut devices));
        heartbeat.store(now_secs(), Ordering::Relaxed);

        match commands.recv_timeout(poll_interval) {
            Ok(Command::Set { id, request, reply }) => {
                let result = apply(&devices, &id, &request);
                // Re-read everything so the reply and the broadcast agree.
                let snapshot = read_all(&mut devices);
                let result = result.and_then(|()| {
                    snapshot
                        .iter()
                        .find(|state| state.id == id)
                        .cloned()
                        .ok_or(SetError::NotFound)
                });
                publish(&snapshots, snapshot);
                let _ = reply.send(result);
            }
            Err(RecvTimeoutError::Timeout) => {}
            Err(RecvTimeoutError::Disconnected) => return,
        }
    }
}

fn refresh_devices(context: &mut Litra, devices: &mut BTreeMap<String, OpenDevice>) {
    if let Err(err) = context.refresh_connected_devices() {
        warn!("HID enumeration failed: {err}");
        return;
    }
    let present: Vec<String> = context
        .get_connected_devices()
        .map(|device| device.device_path())
        .collect();
    devices.retain(|path, device| {
        let keep = present.contains(path);
        if !keep {
            info!("{} ({}) disconnected", device.model, device.id);
        }
        keep
    });

    for device in context.get_connected_devices() {
        let path = device.device_path();
        if devices.contains_key(&path) {
            continue;
        }
        match device.open(context) {
            Ok(handle) => {
                let serial = handle.serial_number().ok().flatten();
                let id = serial.clone().unwrap_or_else(|| {
                    format!("path-{}", &hex::encode(Sha256::digest(&path))[..12])
                });
                let model = device.device_type().to_string();
                info!("{model} ({id}) connected");
                devices.insert(
                    path,
                    OpenDevice { id, serial, model, handle, last: None, failures: 0 },
                );
            }
            Err(err) => warn!("failed to open {path}: {err}"),
        }
    }
}

/// Read every open device. A failed read reports the last good state; repeated
/// failures close the handle so the next enumeration reopens it.
fn read_all(devices: &mut BTreeMap<String, OpenDevice>) -> Vec<DeviceState> {
    let mut states = Vec::with_capacity(devices.len());
    devices.retain(|_, device| match read_state(device) {
        Ok(state) => {
            device.failures = 0;
            device.last = Some(state.clone());
            states.push(state);
            true
        }
        Err(err) => {
            device.failures += 1;
            warn!(
                "read from {} ({}) failed ({}/{MAX_READ_FAILURES}): {err}",
                device.model, device.id, device.failures
            );
            if device.failures >= MAX_READ_FAILURES {
                return false;
            }
            states.extend(device.last.clone());
            true
        }
    });
    states.sort_by(|a, b| a.id.cmp(&b.id));
    states
}

fn read_state(device: &OpenDevice) -> Result<DeviceState, String> {
    let handle = &device.handle;
    let is_on = query(handle, FN_GET_ON)?[4] == 1;
    let brightness = query(handle, FN_GET_BRIGHTNESS)?;
    let temperature = query(handle, FN_GET_TEMPERATURE)?;
    let state = DeviceState {
        id: device.id.clone(),
        serial: device.serial.clone(),
        model: device.model.clone(),
        is_on,
        brightness_lumen: u16::from_be_bytes([brightness[4], brightness[5]]),
        temperature_kelvin: u16::from_be_bytes([temperature[4], temperature[5]]),
        min_brightness_lumen: handle.minimum_brightness_in_lumen(),
        max_brightness_lumen: handle.maximum_brightness_in_lumen(),
        min_temperature_kelvin: handle.minimum_temperature_in_kelvin(),
        max_temperature_kelvin: handle.maximum_temperature_in_kelvin(),
    };
    let brightness_ok =
        (state.min_brightness_lumen..=state.max_brightness_lumen).contains(&state.brightness_lumen);
    let temperature_ok = (state.min_temperature_kelvin..=state.max_temperature_kelvin)
        .contains(&state.temperature_kelvin);
    if !brightness_ok || !temperature_ok {
        return Err(format!(
            "implausible reading {} lm / {} K",
            state.brightness_lumen, state.temperature_kelvin
        ));
    }
    Ok(state)
}

/// Send one HID++ query and return its response.
///
/// The `litra` crate's getters read the next input report, whatever it is. That
/// breaks when another process (e.g. the `litra` CLI) talks to the same light:
/// on macOS every open handle receives every report, so a getter can take
/// someone else's answer and report brightness as temperature. Here, stale
/// reports are drained first and only a response echoing this request's header
/// is accepted, with a timeout instead of a read that can block forever.
fn query(handle: &DeviceHandle, function: u8) -> Result<[u8; 20], String> {
    let feature = match handle.device_type() {
        DeviceType::LitraBeamLX => 0x06,
        DeviceType::LitraGlow | DeviceType::LitraBeam => 0x04,
    };
    let mut request = [0u8; 20];
    request[..4].copy_from_slice(&[0x11, 0xff, feature, function]);

    let hid = handle.hid_device();
    let mut buffer = [0u8; 20];
    let result = (|| -> DeviceResult<Option<[u8; 20]>> {
        while hid.read_timeout(&mut buffer, 0)? > 0 {}
        hid.write(&request)?;
        let deadline = Instant::now() + RESPONSE_TIMEOUT;
        loop {
            let remaining = deadline.saturating_duration_since(Instant::now());
            if remaining.is_zero() {
                return Ok(None);
            }
            let millis = i32::try_from(remaining.as_millis()).unwrap_or(i32::MAX).max(1);
            let read = hid.read_timeout(&mut buffer, millis)?;
            if read >= 6 && buffer[..4] == request[..4] {
                return Ok(Some(buffer));
            }
        }
    })();
    match result {
        Ok(Some(response)) => Ok(response),
        Ok(None) => Err(format!("no response to query {function:#04x}")),
        Err(err) => Err(err.to_string()),
    }
}

fn apply(
    devices: &BTreeMap<String, OpenDevice>,
    id: &str,
    request: &SetRequest,
) -> Result<(), SetError> {
    let device = devices
        .values()
        .find(|device| device.id == id)
        .ok_or(SetError::NotFound)?;
    let handle = &device.handle;
    debug!("set {id}: {request:?}");

    let result = (|| -> DeviceResult<()> {
        // Power on first: brightness/temperature sent to an off Litra are stored
        // but not shown, so on → adjust → off is the only order that always lands.
        if request.on == Some(true) {
            handle.set_on(true)?;
        }
        if let Some(lumen) = request.brightness_lumen {
            let lumen = lumen.clamp(
                handle.minimum_brightness_in_lumen(),
                handle.maximum_brightness_in_lumen(),
            );
            handle.set_brightness_in_lumen(lumen)?;
        }
        if let Some(kelvin) = request.temperature_kelvin {
            // The device only accepts multiples of 100; the bounds already are.
            let kelvin = (((u32::from(kelvin) + 50) / 100) * 100) as u16;
            let kelvin = kelvin.clamp(
                handle.minimum_temperature_in_kelvin(),
                handle.maximum_temperature_in_kelvin(),
            );
            handle.set_temperature_in_kelvin(kelvin)?;
        }
        if request.on == Some(false) {
            handle.set_on(false)?;
        }
        Ok(())
    })();

    result.map_err(|err| SetError::Device(err.to_string()))
}

fn publish(snapshots: &watch::Sender<Snapshot>, states: Vec<DeviceState>) {
    snapshots.send_if_modified(|current| {
        if **current == states {
            false
        } else {
            *current = Arc::new(states);
            true
        }
    });
}

fn now_secs() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or_default()
}
