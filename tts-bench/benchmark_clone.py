"""
Qwen3-TTS MLX - Voice Cloning Benchmark
Tests: voice cloning capability, streaming mode latency
"""
import time
import os
import sys
import gc
import shutil
import subprocess

os.environ["TOKENIZERS_PARALLELISM"] = "false"
import warnings
warnings.filterwarnings("ignore")

from mlx_audio.tts.utils import load_model
from mlx_audio.tts.generate import generate_audio
import soundfile as sf
import numpy as np

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "outputs")
os.makedirs(OUTPUT_DIR, exist_ok=True)

SAMPLE_RATE = 24000

def get_audio_duration(wav_path):
    data, sr = sf.read(wav_path)
    return len(data) / sr

def find_output_wav(output_dir):
    for f in sorted(os.listdir(output_dir)):
        if f.endswith('.wav'):
            return os.path.join(output_dir, f)
    return None

def create_test_reference_audio():
    """Create a short reference audio using the CustomVoice model for cloning test."""
    ref_path = os.path.join(OUTPUT_DIR, "ref_audio.wav")
    if os.path.exists(ref_path):
        print(f"Using existing reference audio: {ref_path}")
        return ref_path

    print("Generating reference audio with CustomVoice model...")
    model = load_model("mlx-community/Qwen3-TTS-12Hz-0.6B-CustomVoice-8bit")

    temp_dir = os.path.join(OUTPUT_DIR, "temp_ref")
    os.makedirs(temp_dir, exist_ok=True)

    generate_audio(
        model=model,
        text="大家好，我是一个测试语音克隆的参考音频。",
        voice="Vivian",
        instruct="",
        speed=1.0,
        lang_code="zh",
        output_path=temp_dir,
        verbose=False,
        play=False,
    )

    wav = find_output_wav(temp_dir)
    if wav:
        shutil.move(wav, ref_path)
        print(f"Reference audio saved: {ref_path} ({get_audio_duration(ref_path):.1f}s)")
    shutil.rmtree(temp_dir, ignore_errors=True)

    del model
    gc.collect()

    return ref_path


def test_voice_cloning():
    """Test voice cloning with the Base model."""
    print("\n" + "="*60)
    print("VOICE CLONING TEST (0.6B-Base-8bit)")
    print("="*60)

    ref_audio = create_test_reference_audio()
    ref_text = "大家好，我是一个测试语音克隆的参考音频。"

    print("\nLoading 0.6B-Base-8bit model...")
    t0 = time.time()
    model = load_model("mlx-community/Qwen3-TTS-12Hz-0.6B-Base-8bit")
    print(f"Model load time: {time.time()-t0:.2f}s")

    test_text = "今天天气不错，适合出门散步。"

    print(f"\nCloning voice with ref_audio: {ref_audio}")
    print(f"Test text: {test_text}")

    temp_dir = os.path.join(OUTPUT_DIR, "temp_clone")
    os.makedirs(temp_dir, exist_ok=True)

    t_start = time.time()
    try:
        generate_audio(
            model=model,
            text=test_text,
            ref_audio=ref_audio,
            ref_text=ref_text,
            lang_code="zh",
            output_path=temp_dir,
            verbose=True,
            play=False,
        )
        elapsed = time.time() - t_start

        wav = find_output_wav(temp_dir)
        if wav:
            duration = get_audio_duration(wav)
            final_path = os.path.join(OUTPUT_DIR, "clone_test.wav")
            shutil.move(wav, final_path)
            print(f"\nVoice Cloning Result:")
            print(f"  Time: {elapsed:.2f}s")
            print(f"  Audio duration: {duration:.2f}s")
            print(f"  RTF: {elapsed/duration:.3f}")
            print(f"  Saved: {final_path}")
        else:
            print(f"\nVoice Cloning: No output (took {elapsed:.2f}s)")
    except Exception as e:
        elapsed = time.time() - t_start
        print(f"\nVoice Cloning FAILED ({elapsed:.2f}s): {e}")
        import traceback
        traceback.print_exc()

    shutil.rmtree(temp_dir, ignore_errors=True)
    del model
    gc.collect()


def test_streaming_latency():
    """Test streaming mode to measure time-to-first-audio-chunk."""
    print("\n" + "="*60)
    print("STREAMING LATENCY TEST (0.6B-CustomVoice-8bit)")
    print("="*60)

    print("\nLoading model...")
    model = load_model("mlx-community/Qwen3-TTS-12Hz-0.6B-CustomVoice-8bit")

    test_text = "今天天气不错，适合出门散步。我建议你可以去公园走走，呼吸一下新鲜空气。"

    temp_dir = os.path.join(OUTPUT_DIR, "temp_stream")
    os.makedirs(temp_dir, exist_ok=True)

    print(f"Testing streaming mode with text: {test_text[:40]}...")
    print(f"Streaming interval: 2.0s")

    t_start = time.time()
    try:
        generate_audio(
            model=model,
            text=test_text,
            voice="Vivian",
            instruct="",
            speed=1.0,
            lang_code="zh",
            output_path=temp_dir,
            verbose=True,
            play=False,
            stream=True,
            streaming_interval=2.0,
        )
        elapsed = time.time() - t_start

        # Check how many chunks were generated
        chunks = sorted([f for f in os.listdir(temp_dir) if f.endswith('.wav')])
        print(f"\nStreaming Result:")
        print(f"  Total time: {elapsed:.2f}s")
        print(f"  Chunks generated: {len(chunks)}")

        for i, chunk in enumerate(chunks):
            chunk_path = os.path.join(temp_dir, chunk)
            dur = get_audio_duration(chunk_path)
            print(f"  Chunk {i}: {chunk} ({dur:.2f}s audio)")

    except Exception as e:
        elapsed = time.time() - t_start
        print(f"\nStreaming FAILED ({elapsed:.2f}s): {e}")
        import traceback
        traceback.print_exc()

    shutil.rmtree(temp_dir, ignore_errors=True)
    del model
    gc.collect()


def test_short_text_latency():
    """Test the actual latency for typical Telegram bot response lengths."""
    print("\n" + "="*60)
    print("TELEGRAM BOT SCENARIO TEST")
    print("="*60)

    print("\nLoading 0.6B-CustomVoice-8bit (pre-warmed)...")
    model = load_model("mlx-community/Qwen3-TTS-12Hz-0.6B-CustomVoice-8bit")

    # Warm up
    temp_dir = os.path.join(OUTPUT_DIR, "temp_warmup")
    os.makedirs(temp_dir, exist_ok=True)
    generate_audio(model=model, text="测试", voice="Vivian", speed=1.0, lang_code="zh",
                   output_path=temp_dir, verbose=False, play=False)
    shutil.rmtree(temp_dir, ignore_errors=True)

    # Typical bot responses
    scenarios = [
        ("greeting", "你好！有什么可以帮你的？"),                           # ~11 chars
        ("short_reply", "好的，我已经帮你设置了明天早上八点的闹钟。"),          # ~18 chars
        ("medium_reply", "根据天气预报，明天上海晴转多云，气温在十五到二十三度之间，比较适合户外活动。建议穿一件薄外套。"),  # ~41 chars
        ("long_reply", "这个问题很好。简单来说，人工智能是通过大量数据训练的模型，它能够理解自然语言、生成文本和语音。目前最流行的方法是使用Transformer架构的大语言模型。"), # ~72 chars
    ]

    print(f"\n{'Scenario':<20} {'Chars':>5} {'Time(s)':>8} {'Audio(s)':>8} {'RTF':>6}")
    print(f"{'-'*20} {'-'*5} {'-'*8} {'-'*8} {'-'*6}")

    for name, text in scenarios:
        times = []
        audio_dur = 0
        for run in range(3):
            temp_dir = os.path.join(OUTPUT_DIR, f"temp_scenario_{name}_{run}")
            os.makedirs(temp_dir, exist_ok=True)

            t_start = time.time()
            generate_audio(model=model, text=text, voice="Vivian", speed=1.0, lang_code="zh",
                          output_path=temp_dir, verbose=False, play=False)
            elapsed = time.time() - t_start

            wav = find_output_wav(temp_dir)
            if wav:
                audio_dur = get_audio_duration(wav)
                times.append(elapsed)
            shutil.rmtree(temp_dir, ignore_errors=True)

        if times:
            avg = sum(times)/len(times)
            rtf = avg/audio_dur if audio_dur > 0 else 0
            verdict = "OK" if avg < 3.0 else "SLOW"
            print(f"{name:<20} {len(text):>5} {avg:>8.2f} {audio_dur:>8.2f} {rtf:>6.3f}  {verdict}")

    del model
    gc.collect()


if __name__ == "__main__":
    test_voice_cloning()
    test_streaming_latency()
    test_short_text_latency()
