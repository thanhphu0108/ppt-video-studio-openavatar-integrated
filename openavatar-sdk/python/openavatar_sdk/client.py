import time
from pathlib import Path
import httpx

class OpenAvatarClient:
    def __init__(self, base_url="http://127.0.0.1:8008", timeout=180):
        self.base_url = base_url.rstrip("/")
        self.client = httpx.Client(timeout=timeout)

    def health(self):
        response = self.client.get(f"{self.base_url}/health")
        response.raise_for_status()
        return response.json()

    def generate_avatar(self, image, audio, engine="wav2lip"):
        with open(image, "rb") as img, open(audio, "rb") as aud:
            response = self.client.post(
                f"{self.base_url}/avatar/generate",
                files={"image": img, "audio": aud},
                data={"engine": engine}
            )
        response.raise_for_status()
        return response.json()

    def job(self, job_id):
        response = self.client.get(f"{self.base_url}/jobs/{job_id}")
        response.raise_for_status()
        return response.json()

    def wait(self, job_id, interval=1.0):
        while True:
            job = self.job(job_id)
            if job["status"] in {"completed", "failed", "cancelled"}:
                return job
            time.sleep(interval)

    def download(self, job_id, output):
        response = self.client.get(f"{self.base_url}/jobs/{job_id}/download")
        response.raise_for_status()
        path = Path(output)
        path.write_bytes(response.content)
        return path
