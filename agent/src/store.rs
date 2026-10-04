//! On-disk agent state: identity, TLS material, and the hashed API token.
//!
//! Everything lives in `~/Library/Application Support/litra-agent/` (mode 0700),
//! owned by the console user the agent runs as. Secrets are written 0600.

use std::fs::{self, OpenOptions};
use std::io::Write;
use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};

use rustls_pki_types::pem::PemObject;
use rustls_pki_types::CertificateDer;
use sha2::{Digest, Sha256};
use subtle::ConstantTimeEq;

pub struct Store {
    dir: PathBuf,
}

impl Store {
    pub fn open() -> std::io::Result<Self> {
        let home = std::env::var_os("HOME")
            .ok_or_else(|| std::io::Error::other("HOME is not set"))?;
        let dir = PathBuf::from(home).join("Library/Application Support/litra-agent");
        fs::create_dir_all(&dir)?;
        fs::set_permissions(&dir, fs::Permissions::from_mode(0o700))?;
        Ok(Self { dir })
    }

    pub fn cert_path(&self) -> PathBuf {
        self.dir.join("cert.pem")
    }

    pub fn key_path(&self) -> PathBuf {
        self.dir.join("key.pem")
    }

    fn token_path(&self) -> PathBuf {
        self.dir.join("token.sha256")
    }

    /// Stable agent identity, used as the Home Assistant config entry unique ID.
    pub fn agent_id(&self) -> std::io::Result<String> {
        let path = self.dir.join("agent_id");
        if let Ok(id) = fs::read_to_string(&path) {
            return Ok(id.trim().to_owned());
        }
        let id = uuid::Uuid::new_v4().simple().to_string();
        write_private(&path, id.as_bytes())?;
        Ok(id)
    }

    /// Generate a self-signed certificate on first run.
    pub fn ensure_certificate(&self, hostname: &str) -> std::io::Result<()> {
        if self.cert_path().exists() && self.key_path().exists() {
            return Ok(());
        }
        let names = vec![hostname.to_owned(), format!("{hostname}.local")];
        let generated = rcgen::generate_simple_self_signed(names)
            .map_err(|err| std::io::Error::other(err.to_string()))?;
        write_private(&self.key_path(), generated.signing_key.serialize_pem().as_bytes())?;
        write_private(&self.cert_path(), generated.cert.pem().as_bytes())?;
        Ok(())
    }

    /// SHA-256 of the certificate's DER encoding, colon-separated uppercase hex —
    /// the value Home Assistant pins at pairing.
    pub fn certificate_fingerprint(&self) -> std::io::Result<String> {
        let cert = CertificateDer::from_pem_file(self.cert_path())
            .map_err(|err| std::io::Error::other(err.to_string()))?;
        Ok(format_fingerprint(&Sha256::digest(cert.as_ref())))
    }

    /// Create a new random token, store only its hash, and return the plaintext.
    /// Any previously issued token stops working immediately.
    pub fn rotate_token(&self) -> std::io::Result<String> {
        let token = hex::encode(rand::random::<[u8; 32]>());
        write_private(&self.token_path(), hex::encode(Sha256::digest(&token)).as_bytes())?;
        Ok(token)
    }

    /// Constant-time check of a presented token against the stored hash. Read on
    /// every call so `litra-agent pair` takes effect without a restart.
    pub fn verify_token(&self, presented: &str) -> bool {
        let Ok(stored) = fs::read_to_string(self.token_path()) else {
            return false;
        };
        let Ok(stored) = hex::decode(stored.trim()) else {
            return false;
        };
        let presented = Sha256::digest(presented.as_bytes());
        stored.len() == presented.len() && bool::from(stored.as_slice().ct_eq(presented.as_slice()))
    }

    pub fn has_token(&self) -> bool {
        self.token_path().exists()
    }
}

fn write_private(path: &Path, contents: &[u8]) -> std::io::Result<()> {
    let tmp = path.with_extension("tmp");
    let mut file = OpenOptions::new()
        .write(true)
        .create(true)
        .truncate(true)
        .mode(0o600)
        .open(&tmp)?;
    file.write_all(contents)?;
    file.sync_all()?;
    fs::rename(tmp, path)
}

fn format_fingerprint(digest: &[u8]) -> String {
    digest
        .iter()
        .map(|byte| format!("{byte:02X}"))
        .collect::<Vec<_>>()
        .join(":")
}
