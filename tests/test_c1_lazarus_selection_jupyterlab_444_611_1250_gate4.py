from warptest.c1_lazarus_selection import jupyterlab_444_611_1250_gate4 as module


def test_444_existing_notebook_api_is_compatible() -> None:
    sections = {
        "test/src/notebook/notebook/actions.spec.ts": (
            b"+        NotebookActions.deleteCells(widget);\n"
            b"+        widget.select(widget.childAt(widget.childCount() - 1));\n"
        ),
        "test/src/notebook/notebook/model.spec.ts": b"+          model.cells.clear();\n",
        "test/src/notebook/notebook/widget.spec.ts": b"+          widget.model.cells.clear();\n",
    }
    assert module._audit_fixed_test_api(444, sections)["fixed_only_api"] is False


def test_611_preserved_mock_support_uses_existing_change_kernel() -> None:
    sections = {
        "test/src/docmanager/mockcontext.ts": (
            b" changeKernel(options: IKernel.IModel): Promise<IKernel> {\n"
            b"+      this._kernel = null;\n"
        ),
        "test/src/notebook/notebook/default-toolbar.spec.ts": (
            b"+        panel.context.changeKernel(void 0).then(() => {\n"
        ),
    }
    audit = module._audit_fixed_test_api(611, sections)
    assert audit["fixed_only_api"] is False
    assert audit["strict_null_checks_available"] is False


def test_1250_existing_svg_renderer_api_is_compatible() -> None:
    sections = {
        "src/renderers/index.ts": (
            b"   isSanitizable(mimetype: string): boolean {\n"
            b"-    return this.mimetypes.indexOf(mimetype) !== -1;\n"
        ),
        "test/src/renderers/renderers.spec.ts": (
            b"+        expect(t.isSanitizable('image/svg+xml')).to.be(false);\n"
        ),
    }
    assert module._audit_fixed_test_api(1250, sections)["fixed_only_api"] is False


def test_grouped_gate4_module_has_no_runtime_entrypoint() -> None:
    assert not hasattr(module, "run_candidate")
    assert not hasattr(module, "build_candidate")
