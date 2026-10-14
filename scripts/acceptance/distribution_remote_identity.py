"""Record a safe remote OS marker and installed helper artifact readback."""

from __future__ import annotations

import hashlib
import json
import platform
import sys
from pathlib import Path

from pypdf import PdfReader

root = Path(sys.argv[1]).resolve()
system = dict(
    line.split("=", 1) for line in Path("/etc/os-release").read_text().splitlines() if "=" in line
)
pdf = root / "data" / "helper-result.pdf"
reader = PdfReader(pdf)
assert len(reader.pages) == 1
assert "Candidate install check" in (reader.pages[0].extract_text() or "")
result = {
    "platform": platform.system(),
    "os_id": system["ID"].strip('"'),
    "os_version": system["VERSION_ID"].strip('"'),
    "machine_marker_sha12": hashlib.sha256(Path("/etc/machine-id").read_bytes()).hexdigest()[:12],
    "helper_pdf_readback": True,
    "helper_pdf_sha256": hashlib.sha256(pdf.read_bytes()).hexdigest(),
}
(root / "remote-identity-helper.json").write_text(
    json.dumps(result, indent=2, sort_keys=True) + "\n"
)
print(json.dumps(result, sort_keys=True))
