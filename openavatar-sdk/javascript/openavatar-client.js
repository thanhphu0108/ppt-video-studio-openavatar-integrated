export class OpenAvatarClient {
  constructor(baseUrl = "http://127.0.0.1:8008") {
    this.baseUrl = baseUrl.replace(/\/$/, "");
  }

  async health() {
    const response = await fetch(`${this.baseUrl}/health`);
    if (!response.ok) throw new Error(await response.text());
    return response.json();
  }

  async generateAvatar(imageFile, audioFile, engine = "wav2lip") {
    const form = new FormData();
    form.append("image", imageFile);
    form.append("audio", audioFile);
    form.append("engine", engine);

    const response = await fetch(`${this.baseUrl}/avatar/generate`, {
      method: "POST",
      body: form
    });
    if (!response.ok) throw new Error(await response.text());
    return response.json();
  }

  async getJob(jobId) {
    const response = await fetch(`${this.baseUrl}/jobs/${jobId}`);
    if (!response.ok) throw new Error(await response.text());
    return response.json();
  }

  async wait(jobId, intervalMs = 1000) {
    while (true) {
      const job = await this.getJob(jobId);
      if (["completed", "failed", "cancelled"].includes(job.status)) {
        return job;
      }
      await new Promise(resolve => setTimeout(resolve, intervalMs));
    }
  }

  downloadUrl(jobId) {
    return `${this.baseUrl}/jobs/${jobId}/download`;
  }
}
