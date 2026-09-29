"""Immutable Gate 1--4 profiles for modern JupyterLab freeze-sprint candidates.

Every profile pins the authoritative capture fingerprints, the materialized
fixed/buggy source fingerprints and the two registered Jest full names, so the
shared Gate 1--7 runner can recompute Gate 1--4 without trusting any narrative.
"""

from __future__ import annotations

from .jupyterlab_modern_gate57 import ModernCandidateProfile


JUPYTERLAB_19227 = ModernCandidateProfile(
    change_number=19227,
    issue_number=19226,
    fixed_revision="10495e8e976f073fc765ddd5ef517d5a51c594ab",
    base_revision="7b7c2cde59c6e9c510623c2450418e34923c265b",
    package_directory="packages/ui-components",
    production_paths=("packages/ui-components/src/components/toolbar.tsx",),
    test_path="packages/ui-components/test/toolbar.spec.ts",
    build_outputs=("packages/ui-components/lib/components/toolbar.js",),
    f2p_full_name=(
        "@jupyterlab/ui-components ReactiveToolbar #insertBefore() "
        "should insert before the target after intermediate items have been removed"
    ),
    p2p_full_name=(
        "@jupyterlab/ui-components Toolbar #insertBefore() "
        "should insert an item into the toolbar before `c`"
    ),
    f2p_test_name=(
        "should insert before the target after intermediate items have been removed"
    ),
    p2p_test_name="should insert an item into the toolbar before `c`",
    profile_id="jupyterlab-19227-node22-reactive-toolbar-positions-v1",
    classification="real_bug_state_desync",
    issue_reproducer=(
        "removing a reactive toolbar item leaves stale entries in the stored "
        "position map, so a later insertRelative places the item in the wrong slot"
    ),
    production_change=(
        "track child removals and shift stored positions so the reactive toolbar "
        "position map stays in sync with the rendered items"
    ),
    raw_hashes={
        "commit.json": (
            "8603f47b38ebf4b85a4fcc58367dcd61eb2d362e0084b6df464fe9a2e32cfcb6"
        ),
        "commit.diff": (
            "42c78c42f28bc5c80651df2411a9e7b14880224316202331dc8ea57b25d7d7e3"
        ),
        "issue-19226.json": (
            "0a1e9cf1edea29ae89a39b7e133ce99041931f78b2cde6d4e065315b90cbfd91"
        ),
        "issue-timeline.json": (
            "60f9fc8478edfdcd44d7ac64ca0f4e300f81a0cc5bcedae13b335c29e2bc906c"
        ),
        "pull.json": (
            "8940a3e5ef0c8051ab76b5c04ff7f784c38d854bc7c93b37beee936a13901546"
        ),
        "files.json": (
            "87d02ad3c742cd0c3166c0cd009575754d6f26ebfab5ab9c27b6f9aa2e6f7e8b"
        ),
    },
    source_hashes={
        "fixed": "572aa2778ad8150f679c9cca7e28da8dacd556da1a59ee5337d3b96f241823b9",
        "buggy": "5689d409647fc4476d7f2cb6b81f2a6906b03deb1b4f602e79981afd57660ea4",
    },
    test_hash="f3bd76ad64c742a257cee533f39941fe0d4e331b30d0705799cb987804b1ceaf",
    issue_title_markers=("insertRelative()", "reactive toolbar"),
    issue_body_markers=("_widgetPositions",),
    production_patch_markers=("onChildRemoved", "_movingToPopup"),
    test_patch_markers=(
        "should insert before the target after intermediate items have been removed",
    ),
    fixed_only_markers=("onChildRemoved", "_movingToPopup"),
    existing_api_used=(
        "ReactiveToolbar",
        "addItem",
        "insertBefore",
        "names",
        "Widget.dispose",
    ),
)


JUPYTERLAB_19136 = ModernCandidateProfile(
    change_number=19136,
    issue_number=19114,
    fixed_revision="ff769d7dd7de4807bf12245ce0775e2881b71b17",
    base_revision="19d94b1e32ab12c921bd65310749d9792c2ee5d5",
    package_directory="packages/application",
    production_paths=("packages/application/src/shell.ts",),
    test_path="packages/application/test/shell.spec.ts",
    build_outputs=("packages/application/lib/shell.js",),
    f2p_full_name=(
        "LabShell #activityBarPosition "
        "should restore a horizontal right activity bar with no current widget"
    ),
    p2p_full_name=(
        "LabShell #activityBarPosition "
        "should report the collapsed state via rightCollapsed in horizontal mode"
    ),
    f2p_test_name="should restore a horizontal right activity bar with no current widget",
    p2p_test_name="should report the collapsed state via rightCollapsed in horizontal mode",
    profile_id="jupyterlab-19136-node22-horizontal-activity-bar-restore-v1",
    classification="real_bug_layout_restore",
    issue_reproducer=(
        "with the activity bar at the bottom, a saved side area is rehydrated as "
        "collapsed, so the right sidebar restores with width zero"
    ),
    production_change=(
        "derive the dehydrated collapsed flag from the user collapse state instead "
        "of the side bar current title when the activity bar is horizontal"
    ),
    raw_hashes={
        "commit.json": (
            "6ac44159ad46101f53bcae525a10dc66caf479c3d64114ded7158dd75214e4db"
        ),
        "commit.diff": (
            "3d83dc8acd2d28d1e981092614bb55b51e480cd87ea0f8fd1f83d4d8e896de51"
        ),
        "issue-19114.json": (
            "0d1de1d5c623b20fce9a1336cd3f51803bc75d0c1e13e3cca5af01891f52221d"
        ),
        "issue-timeline.json": (
            "b75288b560e602ed405eba1c2a9963ca1cf17a213c0447e68d5ddb427eac77d7"
        ),
        "pull.json": (
            "1a3950ef92fb0ad83b7bd51332c61bb7d516a2613d9e9c497cc0be44d626895c"
        ),
        "files.json": (
            "6fea04c34e694e2b785ba79f81eada8b8cc695dc2a18bcd9c9b1715972e868a4"
        ),
    },
    source_hashes={
        "fixed": "3dd3f616435a1dd7399052a0846174c52099bd2d805abf1480b286efc394256f",
        "buggy": "43e992646b84d4abe6135d832f6af43ad10c262f0f21ef470b17e4bb55b23a46",
    },
    test_hash="ba4ea241ee7f2ac21f18adedf1fbe5fc428f0df6876ad1d2f688b903860c0b17",
    issue_title_markers=("Right sidebar", "width zero"),
    issue_body_markers=("sidebar had width zero",),
    production_patch_markers=(": this._isCollapsedByUser;",),
    test_patch_markers=(
        "should restore a horizontal right activity bar with no current widget",
    ),
    fixed_only_markers=(": this._isCollapsedByUser;",),
    existing_api_used=(
        "LabShell",
        "updateConfig",
        "add",
        "saveLayout",
        "rightCollapsed",
    ),
)


JUPYTERLAB_18662 = ModernCandidateProfile(
    change_number=18662,
    issue_number=18660,
    fixed_revision="131029c354651aa064a30dc42a7fba824403f31c",
    base_revision="dea52ebec6356f6c673a815286babb7b2e116a1f",
    package_directory="packages/completer",
    production_paths=("packages/completer/src/ghost.ts",),
    test_path="packages/completer/test/inline.spec.ts",
    build_outputs=("packages/completer/lib/ghost.js",),
    f2p_full_name=(
        "completer/inline InlineCompleter #configure() `minLines` "
        "should reserve space when maxLines is 0 (unlimited)"
    ),
    p2p_full_name=(
        "completer/inline InlineCompleter #configure() `minLines` "
        "should add empty lines when needed"
    ),
    f2p_test_name="`minLines` should reserve space when maxLines is 0 (unlimited)",
    p2p_test_name="should add empty lines when needed",
    profile_id="jupyterlab-18662-node22-inline-completer-reserved-lines-v1",
    classification="real_bug_falsy_default",
    issue_reproducer=(
        "with the default maxLines of 0, meaning unlimited, the ghost text widget "
        "treats the value as falsy and reserves no lines for inline completions"
    ),
    production_change=(
        "normalise the unlimited maxLines default to Infinity before clamping "
        "minLines so reserved ghost lines are added"
    ),
    raw_hashes={
        "commit.json": (
            "954de5837891308c44164ac5ccd97def677e1ff4f99d3fe4b0c1eb11a0196a4c"
        ),
        "commit.diff": (
            "eedf556ec2792d235ca60c34bffe4c21e067e3057e98f1f8e10052ea8413dbf5"
        ),
        "issue-18660.json": (
            "8bf4fa8ff482d37443d7b315898b091c1706515d90a02d62f1cb0ba1b774d3b7"
        ),
        "issue-timeline.json": (
            "c675157fb43c29a2a681003bd325d3dd174ecf96f2e0cebac518422dda5fdf6e"
        ),
        "pull.json": (
            "947610a910f0cbc071400a11f86c9924378dba629153616f5678f68844561424"
        ),
        "files.json": (
            "f20a5ab0472547251b30aaba6749912c1570d718ede26633cd141a706a1bf175"
        ),
    },
    source_hashes={
        "fixed": "08ada09fa31ef189de76de5a1318292776e5f902ba37b1ed03bb2e417d9a51c8",
        "buggy": "42d4b3312747d0770d2835f144810a35e6f0b1fe94ad7e1c0f927f4752f6c702",
    },
    test_hash="38cdc9d6e475f5c71848ca05109cbfb9141bb743e066b812ec6e12c2d2b0007e",
    issue_title_markers=("Reserve lines for inline completion", "no effect"),
    issue_body_markers=("Reserve space for the longest candidate",),
    production_patch_markers=("this.options.maxLines || Infinity",),
    test_patch_markers=("should reserve space when maxLines is 0 (unlimited)",),
    fixed_only_markers=("this.options.maxLines || Infinity",),
    existing_api_used=(
        "InlineCompleter",
        "configure",
        "InlineCompleter.Model.setCompletions",
        "GhostTextManager",
    ),
)


JUPYTERLAB_18992 = ModernCandidateProfile(
    change_number=18992,
    issue_number=18991,
    fixed_revision="fae3f38683c37472bbf8e779c92306d7ee2f76af",
    base_revision="4e61e07d0a91145b53fbf96ac74b0387f6bc51f6",
    package_directory="packages/notebook",
    production_paths=("packages/notebook/src/widget.ts",),
    test_path="packages/notebook/test/widget.spec.ts",
    build_outputs=("packages/notebook/lib/widget.js",),
    f2p_full_name=(
        "@jupyter/notebook StaticNotebook #moveCell() "
        "should refresh hidden cells after moving a collapsed heading down"
    ),
    p2p_full_name=(
        "@jupyter/notebook StaticNotebook #moveCell() "
        "should preserve isDirty state after moving a code cell"
    ),
    f2p_test_name="should refresh hidden cells after moving a collapsed heading down",
    p2p_test_name="should preserve isDirty state after moving a code cell",
    profile_id="jupyterlab-18992-node22-collapsed-heading-visibility-v1",
    classification="real_bug_stale_visibility",
    issue_reproducer=(
        "moving a collapsed heading section down past a following section leaves "
        "the moved cells hidden, so they disappear from the notebook"
    ),
    production_change=(
        "bound the move target and recompute hidden cells from the collapsed "
        "heading structure after every cell move"
    ),
    raw_hashes={
        "commit.json": (
            "de3e1f20fe70d36b8b5c5b2fb5cbd2b9ecffc67344fb462fe8624e24f2c52c0c"
        ),
        "commit.diff": (
            "a14e005e9dcfc8299f65f6aca29e52eb999e32f885af010797847235d668164d"
        ),
        "issue-18991.json": (
            "fc832ed4edae4f62c75d939eb7fadd7a8fa64652f7d624bc95ebdbfb49807f36"
        ),
        "issue-timeline.json": (
            "8fb2f2279daa68850c7f46fddc907ab16b0ad4f98d0416047129c3facff8940f"
        ),
        "pull.json": (
            "b95d71e8e5eeaf03bdc13339e09d0765ea594874c5b8d079eff81c719fdbd56c"
        ),
        "files.json": (
            "d986733831df0efc4ad638a2dc2f535a2201327b6c04045c5065f2fb26af54ea"
        ),
    },
    source_hashes={
        "fixed": "9c4cf9348b821a69d30edc8cd6586bbe00a6351e030dc96d8dc27da389a7f158",
        "buggy": "73c2891043de6b00d86dcdc3f6ae53b6c70aa508421f92dfe988bec44202ea03",
    },
    test_hash="e8f152721a46ae44744698d90be7d638a360c553c204e75c45216a4dd78b5ed4",
    issue_title_markers=("Cells disappear", "moving collapsed cells"),
    issue_body_markers=("disappear into the void",),
    production_patch_markers=("_refreshCollapsedHeadingVisibility",),
    test_patch_markers=(
        "should refresh hidden cells after moving a collapsed heading down",
    ),
    fixed_only_markers=("_refreshCollapsedHeadingVisibility",),
    existing_api_used=(
        "StaticNotebook",
        "moveCell",
        "NotebookActions.setHeadingCollapse",
        "MarkdownCell",
    ),
)


JUPYTERLAB_19121 = ModernCandidateProfile(
    change_number=19121,
    issue_number=19120,
    fixed_revision="a9c79effa9c295494772906831ebe9a24fe5c748",
    base_revision="458355fdd5b393c2d338c89bb0588634f737b0ea",
    package_directory="packages/shortcuts-extension",
    production_paths=("packages/shortcuts-extension/src/components/ShortcutUI.tsx",),
    test_path="packages/shortcuts-extension/test/components/ShortcutUI.spec.ts",
    build_outputs=("packages/shortcuts-extension/lib/components/ShortcutUI.js",),
    f2p_full_name=(
        "@jupyterlab/shortcut-extension ShortcutUI #changed "
        "should refresh shortcut list when settings change externally"
    ),
    p2p_full_name=(
        "@jupyterlab/shortcut-extension ShortcutUI #addKeybinding() "
        "should add a keybinding for given target"
    ),
    f2p_test_name="should refresh shortcut list when settings change externally",
    p2p_test_name="should add a keybinding for given target",
    profile_id="jupyterlab-19121-node22-shortcut-settings-refresh-v1",
    classification="real_bug_missing_signal_subscription",
    issue_reproducer=(
        "saving user preferences while the shortcuts settings UI is open leaves the "
        "displayed shortcut list stale until the tab is reopened"
    ),
    production_change=(
        "keep the loaded settings object and refresh the shortcut list whenever its "
        "changed signal fires"
    ),
    raw_hashes={
        "commit.json": (
            "9dfd8e7a6d36669a8022c80bd5f07cd43db3f8150aedac416298473244fe7e17"
        ),
        "commit.diff": (
            "69421cceee55e97c8bb57c586d8e83566ab7416f7e60d3ba9036727c42af7786"
        ),
        "issue-19120.json": (
            "8c48ece6b675b431ca3e27c1d2b6101d1fde07876067f029ebe24b246022da37"
        ),
        "issue-timeline.json": (
            "d7c5c47b184ae5cfced92305de6a1b4fe0ec971d8c04eb119a1d186abf3de5ed"
        ),
        "pull.json": (
            "f1ed8f26116881a93c00bf15b2325e69b0fd3ea606d114c1a79e253fb76c8cad"
        ),
        "files.json": (
            "73ec7c75b8c3170a02fa5cdf3c4a9792253382ee19e04a85b8c6c3c7fb693f7c"
        ),
    },
    source_hashes={
        "fixed": "cf0f8ac92eca09b4aec178ed7f4128c3fdbbe397b25a98e9d3d07288a971c910",
        "buggy": "84eb61573cea5726144d39745f18a11f8cfcc5000ba4b1bda358fd0682f96a22",
    },
    test_hash="51deec740dfa783c806a1f09840c22c37c905a233892a19a531283e473bf5f61",
    issue_title_markers=("Settings Editor does not update",),
    issue_body_markers=("do not see the changes updated",),
    production_patch_markers=("_disconnectSettings",),
    test_patch_markers=("should refresh shortcut list when settings change externally",),
    fixed_only_markers=("_disconnectSettings",),
    existing_api_used=("ShortcutUI", "SettingRegistry", "Settings", "createRoot"),
)


JUPYTERLAB_18579 = ModernCandidateProfile(
    change_number=18579,
    issue_number=11140,
    fixed_revision="35f4e09757cfcceec50ca0079058e70bdf129d72",
    base_revision="b939aae888ba4063d37868e34c21155015591c9e",
    package_directory="packages/notebook",
    production_paths=(
        "packages/notebook/src/actions.tsx",
        "packages/notebook-extension/src/index.ts",
    ),
    test_path="packages/notebook/test/actions.spec.ts",
    build_outputs=("packages/notebook/lib/actions.js",),
    f2p_full_name=(
        "@jupyterlab/notebook NotebookActions #splitCell() "
        "should be a no-op if the active cell is not editable"
    ),
    p2p_full_name=(
        "@jupyterlab/notebook NotebookActions #splitCell() "
        "should split the active cell into two cells"
    ),
    f2p_test_name="should be a no-op if the active cell is not editable",
    p2p_test_name="should split the active cell into two cells",
    profile_id="jupyterlab-18579-node22-readonly-cell-split-merge-v1",
    classification="real_bug_missing_guard",
    issue_reproducer=(
        "a cell marked non-editable can still be split or merged, so read-only "
        "content is modified"
    ),
    production_change=(
        "reject split and merge on non-editable cells and report the refusal "
        "through a notification"
    ),
    raw_hashes={
        "commit.json": (
            "1279dab3f1555687fd8aec74eb0a8de7dece03271f6fc10c9c668719f4072173"
        ),
        "commit.diff": (
            "92ef4f20a4cddce518bd02a278c3f3800e267cf78ee5caec76d487c7bc2cd2c4"
        ),
        "issue-11140.json": (
            "3b5a2984617e9451326b964b9fed18470ec15828469b563f27a5e6d04bc37b93"
        ),
        "issue-timeline.json": (
            "7acc840d9789deb35917ee58c6a981ac64caec181358a87337dd7ce47712634f"
        ),
        "pull.json": (
            "478156baff896d88efb94709319cda63dcb3aeb2472c5fe575d15ab0df89d0fe"
        ),
        "files.json": (
            "bc90bc9e1eac65de3e120d74ac996ec4bf2b99c2ba0dc7c577a270f2c636740b"
        ),
    },
    source_hashes={
        "fixed": "17e6e8f8bee9101526200daca263442a65c5aabe1359d3607f072053643fe734",
        "buggy": "01dab223cb6c1aae2d74146db7d002859112bfb68816135ef58428244ce7a459",
    },
    test_hash="307c004557816f8d9b66cb55653a6ed706c563c3809b16474568be1ef9844363",
    issue_title_markers=("Non-editable cells can be split and merged",),
    issue_body_markers=("editable",),
    production_patch_markers=("notifySplitReadOnlyAction",),
    test_patch_markers=("should be a no-op if the active cell is not editable",),
    fixed_only_markers=("notifySplitReadOnlyAction",),
    existing_api_used=(
        "NotebookActions.splitCell",
        "NotebookActions.mergeCells",
        "CellModel.getMetadata",
    ),
)


JUPYTERLAB_18518 = ModernCandidateProfile(
    change_number=18518,
    issue_number=15083,
    fixed_revision="149cfb63a45592a085adc352785ea6de9b34378e",
    base_revision="608b4257c31f04e8abf29d01303eeb286fe06227",
    package_directory="packages/services",
    production_paths=("packages/services/src/serverconnection.ts",),
    test_path="packages/services/test/serverconnection.spec.ts",
    build_outputs=("packages/services/lib/serverconnection.js",),
    f2p_full_name=(
        "ServerConnection XSRF cookie handling "
        "should use the last matching cookie when duplicates exist"
    ),
    p2p_full_name=(
        "ServerConnection .makeRequest() should make a request to the server"
    ),
    f2p_test_name="should use the last matching cookie when duplicates exist",
    p2p_test_name="should make a request to the server",
    profile_id="jupyterlab-18518-node22-xsrf-duplicate-cookie-v1",
    classification="real_bug_wrong_cookie_selection",
    issue_reproducer=(
        "duplicate _xsrf cookies at different paths make the client send the "
        "first cookie while the server keeps the last, so requests are rejected"
    ),
    production_change=(
        "select the last matching cookie value instead of the first regular "
        "expression match, matching the server side parser"
    ),
    raw_hashes={
        "commit.json": (
            "e90d25631ff0762a87c3c1a3e3a1293fc125061554f919bd9553f5ace599a908"
        ),
        "commit.diff": (
            "6db74a8f1eb1c897461cbcdd560e49c09af71c7234d01d740953509c2f62ec4d"
        ),
        "issue-15083.json": (
            "063f733008872ea3a1823de37b3f291532e3ae0f34527078ba64fe2c7dadd68f"
        ),
        "issue-timeline.json": (
            "039a0f96c0b51e970e03eb2c34fdadc1980ba0d47dcb1f928104e5c143572fea"
        ),
        "pull.json": (
            "0c1cd80250e1b2e96dbd5a86e7aab6bbf83f88b56ccf5b2e3463f6c27512eec2"
        ),
        "files.json": (
            "ac100110d5c6bae35c8ad7f01b15a5e9c0f4392a563c17a106974a1e4ad98c18"
        ),
    },
    source_hashes={
        "fixed": "07630432099947c278d5022da949605ff34ae01c16e1a01b59aadfd6a5b181fa",
        "buggy": "83a42ca457cc0c543f0db9c141017a4ad7e952ce171c53b5f4f3c5d23f59e79d",
    },
    test_hash="d7dae9f49c7e41eceaea7c265bf84984990a89b32a7433ec3d6517b5ef1b6989",
    issue_title_markers=("XSRF Cookies do not match Post Argument",),
    issue_body_markers=("XSRF Cookies do not match Post Argument",),
    production_patch_markers=("let lastValue",),
    test_patch_markers=("should use the last matching cookie when duplicates exist",),
    fixed_only_markers=("let lastValue",),
    existing_api_used=(
        "ServerConnection.makeSettings",
        "ServerConnection.makeRequest",
        "document.cookie",
    ),
)


MODERN_PROFILES = {
    profile.change_number: profile
    for profile in (
        JUPYTERLAB_19227,
        JUPYTERLAB_19136,
        JUPYTERLAB_18662,
        JUPYTERLAB_18992,
        JUPYTERLAB_19121,
        JUPYTERLAB_18579,
        JUPYTERLAB_18518,
    )
}


__all__ = [
    "JUPYTERLAB_18518",
    "JUPYTERLAB_18579",
    "JUPYTERLAB_18662",
    "JUPYTERLAB_18992",
    "JUPYTERLAB_19121",
    "JUPYTERLAB_19136",
    "JUPYTERLAB_19227",
    "MODERN_PROFILES",
]
