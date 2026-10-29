from token_tracker.providers import _tidy_codex_title


def test_brief_heading_project_tag_and_worker_are_dropped():
    assert _tidy_codex_title("# [Orchestrator] Terra · perf audit", "gpt-6.1-terra") == "perf audit"
    assert _tidy_codex_title("Sol · review providers-diff", "GPT-6.1 Sol") == "review providers-diff"


def test_plain_titles_and_unrelated_prefixes_are_kept():
    assert _tidy_codex_title("Perf attribution", "gpt-6.1-terra") == "Perf attribution"
    assert _tidy_codex_title("Build · release notes", "gpt-6.1-terra") == "Build · release notes"
    assert _tidy_codex_title("#", "gpt-6.1-terra") == "#"


def test_only_whole_model_words_and_real_headings_are_dropped():
    assert _tidy_codex_title("6.1 · release notes", "gpt-6.1-terra") == "6.1 · release notes"
    assert _tidy_codex_title("Ter · actual task", "gpt-6.1-terra") == "Ter · actual task"
    assert _tidy_codex_title("#123 crash", "gpt-6.1-terra") == "#123 crash"
    assert _tidy_codex_title("#include <vector>", "gpt-6.1-terra") == "#include <vector>"


def test_lossy_question_mark_separator_is_read_as_brief_separator():
    title = "Sol ? review packaging ? design review of installer, upgrade, uninstall and rollback"
    assert _tidy_codex_title(title, "gpt-6.1-sol") == "review packaging"
    assert _tidy_codex_title("Luna ? frozen-probe ? prove whether the venv survives", "gpt-6-luna") == "frozen-probe"


def test_replacement_character_separator_is_read_as_brief_separator():
    title = "Sol � review packaging � design review"
    assert _tidy_codex_title(title, "gpt-6.1-sol") == "review packaging"


def test_goal_after_dash_is_cut_from_brief_title():
    assert _tidy_codex_title("Sol · task — goal", "gpt-6.1-sol") == "task"
    assert _tidy_codex_title("Sol · task – goal", "gpt-6.1-sol") == "task"


def test_goal_separator_with_empty_task_keeps_remainder():
    assert _tidy_codex_title("Sol · — goal text", "gpt-6.1-sol") == "— goal text"


def test_question_mark_titles_with_non_model_head_are_unchanged():
    assert _tidy_codex_title("Why is it slow ? fix it", "gpt-6.1-sol") == "Why is it slow ? fix it"
    assert _tidy_codex_title("Sol ? task", "gpt-6.1-terra") == "Sol ? task"
    assert _tidy_codex_title("Why is it slow ?? fix it", "gpt-6.1-sol") == "Why is it slow ?? fix it"


def test_run_of_question_marks_is_a_separator_in_brief_titles():
    assert _tidy_codex_title("Luna ?? state-audit ??? establish ground", "gpt-6-luna") == "state-audit"
    assert _tidy_codex_title("Luna ? state-audit ?? establish ground", "gpt-6-luna") == "state-audit"


def test_replacement_run_cuts_title_without_model_head():
    assert _tidy_codex_title("review claude-support � design review of", "gpt-6.1-sol") == "review claude-support"
    assert _tidy_codex_title("review claude-support �� design review of", "gpt-6.1-sol") == "review claude-support"


def test_standalone_replacement_characters_are_stripped():
    assert _tidy_codex_title("� task name", "gpt-6.1-sol") == "task name"
    assert _tidy_codex_title("task name �", "gpt-6.1-sol") == "task name"


def test_mojibake_is_repaired_before_tidying():
    mojibake = "Sol · pytorch-b — author exercise families".encode("utf-8").decode("cp1252")
    assert _tidy_codex_title(mojibake, "gpt-6.1-sol") == "pytorch-b"


def test_undecodable_mojibake_marker_keeps_original_title():
    title = "Caf\u00c2"
    assert _tidy_codex_title(title, "gpt-6.1-terra") == title


def test_only_first_non_empty_line_is_kept():
    assert _tidy_codex_title("row layout\nGoal: make parent and subagent chat rows", "gpt-6.1-sol") == "row layout"
    assert _tidy_codex_title("Kept-module test fixes\nGoal: restore fixtures", "gpt-6.1-sol") == "Kept-module test fixes"
    assert _tidy_codex_title("\n\nPerf attribution\nGoal: x", "gpt-6.1-terra") == "Perf attribution"


def test_single_line_title_is_unchanged():
    assert _tidy_codex_title("Perf attribution", "gpt-6.1-terra") == "Perf attribution"


def test_model_family_names_are_head_words_without_model_id():
    assert _tidy_codex_title("Luna · core loop", None) == "core loop"
    assert _tidy_codex_title("Terra · fix runtime F4 F5 F7\nGoal: make the runtime pass", None) == "fix runtime F4 F5 F7"
    assert _tidy_codex_title("astra ? plan lanes", "") == "plan lanes"


def test_model_family_fallback_does_not_apply_when_model_id_is_present():
    assert _tidy_codex_title("Sol ? task", "gpt-6.1-terra") == "Sol ? task"
    assert _tidy_codex_title("Luna · core loop", "gpt-6.1-terra") == "Luna · core loop"


def test_non_model_heads_are_unchanged_with_or_without_model_id():
    assert _tidy_codex_title("Track Route Review · review user-approved track URI change", "gpt-6.1-sol") == (
        "Track Route Review · review user-approved track URI change")
    assert _tidy_codex_title("Track Route Review · review user-approved track URI change", None) == (
        "Track Route Review · review user-approved track URI change")
    assert _tidy_codex_title("Why is it slow ? fix it", None) == "Why is it slow ? fix it"
