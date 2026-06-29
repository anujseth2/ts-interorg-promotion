"""UI-driven setup helpers: build a client from explicit config, discover orgs and
connections (so you pick from real values instead of guessing ids/db/schema), and persist
everything to .env + variables/targets.json. Lets the whole tool be configured in the UI
with no hand-editing of files - which is where setup pain usually lives.
"""
import json
import os
from pathlib import Path

from services.ts_client import TSClient

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"
TARGETS_PATH = ROOT / "variables" / "targets.json"

# env keys written/read; order is the .env file order
ENV_KEYS = ["TS_HOST", "TS_USER", "TS_PASSWORD", "TS_TOKEN", "TS_SECRET_KEY",
            "TS_ORG_PRIMARY", "TS_ORG_SOURCE", "TS_RELEASE_TAG", "TS_RESOLVE_LOCAL",
            "GIT_LOCAL_DIR", "GITHUB_REPO", "GITHUB_TOKEN", "GIT_BRANCH"]


def make_client(cfg: dict, org_id="") -> TSClient:
    """A client built straight from form config (no env dependency)."""
    return TSClient(host=cfg["host"],
                    username=cfg.get("user", ""), password=cfg.get("password", ""),
                    token=cfg.get("token", ""), secret_key=cfg.get("secret", ""),
                    org_id=str(org_id))


def list_orgs(cfg: dict):
    """[(id, name)] for every org. Doubles as the auth test - raises on bad creds."""
    c = make_client(cfg, cfg.get("primary_org", "0"))
    data = c._post("/api/rest/2.0/orgs/search", {})
    orgs = data if isinstance(data, list) else data.get("orgs", [])
    return [(str(o.get("id")), o.get("name")) for o in orgs]


def list_connections(cfg: dict, org_id):
    """[(id, name)] of connections in an org (pick by name, no typos)."""
    c = make_client(cfg, org_id)
    data = c._post("/api/rest/2.0/metadata/search",
                   {"metadata": [{"type": "CONNECTION"}], "record_size": -1})
    items = data if isinstance(data, list) else data.get("metadata", [])
    return [(it.get("metadata_id"), it.get("metadata_name")) for it in items]


def fetch_databases(cfg: dict, org_id, conn_identifier):
    """Best-effort list of databases on a connection (to auto-fill ts_db). May come back
    empty for connections this session can't introspect (e.g. OAuth/per-principal warehouses);
    in that case type the database name (you can read it from the connection's Edit page)."""
    try:
        c = make_client(cfg, org_id)
        data = c._post("/api/rest/2.0/connection/search",
                       {"connections": [{"identifier": conn_identifier}],
                        "data_warehouse_object_type": "DATABASE", "record_size": -1})
        conns = data if isinstance(data, list) else data.get("connections", data.get("connection", []))
        dbs = []
        for cc in conns:
            for db in (cc.get("data_warehouse_objects") or {}).get("databases", []) or []:
                if db.get("name"):
                    dbs.append(db["name"])
        return dbs
    except Exception:
        return []


def env_values(cfg: dict) -> dict:
    return {
        "TS_HOST": cfg.get("host", ""),
        "TS_USER": cfg.get("user", ""),
        "TS_PASSWORD": cfg.get("password", ""),
        "TS_TOKEN": cfg.get("token", ""),
        "TS_SECRET_KEY": cfg.get("secret", ""),
        "TS_ORG_PRIMARY": str(cfg.get("primary_org", "0")),
        "TS_ORG_SOURCE": str(cfg.get("source_org", "")),
        "TS_RELEASE_TAG": cfg.get("tag", ""),
        "TS_RESOLVE_LOCAL": "1" if cfg.get("resolve_local") else "",
        "GIT_LOCAL_DIR": cfg.get("git_local_dir", ""),
        "GITHUB_REPO": cfg.get("github_repo", ""),
        "GITHUB_TOKEN": cfg.get("github_token", ""),
        "GIT_BRANCH": cfg.get("git_branch", ""),
    }


def write_config(cfg: dict, targets: dict):
    """Apply config to the running process (os.environ, picked up by the pipeline on the
    next call) and persist it to .env + variables/targets.json. Returns the two paths."""
    env = env_values(cfg)
    for k in ENV_KEYS:
        os.environ[k] = env.get(k, "") or ""           # live for this session
    lines = [f"{k}={env[k]}" for k in ENV_KEYS if env.get(k)]  # only persist the set ones
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    TARGETS_PATH.parent.mkdir(parents=True, exist_ok=True)
    TARGETS_PATH.write_text(json.dumps(targets, indent=2), encoding="utf-8")
    return str(ENV_PATH), str(TARGETS_PATH)
