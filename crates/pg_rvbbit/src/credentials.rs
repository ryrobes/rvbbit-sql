//! Canonical credential-envelope primitives.
//!
//! Ciphertext lives in Postgres, while key material remains in the postmaster
//! environment. SQL owns authorization, scoping, metadata, and audit receipts;
//! these helpers only seal and open one value bound to its immutable reference.

use pgrx::prelude::*;
use ring::{aead, rand as ring_rand};
use ring_rand::SecureRandom;
use sha2::{Digest, Sha256};

const MAGIC: &[u8; 4] = b"RVC1";
const NONCE_LEN: usize = 12;
const TAG_LEN: usize = 16;
const KEY_DOMAIN: &[u8] = b"rvbbit:credentials:v1\0";

fn configured_key_materials() -> Vec<String> {
    for (name, many) in [
        ("RVBBIT_CREDENTIAL_KEYS_FILE", true),
        ("RVBBIT_CREDENTIAL_KEY_FILE", false),
    ] {
        if let Ok(path) = std::env::var(name) {
            if let Ok(contents) = std::fs::read_to_string(path.trim()) {
                let values: Vec<String> = if many {
                    contents
                        .split([',', '\n'])
                        .map(str::trim)
                        .filter(|value| !value.is_empty())
                        .map(str::to_owned)
                        .collect()
                } else {
                    contents
                        .lines()
                        .map(str::trim)
                        .find(|value| !value.is_empty())
                        .map(str::to_owned)
                        .into_iter()
                        .collect()
                };
                if !values.is_empty() {
                    return values;
                }
            }
        }
    }
    if let Ok(keys) = std::env::var("RVBBIT_CREDENTIAL_KEYS") {
        let values: Vec<String> = keys
            .split(',')
            .map(str::trim)
            .filter(|value| !value.is_empty())
            .map(str::to_owned)
            .collect();
        if !values.is_empty() {
            return values;
        }
    }
    if let Ok(value) = std::env::var("RVBBIT_CREDENTIAL_KEY") {
        let value = value.trim();
        if !value.is_empty() {
            return vec![value.to_owned()];
        }
    }
    Vec::new()
}

fn derive_key(material: &str) -> [u8; 32] {
    let mut digest = Sha256::new();
    digest.update(KEY_DOMAIN);
    digest.update(material.as_bytes());
    digest.finalize().into()
}

fn seal_with_material(
    credential_ref: &str,
    plaintext: &[u8],
    material: &str,
) -> Result<Vec<u8>, String> {
    let mut nonce_bytes = [0_u8; NONCE_LEN];
    ring_rand::SystemRandom::new()
        .fill(&mut nonce_bytes)
        .map_err(|_| "credential nonce generation failed".to_string())?;
    let unbound = aead::UnboundKey::new(&aead::AES_256_GCM, &derive_key(material))
        .map_err(|_| "credential encryption setup failed".to_string())?;
    let key = aead::LessSafeKey::new(unbound);
    let mut encrypted = plaintext.to_vec();
    key.seal_in_place_append_tag(
        aead::Nonce::assume_unique_for_key(nonce_bytes),
        aead::Aad::from(credential_ref.as_bytes()),
        &mut encrypted,
    )
    .map_err(|_| "credential encryption failed".to_string())?;

    let mut envelope = Vec::with_capacity(MAGIC.len() + NONCE_LEN + encrypted.len());
    envelope.extend_from_slice(MAGIC);
    envelope.extend_from_slice(&nonce_bytes);
    envelope.extend_from_slice(&encrypted);
    Ok(envelope)
}

fn open_with_material(
    credential_ref: &str,
    envelope: &[u8],
    material: &str,
) -> Result<Vec<u8>, String> {
    if envelope.len() < MAGIC.len() + NONCE_LEN + TAG_LEN || &envelope[..MAGIC.len()] != MAGIC {
        return Err("credential envelope is invalid".to_string());
    }
    let nonce_start = MAGIC.len();
    let payload_start = nonce_start + NONCE_LEN;
    let nonce_bytes: [u8; NONCE_LEN] = envelope[nonce_start..payload_start]
        .try_into()
        .map_err(|_| "credential envelope is invalid".to_string())?;
    let unbound = aead::UnboundKey::new(&aead::AES_256_GCM, &derive_key(material))
        .map_err(|_| "credential decryption setup failed".to_string())?;
    let key = aead::LessSafeKey::new(unbound);
    let mut encrypted = envelope[payload_start..].to_vec();
    let plaintext = key
        .open_in_place(
            aead::Nonce::assume_unique_for_key(nonce_bytes),
            aead::Aad::from(credential_ref.as_bytes()),
            &mut encrypted,
        )
        .map_err(|_| "credential cannot be decrypted with the configured keys".to_string())?;
    Ok(plaintext.to_vec())
}

/// True when the backend can seal and resolve canonical credentials. Presence
/// only: neither the key nor its fingerprint is exposed to SQL.
#[pg_extern(stable)]
fn credential_key_available() -> bool {
    !configured_key_materials().is_empty()
}

/// Seal one value using the primary configured key. The reference is AEAD
/// associated data, so moving ciphertext to another credential row fails.
#[pg_extern(volatile)]
fn credential_seal(credential_ref: &str, secret_value: &str) -> Vec<u8> {
    let reference = credential_ref.trim();
    if reference.is_empty() {
        pgrx::error!("credential reference is required");
    }
    if secret_value.is_empty() {
        pgrx::error!("credential value is required");
    }
    let materials = configured_key_materials();
    let Some(primary) = materials.first() else {
        pgrx::error!(
            "canonical credential encryption is unavailable; configure an RVBBIT credential key or key file"
        );
    };
    seal_with_material(reference, secret_value.as_bytes(), primary)
        .unwrap_or_else(|message| pgrx::error!("{message}"))
}

/// Open one envelope, trying the primary key followed by rotation keys. Errors
/// are intentionally generic and never include key material or plaintext.
#[pg_extern(volatile)]
fn credential_unseal(credential_ref: &str, envelope: Vec<u8>) -> String {
    let reference = credential_ref.trim();
    if reference.is_empty() {
        pgrx::error!("credential reference is required");
    }
    let materials = configured_key_materials();
    if materials.is_empty() {
        pgrx::error!(
            "canonical credential decryption is unavailable; configure an RVBBIT credential key or key file"
        );
    }
    for material in &materials {
        if let Ok(value) = open_with_material(reference, &envelope, material) {
            return String::from_utf8(value)
                .unwrap_or_else(|_| pgrx::error!("credential plaintext is not valid UTF-8"));
        }
    }
    pgrx::error!("credential cannot be decrypted with the configured keys")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn envelope_round_trips_and_is_bound_to_reference() {
        let envelope = seal_with_material(
            "mcp/linear/LINEAR_API_KEY",
            b"not-a-real-secret",
            "test-master-key",
        )
        .unwrap();
        assert_ne!(
            envelope
                .windows(b"not-a-real-secret".len())
                .any(|window| window == b"not-a-real-secret"),
            true
        );
        assert_eq!(
            open_with_material("mcp/linear/LINEAR_API_KEY", &envelope, "test-master-key").unwrap(),
            b"not-a-real-secret"
        );
        assert!(
            open_with_material("mcp/other/LINEAR_API_KEY", &envelope, "test-master-key").is_err()
        );
    }

    #[test]
    fn envelope_rejects_wrong_key_and_tampering() {
        let mut envelope =
            seal_with_material("backend/RVBBIT_CLOVER_KEY", b"value", "first").unwrap();
        assert!(open_with_material("backend/RVBBIT_CLOVER_KEY", &envelope, "second").is_err());
        *envelope.last_mut().unwrap() ^= 0x01;
        assert!(open_with_material("backend/RVBBIT_CLOVER_KEY", &envelope, "first").is_err());
    }
}
