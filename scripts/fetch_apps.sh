#!/usr/bin/env bash
# Fetch benchmark applications and baselines at their pinned commits.
#
# Use this when the repository was obtained as an archive (no .git, so
# `git submodule update` is unavailable). In a git checkout, prefer
#   git submodule update --init --depth 1 games/<app>
#
# Usage: scripts/fetch_apps.sh [app ...]   (default: 2048)
#        scripts/fetch_apps.sh all
set -euo pipefail

cd "$(dirname "$0")/.."

pinned() {
  case "$1" in
    2048)        echo "games/2048 https://github.com/gabrielecirulli/2048.git 8b5c8a899a4acfd3258ad76e0af8e35a6bf7a3b9" ;;
    luanti)      echo "games/luanti https://github.com/luanti-org/luanti.git ba414e29fddf52337b16d673e17f87165bab3eff" ;;
    openra)      echo "games/OpenRA https://github.com/OpenRA/OpenRA.git 7e687f430ee5b194c0ffaa06caaba757dd9155fb" ;;
    jupyterlab)  echo "games/jupyterlab https://github.com/jupyterlab/jupyterlab.git 4a19f8fff4049a9536e21fa2980fe7e5ee592d01" ;;
    jynew)       echo "games/jynew https://github.com/jynew/jynew.git be27d95303cb1915bdfbcfe15046fa3f937b7eb7" ;;
    pal3)        echo "games/Pal3.Unity https://github.com/0x7c13/Pal3.Unity.git 577017d943751a81886604a7744996e9ac968318" ;;
    libreoffice) echo "games/LibreOffice https://github.com/LibreOffice/core.git 3f84070274f87588c9ef69e9ea9e2426335392c8" ;;
    blender)     echo "games/blender https://github.com/blender/blender.git 3d018bcabdaeea66cf4ef4a96ce8913164831f6e" ;;
    agent-s)     echo "baselines/agent-s https://github.com/simular-ai/Agent-S.git ae5ff9a577ecfcd54d865acc471de91e5560392e" ;;
    scalecua)    echo "baselines/ScaleCUA https://github.com/OpenGVLab/ScaleCUA.git 5d92feea9f1e14b8303ce37da45b286fb1f4d3aa" ;;
    *) return 1 ;;
  esac
}

ALL=(2048 luanti openra jupyterlab jynew pal3 libreoffice blender agent-s scalecua)

if [[ $# -eq 0 ]]; then
  set -- 2048
elif [[ "$1" == "all" ]]; then
  set -- "${ALL[@]}"
fi

for app in "$@"; do
  if ! entry=$(pinned "$app"); then
    echo "unknown app: $app (choices: ${ALL[*]})" >&2
    exit 2
  fi
  read -r path url commit <<<"$entry"
  if [[ -e "$path/.git" ]] && [[ "$(git -C "$path" rev-parse HEAD 2>/dev/null)" == "$commit" ]]; then
    echo "$path already at ${commit:0:8}"
    continue
  fi
  echo "fetching $path @ ${commit:0:8}"
  mkdir -p "$path"
  git -C "$path" init -q
  git -C "$path" remote remove origin 2>/dev/null || true
  git -C "$path" remote add origin "$url"
  git -C "$path" fetch -q --depth 1 origin "$commit"
  git -C "$path" checkout -q --detach FETCH_HEAD
done
