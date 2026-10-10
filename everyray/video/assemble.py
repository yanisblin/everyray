"""Assemble the EveryRay pitch video from narration clips + branded slide frames."""
import subprocess
import json
from pathlib import Path

VIDEO_DIR = Path(__file__).parent
AUDIO_DIR = VIDEO_DIR / "audio"
FRAMES_DIR = VIDEO_DIR / "frames"
OUT = VIDEO_DIR / "everyray_pitch.mp4"

W, H = 1280, 720
TRANSITION = 0.6
LEAD_IN = 1.0
GAP = 0.65
END_HOLD = 2.5

NARRATION_DURATIONS = {
    "s1": 6.241587, "s2": 6.480635, "s3": 5.146485,
    "s4": 6.782494, "s5": 6.243810, "s6": 2.360045,
}
SCENES = ["s1", "s2", "s3", "s4", "s5", "s6"]

# --- 1. build the padded audio track (silence + narration + silence) -------
audio_filter_inputs = []
audio_concat_parts = []
inputs = []
idx = 0

def add_silence(dur):
    global idx
    inputs.append(("silence", dur))
    audio_concat_parts.append(f"[{idx}:a]")
    idx += 1

def add_clip(name):
    global idx
    inputs.append(("clip", AUDIO_DIR / f"{name}.wav"))
    audio_concat_parts.append(f"[{idx}:a]")
    idx += 1

add_silence(LEAD_IN)
for i, s in enumerate(SCENES):
    add_clip(s)
    if i < len(SCENES) - 1:
        add_silence(GAP)
add_silence(END_HOLD)

cmd = ["ffmpeg", "-y"]
for kind, val in inputs:
    if kind == "silence":
        cmd += ["-f", "lavfi", "-t", str(val), "-i", "anullsrc=r=44100:cl=stereo"]
    else:
        cmd += ["-i", str(val)]
concat_filter = "".join(audio_concat_parts) + f"concat=n={len(inputs)}:v=0:a=1[aout]"
cmd += ["-filter_complex", concat_filter, "-map", "[aout]", str(AUDIO_DIR / "narration_padded.wav")]
subprocess.run(cmd, check=True)

# --- 2. compute scene start times & hold durations --------------------------
t = LEAD_IN
starts = []
for i, s in enumerate(SCENES):
    starts.append(t)
    t += NARRATION_DURATIONS[s]
    if i < len(SCENES) - 1:
        t += GAP
total_duration = t + END_HOLD

holds = []
for i in range(len(SCENES)):
    nxt = starts[i + 1] if i + 1 < len(SCENES) else total_duration
    holds.append(nxt - starts[i])

print("Scene starts:", starts)
print("Scene holds:", holds)
print("Total duration:", total_duration)

# --- 3. build the video: looped images + xfade chain -------------------------
cmd = ["ffmpeg", "-y"]
for i, s in enumerate(SCENES):
    dur = holds[i] + (TRANSITION if i < len(SCENES) - 1 else 0)
    cmd += ["-loop", "1", "-t", f"{dur:.3f}", "-i", str(FRAMES_DIR / f"scene{i+1}.jpg")]
cmd += ["-i", str(AUDIO_DIR / "narration_padded.wav")]

filter_parts = []
for i, s in enumerate(SCENES):
    filter_parts.append(
        f"[{i}:v]scale={W}:{H}:force_original_aspect_ratio=decrease,"
        f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=0xEDE6D6,setsar=1,fps=30[v{i}]"
    )

chain = "v0"
cum = holds[0]
for i in range(1, len(SCENES)):
    out_label = f"vx{i}" if i < len(SCENES) - 1 else "vout"
    filter_parts.append(
        f"[{chain}][v{i}]xfade=transition=fade:duration={TRANSITION}:offset={cum:.3f}[{out_label}]"
    )
    chain = out_label
    cum += holds[i]

filter_complex = ";".join(filter_parts)
cmd += ["-filter_complex", filter_complex, "-map", f"[{chain}]",
        "-map", f"{len(SCENES)}:a",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
        "-shortest", str(OUT)]
subprocess.run(cmd, check=True)
print("Wrote", OUT)
