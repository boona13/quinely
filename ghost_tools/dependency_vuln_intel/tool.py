import json
import logging
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

OSV_URL = "https://api.osv.dev/v1/querybatch"
MAX_PACKAGES = 80
PROJECT_ROOT = Path(__file__).resolve().parents[2]
log = logging.getLogger("quinely.tools.dependency_vuln_intel")
_NAME_RE = re.compile(r"^([A-Za-z0-9_.-]+)(?:\[[^\]]+\])?\s*(.*)$")


def _parse_requirement(line):
    value = line.split("#", 1)[0].split(";", 1)[0].strip()
    if not value or value.startswith(("-", "git+", "http://", "https://")):
        return None
    match = _NAME_RE.match(value)
    if not match:
        log.warning("Skipping invalid requirement line: %s", value[:120])
        return None
    name, spec = match.groups()
    version = None
    for part in spec.split(","):
        part = part.strip()
        if part.startswith("==="):
            version = part[3:].strip()
            break
        if part.startswith("=="):
            version = part[2:].strip()
            break
    return {"name": name.lower().replace("_", "-"), "version": version or None}


def _safe_manifest(path_value):
    raw = (path_value or "requirements.txt").strip() or "requirements.txt"
    path = Path(raw)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    resolved = path.resolve()
    if resolved != PROJECT_ROOT and PROJECT_ROOT not in resolved.parents:
        raise ValueError("manifest path must stay inside the Quinely project")
    if resolved.name not in ("requirements.txt", "requirements-dev.txt"):
        raise ValueError("only requirements.txt or requirements-dev.txt are supported")
    return resolved


def _query_osv_batch(packages):
    queries = []
    for package in packages:
        query = {"package": {"name": package["name"], "ecosystem": "PyPI"}}
        if package.get("version"):
            query["version"] = package["version"]
        queries.append(query)
    data = json.dumps({"queries": queries}).encode("utf-8")
    req = urllib.request.Request(OSV_URL, data=data, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=12) as response:
        parsed = json.loads(response.read().decode("utf-8"))
    results = parsed.get("results", []) if isinstance(parsed, dict) else []
    return results if isinstance(results, list) else []


def _scan(manifest_path="requirements.txt", include_transitive=False, include_unpinned=True, **kwargs):
    try:
        path = _safe_manifest(manifest_path)
        packages = []
        for line in path.read_text(encoding="utf-8").splitlines():
            item = _parse_requirement(line)
            if item and (include_unpinned or item.get("version")):
                packages.append(item)
            if len(packages) >= MAX_PACKAGES:
                break
        results = _query_osv_batch(packages) if packages else []
        findings = []
        seen = set()
        for package, result in zip(packages, results):
            for vuln in result.get("vulns", []) if isinstance(result, dict) else []:
                vuln_id = vuln.get("id") or "unknown"
                key = (package["name"], vuln_id)
                if key in seen:
                    continue
                seen.add(key)
                findings.append({"package": package["name"], "version": package.get("version"), "id": vuln_id, "summary": (vuln.get("summary") or vuln.get("details") or "")[:220], "aliases": vuln.get("aliases", [])[:6], "modified": vuln.get("modified")})
        unavailable = {"transitive": "not supported"} if include_transitive else {}
        return {"ok": True, "severity": "critical" if findings else "green", "summary": f"Scanned {len(packages)} packages; found {len(findings)} advisories.", "manifest": str(path), "package_count": len(packages), "finding_count": len(findings), "findings": findings, "unavailable": unavailable, "scanned_at": int(time.time())}
    except (ValueError, OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        log.warning("Dependency vulnerability scan failed: %s", exc)
        return {"ok": False, "error": str(exc), "summary": "Dependency vulnerability scan failed.", "findings": [], "unavailable": {"osv": str(exc)[:180]}}


def register(api):
    api.register_tool({"name": "dependency_vuln_scan", "description": "Scan Python requirements against OSV vulnerability intelligence.", "parameters": {"type": "object", "properties": {"manifest_path": {"type": "string", "default": "requirements.txt"}, "include_transitive": {"type": "boolean", "default": False}, "include_unpinned": {"type": "boolean", "default": True}}}, "execute": _scan})
