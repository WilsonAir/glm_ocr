#!/usr/bin/env bash
# Initialize git, commit, create GitHub repo (hub), and push via SSH key.
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_NAME="${REPO_NAME:-glm_ocr}"
SSH_KEY="${SSH_KEY:-/root/.ssh/id_ed25519}"
GIT_SSH_COMMAND="ssh -i ${SSH_KEY} -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"

cd "$PROJECT_ROOT"

if [[ ! -d .git ]]; then
  git init -b main
fi

git add -A
if git diff --cached --quiet; then
  echo "Nothing to commit."
else
  git commit -m "$(cat <<'EOF'
Add GLM-OCR SDK deployment for PPU + vLLM.

Includes self-hosted config, env setup scripts, and PDF parse test
pointing to a local vLLM GLM-OCR service.
EOF
)"
fi

export GIT_SSH_COMMAND

if ! git remote get-url origin >/dev/null 2>&1; then
  echo "Creating GitHub repository WilsonAir/${REPO_NAME} ..."
  hub create -d "GLM-OCR SDK deployment (PPU + vLLM)" -p "glm_ocr,ocr,vllm,ppu" "${REPO_NAME}" 2>/dev/null || {
    echo "hub create failed (need GITHUB_TOKEN). Adding remote manually."
    git remote add origin "git@github.com:WilsonAir/${REPO_NAME}.git"
  }
fi

echo "Pushing to origin main ..."
git push -u origin main

echo "Done: https://github.com/WilsonAir/${REPO_NAME}"
