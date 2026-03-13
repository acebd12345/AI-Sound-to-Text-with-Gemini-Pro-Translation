from faster_whisper import download_model
import os

print("Downloading Whisper model...")
# Download to local directory 'model'
model_path = download_model("large-v3-turbo", output_dir="model")
print(f"Model downloaded to {model_path}")

# Pre-download pyannote models (requires HF_TOKEN)
hf_token = os.environ.get("HF_TOKEN", "")
if hf_token:
    print("Downloading pyannote speaker-diarization-3.1 model...")
    try:
        from pyannote.audio import Pipeline
        pipeline = Pipeline.from_pretrained(
            "pyannote/speaker-diarization-3.1",
            token=hf_token
        )
        print("pyannote model downloaded successfully!")
    except Exception as e:
        print(f"pyannote model download failed (diarization will be unavailable): {e}")
else:
    print("HF_TOKEN not set, skipping pyannote model download.")
    print("Speaker diarization will download models at runtime if HF_TOKEN is available.")
