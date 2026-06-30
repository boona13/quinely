import importlib.util
import sys
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOL_PATH = ROOT / "ghost_tools" / "dependency_vuln_intel" / "tool.py"
spec = importlib.util.spec_from_file_location("dependency_vuln_intel_tool", TOOL_PATH)
vuln_tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vuln_tool)

def test_parse_requirement_handles_extras_markers_and_pins():
    parsed = vuln_tool._parse_requirement('Requests[security]==2.31.0; python_version >= "3.10" # comment')
    assert parsed == {"name": "requests", "version": "2.31.0"}


def test_safe_manifest_blocks_traversal():
    try:
        vuln_tool._safe_manifest('../requirements.txt')
    except ValueError as exc:
        assert 'project' in str(exc)
    else:
        raise AssertionError('path traversal was not blocked')


def test_scan_returns_findings_with_mocked_osv(tmp_path, monkeypatch):
    manifest = ROOT / 'requirements-dev.txt'
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text('demo-pkg==1.0.0\n', encoding='utf-8')

    def fake_query(packages):
        assert packages == [{"name": "demo-pkg", "version": "1.0.0"}]
        return [{"vulns": [{"id": "OSV-TEST", "summary": "test advisory", "aliases": ["CVE-TEST"], "modified": "2026-01-01T00:00:00Z"}]}]

    monkeypatch.setattr(vuln_tool, '_query_osv_batch', fake_query)
    try:
        result = vuln_tool._scan(manifest_path='requirements-dev.txt')
    finally:
        manifest.unlink(missing_ok=True)

    assert result['ok'] is True
    assert result['package_count'] == 1
    assert result['finding_count'] == 1
    assert result['findings'][0]['id'] == 'OSV-TEST'


def test_scan_reports_network_failure(monkeypatch):
    def fail_query(packages):
        raise urllib.error.URLError('offline')

    monkeypatch.setattr(vuln_tool, '_query_osv_batch', fail_query)
    result = vuln_tool._scan(manifest_path='requirements.txt')
    assert result['ok'] is False
    assert 'offline' in result['error']
