"""Unit tests for audio helpers (pure functions only)."""

import audio
from main import _is_valid_sound_file_name


def test_atempo_chain_identity():
    assert audio._atempo_chain(1.0) == "atempo=1"


def test_atempo_chain_splits_large():
    assert audio._atempo_chain(5.0) == "atempo=2,atempo=2,atempo=1.25"


def test_atempo_chain_splits_small():
    assert audio._atempo_chain(0.1) == "atempo=0.5,atempo=0.5,atempo=0.5,atempo=0.8"


def test_atempo_chain_nonpositive_defaults():
    assert audio._atempo_chain(0) == "atempo=1"
    assert audio._atempo_chain(-3) == "atempo=1"


def test_is_valid_sound_file_name_accepts_existing():
    assert _is_valid_sound_file_name("acknowledge.wav") is True
    assert _is_valid_sound_file_name("good-morning-vietnam.wav") is True


def test_is_valid_sound_file_name_rejects_traversal():
    assert _is_valid_sound_file_name("../evil.wav") is False
    assert _is_valid_sound_file_name("a/b.wav") is False
    assert _is_valid_sound_file_name("noext") is False
    assert _is_valid_sound_file_name("missing.wav") is False


def test_every_sound_event_has_a_default_file_that_exists():
    # An empty default silently mutes the event, so each one must name a file that
    # is actually shipped in resources/audios/ (bare name, resolved by the service).
    for event in audio.SOUND_EVENTS:
        default = audio.DEFAULT_SOUND_FILES.get(event)
        assert default, event
        assert (audio.AUDIOS_DIR / default).is_file(), (event, default)


def test_warn_and_process_defaults_are_the_documented_files():
    assert audio.DEFAULT_SOUND_FILES["warn"] == "warn.wav"
    assert audio.DEFAULT_SOUND_FILES["process"] == "process.wav"
