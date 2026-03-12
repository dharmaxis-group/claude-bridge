"""
Qwen3-TTS MLX Benchmark Script
Target: evaluate TTS latency for Telegram bot integration
M4 Max, 128GB RAM, macOS
"""
import time
import os
import sys
import json
import gc
import numpy as np

# Suppress warnings
os.environ["TOKENIZERS_PARALLELISM"] = "false"
import warnings
warnings.filterwarnings("ignore")

from mlx_audio.tts.utils import load_model
from mlx_audio.tts.generate import generate_audio
import soundfile as sf

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "outputs")
os.makedirs(OUTPUT_DIR, exist_ok=True)

# Test texts
TEXTS = {
    "short_22ch": "你好，我是你的智能助手，有什么可以帮你的吗？",
    "medium_54ch": "今天天气不错，适合出门散步。我建议你可以去公园走走，呼吸一下新鲜空气，放松一下心情。下午可能会有阵雨，记得带伞。",
    "long_200ch": (
        "人工智能技术正在深刻改变我们的生活方式。从智能语音助手到自动驾驶汽车，"
        "从医疗诊断到金融分析，AI的应用场景越来越广泛。在教育领域，个性化学习系统"
        "能够根据每个学生的特点制定专属学习计划。在医疗健康方面，AI辅助诊断系统已经"
        "能够识别多种疾病的早期征兆，为患者争取宝贵的治疗时间。未来，随着技术的不断"
        "进步，人工智能将会在更多领域发挥重要作用，推动社会的全面发展和进步。"
    ),
}

# Models to benchmark
MODELS = [
    {
        "name": "0.6B-CustomVoice-8bit",
        "hf_id": "mlx-community/Qwen3-TTS-12Hz-0.6B-CustomVoice-8bit",
        "mode": "custom",
    },
    {
        "name": "1.7B-CustomVoice-8bit",
        "hf_id": "mlx-community/Qwen3-TTS-12Hz-1.7B-CustomVoice-8bit",
        "mode": "custom",
    },
]

SAMPLE_RATE = 24000


def get_audio_duration(wav_path):
    """Get duration of a wav file in seconds."""
    data, sr = sf.read(wav_path)
    return len(data) / sr


def find_output_wav(output_dir):
    """Find the generated wav file in output directory."""
    for f in sorted(os.listdir(output_dir)):
        if f.endswith('.wav'):
            return os.path.join(output_dir, f)
    return None


def benchmark_model(model_info, num_warmup=1, num_runs=3):
    """Benchmark a single model with all test texts."""
    print(f"\n{'='*60}")
    print(f"Model: {model_info['name']}")
    print(f"{'='*60}")

    # Load model (measure cold start)
    print(f"\nLoading model...")
    t0 = time.time()
    model = load_model(model_info["hf_id"])
    load_time = time.time() - t0
    print(f"Model load time: {load_time:.2f}s")

    results = {"model": model_info["name"], "load_time_s": round(load_time, 2), "texts": {}}

    for text_key, text in TEXTS.items():
        char_count = len(text)
        print(f"\n--- {text_key} ({char_count} chars) ---")
        print(f"Text: {text[:60]}...")

        times = []
        audio_duration = 0

        for run_idx in range(num_warmup + num_runs):
            is_warmup = run_idx < num_warmup
            label = "WARMUP" if is_warmup else f"Run {run_idx - num_warmup + 1}"

            # Create temp output dir for this run
            run_output = os.path.join(OUTPUT_DIR, f"run_{model_info['name']}_{text_key}_{run_idx}")
            os.makedirs(run_output, exist_ok=True)

            t_start = time.time()

            try:
                generate_audio(
                    model=model,
                    text=text,
                    voice="Vivian",
                    instruct="",
                    speed=1.0,
                    lang_code="zh",
                    output_path=run_output,
                    verbose=False,
                    play=False,
                )
            except Exception as e:
                print(f"  [{label}] ERROR: {e}")
                continue

            t_end = time.time()
            elapsed = t_end - t_start

            # Find and measure output audio
            wav_path = find_output_wav(run_output)
            if wav_path:
                audio_duration = get_audio_duration(wav_path)
                rtf = elapsed / audio_duration if audio_duration > 0 else float('inf')

                print(f"  [{label}] {elapsed:.2f}s | audio: {audio_duration:.2f}s | RTF: {rtf:.3f}")

                if not is_warmup:
                    times.append(elapsed)

                # Keep last run's audio for quality check
                if run_idx == num_warmup + num_runs - 1:
                    final_wav = os.path.join(OUTPUT_DIR, f"{model_info['name']}_{text_key}.wav")
                    os.rename(wav_path, final_wav)
                    print(f"  Saved: {final_wav}")
            else:
                print(f"  [{label}] {elapsed:.2f}s | NO AUDIO OUTPUT")

            # Clean up temp dir
            import shutil
            shutil.rmtree(run_output, ignore_errors=True)

        if times:
            avg_time = sum(times) / len(times)
            min_time = min(times)
            max_time = max(times)
            rtf_avg = avg_time / audio_duration if audio_duration > 0 else float('inf')

            results["texts"][text_key] = {
                "char_count": char_count,
                "avg_time_s": round(avg_time, 3),
                "min_time_s": round(min_time, 3),
                "max_time_s": round(max_time, 3),
                "audio_duration_s": round(audio_duration, 2),
                "rtf_avg": round(rtf_avg, 3),
                "chars_per_sec": round(char_count / avg_time, 1),
            }

            print(f"\n  Summary: avg={avg_time:.2f}s min={min_time:.2f}s max={max_time:.2f}s RTF={rtf_avg:.3f}")

    # Cleanup model
    del model
    gc.collect()

    return results


def main():
    print("Qwen3-TTS MLX Benchmark")
    print(f"Platform: macOS Apple Silicon")
    print(f"Output: {OUTPUT_DIR}")
    print(f"Test texts: {len(TEXTS)}")

    # Print char counts
    for key, text in TEXTS.items():
        print(f"  {key}: {len(text)} chars")

    all_results = []

    for model_info in MODELS:
        try:
            result = benchmark_model(model_info, num_warmup=1, num_runs=3)
            all_results.append(result)
        except Exception as e:
            print(f"\nFAILED: {model_info['name']}: {e}")
            import traceback
            traceback.print_exc()

    # Summary table
    print(f"\n\n{'='*80}")
    print("BENCHMARK SUMMARY")
    print(f"{'='*80}")
    print(f"{'Model':<30} {'Text':<15} {'Chars':>5} {'Time(s)':>8} {'Audio(s)':>8} {'RTF':>6} {'ch/s':>6}")
    print(f"{'-'*30} {'-'*15} {'-'*5} {'-'*8} {'-'*8} {'-'*6} {'-'*6}")

    for r in all_results:
        for text_key, data in r["texts"].items():
            print(f"{r['model']:<30} {text_key:<15} {data['char_count']:>5} "
                  f"{data['avg_time_s']:>8.2f} {data['audio_duration_s']:>8.2f} "
                  f"{data['rtf_avg']:>6.3f} {data['chars_per_sec']:>6.1f}")

    # Save results
    results_path = os.path.join(OUTPUT_DIR, "benchmark_results.json")
    with open(results_path, "w") as f:
        json.dump(all_results, f, indent=2, ensure_ascii=False)
    print(f"\nResults saved to: {results_path}")

    # Key metric for Telegram bot
    print(f"\n{'='*80}")
    print("TARGET ASSESSMENT: < 3s for ~200 chars Chinese text")
    print(f"{'='*80}")
    for r in all_results:
        long_data = r["texts"].get("long_200ch")
        if long_data:
            verdict = "PASS" if long_data["avg_time_s"] < 3.0 else "FAIL"
            print(f"  {r['model']}: {long_data['avg_time_s']:.2f}s -> {verdict}")


if __name__ == "__main__":
    main()
