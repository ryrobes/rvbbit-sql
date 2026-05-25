"""Echo sidecar — Gradio variant.

A minimal Gradio app that exposes a transform function through
gr.Interface so the rvbbit `gradio` transport has something
deterministic to talk to. Mirrors sidecars/echo for parity.

Mounted on FastAPI via gr.mount_gradio_app — bypasses Gradio's
launch-time localhost reachability check that breaks inside containers.

Endpoints (Gradio attaches under /):
  POST /api/predict
  {"data": ["text", "upper"]}  → {"data": ["TEXT"], ...}
"""
from __future__ import annotations

from fastapi import FastAPI
import gradio as gr
import uvicorn


def transform(text: str, mode: str = "upper") -> str:
    mode = (mode or "upper").lower()
    if mode == "upper":
        return str(text).upper()
    if mode == "reverse":
        return str(text)[::-1]
    if mode == "length":
        return str(len(str(text)))
    return str(text)


demo = gr.Interface(
    fn=transform,
    inputs=[gr.Textbox(label="text"), gr.Textbox(label="mode", value="upper")],
    outputs=gr.Textbox(label="result"),
    title="rvbbit echo (gradio)",
    description="Deterministic transform sidecar for rvbbit's gradio transport.",
).queue(api_open=True)

app = FastAPI()
app = gr.mount_gradio_app(app, demo, path="/")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=7860)
