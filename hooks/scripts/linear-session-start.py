#!/usr/bin/env python3
"""linear-session-start.py — SessionStart hook for Linear Sync

Single Python process replacing 15+ bash/python3 subprocesses.
State read once, written at most once (atomically).
Network calls parallelized. Optional mtime-based cache.
"""

import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ── constants ──────────────────────────────────────────────────────
STATE_DIR = Path.home() / ".claude" / "linear-sync"
STATE_FILE = Path(os.environ["STATE_FILE_OVERRIDE"]) if "STATE_FILE_OVERRIDE" in os.environ else STATE_DIR / "state.json"
CACHE_DIR = STATE_DIR / ".cache"
MCP_JSON = Path.home() / ".claude" / "mcp.json"
DIGEST_INTERVAL = timedelta(minutes=0)  # always fetch fresh digest on session start
SUBPROCESS_TIMEOUT = 10  # leave headroom within 15s hook timeout


# ── output helpers ─────────────────────────────────────────────────
def emit(context):
    """Print hook JSON to stdout and exit."""
    try:
        payload = json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": context,
            }
        })
    except (TypeError, ValueError):
        payload = json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": "[LINEAR-ERROR] Failed to serialize hook context.",
            }
        })
    print(payload)
    sys.exit(0)


def atomic_write_json(path, data):
    """Write JSON to *path* atomically (tmp + rename)."""
    tmp = f"{path}.tmp.{os.getpid()}"
    try:
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.rename(tmp, str(path))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass


# ── git helpers ────────────────────────────────────────────────────
def resolve_git_context(cwd):
    """Return (git_top, repo_name, remote_url, github_org) or None."""
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, cwd=cwd, timeout=5,
        )
        git_top = r.stdout.strip()
    except Exception:
        git_top = ""
    if not git_top:
        if os.path.isdir(os.path.join(cwd, ".git")):
            git_top = cwd
        else:
            return None

    repo_name = os.path.basename(git_top)
    if not repo_name:
        return None

    try:
        remote_url = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True, text=True, cwd=git_top, timeout=5,
        ).stdout.strip()
    except Exception:
        remote_url = ""

    github_org = ""
    if remote_url:
        m = re.search(r"github\.com[:/]([^/]+)/", remote_url)
        if m:
            github_org = m.group(1)

    return (git_top, repo_name, remote_url, github_org)


def get_current_branch(git_top):
    try:
        return subprocess.run(
            ["git", "branch", "--show-current"],
            capture_output=True, text=True, cwd=git_top, timeout=5,
        ).stdout.strip()
    except Exception:
        return ""


# ── state / config loading ─────────────────────────────────────────
def load_state():
    """Load state.json once. Returns (dict, has_workspaces)."""
    if not STATE_FILE.exists():
        return {}, False
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
        return data, bool(data.get("workspaces"))
    except Exception:
        return {}, False


def load_repo_config(git_top):
    """Load .claude/linear-sync.json. Returns (config_dict|None, path|None, warning)."""
    path = os.path.join(git_top, ".claude", "linear-sync.json")
    if not os.path.isfile(path):
        return None, None, ""
    try:
        with open(path) as f:
            cfg = json.load(f)
        ws = cfg.get("workspace", "")
        if not ws:
            return None, path, (
                "[LINEAR-WARNING] .claude/linear-sync.json exists but is malformed."
                " Falling back to local config.\n"
            )
        return cfg, path, ""
    except Exception:
        return None, path, (
            "[LINEAR-WARNING] .claude/linear-sync.json exists but is malformed."
            " Falling back to local config.\n"
        )


# ── MCP resolution ────────────────────────────────────────────────
def resolve_mcp_server(state, ws_id):
    """Resolve MCP server. May mutate *state* in memory (healing).
    Returns (status, server_name, servers_list).
    status: ok | healed | configured_missing | ambiguous | none
    """
    ws = state.get("workspaces", {}).get(ws_id, {})
    configured = ws.get("mcp_server", "")

    mcp_servers = {}
    try:
        with open(MCP_JSON) as f:
            mcp_servers = json.load(f).get("mcpServers", {})
    except Exception:
        pass

    linear_servers = [
        name for name, cfg in mcp_servers.items()
        if any("LINEAR" in k.upper() for k in cfg.get("env", {}))
    ]

    if configured:
        if configured in mcp_servers:
            return ("ok", configured, linear_servers)
        return ("configured_missing", configured, linear_servers)

    if not linear_servers:
        return ("none", "", [])

    if len(linear_servers) == 1:
        server = linear_servers[0]
        state.setdefault("workspaces", {}).setdefault(ws_id, {})["mcp_server"] = server
        return ("healed", server, linear_servers)

    # Multiple — try matching by workspace ID
    ws_norm = ws_id.replace("-", "").replace("_", "").lower()
    matched = [
        n for n in linear_servers
        if ws_norm in n.replace("-", "").replace("_", "").lower()
    ]
    if len(matched) == 1:
        server = matched[0]
        state.setdefault("workspaces", {}).setdefault(ws_id, {})["mcp_server"] = server
        return ("healed", server, linear_servers)

    return ("ambiguous", "", linear_servers)


# ── parallel I/O workers ──────────────────────────────────────────
def _fetch_issue_title(scripts_dir, mcp_server, issue_id):
    api = os.path.join(scripts_dir, "linear-api.sh")
    if not os.path.isfile(api):
        return ""
    try:
        cmd = ["bash", api]
        if mcp_server:
            cmd.append(mcp_server)
        cmd.append(f'query {{ issue(id: "{issue_id}") {{ title }} }}')
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT)
        return json.loads(r.stdout)["data"]["issue"]["title"]
    except Exception:
        return ""


def _detect_stale_branches(git_top, current_branch):
    try:
        r = subprocess.run(
            ["git", "for-each-ref",
             "--format=%(refname:short) %(committerdate:iso-strict)",
             "refs/heads/"],
            capture_output=True, text=True, cwd=git_top, timeout=SUBPROCESS_TIMEOUT,
        )
        cutoff = datetime.now(timezone.utc) - timedelta(days=5)
        stale = []
        for line in r.stdout.strip().split("\n"):
            if not line.strip():
                continue
            parts = line.rsplit(" ", 1)
            if len(parts) != 2:
                continue
            branch, date_str = parts
            if branch in ("main", "master", "develop") or branch == current_branch:
                continue
            try:
                cd = datetime.fromisoformat(date_str)
                if cd < cutoff:
                    days = (datetime.now(timezone.utc) - cd).days
                    stale.append(f"{branch} ({days}d ago)")
            except (ValueError, TypeError):
                pass
        return ", ".join(stale[:3]) if stale else ""
    except Exception:
        return ""


def _fetch_digest(scripts_dir, mcp_server, project):
    api = os.path.join(scripts_dir, "linear-api.sh")
    if not os.path.isfile(api):
        return ""
    query = (
        "query { viewer { assignedIssues(filter: { "
        f'project: {{ name: {{ eq: "{project}" }} }}, '
        'state: { type: { in: ["started", "unstarted"] } } '
        "}, first: 10) { nodes { identifier title state { name } priority } } } }"
    )
    try:
        cmd = ["bash", api]
        if mcp_server:
            cmd.append(mcp_server)
        cmd.append(query)
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT)
        nodes = (
            json.loads(r.stdout)
            .get("data", {}).get("viewer", {})
            .get("assignedIssues", {}).get("nodes", [])
        )
        if not nodes:
            return "No pending items."
        return "\n".join(
            f'{i["identifier"]}: {i["title"]} [{i["state"]["name"]}]' for i in nodes
        )
    except Exception:
        return ""


# ── cache ──────────────────────────────────────────────────────────
def try_load_cache(repo_name, repo_config_path):
    """Return cached context string if valid, else None."""
    cache_path = CACHE_DIR / f"{repo_name}.json"
    if not cache_path.exists():
        return None
    try:
        with open(cache_path) as f:
            cache = json.load(f)
        stored = cache.get("mtimes", {})

        if STATE_FILE.exists():
            if os.path.getmtime(STATE_FILE) != stored.get("state_json"):
                return None
        elif stored.get("state_json") is not None:
            return None

        if repo_config_path and os.path.isfile(repo_config_path):
            if os.path.getmtime(repo_config_path) != stored.get("repo_config"):
                return None
        elif stored.get("repo_config") is not None:
            return None

        if MCP_JSON.exists():
            if os.path.getmtime(MCP_JSON) != stored.get("mcp_json"):
                return None
        elif stored.get("mcp_json") is not None:
            return None

        exp = cache.get("digest_expires_at", "")
        if exp:
            if datetime.now(timezone.utc) > datetime.fromisoformat(exp):
                return None

        return cache.get("context")
    except Exception:
        return None


def save_cache(repo_name, context, repo_config_path, digest_expires_at=None):
    """Save cache atomically."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    mtimes = {}
    if STATE_FILE.exists():
        mtimes["state_json"] = os.path.getmtime(STATE_FILE)
    if repo_config_path and os.path.isfile(repo_config_path):
        mtimes["repo_config"] = os.path.getmtime(repo_config_path)
    if MCP_JSON.exists():
        mtimes["mcp_json"] = os.path.getmtime(MCP_JSON)
    data = {"context": context, "mtimes": mtimes}
    if digest_expires_at:
        data["digest_expires_at"] = digest_expires_at
    atomic_write_json(CACHE_DIR / f"{repo_name}.json", data)


# ── main ───────────────────────────────────────────────────────────
def main():
    hook_dir = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.abspath(__file__))
    scripts_dir = os.path.realpath(os.path.join(hook_dir, "..", "..", "scripts"))

    # Parse stdin
    cwd = ""
    try:
        inp = json.load(sys.stdin)
        cwd = inp.get("cwd") or inp.get("sessionState", {}).get("cwd", "")
    except Exception:
        pass
    if not cwd:
        sys.exit(0)

    # Git context
    gc = resolve_git_context(cwd)
    if gc is None:
        sys.exit(0)
    git_top, repo_name, _remote_url, github_org = gc

    # Load state (ONE read)
    state, has_workspaces = load_state()

    # Load repo config
    repo_config, repo_config_path, config_warning = load_repo_config(git_top)

    # Local entry from state
    local_entry = state.get("repos", {}).get(repo_name)

    # Single opt-out check
    if local_entry and local_entry.get("workspace") == "none":
        sys.exit(0)

    # ── CONFIG SANITY CHECKS ──────────────────────────────────────
    warnings = []
    if repo_config is not None:
        # Detect legacy key format (teamId/teamKey/projectId/labelId)
        legacy_keys = {"teamId", "teamKey", "projectId", "labelId", "github_repo"}
        found_legacy = legacy_keys & set(repo_config.keys())
        if found_legacy:
            warnings.append(
                f"Legacy config keys detected: {', '.join(sorted(found_legacy))}."
                f" Run the setup wizard to migrate to the current schema"
                f" (team/project/label)."
            )
        # Detect github_org drift (actual remote vs config)
        if github_org and repo_config.get("github_org"):
            cfg_org = repo_config["github_org"]
            if cfg_org != github_org:
                warnings.append(
                    f"github_org mismatch: config says '{cfg_org}' but git"
                    f" remote points to '{github_org}'. Repo may have been"
                    f" transferred. Update .claude/linear-sync.json."
                )
    if warnings:
        warning_text = " | ".join(warnings)
        config_warning = (
            f"{config_warning} | {warning_text}\n" if config_warning
            else f"{warning_text}\n"
        )

    # Resolve effective entry (merge repo config if present)
    effective_entry = None
    if repo_config is not None:
        ws = repo_config["workspace"]
        if ws in state.get("workspaces", {}):
            effective_entry = {
                "workspace": ws,
                "project": repo_config.get("project", ""),
                "team": repo_config.get("team", ""),
                "label": repo_config.get("label", ""),
            }
            if local_entry:
                for fld in ("last_issue", "last_issue_title", "last_digest_at"):
                    if local_entry.get(fld):
                        effective_entry[fld] = local_entry[fld]
            has_workspaces = True
        else:
            emit(
                f'[LINEAR-SETUP] Repo "{repo_name}" has a committed'
                f' .claude/linear-sync.json for workspace "{ws}", but your local'
                f" Linear credentials are not set up for this workspace."
                f' Use AskUserQuestion: "This repo uses the {ws} workspace.'
                f" Set up your API key? / This repo doesn't use Linear\"."
                f" If yes, use the linear-sync subagent (foreground) to add the"
                f" workspace entry to the local state file. No project/team/label"
                f" questions needed — they come from the repo config."
            )

    if effective_entry is None and local_entry is not None:
        effective_entry = local_entry

    # ── LINKED REPO ────────────────────────────────────────────────
    if (
        effective_entry is not None
        and effective_entry.get("workspace")
        and effective_entry.get("workspace") != "none"
    ):
        _handle_linked_repo(
            state, effective_entry, repo_name, git_top, scripts_dir,
            repo_config_path, config_warning, has_workspaces,
        )

    # ── UNREGISTERED REPO ──────────────────────────────────────────
    _handle_unregistered_repo(
        state, repo_name, git_top, github_org, has_workspaces,
    )


def _handle_linked_repo(
    state, entry, repo_name, git_top, scripts_dir,
    repo_config_path, config_warning, has_workspaces,
):
    ws_id = entry["workspace"]
    ws_data = state.get("workspaces", {}).get(ws_id)

    if ws_data is None:
        emit(  # emit() calls sys.exit(0)
            f'[LINEAR-SETUP] Repo "{repo_name}" references a workspace that no'
            f" longer exists in the state file. Use AskUserQuestion to ask the"
            f' dev: "The Linear workspace for this repo is missing. Reconfigure?'
            f' / Opt out of Linear for this repo?" If reconfigure, use the'
            f" linear-sync subagent (foreground) to walk through setup. If opt"
            f" out, use the subagent to write workspace:none."
        )
        return  # unreachable, but guards against future refactors

    ws_name = ws_data.get("name", ws_id)
    project = entry.get("project", "")
    team = entry.get("team", ws_data.get("default_team", ""))
    label = entry.get("label", "")
    last_issue = entry.get("last_issue", "")
    last_issue_title = entry.get("last_issue_title", "")

    # Cache fast-path
    cached = try_load_cache(repo_name, repo_config_path)
    if cached is not None:
        emit(cached)
        return

    # MCP resolution (may mutate state in memory)
    state_mutated = False
    mcp_status, mcp_server, mcp_servers = resolve_mcp_server(state, ws_id)
    if mcp_status == "healed":
        state_mutated = True

    if mcp_status == "ambiguous":
        servers_str = ",".join(mcp_servers)
        emit(
            f'[LINEAR-MCP-AMBIGUOUS] Workspace "{ws_id}" has no mcp_server'
            f" configured and multiple Linear MCP servers exist ({servers_str})."
            f" Cannot safely pick one — the wrong server would route to the"
            f" wrong workspace. Use the linear-sync subagent (foreground) to ask"
            f" the dev which server to use for this workspace: AskUserQuestion"
            f" with the server names as options. Then persist the choice to the"
            f" state file."
        )
        return

    # Parallel I/O
    current_branch = get_current_branch(git_top)
    should_digest = True
    last_digest_at = entry.get("last_digest_at", "")
    if last_digest_at:
        try:
            last_dt = datetime.fromisoformat(last_digest_at)
            if datetime.now(timezone.utc) - last_dt <= DIGEST_INTERVAL:
                should_digest = False
        except (ValueError, TypeError):
            pass

    need_title = bool(last_issue and not last_issue_title)
    futures = {}
    with ThreadPoolExecutor(max_workers=3) as pool:
        if need_title:
            futures["title"] = pool.submit(_fetch_issue_title, scripts_dir, mcp_server, last_issue)
        futures["stale"] = pool.submit(_detect_stale_branches, git_top, current_branch)
        if should_digest:
            futures["digest"] = pool.submit(_fetch_digest, scripts_dir, mcp_server, project)

    fetched_title = futures["title"].result() if "title" in futures else ""
    stale_branches = futures["stale"].result()
    digest_text = futures["digest"].result() if "digest" in futures else ""

    # Apply title heal
    if need_title and fetched_title:
        last_issue_title = fetched_title
        state.setdefault("repos", {}).setdefault(repo_name, {})["last_issue_title"] = fetched_title
        state_mutated = True

    # Apply digest timestamp
    digest_expires_at = None
    if should_digest:
        now_iso = datetime.now(timezone.utc).isoformat()
        state.setdefault("repos", {}).setdefault(repo_name, {})["last_digest_at"] = now_iso
        state_mutated = True
        digest_expires_at = (datetime.now(timezone.utc) + DIGEST_INTERVAL).isoformat()

    # Build context string
    header = (
        f"[Linear/{ws_id}] Repo: {repo_name} | Workspace: {ws_name}"
        f" | Project: {project} | Team: {team} | Label: {label}"
        f" | Branch format: {team}-<number>-slug"
        f" | Commit format: {team}-<number>: description"
        f" | mcp_server: {mcp_server} | scripts_dir: {scripts_dir}"
    )

    if last_issue:
        if last_issue_title:
            header += f" | last_issue: {last_issue} ({last_issue_title})"
        else:
            header += f" | last_issue: {last_issue}"

    parts = [header]

    if stale_branches:
        parts.append(
            f"[STALE-BRANCHES] Stale local branches (no commits in 5+ days): {stale_branches}"
        )

    if mcp_status in ("configured_missing", "none"):
        display_server = mcp_server or "linear"
        parts.append(
            f'[LINEAR-MCP-MISSING] The Linear MCP server "{display_server}" is not'
            f" configured in ~/.claude/mcp.json. Linear MCP tools are required"
            f" for this plugin. Tell the user to install it by adding this to"
            f' ~/.claude/mcp.json under "mcpServers":'
            f' {{"{display_server}": {{"type": "stdio", "command": "npx",'
            f' "args": ["-y", "@anthropic/linear-mcp-server"],'
            f' "env": {{"LINEAR_API_KEY": "${{LINEAR_API_KEY}}"}}}}}}'
            f" — they need a Linear Personal API Key"
            f" (Settings > API > Personal API keys). After adding, restart"
            f" Claude Code. Until installed, the plugin will fall back to"
            f" linear-api.sh for API calls."
        )

    if should_digest and digest_text:
        parts.append(f"[LINEAR-DIGEST] Your active issues:\n{digest_text}")

    parts.append(
        f"IMPORTANT: Invoke the /linear-sync skill now to handle this session"
        f" kickoff for {repo_name}."
    )

    context = config_warning + "\n".join(parts)

    # Save state if mutated (at most once, atomic)
    if state_mutated:
        atomic_write_json(STATE_FILE, state)

    # Save cache
    save_cache(repo_name, context, repo_config_path, digest_expires_at)

    emit(context)


def _handle_unregistered_repo(state, repo_name, git_top, github_org, has_workspaces):
    # Template check
    template_ctx = ""
    template_path = os.path.join(git_top, ".linear-sync-template.json")
    if os.path.isfile(template_path):
        try:
            with open(template_path) as f:
                t = json.load(f)
            tp = [f"{k}={t[k]}" for k in ("workspace", "project", "team", "label") if k in t]
            if tp:
                template_ctx = (
                    "[LINEAR-TEMPLATE] Found .linear-sync-template.json with"
                    " defaults: " + ", ".join(tp) + ". Pre-fill setup wizard"
                    " with these values and ask the dev to confirm."
                )
        except Exception:
            pass

    def _with_template(ctx):
        return ctx + "\n" + template_ctx if template_ctx else ctx

    # Org match
    org_match = None
    if github_org:
        ws_id = state.get("github_org_defaults", {}).get(github_org, "")
        if ws_id:
            ws = state.get("workspaces", {}).get(ws_id)
            if ws:
                org_match = {"workspace_id": ws_id, "name": ws.get("name", ws_id)}

    if has_workspaces and org_match:
        match_name = org_match["name"]
        match_id = org_match["workspace_id"]
        emit(_with_template(
            f'[LINEAR-SETUP] New repo "{repo_name}" (org: {github_org}) matches'
            f' workspace "{match_name}". Use AskUserQuestion to confirm: "Link'
            f" {repo_name} to {match_name} workspace? / Choose a different"
            f" workspace / This repo doesn't use Linear\". If confirmed, use the"
            f" linear-sync subagent (foreground) to fetch projects and teams from"
            f' Linear MCP for workspace "{match_id}", present projects as'
            f' AskUserQuestion choices with "Create a new project" as the last'
            f' option. If they pick "Create a new project", ask for a name, then'
            f" delegate to the subagent to create it in Linear before linking."
            f' Ask for a label (suggest "repo:{repo_name}"), and persist to'
            f" state file."
        ))

    elif has_workspaces:
        workspace_names = ", ".join(
            ws.get("name", wid) for wid, ws in state.get("workspaces", {}).items()
        )
        org_str = github_org or "unknown"
        emit(_with_template(
            f'[LINEAR-SETUP] New repo "{repo_name}" (org: {org_str}). Known'
            f" workspaces: {workspace_names}. Use AskUserQuestion to ask:"
            f' "Which workspace for {repo_name}? / {workspace_names} / Set up a'
            f" new workspace / This repo doesn't use Linear\". If an existing"
            f' workspace is picked, ask if org "{github_org}" should default to'
            f" it. Then use the linear-sync subagent (foreground) to fetch"
            f" projects/teams, present projects as choices with \"Create a new"
            f' project" as the last option. If they pick "Create a new project",'
            f" ask for a name, then delegate to the subagent to create it in"
            f' Linear before linking. Ask for a label (suggest "repo:{repo_name}"),'
            f" and persist."
        ))

    else:
        org_str = github_org or "unknown"
        emit(_with_template(
            f'[LINEAR-SETUP] First-time setup. Repo: "{repo_name}" (org:'
            f' {org_str}). Use AskUserQuestion: "Does this repo connect to'
            f' Linear? Yes / No". If yes, use the linear-sync subagent'
            f" (foreground) to discover workspaces/teams/projects from Linear"
            f" MCP. Walk through full setup via AskUserQuestion: pick workspace,"
            f' pick team, present projects with "Create a new project" as the'
            f' last option. If they pick "Create a new project", ask for a name,'
            f" then delegate to the subagent to create it in Linear before"
            f' linking. Choose label (suggest "repo:{repo_name}"), ask if org'
            f' "{github_org}" should default to this workspace. If no, subagent'
            f" writes workspace:none and proceed."
        ))


if __name__ == "__main__":
    main()
