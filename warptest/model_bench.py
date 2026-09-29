from __future__ import annotations

import os
import re
import time
import copy
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from . import run_context
from .c1_evaluation import visual_oracle
from .discovery import RepositoryInspector
from .positioning import auxiliary_positioning
from .reports import new_report_dir, write_json
from .runner import run_spec
from .visual import CascadePolicyConfig, cascade_policy_config


DEFAULT_MODELS = [
    "Qwen/Qwen3-Coder-30B-A3B-Instruct",
    "deepseek-ai/DeepSeek-V3.2",
    "ZhipuAI/GLM-4.6",
    "MiniMax/MiniMax-M2.5",
    "moonshotai/Kimi-K2-Instruct",
    "mistralai/Devstral-Small-2507",
]

DEFAULT_MODELSCOPE_BASE_URL = "https://api-inference.modelscope.cn/v1"
CANONICAL_SPEC_BY_TASK = {
    "2048-merge-left": Path("specs/2048/merge-left.yaml"),
    "2048-near-win": Path("specs/2048/near-win.yaml"),
    "spacerpg-playable-flight": Path("specs/spacerpg/playable-flight.yaml"),
    "spacerpg-station-save": Path("specs/spacerpg/station-save.yaml"),
    "love-resource-action": Path("specs/love/resource-action.yaml"),
    "openra-cnc-gdi01-beachhead": Path("specs/openra/cnc-gdi01-beachhead.yaml"),
    "openra-cnc-nod03a-prison-break": Path("specs/openra/cnc-nod03a-prison-break.yaml"),
    "openra-cnc-hot-box-dropzone": Path("specs/openra/cnc-hot-box-dropzone.yaml"),
    "openra-ra-allies03a-dead-end": Path("specs/openra/ra-allies03a-dead-end.yaml"),
    "openra-ra-soviet05-distant-thunder": Path("specs/openra/ra-soviet05-distant-thunder.yaml"),
    "openra-ra-drop-zone": Path("specs/openra/ra-drop-zone.yaml"),
    "openra-ra-soviet05-war-factory-heavy-tank": Path("specs/openra/ra-soviet05-war-factory-heavy-tank.yaml"),
    "openra-ra-soviet05-barracks-rocket-infantry": Path("specs/openra/ra-soviet05-barracks-rocket-infantry.yaml"),
    "openra-ra-allies07-soviet-war-factory-produces-heavy-tank": Path("specs/openra/ra-allies07-soviet-war-factory-produces-heavy-tank.yaml"),
    "openra-ra-allies05a-soviet-war-factory-produces-vehicle": Path("specs/openra/ra-allies05a-soviet-war-factory-produces-vehicle.yaml"),
    "jupyterlab-text-file-edit": Path("specs/jupyterlab/text-file-edit.yaml"),
    "jupyterlab-markdown-file-save": Path("specs/jupyterlab/markdown-file-save.yaml"),
    "jupyterlab-notebook-cell-output": Path("specs/jupyterlab/notebook-cell-output.yaml"),
    "jupyterlab-notebook-markdown-note": Path("specs/jupyterlab/notebook-markdown-note.yaml"),
    "jupyterlab-workspace-layout": Path("specs/jupyterlab/workspace-layout.yaml"),
    "jupyterlab-session-smoke": Path("specs/jupyterlab/session-smoke.yaml"),
    "libreoffice-writer-board-report-toc": Path("specs/libreoffice/writer-board-report-toc.yaml"),
    "libreoffice-writer-invoice-table-rollup": Path("specs/libreoffice/writer-invoice-table-rollup.yaml"),
    "libreoffice-writer-policy-cross-reference": Path("specs/libreoffice/writer-policy-cross-reference.yaml"),
    "libreoffice-calc-quarterly-budget-rollup": Path("specs/libreoffice/calc-quarterly-budget-rollup.yaml"),
    "libreoffice-calc-sales-sort-ranking": Path("specs/libreoffice/calc-sales-sort-ranking.yaml"),
    "libreoffice-calc-named-range-forecast": Path("specs/libreoffice/calc-named-range-forecast.yaml"),
    "libreoffice-calc-chart-refresh-export": Path("specs/libreoffice/calc-chart-refresh-export.yaml"),
    "libreoffice-impress-status-deck-build": Path("specs/libreoffice/impress-status-deck-build.yaml"),
    "libreoffice-impress-slide-reorder-summary": Path("specs/libreoffice/impress-slide-reorder-summary.yaml"),
    "libreoffice-impress-kpi-chart-pdf": Path("specs/libreoffice/impress-kpi-chart-pdf.yaml"),
    "blender-scene-cube-material": Path("specs/blender/scene-cube-material.yaml"),
    "blender-save-reopen-scene": Path("specs/blender/save-reopen-scene.yaml"),
    "blender-modifier-apply": Path("specs/blender/modifier-apply.yaml"),
    "blender-animation-keyframes": Path("specs/blender/animation-keyframes.yaml"),
}

OPENRA_TASK_REQUIREMENTS: dict[str, dict[str, Any]] = {
    "openra-cnc-gdi01-beachhead": {
        "mod": "cnc",
        "map": "gdi01",
        "title": "01:   Storm the Beachhead",
        "visibility": "MissionSelector",
        "categories": ["Campaign"],
        "target_players": [{"id": "GDI", "playable": True, "required": True, "faction": "gdi"}, {"id": "Nod", "bot": "campaign", "faction": "nod"}],
        "player": {"id": "GDI", "playable": True, "faction": "gdi"},
        "actor": {"actor": "e1", "owner": "Nod", "min_count": 10},
        "objective": {"owner": "GDI", "objective_type": "primary", "objective": "eliminate-nod"},
    },
    "openra-cnc-nod03a-prison-break": {
        "mod": "cnc",
        "map": "nod03a",
        "title": "03a: Sudanese Prison Break",
        "visibility": "MissionSelector",
        "categories": ["Campaign"],
        "target_players": [{"id": "Nod", "playable": True, "required": True, "faction": "nod"}, {"id": "GDI", "bot": "campaign", "faction": "gdi"}],
        "player": {"id": "Nod", "playable": True, "faction": "nod"},
        "actor": {"actor": "jeep", "owner": "GDI", "min_count": 2},
        "objective": {"owner": "Nod", "objective_type": "primary", "objective": "capture-prison"},
    },
    "openra-cnc-hot-box-dropzone": {
        "mod": "cnc",
        "map": "the-hot-box",
        "title": "The Hot Box",
        "visibility": "Lobby",
        "categories": ["Drop Zone"],
        "target_players": [{"id": "Multi0", "playable": True, "faction": "nod"}, {"id": "Multi9", "playable": True, "faction": "nod"}],
        "player": {"id": "Multi0", "playable": True, "faction": "nod"},
        "actor": {"actor": "mpspawn", "owner": "Neutral", "min_count": 10},
    },
    "openra-ra-allies03a-dead-end": {
        "mod": "ra",
        "map": "allies-03a",
        "title": "03a: Dead End",
        "visibility": "MissionSelector",
        "categories": ["Campaign"],
        "target_players": [{"id": "Greece", "playable": True, "required": True, "faction": "allies"}, {"id": "USSR", "bot": "campaign", "faction": "soviet"}],
        "player": {"id": "Greece", "playable": True, "faction": "allies"},
        "actor": {"actor": "e1", "owner": "USSR", "min_count": 20, "match_prefix": True},
        "objective": {"owner": "Greece", "objective_type": "primary", "objective": "destroy-bridges"},
    },
    "openra-ra-soviet05-distant-thunder": {
        "mod": "ra",
        "map": "soviet-05",
        "title": "05:   Distant Thunder",
        "visibility": "MissionSelector",
        "categories": ["Campaign"],
        "target_players": [{"id": "USSR", "playable": True, "required": True, "faction": "soviet"}, {"id": "Greece", "bot": "campaign", "faction": "allies"}, {"id": "France", "bot": "campaign", "faction": "allies"}],
        "player": {"id": "USSR", "playable": True, "faction": "soviet"},
        "actor": {"actor": "2tnk", "owner": "Greece", "location": "59,43", "min_count": 1},
        "objective": {"owner": "USSR", "objective_type": "primary", "objective": "capture-radar-dome"},
    },
    "openra-ra-drop-zone": {
        "mod": "ra",
        "map": "drop-zone",
        "title": "Drop Zone",
        "visibility": "Lobby",
        "categories": ["Drop Zone"],
        "target_players": [{"id": "Multi0", "playable": True, "faction": "soviet"}, {"id": "Multi7", "playable": True, "faction": "soviet"}],
        "player": {"id": "Multi0", "playable": True, "faction": "soviet"},
        "actor": {"actor": "mpspawn", "owner": "Neutral", "min_count": 8},
    },
    "openra-ra-soviet05-war-factory-heavy-tank": {
        "strategy": "gameplay",
        "mod": "ra",
        "map": "soviet-05",
        "title": "05:   Distant Thunder",
        "visibility": "MissionSelector",
        "categories": ["Campaign"],
        "target_players": [{"id": "USSR", "playable": True, "required": True, "faction": "soviet"}],
        "actions": [
            {"type": "openra_deploy_actor", "player": "USSR", "actor_id": "startmcv", "wait_until_actor": "fact"},
            {"type": "openra_build_actor", "player": "USSR", "actor": "powr", "queue": "Building"},
            {"type": "openra_build_actor", "player": "USSR", "actor": "proc", "queue": "Building"},
            {"type": "openra_build_actor", "player": "USSR", "actor": "weap", "queue": "Building"},
            {"type": "openra_build_actor", "player": "USSR", "actor": "fix", "queue": "Building"},
            {"type": "openra_build_actor", "player": "USSR", "actor": "3tnk", "queue": "Vehicle"},
        ],
        "assertions": [
            {"type": "openra_actor_built", "player": "USSR", "actor": "weap"},
            {"type": "openra_actor_built", "player": "USSR", "actor": "fix"},
            {"type": "openra_actor_built", "player": "USSR", "actor": "3tnk"},
            {"type": "openra_production_prerequisite_respected", "player": "USSR", "actor": "3tnk"},
            {"type": "no_openra_gameplay_probe_errors"},
        ],
    },
    "openra-ra-soviet05-barracks-rocket-infantry": {
        "strategy": "gameplay",
        "mod": "ra",
        "map": "soviet-05",
        "title": "05:   Distant Thunder",
        "visibility": "MissionSelector",
        "categories": ["Campaign"],
        "target_players": [{"id": "USSR", "playable": True, "required": True, "faction": "soviet"}],
        "actions": [
            {"type": "openra_deploy_actor", "player": "USSR", "actor_id": "startmcv", "wait_until_actor": "fact"},
            {"type": "openra_build_actor", "player": "USSR", "actor": "powr", "queue": "Building"},
            {"type": "openra_build_actor", "player": "USSR", "actor": "barr", "queue": "Building"},
            {"type": "openra_build_actor", "player": "USSR", "actor": "e1", "queue": "Infantry"},
        ],
        "assertions": [
            {"type": "openra_actor_built", "player": "USSR", "actor": "barr"},
            {"type": "openra_actor_built", "player": "USSR", "actor": "e1"},
            {"type": "openra_production_prerequisite_respected", "player": "USSR", "actor": "e1"},
            {"type": "no_openra_gameplay_probe_errors"},
        ],
    },
    "openra-ra-allies07-soviet-war-factory-produces-heavy-tank": {
        "strategy": "gameplay",
        "mod": "ra",
        "map": "allies-07",
        "title": "07:   Sunken Treasure",
        "visibility": "MissionSelector",
        "categories": ["Campaign"],
        "target_players": [{"id": "Greece", "playable": True, "required": True, "faction": "allies"}, {"id": "USSR", "bot": "campaign", "faction": "soviet"}],
        "actions": [{"type": "openra_build_actor", "player": "USSR", "actor": "3tnk", "queue": "Vehicle"}],
        "assertions": [
            {"type": "openra_actor_exists", "player": "USSR", "actor_id": "USSRWarFactory"},
            {"type": "openra_actor_built", "player": "USSR", "actor": "3tnk"},
            {"type": "openra_production_prerequisite_respected", "player": "USSR", "actor": "3tnk"},
            {"type": "no_openra_gameplay_probe_errors"},
        ],
    },
    "openra-ra-allies05a-soviet-war-factory-produces-vehicle": {
        "strategy": "gameplay",
        "mod": "ra",
        "map": "allies-05a",
        "title": "05a: Tanya's Tale",
        "visibility": "MissionSelector",
        "categories": ["Campaign"],
        "target_players": [{"id": "Greece", "playable": True, "required": True, "faction": "allies"}, {"id": "USSR", "bot": "campaign", "faction": "soviet"}],
        "actions": [{"type": "openra_build_actor", "player": "USSR", "actor": "3tnk", "queue": "Vehicle"}],
        "assertions": [
            {"type": "openra_actor_exists", "player": "USSR", "actor_id": "Warfactory2"},
            {"type": "openra_actor_built", "player": "USSR", "actor": "3tnk"},
            {"type": "openra_production_prerequisite_respected", "player": "USSR", "actor": "3tnk"},
            {"type": "no_openra_gameplay_probe_errors"},
        ],
    },
}

JUPYTERLAB_TASK_REQUIREMENTS: dict[str, dict[str, Any]] = {
    "jupyterlab-text-file-edit": {
        "target": {"kind": "text_file", "path": "notes/warptest.txt", "content": "WarpTest initial\n"},
        "actions": [
            {"type": "jupyterlab_open_file", "path": "notes/warptest.txt"},
            {"type": "jupyterlab_edit_text_file", "path": "notes/warptest.txt", "content": "WarpTest edited\n"},
        ],
        "assertions": [
            {"type": "jupyterlab_file_exists", "path": "notes/warptest.txt"},
            {"type": "jupyterlab_file_text_equals", "path": "notes/warptest.txt", "content": "WarpTest edited\n"},
            {"type": "no_jupyterlab_server_errors"},
        ],
    },
    "jupyterlab-markdown-file-save": {
        "target": {"kind": "text_file", "path": "notes/analysis.md", "content": "# WarpTest\n\nInitial note.\n"},
        "actions": [
            {"type": "jupyterlab_open_file", "path": "notes/analysis.md"},
            {"type": "jupyterlab_edit_text_file", "path": "notes/analysis.md", "content": "# WarpTest\n\nUpdated note.\n"},
        ],
        "assertions": [
            {"type": "jupyterlab_file_text_equals", "path": "notes/analysis.md", "content": "# WarpTest\n\nUpdated note.\n"},
            {"type": "no_jupyterlab_server_errors"},
        ],
    },
    "jupyterlab-notebook-cell-output": {
        "target": {
            "kind": "notebook",
            "path": "notebooks/cell-output.ipynb",
            "cells": [{"cell_type": "code", "source": "print('warptest:42')\n"}],
        },
        "actions": [
            {"type": "jupyterlab_open_file", "path": "notebooks/cell-output.ipynb"},
            {"type": "jupyterlab_run_notebook_cell", "path": "notebooks/cell-output.ipynb", "cell_index": 0},
        ],
        "assertions": [
            {"type": "jupyterlab_notebook_output_contains", "path": "notebooks/cell-output.ipynb", "text": "warptest:42"},
            {"type": "no_jupyterlab_server_errors"},
        ],
    },
    "jupyterlab-notebook-markdown-note": {
        "target": {
            "kind": "notebook",
            "path": "notebooks/markdown-note.ipynb",
            "cells": [{"cell_type": "markdown", "source": "## WarpTest note\n\nSaved through the Contents API.\n"}],
        },
        "actions": [
            {"type": "jupyterlab_open_file", "path": "notebooks/markdown-note.ipynb"},
        ],
        "assertions": [
            {"type": "jupyterlab_file_exists", "path": "notebooks/markdown-note.ipynb"},
            {"type": "no_jupyterlab_server_errors"},
        ],
    },
    "jupyterlab-workspace-layout": {
        "target": {
            "kind": "workspace_layout",
            "workspace": "warptest",
            "paths": ["notes/workspace.md", "notebooks/workspace.ipynb"],
        },
        "actions": [
            {"type": "jupyterlab_open_file", "path": "notes/workspace.md", "workspace": "warptest"},
            {"type": "jupyterlab_open_file", "path": "notebooks/workspace.ipynb", "workspace": "warptest"},
            {
                "type": "jupyterlab_set_workspace_layout",
                "workspace": "warptest",
                "paths": ["notes/workspace.md", "notebooks/workspace.ipynb"],
            },
        ],
        "assertions": [
            {"type": "jupyterlab_workspace_contains", "workspace": "warptest", "path": "notes/workspace.md"},
            {"type": "jupyterlab_workspace_contains", "workspace": "warptest", "path": "notebooks/workspace.ipynb"},
            {"type": "no_jupyterlab_server_errors"},
        ],
    },
    "jupyterlab-session-smoke": {
        "target": {"kind": "terminal_session", "path": "notebooks/session-smoke.ipynb"},
        "actions": [
            {"type": "jupyterlab_create_notebook", "path": "notebooks/session-smoke.ipynb", "cells": []},
            {"type": "jupyterlab_start_session", "path": "notebooks/session-smoke.ipynb", "kernel": "python3"},
        ],
        "assertions": [
            {"type": "jupyterlab_file_exists", "path": "notebooks/session-smoke.ipynb"},
            {"type": "jupyterlab_session_started"},
            {"type": "no_jupyterlab_server_errors"},
        ],
    },
}

LIBREOFFICE_TASK_REQUIREMENTS: dict[str, dict[str, Any]] = {
    "libreoffice-writer-board-report-toc": {'target': {'kind': 'writer_document',
                'path': 'writer-board-report-toc.odt',
                'paragraphs': [{'text': 'Executive Operations Review', 'style': 'Heading 1'},
                               {'text': 'Draft status packet for the board meeting.'},
                               {'text': 'Pipeline Recovery', 'style': 'Heading 2'},
                               {'text': 'Initial recovery narrative is pending owner detail.'},
                               {'text': 'Risk Register', 'style': 'Heading 2'},
                               {'text': 'Security and delivery risks need the consolidated decision log.'},
                               {'text': 'Appendix', 'style': 'Heading 1'}],
                'tables': {'Milestones': [['Milestone', 'Owner', 'Status'], ['Platform audit', 'Mira', 'Open'], ['Launch review', 'Dev', 'At risk']]}},
     'actions': [{'type': 'libreoffice_open_document'},
                 {'type': 'libreoffice_insert_toc', 'title': 'Board Contents', 'include_styles': ['Heading 1', 'Heading 2']},
                 {'type': 'libreoffice_append_paragraph', 'text': 'Q3 recovery owner: Casey. Decision log is ready for board review.'},
                 {'type': 'libreoffice_set_table_cell', 'table': 'Milestones', 'cell': 'C2', 'value': 'Complete'},
                 {'type': 'libreoffice_update_indexes'},
                 {'type': 'libreoffice_save'},
                 {'type': 'libreoffice_reopen'},
                 {'type': 'libreoffice_export_pdf', 'path': 'writer-board-report-toc.pdf'}],
     'assertions': [{'type': 'libreoffice_toc_contains', 'text': 'Pipeline Recovery'},
                    {'type': 'libreoffice_text_contains', 'text': 'Q3 recovery owner: Casey.'},
                    {'type': 'libreoffice_table_cell_equals', 'table': 'Milestones', 'cell': 'C2', 'expected': 'Complete'},
                    {'type': 'libreoffice_pdf_exists', 'path': 'writer-board-report-toc.pdf'},
                    {'type': 'no_libreoffice_errors'}]},
    "libreoffice-writer-invoice-table-rollup": {'target': {'kind': 'writer_document',
                'path': 'writer-invoice-table-rollup.odt',
                'paragraphs': [{'text': 'WarpTest Services Invoice', 'style': 'Heading 1'},
                               {'text': 'Customer: Northstar Labs'},
                               {'text': 'Billing period: Q3 closeout'}],
                'tables': {'Invoice': [['Item', 'Qty', 'Unit', 'Total'],
                                       ['Platform advisory', 4, 150, 600],
                                       ['Support retainer', 2, 80, 160],
                                       ['Subtotal', '', '', 760],
                                       ['Tax', '', '', 76],
                                       ['Grand Total', '', '', 836]]}},
     'actions': [{'type': 'libreoffice_open_document'},
                 {'type': 'libreoffice_insert_table_row', 'table': 'Invoice', 'row': 4, 'values': ['Incident review', 3, 120, 360]},
                 {'type': 'libreoffice_set_table_cell', 'table': 'Invoice', 'cell': 'D5', 'value': 1120},
                 {'type': 'libreoffice_set_table_cell', 'table': 'Invoice', 'cell': 'D6', 'value': 112},
                 {'type': 'libreoffice_set_table_cell', 'table': 'Invoice', 'cell': 'D7', 'value': 1232},
                 {'type': 'libreoffice_append_paragraph', 'text': 'Payment note: revised total includes incident review hours.'},
                 {'type': 'libreoffice_save'},
                 {'type': 'libreoffice_reopen'},
                 {'type': 'libreoffice_export_pdf', 'path': 'writer-invoice-table-rollup.pdf'}],
     'assertions': [{'type': 'libreoffice_table_cell_equals', 'table': 'Invoice', 'cell': 'A4', 'expected': 'Incident review'},
                    {'type': 'libreoffice_table_cell_equals', 'table': 'Invoice', 'cell': 'D7', 'expected': 1232},
                    {'type': 'libreoffice_text_contains', 'text': 'revised total includes incident review hours'},
                    {'type': 'libreoffice_pdf_exists', 'path': 'writer-invoice-table-rollup.pdf'},
                    {'type': 'no_libreoffice_errors'}]},
    "libreoffice-writer-policy-cross-reference": {'target': {'kind': 'writer_document',
                'path': 'writer-policy-cross-reference.odt',
                'paragraphs': [{'text': 'Incident Response Policy', 'style': 'Heading 1'},
                               {'text': 'Purpose', 'style': 'Heading 2'},
                               {'text': 'This policy coordinates response ownership across teams.'},
                               {'text': 'Escalation Matrix', 'style': 'Heading 2'},
                               {'text': 'Severity one events require executive notification.'},
                               {'text': 'Evidence Handling', 'style': 'Heading 2'},
                               {'text': 'Evidence records must remain auditable after handoff.'}]},
     'actions': [{'type': 'libreoffice_open_document'},
                 {'type': 'libreoffice_insert_bookmark', 'name': 'escalation_matrix', 'text': 'Escalation Matrix'},
                 {'type': 'libreoffice_insert_reference_field', 'bookmark': 'escalation_matrix', 'prefix': 'See section: '},
                 {'type': 'libreoffice_apply_paragraph_style', 'text': 'Evidence Handling', 'style': 'Heading 1'},
                 {'type': 'libreoffice_update_indexes'},
                 {'type': 'libreoffice_save'},
                 {'type': 'libreoffice_reopen'}],
     'assertions': [{'type': 'libreoffice_bookmark_exists', 'name': 'escalation_matrix'},
                    {'type': 'libreoffice_reference_text_contains', 'text': 'See section: Escalation Matrix'},
                    {'type': 'libreoffice_paragraph_style_is', 'text': 'Evidence Handling', 'style': 'Heading 1'},
                    {'type': 'no_libreoffice_errors'}]},
    "libreoffice-calc-quarterly-budget-rollup": {'target': {'kind': 'calc_spreadsheet',
                'path': 'calc-quarterly-budget-rollup.ods',
                'sheets': {'January': {'cells': {'A1': 'Category',
                                                 'B1': 'Budget',
                                                 'C1': 'Actual',
                                                 'A2': 'Revenue',
                                                 'B2': 12000,
                                                 'C2': 11800,
                                                 'A3': 'Cost',
                                                 'B3': 7400,
                                                 'C3': 7600}},
                           'February': {'cells': {'A1': 'Category',
                                                  'B1': 'Budget',
                                                  'C1': 'Actual',
                                                  'A2': 'Revenue',
                                                  'B2': 13500,
                                                  'C2': 13200,
                                                  'A3': 'Cost',
                                                  'B3': 7900,
                                                  'C3': 8100}},
                           'March': {'cells': {'A1': 'Category',
                                               'B1': 'Budget',
                                               'C1': 'Actual',
                                               'A2': 'Revenue',
                                               'B2': 14100,
                                               'C2': 13900,
                                               'A3': 'Cost',
                                               'B3': 8200,
                                               'C3': 8400}},
                           'Summary': {'cells': {'A1': 'Metric', 'A2': 'Revenue Actual', 'A3': 'Cost Actual', 'A4': 'Margin'},
                                       'formulas': {'B2': '=SUM(January.C2;February.C2;March.C2)',
                                                    'B3': '=SUM(January.C3;February.C3;March.C3)',
                                                    'B4': '=B2-B3'}}}},
     'actions': [{'type': 'libreoffice_set_cell', 'sheet': 'March', 'cell': 'C2', 'value': 15000},
                 {'type': 'libreoffice_set_cell', 'sheet': 'March', 'cell': 'C3', 'value': 8300},
                 {'type': 'libreoffice_recalculate'},
                 {'type': 'libreoffice_save'},
                 {'type': 'libreoffice_reopen'}],
     'assertions': [{'type': 'libreoffice_cell_value_equals', 'sheet': 'Summary', 'cell': 'B2', 'expected': 40000},
                    {'type': 'libreoffice_cell_value_equals', 'sheet': 'Summary', 'cell': 'B4', 'expected': 16000},
                    {'type': 'libreoffice_sheet_exists', 'sheet': 'March'},
                    {'type': 'no_libreoffice_errors'}]},
    "libreoffice-calc-sales-sort-ranking": {'target': {'kind': 'calc_spreadsheet',
                'path': 'calc-sales-sort-ranking.ods',
                'sheet': 'Sales',
                'cells': {'A1': 'Rep',
                          'B1': 'Region',
                          'C1': 'Revenue',
                          'D1': 'Score',
                          'A2': 'Arun',
                          'B2': 'East',
                          'C2': 9200,
                          'D2': 82,
                          'A3': 'Blair',
                          'B3': 'West',
                          'C3': 13400,
                          'D3': 90,
                          'A4': 'Casey',
                          'B4': 'Central',
                          'C4': 11700,
                          'D4': 86,
                          'A5': 'Devon',
                          'B5': 'East',
                          'C5': 15100,
                          'D5': 94}},
     'actions': [{'type': 'libreoffice_set_cell', 'sheet': 'Sales', 'cell': 'D4', 'value': 96},
                 {'type': 'libreoffice_sort_range', 'sheet': 'Sales', 'range': 'A2:D5', 'key': 'D', 'order': 'descending'},
                 {'type': 'libreoffice_set_formula', 'sheet': 'Sales', 'cell': 'E2', 'formula': '=ROW()-1'},
                 {'type': 'libreoffice_save'},
                 {'type': 'libreoffice_reopen'}],
     'assertions': [{'type': 'libreoffice_cell_value_equals', 'sheet': 'Sales', 'cell': 'A2', 'expected': 'Casey'},
                    {'type': 'libreoffice_cell_value_equals', 'sheet': 'Sales', 'cell': 'D2', 'expected': 96},
                    {'type': 'libreoffice_formula_equals', 'sheet': 'Sales', 'cell': 'E2', 'formula': '=ROW()-1'},
                    {'type': 'no_libreoffice_errors'}]},
    "libreoffice-calc-named-range-forecast": {'target': {'kind': 'calc_spreadsheet',
                'path': 'calc-named-range-forecast.ods',
                'sheets': {'Inputs': {'cells': {'A1': 'Metric',
                                                'B1': 'Value',
                                                'A2': 'BaseRevenue',
                                                'B2': 50000,
                                                'A3': 'GrowthRate',
                                                'B3': 0.08,
                                                'A4': 'CostRate',
                                                'B4': 0.62}},
                           'Forecast': {'cells': {'A1': 'Metric', 'A2': 'Forecast Revenue', 'A3': 'Forecast Cost', 'A4': 'Forecast Profit'},
                                        'formulas': {'B2': '=Inputs.B2*(1+Inputs.B3)', 'B3': '=B2*Inputs.B4', 'B4': '=B2-B3'}}}},
     'actions': [{'type': 'libreoffice_define_named_range', 'name': 'GrowthRate', 'sheet': 'Inputs', 'range': 'B3'},
                 {'type': 'libreoffice_define_named_range', 'name': 'CostRate', 'sheet': 'Inputs', 'range': 'B4'},
                 {'type': 'libreoffice_set_cell', 'sheet': 'Inputs', 'cell': 'B3', 'value': 0.12},
                 {'type': 'libreoffice_recalculate'},
                 {'type': 'libreoffice_save'},
                 {'type': 'libreoffice_reopen'}],
     'assertions': [{'type': 'libreoffice_named_range_exists', 'name': 'GrowthRate'},
                    {'type': 'libreoffice_cell_value_equals', 'sheet': 'Forecast', 'cell': 'B2', 'expected': 56000},
                    {'type': 'libreoffice_cell_value_equals', 'sheet': 'Forecast', 'cell': 'B4', 'expected': 21280},
                    {'type': 'no_libreoffice_errors'}]},
    "libreoffice-calc-chart-refresh-export": {'target': {'kind': 'calc_spreadsheet',
                'path': 'calc-chart-refresh-export.ods',
                'sheet': 'Metrics',
                'cells': {'A1': 'Month', 'B1': 'Revenue', 'A2': 'Jan', 'B2': 12000, 'A3': 'Feb', 'B3': 13500, 'A4': 'Mar', 'B4': 14100, 'A5': 'Apr', 'B5': 15200}},
     'actions': [{'type': 'libreoffice_set_cell', 'sheet': 'Metrics', 'cell': 'B5', 'value': 16800},
                 {'type': 'libreoffice_create_chart', 'sheet': 'Metrics', 'name': 'RevenueTrend', 'range': 'A1:B5', 'chart_type': 'line'},
                 {'type': 'libreoffice_recalculate'},
                 {'type': 'libreoffice_save'},
                 {'type': 'libreoffice_reopen'},
                 {'type': 'libreoffice_export_pdf', 'path': 'calc-chart-refresh-export.pdf'}],
     'assertions': [{'type': 'libreoffice_chart_exists', 'sheet': 'Metrics', 'name': 'RevenueTrend'},
                    {'type': 'libreoffice_cell_value_equals', 'sheet': 'Metrics', 'cell': 'B5', 'expected': 16800},
                    {'type': 'libreoffice_pdf_exists', 'path': 'calc-chart-refresh-export.pdf'},
                    {'type': 'no_libreoffice_errors'}]},
    "libreoffice-impress-status-deck-build": {'target': {'kind': 'impress_presentation',
                'path': 'impress-status-deck-build.odp',
                'slides': [{'title': 'Q3 Delivery Review', 'subtitle': 'Draft deck'},
                           {'title': 'Agenda', 'bullets': ['Delivery health', 'Budget movement', 'Risks']},
                           {'title': 'Delivery Health', 'bullets': ['Green builds', 'Manual QA pending']},
                           {'title': 'Risk Register', 'bullets': ['Security review open', 'Vendor date at risk']}]},
     'actions': [{'type': 'libreoffice_set_slide_title', 'slide': 1, 'title': 'Q3 Delivery Review - Executive Update'},
                 {'type': 'libreoffice_set_slide_bullets', 'slide': 3, 'bullets': ['Green builds', 'Manual QA complete', 'Release candidate signed']},
                 {'type': 'libreoffice_add_shape', 'slide': 4, 'shape': 'callout', 'text': 'Decision needed: approve vendor backup plan'},
                 {'type': 'libreoffice_save'},
                 {'type': 'libreoffice_reopen'}],
     'assertions': [{'type': 'libreoffice_slide_count_equals', 'expected': 4},
                    {'type': 'libreoffice_slide_text_contains', 'slide': 1, 'text': 'Executive Update'},
                    {'type': 'libreoffice_shape_text_contains', 'slide': 4, 'text': 'vendor backup plan'},
                    {'type': 'no_libreoffice_errors'}]},
    "libreoffice-impress-slide-reorder-summary": {'target': {'kind': 'impress_presentation',
                'path': 'impress-slide-reorder-summary.odp',
                'slides': [{'title': 'Appendix Metrics', 'bullets': ['Raw detail follows']},
                           {'title': 'Customer Impact', 'bullets': ['Pilot customers stable', 'Support tickets down']},
                           {'title': 'Executive Summary', 'bullets': ['Draft summary', 'Needs final order']},
                           {'title': 'Mitigation Plan', 'bullets': ['Owner matrix ready']}]},
     'actions': [{'type': 'libreoffice_move_slide', 'from': 3, 'to': 1},
                 {'type': 'libreoffice_set_slide_title', 'slide': 1, 'title': 'Executive Summary - Final Brief'},
                 {'type': 'libreoffice_set_slide_bullets',
                  'slide': 1,
                  'bullets': ['Customer impact stable', 'Mitigation owners assigned', 'Appendix moved to back']},
                 {'type': 'libreoffice_save'},
                 {'type': 'libreoffice_reopen'}],
     'assertions': [{'type': 'libreoffice_slide_count_equals', 'expected': 4},
                    {'type': 'libreoffice_slide_title_equals', 'slide': 1, 'expected': 'Executive Summary - Final Brief'},
                    {'type': 'libreoffice_slide_text_contains', 'slide': 1, 'text': 'Mitigation owners assigned'},
                    {'type': 'no_libreoffice_errors'}]},
    "libreoffice-impress-kpi-chart-pdf": {'target': {'kind': 'impress_presentation',
                'path': 'impress-kpi-chart-pdf.odp',
                'slides': [{'title': 'KPI Snapshot', 'bullets': ['ARR 4.2M', 'NRR 111%', 'Gross margin 68%']},
                           {'title': 'Expansion Motion', 'bullets': ['Pipeline 1.4M', 'Conversion 34%']}]},
     'actions': [{'type': 'libreoffice_set_slide_bullets', 'slide': 1, 'bullets': ['ARR 4.8M', 'NRR 116%', 'Gross margin 70%']},
                 {'type': 'libreoffice_add_shape', 'slide': 1, 'shape': 'kpi-callout', 'text': 'NRR improved to 116% after enterprise renewals'},
                 {'type': 'libreoffice_add_shape', 'slide': 2, 'shape': 'kpi-callout', 'text': 'Pipeline coverage is above 3x target'},
                 {'type': 'libreoffice_save'},
                 {'type': 'libreoffice_reopen'},
                 {'type': 'libreoffice_export_pdf', 'path': 'impress-kpi-chart-pdf.pdf'}],
     'assertions': [{'type': 'libreoffice_slide_text_contains', 'slide': 1, 'text': 'NRR 116%'},
                    {'type': 'libreoffice_shape_text_contains', 'slide': 2, 'text': 'Pipeline coverage'},
                    {'type': 'libreoffice_pdf_exists', 'path': 'impress-kpi-chart-pdf.pdf'},
                    {'type': 'no_libreoffice_errors'}]},
}

BLENDER_TASK_REQUIREMENTS: dict[str, dict[str, Any]] = {
    "blender-scene-cube-material": {
        "target": {
            "kind": "scene",
            "objects": [
                {
                    "name": "WarpCube",
                    "type": "MESH",
                    "primitive": "cube",
                    "size": 2.0,
                    "location": [0.0, 0.0, 0.0],
                    "material": {"name": "WarpRed", "color": [1.0, 0.05, 0.02, 1.0]},
                }
            ],
        },
        "actions": [{"type": "blender_set_object_location", "object": "WarpCube", "location": [1.0, 2.0, 3.0]}],
        "assertions": [
            {"type": "blender_object_exists", "object": "WarpCube", "object_type": "MESH"},
            {"type": "blender_material_color", "object": "WarpCube", "material": "WarpRed", "color": [1.0, 0.05, 0.02, 1.0]},
            {"type": "blender_object_location", "object": "WarpCube", "location": [1.0, 2.0, 3.0]},
            {"type": "no_blender_python_errors"},
        ],
    },
    "blender-save-reopen-scene": {
        "target": {
            "kind": "scene",
            "objects": [
                {
                    "name": "PersistCube",
                    "type": "MESH",
                    "primitive": "cube",
                    "size": 1.5,
                    "location": [-1.0, 0.0, 0.5],
                    "material": {"name": "PersistBlue", "color": [0.05, 0.2, 1.0, 1.0]},
                }
            ],
        },
        "actions": [
            {"type": "blender_save_as", "path": "save-reopen-scene.blend"},
            {"type": "blender_reopen", "path": "save-reopen-scene.blend"},
        ],
        "assertions": [
            {"type": "blender_file_exists", "path": "save-reopen-scene.blend"},
            {"type": "blender_object_exists", "object": "PersistCube", "object_type": "MESH"},
            {"type": "blender_material_color", "object": "PersistCube", "material": "PersistBlue", "color": [0.05, 0.2, 1.0, 1.0]},
            {"type": "no_blender_python_errors"},
        ],
    },
    "blender-modifier-apply": {
        "target": {
            "kind": "scene",
            "objects": [{"name": "BevelCube", "type": "MESH", "primitive": "cube", "size": 2.0, "location": [0.0, 0.0, 0.0]}],
        },
        "actions": [
            {"type": "blender_add_modifier", "object": "BevelCube", "name": "WarpBevel", "modifier_type": "BEVEL", "width": 0.2, "segments": 2},
            {"type": "blender_apply_modifier", "object": "BevelCube", "modifier": "WarpBevel"},
        ],
        "assertions": [
            {"type": "blender_object_exists", "object": "BevelCube", "object_type": "MESH"},
            {"type": "blender_modifier_applied", "object": "BevelCube", "modifier": "WarpBevel", "vertex_count_min": 9},
            {"type": "blender_mesh_geometry", "object": "BevelCube", "vertex_count": 56, "edge_count": 108, "face_count": 54, "volume": 7.736296, "volume_tolerance": 0.001},
            {"type": "no_blender_python_errors"},
        ],
    },
    "blender-animation-keyframes": {
        "target": {
            "kind": "scene",
            "objects": [{"name": "AnimatedCube", "type": "MESH", "primitive": "cube", "size": 1.0, "location": [0.0, 0.0, 0.0]}],
        },
        "actions": [
            {"type": "blender_set_keyframe", "object": "AnimatedCube", "data_path": "location", "frame": 1, "value": [0.0, 0.0, 0.0]},
            {"type": "blender_set_keyframe", "object": "AnimatedCube", "data_path": "location", "frame": 24, "value": [4.0, 0.0, 0.0]},
        ],
        "assertions": [
            {"type": "blender_object_exists", "object": "AnimatedCube", "object_type": "MESH"},
            {"type": "blender_animation_keyframes", "object": "AnimatedCube", "data_path": "location", "frames": [1, 24]},
            {"type": "no_blender_python_errors"},
        ],
    },
}


@dataclass(frozen=True)
class ModelBenchTask:
    slug: str
    title: str
    benchmark: str
    description: str
    repo: str | None = None
    constraints: dict[str, Any] = field(default_factory=dict)


@dataclass
class ModelCallResult:
    content: str
    latency_ms: int


class ModelBenchError(RuntimeError):
    pass


class ModelScopeChatClient:
    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_MODELSCOPE_BASE_URL,
        temperature: float = 0.2,
        max_tokens: int = 4096,
        extra_headers: dict[str, str] | None = None,
        client: Any | None = None,
    ) -> None:
        if not api_key:
            raise ModelBenchError("MODELSCOPE_API_KEY is required for `warptest model-bench`.")
        if client is None:
            from openai import OpenAI

            timeout = float(os.environ.get("WARPTEST_MODEL_BENCH_TIMEOUT", "300"))
            max_retries = int(os.environ.get("WARPTEST_MODEL_BENCH_MAX_RETRIES", "2"))
            kwargs: dict[str, Any] = {
                "api_key": api_key,
                "base_url": base_url,
                "timeout": timeout,
                "max_retries": max_retries,
            }
            if extra_headers:
                kwargs["default_headers"] = dict(extra_headers)
            client = OpenAI(**kwargs)
        self.client = client
        self.base_url = base_url
        self.temperature = temperature
        self.max_tokens = max_tokens

    def generate(self, model: str, prompt: str) -> ModelCallResult:
        start = time.perf_counter()
        response = self.client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": "You generate valid WarpTest YAML specs. Return only one YAML mapping.",
                },
                {"role": "user", "content": prompt},
            ],
            temperature=self.temperature,
            max_tokens=self.max_tokens,
        )
        latency_ms = int((time.perf_counter() - start) * 1000)
        content = response.choices[0].message.content
        if not isinstance(content, str) or not content.strip():
            raise ModelBenchError("Model returned empty content.")
        return ModelCallResult(content=content, latency_ms=latency_ms)


def default_tasks() -> list[ModelBenchTask]:
    return [
        ModelBenchTask(
            slug="2048-merge-left",
            title="2048 merge-left",
            benchmark="2048",
            description=(
                "Create a 2048 spec that restores a 4x4 board with the top row "
                "[2, 2, 0, 0], score 0, bestScore 0, performs one left move, "
                "and verifies a 4 tile appears and score increases by at least 4."
            ),
        ),
        ModelBenchTask(
            slug="2048-near-win",
            title="2048 near-win",
            benchmark="2048",
            description=(
                "Create a 2048 spec that restores a near-win 4x4 board with top row "
                "[1024, 1024, 0, 0] and second row [4, 8, 16, 32], score 4096, "
                "bestScore 4096, performs one left move, and verifies a 2048 tile appears."
            ),
        ),
        ModelBenchTask(
            slug="spacerpg-playable-flight",
            title="SpaceRPG playable-flight",
            benchmark="SpaceRPG",
            description=(
                "Create a SpaceRPG spec that disables the economy backend, reaches playable "
                "flight state in system helion-reach using the existing test hook strategy, "
                "presses W once, and verifies screen remains flight."
            ),
        ),
        ModelBenchTask(
            slug="spacerpg-station-save",
            title="SpaceRPG station-save",
            benchmark="SpaceRPG",
            description=(
                "Create a SpaceRPG spec that disables the economy backend, reaches station "
                "state at station helion-prime in system helion-reach, calls saveGame "
                "for auto slot through the hook, and verifies station screen plus "
                "localStorage key spacerpg-save-slot:auto."
            ),
        ),
        ModelBenchTask(
            slug="love-resource-action",
            title="DatingSim resource-action",
            benchmark="DatingSim",
            description=(
                "Create a DatingSim spec that uses mocked AI, initializes seed 7 for "
                "player WarpTest male at home with money 1000, energy 100, health 100, "
                "mood 100, performs hook action work_coding, and verifies money 1100, "
                "energy 80, no real OpenAI call, and no severe console errors."
            ),
        ),
        ModelBenchTask(
            slug="openra-cnc-gdi01-beachhead",
            title="OpenRA C&C GDI beachhead",
            benchmark="OpenRA",
            description=(
                "Create an OpenRA spec for the cnc mod map gdi01 titled exactly "
                "'01:   Storm the Beachhead' with visibility MissionSelector and "
                "categories ['Campaign']; validate the GDI campaign beachhead map "
                "through openra_utility_map_validate, runs an "
                "openra_validate_map action, and verifies the map, playable GDI player, "
                "Nod e1 actor count, eliminate-nod objective, and no utility errors."
            ),
        ),
        ModelBenchTask(
            slug="openra-ra-drop-zone",
            title="OpenRA RA Drop Zone",
            benchmark="OpenRA",
            description=(
                "Create an OpenRA spec for the ra mod map drop-zone titled exactly "
                "'Drop Zone' with visibility Lobby and categories ['Drop Zone']; "
                "validate the Drop Zone lobby through openra_utility_map_validate, runs an "
                "openra_validate_map action, and verifies the map, playable Multi0 "
                "Soviet player, Neutral mpspawn actor count, and no utility errors."
            ),
        ),
        ModelBenchTask(
            slug="jupyterlab-text-file-edit",
            title="JupyterLab text file edit",
            benchmark="JupyterLab",
            description=(
                "Create a JupyterLab spec that uses jupyterlab_workspace_api_validate, "
                "starts from text file notes/warptest.txt with content 'WarpTest initial\\n', "
                "opens it, edits it to exactly 'WarpTest edited\\n', and verifies the file "
                "exists, the content matches exactly, and no JupyterLab server errors occurred."
            ),
        ),
        ModelBenchTask(
            slug="jupyterlab-notebook-cell-output",
            title="JupyterLab notebook cell output",
            benchmark="JupyterLab",
            description=(
                "Create a JupyterLab spec that uses jupyterlab_workspace_api_validate, "
                "creates notebooks/cell-output.ipynb with one code cell source "
                "\"print('warptest:42')\\n\", opens the notebook, runs cell 0, and verifies "
                "the notebook output contains warptest:42 with no JupyterLab server errors."
            ),
        ),
        ModelBenchTask(
            slug="libreoffice-writer-board-report-toc",
            title="LibreOffice Writer board report TOC",
            benchmark="LibreOffice",
            description=(
                "Create a LibreOffice spec that uses libreoffice_uno_document_validate, "
                "starts from writer-board-report-toc.odt with board-report headings and "
                "a Milestones table, inserts a Board Contents section, appends the Q3 "
                "recovery-owner paragraph, marks Platform audit Complete, saves, reopens, "
                "exports PDF, and verifies the contents, table cell, PDF artifact, and no "
                "LibreOffice errors."
            ),
        ),
        ModelBenchTask(
            slug="libreoffice-calc-quarterly-budget-rollup",
            title="LibreOffice Calc quarterly budget rollup",
            benchmark="LibreOffice",
            description=(
                "Create a LibreOffice Calc spec that uses libreoffice_uno_document_validate, "
                "starts from calc-quarterly-budget-rollup.ods with January, February, March, "
                "and Summary sheets, updates March actuals, recalculates, saves, reopens, "
                "and verifies Summary revenue and margin with no LibreOffice errors."
            ),
        ),
        ModelBenchTask(
            slug="blender-scene-cube-material",
            title="Blender scene cube material",
            benchmark="Blender",
            description=(
                "Create a Blender spec that uses blender_python_scene_validate, starts a visible "
                "GUI checkpoint with cube WarpCube and material WarpRed color [1.0, 0.05, 0.02, 1.0], "
                "moves WarpCube to [1.0, 2.0, 3.0] through bpy/operator automation, and verifies "
                "the object, material color, location, and no Blender Python errors."
            ),
        ),
        ModelBenchTask(
            slug="blender-save-reopen-scene",
            title="Blender save reopen scene",
            benchmark="Blender",
            description=(
                "Create a Blender spec that uses blender_python_scene_validate, starts with cube "
                "PersistCube and PersistBlue material, saves save-reopen-scene.blend under the "
                "report artifact directory, reopens it, and verifies the file, object, material, "
                "and no Blender Python errors."
            ),
        ),
    ]


def load_task_file(path: str | Path) -> list[ModelBenchTask]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("tasks")
    if not isinstance(data, list):
        raise ModelBenchError("Task file must be a YAML/JSON list or a mapping with `tasks:`.")
    tasks = []
    for item in data:
        if not isinstance(item, dict):
            raise ModelBenchError("Each task entry must be a mapping.")
        tasks.append(ModelBenchTask(**item))
    return tasks


def model_slug(model: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "__", model).strip("_")


def repo_for_task(task: ModelBenchTask) -> Path:
    if task.repo:
        return Path(task.repo).expanduser().resolve()
    if task.benchmark in {"libreoffice", "LibreOffice"} and os.environ.get("WARPTEST_LIBREOFFICE_REPO"):
        return Path(os.environ["WARPTEST_LIBREOFFICE_REPO"]).expanduser().resolve()
    repos = {
        "2048": Path("games/2048"),
        "spacerpg": Path("games/SpaceRPG"),
        "SpaceRPG": Path("games/SpaceRPG"),
        "love": Path("games/DatingSim"),
        "DatingSim": Path("games/DatingSim"),
        "openra": Path("games/OpenRA"),
        "OpenRA": Path("games/OpenRA"),
        "jupyterlab": Path("games/jupyterlab"),
        "JupyterLab": Path("games/jupyterlab"),
        "libreoffice": Path("games/LibreOffice"),
        "LibreOffice": Path("games/LibreOffice"),
        "blender": Path("games/blender"),
        "Blender": Path("games/blender"),
    }
    if task.benchmark not in repos:
        raise ModelBenchError(f"No default repository mapping for benchmark: {task.benchmark}")
    return repos[task.benchmark].resolve()


def build_prompt(task: ModelBenchTask, inspect_summary: dict[str, Any]) -> str:
    schema = "\n\n".join(
        [
            _general_yaml_contract(),
            _profile_yaml_contract(task.benchmark),
            _task_yaml_requirements(task),
            _forbidden_yaml_patterns(),
        ]
    )
    return "\n\n".join(
        [
            "Generate exactly one WarpTest YAML spec for the task below.",
            "Return only YAML. Do not output Markdown commentary. Do not explain your reasoning.",
            "The generated YAML will be executed by WarpTest as-is; unknown fields or invented strategy names will fail.",
            f"Task title: {task.title}",
            f"Task description: {task.description}",
            "Repository capability summary:",
            yaml.safe_dump(inspect_summary, sort_keys=True, allow_unicode=False),
            schema,
        ]
    )


def _general_yaml_contract() -> str:
    return """
Required top-level YAML shape:
name: string
benchmark: exactly one of 2048, SpaceRPG, DatingSim, OpenRA, JupyterLab, LibreOffice, Blender
tags: optional list of strings
setup: optional mapping, only with fields listed for the benchmark below
target: mapping, exactly using the fields listed for the benchmark below
checkpoint:
  allowed_strategies: list of exact strategy strings listed for the benchmark below
  expected_validity: exactly one of save-restorable, replay-reachable, injection-only
actions: list of action mappings, each action must have type at the top level
assertions: list of assertion mappings, each assertion must have type at the top level
""".strip()


def _profile_yaml_contract(benchmark: str) -> str:
    if benchmark == "2048":
        return """
Exact executable schema for benchmark 2048:
benchmark: "2048"
target:
  kind: board
  board: 4x4 row-major integer matrix representing the board BEFORE the smoke action
  score: integer score BEFORE the smoke action
  bestScore: integer
checkpoint:
  allowed_strategies: ["localStorage_restore_reload"]
  expected_validity: "save-restorable"
actions:
  - type: move
    direction: left|right|up|down
    wait_ms: integer
assertions may use only:
  - type: tile_value_present
    value: integer
  - type: score_increased_by_at_least
    amount: integer
  - type: no_severe_console
Important: do not put gameState/localStorage under setup; WarpTest builds localStorage from target.board.
""".strip()
    if benchmark == "SpaceRPG":
        return """
Exact executable schema for benchmark SpaceRPG:
benchmark: "SpaceRPG"
setup:
  disable_economy_backend: true
target:
  kind: flight | station
  screen: flight | station
  system_id: helion-reach
  station_id: helion-prime  # required only for station targets
checkpoint:
  allowed_strategies: ["test_hook_jump_and_validate"]
  expected_validity: "replay-reachable"
actions may use only:
  - type: key
    key: "W"
    wait_ms: integer
  - type: hook_call
    method: saveGame
    args: ["auto"]
assertions may use only:
  - type: state_equals
    path: screen
    value: flight | station
  - type: localStorage_key_exists
    key: spacerpg-save-slot:auto
  - type: no_severe_console
Important: do not invent methods such as goToStation, replay, inject, or save-restorable as a strategy.
""".strip()
    if benchmark == "DatingSim":
        return """
Exact executable schema for benchmark DatingSim:
benchmark: "DatingSim"
setup:
  ai_mode: mocked
target:
  kind: deterministic_state
  seed: 7
  player_name: WarpTest
  gender: male
  patch:
    player:
      resources:
        money: integer
        energy: integer
        health: integer
        mood: integer
    world:
      location: home
  expect:
    resources:
      money: integer
      energy: integer
checkpoint:
  allowed_strategies: ["test_hook_seeded_state"]
  expected_validity: "injection-only"
actions:
  - type: hook_action
    action_id: work_coding
    wait_ms: integer >= 0
assertions may use only:
  - type: state_equals
    path: player.resources.money | player.resources.energy
    value: integer
  - type: no_real_openai
  - type: no_severe_console
Important: money and energy assertion paths must be player.resources.money and player.resources.energy.
""".strip()
    if benchmark == "OpenRA":
        return """
Exact executable schema for benchmark OpenRA:
benchmark: "OpenRA"
setup:
  openra_gameplay_probe: optional mapping for gameplay production tasks only
    cash: integer
    fast_build: true
    build_anywhere: false
target:
  kind: openra_map
  mod: cnc | ra
  requires_mod: same value as mod
  map: exact OpenRA map directory id requested by the task
  title: string if known from the task
  visibility: MissionSelector | Lobby if known from the task
  categories: list of strings if known from the task
  players: list of player mappings requested by the task
  actors: list of actor-count mappings requested by the task
  objectives: list of objective mappings requested by the task, when the task is a campaign map
checkpoint:
  allowed_strategies: ["openra_utility_map_validate"] for map-loading tasks OR ["openra_gameplay_production_validate"] for gameplay production tasks
  expected_validity: "replay-reachable"
map-loading actions:
  - type: openra_validate_map
    mod: same value as target.mod
    map: same value as target.map
gameplay production actions:
  - type: openra_wait_ticks
    ticks: integer >= 0
  - type: openra_deploy_actor
    player: player id
    actor_id: named map actor id
    wait_until_actor: actor type produced by deploy
    timeout_ticks: integer
  - type: openra_build_actor
    player: player id
    actor: actor type
    queue: Building | Vehicle | Infantry
    place_at: optional cell string for buildings
    place_near: optional mapping {anchor: "x,y", radius: integer} for buildings
    timeout_ticks: integer
map-loading assertions:
  - type: openra_map_loaded
    mod: same value as target.mod
    map: same value as target.map
  - type: openra_player_exists
    id: player id
    playable: true
    faction: faction id
  - type: openra_actor_count
    actor: actor type
    owner: owner id when required
    min_count: integer
    location: optional cell string
    match_prefix: optional boolean
  - type: openra_objective_declared
    owner: player id
    objective_type: primary | secondary
    objective: objective id
  - type: no_openra_utility_errors
gameplay production assertions:
  - type: openra_actor_built
    player: player id
    actor: actor type
    min_delta: integer
  - type: openra_actor_exists
    player: player id
    actor: actor type OR actor_id: named map actor id
  - type: openra_production_prerequisite_respected
    player: player id
    actor: actor type
  - type: no_openra_gameplay_probe_errors
Important: do not use browser/SDL/menu automation. OpenRA map-loading tasks use native utility validation; gameplay production tasks use the native gameplay probe.
""".strip()
    if benchmark == "JupyterLab":
        return """
Exact executable schema for benchmark JupyterLab:
benchmark: "JupyterLab"
target:
  kind: notebook | text_file | workspace_layout | terminal_session
  path: path under the isolated Jupyter root for notebook/text/session targets
  content: exact text content for text_file targets
  cells: notebook cell list for notebook targets
  workspace: workspace id for workspace_layout targets
  paths: document path list for workspace_layout targets
checkpoint:
  allowed_strategies: ["jupyterlab_workspace_api_validate"]
  expected_validity: "save-restorable"
actions may use only:
  - type: jupyterlab_create_text_file
    path: string
    content: exact text
  - type: jupyterlab_open_file
    path: string
    workspace: optional workspace id
  - type: jupyterlab_edit_text_file
    path: string
    content: exact text
  - type: jupyterlab_create_notebook
    path: string
    cells: list of notebook cells
  - type: jupyterlab_run_notebook_cell
    path: string
    cell_index: integer
  - type: jupyterlab_set_workspace_layout
    workspace: string
    paths: list of strings
  - type: jupyterlab_start_session
    path: string
    kernel: python3
  - type: wait
    ms: integer
assertions may use only:
  - type: jupyterlab_file_exists
    path: string
  - type: jupyterlab_file_text_equals
    path: string
    content: exact text
  - type: jupyterlab_notebook_output_contains
    path: string
    text: expected output substring
  - type: jupyterlab_workspace_contains
    workspace: string
    path: string
  - type: jupyterlab_session_started
  - type: no_jupyterlab_server_errors
Important: do not modify games/jupyterlab, add private hooks, use browser click replay, or write outside the isolated Jupyter root. Use Jupyter Server Contents/session/workspace semantics.
""".strip()
    if benchmark == "LibreOffice":
        return """
Exact executable schema for benchmark LibreOffice:
benchmark: "LibreOffice"
target:
  kind: writer_document | calc_spreadsheet | impress_presentation
  path: ODF artifact path under the isolated LibreOffice runtime artifact directory
  text: exact Writer body text for writer_document targets
  paragraphs: optional Writer paragraph list with text and optional style
  tables: optional Writer table mapping of table name to row arrays
  sheet: Calc sheet name for calc_spreadsheet targets
  cells: optional mapping of Calc cell address to scalar value
  formulas: optional mapping of Calc cell address to formula string
  sheets: optional mapping of Calc sheet name to cells/formulas mappings
  slides: optional Impress slide list with title, subtitle, and bullets
checkpoint:
  allowed_strategies: ["libreoffice_uno_document_validate"]
  expected_validity: "save-restorable"
Writer actions may use only:
  - type: libreoffice_open_document
    path: optional existing ODF path
  - type: libreoffice_replace_text
    old: exact text to replace
    new: replacement text
  - type: libreoffice_append_paragraph
    text: paragraph text
  - type: libreoffice_insert_toc
    title: contents title
    include_styles: list of Writer styles
  - type: libreoffice_update_indexes
  - type: libreoffice_apply_paragraph_style
    text: paragraph text
    style: Writer style name
  - type: libreoffice_insert_bookmark
    name: bookmark name
    text: anchor text
  - type: libreoffice_insert_reference_field
    bookmark: bookmark name
    prefix: reference prefix
  - type: libreoffice_set_table_cell
    table: Writer table name
    cell: table cell address
    value: scalar value
  - type: libreoffice_insert_table_row
    table: Writer table name
    row: 1-based row index to insert before
    values: list of row values
  - type: libreoffice_append_table_row
    table: Writer table name
    values: list of row values
  - type: libreoffice_save
    path: optional ODF path
  - type: libreoffice_export_pdf
    path: PDF artifact path
Calc actions may use only:
  - type: libreoffice_set_cell
    sheet: optional sheet name
    cell: A1-style address
    value: scalar value
  - type: libreoffice_set_formula
    sheet: optional sheet name
    cell: A1-style address
    formula: formula string such as "=SUM(A1:A2)"
  - type: libreoffice_recalculate
  - type: libreoffice_sort_range
    sheet: sheet name
    range: A1:D5 range
    key: sort key column
    order: ascending | descending
  - type: libreoffice_define_named_range
    name: named range name
    sheet: sheet name
    range: cell or range address
  - type: libreoffice_create_chart
    name: chart name
    sheet: sheet name
    range: source range
    chart_type: chart type
  - type: libreoffice_save
    path: optional ODF path
  - type: libreoffice_reopen
    path: optional ODF path
Impress actions may use only:
  - type: libreoffice_set_slide_title
    slide: 1-based slide number
    title: slide title
  - type: libreoffice_set_slide_bullets
    slide: 1-based slide number
    bullets: list of bullet strings
  - type: libreoffice_add_shape
    slide: 1-based slide number
    shape: shape label
    text: shape text
  - type: libreoffice_move_slide
    from: 1-based slide number
    to: 1-based slide number
  - type: libreoffice_save
  - type: libreoffice_reopen
  - type: libreoffice_export_pdf
    path: PDF artifact path
assertions may use only:
  - type: libreoffice_text_contains
    text: expected Writer substring
  - type: libreoffice_toc_contains
    text: expected contents text
  - type: libreoffice_paragraph_style_is
    text: paragraph text
    style: expected Writer style name
  - type: libreoffice_table_cell_equals
    table: Writer table name
    cell: table cell address
    expected: scalar expected value
  - type: libreoffice_bookmark_exists
    name: bookmark name
  - type: libreoffice_reference_text_contains
    text: expected reference substring
  - type: libreoffice_pdf_exists
    path: PDF artifact path
  - type: libreoffice_sheet_exists
    sheet: sheet name
  - type: libreoffice_cell_value_equals
    sheet: optional sheet name
    cell: A1-style address
    expected: scalar expected value
  - type: libreoffice_formula_equals
    sheet: optional sheet name
    cell: A1-style address
    formula: expected formula string
  - type: libreoffice_named_range_exists
    name: named range name
  - type: libreoffice_chart_exists
    sheet: optional sheet name
    name: chart name
  - type: libreoffice_slide_count_equals
    expected: slide count
  - type: libreoffice_slide_title_equals
    slide: 1-based slide number
    expected: expected title
  - type: libreoffice_slide_text_contains
    slide: 1-based slide number
    text: expected slide text
  - type: libreoffice_shape_text_contains
    slide: 1-based slide number
    text: expected shape text
  - type: no_libreoffice_errors
Important: do not modify games/LibreOffice, add private hooks, use GUI click replay, or write outside reports/warptest. Use public UNO/ODF/PDF document semantics through the isolated LibreOffice runtime.
""".strip()
    if benchmark == "Blender":
        return """
Exact executable schema for benchmark Blender:
benchmark: "Blender"
target:
  kind: scene | blend_file
  objects:
    - name: object name
      type: MESH
      primitive: cube | uv_sphere | cylinder | cone
      size: optional float for cube
      location: [x, y, z]
      material:
        name: material name
        color: [r, g, b, a]
checkpoint:
  allowed_strategies: ["blender_python_scene_validate"]
  expected_validity: "save-restorable"
actions may use only:
  - type: blender_add_primitive
    name: object name
    primitive: cube | uv_sphere | cylinder | cone
    location: [x, y, z]
  - type: blender_assign_material
    object: object name
    material: {name: material name, color: [r, g, b, a]}
  - type: blender_set_object_location
    object: object name
    location: [x, y, z]
  - type: blender_add_modifier
    object: object name
    name: modifier name
    modifier_type: BEVEL | SUBSURF | SOLIDIFY | MIRROR | ARRAY
  - type: blender_apply_modifier
    object: object name
    modifier: modifier name
  - type: blender_set_keyframe
    object: object name
    data_path: location
    frame: integer
    value: [x, y, z]
  - type: blender_save_as
    path: relative .blend path under report artifacts
  - type: blender_reopen
    path: relative .blend path under report artifacts
  - type: wait
    ms: integer
assertions may use only:
  - type: blender_object_exists
    object: object name
  - type: blender_material_color
    object: object name
    material: material name
    color: [r, g, b, a]
  - type: blender_object_location
    object: object name
    location: [x, y, z]
  - type: blender_file_exists
    path: relative .blend path
  - type: blender_modifier_exists
    object: object name
    modifier: modifier name
  - type: blender_modifier_applied
    object: object name
    modifier: modifier name
  - type: blender_mesh_geometry
    object: object name
  - type: blender_animation_keyframes
    object: object name
    data_path: location
    frames: list of integers
  - type: no_blender_python_errors
Important: the runtime launches visible Blender GUI as an app bundle and drives official bpy/operator APIs. Do not use --background/headless, pixel clicks, keyboard GUI-agent replay, or modify games/blender source.
""".strip()
    return "No benchmark-specific schema is available."


def _task_yaml_requirements(task: ModelBenchTask) -> str:
    if task.slug in OPENRA_TASK_REQUIREMENTS:
        req = OPENRA_TASK_REQUIREMENTS[task.slug]
        if req.get("strategy") == "gameplay":
            return "\n".join(
                [
                    "Current task required values:",
                    "- target.kind must be openra_map",
                    f"- target.mod and target.requires_mod must be {req['mod']}",
                    f"- target.map must be {req['map']}",
                    f"- target.title must be exactly {req['title']!r}",
                    f"- target.visibility must be {req['visibility']}",
                    f"- target.categories must include {req['categories']}",
                    f"- target.players must include {req['target_players']}",
                    "- checkpoint.allowed_strategies must include openra_gameplay_production_validate",
                    f"- actions must include these gameplay steps: {req['actions']}",
                    f"- assertions must include these gameplay checks: {req['assertions']}",
                ]
            )
        lines = [
            "Current task required values:",
            "- target.kind must be openra_map",
            f"- target.mod and target.requires_mod must be {req['mod']}",
            f"- target.map must be {req['map']}",
            f"- target.title must be exactly {req['title']!r}",
            f"- target.visibility must be {req['visibility']}",
            f"- target.categories must include {req['categories']}",
            f"- target.players must include {req['target_players']}",
            "- checkpoint.allowed_strategies must include openra_utility_map_validate",
            f"- actions must include {{type: openra_validate_map, mod: {req['mod']}, map: {req['map']}}}",
            f"- assertions must include {{type: openra_map_loaded, mod: {req['mod']}, map: {req['map']}}}",
            f"- assertions must include the player check {req['player']}",
            f"- assertions must include the actor-count check {req['actor']}",
            "- assertions must include {type: no_openra_utility_errors}",
        ]
        if "objective" in req:
            lines.append(f"- assertions must include the objective check {req['objective']}")
        return "\n".join(lines)

    if task.slug in JUPYTERLAB_TASK_REQUIREMENTS:
        req = JUPYTERLAB_TASK_REQUIREMENTS[task.slug]
        lines = [
            "Current task required values:",
            "- benchmark must be JupyterLab",
            "- checkpoint.allowed_strategies must include jupyterlab_workspace_api_validate",
            "- checkpoint.expected_validity must be save-restorable",
            f"- target must include {req['target']}",
        ]
        lines.extend(f"- actions must include {action}" for action in req["actions"])
        lines.extend(f"- assertions must include {assertion}" for assertion in req["assertions"])
        return "\n".join(lines)

    if task.slug in LIBREOFFICE_TASK_REQUIREMENTS:
        req = LIBREOFFICE_TASK_REQUIREMENTS[task.slug]
        lines = [
            "Current task required values:",
            "- benchmark must be LibreOffice",
            "- checkpoint.allowed_strategies must include libreoffice_uno_document_validate",
            "- checkpoint.expected_validity must be save-restorable",
            f"- target must include {req['target']}",
        ]
        lines.extend(f"- actions must include {action}" for action in req["actions"])
        lines.extend(f"- assertions must include {assertion}" for assertion in req["assertions"])
        return "\n".join(lines)

    if task.slug in BLENDER_TASK_REQUIREMENTS:
        req = BLENDER_TASK_REQUIREMENTS[task.slug]
        lines = [
            "Current task required values:",
            "- benchmark must be Blender",
            "- checkpoint.allowed_strategies must include blender_python_scene_validate",
            "- checkpoint.expected_validity must be save-restorable",
            f"- target must include {req['target']}",
        ]
        lines.extend(f"- actions must include {action}" for action in req["actions"])
        lines.extend(f"- assertions must include {assertion}" for assertion in req["assertions"])
        return "\n".join(lines)

    requirements = {
        "2048-merge-left": """
Current task required values:
- target.kind must be board
- target.board must be [[2, 2, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0]]
- target.score must be 0
- target.bestScore must be 0
- actions must include {type: move, direction: left}
- assertions must include {type: tile_value_present, value: 4}
- assertions must include {type: score_increased_by_at_least, amount: 4}
""",
        "2048-near-win": """
Current task required values:
- target.kind must be board
- target.board must be [[1024, 1024, 0, 0], [4, 8, 16, 32], [0, 0, 0, 0], [0, 0, 0, 0]]
- target.score must be 4096
- target.bestScore must be 4096
- actions must include {type: move, direction: left}
- assertions must include {type: tile_value_present, value: 2048}
""",
        "spacerpg-playable-flight": """
Current task required values:
- setup.disable_economy_backend must be true
- target.kind must be flight
- target.screen must be flight
- target.system_id must be helion-reach
- actions must include {type: key, key: "W"}
- assertions must include {type: state_equals, path: screen, value: flight}
""",
        "spacerpg-station-save": """
Current task required values:
- setup.disable_economy_backend must be true
- target.kind must be station
- target.screen must be station
- target.system_id must be helion-reach
- target.station_id must be helion-prime
- actions must include {type: hook_call, method: saveGame, args: ["auto"]}
- assertions must include {type: state_equals, path: screen, value: station}
- assertions must include {type: localStorage_key_exists, key: spacerpg-save-slot:auto}
""",
        "love-resource-action": """
Current task required values:
- setup.ai_mode must be mocked
- target.kind must be deterministic_state
- target.seed must be 7
- target.player_name must be WarpTest
- target.gender must be male
- target.patch.player.resources must set money 1000, energy 100, health 100, mood 100
- target.patch.world.location must be home
- target.expect.resources must set money 1000 and energy 100
- actions must include {type: hook_action, action_id: work_coding}
- assertions must include {type: state_equals, path: player.resources.money, value: 1100}
- assertions must include {type: state_equals, path: player.resources.energy, value: 80}
- assertions must include {type: no_real_openai}
""",
    }
    return requirements.get(task.slug, "No task-specific required values.").strip()


def _forbidden_yaml_patterns() -> str:
    return """
Forbidden common mistakes:
- Do not wrap actions as "2048:" or "SpaceRPG:"; action.type must be at the action mapping top level.
- Do not put localStorage or gameState under setup for 2048.
- Do not put the post-action board in target; target is the checkpoint BEFORE the action.
- Do not copy repository capability summary into target.
- Do not use strategy names save-restore, save-restorable, replay, inject, or injection-only inside allowed_strategies.
- Do not invent hook methods like goToStation or restoreState.
- Do not use paths game.currentScreen, money, or energy when exact state paths are specified above.
- Do not use OpenRA SDL menu, GUI click, screenshot, or replay strategy names; use the exact OpenRA strategy and action types listed for the task.
- Do not modify games/jupyterlab, add private hooks, or use browser click replay for JupyterLab; use Contents/session/workspace API action types only.
- Do not modify games/LibreOffice, add private hooks, or use GUI click replay for LibreOffice; use UNO/ODF action types only.
- Do not run Blender with --background/headless or use pixel click/keyboard GUI replay; use visible GUI plus bpy/operator API action types only.
""".strip()


def inspect_prompt_summary(repo: Path) -> dict[str, Any]:
    result = RepositoryInspector().inspect(repo)
    caps = asdict(result.capabilities)
    keep_caps = {
        key: caps[key]
        for key in [
            "browser_runtime",
            "vite",
            "react",
            "vanilla_dom",
            "three",
            "react_three_fiber",
            "local_storage",
            "backend_or_external_api",
            "deterministic_seed",
            "hook_available",
            "hook_name",
            "zustand_detected",
            "zustand_runtime_access_method",
        ]
        if key in caps
    }
    return {
        "repo_name": result.metadata.repo_name,
        "profile": result.profile,
        "adapter_layers": result.adapter_layers,
        "capabilities": keep_caps,
        "storage_policy": asdict(result.storage_policy),
        "instrumentation_mode": result.instrumentation_mode,
    }


def extract_yaml_mapping(text: str) -> dict[str, Any]:
    body = text.strip()
    fence = re.search(r"```(?:yaml|yml)?\s*(.*?)```", body, flags=re.IGNORECASE | re.DOTALL)
    if fence:
        body = fence.group(1).strip()
    data = yaml.safe_load(body)
    if not isinstance(data, dict):
        raise ModelBenchError("Model output did not parse to a YAML mapping.")
    return data


def dump_spec(spec: dict[str, Any]) -> str:
    return yaml.safe_dump(spec, sort_keys=False, allow_unicode=False)


def attach_canonical_visual_oracle(spec: dict[str, Any], task: ModelBenchTask) -> dict[str, Any]:
    canonical_path = CANONICAL_SPEC_BY_TASK.get(task.slug)
    if canonical_path is None:
        return spec
    canonical_path = canonical_path.resolve()
    canonical = yaml.safe_load(canonical_path.read_text(encoding="utf-8"))
    oracle_value = visual_oracle(canonical) if isinstance(canonical, dict) else None
    if not isinstance(oracle_value, dict):
        raise ModelBenchError(f"Canonical spec for {task.slug} is missing visual_oracle: {canonical_path}")
    oracle = copy.deepcopy(oracle_value)
    for key in ("golden_image", "golden_before", "golden_after", "live_fallback_image"):
        if key not in oracle:
            continue
        golden = Path(str(oracle.get(key, ""))).expanduser()
        if not golden.is_absolute():
            oracle[key] = str((canonical_path.parent / golden).resolve())
    updated = copy.deepcopy(spec)
    updated["visual_oracle"] = oracle
    return updated


def validate_task_conformance(spec: dict[str, Any], task: ModelBenchTask) -> tuple[bool, list[str]]:
    errors: list[str] = []
    for key in ["name", "benchmark", "target", "checkpoint", "actions", "assertions"]:
        if key not in spec:
            errors.append(f"Missing required key: {key}")
    if errors:
        return False, errors
    if not _benchmark_matches(task.benchmark, spec.get("benchmark")):
        errors.append(f"Benchmark mismatch: expected {task.benchmark}, got {spec.get('benchmark')}")
    if not isinstance(spec.get("target"), dict):
        errors.append("target must be a mapping")
    if not isinstance(spec.get("checkpoint"), dict):
        errors.append("checkpoint must be a mapping")
    if not isinstance(spec.get("actions"), list):
        errors.append("actions must be a list")
    if not isinstance(spec.get("assertions"), list):
        errors.append("assertions must be a list")
    if errors:
        return False, errors

    checks = {
        "2048-merge-left": _check_2048_merge_left,
        "2048-near-win": _check_2048_near_win,
        "spacerpg-playable-flight": _check_spacerpg_playable_flight,
        "spacerpg-station-save": _check_spacerpg_station_save,
        "love-resource-action": _check_love_resource_action,
        **{slug: _check_openra_task for slug in OPENRA_TASK_REQUIREMENTS},
        **{slug: _check_jupyterlab_task for slug in JUPYTERLAB_TASK_REQUIREMENTS},
        **{slug: _check_libreoffice_task for slug in LIBREOFFICE_TASK_REQUIREMENTS},
        **{slug: _check_blender_task for slug in BLENDER_TASK_REQUIREMENTS},
    }
    checker = checks.get(task.slug)
    if checker:
        if (
            task.slug in OPENRA_TASK_REQUIREMENTS
            or task.slug in JUPYTERLAB_TASK_REQUIREMENTS
            or task.slug in LIBREOFFICE_TASK_REQUIREMENTS
            or task.slug in BLENDER_TASK_REQUIREMENTS
        ):
            errors.extend(checker(spec, task.slug))
        else:
            errors.extend(checker(spec))
    return not errors, errors


def _benchmark_matches(expected: str, actual: Any) -> bool:
    aliases = {
        "2048": {"2048"},
        "SpaceRPG": {"SpaceRPG", "spacerpg", "spacerpg"},
        "DatingSim": {"DatingSim", "datingsim", "love"},
        "OpenRA": {"OpenRA", "openra"},
        "JupyterLab": {"JupyterLab", "jupyterlab"},
        "LibreOffice": {"LibreOffice", "libreoffice"},
        "Blender": {"Blender", "blender"},
    }
    return str(actual) in aliases.get(expected, {expected})


def _strategy_includes(spec: dict[str, Any], strategy: str) -> bool:
    allowed = spec.get("checkpoint", {}).get("allowed_strategies", [])
    return isinstance(allowed, list) and strategy in allowed


def _board_equals(actual: Any, expected: list[list[int]]) -> bool:
    try:
        return [[int(value) for value in row] for row in actual] == expected
    except Exception:
        return False


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _contains_partial(items: Any, expected: dict[str, Any]) -> bool:
    if not isinstance(items, list):
        return False
    for item in items:
        if isinstance(item, dict) and all(item.get(key) == value for key, value in expected.items()):
            return True
    return False


def _target_contains_object(target: dict[str, Any], expected: dict[str, Any]) -> bool:
    return _contains_partial(target.get("objects"), expected)


def _contains_hook_save_auto(items: Any) -> bool:
    if not isinstance(items, list):
        return False
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "hook_call" and item.get("method") == "saveGame" and item.get("args") == ["auto"]:
            return True
    return False


def _check_2048_merge_left(spec: dict[str, Any]) -> list[str]:
    errors = []
    target = spec["target"]
    if target.get("kind") != "board":
        errors.append("2048 target.kind must be board")
    if not _board_equals(target.get("board"), [[2, 2, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0]]):
        errors.append("2048 merge-left board mismatch")
    if _as_int(target.get("score")) != 0 or _as_int(target.get("bestScore")) != 0:
        errors.append("2048 merge-left score/bestScore must be 0")
    if not _strategy_includes(spec, "localStorage_restore_reload"):
        errors.append("2048 must use localStorage_restore_reload")
    if not _contains_partial(spec["actions"], {"type": "move", "direction": "left"}):
        errors.append("2048 merge-left must perform left move")
    if not _contains_partial(spec["assertions"], {"type": "tile_value_present", "value": 4}):
        errors.append("2048 merge-left must assert tile 4")
    if not _contains_partial(spec["assertions"], {"type": "score_increased_by_at_least", "amount": 4}):
        errors.append("2048 merge-left must assert score increase >= 4")
    return errors


def _check_2048_near_win(spec: dict[str, Any]) -> list[str]:
    errors = []
    target = spec["target"]
    if target.get("kind") != "board":
        errors.append("2048 target.kind must be board")
    if not _board_equals(target.get("board"), [[1024, 1024, 0, 0], [4, 8, 16, 32], [0, 0, 0, 0], [0, 0, 0, 0]]):
        errors.append("2048 near-win board mismatch")
    if _as_int(target.get("score")) != 4096 or _as_int(target.get("bestScore")) != 4096:
        errors.append("2048 near-win score/bestScore must be 4096")
    if not _strategy_includes(spec, "localStorage_restore_reload"):
        errors.append("2048 must use localStorage_restore_reload")
    if not _contains_partial(spec["actions"], {"type": "move", "direction": "left"}):
        errors.append("2048 near-win must perform left move")
    if not _contains_partial(spec["assertions"], {"type": "tile_value_present", "value": 2048}):
        errors.append("2048 near-win must assert tile 2048")
    return errors


def _check_spacerpg_playable_flight(spec: dict[str, Any]) -> list[str]:
    errors = []
    target = spec["target"]
    setup = spec.get("setup", {})
    if setup.get("disable_economy_backend") is not True:
        errors.append("SpaceRPG specs must disable economy backend")
    if target.get("kind") != "flight" or target.get("screen") != "flight":
        errors.append("SpaceRPG playable-flight target must be flight screen")
    if target.get("system_id") != "helion-reach":
        errors.append("SpaceRPG playable-flight system_id must be helion-reach")
    if not _strategy_includes(spec, "test_hook_jump_and_validate"):
        errors.append("SpaceRPG must use test_hook_jump_and_validate")
    if not _contains_partial(spec["actions"], {"type": "key", "key": "W"}):
        errors.append("SpaceRPG playable-flight must press W")
    if not _contains_partial(spec["assertions"], {"type": "state_equals", "path": "screen", "value": "flight"}):
        errors.append("SpaceRPG playable-flight must assert screen flight")
    return errors


def _check_spacerpg_station_save(spec: dict[str, Any]) -> list[str]:
    errors = []
    target = spec["target"]
    setup = spec.get("setup", {})
    if setup.get("disable_economy_backend") is not True:
        errors.append("SpaceRPG specs must disable economy backend")
    if target.get("kind") != "station" or target.get("screen") != "station":
        errors.append("SpaceRPG station-save target must be station screen")
    if target.get("system_id") != "helion-reach" or target.get("station_id") != "helion-prime":
        errors.append("SpaceRPG station-save system/station mismatch")
    if not _strategy_includes(spec, "test_hook_jump_and_validate"):
        errors.append("SpaceRPG must use test_hook_jump_and_validate")
    if not _contains_hook_save_auto(spec["actions"]):
        errors.append("SpaceRPG station-save must call saveGame auto")
    if not _contains_partial(spec["assertions"], {"type": "state_equals", "path": "screen", "value": "station"}):
        errors.append("SpaceRPG station-save must assert screen station")
    if not _contains_partial(spec["assertions"], {"type": "localStorage_key_exists", "key": "spacerpg-save-slot:auto"}):
        errors.append("SpaceRPG station-save must assert auto save localStorage key")
    return errors


def _check_love_resource_action(spec: dict[str, Any]) -> list[str]:
    errors = []
    setup = spec.get("setup", {})
    target = spec["target"]
    patch = target.get("patch", {})
    resources = patch.get("player", {}).get("resources", {})
    expect = target.get("expect", {}).get("resources", {})
    if setup.get("ai_mode") != "mocked":
        errors.append("DatingSim must use mocked AI")
    if target.get("kind") != "deterministic_state":
        errors.append("DatingSim target must be deterministic_state")
    if target.get("seed") is not None and _as_int(target.get("seed")) != 7:
        errors.append("DatingSim target seed must be 7 when declared")
    if target.get("player_name") != "WarpTest" or target.get("gender") != "male":
        errors.append("DatingSim player must be WarpTest male")
    for key, value in {"money": 1000, "energy": 100, "health": 100, "mood": 100}.items():
        if resources.get(key) != value:
            errors.append(f"DatingSim initial resource mismatch: {key}")
    if patch.get("world", {}).get("location") != "home":
        errors.append("DatingSim location must be home")
    for key, value in {"money": 1000, "energy": 100}.items():
        if expect.get(key) != value:
            errors.append(f"DatingSim expected resource mismatch: {key}")
    if not _strategy_includes(spec, "test_hook_seeded_state"):
        errors.append("DatingSim must use test_hook_seeded_state")
    if not _contains_partial(spec["actions"], {"type": "hook_action", "action_id": "work_coding"}):
        errors.append("DatingSim must perform work_coding")
    if not _contains_partial(spec["assertions"], {"type": "state_equals", "path": "player.resources.money", "value": 1100}):
        errors.append("DatingSim must assert money 1100")
    if not _contains_partial(spec["assertions"], {"type": "state_equals", "path": "player.resources.energy", "value": 80}):
        errors.append("DatingSim must assert energy 80")
    if not _contains_partial(spec["assertions"], {"type": "no_real_openai"}):
        errors.append("DatingSim must assert no_real_openai")
    return errors


def _check_openra_task(spec: dict[str, Any], slug: str) -> list[str]:
    errors = []
    req = OPENRA_TASK_REQUIREMENTS[slug]
    target = spec["target"]
    if target.get("kind") != "openra_map":
        errors.append("OpenRA target.kind must be openra_map")
    if target.get("mod") != req["mod"] or target.get("requires_mod") != req["mod"]:
        errors.append(f"OpenRA target.mod/requires_mod must be {req['mod']}")
    if target.get("map") != req["map"]:
        errors.append(f"OpenRA target.map must be {req['map']}")
    if target.get("title") != req["title"]:
        errors.append(f"OpenRA target.title must be {req['title']}")
    if target.get("visibility") != req["visibility"]:
        errors.append(f"OpenRA target.visibility must be {req['visibility']}")
    categories = target.get("categories")
    if not isinstance(categories, list) or not all(category in categories for category in req["categories"]):
        errors.append(f"OpenRA target.categories must include {req['categories']}")
    target_players = target.get("players")
    for player in req["target_players"]:
        if not _contains_partial(target_players, player):
            errors.append(f"OpenRA target.players must include {player}")
    if req.get("strategy") == "gameplay":
        if not _strategy_includes(spec, "openra_gameplay_production_validate"):
            errors.append("OpenRA gameplay tasks must use openra_gameplay_production_validate")
        for action in req["actions"]:
            if not _contains_partial(spec["actions"], action):
                errors.append(f"OpenRA gameplay actions must include {action}")
        for assertion in req["assertions"]:
            if not _contains_partial(spec["assertions"], assertion):
                errors.append(f"OpenRA gameplay assertions must include {assertion}")
        return errors

    if not _strategy_includes(spec, "openra_utility_map_validate"):
        errors.append("OpenRA must use openra_utility_map_validate")
    if not _contains_partial(spec["actions"], {"type": "openra_validate_map", "mod": req["mod"], "map": req["map"]}):
        errors.append("OpenRA must run openra_validate_map for the requested mod/map")
    assertions = spec["assertions"]
    if not _contains_partial(assertions, {"type": "openra_map_loaded", "mod": req["mod"], "map": req["map"]}):
        errors.append("OpenRA must assert openra_map_loaded for the requested mod/map")
    if not _contains_partial(assertions, {"type": "openra_player_exists", **req["player"]}):
        errors.append("OpenRA must assert the required player exists")
    if not _contains_partial(assertions, {"type": "openra_actor_count", **req["actor"]}):
        errors.append("OpenRA must assert the required actor count")
    if "objective" in req and not _contains_partial(assertions, {"type": "openra_objective_declared", **req["objective"]}):
        errors.append("OpenRA must assert the required objective")
    if not _contains_partial(assertions, {"type": "no_openra_utility_errors"}):
        errors.append("OpenRA must assert no_openra_utility_errors")
    return errors


def _check_jupyterlab_task(spec: dict[str, Any], slug: str) -> list[str]:
    errors = []
    req = JUPYTERLAB_TASK_REQUIREMENTS[slug]
    target = spec["target"]
    if not _strategy_includes(spec, "jupyterlab_workspace_api_validate"):
        errors.append("JupyterLab must use jupyterlab_workspace_api_validate")
    if spec.get("checkpoint", {}).get("expected_validity") != "save-restorable":
        errors.append("JupyterLab expected_validity must be save-restorable")
    for key, value in req["target"].items():
        if target.get(key) != value:
            errors.append(f"JupyterLab target.{key} must be {value!r}")
    for action in req["actions"]:
        if not _contains_partial(spec["actions"], action):
            errors.append(f"JupyterLab actions must include {action}")
    for assertion in req["assertions"]:
        if not _contains_partial(spec["assertions"], assertion):
            errors.append(f"JupyterLab assertions must include {assertion}")
    return errors


def _check_libreoffice_task(spec: dict[str, Any], slug: str) -> list[str]:
    errors = []
    req = LIBREOFFICE_TASK_REQUIREMENTS[slug]
    target = spec["target"]
    if not _strategy_includes(spec, "libreoffice_uno_document_validate"):
        errors.append("LibreOffice must use libreoffice_uno_document_validate")
    if spec.get("checkpoint", {}).get("expected_validity") != "save-restorable":
        errors.append("LibreOffice expected_validity must be save-restorable")
    for key, value in req["target"].items():
        if target.get(key) != value:
            errors.append(f"LibreOffice target.{key} must be {value!r}")
    for action in req["actions"]:
        if not _contains_partial(spec["actions"], action):
            errors.append(f"LibreOffice actions must include {action}")
    for assertion in req["assertions"]:
        if not _contains_partial(spec["assertions"], assertion):
            errors.append(f"LibreOffice assertions must include {assertion}")
    return errors


def _check_blender_task(spec: dict[str, Any], slug: str) -> list[str]:
    errors = []
    req = BLENDER_TASK_REQUIREMENTS[slug]
    target = spec["target"]
    if not _strategy_includes(spec, "blender_python_scene_validate"):
        errors.append("Blender must use blender_python_scene_validate")
    if spec.get("checkpoint", {}).get("expected_validity") != "save-restorable":
        errors.append("Blender expected_validity must be save-restorable")
    if target.get("kind") != req["target"]["kind"]:
        errors.append(f"Blender target.kind must be {req['target']['kind']}")
    for obj in req["target"].get("objects", []):
        if not _target_contains_object(target, obj):
            errors.append(f"Blender target.objects must include {obj}")
    for action in req["actions"]:
        if not _contains_partial(spec["actions"], action):
            errors.append(f"Blender actions must include {action}")
    for assertion in req["assertions"]:
        if not _contains_partial(spec["assertions"], assertion):
            errors.append(f"Blender assertions must include {assertion}")
    return errors


def run_model_benchmark(
    *,
    models: list[str] | None = None,
    tasks: list[ModelBenchTask] | None = None,
    repeats: int = 1,
    headless: bool = True,
    api_key: str | None = None,
    base_url: str | None = None,
    temperature: float = 0.2,
    max_tokens: int = 4096,
    quiet: bool = False,
    oracle_policy: str = "visual",
    visual_sample_rate: float = 0.0,
    visual_on_semantic_failure: bool = False,
    cascade_config: CascadePolicyConfig | None = None,
    run_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    models = models or DEFAULT_MODELS
    tasks = tasks or default_tasks()
    if repeats < 1:
        raise ModelBenchError("repeats must be >= 1")
    from .providers import resolve_model

    api_key = api_key or os.environ.get("MODELSCOPE_API_KEY", "")
    base_url = base_url or os.environ.get("MODELSCOPE_BASE_URL", DEFAULT_MODELSCOPE_BASE_URL)

    # Build one client per resolved provider so a single run can mix gateways via
    # `model@provider` tags. Untagged ids use the run-level default (ModelScope).
    client_cache: dict[str, ModelScopeChatClient] = {}

    def client_for(model_spec: str) -> tuple[str, ModelScopeChatClient]:
        clean_model, provider = resolve_model(
            model_spec, default_base_url=base_url, default_api_key_env="MODELSCOPE_API_KEY"
        )
        if provider.name not in client_cache:
            provider_key = api_key if provider.name == "default" else os.environ.get(provider.api_key_env, "")
            client_cache[provider.name] = ModelScopeChatClient(
                api_key=provider_key,
                base_url=provider.base_url,
                temperature=temperature,
                max_tokens=max_tokens,
                extra_headers=provider.extra_headers,
            )
        return clean_model, client_cache[provider.name]

    out_dir = new_report_dir()
    cascade = cascade_config or cascade_policy_config()
    total_runs = len(models) * len(tasks) * repeats
    if not quiet:
        print(f"[model-bench] report_dir={out_dir}", flush=True)
        print(f"[model-bench] runs={total_runs} models={len(models)} tasks={len(tasks)} repeats={repeats}", flush=True)
    results: list[dict[str, Any]] = []
    run_number = 0
    for model in models:
        clean_model, client = client_for(model)
        for task in tasks:
            repo = repo_for_task(task)
            inspect_summary = inspect_prompt_summary(repo)
            prompt = build_prompt(task, inspect_summary)
            for repeat_index in range(repeats):
                run_number += 1
                result = _run_one_model_task(
                    client=client,
                    model=clean_model,
                    model_spec=model,
                    task=task,
                    repo=repo,
                    prompt=prompt,
                    repeat_index=repeat_index,
                    out_dir=out_dir,
                    headless=headless,
                    quiet=quiet,
                    run_number=run_number,
                    total_runs=total_runs,
                    oracle_policy=oracle_policy,
                    visual_sample_rate=visual_sample_rate,
                    visual_on_semantic_failure=visual_on_semantic_failure,
                    cascade_config=cascade,
                )
                results.append(result)
    report = {
        "positioning": auxiliary_positioning("model-bench"),
        "provider": "modelscope",
        "base_url": base_url,
        "models": models,
        "repeats": repeats,
        "oracle_policy": oracle_policy,
        "visual_sample_rate": visual_sample_rate,
        "visual_on_semantic_failure": visual_on_semantic_failure,
        "cascade_policy": cascade.cascade_policy,
        "visual_cache": cascade.cache_mode,
        "tasks": [asdict(task) for task in tasks],
        "results": results,
        "summary": summarize_results(results, models),
        "report_dir": str(out_dir),
    }
    run_context.stamp_payload(report, run_meta=run_meta, experiment_id="model_bench")
    write_model_benchmark_report(report, out_dir)
    return report


def _run_one_model_task(
    *,
    client: ModelScopeChatClient,
    model: str,
    model_spec: str | None = None,
    task: ModelBenchTask,
    repo: Path,
    prompt: str,
    repeat_index: int,
    out_dir: Path,
    headless: bool,
    quiet: bool,
    run_number: int,
    total_runs: int,
    oracle_policy: str = "visual",
    visual_sample_rate: float = 0.0,
    visual_on_semantic_failure: bool = False,
    cascade_config: CascadePolicyConfig | None = None,
) -> dict[str, Any]:
    run_id = f"{task.slug}-r{repeat_index + 1}"
    item_dir = out_dir / "model-runs" / model_slug(model) / run_id
    item_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = item_dir / "prompt.txt"
    raw_path = item_dir / "raw_output.txt"
    generated_path = item_dir / "generated.yaml"
    result_path = item_dir / "result.json"
    prompt_path.write_text(prompt, encoding="utf-8")
    prefix = f"[{run_number}/{total_runs}] {model} {task.slug} r{repeat_index + 1}"
    _progress(quiet, f"{prefix}: start")
    result: dict[str, Any] = {
        "model": model,
        "model_spec": model_spec or model,
        "task": task.slug,
        "repeat_index": repeat_index,
        "oracle_policy": oracle_policy,
        "visual_sample_rate": visual_sample_rate,
        "visual_on_semantic_failure": visual_on_semantic_failure,
        "cascade_policy": cascade_config.cascade_policy if cascade_config else None,
        "status": "pending",
        "prompt_path": str(prompt_path),
        "raw_output_path": str(raw_path),
        "generated_spec_path": None,
        "run_report_path": None,
        "model_latency_ms": None,
        "parse_success": False,
        "task_conformance": False,
        "run_success": False,
        "validation_success": False,
        "smoke_success": False,
        "oracle_success": False,
        "visual_success": False,
        "target_validated_before_smoke": False,
        "time_to_target_ms": None,
        "error": None,
    }
    try:
        _progress(quiet, f"{prefix}: calling model")
        call = client.generate(model, prompt)
        result["model_latency_ms"] = call.latency_ms
        raw_path.write_text(call.content, encoding="utf-8")
        _progress(quiet, f"{prefix}: model returned in {call.latency_ms}ms")
    except Exception as error:
        error_text = f"MODEL_CALL_FAILED: {error}"
        raw_path.write_text(error_text, encoding="utf-8")
        result.update({"status": "model_call_failed", "error": str(error)})
        write_json(result_path, result)
        _progress(quiet, f"{prefix}: model_call_failed {error}")
        return result

    try:
        spec = extract_yaml_mapping(raw_path.read_text(encoding="utf-8"))
        result["parse_success"] = True
        generated_path.write_text(dump_spec(spec), encoding="utf-8")
        result["generated_spec_path"] = str(generated_path)
        _progress(quiet, f"{prefix}: parse ok")
    except Exception as error:
        result.update({"status": "parse_failed", "error": str(error)})
        write_json(result_path, result)
        _progress(quiet, f"{prefix}: parse_failed {error}")
        return result

    conforms, errors = validate_task_conformance(spec, task)
    result["task_conformance"] = conforms
    if not conforms:
        result.update({"status": "conformance_failed", "error": "; ".join(errors)})
        write_json(result_path, result)
        _progress(quiet, f"{prefix}: conformance_failed {result['error']}")
        return result
    _progress(quiet, f"{prefix}: conformance ok")
    try:
        spec = attach_canonical_visual_oracle(spec, task)
        generated_path.write_text(dump_spec(spec), encoding="utf-8")
    except Exception as error:
        result.update({"status": "conformance_failed", "error": str(error)})
        write_json(result_path, result)
        _progress(quiet, f"{prefix}: visual_oracle_attach_failed {error}")
        return result

    run_dir = item_dir / "run"
    try:
        _progress(quiet, f"{prefix}: running generated spec")
        report = run_spec(
            generated_path,
            repo_arg=str(repo),
            report_dir=run_dir,
            headless=headless,
            oracle_policy=oracle_policy,
            visual_sample_rate=visual_sample_rate,
            visual_on_semantic_failure=visual_on_semantic_failure,
            cascade_config=cascade_config,
        )
        run_report_path = run_dir / "report.json"
        result["run_report_path"] = str(run_report_path)
        result["run_success"] = report.overall_status == "success"
        result["validation_success"] = report.validation.status == "success"
        result["smoke_success"] = report.smoke_execution.status == "success"
        result["oracle_success"] = report.oracle_result.status == "success"
        result["visual_success"] = report.visual_oracle_result.status == "success"
        result["semantic_status"] = report.semantic_status
        result["visual_audit_status"] = report.visual_audit_status
        result["visual_audit_reason"] = report.visual_audit_reason
        result["target_validated_before_smoke"] = report.target_validated_before_smoke
        result["time_to_target_ms"] = report.metrics.get("time_to_target_ms")
        semantic_success = result["validation_success"] and result["smoke_success"] and result["oracle_success"]
        result["status"] = "success" if result["run_success"] else ("visual_failed" if semantic_success and not result["visual_success"] else "run_failed")
        if not result["run_success"]:
            result["error"] = report.visual_oracle_result.detail if result["status"] == "visual_failed" else report.overall_status
    except Exception as error:
        result.update({"status": "run_failed", "error": str(error)})
    write_json(result_path, result)
    status = result["status"]
    details = (
        f"validation={_yes(result['validation_success'])} "
        f"smoke={_yes(result['smoke_success'])} "
        f"oracle={_yes(result['oracle_success'])}"
        f" visual={_yes(result['visual_success'])}"
    )
    _progress(quiet, f"{prefix}: {status} {details}")
    return result


def _progress(quiet: bool, message: str) -> None:
    if not quiet:
        print(message, flush=True)


def summarize_results(results: list[dict[str, Any]], models: list[str]) -> dict[str, Any]:
    summary: dict[str, Any] = {"models": {}}
    for model in models:
        rows = [row for row in results if row.get("model_spec", row.get("model")) == model]
        latencies = [row["model_latency_ms"] for row in rows if isinstance(row.get("model_latency_ms"), int)]
        target_times = [row["time_to_target_ms"] for row in rows if isinstance(row.get("time_to_target_ms"), int)]
        summary["models"][model] = {
            "attempts": len(rows),
            "successes": sum(1 for row in rows if row.get("status") == "success"),
            "model_call_failures": sum(1 for row in rows if row.get("status") == "model_call_failed"),
            "parse_failures": sum(1 for row in rows if not row.get("parse_success")),
            "conformance_failures": sum(
                1 for row in rows if row.get("parse_success") and not row.get("task_conformance")
            ),
            "run_failures": sum(
                1
                for row in rows
                if row.get("parse_success")
                and row.get("task_conformance")
                and not row.get("run_success")
                and not (row.get("validation_success") and row.get("smoke_success") and row.get("oracle_success"))
            ),
            "visual_failures": sum(
                1
                for row in rows
                if row.get("parse_success")
                and row.get("task_conformance")
                and row.get("validation_success")
                and row.get("smoke_success")
                and row.get("oracle_success")
                and not row.get("visual_success")
            ),
            "avg_model_latency_ms": int(sum(latencies) / len(latencies)) if latencies else None,
            "avg_time_to_target_ms": int(sum(target_times) / len(target_times)) if target_times else None,
        }
    return summary


def write_model_benchmark_report(report: dict[str, Any], out_dir: Path) -> tuple[Path, Path]:
    json_path = out_dir / "model-bench.json"
    md_path = out_dir / "model-bench.md"
    write_json(json_path, report)
    md_path.write_text(_model_bench_markdown(report), encoding="utf-8")
    return json_path, md_path


def _model_bench_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# WarpTest Model Benchmark",
        "",
        f"- Paper role: `{report.get('positioning', {}).get('paper_role', 'unspecified')}`",
        f"- Primary claim evidence: `{report.get('positioning', {}).get('primary_claim', False)}`",
        f"- Note: {report.get('positioning', {}).get('note', '')}",
        f"- Provider: `{report['provider']}`",
        f"- Base URL: `{report['base_url']}`",
        f"- Repeats: `{report['repeats']}`",
        "",
        "## Model Summary",
        "",
        "| Model | Success | Parse Fail | Conformance Fail | Run Fail | Visual Fail | Avg Model Latency ms | Avg Time-to-Target ms |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for model, row in report["summary"]["models"].items():
        lines.append(
            "| "
            + " | ".join(
                [
                    model,
                    f"{row['successes']}/{row['attempts']}",
                    str(row["parse_failures"]),
                    str(row["conformance_failures"]),
                    str(row["run_failures"]),
                    str(row["visual_failures"]),
                    str(row["avg_model_latency_ms"]),
                    str(row["avg_time_to_target_ms"]),
                ]
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "## Task Results",
            "",
            "| Model | Task | Status | Parse | Conform | Run | Validate | Smoke | Oracle | Visual | Error |",
            "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---|",
        ]
    )
    for row in report["results"]:
        lines.append(
            "| "
            + " | ".join(
                [
                    str(row.get("model")),
                    str(row.get("task", "")),
                    str(row.get("status")),
                    _yes(row.get("parse_success")),
                    _yes(row.get("task_conformance")),
                    _yes(row.get("run_success")),
                    _yes(row.get("validation_success")),
                    _yes(row.get("smoke_success")),
                    _yes(row.get("oracle_success")),
                    _yes(row.get("visual_success")),
                    str(row.get("error") or "").replace("|", "\\|"),
                ]
            )
            + " |"
        )
    return "\n".join(lines) + "\n"


def _yes(value: Any) -> str:
    return "1" if bool(value) else "0"
