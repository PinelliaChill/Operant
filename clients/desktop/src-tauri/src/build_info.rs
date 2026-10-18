//! Public build provenance only; never expose environment or arbitrary files.
use serde::{Deserialize, Serialize};
use std::path::Path;
#[cfg(any(target_os = "macos", test))]
use std::path::PathBuf;

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

/// Resolve only Resources beside the real executable in this macOS App.
/// macOS may report an executable through a symlinked ancestor (for example
/// its temporary directory alias). Canonicalize before validating the layout;
/// never use the working directory, an environment override, or a source path.
#[cfg(any(target_os = "macos", test))]
pub(crate) fn macos_bundle_resources(executable: &Path) -> Result<PathBuf, String> {
    let executable = executable
        .canonicalize()
        .map_err(|_| "无法定位当前应用程序。")?;
    let macos = executable.parent().ok_or("应用包目录结构无效。")?;
    let contents = macos.parent().ok_or("应用包目录结构无效。")?;
    let bundle = contents.parent().ok_or("应用包目录结构无效。")?;
    if !executable.is_file()
        || executable.file_name() != Some(std::ffi::OsStr::new("operant-desktop"))
        || macos.file_name() != Some(std::ffi::OsStr::new("MacOS"))
        || contents.file_name() != Some(std::ffi::OsStr::new("Contents"))
        || bundle.extension() != Some(std::ffi::OsStr::new("app"))
    {
        return Err("应用包目录结构无效。".into());
    }
    let expected = contents.join("Resources");
    let resources = expected
        .canonicalize()
        .map_err(|_| "无法定位构建来源摘要。")?;
    if resources != expected || !resources.is_dir() {
        return Err("应用资源目录不属于当前应用包。".into());
    }
    Ok(resources)
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
        eprintln!("operant:build_info:metadata_mismatch");
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
            "release_label": "0.1.0-beta.5",
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
        assert_eq!(info.release_label.as_deref(), Some("0.1.0-beta.5"));
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
    struct TestBundle {
        root: PathBuf,
        executable: PathBuf,
        resources: PathBuf,
    }

    impl TestBundle {
        fn new() -> Self {
            let unique = std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos();
            let root = std::env::temp_dir().join(format!(
                "operant-build-info-{}-{unique}",
                std::process::id()
            ));
            let contents = root.join("App With Spaces.app/Contents");
            std::fs::create_dir_all(contents.join("MacOS")).unwrap();
            std::fs::create_dir_all(contents.join("Resources")).unwrap();
            let executable = contents.join("MacOS/operant-desktop");
            std::fs::write(&executable, "test executable path").unwrap();
            Self {
                root,
                executable,
                resources: contents.join("Resources"),
            }
        }
    }

    impl Drop for TestBundle {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.root);
        }
    }

    #[test]
    fn bundle_resources_use_the_canonical_same_app_with_spaces() {
        let bundle = TestBundle::new();
        let canonical = bundle.executable.canonicalize().unwrap();
        assert_eq!(
            macos_bundle_resources(&canonical).unwrap(),
            bundle.resources.canonicalize().unwrap()
        );
        assert!(macos_bundle_resources(&bundle.root.join("missing")).is_err());
        let wrong = bundle.executable.with_file_name("another-program");
        std::fs::write(&wrong, "not the desktop").unwrap();
        assert!(macos_bundle_resources(&wrong).is_err());
    }

    #[cfg(unix)]
    #[test]
    fn symlinked_ancestor_resolves_but_resources_cannot_escape_the_app() {
        let bundle = TestBundle::new();
        let alias = bundle.root.join("Alias With Spaces");
        std::os::unix::fs::symlink(bundle.root.join("App With Spaces.app"), &alias).unwrap();
        let via_alias = alias.join("Contents/MacOS/operant-desktop");
        assert_eq!(
            macos_bundle_resources(&via_alias).unwrap(),
            bundle.resources.canonicalize().unwrap()
        );
        std::fs::remove_dir(&bundle.resources).unwrap();
        let outside = bundle.root.join("unrelated-resources");
        std::fs::create_dir(&outside).unwrap();
        std::os::unix::fs::symlink(&outside, &bundle.resources).unwrap();
        assert!(macos_bundle_resources(&via_alias).is_err());
    }
}
