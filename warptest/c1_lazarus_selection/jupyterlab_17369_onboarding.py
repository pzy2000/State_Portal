"""Strict modern-Node onboarding for JupyterLab #17369."""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
import hashlib
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess

from .luanti_native_executor import run_bounded_process
from .stable_protocol import ArtifactReference, AuthorizationRecord, StableArtifact, load_stable_artifact, write_stable_artifact


_CANDIDATE = {"repository_key": "jupyterlab", "change_number": 17369}
_FIXED_COMMIT = "5787c883ab6f3b7a9d1cdaa5c8d6d1e774823e75"
_PAIR = PurePosixPath("private/materialized/jupyterlab/17369/current-v1")
_MIRROR = PurePosixPath("private/repository-enumeration-git/jupyterlab/mirror.git")
_NODE = Path("/opt/homebrew/Cellar/node@22/22.22.0_1/bin/node")
_HOME = PurePosixPath("private/runtime-home/jupyterlab-17369-gate57-v1")
_VERSION = "1.0"

_SOURCE = ArtifactReference("candidate_source_packet/jupyterlab/17369/source/v3", "candidate_source_packet", "3.0")
_PATCH = ArtifactReference("candidate_test_patch/jupyterlab/17369/screening/v3", "candidate_test_patch", "3.0")
_ATOMICITY = ArtifactReference("source_atomicity/jupyterlab/17369/preannotation/v3", "source_atomicity", "3.0")
_MANIFEST = ArtifactReference("candidate_stage/jupyterlab/17369/test_manifest/v3", "candidate_stage", "3.0")
_CAPSULE = ArtifactReference("fixed_source_capsule/jupyterlab/17369/fixed/v3", "fixed_source_capsule", "3.0")
_PROFILE = ArtifactReference("candidate_profile/jupyterlab/17369/strict-v1", "candidate_profile", _VERSION)
_RUNTIME = ArtifactReference("candidate_runtime/jupyterlab/17369/node22-v1", "candidate_runtime", _VERSION)
_MATERIALIZATION = ArtifactReference("candidate_materialization/jupyterlab/17369/fixed-buggy/v3", "candidate_materialization", "3.0")
_AGGREGATE = ArtifactReference("candidate_gate57_aggregate/jupyterlab/17369/node22-v1", "candidate_gate57_aggregate", _VERSION)
_TRANSITION = ArtifactReference("candidate_gate57_transition/jupyterlab/17369/node22-v1", "candidate_gate57_transition", _VERSION)
_ADMISSION = ArtifactReference("candidate_pool_admission/jupyterlab/17369/strict-v1", "candidate_pool_admission", _VERSION)
_RECOMMENDATION = ArtifactReference("candidate_replacement_recommendation/jupyterlab/17369/current-v1", "candidate_replacement_recommendation", _VERSION)

_F2P = "should correctly merge lines if the first line does not end with new line"
_P2P = "should consolidate consecutive stream outputs of the same kind"
_ISSUE_BODY = """print() with newline and end=\"\" can produce corrupt output. Expected Some people... More text., observed More text.e..."""
_PR_BODY = """Fixes #17358. Update current position to end of text. Better handling of cell output streams."""


def onboard_jupyterlab_17369(
    artifact_root: str | Path, *, authorization: AuthorizationRecord
) -> tuple[StableArtifact, StableArtifact, StableArtifact, StableArtifact]:
    _auth(authorization); root = Path(artifact_root).absolute().resolve(strict=True)
    chain = _register_static(root, authorization)
    attempts = tuple(
        _run_attempt(root, phase, variant, repetition, chain, authorization)
        for phase, selector in (("f2p", _F2P), ("p2p", _P2P))
        for variant in ("fixed", "buggy")
        for repetition in (1, 2, 3)
    )
    aggregate = _register_aggregate(root, chain, attempts, authorization)
    transition = _register_transition(root, chain, aggregate, attempts, authorization)
    admission = _register_admission(root, chain, transition, aggregate, authorization)
    recommendation = _register_recommendation(root, admission, authorization)
    return aggregate, transition, admission, recommendation


def _register_static(root: Path, auth: AuthorizationRecord) -> tuple[StableArtifact, ...]:
    mirror = _dir(root / _MIRROR, root); fixed = _dir(root / _PAIR / "fixed", root); buggy = _dir(root / _PAIR / "buggy", root)
    source = _write(root, _SOURCE, (), {
        "candidate": _CANDIDATE, "complete": True, "authority": "github:jupyterlab/jupyterlab",
        "pull_request": {"number":17369,"title":"Fix cell output stream if previous chunk did not end in new line","state":"merged","url":"https://github.com/jupyterlab/jupyterlab/pull/17369","closing_issue_numbers":[17358],"body_fingerprint":_text_fingerprint(_PR_BODY)},
        "issues": [{"number":17358,"title":"print() with newline and end empty can produce corrupt output","labels":["bug","tag:Regression"],"state":"closed","url":"https://github.com/jupyterlab/jupyterlab/issues/17358","body_fingerprint":_text_fingerprint(_ISSUE_BODY)}],
        "incidental_cross_references": [{"number":17320,"source":"issue_17358_member_comment","authoritative":False}],
        "fixed_revision": _FIXED_COMMIT, "source_complete": True, "network_read_only": True,
        "paid_actions_authorized": False, "formal_trials_authorized": False,
    }, auth)
    patch = _write(root, _PATCH, (source.reference,), {
        "candidate":_CANDIDATE,"complete":True,"outcome":"patch_eligible","fixed_revision":_FIXED_COMMIT,
        "production_paths":["packages/outputarea/src/model.ts"],"test_paths":["packages/outputarea/test/model.spec.ts"],
        "production_change":"move idx0 prefix advancement before the no-newline break","production_only_inverse":True,
        "paid_actions_authorized":False,"formal_trials_authorized":False,
    }, auth)
    atomicity = _write(root, _ATOMICITY, (source.reference,patch.reference), {
        "candidate":_CANDIDATE,"complete":True,"outcome":"accepted","primary_issue_number":17358,
        "authoritative_issue_count":1,"authoritative_issue_numbers":[17358],"incidental_issue_numbers":[17320],
        "classification":"real_bug_regression","issue_reproducer":"two stdout stream chunks where the first ends without newline",
        "paid_actions_authorized":False,"formal_trials_authorized":False,
    }, auth)
    manifest = _write(root, _MANIFEST, (source.reference,patch.reference,atomicity.reference), {
        "candidate":_CANDIDATE,"state":"passed","stage":"test_manifest","complete":True,
        "executable_test_paths":["packages/outputarea/test/model.spec.ts"],
        "f2p_selector":_F2P,"p2p_selector":_P2P,"runner":"jest_node22","test_count":2,
        "paid_actions_authorized":False,"formal_trials_authorized":False,
    }, auth)
    entries = _git(mirror,"ls-tree","-r",_FIXED_COMMIT).splitlines(); differences=[]; left_value=hashlib.sha256(); right_value=hashlib.sha256()
    for entry in entries:
        meta, relative = entry.split("\t",1); mode=meta.split(" ",1)[0]
        left=_tree_bytes(fixed/PurePosixPath(relative),mode,root); right=_tree_bytes(buggy/PurePosixPath(relative),mode,root)
        for value,content in ((left_value,left),(right_value,right)):
            value.update(relative.encode()+b"\0"+mode.encode()+b"\0"+hashlib.sha256(content).digest())
        if left != right: differences.append(relative)
    if differences != ["packages/outputarea/src/model.ts"]: raise RuntimeError("#17369 inverse is not production-only")
    test_path=PurePosixPath("packages/outputarea/test/model.spec.ts")
    if _file(fixed/test_path,root).read_bytes()!=_file(buggy/test_path,root).read_bytes(): raise RuntimeError("#17369 fixed test bytes changed")
    capsule = _write(root, _CAPSULE, (source.reference,patch.reference,atomicity.reference,manifest.reference), {
        "candidate":_CANDIDATE,"state":"materialized","complete":True,"source_revision":_FIXED_COMMIT,
        "source_tree_path":f"{_PAIR.as_posix()}/fixed","tracked_file_count":len(entries),"tree_fingerprint":left_value.hexdigest(),
        "production_paths":["packages/outputarea/src/model.ts"],"test_paths":[test_path.as_posix()],
        "paid_actions_authorized":False,"formal_trials_authorized":False,
    }, auth)
    profile = _write(root, _PROFILE, (source.reference,patch.reference,atomicity.reference,manifest.reference,capsule.reference), {
        "candidate":_CANDIDATE,"state":"passed","complete":True,"profile_id":"jupyterlab-17369-node22-stream-merge-v1",
        "gate_status":{"source_authority":"passed","single_bug_mapping":"passed","strict_production_inverse":"passed","fixed_test_buggy_api_compatibility":"passed"},
        "buggy_api_surface_unchanged":True,"fixed_only_identifiers":[],"test_import":"OutputAreaModel","existing_methods_used":["add","get","toJSON"],
        "defect_locus":"at_goal","old_attempts_loaded":False,"paid_actions_authorized":False,"formal_trials_authorized":False,
    }, auth)
    version = subprocess.run((str(_NODE),"--version"),capture_output=True,check=False,timeout=15)
    if version.returncode != 0 or version.stdout.strip()!=b"v22.22.0": raise RuntimeError("#17369 Node22 runtime drifted")
    runtime = _write(root, _RUNTIME, (profile.reference,), {
        "candidate":_CANDIDATE,"state":"ready","complete":True,"runtime":"node","runtime_path":str(_NODE),"runtime_version":"v22.22.0",
        "jest_path":f"{_PAIR.as_posix()}/{{variant}}/node_modules/jest/bin/jest.js","dependency_snapshot":"candidate-local APFS clone from configured jupyterlab workspace cache",
        "dependencies_copied_per_variant":True,"old_attempts_loaded":False,"paid_actions_authorized":False,"formal_trials_authorized":False,
    }, auth)
    fixed_test=_file(fixed/test_path,root); buggy_test=_file(buggy/test_path,root)
    materialization = _write(root, _MATERIALIZATION, (capsule.reference,profile.reference,runtime.reference), {
        "candidate":_CANDIDATE,"state":"built","complete":True,"fixed_tree_path":f"{_PAIR.as_posix()}/fixed","buggy_tree_path":f"{_PAIR.as_posix()}/buggy",
        "only_different_tracked_paths":differences,"fixed_inventory_fingerprint":left_value.hexdigest(),"buggy_inventory_fingerprint":right_value.hexdigest(),
        "fixed_test_fingerprint":_fingerprint(fixed_test),"buggy_test_fingerprint":_fingerprint(buggy_test),"fixed_test_bytes_preserved":True,
        "fixed_compiled_model":f"{_PAIR.as_posix()}/fixed/packages/outputarea/lib/model.js","buggy_compiled_model":f"{_PAIR.as_posix()}/buggy/packages/outputarea/lib/model.js",
        "node_modules_snapshot_per_variant":True,"old_attempts_loaded":False,"candidate_execution_performed":False,"paid_actions_authorized":False,"formal_trials_authorized":False,
    }, auth)
    return source,patch,atomicity,manifest,capsule,profile,runtime,materialization


def _run_attempt(root: Path, phase: str, variant: str, repetition: int, chain: tuple[StableArtifact,...], auth: AuthorizationRecord) -> StableArtifact:
    selector = _F2P if phase=="f2p" else _P2P; tree=_dir(root/_PAIR/variant,root); package=_dir(tree/"packages/outputarea",root); home=root/_HOME/phase/variant/str(repetition); home.mkdir(parents=True,mode=0o700)
    argv=(str(_NODE),str(tree/"node_modules/jest/bin/jest.js"),"--runTestsByPath","test/model.spec.ts","-t",selector,"--runInBand","--no-cache", "--cacheDirectory", str(home/"jest-cache"))
    receipt=run_bounded_process(argv,cwd=package,env={**os.environ,"HOME":str(home),"LANG":"C","LC_ALL":"C","CI":"true"},timeout_seconds=300)
    observed="pass" if receipt.outcome=="completed" and receipt.returncode==0 else "failure" if receipt.outcome=="completed" else receipt.outcome
    expected="failure" if phase=="f2p" and variant=="buggy" else "pass"
    ref=ArtifactReference(f"candidate_test_attempt/jupyterlab/17369/{phase}/{variant}/{repetition}/node22-v1","candidate_test_attempt",_VERSION)
    payload={"candidate":_CANDIDATE,"fresh_epoch":"jupyterlab-17369-node22-v1","phase":phase,"variant":variant,"repetition":repetition,"selector":selector,
        "expected_outcome":expected,"observed_outcome":observed,"matched_expected":observed==expected,"process_outcome":receipt.outcome,"returncode":receipt.returncode,"argv":list(argv),
        "stdout_base64":base64.b64encode(receipt.stdout).decode(),"stderr_base64":base64.b64encode(receipt.stderr).decode(),"stdout_truncated":receipt.stdout_truncated,"stderr_truncated":receipt.stderr_truncated,
        "old_attempts_loaded":False,"candidate_execution_performed":True,"paid_actions_authorized":False,"formal_trials_authorized":False}
    return _write(root,ref,tuple(item.reference for item in chain),payload,auth)


def _register_aggregate(root: Path, chain: tuple[StableArtifact,...], attempts: tuple[StableArtifact,...], auth: AuthorizationRecord) -> StableArtifact:
    groups={}
    for attempt in attempts: groups.setdefault((attempt.payload["phase"],attempt.payload["variant"]),[]).append(attempt)
    cells=[]
    for phase,variant in (("f2p","fixed"),("f2p","buggy"),("p2p","fixed"),("p2p","buggy")):
        items=groups[(phase,variant)]; cells.append({"phase":phase,"variant":variant,"observed_outcomes":[item.payload["observed_outcome"] for item in items],"matched_expected":[item.payload["matched_expected"] for item in items]})
    qualified=len(attempts)==12 and all(item.payload["matched_expected"] is True and item.payload["process_outcome"]=="completed" for item in attempts)
    return _write(root,_AGGREGATE,tuple(item.reference for item in chain)+tuple(item.reference for item in attempts),{"candidate":_CANDIDATE,"state":"qualified" if qualified else "blocked","outcome":"gate57_qualified" if qualified else "gate57_failed","qualified":qualified,"complete":True,"attempt_count":len(attempts),"repetitions_per_cell":3,"cells":cells,"no_timeout_or_unknown":qualified,"stable_repetitions":qualified,"old_attempts_loaded":False,"candidate_execution_performed":True,"paid_actions_authorized":False,"formal_trials_authorized":False},auth)


def _register_transition(root: Path, chain: tuple[StableArtifact,...], aggregate: StableArtifact, attempts: tuple[StableArtifact,...], auth: AuthorizationRecord) -> StableArtifact:
    passed=aggregate.payload.get("qualified") is True
    return _write(root,_TRANSITION,tuple(item.reference for item in chain)+(aggregate.reference,)+tuple(item.reference for item in attempts),{"candidate":_CANDIDATE,"state":"passed" if passed else "blocked","outcome":"qualified" if passed else "failed","complete":passed,"terminal":False,"resumable":True,"attempt_count":12,"gate_status":{"source_authority":"passed","single_bug_mapping":"passed","strict_production_inverse":"passed","test_api_compatibility":"passed","fixed_f2p_3x":"passed" if passed else "failed","buggy_f2p_3x_failure":"passed" if passed else "failed","fixed_buggy_p2p_3x":"passed" if passed else "failed"},"old_attempts_loaded":False,"candidate_execution_performed":True,"paid_actions_authorized":False,"formal_trials_authorized":False},auth)


def _register_admission(root: Path, chain: tuple[StableArtifact,...], transition: StableArtifact, aggregate: StableArtifact, auth: AuthorizationRecord) -> StableArtifact:
    if transition.payload.get("state")!="passed" or aggregate.payload.get("qualified") is not True: raise RuntimeError("#17369 not strict")
    return _write(root,_ADMISSION,tuple(item.reference for item in chain)+(aggregate.reference,transition.reference),{"candidate":_CANDIDATE,"state":"admitted","outcome":"strict_gate1_7_pool_admission_passed","complete":True,"defect_locus":"at_goal","strict_gate_1_7":True,"no_timeout_unknown_or_inheritance":True,"gate8_to_13":"pending_post_admission","catalog_admission_ready":False,"old_attempts_loaded":False,"candidate_execution_performed":True,"paid_actions_authorized":False,"formal_trials_authorized":False},auth)


def _register_recommendation(root: Path, admission: StableArtifact, auth: AuthorizationRecord) -> StableArtifact:
    return _write(root,_RECOMMENDATION,(admission.reference,),{"candidate":_CANDIDATE,"state":"recommended","complete":True,"recommendation":"replace","replace_candidate":{"repository_key":"jupyterlab","change_number":574,"defect_locus":"at_goal"},"reason":"#17369 has strict fresh Gate1-7 on modern Node22 while #574 remains only Gate1-4; replacement preserves the 16 at_goal / 8 on_path roster and repository diversity","roster_balance_after":{"at_goal":16,"on_path":8,"total":24},"apply_to_roster_now":False,"requires_roster_owner_update":True,"paid_actions_authorized":False,"formal_trials_authorized":False},auth)


def _auth(value: AuthorizationRecord) -> None:
    if type(value) is not AuthorizationRecord or value.scope!="preannotation-execution" or value.revoked or value.local_user_id!=os.getuid(): raise PermissionError("inactive authorization")


def _load(root: Path, ref: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(root,artifact_id=ref.artifact_id,artifact_kind=ref.artifact_kind,semantic_version=ref.semantic_version)


def _write(root: Path, ref: ArtifactReference, inputs: tuple[ArtifactReference,...], payload: dict[str,object], auth: AuthorizationRecord) -> StableArtifact:
    try: existing=_load(root,ref)
    except RuntimeError as error:
        if str(error) not in {"artifact directory is missing","artifact current head is missing"}: raise
    else:
        if existing.inputs!=inputs or dict(existing.payload)!=payload: raise RuntimeError(f"artifact conflict {ref.artifact_id}")
        return existing
    write_stable_artifact(root,artifact_id=ref.artifact_id,artifact_kind=ref.artifact_kind,semantic_version=ref.semantic_version,created_at=_now(),inputs=inputs,payload=payload,authorization=auth); return _load(root,ref)


def _dir(path: Path, root: Path) -> Path:
    resolved=path.resolve(strict=True);resolved.relative_to(root)
    if path.is_symlink() or not resolved.is_dir(): raise RuntimeError("unsafe directory")
    return resolved


def _file(path: Path, root: Path) -> Path:
    resolved=path.resolve(strict=True);resolved.relative_to(root)
    if path.is_symlink() or not resolved.is_file(): raise RuntimeError("unsafe file")
    return resolved


def _tree_bytes(path: Path, mode: str, root: Path) -> bytes:
    if mode=="120000": return os.readlink(path).encode()
    return _file(path,root).read_bytes()


def _git(cwd: Path,*args:str)->str:
    result=subprocess.run(("git","-C",str(cwd),*args),capture_output=True,check=False)
    if result.returncode!=0: raise RuntimeError(result.stderr.decode(errors="replace")[:500])
    return result.stdout.decode().strip()


def _fingerprint(path: Path)->str:
    value=hashlib.sha256();value.update(path.read_bytes());return value.hexdigest()


def _text_fingerprint(value:str)->str:return hashlib.sha256(value.encode()).hexdigest()


def _now()->str:return (datetime.now(UTC)+timedelta(microseconds=1)).isoformat(timespec="microseconds").replace("+00:00","Z")


__all__=["onboard_jupyterlab_17369"]
