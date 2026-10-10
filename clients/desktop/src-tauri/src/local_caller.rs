//! Internal, request-scoped `local-caller.v1` proof construction.
//! The key and nonce stay in native memory. Bootstrap only writes to a
//! caller-provided anonymous pipe; this module does not send network requests
//! or expose the key to JS.

use std::io::Write;

use hmac::{Hmac, Mac};
use sha2::{Digest, Sha256};

pub(crate) const PROTOCOL: &str = "local-caller.v1";
const MAX_BODY_BYTES: usize = 1024 * 1024;
const MAX_PATH_BYTES: usize = 2048;
const MAX_QUERY_BYTES: usize = 4096;
const MAX_IDEMPOTENCY_BYTES: usize = 300;
const MAX_TIMESTAMP: u64 = 9_999_999_999_999;

type HmacSha256 = Hmac<Sha256>;

/// Neither `Debug` nor `Serialize` is implemented for this secret-bearing type.
pub(crate) struct LocalCallerKey([u8; 32]);

#[derive(Debug, PartialEq, Eq)]
pub(crate) enum SignError {
    RandomUnavailable,
    InvalidMethod,
    InvalidPath,
    InvalidQuery,
    InvalidTimestamp,
    InvalidNonce,
    InvalidIdempotencyKey,
    InvalidBody,
    BootstrapWriteFailed,
}

pub(crate) struct ProofRequest<'a> {
    pub(crate) method: &'a str,
    pub(crate) raw_path: &'a str,
    pub(crate) raw_query: &'a str,
    pub(crate) timestamp: u64,
    pub(crate) idempotency_key: Option<&'a str>,
    pub(crate) body: &'a [u8],
}

/// Values to place in the fixed local-caller headers. The caller keeps the
/// original method, target, idempotency key and body byte-for-byte unchanged.
pub(crate) struct SignedProof {
    pub(crate) timestamp: String,
    pub(crate) nonce: String,
    pub(crate) signature: String,
}

impl LocalCallerKey {
    pub(crate) fn generate() -> Result<Self, SignError> {
        let mut key = [0u8; 32];
        getrandom::fill(&mut key).map_err(|_| SignError::RandomUnavailable)?;
        Ok(Self(key))
    }

    pub(crate) fn sign(&self, request: &ProofRequest<'_>) -> Result<SignedProof, SignError> {
        let mut nonce = [0u8; 16];
        getrandom::fill(&mut nonce).map_err(|_| SignError::RandomUnavailable)?;
        self.sign_with_nonce(request, nonce)
    }

    /// Write only the 32 key bytes. The caller owns the anonymous pipe and
    /// must abandon bootstrap if a short or failed write occurs.
    pub(crate) fn write_bootstrap(&self, pipe: &mut impl Write) -> Result<(), SignError> {
        pipe.write_all(&self.0)
            .map_err(|_| SignError::BootstrapWriteFailed)
    }

    /// Verify the Core's response to a caller-chosen, fresh nonce. The caller
    /// must compare against its outstanding nonce, not a response-supplied one.
    pub(crate) fn verify_core_identity(&self, nonce: &str, proof: &str) -> bool {
        let Some(_nonce_bytes) = decode_lower_hex::<16>(nonce) else {
            return false;
        };
        let Some(proof_bytes) = decode_lower_hex::<32>(proof) else {
            return false;
        };
        let mut mac = HmacSha256::new_from_slice(&self.0).expect("32-byte HMAC key is valid");
        mac.update(b"local-caller.core.v1\n");
        mac.update(nonce.as_bytes());
        mac.verify_slice(&proof_bytes).is_ok()
    }

    /// Extra proof for the native-only, user-confirmed caller challenge route.
    /// Its domain, method and path are fixed here; the caller cannot turn it
    /// into a generic signing service.
    pub(crate) fn sign_native_confirmation(
        &self,
        request_nonce: &str,
        body: &[u8],
    ) -> Result<String, SignError> {
        if decode_lower_hex::<16>(request_nonce).is_none() {
            return Err(SignError::InvalidNonce);
        }
        if body.len() > MAX_BODY_BYTES || std::str::from_utf8(body).is_err() {
            return Err(SignError::InvalidBody);
        }
        let digest = hex_lower(&Sha256::digest(body));
        let canonical = format!(
            "local-caller.confirm.v1\nPOST\n/v1/local-callers/challenges\n{request_nonce}\n{digest}"
        );
        let mut mac = HmacSha256::new_from_slice(&self.0).expect("32-byte HMAC key is valid");
        mac.update(canonical.as_bytes());
        Ok(hex_lower(&mac.finalize().into_bytes()))
    }

    fn sign_with_nonce(
        &self,
        request: &ProofRequest<'_>,
        nonce_bytes: [u8; 16],
    ) -> Result<SignedProof, SignError> {
        let proof = canonical_proof(request, nonce_bytes)?;
        let mut mac = HmacSha256::new_from_slice(&self.0).expect("32-byte HMAC key is valid");
        mac.update(proof.as_bytes());
        Ok(SignedProof {
            timestamp: request.timestamp.to_string(),
            nonce: hex_lower(&nonce_bytes),
            signature: hex_lower(&mac.finalize().into_bytes()),
        })
    }
}

fn canonical_proof(request: &ProofRequest<'_>, nonce_bytes: [u8; 16]) -> Result<String, SignError> {
    if !matches!(request.method, "GET" | "POST" | "PUT" | "PATCH" | "DELETE") {
        return Err(SignError::InvalidMethod);
    }
    let path = request.raw_path.as_bytes();
    if path.is_empty()
        || path.len() > MAX_PATH_BYTES
        || path[0] != b'/'
        || !path.iter().all(|byte| {
            byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'~' | b'/' | b'-')
        })
        || path.windows(2).any(|pair| pair == b"//")
        || path
            .split(|byte| *byte == b'/')
            .any(|segment| segment == b"." || segment == b"..")
    {
        return Err(SignError::InvalidPath);
    }
    let query = request.raw_query.as_bytes();
    if query.len() > MAX_QUERY_BYTES
        || query
            .iter()
            .any(|byte| !(0x21..=0x7e).contains(byte) || *byte == b'#')
    {
        return Err(SignError::InvalidQuery);
    }
    if request.timestamp == 0 || request.timestamp > MAX_TIMESTAMP {
        return Err(SignError::InvalidTimestamp);
    }
    let idempotency = request.idempotency_key.unwrap_or("");
    if (request.method != "GET" && idempotency.is_empty())
        || idempotency.len() > MAX_IDEMPOTENCY_BYTES
        || idempotency
            .bytes()
            .any(|byte| !(0x21..=0x7e).contains(&byte))
    {
        return Err(SignError::InvalidIdempotencyKey);
    }
    if request.body.len() > MAX_BODY_BYTES || std::str::from_utf8(request.body).is_err() {
        return Err(SignError::InvalidBody);
    }

    let target = if request.raw_query.is_empty() {
        request.raw_path.to_owned()
    } else {
        format!("{}?{}", request.raw_path, request.raw_query)
    };
    let body_digest = hex_lower(&Sha256::digest(request.body));
    Ok(format!(
        "{PROTOCOL}\n{}\n{target}\n{}\n{}\n{idempotency}\n{body_digest}",
        request.method,
        request.timestamp,
        hex_lower(&nonce_bytes)
    ))
}

fn hex_lower(bytes: &[u8]) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut result = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        result.push(char::from(HEX[usize::from(byte >> 4)]));
        result.push(char::from(HEX[usize::from(byte & 0x0f)]));
    }
    result
}

fn decode_lower_hex<const N: usize>(value: &str) -> Option<[u8; N]> {
    if value.len() != N * 2 {
        return None;
    }
    let mut result = [0u8; N];
    for (index, pair) in value.as_bytes().chunks_exact(2).enumerate() {
        let nibble = |byte: u8| -> Option<u8> {
            match byte {
                b'0'..=b'9' => Some(byte - b'0'),
                b'a'..=b'f' => Some(byte - b'a' + 10),
                _ => None,
            }
        };
        result[index] = (nibble(pair[0])? << 4) | nibble(pair[1])?;
    }
    Some(result)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io;

    const NONCE: [u8; 16] = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15];

    fn fixture_request<'a>(body: &'a [u8]) -> ProofRequest<'a> {
        ProofRequest {
            method: "POST",
            raw_path: "/v1/setup/skill-sources",
            raw_query: "b=2&a=1",
            timestamp: 1_800_000_000,
            idempotency_key: Some("fixture-key-1"),
            body,
        }
    }

    #[test]
    fn matches_public_python_fixture_exactly() {
        // Python stdlib: key=bytes(range(32)); nonce=bytes(range(16));
        // proof=b'\n'.join((b'local-caller.v1', b'POST', target, timestamp,
        // nonce.hex().encode(), idempotency, sha256(body).hexdigest().encode()));
        // hmac.new(key, proof, hashlib.sha256).hexdigest().
        let key = LocalCallerKey(std::array::from_fn(|index| index as u8));
        let request = fixture_request("{\"path\":\"/tmp/skills\",\"label\":\"中文\"}".as_bytes());
        let proof = canonical_proof(&request, NONCE).unwrap();
        assert_eq!(
            proof,
            "local-caller.v1\nPOST\n/v1/setup/skill-sources?b=2&a=1\n1800000000\n000102030405060708090a0b0c0d0e0f\nfixture-key-1\nb247e9781f9424999daa07d6968763ac04c6933b870b07826bda3c4ef6ca1b94"
        );
        let signed = key.sign_with_nonce(&request, NONCE).unwrap();
        assert_eq!(signed.timestamp, "1800000000");
        assert_eq!(signed.nonce, "000102030405060708090a0b0c0d0e0f");
        assert_eq!(
            signed.signature,
            "2d6729abb36dcc7c0f18cd5806bd7c9f36ad7767a8a8fbaf1eec46ed6511e7d6"
        );
    }

    #[test]
    fn bootstrap_writes_exact_key_bytes_without_returning_them() {
        let key = LocalCallerKey(std::array::from_fn(|index| index as u8));
        let mut pipe = Vec::new();
        assert_eq!(key.write_bootstrap(&mut pipe), Ok(()));
        assert_eq!(pipe, (0u8..32).collect::<Vec<_>>());
    }

    #[test]
    fn bootstrap_failure_has_only_a_fixed_error_class() {
        struct BrokenPipe;
        impl Write for BrokenPipe {
            fn write(&mut self, _bytes: &[u8]) -> io::Result<usize> {
                Err(io::Error::other("SENTINEL_PRIVATE_PIPE_DETAIL"))
            }
            fn flush(&mut self) -> io::Result<()> {
                Ok(())
            }
        }
        let key = LocalCallerKey([0x42; 32]);
        assert_eq!(
            key.write_bootstrap(&mut BrokenPipe),
            Err(SignError::BootstrapWriteFailed)
        );
        assert!(!format!("{:?}", SignError::BootstrapWriteFailed).contains("SENTINEL"));
    }

    #[test]
    fn core_identity_matches_public_python_vector_and_rejects_mutation() {
        // Python: hmac.new(bytes(range(32)),
        // b'local-caller.core.v1\n'+bytes(range(16)).hex().encode(),
        // hashlib.sha256).hexdigest()
        let key = LocalCallerKey(std::array::from_fn(|index| index as u8));
        let nonce = "000102030405060708090a0b0c0d0e0f";
        let proof = "ef52b0460d03e2d3186a9d0773e465657ee161846f6656b12c5c3822932737b7";
        assert!(key.verify_core_identity(nonce, proof));
        assert!(!key.verify_core_identity("100102030405060708090a0b0c0d0e0f", proof));
        assert!(!key.verify_core_identity(
            nonce,
            "ff52b0460d03e2d3186a9d0773e465657ee161846f6656b12c5c3822932737b7"
        ));
        assert!(!LocalCallerKey([0x42; 32]).verify_core_identity(nonce, proof));
    }

    #[test]
    fn native_confirmation_matches_fixed_python_vector_and_rejects_bad_nonce() {
        // Python: hmac.new(bytes(range(32)), b"local-caller.confirm.v1\nPOST\n"
        // b"/v1/local-callers/challenges\n" + bytes(range(16)).hex().encode()
        // + b"\n" + sha256(b'{"ttl_seconds":120}').hexdigest().encode(), sha256).
        let key = LocalCallerKey(std::array::from_fn(|index| index as u8));
        let nonce = "000102030405060708090a0b0c0d0e0f";
        assert_eq!(
            key.sign_native_confirmation(nonce, b"{\"ttl_seconds\":120}")
                .unwrap(),
            "120fe367fd4535afacade7ea709c5399b8be43b31834286b82433f752899b375"
        );
        assert_eq!(
            key.sign_native_confirmation("000102030405060708090a0b0c0d0e0F", b"{}"),
            Err(SignError::InvalidNonce)
        );
        assert_eq!(
            key.sign_native_confirmation(nonce, b"\xff"),
            Err(SignError::InvalidBody)
        );
    }

    #[test]
    fn core_identity_rejects_noncanonical_hex_and_length() {
        let key = LocalCallerKey(std::array::from_fn(|index| index as u8));
        let nonce = "000102030405060708090a0b0c0d0e0f";
        let proof = "ef52b0460d03e2d3186a9d0773e465657ee161846f6656b12c5c3822932737b7";
        assert!(key.verify_core_identity(nonce, proof));
        for invalid_nonce in [
            "",
            "0",
            "000102030405060708090a0b0c0d0e0F",
            "000102030405060708090a0b0c0d0e0g",
        ] {
            assert!(!key.verify_core_identity(invalid_nonce, proof));
        }
        for invalid_proof in [
            "",
            "0",
            "Ef52b0460d03e2d3186a9d0773e465657ee161846f6656b12c5c3822932737b7",
            "ef52b0460d03e2d3186a9d0773e465657ee161846f6656b12c5c3822932737b8",
        ] {
            assert!(!key.verify_core_identity(nonce, invalid_proof));
        }
    }

    #[test]
    fn changed_fields_change_signature_without_reordering_query() {
        let key = LocalCallerKey([0x42; 32]);
        let body = b"{}";
        let original = fixture_request(body);
        let original_signature = key.sign_with_nonce(&original, NONCE).unwrap().signature;
        let changed = [
            ProofRequest {
                method: "PUT",
                ..fixture_request(body)
            },
            ProofRequest {
                raw_path: "/v1/setup/other-sources",
                ..fixture_request(body)
            },
            ProofRequest {
                raw_query: "a=1&b=2",
                ..fixture_request(body)
            },
            ProofRequest {
                timestamp: 1_800_000_001,
                ..fixture_request(body)
            },
            ProofRequest {
                idempotency_key: Some("fixture-key-2"),
                ..fixture_request(body)
            },
            fixture_request(b"{\"changed\":true}"),
        ];
        for request in changed {
            assert_ne!(
                key.sign_with_nonce(&request, NONCE).unwrap().signature,
                original_signature
            );
        }
        assert_ne!(
            key.sign_with_nonce(&original, [0x99; 16])
                .unwrap()
                .signature,
            original_signature
        );
    }

    #[test]
    fn rejects_invalid_method_path_query_timestamp_and_header_values() {
        let body = b"{}";
        let base = fixture_request(body);
        let invalid = [
            (
                ProofRequest {
                    method: "HEAD",
                    ..fixture_request(body)
                },
                SignError::InvalidMethod,
            ),
            (
                ProofRequest {
                    raw_path: "/v1//setup",
                    ..fixture_request(body)
                },
                SignError::InvalidPath,
            ),
            (
                ProofRequest {
                    raw_path: "/v1/../setup",
                    ..fixture_request(body)
                },
                SignError::InvalidPath,
            ),
            (
                ProofRequest {
                    raw_path: "/v1/%2e%2e/setup",
                    ..fixture_request(body)
                },
                SignError::InvalidPath,
            ),
            (
                ProofRequest {
                    raw_path: "/v1/技能",
                    ..fixture_request(body)
                },
                SignError::InvalidPath,
            ),
            (
                ProofRequest {
                    raw_query: "a=1#fragment",
                    ..fixture_request(body)
                },
                SignError::InvalidQuery,
            ),
            (
                ProofRequest {
                    raw_query: "a=1\r\nb=2",
                    ..fixture_request(body)
                },
                SignError::InvalidQuery,
            ),
            (
                ProofRequest {
                    timestamp: 0,
                    ..fixture_request(body)
                },
                SignError::InvalidTimestamp,
            ),
            (
                ProofRequest {
                    timestamp: MAX_TIMESTAMP + 1,
                    ..fixture_request(body)
                },
                SignError::InvalidTimestamp,
            ),
            (
                ProofRequest {
                    idempotency_key: None,
                    ..fixture_request(body)
                },
                SignError::InvalidIdempotencyKey,
            ),
            (
                ProofRequest {
                    idempotency_key: Some("has space"),
                    ..fixture_request(body)
                },
                SignError::InvalidIdempotencyKey,
            ),
            (
                ProofRequest {
                    idempotency_key: Some("a\r\nb"),
                    ..fixture_request(body)
                },
                SignError::InvalidIdempotencyKey,
            ),
        ];
        for (request, expected) in invalid {
            assert_eq!(canonical_proof(&request, NONCE), Err(expected));
        }
        assert!(canonical_proof(&base, NONCE).is_ok());
    }

    #[test]
    fn enforces_exact_ascii_field_lengths() {
        let body = b"{}";
        let path_at_limit = format!("/{}", "a".repeat(MAX_PATH_BYTES - 1));
        let path_too_long = format!("/{}", "a".repeat(MAX_PATH_BYTES));
        let query_at_limit = "a".repeat(MAX_QUERY_BYTES);
        let query_too_long = "a".repeat(MAX_QUERY_BYTES + 1);
        let key_at_limit = "k".repeat(MAX_IDEMPOTENCY_BYTES);
        let key_too_long = "k".repeat(MAX_IDEMPOTENCY_BYTES + 1);
        assert!(canonical_proof(
            &ProofRequest {
                raw_path: &path_at_limit,
                ..fixture_request(body)
            },
            NONCE
        )
        .is_ok());
        assert_eq!(
            canonical_proof(
                &ProofRequest {
                    raw_path: &path_too_long,
                    ..fixture_request(body)
                },
                NONCE
            ),
            Err(SignError::InvalidPath)
        );
        assert!(canonical_proof(
            &ProofRequest {
                raw_query: &query_at_limit,
                ..fixture_request(body)
            },
            NONCE
        )
        .is_ok());
        assert_eq!(
            canonical_proof(
                &ProofRequest {
                    raw_query: &query_too_long,
                    ..fixture_request(body)
                },
                NONCE
            ),
            Err(SignError::InvalidQuery)
        );
        assert!(canonical_proof(
            &ProofRequest {
                idempotency_key: Some(&key_at_limit),
                ..fixture_request(body)
            },
            NONCE
        )
        .is_ok());
        assert_eq!(
            canonical_proof(
                &ProofRequest {
                    idempotency_key: Some(&key_too_long),
                    ..fixture_request(body)
                },
                NONCE
            ),
            Err(SignError::InvalidIdempotencyKey)
        );
    }

    #[test]
    fn enforces_utf8_body_limit_and_optional_get_idempotency() {
        let key = LocalCallerKey([0x42; 32]);
        let too_large = vec![b'x'; MAX_BODY_BYTES + 1];
        assert_eq!(
            canonical_proof(&fixture_request(&too_large), NONCE),
            Err(SignError::InvalidBody)
        );
        assert_eq!(
            canonical_proof(&fixture_request(&[0xff]), NONCE),
            Err(SignError::InvalidBody)
        );
        assert!(canonical_proof(&fixture_request(&vec![b'x'; MAX_BODY_BYTES]), NONCE).is_ok());
        let get = ProofRequest {
            method: "GET",
            idempotency_key: None,
            ..fixture_request(b"")
        };
        assert!(key.sign_with_nonce(&get, NONCE).is_ok());
        let signed = key.sign(&get).unwrap();
        assert_eq!(signed.nonce.len(), 32);
        assert!(signed
            .nonce
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase()));
        assert!(LocalCallerKey::generate().is_ok());
    }
}
