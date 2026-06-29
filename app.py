"""
Inter-Org Promotion - Streamlit UI.

Configure everything in the Setup tab (host, auth, source org, git store, targets) with
live org/connection discovery - no .env or targets.json hand-editing. Then run the flow:
Snapshot -> Variables -> Deploy.

Run:  streamlit run app.py
"""
import os

from dotenv import load_dotenv
import streamlit as st

from services import pipeline, ui_setup

load_dotenv()

st.set_page_config(page_title="Inter-Org Promotion", layout="wide")
st.title("ThoughtSpot Inter-Org Promotion")


def _store_caption() -> str:
    if os.environ.get("GIT_LOCAL_DIR"):
        return f"Git store: local folder `{os.environ['GIT_LOCAL_DIR']}`"
    if os.environ.get("GITHUB_REPO"):
        return f"Git store: github.com/{os.environ['GITHUB_REPO']}"
    return "Git store: not configured yet - set it in the **Setup** tab"


st.caption(_store_caption() + "  ·  one parameterized `release/`; each org's values resolve "
           "`${ts_db}`/`${ts_schema}`; obj_id is the cross-org identity.")

tabs = st.tabs(["0 · Setup", "1 · Snapshot", "2 · Variables", "3 · Deploy", "Repo state"])

# ── 0 · setup ──────────────────────────────────────────────────────────────────────
with tabs[0]:
    st.subheader("Configure everything here - no file editing")
    ss = st.session_state

    st.markdown("**ThoughtSpot connection**")
    host = st.text_input("Host", value=os.environ.get("TS_HOST", ""),
                         placeholder="https://your-instance.thoughtspot.cloud")
    auth = st.radio("Auth method",
                    ["Secret key (trusted auth)", "Bearer token", "Username + password"],
                    horizontal=True)
    user = secret = token = password = ""
    if auth.startswith("Secret"):
        user = st.text_input("Username (token is minted for this user)", value=os.environ.get("TS_USER", ""))
        secret = st.text_input("Trusted-auth secret key", value=os.environ.get("TS_SECRET_KEY", ""), type="password")
    elif auth.startswith("Bearer"):
        token = st.text_input("Bearer token", value=os.environ.get("TS_TOKEN", ""), type="password")
    else:
        user = st.text_input("Username", value=os.environ.get("TS_USER", ""))
        password = st.text_input("Password", value=os.environ.get("TS_PASSWORD", ""), type="password")
    primary_org = st.text_input("Primary org id (variables are managed here)",
                                value=os.environ.get("TS_ORG_PRIMARY", "0"))

    def _cfg() -> dict:
        return {"host": host.rstrip("/"), "user": user, "secret": secret, "token": token,
                "password": password, "primary_org": primary_org,
                "source_org": ss.get("source_org", ""), "tag": ss.get("tag", ""),
                "resolve_local": ss.get("resolve_local", True),
                "git_local_dir": ss.get("git_local_dir", ""),
                "github_repo": ss.get("github_repo", ""), "github_token": ss.get("github_token", ""),
                "git_branch": ss.get("git_branch", "")}

    if st.button("Test connection & load orgs"):
        try:
            ss["orgs"] = ui_setup.list_orgs(_cfg())
            st.success(f"Connected. Loaded {len(ss['orgs'])} orgs.")
        except Exception as e:
            ss.pop("orgs", None)
            st.error(f"Connection failed - {type(e).__name__}: {str(e)[:300]}")

    if ss.get("orgs"):
        orgs = ss["orgs"]
        id2name = {i: n for i, n in orgs}
        ids = [i for i, _ in orgs]

        st.markdown("**Source org** (snapshot FROM)")
        ss["source_org"] = st.selectbox(
            "Source org", ids, format_func=lambda i: f"{id2name.get(i, i)}  ({i})",
            index=ids.index(ss["source_org"]) if ss.get("source_org") in ids else 0)

        st.markdown("**Git store**")
        gitmode = st.radio("Where to store the release", ["Local folder", "GitHub repo"], horizontal=True)
        if gitmode == "Local folder":
            ss["git_local_dir"] = st.text_input("Local folder path (any folder, e.g. inside a git clone)",
                                                value=ss.get("git_local_dir", "") or os.environ.get("GIT_LOCAL_DIR", ""))
            ss["github_repo"] = ss["github_token"] = ss["git_branch"] = ""
        else:
            ss["github_repo"] = st.text_input("GitHub repo (owner/name)",
                                              value=ss.get("github_repo", "") or os.environ.get("GITHUB_REPO", ""))
            ss["github_token"] = st.text_input("GitHub token (repo scope)",
                                               value=ss.get("github_token", "") or os.environ.get("GITHUB_TOKEN", ""), type="password")
            ss["git_branch"] = st.text_input(
                "Release branch - commits here and opens a PR into main (use when main is protected). "
                "Blank = commit straight to main.",
                value=ss.get("git_branch", "") or os.environ.get("GIT_BRANCH", "ts-release"))
            ss["git_local_dir"] = ""

        st.markdown("**Options**")
        ss["resolve_local"] = st.checkbox(
            "Resolve variables locally (use when the Variables feature isn't enabled on the cluster)",
            value=ss.get("resolve_local", True))
        ss["tag"] = st.text_input("Release tag (empty = ALL objects in the source org)",
                                  value=ss.get("tag", "") or os.environ.get("TS_RELEASE_TAG", ""))

        st.markdown("**Targets** (deploy TO) - add one entry per target org")
        ss.setdefault("targets", {})
        c1, c2 = st.columns([3, 2])
        with c1:
            tgt_org = st.selectbox("Target org", ids, format_func=lambda i: f"{id2name.get(i, i)}  ({i})", key="tgt_org")
        with c2:
            if st.button("Load connections for this org"):
                try:
                    ss.setdefault("conns", {})[tgt_org] = ui_setup.list_connections(_cfg(), tgt_org)
                except Exception as e:
                    st.error(f"Couldn't list connections - {str(e)[:200]}")
        conns = ss.get("conns", {}).get(tgt_org, [])
        if conns:
            conn_name = st.selectbox("Connection (in the target org)", [n for _, n in conns])
            conn_id = next((i for i, n in conns if n == conn_name), None)
        else:
            conn_name = st.text_input("Connection name (in the target org)", key="conn_manual")
            conn_id = None
        d1, d2, d3 = st.columns([2, 2, 1])
        with d1:
            ts_db = st.text_input("Database (ts_db)", key="ts_db_in")
        with d2:
            ts_schema = st.text_input("Schema (ts_schema)", key="ts_schema_in")
        with d3:
            if st.button("Fetch dbs"):
                dbs = ui_setup.fetch_databases(_cfg(), tgt_org, conn_id) if conn_id else []
                st.info("DBs: " + (", ".join(dbs) if dbs else "(none returned - type it; read it off the connection's Edit page)"))
        if st.button("Add / update target"):
            key = (id2name.get(tgt_org, str(tgt_org)) or str(tgt_org)).lower().replace(" ", "_")
            ss["targets"][key] = {"name": id2name.get(tgt_org, str(tgt_org)), "org_id": str(tgt_org),
                                  "connection": conn_name, "values": {"ts_db": ts_db, "ts_schema": ts_schema}}
            st.success(f"Target '{key}' set.")
        if ss["targets"]:
            st.table([{"key": k, "org_id": v["org_id"], "connection": v["connection"],
                       "ts_db": v["values"]["ts_db"], "ts_schema": v["values"]["ts_schema"]}
                      for k, v in ss["targets"].items()])
            if st.button("Clear targets"):
                ss["targets"] = {}

        st.divider()
        if st.button("Save configuration", type="primary"):
            try:
                p1, p2 = ui_setup.write_config(_cfg(), ss.get("targets", {}))
                st.success(f"Saved and live for this session (also written to {p1} and {p2}). "
                           "Use the Snapshot / Variables / Deploy tabs now.")
            except Exception as e:
                st.error(f"Save failed - {str(e)[:300]}")
    else:
        st.info("Enter host + auth, then click **Test connection & load orgs**.")

# ── 1 · snapshot ───────────────────────────────────────────────────────────────────
with tabs[1]:
    st.subheader("Snapshot a release into the Git store")
    st.write("Export the source org's objects (or the bundled seed), parameterize "
             "(db/schema → `${...}`, keep obj_id, strip guids), and write `release/`.")
    from_seed = st.checkbox("Use bundled seed (demo)", value=False)
    src = "" if from_seed else st.text_input("Source org id", value=os.environ.get("TS_ORG_SOURCE", ""))
    tag = st.text_input("Release tag (empty = ALL objects)", value=os.environ.get("TS_RELEASE_TAG", ""))
    if st.button("Snapshot", type="primary"):
        with st.spinner("Parameterizing + writing release…"):
            r = pipeline.snapshot(source_org=src or None, tag=tag or None, from_seed=from_seed)
        st.success(f"Wrote {len(r['files'])} file(s) to `release/` @ `{r['sha'][:8]}`")
        st.write("variables referenced:", r["variables"])
        st.table([{"file": f} for f in r["files"]])
        if r["warnings"]:
            st.warning(r["warnings"])

# ── 2 · variables ──────────────────────────────────────────────────────────────────
with tabs[2]:
    st.subheader("Create variables + assign per-org values")
    st.write("Creates the TABLE_MAPPING variables in the **Primary** org and assigns each "
             "target org its values from the configured targets.")
    targets = pipeline._targets()
    if not targets:
        st.warning("No targets configured - add them in the Setup tab.")
    else:
        st.table([{"name": v.get("name", k), "org_id": v.get("org_id"),
                   "connection": v.get("connection"), "values": v.get("values")} for k, v in targets.items()])
        st.caption("Skip this step if you resolve variables locally (Setup → Resolve variables locally).")
        if st.button("Create + assign", type="primary"):
            values_by_org = {c["org_id"]: c["values"] for c in targets.values() if c.get("values")}
            with st.spinner("Setting up variables…"):
                r = pipeline.setup_vars(values_by_org)
            st.success(f"created {r['created'] or '(all existed)'}; {len(r['assigned'])} value(s) assigned")
            st.table(r["assigned"])

# ── 3 · deploy ─────────────────────────────────────────────────────────────────────
with tabs[3]:
    st.subheader("Deploy release to a target org")
    st.write("Reads `release/`, remaps the connection to the target org's, imports "
             "(tables first). VALIDATE_ONLY runs first and blocks the import if it fails. Never deletes.")
    targets = pipeline._targets()
    if not targets:
        st.warning("No targets configured - add them in the Setup tab.")
    else:
        tgt = st.selectbox("Target", list(targets.keys()),
                           format_func=lambda k: f"{targets[k].get('name', k)}  ({k})")
        only = st.checkbox("Validate only (no import)", value=True)
        if st.button(f"{'Validate' if only else 'Deploy'} → {tgt}", type="primary"):
            with st.spinner("Validating + deploying…"):
                r = pipeline.deploy(tgt, validate_only=only)
            st.write(f"**Target:** `{r['target']}` (org {r['org']})")
            st.write("**Validate:**")
            st.table([{"status": v["status"], "type": v["type"], "name": v["name"],
                       "error": v.get("error") or ""} for v in r["validate"]])
            if r.get("blocked"):
                st.error("Validate failed - nothing imported.")
            elif r.get("imported"):
                st.write("**Import:**")
                st.table([{"status": v["status"], "type": v["type"], "name": v["name"],
                           "new_id": v.get("new_id"), "error": v.get("error") or ""} for v in r["imported"]])
                st.success(f"Deployed to `{tgt}`. Re-run is idempotent.")

# ── repo state ─────────────────────────────────────────────────────────────────────
with tabs[4]:
    st.subheader("Git release + audit trail")
    if st.button("Refresh"):
        st.session_state.pop("io_repo", None)
    if "io_repo" not in st.session_state:
        try:
            g = pipeline.git()
            st.session_state.io_repo = {
                "files": sorted(f for f in g.read_area(pipeline.RELEASE) if f.endswith(".tml")),
                "commits": [(c.sha[:8], c.commit.message.splitlines()[0])
                            for c in g._repo.get_commits(sha="main")[:10]],
            }
        except Exception as e:
            st.session_state.io_repo = {"files": [], "commits": [], "error": str(e)[:200]}
    state = st.session_state.io_repo
    if state.get("error"):
        st.warning(f"Could not read the git store: {state['error']} (configure it in Setup).")
    st.markdown("**`release/` (parameterized, org-agnostic)**")
    for f in state["files"]:
        st.write(f"`{f}`")
    if state["commits"]:
        st.markdown("**Commit history (`main`)**")
        st.table([{"sha": s, "message": m} for s, m in state["commits"]])
