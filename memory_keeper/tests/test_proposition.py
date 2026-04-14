"""Unit tests for memory_keeper.proposition and memory_keeper.pending.

Coverage:
  1.  Proposition.to_dict() / from_dict() round-trip
  2.  from_dict() fallbacks: invalid type → "fact", scores=None → zero dict
  3.  created_at has timezone (+00:00)
  4.  append_pending + load_pending round-trip (tmp_path)
  5.  append_pending dedup — same content not written twice
  6.  update_status changes status
  7.  collect_approved returns approved props and marks them consumed
  8.  DRY_RUN: append_pending and update_status do NOT write files
  9.  proposition_audit mock: total>=5 → write/pending(bootstrap), 3-4 → pending, <=2 → discard
  10. _load_audit_context returns (str, bool)
  11. _build_few_shot contains a discard sample
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

import memory_keeper._config as _cfg_mod


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_prop(**overrides):
    """Create a minimal valid Proposition without touching the file system."""
    from memory_keeper.proposition import Proposition

    defaults = dict(
        content="用户偏好使用rtk前缀",
        type="preference",
        support_status="supported",
        scores={"durability": 2, "novelty": 2, "actionability": 2, "confidence": 2, "scope": 2},
        total_score=10,
        action="write",
        source_session="session_abc.jsonl",
        source_excerpt="所有Bash命令加rtk",
    )
    defaults.update(overrides)
    return Proposition(**defaults)


# ── 1. to_dict / from_dict round-trip ────────────────────────────────────────

class TestPropositionSerialization:
    def test_roundtrip_identity(self):
        from memory_keeper.proposition import Proposition

        prop = _make_prop()
        d = prop.to_dict()
        prop2 = Proposition.from_dict(d)

        assert prop2.content == prop.content
        assert prop2.type == prop.type
        assert prop2.scores == prop.scores
        assert prop2.total_score == prop.total_score
        assert prop2.action == prop.action
        assert prop2.source_session == prop.source_session
        assert prop2.id == prop.id
        assert prop2.status == prop.status

    def test_to_dict_has_all_fields(self):
        prop = _make_prop()
        d = prop.to_dict()
        expected_keys = {
            "content", "type", "support_status", "scores", "total_score",
            "action", "source_session", "source_excerpt", "created_at", "status", "id",
        }
        assert expected_keys == set(d.keys())


# ── 2. from_dict fallbacks ────────────────────────────────────────────────────

class TestFromDictFallbacks:
    def test_invalid_type_falls_back_to_fact(self):
        from memory_keeper.proposition import Proposition

        prop = _make_prop()
        d = prop.to_dict()
        d["type"] = "nonexistent_type"
        prop2 = Proposition.from_dict(d)
        assert prop2.type == "fact"

    def test_none_scores_falls_back_to_zero_dict(self):
        from memory_keeper.proposition import Proposition

        prop = _make_prop()
        d = prop.to_dict()
        d["scores"] = None
        prop2 = Proposition.from_dict(d)
        assert prop2.scores == {
            "durability": 0,
            "novelty": 0,
            "actionability": 0,
            "confidence": 0,
            "scope": 0,
        }

    def test_missing_scores_key_falls_back_to_zero_dict(self):
        from memory_keeper.proposition import Proposition

        prop = _make_prop()
        d = prop.to_dict()
        del d["scores"]
        prop2 = Proposition.from_dict(d)
        assert prop2.scores == {
            "durability": 0,
            "novelty": 0,
            "actionability": 0,
            "confidence": 0,
            "scope": 0,
        }

    def test_all_valid_types_accepted(self):
        from memory_keeper.proposition import Proposition

        valid_types = {"preference", "decision", "fact", "correction", "pattern", "gotcha"}
        for t in valid_types:
            d = _make_prop().to_dict()
            d["type"] = t
            assert Proposition.from_dict(d).type == t


# ── 3. created_at timezone ────────────────────────────────────────────────────

class TestCreatedAtTimezone:
    def test_created_at_has_utc_offset(self):
        prop = _make_prop()
        # isoformat() with UTC timezone produces "+00:00"
        assert "+00:00" in prop.created_at

    def test_created_at_survives_roundtrip(self):
        from memory_keeper.proposition import Proposition

        prop = _make_prop()
        prop2 = Proposition.from_dict(prop.to_dict())
        assert prop2.created_at == prop.created_at


# ── 4. append_pending + load_pending round-trip ───────────────────────────────

class TestPendingRoundtrip:
    def test_append_and_load(self, tmp_path, monkeypatch):
        monkeypatch.setattr(_cfg_mod, "PENDING_STORE", tmp_path / "pending.jsonl")
        monkeypatch.setattr(_cfg_mod, "DRY_RUN", False)

        from memory_keeper.pending import append_pending, load_pending

        props = [_make_prop(content="命题A"), _make_prop(content="命题B")]
        append_pending(props)

        loaded = load_pending()
        assert len(loaded) == 2
        contents = {p.content for p in loaded}
        assert contents == {"命题A", "命题B"}

    def test_load_empty_if_no_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(_cfg_mod, "PENDING_STORE", tmp_path / "missing.jsonl")

        from memory_keeper.pending import load_pending

        assert load_pending() == []

    def test_load_status_filter(self, tmp_path, monkeypatch):
        monkeypatch.setattr(_cfg_mod, "PENDING_STORE", tmp_path / "pending.jsonl")
        monkeypatch.setattr(_cfg_mod, "DRY_RUN", False)

        from memory_keeper.pending import append_pending, load_pending

        p1 = _make_prop(content="pending prop", status="pending")
        p2 = _make_prop(content="approved prop", status="approved")
        append_pending([p1, p2])

        pending_only = load_pending(status="pending")
        assert len(pending_only) == 1
        assert pending_only[0].content == "pending prop"

        approved_only = load_pending(status="approved")
        assert len(approved_only) == 1
        assert approved_only[0].content == "approved prop"


# ── 5. append_pending dedup ───────────────────────────────────────────────────

class TestPendingDedup:
    def test_same_content_not_duplicated(self, tmp_path, monkeypatch):
        monkeypatch.setattr(_cfg_mod, "PENDING_STORE", tmp_path / "pending.jsonl")
        monkeypatch.setattr(_cfg_mod, "DRY_RUN", False)

        from memory_keeper.pending import append_pending, load_pending

        prop = _make_prop(content="重复命题内容")
        append_pending([prop])
        append_pending([prop])  # second append with same content

        loaded = load_pending()
        assert len(loaded) == 1

    def test_different_content_both_stored(self, tmp_path, monkeypatch):
        monkeypatch.setattr(_cfg_mod, "PENDING_STORE", tmp_path / "pending.jsonl")
        monkeypatch.setattr(_cfg_mod, "DRY_RUN", False)

        from memory_keeper.pending import append_pending, load_pending

        append_pending([_make_prop(content="内容一")])
        append_pending([_make_prop(content="内容二")])

        loaded = load_pending()
        assert len(loaded) == 2


# ── 6. update_status ─────────────────────────────────────────────────────────

class TestUpdateStatus:
    def test_update_existing_id(self, tmp_path, monkeypatch):
        monkeypatch.setattr(_cfg_mod, "PENDING_STORE", tmp_path / "pending.jsonl")
        monkeypatch.setattr(_cfg_mod, "DRY_RUN", False)

        from memory_keeper.pending import append_pending, load_pending, update_status

        prop = _make_prop(content="可审命题", status="pending")
        append_pending([prop])

        found = update_status(prop.id, "approved")
        assert found is True

        loaded = load_pending(status="approved")
        assert len(loaded) == 1
        assert loaded[0].id == prop.id

    def test_update_nonexistent_id_returns_false(self, tmp_path, monkeypatch):
        monkeypatch.setattr(_cfg_mod, "PENDING_STORE", tmp_path / "pending.jsonl")
        monkeypatch.setattr(_cfg_mod, "DRY_RUN", False)

        from memory_keeper.pending import append_pending, update_status

        prop = _make_prop(content="有效命题")
        append_pending([prop])

        found = update_status("nonexistent_id_xyz", "approved")
        assert found is False

    def test_update_missing_file_returns_false(self, tmp_path, monkeypatch):
        monkeypatch.setattr(_cfg_mod, "PENDING_STORE", tmp_path / "missing.jsonl")

        from memory_keeper.pending import update_status

        assert update_status("any_id", "approved") is False


# ── 7. collect_approved ───────────────────────────────────────────────────────

class TestCollectApproved:
    def test_collect_approved_marks_consumed(self, tmp_path, monkeypatch):
        monkeypatch.setattr(_cfg_mod, "PENDING_STORE", tmp_path / "pending.jsonl")
        monkeypatch.setattr(_cfg_mod, "DRY_RUN", False)

        from memory_keeper.pending import append_pending, collect_approved, load_pending

        approved = _make_prop(content="已批准命题", status="approved")
        pending = _make_prop(content="待审命题", status="pending")
        append_pending([approved, pending])

        result = collect_approved()
        assert len(result) == 1
        assert result[0].content == "已批准命题"

        # After collect, the record should be consumed
        consumed = load_pending(status="consumed")
        assert len(consumed) == 1
        assert consumed[0].content == "已批准命题"

        # Pending one should remain pending
        still_pending = load_pending(status="pending")
        assert len(still_pending) == 1

    def test_collect_auto_approved(self, tmp_path, monkeypatch):
        monkeypatch.setattr(_cfg_mod, "PENDING_STORE", tmp_path / "pending.jsonl")
        monkeypatch.setattr(_cfg_mod, "DRY_RUN", False)

        from memory_keeper.pending import append_pending, collect_approved

        auto = _make_prop(content="自动批准命题", status="auto_approved")
        append_pending([auto])

        result = collect_approved()
        assert len(result) == 1
        assert result[0].content == "自动批准命题"

    def test_collect_empty_when_none_approved(self, tmp_path, monkeypatch):
        monkeypatch.setattr(_cfg_mod, "PENDING_STORE", tmp_path / "pending.jsonl")
        monkeypatch.setattr(_cfg_mod, "DRY_RUN", False)

        from memory_keeper.pending import append_pending, collect_approved

        pending = _make_prop(content="仅待审", status="pending")
        append_pending([pending])

        result = collect_approved()
        assert result == []


# ── 8. DRY_RUN mode ──────────────────────────────────────────────────────────

class TestDryRun:
    def test_append_pending_dry_run_no_file_written(self, tmp_path, monkeypatch, capsys):
        store_path = tmp_path / "pending.jsonl"
        monkeypatch.setattr(_cfg_mod, "PENDING_STORE", store_path)
        monkeypatch.setattr(_cfg_mod, "DRY_RUN", True)

        from memory_keeper.pending import append_pending

        append_pending([_make_prop(content="dry run test")])

        assert not store_path.exists(), "DRY_RUN must not create pending.jsonl"

        captured = capsys.readouterr()
        assert "dry-run" in captured.out

    def test_update_status_dry_run_no_file_changed(self, tmp_path, monkeypatch):
        store_path = tmp_path / "pending.jsonl"
        monkeypatch.setattr(_cfg_mod, "PENDING_STORE", store_path)

        # Write file while not in dry-run
        monkeypatch.setattr(_cfg_mod, "DRY_RUN", False)
        from memory_keeper.pending import append_pending, update_status

        prop = _make_prop(content="status test", status="pending")
        append_pending([prop])

        original_text = store_path.read_text(encoding="utf-8")

        # Now switch to dry-run and update
        monkeypatch.setattr(_cfg_mod, "DRY_RUN", True)
        found = update_status(prop.id, "approved")
        assert found is True  # Still returns True (found the id)

        # File content must be unchanged
        assert store_path.read_text(encoding="utf-8") == original_text


# ── 9. proposition_audit mock ─────────────────────────────────────────────────

class TestPropositionAudit:
    """Mock llm_json and _load_audit_context to test routing logic."""

    def _run_audit(self, tmp_path, monkeypatch, llm_items: list[dict], bootstrap: bool = False):
        """Helper: wire up mocks and run proposition_audit."""
        store_path = tmp_path / "pending.jsonl"
        monkeypatch.setattr(_cfg_mod, "PENDING_STORE", store_path)
        monkeypatch.setattr(_cfg_mod, "MEMORY_DIR", tmp_path)
        monkeypatch.setattr(_cfg_mod, "DRY_RUN", False)

        from memory_keeper import proposition as prop_mod

        # Mock _load_audit_context to return controlled bootstrap flag
        monkeypatch.setattr(
            prop_mod,
            "_load_audit_context",
            lambda: ("", bootstrap),
        )
        # Mock llm_json to return our prepared items
        monkeypatch.setattr(
            prop_mod,
            "llm_json",
            lambda prompt, **kw: llm_items,
        )

        from memory_keeper.proposition import proposition_audit

        return proposition_audit(["test item"], session_id="test_session.jsonl")

    def _item(self, total: int, content: str = "test content") -> dict:
        """Build a mock LLM judge result dict with given total score."""
        # Distribute total evenly across dimensions (capped at 2 per dim)
        dims = ["durability", "novelty", "actionability", "confidence", "scope"]
        scores = {}
        remaining = total
        for dim in dims:
            v = min(2, remaining)
            scores[dim] = max(0, v)
            remaining -= v
        return {
            "content": content,
            "type": "fact",
            "support_status": "supported",
            "scores": scores,
            "source_excerpt": "test excerpt",
        }

    def test_high_score_non_bootstrap_becomes_auto_approved(self, tmp_path, monkeypatch):
        """total >= 5, non-bootstrap → action=write, status=auto_approved."""
        results = self._run_audit(tmp_path, monkeypatch, [self._item(7)], bootstrap=False)
        assert len(results) == 1
        assert results[0].action == "write"
        assert results[0].status == "auto_approved"

    def test_high_score_bootstrap_demoted_to_pending(self, tmp_path, monkeypatch):
        """total >= 5 but bootstrap=True → action forced to pending, status=pending."""
        results = self._run_audit(tmp_path, monkeypatch, [self._item(7)], bootstrap=True)
        assert len(results) == 1
        assert results[0].action == "pending"
        assert results[0].status == "pending"

    def test_mid_score_becomes_pending(self, tmp_path, monkeypatch):
        """total = 3 or 4 → action=pending."""
        results = self._run_audit(tmp_path, monkeypatch, [self._item(3)], bootstrap=False)
        assert len(results) == 1
        assert results[0].action == "pending"

    def test_low_score_discarded(self, tmp_path, monkeypatch):
        """total <= 2 → discard, not in results, not written."""
        results = self._run_audit(tmp_path, monkeypatch, [self._item(2)], bootstrap=False)
        assert results == []

    def test_empty_distill_items_returns_empty(self, tmp_path, monkeypatch):
        store_path = tmp_path / "pending.jsonl"
        monkeypatch.setattr(_cfg_mod, "PENDING_STORE", store_path)
        monkeypatch.setattr(_cfg_mod, "DRY_RUN", False)

        from memory_keeper.proposition import proposition_audit

        results = proposition_audit([], session_id="empty_session.jsonl")
        assert results == []

    def test_multiple_items_mixed_routing(self, tmp_path, monkeypatch):
        """Write-score + pending-score + discard in one batch."""
        items = [
            self._item(8, "高分命题"),
            self._item(4, "中分命题"),
            self._item(1, "低分命题"),
        ]
        results = self._run_audit(tmp_path, monkeypatch, items, bootstrap=False)
        assert len(results) == 2  # low-score discarded
        actions = {r.content: r.action for r in results}
        assert actions["高分命题"] == "write"
        assert actions["中分命题"] == "pending"

    def test_results_written_to_pending_store(self, tmp_path, monkeypatch):
        """Kept propositions actually appear in pending.jsonl."""
        results = self._run_audit(tmp_path, monkeypatch, [self._item(6)], bootstrap=False)
        assert len(results) == 1

        from memory_keeper.pending import load_pending

        loaded = load_pending()
        assert len(loaded) == 1
        assert loaded[0].content == results[0].content

    def test_empty_content_proposition_discarded(self, tmp_path, monkeypatch):
        """Items with empty content are not kept even if score is high."""
        item = self._item(8, "")
        item["content"] = "   "  # whitespace only
        results = self._run_audit(tmp_path, monkeypatch, [item], bootstrap=False)
        assert results == []

    def test_llm_returns_nothing(self, tmp_path, monkeypatch):
        """llm_json returning None/empty → empty results."""
        store_path = tmp_path / "pending.jsonl"
        monkeypatch.setattr(_cfg_mod, "PENDING_STORE", store_path)
        monkeypatch.setattr(_cfg_mod, "DRY_RUN", False)

        from memory_keeper import proposition as prop_mod

        monkeypatch.setattr(prop_mod, "_load_audit_context", lambda: ("", False))
        monkeypatch.setattr(prop_mod, "llm_json", lambda prompt, **kw: None)

        from memory_keeper.proposition import proposition_audit

        results = proposition_audit(["something"], session_id="s.jsonl")
        assert results == []


# ── 10. _load_audit_context returns (str, bool) ───────────────────────────────

class TestLoadAuditContext:
    def test_returns_str_and_bool(self, tmp_path, monkeypatch):
        monkeypatch.setattr(_cfg_mod, "PENDING_STORE", tmp_path / "pending.jsonl")
        monkeypatch.setattr(_cfg_mod, "MEMORY_DIR", tmp_path)

        from memory_keeper.proposition import _load_audit_context

        result = _load_audit_context()

        assert isinstance(result, tuple), "Must return a tuple"
        assert len(result) == 2
        baseline, is_bootstrap = result
        assert isinstance(baseline, str), "First element must be str"
        assert isinstance(is_bootstrap, bool), "Second element must be bool"

    def test_bootstrap_true_when_no_approved(self, tmp_path, monkeypatch):
        """With 0 approved/consumed propositions, bootstrap=True."""
        monkeypatch.setattr(_cfg_mod, "PENDING_STORE", tmp_path / "pending.jsonl")
        monkeypatch.setattr(_cfg_mod, "MEMORY_DIR", tmp_path)

        from memory_keeper.proposition import _load_audit_context

        _, is_bootstrap = _load_audit_context()
        assert is_bootstrap is True

    def test_bootstrap_false_when_enough_calibrated(self, tmp_path, monkeypatch):
        """With >= 10 approved/consumed propositions, bootstrap=False."""
        monkeypatch.setattr(_cfg_mod, "PENDING_STORE", tmp_path / "pending.jsonl")
        monkeypatch.setattr(_cfg_mod, "MEMORY_DIR", tmp_path)
        monkeypatch.setattr(_cfg_mod, "DRY_RUN", False)

        from memory_keeper.pending import append_pending
        from memory_keeper.proposition import _load_audit_context

        calibrated_props = [
            _make_prop(content=f"calibrated {i}", status="consumed")
            for i in range(10)
        ]
        append_pending(calibrated_props)

        _, is_bootstrap = _load_audit_context()
        assert is_bootstrap is False

    def test_baseline_includes_memory_md_content(self, tmp_path, monkeypatch):
        """If a .md file exists in MEMORY_DIR, its content appears in baseline."""
        monkeypatch.setattr(_cfg_mod, "PENDING_STORE", tmp_path / "pending.jsonl")
        monkeypatch.setattr(_cfg_mod, "MEMORY_DIR", tmp_path)

        marker = "独特标记内容_XYZ_12345"
        (tmp_path / "test_memory.md").write_text(marker, encoding="utf-8")

        from memory_keeper.proposition import _load_audit_context

        baseline, _ = _load_audit_context()
        assert marker in baseline

    def test_baseline_truncated_to_3000(self, tmp_path, monkeypatch):
        """Baseline is capped at 3000 characters."""
        monkeypatch.setattr(_cfg_mod, "PENDING_STORE", tmp_path / "pending.jsonl")
        monkeypatch.setattr(_cfg_mod, "MEMORY_DIR", tmp_path)

        # Write a very large memory file
        (tmp_path / "large_memory.md").write_text("X" * 10000, encoding="utf-8")

        from memory_keeper.proposition import _load_audit_context

        baseline, _ = _load_audit_context()
        assert len(baseline) <= 3000


# ── 11. _build_few_shot contains discard sample ───────────────────────────────

class TestBuildFewShot:
    def test_few_shot_contains_discard_example(self):
        """FEW_SHOT_EXAMPLES must contain at least one discard negative example."""
        from memory_keeper.rubrics.judge_prompt_v1 import FEW_SHOT_EXAMPLES

        # The discard section always contains "应丢弃" or "discard" in the header
        assert FEW_SHOT_EXAMPLES, "FEW_SHOT_EXAMPLES must not be empty"
        assert "应丢弃" in FEW_SHOT_EXAMPLES or "discard" in FEW_SHOT_EXAMPLES.lower()

    def test_few_shot_contains_positive_examples(self):
        """FEW_SHOT_EXAMPLES must contain at least one positive (non-discard) example."""
        from memory_keeper.rubrics.judge_prompt_v1 import FEW_SHOT_EXAMPLES

        assert "示例" in FEW_SHOT_EXAMPLES

    def test_build_few_shot_function_returns_string(self):
        """_build_few_shot() returns a non-empty string when seed samples exist."""
        from memory_keeper.rubrics.judge_prompt_v1 import _build_few_shot

        result = _build_few_shot()
        assert isinstance(result, str)

    def test_build_few_shot_discard_outputs_empty_array(self):
        """The discard example in few-shot shows '[]' (empty JSON array output)."""
        from memory_keeper.rubrics.judge_prompt_v1 import _build_few_shot

        result = _build_few_shot()
        if result:  # only assert when seed samples exist
            assert "[]" in result
