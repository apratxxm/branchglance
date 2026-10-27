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
