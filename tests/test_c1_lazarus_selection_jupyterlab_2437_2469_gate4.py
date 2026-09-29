from warptest.c1_lazarus_selection import jupyterlab_2437_2469_gate4 as module


def test_2437_assertion_only_change_is_buggy_api_compatible() -> None:
    audit = module._audit_test_api(
        2437,
        {
            "packages/notebook/src/celltools.ts": (
                b"+      getter?: (cell: Cell) => JSONValue;\n"
                b"+      setter?: (cell: Cell, value: JSONValue) => void;\n"
            ),
            "test/src/notebook/celltools.spec.ts": (
                b"-      expect(metadata.get('slideshow')).to.be('slide');\n"
                b"+      expect(metadata.get('slideshow')).to.eql({ 'slide_type': 'slide' });\n"
            ),
        },
    )
    assert audit["fixed_only_api"] is False
    assert audit["buggy_compile_expected"] is True


def test_2469_nullable_generic_is_fixed_only() -> None:
    audit = module._audit_test_api(
        2469,
        {
            "packages/apputils/src/vdom.ts": (
                b"-abstract class VDomRenderer<T extends VDomRenderer.IModel> extends Widget\n"
                b"+abstract class VDomRenderer<T extends VDomRenderer.IModel | null> extends Widget\n"
            ),
            "test/src/apputils/vdom.spec.ts": (
                b"+class TestWidgetNoModel extends VDomRenderer<null> {\n"
            ),
        },
    )
    assert audit["fixed_only_api"] is True
    assert audit["buggy_compile_expected"] is False


def test_gate4_module_has_no_runtime_entrypoint() -> None:
    assert not hasattr(module, "run_candidate")
    assert not hasattr(module, "build_candidate")
