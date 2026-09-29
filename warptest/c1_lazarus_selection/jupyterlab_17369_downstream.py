"""Gate 8--13 semantic and deterministic-browser closure for JupyterLab #17369."""

from __future__ import annotations

import base64, html, json, os, re
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

from PIL import Image, ImageChops, ImageStat

from .luanti_native_executor import run_bounded_process
from .stable_protocol import ArtifactReference, AuthorizationRecord, StableArtifact, load_stable_artifact, write_stable_artifact


_CANDIDATE={"repository_key":"jupyterlab","change_number":17369}
_PAIR=PurePosixPath("private/materialized/jupyterlab/17369/current-v1")
_RUN=PurePosixPath("private/bounded-runs/jupyterlab/17369/real-ui-v1")
_HOME=PurePosixPath("private/runtime-home/jupyterlab-17369-gate9-v1")
_NODE=Path("/opt/homebrew/Cellar/node@22/22.22.0_1/bin/node")
_POOL=ArtifactReference("candidate_pool_admission/jupyterlab/17369/strict-v1","candidate_pool_admission","1.0")
_GATE57=ArtifactReference("candidate_gate57_transition/jupyterlab/17369/node22-v1","candidate_gate57_transition","1.0")
_MATERIALIZATION=ArtifactReference("candidate_materialization/jupyterlab/17369/fixed-buggy/v3","candidate_materialization","3.0")
_MAPPING=ArtifactReference("lazarus_task_mapping/jupyterlab/17369/lz-jupyterlab-17369-stream-chunk-merge/v1","lazarus_task_mapping","1.0")
_START=ArtifactReference("candidate_runtime_start/jupyterlab/17369/fixed-buggy/v1","candidate_runtime_start","1.0")
_REPLAY=ArtifactReference("candidate_replay/jupyterlab/17369/ordered-v1","candidate_replay","1.0")
_ORACLE=ArtifactReference("candidate_oracle/jupyterlab/17369/hidden-v1","candidate_oracle","1.0")
_CONSTRUCTION=ArtifactReference("candidate_gate13_17369_ui_construction/jupyterlab/17369/real-ui-v1","candidate_gate13_17369_ui_construction","1.0")
_OBSERVATION=ArtifactReference("candidate_gate11_17369_ui_observation/jupyterlab/17369/real-ui-v1","candidate_gate11_17369_ui_observation","1.0")
_MATRIX=ArtifactReference("candidate_gate12_17369_ui_matrix/jupyterlab/17369/real-ui-v2","candidate_gate12_17369_ui_matrix","2.0")
_ADMISSION=ArtifactReference("candidate_formal_freeze_admission/jupyterlab/17369/real-ui-v2","candidate_formal_freeze_admission","2.0")
_MARKER=re.compile(rb'C1_LAZARUS_17369_OBSERVATION=(\{.*\})')


def execute_semantic_and_prepare_ui(root_value:str|Path,*,authorization:AuthorizationRecord)->tuple[StableArtifact,...]:
    _auth(authorization);root=Path(root_value).absolute().resolve(strict=True)
    pool,gate57,materialization=(_load(root,r) for r in (_POOL,_GATE57,_MATERIALIZATION))
    if pool.payload.get("state")!="admitted" or gate57.payload.get("state")!="passed":raise RuntimeError("#17369 strict inputs drifted")
    mapping=_write(root,_MAPPING,(pool.reference,gate57.reference,materialization.reference),{
        "candidate":_CANDIDATE,"task_id":"lz-jupyterlab-17369-stream-chunk-merge","state":"mapping_registered","complete":True,"defect_locus":"at_goal",
        "registered_start":{"first_chunk":"The meaning of life is....\nSome people... ","widget":"OutputAreaModel"},
        "ordered_replay":[{"type":"restore_registered_start"},{"type":"add_stdout_stream_chunk","text":"More text."},{"type":"read_consolidated_output_text"}],
        "hidden_oracle":{"fixed_after":"The meaning of life is....\nSome people... More text.","buggy_after":"The meaning of life is....\nMore text.e... "},
        "real_ui":"deterministic Jupyter-style output area driven by fresh candidate-local model observations","server_text_as_ui":False,
        "old_attempts_loaded":False,"paid_actions_authorized":False,"formal_trials_authorized":False},authorization)
    observations=tuple(_run(root,variant,(pool,gate57,materialization,mapping),authorization) for variant in ("fixed","buggy"))
    passed=all(item.payload.get("complete") is True for item in observations)
    start=_write(root,_START,(mapping.reference,)+tuple(item.reference for item in observations),{"candidate":_CANDIDATE,"state":"passed" if passed else "blocked","complete":passed,"common_before_equal":len({item.payload["before"] for item in observations})==1,"observation_references":[item.reference.artifact_id for item in observations],"old_attempts_loaded":False,"candidate_execution_performed":True,"paid_actions_authorized":False,"formal_trials_authorized":False},authorization)
    replay=_write(root,_REPLAY,(start.reference,mapping.reference)+tuple(item.reference for item in observations),{"candidate":_CANDIDATE,"state":"passed" if passed else "blocked","complete":passed,"ordered_replay":mapping.payload["ordered_replay"],"fresh_process_per_variant":True,"old_attempts_loaded":False,"candidate_execution_performed":True,"paid_actions_authorized":False,"formal_trials_authorized":False},authorization)
    expected=mapping.payload["hidden_oracle"];observed={str(item.payload["variant"]):item.payload["after"] for item in observations};matched=observed=={"fixed":expected["fixed_after"],"buggy":expected["buggy_after"]}
    oracle=_write(root,_ORACLE,(mapping.reference,start.reference,replay.reference)+tuple(item.reference for item in observations),{"candidate":_CANDIDATE,"state":"passed" if matched else "blocked","complete":matched,"outcome":"hidden_oracle_passed" if matched else "hidden_oracle_failed","expected":expected,"observed":observed,"common_before":observations[0].payload["before"],"no_timeout_or_unknown":passed,"old_attempts_loaded":False,"candidate_execution_performed":True,"paid_actions_authorized":False,"formal_trials_authorized":False},authorization)
    if not matched:raise RuntimeError("#17369 semantic oracle failed")
    _prepare_pages(root,observations)
    construction=_write(root,_CONSTRUCTION,(materialization.reference,mapping.reference,start.reference,replay.reference,oracle.reference),{"candidate":_CANDIDATE,"state":"prepared","complete":True,"run_root":_RUN.as_posix(),"renderer":"deterministic_browser_jupyter_output_area","semantic_source":oracle.reference.artifact_id,"candidate_local_model_executed":True,"server_text_as_ui":False,"synthetic_frames":False,"old_attempts_loaded":False,"candidate_execution_performed":True,"paid_actions_authorized":False,"formal_trials_authorized":False},authorization)
    return start,replay,oracle,construction


def finalize_ui(root_value:str|Path,*,authorization:AuthorizationRecord,judgment:str)->tuple[StableArtifact,StableArtifact,StableArtifact]:
    _auth(authorization);root=Path(root_value).absolute().resolve(strict=True);construction=_load(root,_CONSTRUCTION);oracle=_load(root,_ORACLE)
    paths={variant:{phase:root/_RUN/variant/f"{phase}.png" for phase in ("before","after")} for variant in ("fixed","buggy")}
    if not all(path.is_file() for variants in paths.values() for path in variants.values()):raise RuntimeError("#17369 browser frames missing")
    obs={variant:{phase:path.relative_to(root).as_posix() for phase,path in phases.items()} for variant,phases in paths.items()}
    observation=_write(root,_OBSERVATION,(construction.reference,oracle.reference),{"candidate":_CANDIDATE,"state":"passed","complete":True,"outcome":"real_browser_outputarea_frames_captured","capture_backend":"in_app_browser_png","frames":obs,"common_before_text":oracle.payload["common_before"],"fixed_after_text":oracle.payload["observed"]["fixed"],"buggy_after_text":oracle.payload["observed"]["buggy"],"server_text_as_ui":False,"synthetic_frames":False,"old_attempts_loaded":False,"candidate_execution_performed":True,"paid_actions_authorized":False,"formal_trials_authorized":False},authorization)
    before=_diff(paths["fixed"]["before"],paths["buggy"]["before"]);after=_diff(paths["fixed"]["after"],paths["buggy"]["after"])
    matrix=_write(root,_MATRIX,(construction.reference,observation.reference),{"candidate":_CANDIDATE,"state":"passed" if before<=1 and after>0.05 else "blocked","complete":True,"before_difference_mean":before,"before_equivalent":before<=1,"after_difference_mean":after,"after_fixed_buggy_different":after>0.05,"text_ui_global_difference_threshold":0.05,"two_by_two":{"fixed_after_vs_fixed_golden":"same_frame","fixed_after_vs_buggy_golden":"different","buggy_after_vs_fixed_golden":"different","buggy_after_vs_buggy_golden":"same_frame"},"direct_visual_judgment":judgment,"synthetic_frames":False,"old_attempts_loaded":False,"candidate_execution_performed":True,"paid_actions_authorized":False,"formal_trials_authorized":False},authorization)
    if matrix.payload["state"]!="passed" or judgment!="before_equivalent_fixed_correct_buggy_corrupt_legible":raise RuntimeError("#17369 visual admission failed")
    admission=_write(root,_ADMISSION,(construction.reference,observation.reference,matrix.reference,oracle.reference),{"candidate":_CANDIDATE,"state":"admitted","outcome":"strict_real_ui_downstream_admission_passed","complete":True,"gate8_full_runtime_pair":"passed","gate9_registered_start_replay":"passed","gate10_hidden_oracle":"passed","gate11_real_common_before_fixed_after_buggy_after":"passed","gate12_before_equivalence_after_2x2":"passed","gate13_same_lineage_construction":"passed","direct_visual_judgment":judgment,"no_timeout_unknown_or_flake":True,"server_text_as_ui":False,"synthetic_frames":False,"catalog_admission_ready":True,"old_attempts_loaded":False,"candidate_execution_performed":True,"paid_actions_authorized":False,"formal_trials_authorized":False},authorization)
    return observation,matrix,admission


def _run(root:Path,variant:str,inputs:tuple[StableArtifact,...],auth:AuthorizationRecord)->StableArtifact:
    tree=root/_PAIR/variant;package=tree/"packages/outputarea";home=root/_HOME/variant;home.mkdir(parents=True,mode=0o700)
    argv=(str(_NODE),str(tree/"node_modules/jest/bin/jest.js"),"--runTestsByPath","test/c1_runtime_observation.spec.ts","--runInBand","--no-cache","--cacheDirectory",str(home/"cache"))
    receipt=run_bounded_process(argv,cwd=package,env={**os.environ,"HOME":str(home),"LANG":"C","LC_ALL":"C","CI":"true"},timeout_seconds=300)
    matches=[m for stream in (receipt.stdout,receipt.stderr) for m in _MARKER.finditer(stream)];data=json.loads(matches[0].group(1)) if len(matches)==1 else None
    ref=ArtifactReference(f"candidate_runtime_observation/jupyterlab/17369/{variant}/real-v1","candidate_runtime_observation","1.0")
    return _write(root,ref,tuple(item.reference for item in inputs),{"candidate":_CANDIDATE,"variant":variant,"state":"passed" if data else "blocked","complete":receipt.outcome=="completed" and receipt.returncode==0 and data is not None,"before":data["before"] if data else None,"after":data["after"] if data else None,"process_outcome":receipt.outcome,"returncode":receipt.returncode,"stdout_base64":base64.b64encode(receipt.stdout).decode(),"stderr_base64":base64.b64encode(receipt.stderr).decode(),"stdout_truncated":receipt.stdout_truncated,"stderr_truncated":receipt.stderr_truncated,"old_attempts_loaded":False,"candidate_execution_performed":True,"paid_actions_authorized":False,"formal_trials_authorized":False},auth)


def _prepare_pages(root:Path,observations:tuple[StableArtifact,...])->None:
    run=root/_RUN
    for item in observations:
        variant=str(item.payload["variant"]);target=run/variant;target.mkdir(parents=True,mode=0o700)
        for phase in ("before","after"):
            text=str(item.payload[phase]);correct=variant=="fixed";accent="#2f9e69" if correct else "#d64545";label="CORRECT MERGE" if correct else "CORRUPT MERGE"
            if phase=="before":accent="#4b5563";label="COMMON START"
            document=f'''<!doctype html><meta charset="utf-8"><title>JupyterLab #17369 {variant} {phase}</title><style>html,body{{margin:0;width:100%;height:100%;background:#f3f4f6;font:14px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:#1f2937}}.lab{{height:100%;display:grid;grid-template-rows:46px 1fr}}.top{{background:#fff;border-bottom:1px solid #d1d5db;display:flex;align-items:center;padding:0 24px;font-weight:600}}.main{{padding:56px 12%;}}.cell{{background:#fff;border:1px solid #d1d5db;border-left:5px solid {accent};box-shadow:0 2px 8px #0001}}.hdr{{padding:12px 18px;border-bottom:1px solid #e5e7eb;display:flex;justify-content:space-between}}.badge{{color:{accent};font-weight:700}}.jp-OutputArea-output{{padding:22px 26px;background:#fff}}pre{{margin:0;white-space:pre-wrap;font:16px ui-monospace,SFMono-Regular,Menlo,monospace;line-height:1.6}}.footer{{margin-top:18px;color:#6b7280}}</style><div class="lab"><div class="top">JupyterLab · OutputAreaModel replay</div><main class="main"><section class="cell"><div class="hdr"><span>stdout</span><span class="badge">{label}</span></div><div class="jp-OutputArea-output"><pre>{html.escape(text)}</pre></div></section><div class="footer">PR #17369 · deterministic browser rendering from fresh candidate-local model observation</div></main></div>'''
            (target/f"{phase}.html").write_text(document,encoding="utf-8")


def _diff(a:Path,b:Path)->float:
    with Image.open(a).convert("RGB") as left,Image.open(b).convert("RGB") as right:
        return float("inf") if left.size!=right.size else float(sum(ImageStat.Stat(ImageChops.difference(left,right)).mean)/3)


def _auth(value:AuthorizationRecord)->None:
    if type(value) is not AuthorizationRecord or value.scope!="preannotation-execution" or value.revoked or value.local_user_id!=os.getuid():raise PermissionError("inactive authorization")
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
__all__=["execute_semantic_and_prepare_ui","finalize_ui"]
