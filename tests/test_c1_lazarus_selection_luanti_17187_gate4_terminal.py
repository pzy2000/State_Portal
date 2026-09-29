from warptest.c1_lazarus_selection.luanti_17187_gate4_terminal import _audit_patch


def test_17187_preserved_translation_test_uses_pr_introduced_helpers() -> None:
    raw = """diff --git a/src/server.cpp b/src/server.cpp
diff --git a/src/translation.cpp b/src/translation.cpp
+const std::string_view Translations::getFileBaseName(std::string_view filename)
diff --git a/src/translation.h b/src/translation.h
+\tstatic const std::string_view getFileBaseName(std::string_view filename);
+\tstatic inline bool isTranslationFileType(std::string_view filename)
diff --git a/src/test/test_translations.cpp b/src/test/test_translations.cpp
+\t\tCHECK(Translations::getFileBaseName(TEST_PO_NAME) == "translation_po.de");
+\t\tCHECK(Translations::isTranslationFileType(TEST_PO_NAME));
"""
    result = _audit_patch(raw.encode())
    assert result["incompatible"] is True
    assert len(result["fixed_test_calls"]) == 2
