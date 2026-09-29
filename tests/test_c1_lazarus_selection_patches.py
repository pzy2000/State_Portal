from __future__ import annotations

from warptest.c1_lazarus_selection.patches import classify_unified_patch


def test_patch_classifier_handles_quoted_rename_binary_and_relevant_test() -> None:
    raw = b"""diff --git \"a/source/old file.cc\" \"b/source/new file.cc\"
similarity index 90%
rename from source/old file.cc
rename to source/new file.cc
--- \"a/source/old file.cc\"
+++ \"b/source/new file.cc\"
@@ -1 +1 @@
-return 0;
+return 1;
diff --git a/tests/test_new_file.cc b/tests/test_new_file.cc
new file mode 100644
--- /dev/null
+++ b/tests/test_new_file.cc
@@ -0,0 +1 @@
+assert(run() == 1);
diff --git a/assets/sample.bin b/assets/sample.bin
Binary files a/assets/sample.bin and b/assets/sample.bin differ
"""

    decision = classify_unified_patch(raw)

    assert decision.status == "eligible"
    assert decision.production_paths == ("source/new file.cc",)
    assert decision.test_paths == ("tests/test_new_file.cc",)


def test_patch_classifier_rejects_patch_without_regression_test() -> None:
    decision = classify_unified_patch(
        b"diff --git a/src/main.py b/src/main.py\n"
        b"--- a/src/main.py\n+++ b/src/main.py\n"
        b"@@ -1 +1 @@\n-old\n+new\n"
    )

    assert decision.status == "rejected"
    assert decision.reason == "no_regression_test"
