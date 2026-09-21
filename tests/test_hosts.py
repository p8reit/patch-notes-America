import json
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.main import (
    build_speech_chunks,
    parse_hosts,
    parse_speaker_script,
    remove_accidental_transcript_repetition,
)


def hosts():
    return [
        {"name": "Major Patchnotes", "voice": "am_michael", "tempo": 1.0},
        {"name": "Alex", "voice": "af_heart", "tempo": 1.05},
        {"name": "Sam", "voice": "bm_george", "tempo": 0.95},
    ]


def test_parse_hosts_accepts_any_number_of_hosts():
    configured = parse_hosts(json.dumps(hosts()))
    assert len(configured) == 3
    assert configured[2]["name"] == "Sam"


def test_untagged_script_uses_first_host():
    sections = parse_speaker_script("Hello world.", hosts())
    assert sections == [{"host": "Major Patchnotes", "text": "Hello world."}]


def test_speaker_tags_switch_hosts_in_order():
    script = """Welcome to the show.\n\n[Alex]\nGlad to be here.\n\n[Sam]\nLet's go."""
    sections = parse_speaker_script(script, hosts())
    assert [section["host"] for section in sections] == ["Major Patchnotes", "Alex", "Sam"]


def test_build_chunks_preserves_voice_and_tempo():
    script = """Opening line.\n\n[Alex]\nSecond line."""
    chunks = build_speech_chunks(script, hosts(), max_chars=500)
    assert chunks[0]["voice"] == "am_michael"
    assert chunks[1]["voice"] == "af_heart"
    assert chunks[1]["tempo"] == 1.05


def test_long_same_speaker_turn_prefers_sentences_then_marks_hard_splits():
    script = "A natural sentence ends here. " + " ".join(["unbroken"] * 30)
    chunks = build_speech_chunks(script, hosts(), max_chars=40)

    assert len({chunk["parent_turn_id"] for chunk in chunks}) == 1
    assert chunks[1]["boundary_reason"] == "sentence_break"
    assert all(
        chunk["boundary_reason"] == "technical_continuation"
        for chunk in chunks[2:]
    )
    assert all(len(chunk["text"]) <= 40 for chunk in chunks)


def test_paragraph_boundary_is_recorded_when_it_requires_a_new_chunk():
    chunks = build_speech_chunks("First paragraph.\n\nSecond paragraph.", hosts(), max_chars=20)

    assert [chunk["boundary_reason"] for chunk in chunks] == [None, "paragraph_break"]
    assert chunks[0]["parent_turn_id"] == chunks[1]["parent_turn_id"]


def test_speaker_change_starts_a_new_parent_turn_and_boundary():
    chunks = build_speech_chunks("Hello.\n[Alex]\nHi.", hosts(), max_chars=100)

    assert chunks[1]["boundary_reason"] == "speaker_change"
    assert chunks[0]["parent_turn_id"] != chunks[1]["parent_turn_id"]


def test_episode_chunks_put_host_intro_lines_before_main_script():
    from app.main import build_episode_speech_chunks

    chunks = build_episode_speech_chunks(
        "Main story starts now.",
        hosts(),
        "Welcome to the show.\n\n[Alex]\nHere is what is ahead.",
        max_chars=500,
    )

    assert [chunk["section"] for chunk in chunks] == ["host_intro", "host_intro", "episode"]
    assert [chunk["host"] for chunk in chunks] == ["Major Patchnotes", "Alex", "Major Patchnotes"]


def test_exact_long_script_repetition_is_rendered_only_once():
    passage = "\n\n".join(f"Paragraph {number}: " + ("history " * 20).strip() for number in range(6))

    cleaned = remove_accidental_transcript_repetition(f"{passage}\n\n{passage}")

    assert cleaned == passage.strip()


def test_short_intentional_repetition_is_preserved():
    assert remove_accidental_transcript_repetition("Patch it.\n\nPatch it.") == "Patch it.\n\nPatch it."


def test_full_script_accidentally_used_as_intro_is_not_rendered_twice():
    from app.main import build_episode_speech_chunks

    passage = "\n\n".join(f"Paragraph {number}: " + ("history " * 20).strip() for number in range(6))
    chunks = build_episode_speech_chunks(passage, hosts(), passage, max_chars=10_000)

    assert len(chunks) == 1
    assert chunks[0]["section"] == "episode"
    assert chunks[0]["text"] == passage.strip()


def test_long_intro_suffix_overlapping_episode_start_is_removed():
    from app.main import build_episode_speech_chunks

    overlap = "\n\n".join(f"Shared paragraph {number}: " + ("context " * 20).strip() for number in range(4))
    script = f"{overlap}\n\nThe episode continues with new material."
    intro = f"Welcome to the show.\n\n{overlap}"

    chunks = build_episode_speech_chunks(script, hosts(), intro, max_chars=10_000)

    assert [chunk["section"] for chunk in chunks] == ["host_intro", "episode"]
    assert chunks[0]["text"] == "Welcome to the show."
    assert chunks[1]["text"] == script


def test_unknown_host_tag_is_rejected():
    with pytest.raises(HTTPException) as exc:
        parse_speaker_script("[Nobody]\nHello", hosts())
    assert exc.value.status_code == 400
    assert "Unknown host tag" in exc.value.detail


def test_speaker_names_are_case_sensitive():
    with pytest.raises(HTTPException) as exc:
        parse_speaker_script("[alex]\nHello", hosts())
    assert "Unknown host tag [alex]" in exc.value.detail


def test_redundant_speaker_tag_is_ignored_without_creating_a_transition():
    sections = parse_speaker_script("Hello.\n\n[Major Patchnotes]\nStill talking.", hosts())
    assert sections == [{"host": "Major Patchnotes", "text": "Hello.\n\nStill talking."}]


def test_dialogue_cannot_share_speaker_tag_line():
    with pytest.raises(HTTPException) as exc:
        parse_speaker_script("Hello.\n[Alex] Same line.", hosts())
    assert "own line" in exc.value.detail


def test_character_profile_fields_are_preserved():
    configured = parse_hosts(json.dumps([
        {
            "name": "Wade Mercer",
            "role": "Southern Everyman",
            "voice": "am_michael",
            "tempo": 0.96,
            "traits": ["practical", "dry humor"],
            "debate_style": "Brings policy back to everyday life.",
            "humor_style": "Deadpan analogies.",
            "interruption_frequency": "low",
            "sentence_style": "Conversational.",
            "political_posture": "Independent-minded.",
            "flaws": "Can oversimplify.",
            "character_notes": "Southern without being a caricature."
        }
    ]))
    host = configured[0]
    assert host["role"] == "Southern Everyman"
    assert host["traits"] == ["practical", "dry humor"]
    assert host["interruption_frequency"] == "low"
    assert host["flaws"] == "Can oversimplify."


def test_traits_accept_comma_separated_text():
    configured = parse_hosts(json.dumps([
        {"name": "Marcus Reed", "voice": "am_eric", "tempo": 1.05, "traits": "quick, analytical, pragmatic"}
    ]))
    assert configured[0]["traits"] == ["quick", "analytical", "pragmatic"]


def test_invalid_interruption_frequency_is_rejected():
    with pytest.raises(HTTPException) as exc:
        parse_hosts(json.dumps([
            {"name": "Julian Cross", "voice": "bm_george", "tempo": 0.98, "interruption_frequency": "constant"}
        ]))
    assert exc.value.status_code == 400
    assert "Interruption frequency" in exc.value.detail

from app.main import build_conversation_prompt, extract_response_text


def test_conversation_prompt_contains_cast_and_source_notes():
    prompt = build_conversation_prompt("Verified story fact here.", parse_hosts(json.dumps(hosts())), 8)
    assert "Verified story fact here." in prompt
    assert "Major Patchnotes" in prompt
    assert "Alex" in prompt
    assert "Do not invent factual details" in prompt
    assert "Begin directly with Major Patchnotes's dialogue" in prompt
    assert "Insert a speaker tag only when the active speaker changes" in prompt
    assert "Never emit role labels or invented speaker names" in prompt
    assert "Patch Notes: America" not in prompt


def test_conversation_prompt_rejects_bad_length():
    with pytest.raises(HTTPException):
        build_conversation_prompt("Story", parse_hosts(json.dumps(hosts())), 1)


def test_extract_response_text_handles_responses_shape():
    payload = {"output": [{"content": [{"type": "output_text", "text": "[Alex]\\nHello."}]}]}
    assert extract_response_text(payload) == "[Alex]\\nHello."


def test_research_packet_parsing_and_notes():
    from app.main import parse_research_packet, research_packet_to_notes
    import json
    packet = parse_research_packet(json.dumps({
        "title": "Weekly show",
        "episode_angle": "What actually matters",
        "stories": [{
            "headline": "Story one",
            "importance": "high",
            "verified_facts": "Fact A is verified.",
            "disputed_or_uncertain": "Claim B remains disputed.",
            "angles": "Why listeners should care.",
            "sources": ["Reuters", "AP"]
        }]
    }))
    assert packet["stories"][0]["headline"] == "Story one"
    notes = research_packet_to_notes(packet)
    assert "Verified facts:" in notes
    assert "Claim B remains disputed." in notes
    assert "Reuters" in notes


def test_research_packet_requires_story_facts():
    from app.main import parse_research_packet
    from fastapi import HTTPException
    import json, pytest
    with pytest.raises(HTTPException):
        parse_research_packet(json.dumps({"title": "Bad", "stories": [{"headline": "No facts"}]}))


def test_clip_suggestions_prefer_multi_speaker_windows():
    from app.main import suggest_clip_windows
    metadata = {"chunks": [
        {"host": "Wade", "text": "Here is the problem and why it matters.", "start": 0.0, "end": 12.0},
        {"host": "Marcus", "text": "But wait, that's the point. What happens next?", "start": 12.0, "end": 25.0},
        {"host": "Julian", "text": "Actually, there is a bigger context because the rest of the world reacts too.", "start": 25.0, "end": 39.0},
        {"host": "Wade", "text": "And normal people still have to pay for it.", "start": 39.0, "end": 50.0},
    ]}
    suggestions = suggest_clip_windows(metadata, limit=3)
    assert suggestions
    assert 20 <= suggestions[0]["duration"] <= 60
    assert len(suggestions[0]["speakers"]) >= 2


def test_build_clip_ass_contains_speaker_and_text(tmp_path):
    from app.main import build_clip_ass
    metadata = {"chunks": [
        {"host": "Wade Mercer", "text": "Now hold on a minute. This is the useful part.", "start": 5.0, "end": 12.0},
    ]}
    dest = tmp_path / "clip.ass"
    build_clip_ass(metadata, 4.0, 14.0, dest, 1080, 1920)
    text = dest.read_text()
    assert "Wade Mercer" in text
    assert "This is the useful part." in text
    assert "PlayResX: 1080" in text


def test_chatterbox_voice_controls_are_preserved_in_chunks():
    configured = parse_hosts(json.dumps([{
        "name": "Wade", "voice": "wade_mercer", "tempo": 1.0,
        "exaggeration": 0.6, "cfg_weight": 0.32, "temperature": 0.75,
        "min_p": 0.05, "top_p": 1.0, "repetition_penalty": 1.2,
    }]))
    chunks = build_speech_chunks("Testing voice controls.", configured)
    assert chunks[0]["exaggeration"] == 0.6
    assert chunks[0]["cfg_weight"] == 0.32
    assert chunks[0]["temperature"] == 0.75
    assert chunks[0]["min_p"] == 0.05
    assert chunks[0]["top_p"] == 1.0
    assert chunks[0]["repetition_penalty"] == 1.2


def test_chatterbox_voice_controls_reject_out_of_range_values():
    with pytest.raises(HTTPException, match="top_p for Wade must be between 0 and 1"):
        parse_hosts(json.dumps([{"name": "Wade", "voice": "default", "top_p": 1.5}]))


def test_bundled_host_profiles_only_select_available_reference_audio():
    repository = Path(__file__).resolve().parent.parent
    profiles = json.loads((repository / "config" / "host_profiles.json").read_text())
    for profile in profiles:
        voice = profile["voice"]
        assert voice == "default" or (repository / "voices" / f"{voice}.wav").is_file()
