"""Inter-org promotion pipeline (variable-based).

Verbs (one shared engine for the CLI):
  snapshot    export authoring-org objects (or the bundled seed) -> parameterize ->
              commit `release/` (org-agnostic TML) + a variable manifest to Git.
  setup_vars  create the TABLE_MAPPING variables in the Primary org and assign each
              target org its own values (the per-org data binding).
  deploy      read `release/` from Git and import it into a target org. obj_id is the
              cross-org identity; the org's variable values resolve the ${...} tokens.

Variables are managed from the Primary org (TS_ORG_PRIMARY, default 0).
"""
import json
import os
import re
from pathlib import Path

import yaml

import config as C
from services.ts_client import TSClient
from services.param_transform import load_tml, parameterize_bundle, tml_type, retarget_connection
from services import variables as V
from services.gh_creds import github_repo, github_token
from services.git_repo import AreaGitRepo, LocalRepo

ROOT = Path(__file__).resolve().parent.parent
_BASE = os.environ.get("GIT_BASE_PATH", "").strip().replace("\\", "/").strip("/")   # repo subfolder (forward slashes)
RELEASE = f"{_BASE}/release" if _BASE else "release"   # Git folder for the parameterized TML
_MANIFEST = f"{_BASE}/variables/manifest.json" if _BASE else "variables/manifest.json"
_ORDER = {"connection": 0, "table": 1, "view": 1, "sql_view": 1,
          "model": 2, "worksheet": 2, "answer": 3, "liveboard": 4}


def _auth():
    return dict(username=os.environ.get("TS_USER", ""),
                password=os.environ.get("TS_PASSWORD", ""),
                token=os.environ.get("TS_TOKEN", ""),
                secret_key=os.environ.get("TS_SECRET_KEY", ""))


def primary_client() -> TSClient:
    return TSClient(host=os.environ["TS_HOST"],
                    org_id=os.environ.get("TS_ORG_PRIMARY", "0"), **_auth())


def org_client(org) -> TSClient:
    return TSClient(host=os.environ["TS_HOST"], org_id=str(org), **_auth())


def git():
    """GIT_LOCAL_DIR set -> read/write the release in that local folder (any git clone;
    you push/PR yourself). Otherwise commit to the GitHub repo over the API."""
    local = os.environ.get("GIT_LOCAL_DIR")
    if local:
        return LocalRepo(local)
    return AreaGitRepo(github_token(), github_repo())


def _branch():
    """Release branch for the GitHub backend: snapshot commits here and opens a PR into
    main - works with a protected main (which rejects direct pushes). None -> commit
    straight to main, or local-folder mode (GIT_LOCAL_DIR), where branches don't apply."""
    if os.environ.get("GIT_LOCAL_DIR"):
        return None
    return os.environ.get("GIT_BRANCH") or None


def _filename(doc: dict) -> str:
    typ = tml_type(doc) or "object"
    base = doc.get("obj_id") or (doc.get(typ, {}) or {}).get("name", "object")
    base = re.sub(r"[^0-9A-Za-z]+", "_", base.split("__")[0]).strip("_").lower() or "object"
    return f"{base}.{typ}.tml"


def snapshot(source_org=None, tag=None, from_seed=False) -> dict:
    g = git()
    if from_seed:
        docs = [load_tml(p.read_text()) for p in sorted((ROOT / "seed").glob("*.tml"))]
    else:
        ts = org_client(source_org or os.environ.get("TS_ORG_SOURCE", "0"))
        types = ["LOGICAL_TABLE", "LIVEBOARD", "ANSWER"]
        if tag:                                    # scope the release to a tag
            found = ts.search_by_tag(tag, types)
            if not found:
                raise RuntimeError(f"no objects tagged '{tag}' in the source org")
        else:                                      # empty tag -> ALL assets in the org
            found = ts.list_objects(types)
            if not found:
                raise RuntimeError("no objects found in the source org")
        edocs = ts.export_associated_edocs([f["id"] for f in found])
        docs = [load_tml(e) for e in edocs]

    out, used, warns = parameterize_bundle(docs)
    files = {_filename(d): yaml.safe_dump(d, sort_keys=False, width=120) for d in out}
    branch = _branch()
    # On a release branch, reset from main each snapshot for a clean single commit + PR;
    # branch=None commits straight to main (unprotected repos / local mode), as before.
    sha = g.commit_area(RELEASE, files, message="snapshot parameterized release",
                        branch=branch, reset_from=("main" if branch else None))
    g.put_file(_MANIFEST, json.dumps(sorted(used), indent=2),
               "chore: variable manifest", branch=branch)
    # prune stale files left by a previous (different) snapshot, so a release fully replaces
    pruned = [fn for fn in list(g.read_area(RELEASE, ref=branch)) if fn.endswith(".tml") and fn not in files
              and g.delete_file(f"{RELEASE}/{fn}", "chore: drop stale release file", branch=branch)]
    pr_url = None
    if branch:                                  # open (or reuse) a PR into main for review/merge
        try:
            pr_url = g.open_pr(branch, "ThoughtSpot inter-org release",
                               "Parameterized `release/` snapshot. Review and merge to record it on `main`.")
        except Exception as e:
            warns.append(f"committed to '{branch}', but no PR opened: {str(e)[:140]}")
    return {"files": list(files), "variables": sorted(used), "warnings": warns,
            "sha": sha, "pruned": pruned, "branch": branch, "pr_url": pr_url}


def setup_vars(values_by_org: dict) -> dict:
    """values_by_org: {org_identifier: {var_name: value}}. Creates the TABLE_MAPPING
    variables in the Primary org (idempotent) and assigns each org its values."""
    pc = primary_client()
    created = [v for v in C.TABLE_MAPPING_VARS if V.ensure_variable(pc, v, "TABLE_MAPPING")]
    assigned = []
    for org, vals in values_by_org.items():
        for var, val in vals.items():
            V.set_org_value(pc, var, str(org), [val], operation="REPLACE")
            assigned.append({"org": org, "variable": var, "value": val})
    return {"created": created, "assigned": assigned}


def _targets() -> dict:
    """Per-target config from variables/targets.json: {name: {org_id, connection, values}}."""
    p = ROOT / "variables" / "targets.json"
    raw = json.loads(p.read_text()) if p.exists() else {}
    return {k: v for k, v in raw.items() if not k.startswith("_")}


def deploy(target: str, validate_only: bool = False) -> dict:
    """Deploy release/ into a target org, remapping the connection to that org's.

    `target` is a key in variables/targets.json ({org_id, connection, ...}). Order:
    tables first; VALIDATE_ONLY runs first and a failed validate BLOCKS the import.
    Never deletes. obj_id alignment across orgs is a one-time setup step (align_obj_id),
    not part of deploy, because a physical-match import keeps the existing obj_id.
    """
    cfg = _targets().get(target)
    if not cfg:
        raise RuntimeError(f"target '{target}' not in variables/targets.json")
    ts = org_client(cfg["org_id"])
    files = {k: v for k, v in git().read_area(RELEASE, ref=_branch()).items() if k.endswith(".tml")}
    if not files:
        raise RuntimeError("release/ is empty in Git — run snapshot first")
    docs = [load_tml(v) for v in files.values()]
    for d in docs:                                   # remap connection to the target org's
        if cfg.get("connection"):
            retarget_connection(d, cfg["connection"])
    docs.sort(key=lambda d: _ORDER.get(tml_type(d), 9))   # tables -> models -> liveboards
    strings = [json.dumps(d) for d in docs]
    # TS_RESOLVE_LOCAL: bake the target org's values into the ${var} tokens here, instead of
    # relying on the server-side Variable Store. Use when Variables aren't enabled on the cluster.
    if os.environ.get("TS_RESOLVE_LOCAL"):
        for var, val in (cfg.get("values") or {}).items():
            strings = [s.replace("${" + var + "}", val) for s in strings]
    validate = ts.import_tml(strings, policy="VALIDATE_ONLY")
    errs = [r for r in validate if r["status"] != "OK"]
    if validate_only or errs:                        # gate: never import on a failed validate
        return {"target": target, "org": str(cfg["org_id"]), "validate": validate,
                "imported": None, "blocked": bool(errs)}
    results = ts.import_tml(strings, policy="ALL_OR_NONE")
    return {"target": target, "org": str(cfg["org_id"]), "validate": validate,
            "imported": results, "blocked": False}


def align_obj_id(org, current_obj_id: str, new_obj_id: str) -> dict:
    """Set an object's obj_id in a given org (update-obj-id). Needed to make obj_ids
    consistent across orgs, since a physical-match import keeps the existing obj_id."""
    return org_client(org).set_obj_id(current_obj_id, new_obj_id)
