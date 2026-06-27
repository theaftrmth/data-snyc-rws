"""
নির্ভরতা (একবার ইনস্টল করো):
  pip install noisereduce soundfile numpy scipy
"""

import subprocess
import sys
import os
import tempfile
import numpy as np

def find_ffmpeg():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    for name in ("ffmpeg.exe", "ffmpeg"):
        local = os.path.join(script_dir, name)
        if os.path.exists(local):
            return local
    return "ffmpeg"

def extract_audio(input_file, wav_path, ffmpeg):
    """ভিডিও থেকে audio বের করো WAV হিসেবে"""
    subprocess.run([
        ffmpeg, "-i", input_file,
        "-vn", "-acodec", "pcm_f32le",
        "-ar", "48000",
        wav_path, "-y", "-loglevel", "error"
    ], check=True)

def merge_audio(input_video, wav_path, output_file, ffmpeg):
    """processed audio ভিডিওতে মেশাও + aftercare"""
    subprocess.run([
        ffmpeg,
        "-i", input_video,
        "-i", wav_path,
        "-c:v", "copy",
        "-map", "0:v:0", "-map", "1:a:0",
        "-af", (
            "equalizer=f=200:t=o:w=1:g=2,"          # warmth ফেরানো — noise reduce কেটেছিল
            "acompressor=threshold=-18dB:ratio=1.5:attack=20:release=200:knee=6dB,"
            "alimiter=limit=0.75:level=false"         # আগে 0.891 → AAC inter-sample peak ক্লিপ করতো
        ),
        "-c:a", "aac", "-b:a", "320k",
        output_file, "-y", "-loglevel", "error"
    ], check=True)

def highpass_filter(audio, rate, cutoff=80):
    """Low rumble কাটো"""
    from scipy import signal
    b, h = signal.butter(4, cutoff / (rate / 2), btype='high')
    if audio.ndim == 1:
        return signal.filtfilt(b, h, audio)
    return np.stack([signal.filtfilt(b, h, audio[:, ch])
                     for ch in range(audio.shape[1])], axis=1)

def peak_normalize(audio, target_db=-1.0):
    """
    Audacity-র 'Normalize peak amplitude to -1.0 dB'-এর মতো।
    সর্বোচ্চ peak ধরে পুরো audio uniformly তোলো।
    """
    target_linear = 10 ** (target_db / 20.0)
    max_peak = np.max(np.abs(audio))
    if max_peak == 0:
        return audio
    return audio * (target_linear / max_peak)

def process(input_file, output_file, target_db=-6.0):
    try:
        import soundfile as sf
        import noisereduce as nr
    except ImportError as exc:
        raise ImportError(
            "fix_audio deps missing. Run: pip install noisereduce soundfile numpy scipy"
        ) from exc

    if not os.path.exists(input_file):
        raise FileNotFoundError(f"fix_audio: input file not found: {input_file}")

    ffmpeg = find_ffmpeg()

    print(f"\n{'='*52}")
    print(f"  Input  : {input_file}")
    print(f"  Output : {output_file}")
    print(f"{'='*52}")

    with tempfile.TemporaryDirectory() as tmp:
        wav_in  = os.path.join(tmp, "input.wav")
        wav_out = os.path.join(tmp, "output.wav")

        # Step 1: audio বের করো
        print("\n[1/4] Audio extract করছি...")
        extract_audio(input_file, wav_in, ffmpeg)

        audio, rate = sf.read(wav_in, dtype='float32')
        print(f"  Sample rate : {rate} Hz")
        print(f"  Channels    : {'Stereo' if audio.ndim > 1 else 'Mono'}")

        # Step 2: Highpass (low rumble কাটো)
        print("\n[2/4] Highpass filter (80 Hz)...")
        audio = highpass_filter(audio, rate, cutoff=80)

        # Step 3: Noise reduction — stationary mode, no manual profile needed।
        # পুরো audio-র statistics থেকে noise floor নিজেই বোঝে।
        # y_noise দিলে ভুল section noise profile হিসেবে ঢুকে voice কেটে যায়।
        print("\n[3/4] Noise reduction করছি...")

        def denoise(a):
            if a.ndim == 1:
                return nr.reduce_noise(
                    y=a, sr=rate,
                    prop_decrease=0.35,  # কম aggressive → voice harmonic বাঁচে
                    stationary=True,
                    n_fft=2048
                )
            channels = []
            for ch in range(a.shape[1]):
                channels.append(nr.reduce_noise(
                    y=a[:, ch], sr=rate,
                    prop_decrease=0.35,
                    stationary=True,
                    n_fft=2048
                ))
            return np.stack(channels, axis=1)

        print("  Pass 1...")
        audio = denoise(audio)

        # Step 4: Peak normalize — target adjustable (default -6 dBFS)
        print(f"\n[4/4] Peak normalize করছি ({target_db} dBFS)...")
        max_before = np.max(np.abs(audio))
        audio = peak_normalize(audio, target_db=target_db)
        boost_db = 20 * np.log10(10 ** (target_db / 20) / max_before) if max_before > 0 else 0
        print(f"  Boost applied : +{boost_db:.1f} dB  →  peak = {target_db} dBFS")

        # WAV লেখো ও ভিডিওতে মেশাও
        sf.write(wav_out, audio.astype(np.float32), rate, subtype='FLOAT')
        print("\n  ভিডিওতে মেশাচ্ছি...")
        merge_audio(input_file, wav_out, output_file, ffmpeg)

    print(f"\n✅ Done → {output_file}\n")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("ব্যবহার:")
        print("  python fix_audio.py input.mp4 [output.mp4] [peak_dBFS]")
        print("  peak_dBFS default: -6  (কম = নরম ও clean, বেশি = loud)")
        sys.exit(0)

    inp        = sys.argv[1]
    out        = sys.argv[2] if len(sys.argv) > 2 else f"fixed_{os.path.basename(inp)}"
    target_db  = float(sys.argv[3]) if len(sys.argv) > 3 else -6.0
    process(inp, out, target_db)
