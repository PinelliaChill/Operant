//! Public build provenance only; never expose environment or arbitrary files.
use serde::{Deserialize, Serialize};
use std::path::Path;

#[derive(Deserialize)]
struct SourceManifest {
    product_version: String,
    release_label: Option<String>,
    source_head: String,
    candidate_id: Option<String>,
    bundle_identifier: String,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct BuildInfo {
    build_kind: &'static str,
    product_version: String,
    release_label: Option<String>,
    source_commit: Option<String>,
    source_dirty: bool,
    candidate_id: Option<String>,
    bundle_identifier: String,
}

fn source_commit() -> Option<String> {
    option_env!("OPERANT_BUILD_SOURCE_HEAD")
        .filter(|value| valid_commit(value))
        .map(String::from)
}

fn valid_commit(value: &str) -> bool {
    value.len() == 40 && value.bytes().all(|byte| byte.is_ascii_hexdigit())
}

fn candidate_matches_identifier(candidate: Option<&str>, identifier: &str) -> bool {
    match candidate {
        Some(id) => {
            !id.is_empty()
                && id.len() <= 48
                && id.split('-').all(|part| {
                    !part.is_empty()
                        && part
                            .bytes()
                            .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit())
                })
                && identifier.ends_with(&format!(".candidate.{id}"))
        }
        None => !identifier.contains(".candidate."),
    }
}

pub(crate) fn read_build_info(resources: &Path, identifier: &str) -> Result<BuildInfo, String> {
    let development = cfg!(debug_assertions);
    let source = source_commit();
    if !development {
        let path = resources.join("来源摘要.json");
        if path.exists() {
            let raw = std::fs::read(path).map_err(|_| {
                eprintln!("operant:build_info:manifest_unreadable");
                "无法读取构建来源摘要。"
            })?;
            return packaged_build_info(
                &raw,
                source.as_deref(),
                option_env!("OPERANT_BUILD_SOURCE_DIRTY") != Some("false"),
                identifier,
            );
        }
    }
    Ok(BuildInfo {
        build_kind: if development {
            "development"
        } else {
            "unmarked"
        },
        product_version: env!("CARGO_PKG_VERSION").into(),
        release_label: None,
        source_commit: source,
        source_dirty: option_env!("OPERANT_BUILD_SOURCE_DIRTY") != Some("false"),
        candidate_id: None,
        bundle_identifier: identifier.into(),
    })
}

fn packaged_build_info(
    raw: &[u8],
    source: Option<&str>,
    dirty: bool,
    identifier: &str,
) -> Result<BuildInfo, String> {
    let manifest: SourceManifest = serde_json::from_slice(raw).map_err(|_| {
        eprintln!("operant:build_info:manifest_invalid");
        "构建来源摘要格式无效。"
    })?;
    if dirty
        || !valid_commit(&manifest.source_head)
        || source != Some(manifest.source_head.as_str())
        || manifest.product_version != env!("CARGO_PKG_VERSION")
        || manifest.bundle_identifier != identifier
        || !candidate_matches_identifier(manifest.candidate_id.as_deref(), identifier)
    {
        eprintln!("operant:build_info:metadata_mismatch dirty={} source={} version={} identifier={} candidate={}",
            dirty,
            source == Some(manifest.source_head.as_str()),
            manifest.product_version == env!("CARGO_PKG_VERSION"),
            manifest.bundle_identifier == identifier,
            candidate_matches_identifier(manifest.candidate_id.as_deref(), identifier));
        return Err("构建来源摘要与当前程序不一致。".into());
    }
    Ok(BuildInfo {
        build_kind: "packaged",
        product_version: manifest.product_version,
        release_label: manifest.release_label,
        source_commit: Some(manifest.source_head),
        source_dirty: false,
        candidate_id: manifest.candidate_id,
        bundle_identifier: manifest.bundle_identifier,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn commit_must_be_a_full_hash() {
        assert!(valid_commit(&"a".repeat(40)));
        assert!(!valid_commit("33d8fa4"));
        assert!(!valid_commit(&"z".repeat(40)));
    }

    #[test]
    fn package_uses_explicit_release_and_candidate_from_matching_source() {
        let head = "a".repeat(40);
        let raw = serde_json::to_vec(&serde_json::json!({
            "product_version": env!("CARGO_PKG_VERSION"),
            "release_label": "2.0.0-beta.5",
            "source_head": head,
            "candidate_id": "usage-polish",
            "bundle_identifier": "dev.operant.desktop.candidate.usage-polish"
        }))
        .unwrap();
        let info = packaged_build_info(
            &raw,
            Some(&head),
            false,
            "dev.operant.desktop.candidate.usage-polish",
        )
        .unwrap();
        assert_eq!(info.release_label.as_deref(), Some("2.0.0-beta.5"));
        assert_eq!(info.candidate_id.as_deref(), Some("usage-polish"));
        assert_eq!(info.source_commit.as_deref(), Some(head.as_str()));
        assert!(packaged_build_info(
            &raw,
            Some(&"b".repeat(40)),
            false,
            "dev.operant.desktop.candidate.usage-polish"
        )
        .is_err());
        assert!(packaged_build_info(
            &raw,
            None,
            false,
            "dev.operant.desktop.candidate.usage-polish"
        )
        .is_err());
        assert!(packaged_build_info(
            &raw,
            Some(&head),
            true,
            "dev.operant.desktop.candidate.usage-polish"
        )
        .is_err());
        assert!(packaged_build_info(&raw, Some(&head), false, "dev.operant.desktop").is_err());
    }

    #[test]
    fn legacy_manifest_does_not_invent_a_release_label() {
        let head = "a".repeat(40);
        let raw = serde_json::to_vec(&serde_json::json!({
            "product_version": env!("CARGO_PKG_VERSION"),
            "source_head": head,
            "candidate_id": null,
            "bundle_identifier": "dev.operant.desktop"
        }))
        .unwrap();
        assert!(
            packaged_build_info(&raw, Some(&head), false, "dev.operant.desktop")
                .unwrap()
                .release_label
                .is_none()
        );
    }

    #[test]
    fn candidate_metadata_cannot_disagree_with_bundle_identity() {
        assert!(candidate_matches_identifier(
            Some("usage-polish"),
            "dev.operant.desktop.candidate.usage-polish"
        ));
        assert!(candidate_matches_identifier(None, "dev.operant.desktop"));
        assert!(!candidate_matches_identifier(
            Some("usage-polish"),
            "dev.operant.desktop"
        ));
        assert!(!candidate_matches_identifier(
            None,
            "dev.operant.desktop.candidate.usage-polish"
        ));
        assert!(!candidate_matches_identifier(
            Some("other"),
            "dev.operant.desktop.candidate.usage-polish"
        ));
        assert!(!candidate_matches_identifier(
            Some("has--gap"),
            "dev.operant.desktop.candidate.has--gap"
        ));
    }

    #[test]
    fn debug_build_cannot_claim_packaged_identity() {
        let info =
            read_build_info(Path::new("/nonexistent-operant-build-info"), "test.dev").unwrap();
        assert_eq!(info.build_kind, "development");
        assert!(info.release_label.is_none());
        assert!(info.candidate_id.is_none());
    }
}
