use std::process::Command;

fn git(root: &std::path::Path, args: &[&str]) -> Option<String> {
    let result = Command::new("git")
        .args(["-c", "core.fsmonitor=false"])
        .args(args)
        .current_dir(root)
        .output()
        .ok()?;
    if !result.status.success() {
        return None;
    }
    String::from_utf8(result.stdout)
        .ok()
        .map(|text| text.trim().into())
}

fn main() {
    let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../..");
    if let Some(head) = git(&root, &["rev-parse", "HEAD"]) {
        println!("cargo:rustc-env=OPERANT_BUILD_SOURCE_HEAD={head}");
    }
    let clean = git(&root, &["status", "--porcelain"]).is_some_and(|text| text.is_empty());
    println!("cargo:rustc-env=OPERANT_BUILD_SOURCE_DIRTY={}", !clean);
    // A missing input intentionally reruns this cheap provenance check on each
    // Cargo invocation, including empty commits and edits outside this crate.
    // Watching only HEAD/index or selected source directories can cache lies.
    let refresh = std::path::Path::new(&std::env::var("OUT_DIR").unwrap())
        .join("build-provenance-always-refresh");
    println!("cargo:rerun-if-changed={}", refresh.display());
    tauri_build::build()
}
