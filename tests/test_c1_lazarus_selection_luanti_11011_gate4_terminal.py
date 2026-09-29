from warptest.c1_lazarus_selection.luanti_11011_gate4_terminal import (
    _audit_fixed_only_api,
)


def test_11011_fixed_schematic_tests_are_incompatible_with_production_inverse() -> None:
    production = (
        "-bool Schematic::deserializeFromMts(std::istream *is,\n",
        "+bool Schematic::deserializeFromMts(std::istream *is)\n",
        "-bool Schematic::serializeToMts(std::ostream *os,\n",
        "+bool Schematic::serializeToMts(std::ostream *os) const\n",
        "-bool Schematic::serializeToLua(std::ostream *os,\n",
        "+bool Schematic::serializeToLua(std::ostream *os, bool use_comments, u32 indent_spaces) const\n",
    )
    test = (
        "+\tUASSERT(schem.serializeToMts(&ss));\n",
        "+\tUASSERT(schem2.deserializeFromMts(&ss));\n",
        "+\tUASSERT(schem.serializeToLua(&ss, false, 0));\n",
    )
    audit = _audit_fixed_only_api(
        {
            "src/mapgen/mg_schematic.cpp": production[:6],
            "src/mapgen/mg_schematic.h": (),
            "src/nodedef.cpp": (),
            "src/nodedef.h": (),
            "src/script/lua_api/l_mapgen.cpp": (),
            "src/unittest/test_noderesolver.cpp": (),
            "src/unittest/test_schematic.cpp": test,
        }
    )
    assert audit["incompatible"] is True
    assert len(audit["fixed_test_calls"]) == 3


def test_11011_without_preserved_new_calls_does_not_claim_gate4_failure() -> None:
    production = (
        "-bool Schematic::deserializeFromMts(std::istream *is,\n",
        "+bool Schematic::deserializeFromMts(std::istream *is)\n",
        "-bool Schematic::serializeToMts(std::ostream *os,\n",
        "+bool Schematic::serializeToMts(std::ostream *os) const\n",
        "-bool Schematic::serializeToLua(std::ostream *os,\n",
        "+bool Schematic::serializeToLua(std::ostream *os, bool use_comments, u32 indent_spaces) const\n",
    )
    audit = _audit_fixed_only_api(
        {
            "src/mapgen/mg_schematic.cpp": production,
            "src/mapgen/mg_schematic.h": (),
            "src/nodedef.cpp": (),
            "src/nodedef.h": (),
            "src/script/lua_api/l_mapgen.cpp": (),
            "src/unittest/test_noderesolver.cpp": (),
            "src/unittest/test_schematic.cpp": (),
        }
    )
    assert audit["incompatible"] is False
