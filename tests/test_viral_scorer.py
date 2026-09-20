import pytest
from src.analytics.scorer import calculate_viral_score

def test_calculate_viral_score_high_engagement():
    score = calculate_viral_score(
        likes=5800,
        reposts=1200,
        quotes=300,
        bookmarks=3400,
        views=240000,
        age_in_hours=2.5
    )
    # Score should be well above the 75.0 threshold for viral posts
    assert score >= 75.0

def test_calculate_viral_score_low_engagement_old():
    score = calculate_viral_score(
        likes=10,
        reposts=0,
        quotes=0,
        bookmarks=0,
        views=500,
        age_in_hours=48.0
    )
    # Score should be below the 40.0 threshold for regular posts
    assert score < 40.0

def test_calculate_viral_score_edge_cases():
    # Test protection against log(0) and division by zero
    score = calculate_viral_score(
        likes=100,
        reposts=10,
        quotes=0,
        bookmarks=5,
        views=0,        # Should be clamped to 100
        age_in_hours=0.0  # Should be clamped to 1.0
    )
    assert isinstance(score, float)
    assert score > 0.0