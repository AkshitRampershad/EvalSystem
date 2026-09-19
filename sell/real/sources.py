"""Fetching real contracts, and caching them so a scan is cheap to repeat.

Two facts about real specs drive this module: they are large (Stripe's is ~8MB
of JSON), and provider history is addressable by git ref, which means real drift
between two shipped versions is available without waiting for one to happen.

Extracted contracts are cached separately from raw specs, because the extraction
is the expensive part and the raw file is only needed once.
"""

from __future__ import annotations

import json
import os
import ssl
import subprocess
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import openapi


@dataclass(frozen=True)
class Provider:
    name: str
    repo: str                  # git remote, for version discovery
    template: str              # raw URL, formatted with {ref}
    default_ref: str = "master"

    def url(self, ref: str) -> str:
        return self.template.format(ref=ref)


PROVIDERS: dict[str, Provider] = {
    "stripe": Provider(
        "stripe", "https://github.com/stripe/openapi",
        "https://raw.githubusercontent.com/stripe/openapi/{ref}/openapi/spec3.json"),
    "github": Provider(
        "github", "https://github.com/github/rest-api-description",
        "https://raw.githubusercontent.com/github/rest-api-description/{ref}"
        "/descriptions/api.github.com/api.github.com.json", default_ref="main"),
}


def cache_dir() -> Path:
    root = os.environ.get("SELL_CACHE") or (Path.home() / ".cache" / "sell-engine")
    path = Path(root)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _ssl_context() -> ssl.SSLContext | None:
    bundle = os.environ.get("SELL_CA_BUNDLE") or os.environ.get("SSL_CERT_FILE")
    return ssl.create_default_context(cafile=bundle) if bundle else None


def fetch(url: str, dest: Path, *, timeout: int = 180,
          refresh: bool = False) -> Path:
    if dest.exists() and dest.stat().st_size > 0 and not refresh:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    request = urllib.request.Request(url, headers={"User-Agent": "sell-engine/0.1"})
    with urllib.request.urlopen(request, timeout=timeout,
                                context=_ssl_context()) as response:
        if response.status != 200:
            raise RuntimeError(f"{response.status} fetching {url}")
        with open(tmp, "wb") as fh:
            while chunk := response.read(1 << 20):
                fh.write(chunk)
    tmp.replace(dest)
    return dest


def list_versions(provider: str, *, limit: int = 20) -> list[str]:
    """Real shipped versions, newest last, discovered from the provider's tags."""
    prov = PROVIDERS[provider]
    out = subprocess.run(["git", "ls-remote", "--tags", prov.repo],
                         capture_output=True, text=True, timeout=180)
    if out.returncode != 0:
        raise RuntimeError(f"git ls-remote failed: {out.stderr.strip()[:200]}")
    tags: list[str] = []
    for line in out.stdout.splitlines():
        ref = line.split("\t")[-1]
        if ref.startswith("refs/tags/") and not ref.endswith("^{}"):
            tags.append(ref[len("refs/tags/"):])
    numeric = [t for t in tags if t.startswith("v") and t[1:].isdigit()]
    ordered = sorted(numeric, key=lambda t: int(t[1:])) if numeric else sorted(tags)
    return ordered[-limit:]


def resolve_spec(ref: str, *, provider: str = "stripe", refresh: bool = False) -> Path:
    """Accepts a local path, an absolute URL, or a provider git ref."""
    candidate = Path(ref)
    if candidate.exists():
        return candidate
    if ref.startswith(("http://", "https://")):
        name = ref.rstrip("/").replace("://", "_").replace("/", "_")[-120:]
        return fetch(ref, cache_dir() / "specs" / name, refresh=refresh)
    prov = PROVIDERS[provider]
    return fetch(prov.url(ref), cache_dir() / "specs" / f"{provider}-{ref}.json",
                 refresh=refresh)


def load_contracts(ref: str, *, provider: str = "stripe",
                   ops: list[tuple[str, str]] | None = None, max_depth: int = 1,
                   refresh: bool = False) -> dict[str, dict[str, Any]]:
    """Extracted contracts for a version, cached. This is the expensive step."""
    key = f"{provider}-{ref}-d{max_depth}-{'all' if ops is None else len(ops)}.contracts.json"
    cached = cache_dir() / "contracts" / key
    if cached.exists() and not refresh:
        with open(cached, encoding="utf-8") as fh:
            return json.load(fh)
    spec = openapi.load(resolve_spec(ref, provider=provider, refresh=refresh))
    result = openapi.contracts(spec, ops, max_depth=max_depth)
    cached.parent.mkdir(parents=True, exist_ok=True)
    cached.write_text(json.dumps(result), encoding="utf-8")
    return result


def load_contract(ref: str, method: str, path: str, *, provider: str = "stripe",
                  max_depth: int = 2, refresh: bool = False) -> dict[str, Any]:
    spec = openapi.load(resolve_spec(ref, provider=provider, refresh=refresh))
    return openapi.contract(spec, method, path, max_depth=max_depth)
