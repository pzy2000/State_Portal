"""Authoritative Gate-2 terminal for JupyterLab #18563."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import os
from pathlib import Path

from .stable_protocol import ArtifactReference, AuthorizationRecord, StableArtifact, load_stable_artifact, write_stable_artifact


_CANDIDATE={"repository_key":"jupyterlab","change_number":18563}
_SOURCE=ArtifactReference("candidate_source_packet/jupyterlab/18563/source/v3","candidate_source_packet","3.0")
_PATCH=ArtifactReference("candidate_test_patch/jupyterlab/18563/screening/v3","candidate_test_patch","3.0")
_ATOMICITY=ArtifactReference("source_atomicity/jupyterlab/18563/preannotation/v3","source_atomicity","3.0")
_TERMINAL=ArtifactReference("candidate_sprint_terminal/jupyterlab/18563/gate2-v1","candidate_sprint_terminal","1.0")


def terminal_jupyterlab_18563(root_value:str|Path,*,authorization:AuthorizationRecord)->StableArtifact:
    if type(authorization) is not AuthorizationRecord or authorization.scope!="preannotation-execution" or authorization.revoked or authorization.local_user_id!=os.getuid():raise PermissionError("inactive authorization")
    root=Path(root_value).absolute().resolve(strict=True)
    source=_write(root,_SOURCE,(),{
        "candidate":_CANDIDATE,"complete":True,"authority":"github:jupyterlab/jupyterlab",
        "pull_request":{"number":18563,"title":"Fall back to line comments when block comment syntax is unavailable","state":"merged","url":"https://github.com/jupyterlab/jupyterlab/pull/18563","closing_issue_numbers":[16212],"merge_revision":"aedb2a1119a6d8c464632b7fb95f2009bd341421"},
        "issues":[{"number":16212,"title":"Make the block comment work in Python and other languages without block comments","state":"closed","labels":["enhancement","status:Needs Discussion","pkg:shortcuts","good first issue"],"url":"https://github.com/jupyterlab/jupyterlab/issues/16212","body_classification":"problem_and_proposed_solution_for_missing_behavior"}],
        "related_non_authoritative_changes":[{"number":16770,"relationship":"earlier_attempt_stale","authoritative":False}],
        "source_complete":True,"network_read_only":True,"paid_actions_authorized":False,"formal_trials_authorized":False,
    },authorization)
    patch=_write(root,_PATCH,(source.reference,),{
        "candidate":_CANDIDATE,"complete":True,"outcome":"patch_parsed",
        "production_paths":["packages/codemirror-extension/src/commands.ts"],
        "test_paths":["galata/test/jupyterlab/file-edit.test.ts","galata/test/documentation/commands.test.ts-snapshots/commandsList-documentation-linux.json"],
        "production_change":"add fallback from toggleBlockComment to toggleComment for languages without block comment tokens",
        "new_user_facing_behavior":True,"paid_actions_authorized":False,"formal_trials_authorized":False,
    },authorization)
    atomicity=_write(root,_ATOMICITY,(source.reference,patch.reference),{
        "candidate":_CANDIDATE,"complete":True,"outcome":"rejected","primary_issue_number":16212,
        "authoritative_issue_count":1,"authoritative_issue_numbers":[16212],"incidental_change_numbers":[16770],
        "classification":"enhancement_missing_behavior","gate2_real_bug_regression":False,
        "evidence":{"issue_label":"enhancement","issue_status":"status:Needs Discussion","issue_language":"Proposed Solution","pr_language":"shortcut previously was a no-op; it now falls back to line comments"},
        "paid_actions_authorized":False,"formal_trials_authorized":False,
    },authorization)
    return _write(root,_TERMINAL,(source.reference,patch.reference,atomicity.reference),{
        "candidate":_CANDIDATE,"state":"terminal","outcome":"terminal_rejected","complete":True,"terminal":True,"resumable":False,
        "failed_gate":2,"reason":"authoritative_issue_16212_is_enhancement_not_bug_or_regression",
        "fresh_gate57_execution_started":False,"old_attempts_loaded":False,"paid_actions_authorized":False,"formal_trials_authorized":False,
        "pool_admission_written":False,"replacement_recommendation_written":False,
    },authorization)


def _load(root:Path,ref:ArtifactReference)->StableArtifact:return load_stable_artifact(root,artifact_id=ref.artifact_id,artifact_kind=ref.artifact_kind,semantic_version=ref.semantic_version)
def _write(root:Path,ref:ArtifactReference,inputs:tuple[ArtifactReference,...],payload:dict[str,object],auth:AuthorizationRecord)->StableArtifact:
    try:existing=_load(root,ref)
    except RuntimeError as error:
        if str(error) not in {"artifact directory is missing","artifact current head is missing"}:raise
    else:
        if existing.inputs!=inputs or dict(existing.payload)!=payload:raise RuntimeError(f"artifact conflict {ref.artifact_id}")
        return existing
    write_stable_artifact(root,artifact_id=ref.artifact_id,artifact_kind=ref.artifact_kind,semantic_version=ref.semantic_version,created_at=_now(),inputs=inputs,payload=payload,authorization=auth);return _load(root,ref)
def _now()->str:return (datetime.now(UTC)+timedelta(microseconds=1)).isoformat(timespec="microseconds").replace("+00:00","Z")
__all__=["terminal_jupyterlab_18563"]
