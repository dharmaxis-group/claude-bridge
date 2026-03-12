#!/usr/bin/env python3
"""
ElevenLabs API Test Script
--------------------------
Tests: list voices, Chinese TTS generation, latency measurement.

Prerequisites:
  pip3 install elevenlabs
  Store API key:
    security add-generic-password -s elevenlabs-api-key -a elevenlabs -w "YOUR_KEY_HERE"

Usage:
  python3 ~/.claude-bridge/scripts/test-elevenlabs.py
"""

import os
import sys
import time
import subprocess
from pathlib import Path


def get_api_key() -> str:
    """Try env var first, then macOS Keychain."""
    key = os.environ.get("ELEVEN_API_KEY") or os.environ.get("ELEVENLABS_API_KEY")
    if key:
        return key

    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", "elevenlabs-api-key", "-w"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass

    print("ERROR: No API key found.")
    print("  Set env:  export ELEVEN_API_KEY='sk_...'")
    print("  Or keychain:  security add-generic-password -s elevenlabs-api-key -a elevenlabs -w 'sk_...'")
    sys.exit(1)


def test_list_voices(client):
    """List available voices, highlight any Chinese-tagged ones."""
    print("\n" + "=" * 60)
    print("TEST 1: List Available Voices")
    print("=" * 60)

    t0 = time.time()
    response = client.voices.search()
    elapsed = time.time() - t0

    voices = response.voices
    print(f"  Found {len(voices)} voices (fetched in {elapsed:.2f}s)")
    print()

    # Show first 10 voices
    print("  Sample voices (first 10):")
    for v in voices[:10]:
        labels = ""
        if v.labels:
            labels = ", ".join(f"{k}={val}" for k, val in v.labels.items())
        print(f"    - {v.name} (id={v.voice_id[:12]}...) [{labels}]")

    # Find Chinese-tagged voices
    chinese_voices = []
    for v in voices:
        name_lower = v.name.lower()
        labels_str = str(v.labels).lower() if v.labels else ""
        if any(kw in name_lower or kw in labels_str
               for kw in ["chinese", "mandarin", "中文", "普通话"]):
            chinese_voices.append(v)

    print()
    if chinese_voices:
        print(f"  Chinese-tagged voices found: {len(chinese_voices)}")
        for v in chinese_voices:
            print(f"    - {v.name} (id={v.voice_id})")
    else:
        print("  No explicitly Chinese-tagged voices found in your library.")
        print("  (Multilingual models can still generate Chinese with any voice)")

    return voices


def test_list_models(client):
    """List available models and their language support."""
    print("\n" + "=" * 60)
    print("TEST 2: List Models & Chinese Support")
    print("=" * 60)

    t0 = time.time()
    models = client.models.list()
    elapsed = time.time() - t0

    print(f"  Found {len(models)} models (fetched in {elapsed:.2f}s)")
    print()

    chinese_models = []
    for m in models:
        languages = []
        supports_chinese = False
        if m.languages:
            for lang in m.languages:
                lang_name = lang.name if hasattr(lang, 'name') else str(lang)
                languages.append(lang_name)
                if any(kw in lang_name.lower() for kw in ["chinese", "mandarin"]):
                    supports_chinese = True

        status = "CHINESE" if supports_chinese else "no Chinese"
        print(f"  {m.model_id}")
        print(f"    Name: {m.name}")
        print(f"    Languages: {len(languages)} [{status}]")
        if supports_chinese:
            chinese_models.append(m)
        print()

    return chinese_models


def test_chinese_tts(client, voice_id: str, model_id: str = "eleven_v3"):
    """Generate a short Chinese TTS sample and measure latency."""
    print("\n" + "=" * 60)
    print("TEST 3: Chinese TTS Generation")
    print("=" * 60)

    text = "你好，我是你的语音助手。今天天气很好，适合出去散步。"
    print(f"  Text: {text}")
    print(f"  Model: {model_id}")
    print(f"  Voice: {voice_id}")
    print()

    output_dir = Path.home() / ".claude-bridge" / "scripts"
    output_file = output_dir / "elevenlabs-test-chinese.mp3"

    # Measure time to first byte (streaming)
    print("  --- Streaming TTS ---")
    t0 = time.time()
    audio_stream = client.text_to_speech.stream(
        text=text,
        voice_id=voice_id,
        model_id=model_id,
        output_format="mp3_44100_128",
    )

    first_chunk = True
    total_bytes = 0
    ttfb = None
    chunks = []

    for chunk in audio_stream:
        if isinstance(chunk, bytes):
            if first_chunk:
                ttfb = time.time() - t0
                first_chunk = False
            total_bytes += len(chunk)
            chunks.append(chunk)

    total_time = time.time() - t0

    print(f"  Time to first byte: {ttfb:.3f}s" if ttfb else "  TTFB: N/A")
    print(f"  Total generation time: {total_time:.3f}s")
    print(f"  Audio size: {total_bytes:,} bytes ({total_bytes / 1024:.1f} KB)")

    # Save audio file
    with open(output_file, "wb") as f:
        for chunk in chunks:
            f.write(chunk)
    print(f"  Saved to: {output_file}")

    # Non-streaming comparison
    print()
    print("  --- Non-streaming TTS ---")
    t0 = time.time()
    audio = client.text_to_speech.convert(
        text=text,
        voice_id=voice_id,
        model_id=model_id,
        output_format="mp3_44100_128",
    )
    # Consume the generator
    audio_bytes = b"".join(audio)
    convert_time = time.time() - t0

    output_file_sync = output_dir / "elevenlabs-test-chinese-sync.mp3"
    with open(output_file_sync, "wb") as f:
        f.write(audio_bytes)

    print(f"  Total time: {convert_time:.3f}s")
    print(f"  Audio size: {len(audio_bytes):,} bytes ({len(audio_bytes) / 1024:.1f} KB)")
    print(f"  Saved to: {output_file_sync}")

    return ttfb, total_time


def test_flash_model_latency(client, voice_id: str):
    """Compare latency between v3 and Flash v2.5 models."""
    print("\n" + "=" * 60)
    print("TEST 4: Model Latency Comparison (v3 vs Flash v2.5)")
    print("=" * 60)

    text = "你好世界"  # Short text for pure latency test

    results = {}
    for model_id in ["eleven_v3", "eleven_flash_v2_5"]:
        print(f"\n  Model: {model_id}")
        t0 = time.time()
        audio_stream = client.text_to_speech.stream(
            text=text,
            voice_id=voice_id,
            model_id=model_id,
            output_format="mp3_22050_32",  # Smallest format for speed
        )

        ttfb = None
        total_bytes = 0
        for chunk in audio_stream:
            if isinstance(chunk, bytes):
                if ttfb is None:
                    ttfb = time.time() - t0
                total_bytes += len(chunk)

        total = time.time() - t0
        results[model_id] = {"ttfb": ttfb, "total": total, "bytes": total_bytes}
        print(f"    TTFB: {ttfb:.3f}s" if ttfb else "    TTFB: N/A")
        print(f"    Total: {total:.3f}s")
        print(f"    Size: {total_bytes:,} bytes")

    return results


def main():
    from elevenlabs.client import ElevenLabs

    api_key = get_api_key()
    print(f"API Key: {api_key[:8]}...{api_key[-4:]}")

    client = ElevenLabs(api_key=api_key)

    # Test 1: List voices
    voices = test_list_voices(client)

    # Test 2: List models
    chinese_models = test_list_models(client)

    if not voices:
        print("ERROR: No voices available.")
        sys.exit(1)

    # Use first available voice (all multilingual models can speak Chinese)
    voice_id = voices[0].voice_id
    print(f"\nUsing voice: {voices[0].name} ({voice_id})")

    # Determine best model for Chinese
    model_id = "eleven_v3"  # Default: best Chinese support
    if chinese_models:
        # Prefer v3, then multilingual_v2
        for m in chinese_models:
            if m.model_id == "eleven_v3":
                model_id = "eleven_v3"
                break
            elif m.model_id == "eleven_multilingual_v2":
                model_id = "eleven_multilingual_v2"

    # Test 3: Chinese TTS
    ttfb, total = test_chinese_tts(client, voice_id, model_id)

    # Test 4: Latency comparison
    latency_results = test_flash_model_latency(client, voice_id)

    # Summary
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"  Voices available: {len(voices)}")
    print(f"  Models with Chinese: {len(chinese_models)}")
    print(f"  Chinese TTS TTFB: {ttfb:.3f}s" if ttfb else "  Chinese TTS TTFB: N/A")
    print(f"  Chinese TTS total: {total:.3f}s")
    for model, r in latency_results.items():
        ttfb_str = f"{r['ttfb']:.3f}s" if r['ttfb'] else "N/A"
        print(f"  {model}: TTFB={ttfb_str}, total={r['total']:.3f}s")
    print()
    print("Audio files saved in ~/.claude-bridge/scripts/")


if __name__ == "__main__":
    main()
