from scripts.ci_scope import required_suites


def test_documentation_does_not_start_product_suites() -> None:
    assert required_suites(["README.md", "docs/releases/v0.1.0-beta.1.md"]) == (False, False)


def test_gui_and_backend_changes_keep_their_checks() -> None:
    assert required_suites(["clients/gui/src/app/App.tsx"]) == (False, True)
    assert required_suites(["uv.lock", "tests/test_runtime.py"]) == (True, False)
    assert required_suites(["plugins/memory-standard/plugin.py"]) == (True, False)
    assert required_suites(["src/operant/api.py", "clients/gui/package-lock.json"]) == (True, True)


def test_shared_unknown_and_empty_changes_fail_safe() -> None:
    for path in [
        "sdk/typescript-client/index.ts",
        ".github/workflows/ci.yml",
        "new-config.json",
        "docs/schema.json",
    ]:
        assert required_suites([path]) == (True, True)
    assert required_suites([]) == (True, True)


def test_code_deletion_or_rename_still_runs_checks() -> None:
    assert required_suites(["src/operant/api.py", "docs/deleted-api.md"]) == (True, False)
