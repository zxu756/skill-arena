#!/usr/bin/env bash
# Shared environment bootstrap & path scrubber for SkillArena scripts.
# Works in any clone directory name and under isolated HOME without network access.

export PYTHONPATH="${ROOT_DIR}/src${PYTHONPATH:+:${PYTHONPATH}}"

if [[ ! -x "${ROOT_DIR}/.venv/bin/pytest" ]]; then
  if command -v pytest >/dev/null 2>&1 && python3 -c 'import sys, pytest; assert sys.version_info >= (3, 11)' >/dev/null 2>&1; then
    :
  elif command -v uv >/dev/null 2>&1; then
    _SA_PW_HOME="$(/usr/bin/python3 -c 'import os, pwd; print(pwd.getpwuid(os.getuid()).pw_dir)' 2>/dev/null || true)"
    _SA_UV_CACHE="${UV_CACHE_DIR:-}"
    _SA_UV_PY_DIR="${UV_PYTHON_INSTALL_DIR:-}"
    if [[ -z "$_SA_UV_CACHE" ]]; then
      if [[ -d "${HOME}/.cache/uv" ]]; then
        _SA_UV_CACHE="${HOME}/.cache/uv"
      elif [[ -n "$_SA_PW_HOME" && -d "${_SA_PW_HOME}/.cache/uv" ]]; then
        _SA_UV_CACHE="${_SA_PW_HOME}/.cache/uv"
      fi
    fi
    if [[ -z "$_SA_UV_PY_DIR" ]]; then
      if [[ -d "${HOME}/.local/share/uv/python" ]]; then
        _SA_UV_PY_DIR="${HOME}/.local/share/uv/python"
      elif [[ -n "$_SA_PW_HOME" && -d "${_SA_PW_HOME}/.local/share/uv/python" ]]; then
        _SA_UV_PY_DIR="${_SA_PW_HOME}/.local/share/uv/python"
      fi
    fi
    (
      cd "$ROOT_DIR"
      UV_CACHE_DIR="${_SA_UV_CACHE}" \
      UV_PYTHON_INSTALL_DIR="${_SA_UV_PY_DIR}" \
      uv sync --offline --frozen --quiet
    )
  fi
fi

if [[ -d "${ROOT_DIR}/.venv/bin" ]]; then
  export PATH="${ROOT_DIR}/.venv/bin:${PATH}"
  PYTHON="${ROOT_DIR}/.venv/bin/python3"
  PYTEST="${ROOT_DIR}/.venv/bin/pytest"
else
  PYTHON="$(command -v python3)"
  PYTEST="$(command -v pytest)"
fi

scrub_report_file() {
  local in_file="$1"
  local out_file="$2"
  "$PYTHON" - "$ROOT_DIR" "$in_file" "$out_file" <<'PY'
import os
from pathlib import Path
import pwd
import re
import sys

repo_arg = sys.argv[1]
in_path = Path(sys.argv[2])
out_path = Path(sys.argv[3])
repo_real = os.path.realpath(repo_arg)
pw_home = pwd.getpwuid(os.getuid()).pw_dir
text = in_path.read_text(encoding="utf-8")

for candidate in sorted({repo_real, repo_arg}, key=len, reverse=True):
    if candidate:
        text = text.replace(candidate, "<REPO_ROOT>")
if pw_home:
    text = text.replace(pw_home, "<USER_HOME>")

text = re.sub(r"/(?:private/)?(?:var/folders|tmp)/[^\s\"']+", "<TMPDIR>", text)
text = re.sub(r"/(?:Users|home)/[^\s/\"']+", "<USER_HOME>", text)
out_path.write_text(text, encoding="utf-8")
PY
}
