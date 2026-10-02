# -*- coding: utf-8 -*-
"""
e2e_test.py — full end-to-end test of the CareVoice pipeline using REAL synthesized
speech (edge-tts neural voices) in English, Hindi, and Kannada.

For each phrase:
  1. Synthesize speech (edge-tts) -> mp3
  2. Transcode to 16kHz mono WAV (ffmpeg if present, else pydub/audioread fallback)
  3. POST to the running server /audio/ingest
  4. Record transcript, intent, priority, should_alert, and round-trip latency

Writes a full report to e2e_result.txt.
"""
import asyncio, time, io, wave, struct, sys
from pathlib import Path

import edge_tts
import requests

SERVER = "http://localhost:8000"
# seed password is 5432 (set by earlier rehash)
PATIENT_USER, PATIENT_PW = "patient_raj", "5432"   # room 4B

VOICES = {
    "en": "en-US-AriaNeural",
    "hi": "hi-IN-SwaraNeural",
    "kn": "kn-IN-SapnaNeural",
}

# (language, phrase, expected_intent_family, expected_priority)
CASES = [
    ("en", "I have severe chest pain and I cannot breathe", "Emergency", "Critical"),
    ("en", "I have a lot of pain in my knee",               "Pain",      "Urgent"),
    ("en", "Can I please get a glass of water",             "Food/Water","Routine"),
    ("hi", "मुझे साँस नहीं आ रही है मदद करो",                 "Emergency", "Critical"),
    ("hi", "मेरे घुटने में बहुत दर्द है",                     "Pain",      "Urgent"),
    ("hi", "मुझे पानी चाहिए",                                "Food/Water","Routine"),
    ("kn", "ನನಗೆ ಉಸಿರಾಡಲು ಆಗುತ್ತಿಲ್ಲ ಸಹಾಯ ಮಾಡಿ",             "Emergency", "Critical"),
    ("kn", "ನನ್ನ ತಲೆ ತುಂಬಾ ನೋಯುತ್ತಿದೆ",                       "Pain",      "Urgent"),
    ("kn", "ನನಗೆ ನೀರು ಬೇಕು",                                 "Food/Water","Routine"),
]


async def synth(text, voice, out_mp3):
    await edge_tts.Communicate(text, voice).save(out_mp3)


def mp3_to_wav16k(mp3_path, wav_path):
    """Transcode mp3 -> 16kHz mono 16-bit WAV using PyAV."""
    import av
    container = av.open(mp3_path)
    resampler = av.audio.resampler.AudioResampler(format="s16", layout="mono", rate=16000)
    pcm = bytearray()
    for frame in container.decode(audio=0):
        for rf in resampler.resample(frame):
            pcm += bytes(rf.to_ndarray().tobytes())
    with wave.open(wav_path, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
        w.writeframes(bytes(pcm))


def main():
    lines = []
    # Login as patient (room 4B) to get a token
    r = requests.post(f"{SERVER}/auth/login",
                      data={"username": PATIENT_USER, "password": PATIENT_PW}, timeout=10)
    if r.status_code != 200:
        Path(__file__).with_name("e2e_result.txt").write_text(
            f"LOGIN FAILED {r.status_code}: {r.text}", encoding="utf-8")
        return
    token = r.json()["access_token"]
    room  = r.json().get("room_number") or "4B"
    headers = {"Authorization": f"Bearer {token}"}

    tmp = Path(__file__).parent / "_tts_tmp"
    tmp.mkdir(exist_ok=True)

    correct_intent = correct_prio = 0
    for i, (lang, phrase, exp_intent, exp_prio) in enumerate(CASES):
        mp3 = str(tmp / f"c{i}.mp3"); wav = str(tmp / f"c{i}.wav")
        asyncio.run(synth(phrase, VOICES[lang], mp3))
        mp3_to_wav16k(mp3, wav)
        with open(wav, "rb") as f:
            wav_bytes = f.read()

        t0 = time.time()
        resp = requests.post(f"{SERVER}/audio/ingest", headers=headers,
                             files={"audio": ("rec.wav", wav_bytes, "audio/wav")},
                             data={"room_id": room}, timeout=30)
        dt = time.time() - t0
        if resp.status_code not in (200, 201):
            lines.append(f"[{lang}] HTTP {resp.status_code}: {resp.text[:80]}")
            continue
        d = resp.json()
        got_intent = d.get("intent", "?")
        got_prio   = d.get("priority", "?")
        tx         = d.get("transcript", "")
        ci = "OK" if got_intent == exp_intent else "XX"
        cp = "OK" if got_prio == exp_prio else "XX"
        correct_intent += (got_intent == exp_intent)
        correct_prio   += (got_prio == exp_prio)
        lines.append(
            f"[{lang}] {dt:4.2f}s  intent {ci} exp={exp_intent:11s} got={got_intent:11s}  "
            f"prio {cp} exp={exp_prio:8s} got={got_prio:8s}\n"
            f"        said : {phrase}\n"
            f"        heard: {tx}"
        )

    n = len(CASES)
    lines.append("")
    lines.append(f"Intent accuracy:   {correct_intent}/{n}")
    lines.append(f"Priority accuracy: {correct_prio}/{n}")
    Path(__file__).with_name("e2e_result.txt").write_text("\n".join(lines), encoding="utf-8")
    print("done")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        Path(__file__).with_name("e2e_result.txt").write_text(
            "CRASH:\n" + traceback.format_exc(), encoding="utf-8")
        print("crashed")
