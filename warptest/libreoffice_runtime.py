from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
import time
import uuid
from pathlib import Path
from typing import Any

from .reports import write_json
from .runtime import RuntimeErrorWithLog


HELPER_SOURCE = r'''
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

try:
    import uno
    from com.sun.star.beans import PropertyValue
    from com.sun.star.connection import NoConnectException
except Exception as error:
    print(
        "LibreOffice UNO import failed. Configure WARPTEST_LIBREOFFICE_PYTHON, "
        "WARPTEST_LIBREOFFICE_SOFFICE, URE_BOOTSTRAP, and PYTHONPATH so Python can import uno/pyuno. "
        f"Import error: {error}",
        file=sys.stderr,
    )
    raise


def prop(name, value):
    item = PropertyValue()
    item.Name = name
    item.Value = value
    return item


def file_url(path):
    return uno.systemPathToFileUrl(str(Path(path).resolve()))


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, data):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def start_office(soffice, user_dir, pipe_name, stdout_path, stderr_path, headless=True):
    user_dir.mkdir(parents=True, exist_ok=True)
    args = [
        soffice,
        "--nologo",
        "--nodefault",
        "--nofirststartwizard",
        "--norestore",
        "--quickstart=no",
        f"--accept=pipe,name={pipe_name};urp;",
        "-env:UserInstallation=" + file_url(user_dir),
    ]
    if headless:
        args.insert(1, "--headless")
    stdout = Path(stdout_path).open("w", encoding="utf-8")
    stderr = Path(stderr_path).open("w", encoding="utf-8")
    proc = subprocess.Popen(args, stdout=stdout, stderr=stderr, text=True)
    return proc, stdout, stderr


def connect(pipe_name, proc):
    local_ctx = uno.getComponentContext()
    resolver = local_ctx.ServiceManager.createInstanceWithContext("com.sun.star.bridge.UnoUrlResolver", local_ctx)
    url = f"uno:pipe,name={pipe_name};urp;StarOffice.ComponentContext"
    deadline = time.time() + 45
    last_error = ""
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"soffice exited before UNO connection was ready with code {proc.returncode}")
        try:
            return resolver.resolve(url)
        except NoConnectException as error:
            last_error = str(error)
            time.sleep(0.4)
    raise RuntimeError(f"Timed out connecting to LibreOffice UNO pipe {pipe_name}: {last_error}")


class LibreOfficeCheckpoint:
    def __init__(self, request):
        self.request = request
        self.spec = request["spec"]
        self.spec_path = Path(request["spec_path"])
        self.runtime_root = Path(request["runtime_root"])
        self.artifact_dir = self.runtime_root / "artifacts"
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        self.visual_capture = bool(request.get("visual_capture"))
        self.screenshots_dir = self.runtime_root / "screenshots"
        if self.visual_capture:
            self.screenshots_dir.mkdir(parents=True, exist_ok=True)
        self.ctx = None
        self.desktop = None
        self.doc = None
        self.doc_kind = None
        self.current_path = None
        self.screenshots = {}
        self.errors = []
        self.soffice_pid = request.get("soffice_pid")

    def attach(self, ctx):
        self.ctx = ctx
        self.desktop = ctx.ServiceManager.createInstanceWithContext("com.sun.star.frame.Desktop", ctx)

    def _load_properties(self):
        return (prop("Hidden", True),)

    def run(self):
        checks = []
        self.run_check(checks, "target.state_synthesized", lambda: self.restore_target(self.spec.get("target") or {}))
        self.run_check(checks, "target.validate.kind", lambda: self.validate_target(self.spec.get("target") or {}))
        if self.visual_capture:
            self.run_check(checks, "visual.screenshot_before", lambda: self._capture_visual("before"))
        for index, action in enumerate(self.spec.get("actions") or []):
            self.run_check(checks, f"action[{action.get('type', 'unknown')}]@{index}", lambda action=action: self.run_action(action))
        if self.visual_capture:
            self.run_check(checks, "visual.screenshot_after", lambda: self._capture_visual("after"))
        for index, assertion in enumerate(self.spec.get("assertions") or []):
            self.run_check(
                checks,
                f"assertion[{assertion.get('type', 'unknown')}]@{index}",
                lambda assertion=assertion: self.run_assertion(assertion),
            )
        status = "success" if all(check.get("status") == "success" for check in checks) else "failure"
        return {
            "status": status,
            "checks": checks,
            "document_path": str(self.current_path) if self.current_path else None,
            "artifact_dir": str(self.artifact_dir),
            "screenshots": dict(self.screenshots),
            "screenshot_before": self.screenshots.get("before"),
            "screenshot_after": self.screenshots.get("after"),
        }

    def run_check(self, checks, name, func):
        start = time.perf_counter()
        try:
            detail, data = func()
            checks.append(
                {
                    "name": name,
                    "status": "success",
                    "detail": detail,
                    "data": data,
                    "duration_ms": int((time.perf_counter() - start) * 1000),
                }
            )
        except Exception as error:
            self.errors.append(f"{name}: {error}")
            checks.append(
                {
                    "name": name,
                    "status": "failure",
                    "detail": str(error),
                    "data": {},
                    "duration_ms": int((time.perf_counter() - start) * 1000),
                }
            )

    def restore_target(self, target):
        kind = target.get("kind")
        if kind == "writer_document":
            self.doc_kind = "writer"
            self.doc = self.desktop.loadComponentFromURL("private:factory/swriter", "_blank", 0, self._load_properties())
            paragraphs = target.get("paragraphs")
            if isinstance(paragraphs, list) and paragraphs:
                for index, paragraph in enumerate(paragraphs):
                    if index:
                        self._append_writer_text("\n")
                    text = str(paragraph.get("text", ""))
                    self._append_writer_text(text)
                    style = paragraph.get("style")
                    if style:
                        self._apply_style_to_text(text, str(style))
            else:
                self._append_writer_text(str(target.get("text", "")))
            for table_name, rows in (target.get("tables") or {}).items():
                self._insert_writer_table(str(table_name), rows or [])
            self.current_path = self._target_path(target, ".odt")
            self._save_doc(self.current_path)
            return "LibreOffice Writer document synthesized.", {"path": str(self.current_path), "kind": kind}
        if kind == "calc_spreadsheet":
            self.doc_kind = "calc"
            self.doc = self.desktop.loadComponentFromURL("private:factory/scalc", "_blank", 0, self._load_properties())
            if isinstance(target.get("sheets"), dict):
                for sheet_name, sheet_data in target["sheets"].items():
                    self._ensure_sheet(str(sheet_name))
                    for address, value in (sheet_data.get("cells") or {}).items():
                        self._set_cell(str(address), value, sheet_name=str(sheet_name))
                    for address, formula in (sheet_data.get("formulas") or {}).items():
                        self._set_formula(str(address), str(formula), sheet_name=str(sheet_name))
            else:
                sheet_name = str(target.get("sheet", "Sheet1"))
                self._ensure_sheet(sheet_name)
                for address, value in (target.get("cells") or {}).items():
                    self._set_cell(str(address), value, sheet_name=sheet_name)
                for address, formula in (target.get("formulas") or {}).items():
                    self._set_formula(str(address), str(formula), sheet_name=sheet_name)
            self._calculate()
            self.current_path = self._target_path(target, ".ods")
            self._save_doc(self.current_path)
            return "LibreOffice Calc spreadsheet synthesized.", {"path": str(self.current_path), "kind": kind}
        if kind == "impress_presentation":
            self.doc_kind = "impress"
            self.doc = self.desktop.loadComponentFromURL("private:factory/simpress", "_blank", 0, self._load_properties())
            slides = target.get("slides") or []
            self._reset_impress_slides(len(slides))
            for index, slide in enumerate(slides, start=1):
                page = self._ensure_slide(index)
                if slide.get("title"):
                    self._set_slide_title(index, str(slide["title"]))
                if slide.get("subtitle"):
                    self._add_slide_text(index, str(slide["subtitle"]), role="subtitle")
                if slide.get("bullets"):
                    self._set_slide_bullets(index, slide.get("bullets") or [])
            self.current_path = self._target_path(target, ".odp")
            self._save_doc(self.current_path)
            return "LibreOffice Impress presentation synthesized.", {"path": str(self.current_path), "kind": kind}
        raise ValueError(f"Unsupported LibreOffice target.kind: {kind!r}")

    def validate_target(self, target):
        expected_kind = target.get("kind")
        actual = (
            "writer_document"
            if self.doc_kind == "writer"
            else "calc_spreadsheet"
            if self.doc_kind == "calc"
            else "impress_presentation"
            if self.doc_kind == "impress"
            else None
        )
        if actual != expected_kind:
            raise AssertionError(f"Expected document kind {expected_kind}, got {actual}")
        if self.current_path and not self.current_path.exists():
            raise AssertionError(f"Expected document path to exist: {self.current_path}")
        return "LibreOffice target kind and artifact path validated.", {"kind": actual, "path": str(self.current_path)}

    def run_action(self, action):
        action_type = action.get("type")
        if action_type == "libreoffice_open_document":
            path = Path(action.get("path") or self.current_path)
            self._open_doc(path)
            return "Document opened through UNO.", {"path": str(path)}
        if action_type == "libreoffice_replace_text":
            self._require_kind("writer")
            old = str(action["old"])
            new = str(action["new"])
            count = self._replace_text(old, new)
            return "Writer text replaced.", {"old": old, "new": new, "count": count}
        if action_type == "libreoffice_append_paragraph":
            self._require_kind("writer")
            text = str(action["text"])
            self._append_writer_text("\n" + text)
            return "Writer paragraph appended.", {"text": text}
        if action_type == "libreoffice_insert_toc":
            self._require_kind("writer")
            title = str(action.get("title") or "Contents")
            headings = self._collect_target_headings(action.get("include_styles") or [])
            self._append_writer_text("\n" + title + "\n" + "\n".join(headings))
            return "Writer contents section inserted through UNO text operations.", {"title": title, "headings": headings}
        if action_type == "libreoffice_update_indexes":
            self._update_document_indexes()
            return "LibreOffice document indexes updated when available.", {}
        if action_type == "libreoffice_apply_paragraph_style":
            self._require_kind("writer")
            self._apply_style_to_text(str(action["text"]), str(action["style"]))
            return "Writer paragraph style applied.", {"text": action["text"], "style": action["style"]}
        if action_type == "libreoffice_insert_bookmark":
            self._require_kind("writer")
            name = str(action["name"])
            text = str(action["text"])
            self._insert_bookmark(name, text)
            return "Writer bookmark inserted.", {"name": name, "text": text}
        if action_type == "libreoffice_insert_reference_field":
            self._require_kind("writer")
            bookmark = str(action["bookmark"])
            prefix = str(action.get("prefix") or "")
            self._insert_reference_field(bookmark, prefix=prefix)
            return "Writer reference text inserted.", {"bookmark": bookmark, "prefix": prefix}
        if action_type == "libreoffice_set_table_cell":
            self._require_kind("writer")
            self._set_writer_table_cell(str(action["table"]), str(action["cell"]), action.get("value"))
            return "Writer table cell updated.", {"table": action["table"], "cell": action["cell"], "value": action.get("value")}
        if action_type == "libreoffice_insert_table_row":
            self._require_kind("writer")
            self._insert_writer_table_row(str(action["table"]), int(action["row"]), action.get("values") or [])
            return "Writer table row inserted.", {"table": action["table"], "row": action["row"], "values": action.get("values") or []}
        if action_type == "libreoffice_append_table_row":
            self._require_kind("writer")
            self._append_writer_table_row(str(action["table"]), action.get("values") or [])
            return "Writer table row appended.", {"table": action["table"], "values": action.get("values") or []}
        if action_type == "libreoffice_set_cell":
            self._require_kind("calc")
            self._set_cell(str(action["cell"]), action.get("value"), sheet_name=action.get("sheet", "Sheet1"))
            return "Calc cell value set.", {"cell": action["cell"], "value": action.get("value")}
        if action_type == "libreoffice_set_formula":
            self._require_kind("calc")
            self._set_formula(str(action["cell"]), str(action["formula"]), sheet_name=action.get("sheet", "Sheet1"))
            return "Calc formula set.", {"cell": action["cell"], "formula": action["formula"]}
        if action_type == "libreoffice_recalculate":
            self._require_kind("calc")
            self._calculate()
            return "Calc formulas recalculated.", {}
        if action_type == "libreoffice_sort_range":
            self._require_kind("calc")
            self._sort_range(
                str(action["range"]),
                sheet_name=str(action.get("sheet", "Sheet1")),
                key=str(action["key"]),
                descending=str(action.get("order", "ascending")).lower() == "descending",
            )
            return "Calc range sorted.", {"sheet": action.get("sheet", "Sheet1"), "range": action["range"], "key": action["key"]}
        if action_type == "libreoffice_define_named_range":
            self._require_kind("calc")
            self._define_named_range(str(action["name"]), str(action["range"]), sheet_name=str(action.get("sheet", "Sheet1")))
            return "Calc named range defined.", {"name": action["name"], "sheet": action.get("sheet", "Sheet1"), "range": action["range"]}
        if action_type == "libreoffice_create_chart":
            self._require_kind("calc")
            self._create_chart(
                str(action["name"]),
                str(action["range"]),
                sheet_name=str(action.get("sheet", "Sheet1")),
                chart_type=str(action.get("chart_type", "line")),
            )
            return "Calc chart created.", {"name": action["name"], "sheet": action.get("sheet", "Sheet1"), "range": action["range"]}
        if action_type == "libreoffice_set_slide_title":
            self._require_kind("impress")
            self._set_slide_title(int(action["slide"]), str(action["title"]))
            return "Impress slide title set.", {"slide": action["slide"], "title": action["title"]}
        if action_type == "libreoffice_set_slide_bullets":
            self._require_kind("impress")
            self._set_slide_bullets(int(action["slide"]), action.get("bullets") or [])
            return "Impress slide bullets set.", {"slide": action["slide"], "bullets": action.get("bullets") or []}
        if action_type == "libreoffice_add_shape":
            self._require_kind("impress")
            self._add_slide_text(int(action["slide"]), str(action["text"]), role=str(action.get("shape") or "shape"))
            return "Impress shape text added.", {"slide": action["slide"], "text": action["text"], "shape": action.get("shape")}
        if action_type == "libreoffice_move_slide":
            self._require_kind("impress")
            self._move_slide(int(action["from"]), int(action["to"]))
            return "Impress slide moved.", {"from": action["from"], "to": action["to"]}
        if action_type == "libreoffice_save":
            path = Path(action.get("path") or self.current_path)
            self._save_doc(path)
            self.current_path = path
            return "LibreOffice document saved.", {"path": str(path)}
        if action_type == "libreoffice_reopen":
            path = Path(action.get("path") or self.current_path)
            self._open_doc(path)
            return "LibreOffice document reopened.", {"path": str(path)}
        if action_type == "libreoffice_export_pdf":
            path = self._artifact_path(str(action.get("path") or f"{self.spec_path.stem}.pdf"))
            filter_name = {
                "writer": "writer_pdf_Export",
                "calc": "calc_pdf_Export",
                "impress": "impress_pdf_Export",
            }.get(self.doc_kind)
            if not filter_name:
                raise AssertionError(f"Cannot export unsupported LibreOffice kind {self.doc_kind!r} to PDF")
            self.doc.storeToURL(file_url(path), (prop("FilterName", filter_name),))
            return "LibreOffice document exported to PDF.", {"path": str(path), "filter": filter_name}
        raise ValueError(f"Unsupported LibreOffice action type: {action_type!r}")

    def run_assertion(self, assertion):
        assertion_type = assertion.get("type")
        if assertion_type == "libreoffice_text_contains":
            self._require_kind("writer")
            text = str(assertion["text"])
            body = self.doc.Text.String
            if text not in body:
                raise AssertionError(f"Writer text does not contain {text!r}")
            return "Writer text contains expected substring.", {"text": text}
        if assertion_type == "libreoffice_toc_contains":
            self._require_kind("writer")
            text = str(assertion["text"])
            body = self.doc.Text.String
            if text not in body:
                raise AssertionError(f"Writer contents section does not contain {text!r}")
            return "Writer contents section contains expected text.", {"text": text}
        if assertion_type == "libreoffice_paragraph_style_is":
            self._require_kind("writer")
            text = str(assertion["text"])
            expected = str(assertion["style"])
            actual = self._paragraph_style_for_text(text)
            if actual != expected:
                raise AssertionError(f"Paragraph style for {text!r} is {actual!r}, expected {expected!r}")
            return "Writer paragraph style matched.", {"text": text, "style": actual}
        if assertion_type == "libreoffice_table_cell_equals":
            self._require_kind("writer")
            actual = self._writer_table_cell_text(str(assertion["table"]), str(assertion["cell"]))
            expected = assertion.get("expected")
            if not self._values_equal(actual, expected):
                raise AssertionError(
                    f"Writer table {assertion['table']} cell {assertion['cell']} is {actual!r}, expected {expected!r}"
                )
            return "Writer table cell matched.", {"table": assertion["table"], "cell": assertion["cell"], "actual": actual}
        if assertion_type == "libreoffice_bookmark_exists":
            self._require_kind("writer")
            name = str(assertion["name"])
            if not self._bookmark_exists(name):
                raise AssertionError(f"Writer bookmark does not exist: {name}")
            return "Writer bookmark exists.", {"name": name}
        if assertion_type == "libreoffice_reference_text_contains":
            self._require_kind("writer")
            text = str(assertion["text"])
            if text not in self.doc.Text.String:
                raise AssertionError(f"Writer reference text does not contain {text!r}")
            return "Writer reference text contains expected substring.", {"text": text}
        if assertion_type == "libreoffice_pdf_exists":
            path = self._artifact_path(str(assertion["path"]))
            if not path.exists() or path.stat().st_size <= 0:
                raise AssertionError(f"Expected non-empty PDF at {path}")
            return "PDF artifact exists.", {"path": str(path), "bytes": path.stat().st_size}
        if assertion_type == "libreoffice_sheet_exists":
            self._require_kind("calc")
            sheet = str(assertion.get("sheet", "Sheet1"))
            self._sheet(sheet)
            return "Calc sheet exists.", {"sheet": sheet}
        if assertion_type == "libreoffice_cell_value_equals":
            self._require_kind("calc")
            sheet_name = str(assertion.get("sheet", "Sheet1"))
            cell = self._cell(str(assertion["cell"]), sheet_name=sheet_name)
            expected = assertion.get("expected")
            actual = self._cell_assertion_value(cell, sheet_name, expected)
            if not self._values_equal(actual, expected):
                raise AssertionError(f"Cell {assertion['cell']} value is {actual!r}, expected {expected!r}")
            return "Calc cell value matched.", {"cell": assertion["cell"], "actual": actual}
        if assertion_type == "libreoffice_formula_equals":
            self._require_kind("calc")
            cell = self._cell(str(assertion["cell"]), sheet_name=assertion.get("sheet", "Sheet1"))
            expected = str(assertion["formula"])
            if cell.Formula != expected:
                raise AssertionError(f"Cell {assertion['cell']} formula is {cell.Formula!r}, expected {expected!r}")
            return "Calc formula matched.", {"cell": assertion["cell"], "formula": cell.Formula}
        if assertion_type == "libreoffice_named_range_exists":
            self._require_kind("calc")
            name = str(assertion["name"])
            if not self._named_range_exists(name):
                raise AssertionError(f"Calc named range does not exist: {name}")
            return "Calc named range exists.", {"name": name}
        if assertion_type == "libreoffice_chart_exists":
            self._require_kind("calc")
            name = str(assertion["name"])
            sheet_name = str(assertion.get("sheet", "Sheet1"))
            if not self._chart_exists(name, sheet_name=sheet_name):
                raise AssertionError(f"Calc chart does not exist: {sheet_name}.{name}")
            return "Calc chart exists.", {"name": name, "sheet": sheet_name}
        if assertion_type == "libreoffice_slide_count_equals":
            self._require_kind("impress")
            expected = int(assertion["expected"])
            actual = self.doc.getDrawPages().getCount()
            if actual != expected:
                raise AssertionError(f"Impress slide count is {actual}, expected {expected}")
            return "Impress slide count matched.", {"actual": actual}
        if assertion_type == "libreoffice_slide_title_equals":
            self._require_kind("impress")
            slide = int(assertion["slide"])
            expected = str(assertion["expected"])
            actual = self._slide_title(slide)
            if actual != expected:
                raise AssertionError(f"Slide {slide} title is {actual!r}, expected {expected!r}")
            return "Impress slide title matched.", {"slide": slide, "title": actual}
        if assertion_type == "libreoffice_slide_text_contains":
            self._require_kind("impress")
            slide = int(assertion["slide"])
            text = str(assertion["text"])
            body = self._slide_text(slide)
            if text not in body:
                raise AssertionError(f"Slide {slide} text does not contain {text!r}")
            return "Impress slide text contains expected substring.", {"slide": slide, "text": text}
        if assertion_type == "libreoffice_shape_text_contains":
            self._require_kind("impress")
            slide = int(assertion["slide"])
            text = str(assertion["text"])
            body = self._slide_text(slide)
            if text not in body:
                raise AssertionError(f"Slide {slide} shape text does not contain {text!r}")
            return "Impress shape text contains expected substring.", {"slide": slide, "text": text}
        if assertion_type == "no_libreoffice_errors":
            if self.errors:
                raise AssertionError("; ".join(self.errors))
            return "No LibreOffice runtime errors were recorded.", {}
        raise ValueError(f"Unsupported LibreOffice assertion type: {assertion_type!r}")

    def _target_path(self, target, suffix):
        return self._artifact_path(str(target.get("path") or f"{self.spec_path.stem}{suffix}"))

    def _artifact_path(self, raw_path):
        path = Path(raw_path)
        if not path.is_absolute():
            path = self.artifact_dir / path
        path.parent.mkdir(parents=True, exist_ok=True)
        return path.resolve()

    def _require_kind(self, kind):
        if self.doc_kind != kind:
            raise AssertionError(f"Current LibreOffice document kind is {self.doc_kind!r}, expected {kind!r}")

    def _save_doc(self, path):
        path = self._artifact_path(str(path))
        self.doc.storeAsURL(file_url(path), ())
        self.current_path = path

    def _open_doc(self, path):
        path = self._artifact_path(str(path))
        if not path.exists():
            raise FileNotFoundError(path)
        if self.doc is not None:
            try:
                self.doc.close(True)
            except Exception:
                self.doc.dispose()
        self.doc = self.desktop.loadComponentFromURL(file_url(path), "_blank", 0, self._load_properties())
        suffix = path.suffix.lower()
        self.doc_kind = "calc" if suffix in {".ods", ".xlsx", ".xls"} else "impress" if suffix in {".odp", ".ppt", ".pptx"} else "writer"
        self.current_path = path

    def _capture_visual(self, label):
        if not self.visual_capture:
            return "LibreOffice visual capture disabled.", {}
        original_path = self.current_path
        reopened_for_capture = False
        if original_path and Path(original_path).exists():
            if self.doc is not None:
                try:
                    self.doc.close(True)
                except Exception:
                    try:
                        self.doc.dispose()
                    except Exception:
                        pass
            self.doc = self.desktop.loadComponentFromURL(file_url(original_path), "_blank", 0, ())
            self.current_path = original_path
            reopened_for_capture = True
            time.sleep(0.3)
        self._prepare_visual_document(label)
        try:
            frame = self.doc.CurrentController.Frame if self.doc is not None else None
            window = frame.ContainerWindow if frame is not None else None
            if window is not None:
                window.setVisible(True)
                try:
                    window.setPosSize(80, 80, 1400, 1000, 15)
                except Exception:
                    pass
                window.toFront()
        except Exception:
            pass
        self._activate_visual_window()
        time.sleep(0.8)
        path = (self.screenshots_dir / f"{self.spec_path.stem}.{label}.png").resolve()
        if sys.platform == "darwin":
            window_id = self._visual_window_id()
            command = ["/usr/sbin/screencapture", "-x", "-l", window_id, str(path)] if window_id else ["/usr/sbin/screencapture", "-x", str(path)]
        else:
            gnome = shutil.which("gnome-screenshot")
            imagemagick = shutil.which("import")
            if gnome:
                command = [gnome, "-f", str(path)]
            elif imagemagick:
                command = [imagemagick, "-window", "root", str(path)]
            else:
                raise RuntimeError(
                    "LibreOffice desktop screenshot capture requires macOS screencapture, "
                    "gnome-screenshot, or ImageMagick import."
                )
        result = subprocess.run(command, capture_output=True, text=True, timeout=20)
        if result.returncode != 0:
            raise RuntimeError(f"LibreOffice desktop screenshot failed: {(result.stderr or result.stdout).strip()}")
        if not path.exists() or path.stat().st_size <= 0:
            raise RuntimeError(f"LibreOffice desktop screenshot did not produce a non-empty PNG: {path}")
        self.screenshots[label] = str(path)
        if reopened_for_capture and label != "after":
            try:
                self.doc.close(True)
            except Exception:
                try:
                    self.doc.dispose()
                except Exception:
                    pass
            self.doc = self.desktop.loadComponentFromURL(file_url(original_path), "_blank", 0, self._load_properties())
            self.current_path = original_path
        return f"LibreOffice {label} screenshot captured.", {"path": str(path), "source": "desktop_screenshot"}

    def _prepare_visual_document(self, label):
        if self.doc is None:
            return
        try:
            capture = self._visual_capture_options()
            if self.doc_kind == "calc":
                preferred = capture.get(f"{label}_active_sheet") or capture.get("active_sheet")
                target = self.spec.get("target") or {}
                sheets = target.get("sheets") if isinstance(target, dict) else None
                if not preferred and isinstance(sheets, dict):
                    preferred = next(iter(sheets.keys()), None)
                if not preferred and isinstance(target, dict):
                    preferred = target.get("sheet")
                if preferred and self.doc.Sheets.hasByName(preferred):
                    self.doc.CurrentController.setActiveSheet(self.doc.Sheets.getByName(preferred))
            elif self.doc_kind == "impress":
                pages = self.doc.getDrawPages()
                if pages.getCount() > 0:
                    active_slide = capture.get(f"{label}_active_slide") or capture.get("active_slide") or 1
                    index = max(0, min(int(active_slide) - 1, pages.getCount() - 1))
                    self.doc.CurrentController.setCurrentPage(pages.getByIndex(index))
        except Exception:
            pass

    def _visual_capture_options(self):
        oracle = self.spec.get("visual_oracle") or {}
        capture = oracle.get("capture") if isinstance(oracle, dict) else None
        return capture if isinstance(capture, dict) else {}

    def _activate_visual_window(self):
        if sys.platform != "darwin" or not self.soffice_pid:
            return
        script = (
            'ObjC.import("AppKit");'
            f'var app=$.NSRunningApplication.runningApplicationWithProcessIdentifier({int(self.soffice_pid)});'
            'if (app) { app.activateWithOptions($.NSApplicationActivateAllWindows | $.NSApplicationActivateIgnoringOtherApps); }'
        )
        subprocess.run(["/usr/bin/osascript", "-l", "JavaScript", "-e", script], capture_output=True, text=True, timeout=5)
        time.sleep(0.4)

    def _visual_window_id(self):
        if sys.platform != "darwin" or not self.soffice_pid:
            return None
        helper = Path(os.environ.get("WARPTEST_MAC_WINDOW_ID_HELPER", "/private/tmp/warptest-window-id"))
        if helper.exists():
            result = subprocess.run([str(helper), str(int(self.soffice_pid))], capture_output=True, text=True, timeout=5)
            window_id = result.stdout.strip()
            if window_id.isdigit():
                return window_id
        script = (
            'ObjC.import("CoreGraphics");'
            'var wins=$.CGWindowListCopyWindowInfo($.kCGWindowListOptionOnScreenOnly,$.kCGNullWindowID);'
            'var best=null; var bestArea=0;'
            f'var targetPid={int(self.soffice_pid)};'
            'for (var i=0;i<wins.count;i++){'
            '  var w=wins.objectAtIndex(i);'
            '  var pid=ObjC.unwrap(w.objectForKey("kCGWindowOwnerPID"));'
            '  var layer=ObjC.unwrap(w.objectForKey("kCGWindowLayer"));'
            '  var num=ObjC.unwrap(w.objectForKey("kCGWindowNumber"));'
            '  var bounds=ObjC.unwrap(w.objectForKey("kCGWindowBounds"));'
            '  var width=Number(bounds.Width || bounds.width || 0);'
            '  var height=Number(bounds.Height || bounds.height || 0);'
            '  var area=width*height;'
            '  if (pid===targetPid && layer===0 && width>300 && height>200 && area>bestArea){ best=num; bestArea=area; }'
            '}'
            'best ? String(best) : "";'
        )
        result = subprocess.run(["/usr/bin/osascript", "-l", "JavaScript", "-e", script], capture_output=True, text=True, timeout=5)
        window_id = result.stdout.strip()
        return window_id if window_id.isdigit() else None

    def _append_writer_text(self, text):
        cursor = self.doc.Text.createTextCursor()
        cursor.gotoEnd(False)
        self.doc.Text.insertString(cursor, text, False)

    def _collect_target_headings(self, styles):
        styles = {str(item) for item in styles}
        headings = []
        for paragraph in (self.spec.get("target") or {}).get("paragraphs") or []:
            if not styles or str(paragraph.get("style")) in styles:
                text = str(paragraph.get("text", "")).strip()
                if text:
                    headings.append(text)
        return headings

    def _insert_writer_table(self, name, rows):
        rows = list(rows or [])
        row_count = max(1, len(rows))
        col_count = max(1, max((len(row or []) for row in rows), default=1))
        self._append_writer_text("\n")
        cursor = self.doc.Text.createTextCursor()
        cursor.gotoEnd(False)
        table = self.doc.createInstance("com.sun.star.text.TextTable")
        table.initialize(row_count, col_count)
        try:
            table.Name = name
        except Exception:
            pass
        self.doc.Text.insertTextContent(cursor, table, False)
        for row_index, row in enumerate(rows, start=1):
            for col_index, value in enumerate(row or [], start=1):
                table.getCellByName(self._cell_name(col_index, row_index)).String = "" if value is None else str(value)
        self._append_writer_text("\n")

    def _writer_table(self, name):
        tables = self.doc.TextTables
        if not tables.hasByName(name):
            raise AssertionError(f"Writer table does not exist: {name}")
        return tables.getByName(name)

    def _set_writer_table_cell(self, table_name, cell_name, value):
        self._writer_table(table_name).getCellByName(cell_name).String = "" if value is None else str(value)

    def _insert_writer_table_row(self, table_name, row, values):
        table = self._writer_table(table_name)
        row_count = table.Rows.getCount()
        if row < 1 or row > row_count + 1:
            raise AssertionError(f"Cannot insert row {row} into Writer table {table_name!r} with {row_count} rows")
        row_index = row - 1
        table.Rows.insertByIndex(row_index, 1)
        for col_index, value in enumerate(values, start=1):
            table.getCellByName(self._cell_name(col_index, row)).String = "" if value is None else str(value)

    def _append_writer_table_row(self, table_name, values):
        table = self._writer_table(table_name)
        row_index = table.Rows.getCount()
        table.Rows.insertByIndex(row_index, 1)
        for col_index, value in enumerate(values, start=1):
            table.getCellByName(self._cell_name(col_index, row_index + 1)).String = "" if value is None else str(value)

    def _writer_table_cell_text(self, table_name, cell_name):
        return self._writer_table(table_name).getCellByName(cell_name).String

    def _cell_name(self, col_index, row_index):
        letters = ""
        col = int(col_index)
        while col:
            col, remainder = divmod(col - 1, 26)
            letters = chr(ord("A") + remainder) + letters
        return f"{letters}{int(row_index)}"

    def _update_document_indexes(self):
        indexes = getattr(self.doc, "DocumentIndexes", None)
        if indexes is None and hasattr(self.doc, "getDocumentIndexes"):
            indexes = self.doc.getDocumentIndexes()
        if indexes is None:
            return
        for index in range(indexes.getCount()):
            try:
                indexes.getByIndex(index).update()
            except Exception:
                pass

    def _insert_bookmark(self, name, text):
        found = self._find_writer_text(text)
        if found is None:
            raise AssertionError(f"Could not find bookmark anchor text {text!r}")
        bookmark = self.doc.createInstance("com.sun.star.text.Bookmark")
        self._set_uno_name(bookmark, name)
        found.Text.insertTextContent(found, bookmark, True)
        if not self._bookmark_exists(name):
            raise AssertionError(f"Writer bookmark was not created: {name}")

    def _set_uno_name(self, target, name):
        if hasattr(target, "setName"):
            target.setName(name)
        else:
            target.Name = name

    def _set_uno_property(self, target, name, value):
        if hasattr(target, "setPropertyValue"):
            target.setPropertyValue(name, value)
        else:
            setattr(target, name, value)

    def _bookmark_exists(self, name):
        try:
            bookmarks = self.doc.Bookmarks
            if bookmarks.hasByName(name):
                return True
        except Exception:
            pass
        return False

    def _bookmark_text(self, name):
        try:
            bookmark = self.doc.Bookmarks.getByName(name)
            anchor = getattr(bookmark, "Anchor", None)
            if anchor is None and hasattr(bookmark, "getAnchor"):
                anchor = bookmark.getAnchor()
            text = str(getattr(anchor, "String", "") or "")
            if text:
                return text
        except Exception as error:
            raise AssertionError(f"Writer bookmark does not expose anchor text: {name}") from error
        raise AssertionError(f"Writer bookmark has empty anchor text: {name}")

    def _insert_reference_field(self, bookmark, prefix=""):
        if not self._bookmark_exists(bookmark):
            raise AssertionError(f"Writer bookmark does not exist: {bookmark}")
        field = self.doc.createInstance("com.sun.star.text.TextField.GetReference")
        try:
            from com.sun.star.text.ReferenceFieldSource import BOOKMARK
            from com.sun.star.text.ReferenceFieldPart import TEXT

            self._set_uno_property(field, "ReferenceFieldSource", int(BOOKMARK))
            self._set_uno_property(field, "ReferenceFieldPart", int(TEXT))
        except Exception:
            # UNO installations that do not expose generated constant modules still
            # accept the underlying short values for bookmark source and reference text.
            self._set_uno_property(field, "ReferenceFieldSource", 2)
            self._set_uno_property(field, "ReferenceFieldPart", 2)
        self._set_uno_property(field, "SourceName", bookmark)
        cursor = self.doc.Text.createTextCursor()
        cursor.gotoEnd(False)
        self.doc.Text.insertString(cursor, "\n" + prefix, False)
        cursor.gotoEnd(False)
        self.doc.Text.insertTextContent(cursor, field, False)
        self._refresh_text_fields()
        expected = prefix + self._bookmark_text(bookmark)
        if expected not in self.doc.Text.String:
            raise AssertionError(f"Writer reference field did not render expected text {expected!r}")

    def _refresh_text_fields(self):
        fields = getattr(self.doc, "TextFields", None)
        if fields is None and hasattr(self.doc, "getTextFields"):
            fields = self.doc.getTextFields()
        if fields is not None and hasattr(fields, "refresh"):
            fields.refresh()

    def _replace_text(self, old, new):
        descriptor = self.doc.createReplaceDescriptor()
        descriptor.SearchString = old
        descriptor.ReplaceString = new
        return self.doc.replaceAll(descriptor)

    def _apply_style_to_text(self, text, style):
        found = self._find_writer_text(text)
        if found is None:
            raise AssertionError(f"Could not find paragraph text {text!r}")
        found.ParaStyleName = style

    def _paragraph_style_for_text(self, text):
        found = self._find_writer_text(text)
        if found is None:
            raise AssertionError(f"Could not find paragraph text {text!r}")
        return found.ParaStyleName

    def _find_writer_text(self, text):
        descriptor = self.doc.createSearchDescriptor()
        descriptor.SearchString = text
        return self.doc.findFirst(descriptor)

    def _sheet(self, name):
        sheets = self.doc.Sheets
        if not sheets.hasByName(name):
            raise AssertionError(f"Sheet does not exist: {name}")
        return sheets.getByName(name)

    def _ensure_sheet(self, name):
        sheets = self.doc.Sheets
        if not sheets.hasByName(name):
            sheets.insertNewByName(name, sheets.getCount())
        return sheets.getByName(name)

    def _cell(self, address, sheet_name="Sheet1"):
        return self._sheet(sheet_name).getCellRangeByName(address)

    def _set_cell(self, address, value, sheet_name="Sheet1"):
        cell = self._cell(address, sheet_name=sheet_name)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            cell.Value = value
        else:
            cell.String = "" if value is None else str(value)

    def _set_formula(self, address, formula, sheet_name="Sheet1"):
        self._cell(address, sheet_name=sheet_name).Formula = formula

    def _cell_assertion_value(self, cell, sheet_name, expected):
        formula = str(cell.Formula or "")
        if formula.startswith("=") and isinstance(expected, (int, float)) and not isinstance(expected, bool):
            value = self._eval_formula(formula, sheet_name, cell.CellAddress.Row + 1)
            if value is not None:
                return value
        return cell.Value if isinstance(expected, (int, float)) and not isinstance(expected, bool) else cell.String

    def _calculate(self):
        if hasattr(self.doc, "calculateAll"):
            self.doc.calculateAll()
        try:
            self.doc.calculate()
        except Exception:
            pass

    def _evaluate_known_formulas(self):
        if self.doc_kind != "calc":
            return
        sheets = self.doc.Sheets
        for sheet_index in range(sheets.getCount()):
            sheet = sheets.getByIndex(sheet_index)
            cursor = sheet.createCursor()
            cursor.gotoEndOfUsedArea(True)
            used = cursor.RangeAddress
            for row in range(used.StartRow, used.EndRow + 1):
                for col in range(used.StartColumn, used.EndColumn + 1):
                    cell = sheet.getCellByPosition(col, row)
                    formula = str(cell.Formula or "")
                    if not formula.startswith("="):
                        continue
                    value = self._eval_formula(formula, sheet.Name, row + 1)
                    if value is not None:
                        yield cell, value

    def _eval_formula(self, formula, current_sheet, row_number):
        expr = formula[1:].strip()
        if expr.upper() == "ROW()-1":
            return row_number - 1
        sum_match = re.fullmatch(r"SUM\(([^)]+)\)", expr, flags=re.IGNORECASE)
        if sum_match:
            total = 0
            for ref in re.split(r"[;,]", sum_match.group(1)):
                total += self._reference_value(ref.strip(), current_sheet)
            return total
        for operator in ("-", "+", "*", "/"):
            parts = self._split_top_level(expr, operator)
            if parts:
                left = self._eval_operand(parts[0], current_sheet)
                right = self._eval_operand(parts[1], current_sheet)
                if operator == "-":
                    return left - right
                if operator == "+":
                    return left + right
                if operator == "*":
                    return left * right
                if operator == "/":
                    return left / right
        return None

    def _split_top_level(self, expr, operator):
        depth = 0
        for index, char in enumerate(expr):
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            elif char == operator and depth == 0:
                return expr[:index].strip(), expr[index + 1 :].strip()
        return None

    def _eval_operand(self, value, current_sheet):
        value = value.strip()
        if value.startswith("(") and value.endswith(")"):
            nested = self._eval_formula("=" + value[1:-1], current_sheet, 1)
            return float(nested) if nested is not None else 0.0
        try:
            return float(value)
        except ValueError:
            return self._reference_value(value, current_sheet)

    def _reference_value(self, ref, current_sheet):
        ref = ref.replace("$", "").strip()
        if "." in ref:
            sheet_name, address = ref.split(".", 1)
        else:
            sheet_name, address = current_sheet, ref
        cell = self._cell(address, sheet_name=sheet_name)
        formula = str(cell.Formula or "")
        if formula.startswith("="):
            value = self._eval_formula(formula, sheet_name, cell.CellAddress.Row + 1)
            if value is not None:
                return float(value)
        return float(cell.Value)

    def _sort_range(self, range_name, sheet_name, key, descending=False):
        sheet = self._sheet(sheet_name)
        start_col, start_row, end_col, end_row = self._range_bounds(range_name)
        key_col = self._column_index(key) if key.isalpha() else start_col + int(key) - 1
        rows = []
        for row in range(start_row, end_row + 1):
            values = []
            for col in range(start_col, end_col + 1):
                cell = sheet.getCellByPosition(col, row)
                values.append((cell.String, cell.Value, cell.Formula))
            rows.append(values)
        key_offset = key_col - start_col

        def sort_key(row_values):
            text, number, _formula = row_values[key_offset]
            return number if number else text

        rows.sort(key=sort_key, reverse=descending)
        for row_offset, values in enumerate(rows):
            for col_offset, (text, number, formula) in enumerate(values):
                cell = sheet.getCellByPosition(start_col + col_offset, start_row + row_offset)
                if formula:
                    cell.Formula = formula
                elif number:
                    cell.Value = number
                else:
                    cell.String = text

    def _define_named_range(self, name, range_name, sheet_name):
        address = self._cell(range_name, sheet_name=sheet_name).CellAddress
        self.doc.NamedRanges.addNewByName(name, f"${sheet_name}.${range_name}", address, 0)
        if not self.doc.NamedRanges.hasByName(name):
            raise AssertionError(f"Calc named range was not created: {name}")

    def _named_range_exists(self, name):
        try:
            if self.doc.NamedRanges.hasByName(name):
                return True
        except Exception:
            pass
        return False

    def _create_chart(self, name, range_name, sheet_name, chart_type="line"):
        sheet = self._sheet(sheet_name)
        rect = uno.createUnoStruct("com.sun.star.awt.Rectangle")
        rect.X = 8000
        rect.Y = 1000
        rect.Width = 12000
        rect.Height = 7000
        source = sheet.getCellRangeByName(range_name)
        sheet.Charts.addNewByName(name, rect, (source.RangeAddress,), True, True)
        if not sheet.Charts.hasByName(name):
            raise AssertionError(f"Calc chart was not created: {sheet_name}.{name}")

    def _chart_exists(self, name, sheet_name):
        try:
            charts = self._sheet(sheet_name).Charts
            if charts.hasByName(name):
                return True
        except Exception:
            pass
        return False

    def _column_index(self, letters):
        value = 0
        for char in letters.upper():
            value = value * 26 + (ord(char) - ord("A") + 1)
        return value - 1

    def _range_bounds(self, range_name):
        start, end = range_name.split(":", 1)
        start_col, start_row = self._address_parts(start)
        end_col, end_row = self._address_parts(end)
        return start_col, start_row, end_col, end_row

    def _address_parts(self, address):
        match = re.fullmatch(r"([A-Za-z]+)([0-9]+)", address)
        if not match:
            raise ValueError(f"Unsupported Calc address: {address}")
        return self._column_index(match.group(1)), int(match.group(2)) - 1

    def _values_equal(self, actual, expected):
        if isinstance(expected, (int, float)) and not isinstance(expected, bool):
            try:
                return abs(float(actual) - float(expected)) <= 1e-6
            except Exception:
                return False
        return str(actual) == str(expected)

    def _ensure_slide(self, slide_number):
        pages = self.doc.getDrawPages()
        while pages.getCount() < slide_number:
            pages.insertNewByIndex(pages.getCount())
        return pages.getByIndex(slide_number - 1)

    def _reset_impress_slides(self, count):
        pages = self.doc.getDrawPages()
        count = max(1, int(count))
        while pages.getCount() < count:
            pages.insertNewByIndex(pages.getCount())
        while pages.getCount() > count:
            pages.remove(pages.getByIndex(pages.getCount() - 1))
        for index in range(count):
            self._clear_slide(pages.getByIndex(index))

    def _set_slide_title(self, slide_number, title):
        self._upsert_slide_text(slide_number, "WarpTestTitle", title, 1000, 900, 24000, 2200)

    def _set_slide_bullets(self, slide_number, bullets):
        text = "\n".join(f"- {item}" for item in bullets)
        self._upsert_slide_text(slide_number, "WarpTestBullets", text, 1300, 3600, 22000, 9000)

    def _add_slide_text(self, slide_number, text, role="shape"):
        page = self._ensure_slide(slide_number)
        name = f"WarpTest{role.title()}{page.getCount() + 1}"
        self._upsert_slide_text(slide_number, name, text, 1600, 11800, 21000, 3200)

    def _upsert_slide_text(self, slide_number, name, text, x, y, width, height):
        page = self._ensure_slide(slide_number)
        shape = self._slide_shape_by_name(page, name)
        if shape is None:
            shape = self.doc.createInstance("com.sun.star.drawing.TextShape")
            size = uno.createUnoStruct("com.sun.star.awt.Size")
            size.Width = width
            size.Height = height
            position = uno.createUnoStruct("com.sun.star.awt.Point")
            position.X = x
            position.Y = y
            shape.setSize(size)
            shape.setPosition(position)
            try:
                shape.Name = name
            except Exception:
                pass
            page.add(shape)
        shape.String = text

    def _slide_shape_by_name(self, page, name):
        for index in range(page.getCount()):
            shape = page.getByIndex(index)
            if getattr(shape, "Name", None) == name:
                return shape
        return None

    def _slide_text(self, slide_number):
        page = self._ensure_slide(slide_number)
        values = []
        for index in range(page.getCount()):
            shape = page.getByIndex(index)
            text = getattr(shape, "String", "")
            if text:
                values.append(str(text))
        return "\n".join(values)

    def _slide_title(self, slide_number):
        page = self._ensure_slide(slide_number)
        title_shape = self._slide_shape_by_name(page, "WarpTestTitle")
        if title_shape is not None:
            return str(title_shape.String)
        text = self._slide_text(slide_number).strip()
        return text.splitlines()[0] if text else ""

    def _move_slide(self, from_slide, to_slide):
        pages = self.doc.getDrawPages()
        count = pages.getCount()
        if from_slide < 1 or from_slide > count or to_slide < 1 or to_slide > count:
            raise AssertionError(f"Cannot move slide {from_slide} to {to_slide} in deck with {count} slides")
        if from_slide == to_slide:
            return
        payloads = [self._slide_payload(index + 1) for index in range(count)]
        moved = payloads.pop(from_slide - 1)
        payloads.insert(to_slide - 1, moved)
        for index, payload in enumerate(payloads, start=1):
            page = pages.getByIndex(index - 1)
            self._clear_slide(page)
            for shape_payload in payload:
                self._add_slide_payload_shape(page, shape_payload)

    def _slide_payload(self, slide_number):
        page = self._ensure_slide(slide_number)
        payload = []
        for index in range(page.getCount()):
            shape = page.getByIndex(index)
            name = str(getattr(shape, "Name", "") or "")
            try:
                text = str(shape.String or "")
            except Exception as error:
                raise AssertionError(f"Unsupported Impress shape on slide {slide_number}: {name or index}") from error
            try:
                position = shape.getPosition()
                size = shape.getSize()
            except Exception as error:
                raise AssertionError(f"Unsupported Impress shape on slide {slide_number}: {name or index}") from error
            payload.append(
                {
                    "name": name,
                    "text": text,
                    "x": int(position.X),
                    "y": int(position.Y),
                    "width": int(size.Width),
                    "height": int(size.Height),
                }
            )
        return payload

    def _clear_slide(self, page):
        for index in range(page.getCount() - 1, -1, -1):
            page.remove(page.getByIndex(index))

    def _add_slide_payload_shape(self, page, payload):
        shape = self.doc.createInstance("com.sun.star.drawing.TextShape")
        size = uno.createUnoStruct("com.sun.star.awt.Size")
        size.Width = int(payload["width"])
        size.Height = int(payload["height"])
        position = uno.createUnoStruct("com.sun.star.awt.Point")
        position.X = int(payload["x"])
        position.Y = int(payload["y"])
        shape.setSize(size)
        shape.setPosition(position)
        if payload.get("name"):
            try:
                shape.Name = str(payload["name"])
            except Exception:
                pass
        page.add(shape)
        shape.String = str(payload.get("text", ""))

    def close(self):
        if self.doc is not None:
            try:
                self.doc.close(True)
            except Exception:
                self.doc.dispose()
            self.doc = None


def main():
    request_path = Path(sys.argv[1])
    report_path = Path(sys.argv[2])
    request = read_json(request_path)
    stdout_path = request["stdout_path"]
    stderr_path = request["stderr_path"]
    pipe_name = request["pipe_name"]
    proc = None
    handles = []
    result = {
        "status": "failure",
        "checks": [],
        "request_path": str(request_path),
        "report_path": str(report_path),
    }
    checkpoint = None
    try:
        proc, stdout_handle, stderr_handle = start_office(
            request["soffice"],
            Path(request["user_dir"]),
            pipe_name,
            stdout_path,
            stderr_path,
            headless=bool(request.get("headless", True)),
        )
        request["soffice_pid"] = proc.pid
        handles = [stdout_handle, stderr_handle]
        ctx = connect(pipe_name, proc)
        checkpoint = LibreOfficeCheckpoint(request)
        checkpoint.attach(ctx)
        result.update(checkpoint.run())
        result["exit_code"] = proc.poll()
    except Exception as error:
        result["status"] = "failure"
        result["error"] = str(error)
        result["checks"].append({"name": "runtime.libreoffice_uno", "status": "failure", "detail": str(error), "data": {}})
    finally:
        if checkpoint is not None:
            checkpoint.close()
        if proc is not None:
            try:
                ctx = checkpoint.ctx if checkpoint is not None else None
                if ctx is not None:
                    desktop = ctx.ServiceManager.createInstanceWithContext("com.sun.star.frame.Desktop", ctx)
                    desktop.terminate()
            except Exception:
                pass
            try:
                proc.terminate()
                proc.wait(timeout=10)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        for handle in handles:
            try:
                handle.close()
            except Exception:
                pass
    write_json(report_path, result)


if __name__ == "__main__":
    main()
'''


class LibreOfficeRuntimeAdapter:
    """Run LibreOffice document checkpoints through public UNO/ODF semantics."""

    def __init__(self, repo: Path, report_dir: Path, headless: bool = True, visual_capture: bool = False) -> None:
        self.repo = repo.resolve()
        self.report_dir = report_dir.resolve()
        self.headless = headless
        self.visual_capture = bool(visual_capture)
        self.runtime_root = self.report_dir / "libreoffice-runtime"
        self.helper_path = self.runtime_root / "libreoffice_helper.py"
        self.user_dir = self.runtime_root / "user"
        self.out_dir = self.report_dir / "libreoffice"
        self.soffice = self._resolve_soffice()
        self.python = Path(os.environ.get("WARPTEST_LIBREOFFICE_PYTHON", sys.executable)).expanduser().resolve()
        self.program_dir = self._resolve_program_dir()
        self.version = self._soffice_version()
        mode_arg = "--headless " if self.headless and not self.visual_capture else ""
        self.launch_command = f"{self.soffice} {mode_arg}--accept=pipe,name={{pipe}};urp;"
        self.app_url = None
        self.logs: list[str] = []
        self._last_screenshot_path: Path | None = None
        self._last_screenshot_metadata: dict[str, Any] = {}

    def __enter__(self) -> "LibreOfficeRuntimeAdapter":
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def run_checkpoint(self, spec: dict[str, Any], spec_path: Path) -> dict[str, Any]:
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self._ensure_python_dependency()
        self.helper_path.write_text(HELPER_SOURCE, encoding="utf-8")

        stem = Path(spec_path).stem
        request_path = self.out_dir / f"{stem}-libreoffice-request.json"
        report_path = self.out_dir / f"{stem}-libreoffice-result.json"
        stdout_path = self.out_dir / f"{stem}-libreoffice-stdout.log"
        stderr_path = self.out_dir / f"{stem}-libreoffice-stderr.log"
        pipe_name = "warptest_lo_" + uuid.uuid4().hex
        request = {
            "spec_path": str(Path(spec_path).resolve()),
            "spec": spec,
            "runtime_root": str(self.runtime_root),
            "soffice": str(self.soffice),
            "soffice_version": self.version,
            "python": str(self.python),
            "program_dir": str(self.program_dir) if self.program_dir else None,
            "headless": bool(self.headless and not self.visual_capture),
            "visual_capture": self.visual_capture,
            "user_dir": str(self.user_dir),
            "pipe_name": pipe_name,
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
        }
        write_json(request_path, request)
        env = self._python_env()
        command = [str(self.python), str(self.helper_path), str(request_path), str(report_path)]
        completed = subprocess.run(
            command,
            cwd=str(self.repo),
            capture_output=True,
            text=True,
            timeout=int(spec.get("timeout_ms", 120_000) / 1000) if isinstance(spec.get("timeout_ms"), int) else 120,
            env=env,
        )
        self.logs.extend(str(path) for path in [request_path, report_path, stdout_path, stderr_path] if path.exists())
        if completed.returncode != 0:
            raise RuntimeErrorWithLog(
                "LibreOffice UNO helper failed. Configure WARPTEST_LIBREOFFICE_PYTHON, "
                "WARPTEST_LIBREOFFICE_SOFFICE, URE_BOOTSTRAP, and PYTHONPATH for pyuno. "
                f"Command: {' '.join(command)}. stderr:\n{completed.stderr[-4000:]}"
            )
        if not report_path.exists():
            raise RuntimeErrorWithLog(
                f"LibreOffice UNO helper did not write a report: {report_path}. stderr:\n{completed.stderr[-4000:]}"
            )
        result = json.loads(report_path.read_text(encoding="utf-8"))
        result.setdefault("request_path", str(request_path))
        result.setdefault("report_path", str(report_path))
        result.setdefault("stdout_path", str(stdout_path))
        result.setdefault("stderr_path", str(stderr_path))
        result["soffice"] = str(self.soffice)
        result["soffice_version"] = self.version
        result["python"] = str(self.python)
        screenshots = result.get("screenshots") if isinstance(result.get("screenshots"), dict) else {}
        self._last_screenshot_metadata = {
            "screenshot_before": result.get("screenshot_before"),
            "screenshot_after": result.get("screenshot_after"),
            "screenshots": screenshots,
            "live_source": "libreoffice_desktop_screenshot" if screenshots else None,
        }
        after_path = result.get("screenshot_after") or result.get("screenshot_before")
        if after_path:
            self._last_screenshot_path = Path(after_path)
        return result

    def screenshot(self, path: str | Path | None = None, full_page: bool = False) -> Path:
        if self._last_screenshot_path is None:
            raise RuntimeErrorWithLog(
                "LibreOffice screenshot was not captured. Run with visual capture enabled "
                "through a visual oracle policy and a desktop-capable soffice process."
            )
        destination = Path(path).expanduser().resolve() if path else self._last_screenshot_path
        if destination != self._last_screenshot_path:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(self._last_screenshot_path, destination)
            self._last_screenshot_path = destination
        return destination

    def last_screenshot_metadata(self) -> dict[str, Any]:
        return dict(self._last_screenshot_metadata)

    def _resolve_soffice(self) -> Path:
        configured = os.environ.get("WARPTEST_LIBREOFFICE_SOFFICE")
        candidates: list[Path] = []
        if configured:
            candidates.append(Path(configured).expanduser())
        candidates.extend(
            [
                self.repo / "instdir" / "program" / "soffice",
                self.repo / "instdir" / "LibreOfficeDev.app" / "Contents" / "MacOS" / "soffice",
                self.repo / "instdir" / "LibreOffice.app" / "Contents" / "MacOS" / "soffice",
            ]
        )
        path_soffice = shutil.which("soffice")
        if path_soffice:
            candidates.append(Path(path_soffice))
        for candidate in candidates:
            if candidate.exists() and os.access(candidate, os.X_OK):
                return candidate.resolve()
        raise RuntimeErrorWithLog(
            "LibreOffice soffice executable was not found. Set WARPTEST_LIBREOFFICE_SOFFICE "
            "or build games/LibreOffice so instdir contains soffice."
        )

    def _resolve_program_dir(self) -> Path | None:
        configured = os.environ.get("WARPTEST_LIBREOFFICE_PROGRAM_DIR")
        if configured:
            return Path(configured).expanduser().resolve()
        candidates = [
            self.soffice.parent,
            self.soffice.parent.parent / "Resources",
            self.repo / "instdir" / "program",
        ]
        for candidate in candidates:
            if (candidate / "uno.py").exists() or (candidate / "fundamentalrc").exists():
                return candidate.resolve()
        return None

    def _soffice_version(self) -> str | None:
        try:
            result = subprocess.run(
                [str(self.soffice), "--headless", "--version"],
                capture_output=True,
                text=True,
                timeout=20,
            )
            text = (result.stdout or result.stderr).strip()
            return text or None
        except Exception:
            return None

    def _python_env(self) -> dict[str, str]:
        env = os.environ.copy()
        if self.program_dir:
            paths = [str(self.program_dir)]
            existing = env.get("PYTHONPATH")
            if existing:
                paths.append(existing)
            env["PYTHONPATH"] = os.pathsep.join(paths)
            fundamental = self.program_dir / "fundamentalrc"
            if fundamental.exists() and not env.get("URE_BOOTSTRAP"):
                env["URE_BOOTSTRAP"] = "file://" + str(fundamental)
        env.setdefault("PYTHONUNBUFFERED", "1")
        return env

    def _ensure_python_dependency(self) -> None:
        command = [str(self.python), "-c", "import uno"]
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=self._python_env(),
            )
        except Exception as exc:
            raise RuntimeErrorWithLog(
                "LibreOffice UNO runtime dependencies are not available. Configure "
                "WARPTEST_LIBREOFFICE_PYTHON to a Python that can import uno/pyuno, "
                "set WARPTEST_LIBREOFFICE_SOFFICE to the matching soffice executable, "
                "and set URE_BOOTSTRAP/PYTHONPATH or WARPTEST_LIBREOFFICE_PROGRAM_DIR "
                f"if needed. Python: {self.python}. soffice: {self.soffice}. Python launch error: {exc}"
            ) from exc
        try:
            stdout, stderr = process.communicate(timeout=15)
        except subprocess.TimeoutExpired as exc:
            try:
                process.kill()
            except Exception:
                pass
            raise RuntimeErrorWithLog(
                "LibreOffice UNO runtime dependencies are not available. Configure "
                "WARPTEST_LIBREOFFICE_PYTHON to a Python that can import uno/pyuno, "
                "set WARPTEST_LIBREOFFICE_SOFFICE to the matching soffice executable, "
                "and set URE_BOOTSTRAP/PYTHONPATH or WARPTEST_LIBREOFFICE_PROGRAM_DIR "
                f"if needed. Python: {self.python}. soffice: {self.soffice}. "
                "Import check timed out after 15 seconds while running `import uno`."
            ) from exc
        if process.returncode != 0:
            detail = (stderr or stdout or "").strip()
            raise RuntimeErrorWithLog(
                "LibreOffice UNO runtime dependencies are not available. Configure "
                "WARPTEST_LIBREOFFICE_PYTHON to a Python that can import uno/pyuno, "
                "set WARPTEST_LIBREOFFICE_SOFFICE to the matching soffice executable, "
                "and set URE_BOOTSTRAP/PYTHONPATH or WARPTEST_LIBREOFFICE_PROGRAM_DIR "
                f"if needed. Python: {self.python}. soffice: {self.soffice}. Import error: {detail}"
            )
