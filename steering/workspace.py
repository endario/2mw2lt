"""The workspace: one directory per repository, shared by every worktree of it.

Kept free of daemon imports so a hook can resolve it in a few milliseconds."""
from __future__ import annotations

import subprocess

from local_workspace import (
    workspace_root,
    required_workspace_root,
    common_root,
    _is_gitdir,
    _pointer,
    _mine,
    _read_common_dir,
    _common_dir,
    origin_slug,
    origin_url,
    ssh_host,
    github_slug,
    workspace_header,
    header_workspace,
    header_path,
    path_header,
    _key_name,
    key_file,
    legacy_key_file,
    agent_key,
    agent_port,
    _record_env_line,
    _port_key,
    _key_file_without_record,
    agent_key_file,
    write_agent_port,
    write_agent_key_file,
    DEFAULT_AGENT_PORT,
    KEEP_ALIVE_SECONDS,
    KEY_HEADER,
    WORKSPACE_HEADER,
    _GIT_ENV,
)


