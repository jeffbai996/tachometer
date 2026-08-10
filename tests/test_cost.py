"""Cost accounting — the part where a bug quietly misreports money."""
from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tachometer import claude_usage, config  # noqa: E402


@pytest.fixture(autouse=True)
def _rates(monkeypatch):
    """Pin the rate table so these tests describe the maths, not the prices."""
    monkeypatch.setattr(config, "_cache", {
        "config_dir": "~/.claude",
        "rates": {"opus": [5.0, 25.0], "sonnet": [3.0, 15.0],
                  "haiku": [1.0, 5.0]},
        "cache_read_multiplier": 0.10,
        "cache_write_multiplier": 1.25,
    })


def test_model_ids_match_by_substring_so_version_suffixes_still_price():
    """Model ids carry version and date suffixes the rate does not depend on.
    A table keyed on exact ids would silently price every new release at $0."""
    assert claude_usage.rate_for("claude-opus-5") == (5.0, 25.0)
    assert claude_usage.rate_for("claude-opus-4-8") == (5.0, 25.0)
    assert claude_usage.rate_for("claude-sonnet-5") == (3.0, 15.0)
    assert claude_usage.rate_for("claude-haiku-4-5-20251001") == (1.0, 5.0)


def test_the_most_specific_rate_wins_not_the_first_one_written(monkeypatch):
    """A user adding a rate for one specific model must not be overridden by a
    broader key that happens to sort earlier. Specificity decides, not the
    order someone typed the config file in."""
    monkeypatch.setattr(config, "_cache", {
        "config_dir": "~/.claude",
        "rates": {"opus": [5.0, 25.0], "opus-4-8": [99.0, 99.0]},
        "cache_read_multiplier": 0.10, "cache_write_multiplier": 1.25,
    })
    assert claude_usage.rate_for("claude-opus-4-8") == (99.0, 99.0)
    assert claude_usage.rate_for("claude-opus-5") == (5.0, 25.0)


def test_an_unknown_model_is_none_rather_than_a_guessed_default():
    """Returning a default would bury an unpriced model inside a
    plausible-looking total. None is what lets --doctor name it out loud."""
    assert claude_usage.rate_for("some-model-we-have-never-seen") is None
    assert claude_usage.rate_for("") is None


def test_cache_lanes_are_priced_off_the_input_rate():
    """Cache reads bill at a fraction of input and writes at a premium — they
    are not free, and they are not full price. On long conversations the cache
    lanes dominate, so getting these multipliers wrong misstates the total by
    more than the visible token counts do."""
    # 1M cache reads at 10% of a $5 input rate = $0.50
    assert claude_usage._cost("claude-opus-5", 0, 0, 1_000_000, 0) == \
        pytest.approx(0.50)
    # 1M cache writes at 125% of $5 = $6.25
    assert claude_usage._cost("claude-opus-5", 0, 0, 0, 1_000_000) == \
        pytest.approx(6.25)
    # input and output at face value
    assert claude_usage._cost("claude-opus-5", 1_000_000, 0, 0, 0) == \
        pytest.approx(5.00)
    assert claude_usage._cost("claude-opus-5", 0, 1_000_000, 0, 0) == \
        pytest.approx(25.00)


def test_an_unpriced_model_costs_zero_rather_than_raising():
    """A model with no rate must not take the whole scan down — the panel
    should still show the tokens it counted."""
    assert claude_usage._cost("unknown-model", 1_000_000, 1_000_000, 0, 0) == 0.0


def test_compute_reads_transcripts_and_skips_synthetic_turns(tmp_path):
    """Synthetic entries are Claude Code's own bookkeeping, not model work.
    Counting them would inflate every total with turns nobody ran."""
    proj = tmp_path / "projects" / "-home-alice-api"
    proj.mkdir(parents=True)
    lines = [
        {"message": {"model": "claude-opus-5", "usage": {
            "input_tokens": 1000, "output_tokens": 2000,
            "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}}},
        # synthetic: must not be counted
        {"message": {"model": "<synthetic>", "usage": {
            "input_tokens": 999999, "output_tokens": 999999,
            "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}}},
        # all-zero: no work happened, so it is not a turn
        {"message": {"model": "claude-opus-5", "usage": {
            "input_tokens": 0, "output_tokens": 0,
            "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}}},
    ]
    (proj / "a.jsonl").write_text(
        "\n".join(json.dumps(x) for x in lines), encoding="utf-8")

    res = claude_usage.compute(str(tmp_path / "projects"))
    assert res["turns"] == 1, "synthetic or empty turns were counted"
    assert res["total"]["input"] == 1000
    assert res["total"]["output"] == 2000
    # 1000 in @ $5/M + 2000 out @ $25/M
    assert res["total"]["cost"] == pytest.approx(0.005 + 0.05)


def test_a_malformed_line_does_not_abort_the_scan(tmp_path):
    """Transcripts are appended to live; a half-written last line is normal.
    One bad line must cost that line, not the whole file."""
    proj = tmp_path / "projects" / "-home-alice-api"
    proj.mkdir(parents=True)
    good = json.dumps({"message": {"model": "claude-opus-5", "usage": {
        "input_tokens": 10, "output_tokens": 20,
        "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}}})
    (proj / "a.jsonl").write_text(
        good + '\n{"message": {"usage": {"inp' + "\n", encoding="utf-8")
    res = claude_usage.compute(str(tmp_path / "projects"))
    assert res["turns"] == 1
