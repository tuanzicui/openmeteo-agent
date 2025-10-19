# agent_adapter/main.py
import json, os, time, uuid, hashlib, threading
from typing import Any, Dict, Optional, List
from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel, Field, field_validator
import httpx

APP = FastAPI(title="OpenMeteo A2A Agent")
TASKS: Dict[str, Dict[str, Any]] = {}

def sha256(s: bytes) -> str:
    return hashlib.sha256(s).hexdigest()

AGENT_CARD = {
    "id": "agent:openmeteo:v1",
    "name": "OpenMeteo Agent",
    "version": "1.0.0",
    "owner": "your-org",
    "capabilities": ["data.fetch", "geo.monitor"],
    "modalities": ["json"],
    "auth": {"type": "api-key"},
    "endpoints": {"task":"POST /a2a/task","status":"GET /a2a/task/{id}"},
    "policies": {"network":"egress-allowlist","pii":"no-store","logs":"hash-only"},
    "schema": "https://a2a-protocol.org/schema/v1"
}

class ForecastInputs(BaseModel):
    latitude: float
    longitude: float
    hourly: Optional[List[str]] = None
    daily: Optional[List[str]] = None
    timezone: Optional[str] = "UTC"
    forecast_days: Optional[int] = Field(default=1, ge=1, le=16)
    past_days: Optional[int] = Field(default=0, ge=0, le=14)
    model: Optional[str] = None

    @field_validator("latitude")
    @classmethod
    def lat_range(cls, v):
        if v < -90 or v > 90:
            raise ValueError("latitude out of range [-90,90]")
        return v

    @field_validator("longitude")
    @classmethod
    def lon_range(cls, v):
        if v < -180 or v > 180:
            raise ValueError("longitude out of range [-180,180]")
        return v

class A2ATask(BaseModel):
    task_id: Optional[str] = None
    type: str
    inputs: ForecastInputs
    constraints: Optional[Dict[str, Any]] = None
    evidence: Optional[Any] = None
    callback: Optional[str] = None
    idempotency_key: Optional[str] = None

OPEN_METEO_BASE = "https://api.open-meteo.com/v1/forecast"

def build_query(i: ForecastInputs) -> Dict[str, Any]:
    q = {"latitude": i.latitude, "longitude": i.longitude,
         "timezone": i.timezone, "forecast_days": i.forecast_days,
         "past_days": i.past_days}
    if i.hourly: q["hourly"] = ",".join(i.hourly)
    if i.daily:  q["daily"]  = ",".join(i.daily)
    if i.model:  q["models"] = i.model
    return q

def request_open_meteo(query: Dict[str, Any], timeout_s: int = 10) -> Dict[str, Any]:
    backoff, last = 0.8, None
    for _ in range(2):
        try:
            with httpx.Client(timeout=timeout_s) as client:
                r = client.get(OPEN_METEO_BASE, params=query)
                if r.status_code == 200: return {"ok": True, "json": r.json()}
                last = f"status={r.status_code}, body={r.text[:800]}"
        except Exception as e:
            last = str(e)
        time.sleep(backoff); backoff *= 2
    return {"ok": False, "error": last}

def worker(task_id: str, payload: A2ATask):
    TASKS[task_id]["status"] = "working"
    timeout_ms = int((payload.constraints or {}).get("latency_ms", 20000))
    timeout_s = max(5, min(60, timeout_ms // 1000))
    query = build_query(payload.inputs)
    qhash = sha256(json.dumps(query, sort_keys=True).encode())
    TASKS[task_id]["idem"] = payload.idempotency_key or qhash
    res = request_open_meteo(query, timeout_s)
    if not res.get("ok"):
        TASKS[task_id].update({"status":"error","outputs":{"message":"upstream_failed","detail":res.get("error")}})
        return
    data = res["json"]
    summary = {
        "latitude": data.get("latitude"),
        "longitude": data.get("longitude"),
        "hourly_fields": list(data.get("hourly", {}).keys()) if data.get("hourly") else [],
        "daily_fields": list(data.get("daily", {}).keys()) if data.get("daily") else []
    }
    evidence = [{"type":"upstream.http","value":{"url":OPEN_METEO_BASE,"query_sha256":qhash,"timestamp":int(time.time())}}]
    TASKS[task_id].update({"status":"completed","outputs":{"summary":summary,"open_meteo":data},"evidence":evidence})

from fastapi.responses import JSONResponse

from fastapi import status
@APP.get("/a2a/agent-card")
def agent_card(): return AGENT_CARD

@APP.post("/a2a/task")
async def create_task(req: Request):
    if not (req.headers.get("authorization","").startswith("Bearer ")):
        raise HTTPException(status_code=401, detail="missing api-key")
    try:
        body = await req.json()
        task = A2ATask(**body)
    except Exception as e:
        # 新手友好：把错误当成 input_required 返回
        return JSONResponse(status_code=status.HTTP_200_OK, content={
            "status": "input_required",
            "outputs": {"error": f"{e}", "example": {
                "type": "weather.forecast",
                "inputs": {"latitude": 35.6762, "longitude": 139.6503, "hourly": ["temperature_2m"], "timezone": "Asia/Tokyo", "forecast_days": 1}
            }}
        })
    if task.type != "weather.forecast":
        return {"status":"input_required","outputs":{"expected_type":"weather.forecast"}}
    tid = task.task_id or str(uuid.uuid4())
    TASKS[tid] = {"status":"accepted","outputs":{},"evidence":[]}
    threading.Thread(target=worker, args=(tid, task), daemon=True).start()
    return {"task_id": tid, "status": "accepted"}

@APP.get("/a2a/task/{task_id}")
def get_task(task_id: str):
    if task_id not in TASKS: raise HTTPException(404, "no such task")
    return {"task_id": task_id, **TASKS[task_id]}

@APP.get("/healthz")
def healthz(): return {"ok": True}
