#!/usr/bin/env bash
# Git calls this helper. The credential remains in the CI environment.
case "$1" in
  *Username*)
    if [ "$GITOPS_BACKEND" = github ]; then printf '%s\n' x-access-token;
    else printf '%s\n' gitlab-ci-token; fi ;;
  *Password*)
    if [ "$GITOPS_BACKEND" = github ]; then printf '%s\n' "$GITHUB_GITOPS_TOKEN";
    else printf '%s\n' "$CI_JOB_TOKEN"; fi ;;
  *) exit 1 ;;
esac
