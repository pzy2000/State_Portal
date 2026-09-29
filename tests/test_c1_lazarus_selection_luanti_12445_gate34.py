from warptest.c1_lazarus_selection import luanti_12445_gate34 as module


def test_12445_fixed_only_lua_api_is_terminal_for_bundled_lua() -> None:
    sections = {path: () for path in module._PRODUCTION_PATHS + module._TEST_PATHS}
    sections["lib/lua/src/lua.h"] = (
        "+typedef int (*lua_CFunctionwrapper)(lua_State *L, lua_CFunction f);\n",
        "+LUA_API lua_CFunctionwrapper (lua_atccall) (lua_State *L,\n",
    )
    sections["lib/lua/src/lapi.c"] = (
        "+LUA_API lua_CFunctionwrapper lua_atccall (lua_State *L,\n",
    )
    sections["src/unittest/test_lua.cpp"] = (
        "+#else\n",
        "+\tlua_atccall(L, wrapper);\n",
    )

    audit = module._audit_fixed_only_api(sections)

    assert audit["incompatible"] is True
    assert audit["discriminatory_configuration"] == "USE_LUAJIT=0 bundled PUC Lua"


def test_12445_no_test_call_does_not_claim_gate4_failure() -> None:
    sections = {path: () for path in module._PRODUCTION_PATHS + module._TEST_PATHS}
    sections["lib/lua/src/lua.h"] = (
        "+typedef int (*lua_CFunctionwrapper)(lua_State *L, lua_CFunction f);\n",
        "+LUA_API lua_CFunctionwrapper (lua_atccall) (lua_State *L,\n",
    )
    sections["lib/lua/src/lapi.c"] = (
        "+LUA_API lua_CFunctionwrapper lua_atccall (lua_State *L,\n",
    )

    assert module._audit_fixed_only_api(sections)["incompatible"] is False


def test_12445_module_has_no_candidate_runner() -> None:
    assert not hasattr(module, "run_candidate")
    assert not hasattr(module, "build_candidate")
