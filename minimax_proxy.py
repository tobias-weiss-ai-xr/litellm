import json
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
import uvicorn

app = FastAPI()
VLLM_URL = "http://192.168.0.27:8000/v1"
MODEL_NAME = "deepseek-v4-flash"


@app.post("/v1/chat/completions")
async def chat(request: Request):
    body = await request.json()
    streaming = body.get("stream", False)
    body["model"] = MODEL_NAME

    async with httpx.AsyncClient(timeout=600.0) as client:
        resp = await client.post(f"{VLLM_URL}/chat/completions", json=body)

        if not streaming:
            data = resp.json()
            for choice in data.get("choices", []):
                msg = choice.get("message", {})
                if msg.get("content") is None and msg.get("reasoning"):
                    msg["content"] = msg["reasoning"]
            return JSONResponse(content=data)
        else:
            async def stream_forward():
                buffer = ""
                async for chunk in resp.aiter_bytes():
                    decoded = chunk.decode("utf-8", errors="replace")
                    buffer += decoded
                    while "\n\n" in buffer:
                        event, buffer = buffer.split("\n\n", 1)
                        for line in event.split("\n"):
                            if line.startswith("data: "):
                                data_str = line[6:]
                                if data_str.strip() == "[DONE]":
                                    yield "data: [DONE]\n\n"
                                    return
                                try:
                                    data_obj = json.loads(data_str)
                                    if "choices" in data_obj:
                                        for choice in data_obj["choices"]:
                                            delta = choice.get("delta", {})
                                            if delta.get("content") is None and delta.get("reasoning"):
                                                delta["content"] = delta["reasoning"]
                                    yield f"data: {json.dumps(data_obj)}\n\n"
                                except json.JSONDecodeError:
                                    yield decoded
                if buffer.strip():
                    yield buffer

            return StreamingResponse(
                stream_forward(),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
            )


@app.get("/v1/models")
async def models():
    return JSONResponse(content={
        "object": "list",
        "data": [{"id": "deepseek-v4-flash", "object": "model", "created": 0, "owned_by": "local"}]
    })


@app.get("/health")
async def health():
    return {"status": "ok"}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8001)
