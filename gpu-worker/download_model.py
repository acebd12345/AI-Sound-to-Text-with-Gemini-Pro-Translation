from faster_whisper import download_model
import os

print("Downloading model...")
# Download to local directory 'model'
model_path = download_model("large-v3-turbo", output_dir="model")
print(f"Model downloaded to {model_path}")
